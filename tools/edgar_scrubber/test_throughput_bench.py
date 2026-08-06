"""
Gate for the throughput benchmark harness (issue #108). Same convention as
`test_eval_harness.py` / `test_extraction_ladder.py`: stdlib only, run
directly, exit 0 = pass, no network and no GPU -- a fake `/api/generate`
transport scripts Ollama's native timing fields, and a fake VRAM sampler
scripts a reading sequence, the same fake-client convention used everywhere
else in this scrubber.

Every acceptance criterion in #108's throughput half is checked here:

  * routing (whole_section vs sub_block) and preprocessing (raw_table vs
    flattened_pairs) build real, distinct prompt text from #101's actual
    reduction primitives -- not stand-ins;
  * the matrix enumerates every (model, batch, kv_quant, routing,
    preprocessing) cell, `kv_quant` grouped first, since it is a server
    restart between values and everything else is a same-server knob;
  * one cell measures prefill tok/s, decode tok/s, tokens/doc, seconds/doc,
    and PEAK (not just before/after) VRAM;
  * `run_matrix` fires the kv_quant-change hook exactly once per value, not
    once per cell;
  * the four specific questions the issue asks the benchmark to settle each
    have a function that answers them from measured cells, not estimates.

Run:  python tools/edgar_scrubber/test_throughput_bench.py
"""
import throughput_bench as tb
from field_spec import load_specs
from normalize import normalize_html
from reduce import split_sections
from validation import RenderDocument

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


SPEC = load_specs()["structured_note"]
FIELD = SPEC.field("barrier_pct")

HTML = ("<html><body><h2>Downside Protection</h2>"
       "<p>Barrier: 70.00% of the Initial Value; principal is at risk below it. "
       + ("Additional boilerplate risk language repeated for length. " * 25) +
       "</p></body></html>")


def _render_doc():
    nd = normalize_html(HTML)
    return nd, RenderDocument.from_normalized(nd), split_sections(nd)


class FakeGenerate:
    """Scripts Ollama's native /api/generate response shape (prompt_eval_*
    / eval_* fields) -- the level below FakeChatClient's chat-completions
    shape, and the one this module actually calls (see the module
    docstring: OpenAI-compat does not expose these fields)."""

    def __init__(self, prompt_tok_per_char=0.25, prefill_tok_s=1000.0, decode_tok_s=35.0,
                decode_tokens=150):
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


class FakeVram:
    def __init__(self, total=12.0, readings=(9.0,)):
        self.total = total
        self.readings = list(readings)
        self.i = 0

    def __call__(self):
        free = self.readings[min(self.i, len(self.readings) - 1)]
        self.i += 1
        return self.total, free


# --------------------------------------------------------------------------- #
def test_routing_and_preprocessing_build_distinct_real_text():
    section("routing/preprocessing axes build distinct prompt text from #101's real primitives")
    nd, render_doc, sections = _render_doc()

    whole = tb.build_prompt_text(FIELD, render_doc, sections,
                                 routing="whole_section", preprocessing="flattened_pairs")
    warm = tb.build_prompt_text(FIELD, render_doc, sections,
                                routing="sub_block", preprocessing="flattened_pairs")
    check("both routings still locate the true value", "70.00%" in whole and "70.00%" in warm)
    check("sub_block is narrower than whole_section (the #101 warm-path point)",
          len(warm) < len(whole))

    raw = tb.build_prompt_text(FIELD, render_doc, sections,
                               routing="sub_block", preprocessing="raw_table")
    check("raw_table preprocessing still locates the value", "70.00%" in raw)
    check("raw and flattened_pairs text differ for the same window (different preprocessing)",
          raw != warm or "<" in raw)

    messages = tb.build_prompt(FIELD, render_doc, sections,
                               routing="whole_section", preprocessing="flattened_pairs")
    check("build_prompt flattens chat messages into one string", isinstance(messages, str))
    check("flattened prompt carries the field name (from build_messages' FIELD: line)",
          "barrier_pct" in messages)


# --------------------------------------------------------------------------- #
def test_matrix_axes_groups_by_kv_quant_first():
    section("matrix_axes enumerates every cell with kv_quant grouped first")
    axes = list(tb.matrix_axes(models=("3b", "7b"), batches=(1, 4), kv_quants=("fp16", "q8_0"),
                               routing=("whole_section", "sub_block"),
                               preprocessing=("raw_table", "flattened_pairs")))
    check("full matrix size is the product of every axis",
          len(axes) == 2 * 2 * 2 * 2 * 2)
    kvs = [a.kv_quant for a in axes]
    fp16_block = kvs[:kvs.index("q8_0")]
    check("every fp16 cell precedes every q8_0 cell (one restart point, not per-cell)",
          all(k == "fp16" for k in fp16_block) and all(k == "q8_0" for k in kvs[len(fp16_block):]))
    check("cell_id is unique per axis combination",
          len({a.cell_id() for a in axes}) == len(axes))


# --------------------------------------------------------------------------- #
def test_run_cell_measures_prefill_decode_tokens_and_peak_vram():
    section("run_cell measures prefill/decode tok/s, tokens/doc, seconds/doc, and PEAK VRAM")
    nd, render_doc, sections = _render_doc()
    warm = tb.build_prompt_text(FIELD, render_doc, sections, routing="sub_block",
                                preprocessing="flattened_pairs")

    axes = tb.BenchAxes(model="qwen2.5:3b-instruct-q4_K_M", batch=4, kv_quant="q8_0",
                        routing="sub_block", preprocessing="flattened_pairs")
    gen = FakeGenerate(prefill_tok_s=1000.0, decode_tok_s=35.0, decode_tokens=150)
    # VRAM ramps 10 -> 3 -> 10 across the run's samples; peak usage must catch
    # the trough in FREE memory (i.e. the peak in USED memory), not just the
    # first or last reading.
    vram = FakeVram(total=12.0, readings=(10.0, 3.0, 10.0))
    result = tb.run_cell(axes, [warm] * 6, generate_fn=gen, vram_sampler=vram)

    check("every prompt produced one call", result.n_calls == 6)
    check("prefill tok/s matches the scripted rate",
          abs(result.prefill_tok_s - 1000.0) < 1.0)
    check("decode tok/s matches the scripted rate", abs(result.decode_tok_s - 35.0) < 1.0)
    check("tokens_per_doc counts prompt + decode tokens",
          result.tokens_per_doc > gen.decode_tokens)
    check("peak_vram_gb catches the ramp's trough in free memory (12 - 3 = 9), not the last sample",
          result.peak_vram_gb == 9.0)
    check("baseline_vram_gb is the FIRST sample, before the run started",
          result.baseline_vram_gb == 2.0)
    check("decode_bound flags true when decode seconds dominate (150 tok @ 35 tok/s >> prefill)",
          result.decode_bound is True)


# --------------------------------------------------------------------------- #
def test_run_matrix_fires_kv_quant_change_once_per_value():
    section("run_matrix fires on_kv_quant_change exactly once per NEW kv_quant value")
    nd, render_doc, sections = _render_doc()
    prompt = tb.build_prompt_text(FIELD, render_doc, sections, routing="sub_block",
                                  preprocessing="flattened_pairs")
    axes = list(tb.matrix_axes(models=("3b",), batches=(1, 4), kv_quants=("fp16", "q8_0"),
                               routing=("whole_section",), preprocessing=("flattened_pairs",)))
    changes = []
    results = tb.run_matrix(axes, prompt_builder=lambda a: [prompt, prompt],
                            generate_fn=FakeGenerate(), vram_sampler=FakeVram(),
                            on_kv_quant_change=lambda kv: changes.append(kv))
    check("one result per axis combination", len(results) == len(axes))
    check("the change hook fired exactly once per kv_quant value, in order",
          changes == ["fp16", "q8_0"])


# --------------------------------------------------------------------------- #
def test_open_questions_answered_from_measured_cells():
    section("The issue's four specific questions are answered from measurement, not estimate")
    nd, render_doc, sections = _render_doc()
    whole_raw = tb.build_prompt_text(FIELD, render_doc, sections, routing="whole_section",
                                     preprocessing="raw_table")
    warm_flat = tb.build_prompt_text(FIELD, render_doc, sections, routing="sub_block",
                                     preprocessing="flattened_pairs")

    baseline_axes = tb.BenchAxes("qwen2.5:7b-instruct-q4_K_M", 1, "fp16", "whole_section", "raw_table")
    reduced_axes = tb.BenchAxes("qwen2.5:7b-instruct-q4_K_M", 1, "fp16", "sub_block", "flattened_pairs")
    baseline_cell = tb.run_cell(baseline_axes, [whole_raw] * 3,
                               generate_fn=FakeGenerate(prompt_tok_per_char=0.3), vram_sampler=FakeVram())
    reduced_cell = tb.run_cell(reduced_axes, [warm_flat] * 3,
                              generate_fn=FakeGenerate(prompt_tok_per_char=0.3), vram_sampler=FakeVram())

    # Q: does #111's ~8x token reduction hold?
    trc = tb.token_reduction_check(baseline_cell, reduced_cell)
    check("token_reduction_check reports a measured ratio > 1 (raw+whole costs more than flat+warm)",
          trc["measured_reduction_ratio"] > 1.0)
    check("the claimed #111 ratio (9x) is reported alongside the measured one for comparison",
          trc["claimed_reduction_ratio"] == 9.0)

    # Q: is 3B sufficient / does a fast config sacrifice accuracy? --
    # compare_accuracy_tradeoff needs .accuracy attached by the caller
    # (eval_harness.run_eval's output) -- refuses to guess without it.
    raised = False
    try:
        tb.compare_accuracy_tradeoff(baseline_cell, reduced_cell)
    except ValueError:
        raised = True
    check("compare_accuracy_tradeoff refuses to compare cells with no attached accuracy",
          raised)

    fast, slow = reduced_cell, baseline_cell
    fast.accuracy = {"barrier_pct": {"precision": 0.94, "recall": 0.92}}
    slow.accuracy = {"barrier_pct": {"precision": 0.95, "recall": 0.93}}
    tradeoff = tb.compare_accuracy_tradeoff(fast, slow, tolerance=0.02)
    check("a small (<=tolerance) accuracy difference does not flag a regression",
          tradeoff["regressed_fields"] == [])
    check("worth_it is true when faster AND not meaningfully worse",
          tradeoff["worth_it"] is True)

    # Q: does the prefix cache actually hit? -- measured on PREFILL TOKENS.
    with_prefix = tb.run_cell(baseline_axes, [warm_flat[:150]] * 3, generate_fn=FakeGenerate(),
                              vram_sampler=FakeVram())
    without_prefix = tb.run_cell(baseline_axes, [warm_flat] * 3, generate_fn=FakeGenerate(),
                                 vram_sampler=FakeVram())
    pcc = tb.prefix_cache_check(without_prefix, with_prefix)
    check("prefix_cache_check reads a positive token saving as a cache hit",
          pcc["cache_appears_to_hit"] and pcc["measured_prefill_token_savings"] > 0)

    # Q: where is the real bottleneck (decode- vs prefill-bound)?
    summary = tb.summarize_matrix([baseline_cell, reduced_cell])
    check("summarize_matrix reports per-model decode-bound cell counts",
          "qwen2.5:7b-instruct-q4_K_M" in summary and
          "decode_bound_cells" in summary["qwen2.5:7b-instruct-q4_K_M"])


def main():
    print("Throughput benchmark harness gate (#108)")
    test_routing_and_preprocessing_build_distinct_real_text()
    test_matrix_axes_groups_by_kv_quant_first()
    test_run_cell_measures_prefill_decode_tokens_and_peak_vram()
    test_run_matrix_fires_kv_quant_change_once_per_value()
    test_open_questions_answered_from_measured_cells()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nthroughput_bench gate: PASS")


if __name__ == "__main__":
    main()
