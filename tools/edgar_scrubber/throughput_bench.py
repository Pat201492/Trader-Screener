"""
Throughput benchmark (issue #108, part of #95) -- replace every performance
number in #95/#103/#107/#111 with a MEASUREMENT on the actual target GPU.

Every existing number is derived from hardware characteristics, not run:
`hardware_probe.py`'s KV-cache sizing is arithmetic, and #111's "~8x token
reduction" and "the run becomes decode-bound" are predictions. This module
is the harness that settles them -- a 20-document run against a live Ollama
server, across the matrix the issue specifies:

    model:          qwen2.5:3b-q4_K_M, qwen2.5:7b-q4_K_M
    batch:           1, 4, 8, 16
    KV quant:        fp16, q8_0
    routing:         whole_section vs sub_block (#101)
    preprocessing:   raw table markup vs flattened pairs (#101)

Per cell: measured prefill tok/s, decode tok/s, tokens/doc, seconds/doc, peak
VRAM. Per-field accuracy is NOT computed here -- that is `eval_harness.py`'s
job -- but `compare_accuracy_tradeoff` ties the two together, because a cell
that is 2x faster and 20% worse on `estimated_value_per_1000` is not faster.

Two axes are NOT requestable per-call and the harness does not pretend
otherwise: KV-cache quantization and true concurrent batching are Ollama
SERVER-startup settings (`config.py`'s `verify_server_runtime` already
measures this the hard way -- exporting `OLLAMA_KV_CACHE_TYPE` from a client
process cannot reach a server already running). `run_matrix` groups cells by
`kv_quant` and calls `on_kv_quant_change` once per group so a caller can
pause, restart the Ollama service with the right env var, and verify it
actually took before trusting the numbers underneath it.

`/api/generate` (Ollama's NATIVE endpoint), not the OpenAI-compatible
`/v1/chat/completions` `ollama_client.py` uses everywhere else, is what this
module calls -- it is the one endpoint that reports `prompt_eval_count`,
`prompt_eval_duration`, `eval_count`, `eval_duration`, the fields prefill/
decode tok/s are computed from. Same low-level pattern `config.
verify_server_runtime` already uses for the same reason.

stdlib only for the harness itself; `requests` only inside the default
(non-injected) transport. Run the self-check (fake transport, no network,
no GPU):  python tools/edgar_scrubber/throughput_bench.py
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as _dc_field

try:  # package import: tools.edgar_scrubber.throughput_bench
    from .extraction_ladder import build_messages
    from .hardware_probe import get_nvidia_vram
    from .reduce import sub_block
    from .validation import build_field_context
except ImportError:  # standalone: python tools/edgar_scrubber/throughput_bench.py
    from extraction_ladder import build_messages
    from hardware_probe import get_nvidia_vram
    from reduce import sub_block
    from validation import build_field_context

DEFAULT_MODELS = ("qwen2.5:3b-instruct-q4_K_M", "qwen2.5:7b-instruct-q4_K_M")
DEFAULT_BATCHES = (1, 4, 8, 16)
DEFAULT_KV_QUANTS = ("fp16", "q8_0")
DEFAULT_ROUTING = ("whole_section", "sub_block")
DEFAULT_PREPROCESSING = ("raw_table", "flattened_pairs")
SUB_BLOCK_WINDOW = 500  # matches reduce.py stage 4's default warm-path window


# --------------------------------------------------------------------------- #
# Prompt construction -- the routing x preprocessing axes, built on #101's
# real reduction primitives so the benchmark measures the text the ladder
# would really send, not a stand-in.
# --------------------------------------------------------------------------- #

@dataclass
class _TextDoc:
    """`reduce.sub_block` wants a `.text`/`.offset_map` object; a
    `build_field_context` result is a bare `(text, map)` tuple. This is that
    tuple wrapped back into the shape sub_block needs -- nothing else."""
    text: str
    offset_map: object


def _first_anchor_offset(text, anchors):
    """Text offset of the first field anchor found in `text` (case-
    insensitive), or None. The warm-path stand-in: #107 induces a real
    anchor from validated spans, but the benchmark only needs A window
    center, not the induction logic itself."""
    low = text.lower()
    best = None
    for a in anchors or ():
        key = a.lower().rstrip(": ").strip()
        if not key:
            continue
        i = low.find(key)
        if i >= 0 and (best is None or i < best):
            best = i
    return best


def build_prompt_text(field_def, render_doc, sections, *, routing, preprocessing,
                      sub_block_window=SUB_BLOCK_WINDOW):
    """The SOURCE TEXT segment of a rung-3 prompt for one field, at one
    (routing, preprocessing) cell.

    routing:
      whole_section -- every section the field's spec routes into (#101
                        stage 3, the cold path) -- `validation.
                        build_field_context`.
      sub_block      -- +/-`sub_block_window` chars around the field's first
                        matched anchor within that section text (#101 stage
                        4, the warm path #107 unlocks once an anchor is
                        known).

    preprocessing:
      flattened_pairs -- the text as `normalize_html` already produces it:
                          every table pre-parsed into `label: value` lines
                          (#101 stage 1). This is what `render_doc.text`
                          already contains -- no separate document to build.
      raw_table        -- the ORIGINAL HTML markup covering the exact same
                          window, via `render_doc.source` and the window's
                          resolved source span -- what stage 1 exists to
                          avoid sending, and the thing #111's token-cost
                          claim is measured against.
    """
    ctx_text, ctx_map = build_field_context(render_doc, sections, field_def)

    if routing == "whole_section":
        window_text, src_span = ctx_text, ctx_map.resolve(0, len(ctx_text))
    elif routing == "sub_block":
        anchor = _first_anchor_offset(ctx_text, field_def.anchors)
        block = sub_block(_TextDoc(ctx_text, ctx_map),
                          text_offset=anchor if anchor is not None else 0,
                          window=sub_block_window)
        window_text, src_span = block.text, block.source_span
    else:
        raise ValueError(f"unknown routing {routing!r}")

    if preprocessing == "flattened_pairs":
        return window_text
    if preprocessing == "raw_table":
        if src_span is None:
            return window_text  # nothing resolved -- fall back rather than fail the cell
        return render_doc.source[src_span[0]:src_span[1]]
    raise ValueError(f"unknown preprocessing {preprocessing!r}")


def _flatten_messages(messages):
    """`/api/generate` takes one `prompt` string; `build_messages` (#104)
    returns chat messages. Concatenating role-tagged content is an
    approximation of the real chat template -- close enough for a token/
    throughput measurement, which is what this harness reports, not exact
    per-model formatting."""
    return "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)


def build_prompt(field_def, render_doc, sections, *, routing, preprocessing,
                 sub_block_window=SUB_BLOCK_WINDOW, exemplars=None):
    text = build_prompt_text(field_def, render_doc, sections, routing=routing,
                             preprocessing=preprocessing, sub_block_window=sub_block_window)
    return _flatten_messages(build_messages(field_def, text, exemplars=exemplars))


# --------------------------------------------------------------------------- #
# Axes + one cell's measurement
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BenchAxes:
    model: str
    batch: int
    kv_quant: str        # "fp16" | "q8_0"
    routing: str          # "whole_section" | "sub_block"
    preprocessing: str    # "raw_table" | "flattened_pairs"

    def cell_id(self):
        return (f"{self.model}|batch={self.batch}|kv={self.kv_quant}|"
                f"route={self.routing}|prep={self.preprocessing}")


def matrix_axes(models=DEFAULT_MODELS, batches=DEFAULT_BATCHES, kv_quants=DEFAULT_KV_QUANTS,
                routing=DEFAULT_ROUTING, preprocessing=DEFAULT_PREPROCESSING):
    """Every cell of the matrix, `kv_quant` OUTERMOST -- it is a server
    restart between values, everything else is a same-server request-time
    knob, so grouping this way is what makes `on_kv_quant_change` in
    `run_matrix` a single pause point per quant value, not one per cell."""
    for kv in kv_quants:
        for model in models:
            for batch in batches:
                for route in routing:
                    for prep in preprocessing:
                        yield BenchAxes(model=model, batch=batch, kv_quant=kv,
                                        routing=route, preprocessing=prep)


@dataclass
class BenchSample:
    """One `/api/generate` call's measured timing -- Ollama's native fields,
    which the OpenAI-compatible endpoint used everywhere else in this
    scrubber does not expose."""
    prompt_tokens: int
    prompt_duration_ns: int
    decode_tokens: int
    decode_duration_ns: int

    @property
    def prefill_tok_s(self):
        return self.prompt_tokens / (self.prompt_duration_ns / 1e9) if self.prompt_duration_ns else 0.0

    @property
    def decode_tok_s(self):
        return self.decode_tokens / (self.decode_duration_ns / 1e9) if self.decode_duration_ns else 0.0

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.decode_tokens

    @property
    def total_seconds(self):
        return (self.prompt_duration_ns + self.decode_duration_ns) / 1e9


@dataclass
class CellResult:
    axes: BenchAxes
    n_calls: int
    prefill_tok_s: float
    decode_tok_s: float
    tokens_per_doc: float
    seconds_per_doc: float
    prefill_seconds_per_doc: float
    decode_seconds_per_doc: float
    peak_vram_gb: float
    baseline_vram_gb: float
    # WALL-CLOCK seconds per document across the whole cell. `seconds_per_doc`
    # above is the mean of per-request SERVER durations, which cannot show a
    # batch win: four concurrent requests each taking 1s still average 1s, while
    # the wall clock shows ~1s for four docs. Without this the batch axis stays
    # unmeasurable even once dispatch is genuinely concurrent.
    wall_seconds_per_doc: float = 0.0
    accuracy: dict = None   # optional {field: FieldMetrics.as_dict()} from eval_harness

    @property
    def decode_bound(self):
        """#111 predicts the run becomes decode-bound once prompts shrink:
        decode WALL-CLOCK TIME dominating (not prefill tok/s vs decode
        tok/s, which are never directly comparable -- prefill and decode
        rates differ by design) is the measured signal that prediction is
        checked against."""
        return self.decode_seconds_per_doc > self.prefill_seconds_per_doc

    def as_dict(self):
        return {
            "cell_id": self.axes.cell_id(), "model": self.axes.model, "batch": self.axes.batch,
            "kv_quant": self.axes.kv_quant, "routing": self.axes.routing,
            "preprocessing": self.axes.preprocessing, "n_calls": self.n_calls,
            "prefill_tok_s": self.prefill_tok_s, "decode_tok_s": self.decode_tok_s,
            "tokens_per_doc": self.tokens_per_doc, "seconds_per_doc": self.seconds_per_doc,
            "prefill_seconds_per_doc": self.prefill_seconds_per_doc,
            "decode_seconds_per_doc": self.decode_seconds_per_doc,
            "decode_bound": self.decode_bound,
            "peak_vram_gb": self.peak_vram_gb, "baseline_vram_gb": self.baseline_vram_gb,
            "accuracy": self.accuracy,
        }


def _default_generate(base_url, payload, *, timeout=300):
    import requests
    resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _default_vram_sampler():
    return get_nvidia_vram()  # (total_gb, free_gb)


def run_cell(axes, prompts, *, generate_fn, vram_sampler=_default_vram_sampler,
            num_ctx=8192, num_predict=512):
    """Run every prompt in `prompts` through one (model, batch, kv_quant,
    routing, preprocessing) cell, `axes.batch` at a time, and average the
    measured native timing fields. `generate_fn(payload) -> dict` is
    injectable (a fake in tests; `_default_generate` bound to a live
    `base_url` in real use) -- Ollama's request-level "batch" is concurrent
    in-flight requests against the server's configured parallel slots, not a
    single batched call, so this sends `axes.batch` requests per chunk
    rather than one call carrying `axes.batch` prompts.
    """
    if not prompts:
        raise ValueError("run_cell: no prompts given")

    total0, free0 = vram_sampler()
    baseline_used = total0 - free0
    peak_used = baseline_used

    def _one(p):
        payload = {"model": axes.model, "prompt": p, "stream": False,
                   "options": {"num_ctx": num_ctx, "num_predict": num_predict}}
        resp = generate_fn(payload)
        return BenchSample(
            prompt_tokens=resp.get("prompt_eval_count", 0),
            prompt_duration_ns=resp.get("prompt_eval_duration", 0),
            decode_tokens=resp.get("eval_count", 0),
            decode_duration_ns=resp.get("eval_duration", 0),
        )

    # The chunk is dispatched CONCURRENTLY. It used to loop `for p in chunk`
    # sequentially, which made `axes.batch` a no-op: batch=1/4/8/16 produced
    # statistically identical timings for every run of this harness, since batch
    # only changed how often VRAM was sampled. Ollama's request-level batch is
    # concurrent in-flight requests against its parallel slots, so measuring it
    # requires actually firing them at once.
    samples = []
    wall0 = time.perf_counter()
    for i in range(0, len(prompts), axes.batch):
        chunk = prompts[i:i + axes.batch]
        if axes.batch == 1:
            chunk_samples = [_one(chunk[0])]
        else:
            with ThreadPoolExecutor(max_workers=axes.batch) as pool:
                chunk_samples = list(pool.map(_one, chunk))
        samples.extend(chunk_samples)
        total, free = vram_sampler()
        peak_used = max(peak_used, total - free)
    wall_seconds = time.perf_counter() - wall0

    n = len(samples)
    mean_prefill = sum(s.prefill_tok_s for s in samples) / n
    mean_decode = sum(s.decode_tok_s for s in samples) / n
    tokens_per_doc = sum(s.total_tokens for s in samples) / n
    prefill_seconds = sum(s.prompt_duration_ns / 1e9 for s in samples) / n
    decode_seconds = sum(s.decode_duration_ns / 1e9 for s in samples) / n
    seconds_per_doc = sum(s.total_seconds for s in samples) / n

    return CellResult(
        axes=axes, n_calls=n, prefill_tok_s=round(mean_prefill, 1),
        decode_tok_s=round(mean_decode, 1), tokens_per_doc=round(tokens_per_doc, 1),
        seconds_per_doc=round(seconds_per_doc, 3),
        prefill_seconds_per_doc=round(prefill_seconds, 3),
        decode_seconds_per_doc=round(decode_seconds, 3),
        peak_vram_gb=round(peak_used, 2), baseline_vram_gb=round(baseline_used, 2),
        wall_seconds_per_doc=round(wall_seconds / n, 3),
    )


def run_matrix(axes_list, prompt_builder, *, generate_fn, vram_sampler=_default_vram_sampler,
              on_kv_quant_change=None):
    """Run every cell in `axes_list` (in the order given -- pass
    `matrix_axes()`'s output, which is already `kv_quant`-grouped).
    `prompt_builder(axes) -> [prompt_str, ...]` supplies the prompts for one
    cell (see `build_prompt` for the routing/preprocessing wiring).
    `on_kv_quant_change(kv_quant)` fires once per NEW kv_quant value
    encountered -- the pause point for a real run to restart the Ollama
    service and re-verify (`config.verify_server_runtime`) before trusting
    the cells under it.
    """
    results = []
    seen_kv = object()  # sentinel that can never equal a real kv_quant string
    for axes in axes_list:
        if axes.kv_quant != seen_kv:
            seen_kv = axes.kv_quant
            if on_kv_quant_change:
                on_kv_quant_change(axes.kv_quant)
        prompts = prompt_builder(axes)
        results.append(run_cell(axes, prompts, generate_fn=generate_fn, vram_sampler=vram_sampler))
    return results


# --------------------------------------------------------------------------- #
# The issue's specific questions
# --------------------------------------------------------------------------- #

def summarize_matrix(results):
    """Coarse roll-up over a full `run_matrix` result: mean tok/s per model,
    and whether the average cell is decode-bound (#111's prediction that the
    run becomes decode-bound once prompts shrink)."""
    by_model = {}
    for r in results:
        by_model.setdefault(r.axes.model, []).append(r)
    out = {}
    for model, cells in by_model.items():
        n = len(cells)
        out[model] = {
            "n_cells": n,
            "mean_prefill_tok_s": round(sum(c.prefill_tok_s for c in cells) / n, 1),
            "mean_decode_tok_s": round(sum(c.decode_tok_s for c in cells) / n, 1),
            "mean_seconds_per_doc": round(sum(c.seconds_per_doc for c in cells) / n, 3),
            "decode_bound_cells": sum(1 for c in cells if c.decode_bound),
        }
    return out


def token_reduction_check(baseline_cell, reduced_cell):
    """Does #111's ~8x token-reduction claim hold? Compares two MEASURED
    cells (typically `raw_table`/`whole_section` as the baseline against
    `flattened_pairs`/`sub_block` as the reduced configuration) rather than
    the estimate against itself. `baseline_claim`/`reduced_claim` in the
    output are #111's own numbers (~7,500 prefill + ~600 decode -> ~700 +
    ~200), included so the measured reduction and the claimed reduction sit
    side by side in the report.
    """
    measured_ratio = (baseline_cell.tokens_per_doc / reduced_cell.tokens_per_doc
                      if reduced_cell.tokens_per_doc else None)
    return {
        "baseline_cell": baseline_cell.axes.cell_id(),
        "reduced_cell": reduced_cell.axes.cell_id(),
        "measured_baseline_tokens_per_doc": baseline_cell.tokens_per_doc,
        "measured_reduced_tokens_per_doc": reduced_cell.tokens_per_doc,
        "measured_reduction_ratio": round(measured_ratio, 2) if measured_ratio else None,
        "claimed_baseline_tokens_per_doc": 7500 + 600,
        "claimed_reduced_tokens_per_doc": 700 + 200,
        "claimed_reduction_ratio": round((7500 + 600) / (700 + 200), 2),
    }


def prefix_cache_check(without_static_prefix, with_static_prefix):
    """Does the prompt-prefix cache actually hit once #106 orders exemplars/
    instructions as a static prefix? The signal is measured PREFILL TOKENS
    for the SAME logical prompt with and without that ordering -- a hit
    shows up as a lower `prompt_eval_count` relative to the raw prompt
    length (Ollama does not bill cached prefix tokens the same as fresh
    ones), not as a prefill tok/s change. Feed two `CellResult`s built from
    otherwise-identical axes, one from a `prompt_builder` that puts
    exemplars first and stable, one that does not.
    """
    delta = without_static_prefix.tokens_per_doc - with_static_prefix.tokens_per_doc
    return {
        "without_static_prefix_tokens_per_doc": without_static_prefix.tokens_per_doc,
        "with_static_prefix_tokens_per_doc": with_static_prefix.tokens_per_doc,
        "measured_prefill_token_savings": round(delta, 1),
        "cache_appears_to_hit": delta > 0,
    }


def compare_accuracy_tradeoff(fast_cell, slow_cell, *, fields=None, tolerance=0.02):
    """"A config that is 2x faster and 20% worse ... is not faster" -- the
    issue's own framing. Both cells must carry `.accuracy` (an
    `eval_harness.EvalReport.fields`-shaped dict, set by the caller after
    grading each cell's model/routing/preprocessing combination on the held-
    out set). Returns which fields regressed beyond `tolerance` precision/
    recall when moving from `slow_cell` to `fast_cell`, and the speedup
    factor -- so "faster" and "still accurate enough" are reported together,
    never one without the other.
    """
    if not fast_cell.accuracy or not slow_cell.accuracy:
        raise ValueError("compare_accuracy_tradeoff needs .accuracy set on both cells "
                         "(run eval_harness.run_eval per cell and attach the result)")
    field_names = fields or sorted(set(fast_cell.accuracy) & set(slow_cell.accuracy))
    regressed = []
    for name in field_names:
        fm, sm = fast_cell.accuracy.get(name), slow_cell.accuracy.get(name)
        if not fm or not sm:
            continue
        for metric in ("precision", "recall"):
            f, s = fm.get(metric), sm.get(metric)
            if f is None or s is None:
                continue
            if f < s - tolerance:
                regressed.append({"field": name, "metric": metric, "slow": s, "fast": f,
                                  "delta": round(f - s, 4)})
    speedup = (slow_cell.seconds_per_doc / fast_cell.seconds_per_doc
              if fast_cell.seconds_per_doc else None)
    return {
        "fast_cell": fast_cell.axes.cell_id(), "slow_cell": slow_cell.axes.cell_id(),
        "speedup_x": round(speedup, 2) if speedup else None,
        "regressed_fields": regressed,
        "worth_it": speedup is not None and speedup > 1.0 and not regressed,
    }


# --------------------------------------------------------------------------- #
# Self-check -- fake transport, no network, no GPU
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    try:
        from field_spec import load_specs
        from normalize import normalize_html
        from reduce import split_sections
        from validation import RenderDocument
    except ImportError:
        from .field_spec import load_specs
        from .normalize import normalize_html
        from .reduce import split_sections
        from .validation import RenderDocument

    spec = load_specs()["structured_note"]
    field_def = spec.field("barrier_pct")

    html = ("<html><body><h2>Downside Protection</h2>"
           "<p>Barrier: 70.00% of the Initial Value; principal is at risk below it. "
           + ("Additional boilerplate risk language repeated for length. " * 20) +
           "</p></body></html>")
    nd = normalize_html(html)
    render_doc = RenderDocument.from_normalized(nd)
    sections = split_sections(nd)

    # -- prompt building: sub_block is narrower than whole_section -----------
    whole = build_prompt_text(field_def, render_doc, sections,
                              routing="whole_section", preprocessing="flattened_pairs")
    warm = build_prompt_text(field_def, render_doc, sections,
                             routing="sub_block", preprocessing="flattened_pairs")
    assert "70.00%" in whole and "70.00%" in warm
    assert len(warm) < len(whole), "sub_block must be narrower than the whole section"
    print(f"whole_section: {len(whole)} chars, sub_block: {len(warm)} chars -- "
         f"{100 * (1 - len(warm) / len(whole)):.0f}% narrower")

    raw = build_prompt_text(field_def, render_doc, sections,
                            routing="sub_block", preprocessing="raw_table")
    assert "70.00%" in raw
    print("raw_table preprocessing still locates the value: PASS")

    # -- axes + matrix enumeration, kv_quant grouped first -------------------
    axes_list = list(matrix_axes(models=("m3b", "m7b"), batches=(1, 4), kv_quants=("fp16", "q8_0"),
                                 routing=("whole_section",), preprocessing=("flattened_pairs",)))
    assert len(axes_list) == 2 * 2 * 2  # models x batches x kv_quants
    kv_sequence = [a.kv_quant for a in axes_list]
    assert kv_sequence == sorted(kv_sequence, key=kv_sequence.index) and \
        kv_sequence.count("fp16") == kv_sequence.index("q8_0"), \
        "kv_quant must be the outer grouping so a caller only restarts once per value"
    print(f"matrix_axes: {len(axes_list)} cells, kv_quant grouped: PASS")

    # -- run_cell against a fake transport ------------------------------------
    class _FakeGenerate:
        """Scripts Ollama's native /api/generate response shape -- the same
        fake-client convention as test_extraction_ladder.py's
        FakeChatClient, one level lower (raw generate, not chat)."""

        def __init__(self, prompt_tok_per_char=0.25, prefill_tok_s=1200.0, decode_tok_s=40.0,
                    decode_tokens=200):
            self.prompt_tok_per_char = prompt_tok_per_char
            self.prefill_tok_s = prefill_tok_s
            self.decode_tok_s = decode_tok_s
            self.decode_tokens = decode_tokens
            self.calls = []

        def __call__(self, payload):
            self.calls.append(payload)
            prompt_tokens = max(1, round(len(payload["prompt"]) * self.prompt_tok_per_char))
            prompt_ns = round(prompt_tokens / self.prefill_tok_s * 1e9)
            decode_ns = round(self.decode_tokens / self.decode_tok_s * 1e9)
            return {"prompt_eval_count": prompt_tokens, "prompt_eval_duration": prompt_ns,
                   "eval_count": self.decode_tokens, "eval_duration": decode_ns}

    class _FakeVram:
        """Ramps used VRAM up then back down across successive samples --
        peak_vram_gb must catch the ramp, not just the first/last reading."""

        def __init__(self, total=12.0, readings=(10.0, 6.0, 4.0, 6.0, 10.0)):
            self.total = total
            self.readings = list(readings)
            self.i = 0

        def __call__(self):
            free = self.readings[min(self.i, len(self.readings) - 1)]
            self.i += 1
            return self.total, free

    axes = BenchAxes(model="qwen2.5:3b-instruct-q4_K_M", batch=4, kv_quant="q8_0",
                     routing="sub_block", preprocessing="flattened_pairs")
    fake_gen = _FakeGenerate()
    fake_vram = _FakeVram()
    prompts = [warm, warm, warm, warm, whole]
    result = run_cell(axes, prompts, generate_fn=fake_gen, vram_sampler=fake_vram)
    assert result.n_calls == 5
    assert result.prefill_tok_s > 0 and result.decode_tok_s > 0
    assert result.peak_vram_gb == 12.0 - min(fake_vram.readings), \
        f"peak VRAM must catch the ramp, got {result.peak_vram_gb}"
    print(f"run_cell: {result.as_dict()}")

    # -- run_matrix groups by kv_quant and fires the change hook once/value --
    kv_changes = []
    result_list = run_matrix(
        axes_list, prompt_builder=lambda a: [warm, warm],
        generate_fn=_FakeGenerate(), vram_sampler=_FakeVram(),
        on_kv_quant_change=lambda kv: kv_changes.append(kv),
    )
    assert len(result_list) == len(axes_list)
    assert kv_changes == ["fp16", "q8_0"], kv_changes
    print(f"run_matrix: {len(result_list)} cells, kv_quant change fired for: {kv_changes}")

    # -- token_reduction_check compares measurement, not estimate to itself --
    baseline_cell = run_cell(
        BenchAxes("qwen2.5:7b-instruct-q4_K_M", 1, "fp16", "whole_section", "raw_table"),
        [whole] * 3, generate_fn=_FakeGenerate(prompt_tok_per_char=0.3), vram_sampler=_FakeVram())
    reduced_cell = run_cell(
        BenchAxes("qwen2.5:7b-instruct-q4_K_M", 1, "fp16", "sub_block", "flattened_pairs"),
        [warm] * 3, generate_fn=_FakeGenerate(prompt_tok_per_char=0.3), vram_sampler=_FakeVram())
    trc = token_reduction_check(baseline_cell, reduced_cell)
    assert trc["measured_reduction_ratio"] > 1.0, trc
    print(f"token_reduction_check: measured {trc['measured_reduction_ratio']}x "
         f"vs claimed {trc['claimed_reduction_ratio']}x")

    # -- prefix_cache_check reads the token delta, not a rate ----------------
    with_prefix = run_cell(axes, [warm[:200]] * 3, generate_fn=_FakeGenerate(), vram_sampler=_FakeVram())
    without_prefix = run_cell(axes, [warm] * 3, generate_fn=_FakeGenerate(), vram_sampler=_FakeVram())
    pcc = prefix_cache_check(without_prefix, with_prefix)
    assert pcc["cache_appears_to_hit"] is True, pcc
    print(f"prefix_cache_check: {pcc}")

    # -- compare_accuracy_tradeoff needs .accuracy set, and flags a fast/bad cell
    fast = run_cell(axes, [warm] * 2, generate_fn=_FakeGenerate(decode_tok_s=80.0), vram_sampler=_FakeVram())
    slow = run_cell(axes, [whole] * 2, generate_fn=_FakeGenerate(decode_tok_s=40.0), vram_sampler=_FakeVram())
    fast.accuracy = {"estimated_value_per_1000": {"precision": 0.70, "recall": 0.65}}
    slow.accuracy = {"estimated_value_per_1000": {"precision": 0.95, "recall": 0.93}}
    tradeoff = compare_accuracy_tradeoff(fast, slow)
    assert tradeoff["speedup_x"] and tradeoff["speedup_x"] > 1.0
    assert tradeoff["regressed_fields"], "a 25-point precision drop must be flagged"
    assert tradeoff["worth_it"] is False, "faster but worse must not read as worth it"
    print(f"compare_accuracy_tradeoff: {tradeoff}")

    print("\nthroughput_bench self-check: PASS")
