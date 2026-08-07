# Throughput benchmark (issue #108)

Part of #95. Every performance number in #95, #103, #107, and #111 was an
estimate derived from hardware characteristics — `hardware_probe.py`'s
KV-cache sizing is arithmetic, and #111's "~8x token reduction" and "the run
becomes decode-bound" were predictions. See [EVAL_HARNESS.md](EVAL_HARNESS.md)
for the quality half of #108.

## Measured (2026-08-06, RTX 4070 Ti)

[`throughput_results/2026-08-06_rtx4070ti.json`](throughput_results/2026-08-06_rtx4070ti.json)
is a real run of [`run_throughput_matrix.py`](run_throughput_matrix.py) against
5 real 424B2 filings fetched live from EDGAR (4 JPMorgan, 1 BofA Finance) and
a live local Ollama server on the target RTX 4070 Ti, both `qwen2.5` models,
both routing modes, both preprocessing modes:

- **#111's ~8x token-reduction claim holds, and then some** — measured
  `whole_section`/`raw_table` -> `sub_block`/`flattened_pairs` reduction is
  **8.1x** on the 3B model (4577 -> 567 tokens/doc) and **9.8x** on the 7B
  model (4559 -> 465 tokens/doc).
- **Every one of the 8 measured cells is decode-bound** (`decode_bound: true`
  — measured decode wall-clock time exceeds prefill wall-clock time in
  every cell), confirming #111's prediction.
- 3B decode throughput measured at ~111-113 tok/s; 7B at ~58-61 tok/s, both
  well above what the pre-existing #103 arithmetic assumed.

**Not yet measured, scoped as follow-up work**:
- **KV-quant (`fp16` vs `q8_0`) comparison** — this run only measured
  whatever quantization the Ollama server already had running; flipping
  `OLLAMA_KV_CACHE_TYPE` requires restarting the server, which this pass
  intentionally did not do so as not to disrupt other GPU use during the
  run. `on_kv_quant_change` in `run_matrix` is the pause point for whoever
  does this next.
- **Batch concurrency (1/4/8/16)** — `run_cell` now dispatches each chunk
  concurrently (`ThreadPoolExecutor(max_workers=axes.batch)`), and
  `CellResult.wall_seconds_per_doc` is the metric that can show the win.
  `seconds_per_doc` cannot: it is a mean of per-request SERVER durations, and
  four concurrent 1s calls still average 1s. **The axis is now measurable but
  has not been measured** — the live pass predates the fix and ran `batch=1`
  only. Re-run with `BATCHES=(1,4,8,16)` to fill it in.
- **Prefix-cache hit rate (#106 static-prefix ordering)** — one of the issue's
  four open questions, and **not measured**. `run_throughput_matrix.py` never
  passes `exemplars=` when building prompts, so the real run never exercises
  the static-prefix ordering and `prefix_cache_check` is never driven
  end-to-end. Wiring exemplars into the matrix run is the prerequisite; until
  then "does the prefix cache actually hit?" is open.
- **`compare_accuracy_tradeoff`** — needs `eval_harness.run_eval` output
  attached per cell, which needs a real #105 held-out set; none exists in
  this environment yet, so accuracy-tied cells are unmeasured.
- Only 5 documents were used (not the full 20) to keep the live run under a
  few minutes; re-running `run_throughput_matrix.py` with more `N_DOCS` only
  costs more EDGAR fetches (cached under `.bench_cache/`) and GPU time.

```
tools/edgar_scrubber/
  throughput_bench.py       # matrix runner: prefill/decode tok/s, tokens/doc, peak VRAM
  test_throughput_bench.py  # every #108 throughput acceptance criterion
```

## The matrix

| axis | values |
|---|---|
| model | `qwen2.5:3b-instruct-q4_K_M`, `qwen2.5:7b-instruct-q4_K_M` |
| batch | 1, 4, 8, 16 |
| KV quant | `fp16`, `q8_0` |
| routing | whole_section vs sub_block (#101) |
| preprocessing | raw table markup vs flattened pairs (#101) |

```python
from throughput_bench import matrix_axes, run_matrix, build_prompt

axes = list(matrix_axes())   # every cell, kv_quant grouped first
results = run_matrix(axes, prompt_builder=my_prompt_builder,
                     generate_fn=my_generate_fn, on_kv_quant_change=restart_server_and_verify)
```

Per cell, measured: **prefill tok/s, decode tok/s, tokens per document,
seconds per document, peak VRAM** — plus, optionally, per-field accuracy
(`CellResult.accuracy`, set by the caller from `eval_harness.run_eval`'s
output), because a config that is 2x faster and 20% worse on
`estimated_value_per_1000` is not faster.

## Why `kv_quant` is the outer loop

KV-cache quantization and true parallel batching are Ollama **server-startup**
settings — `config.py`'s `verify_server_runtime` already had to prove this the
hard way (exporting `OLLAMA_KV_CACHE_TYPE` from a client process cannot reach
a server that is already running; a measured 4070 Ti run showed eight
"parallel" requests moving peak VRAM by 17 MiB when 1.8–3.7 GB should have
moved if the setting had really taken).

`matrix_axes()` therefore groups every cell by `kv_quant` first, and
`run_matrix`'s `on_kv_quant_change(kv_quant)` fires exactly once per new
value — the pause point for a real run to restart the Ollama service with the
right `OLLAMA_KV_CACHE_TYPE` and re-verify with `config.verify_server_runtime`
before trusting the cells underneath it. Batch size, by contrast, **is** a
per-request knob here: Ollama's "batch" is concurrent in-flight requests
against the server's configured parallel slots, so `run_cell` sends
`axes.batch` requests per chunk rather than one call carrying many prompts.

## Why `/api/generate`, not the OpenAI-compatible endpoint

`ollama_client.py`'s `OllamaClient.chat_completion` hits
`/v1/chat/completions` everywhere else in this scrubber — the OpenAI-
compatible surface #104's ladder is built on. That endpoint does not report
timing. Ollama's **native** `/api/generate` does: `prompt_eval_count`,
`prompt_eval_duration`, `eval_count`, `eval_duration` — prefill tok/s and
decode tok/s are computed straight from those. This is the one place in the
scrubber that calls `/api/generate` directly, the same low-level pattern
`config.verify_server_runtime` already uses for the same reason (measuring
what the server actually did, not what was asked of it).

## Routing and preprocessing build real text, not stand-ins

`build_prompt_text` reuses #101's actual reduction primitives so the
benchmark measures the text the ladder would really send:

- **whole_section** — `validation.build_field_context`: every section the
  field's spec routes into (#101 stage 3, the cold path).
- **sub_block** — `reduce.sub_block`, centered on the field's first matched
  anchor within that section text (#101 stage 4, the warm path #107 unlocks
  once an anchor is known).
- **flattened_pairs** — the text as `normalize_html` already produces it:
  every table pre-parsed into `label: value` lines (#101 stage 1) — what
  `render_doc.text` already contains.
- **raw_table** — the *original* HTML markup covering the identical window,
  via `render_doc.source` and the window's resolved source span — what
  stage 1 exists to avoid sending, and what #111's token-cost claim is
  measured against.

## The four questions this benchmark settles

**Is 3B sufficient for fixed-anchor fields?** Grade both models on the same
held-out set (`eval_harness.run_eval`), attach the per-field results to each
model's `CellResult.accuracy`, and call `compare_accuracy_tradeoff`. If 3B
holds accuracy on `field_spec`'s `fixed-anchor` fields, batch 16 opens up and
the bulk pass gets materially cheaper.

```python
from throughput_bench import compare_accuracy_tradeoff
tradeoff = compare_accuracy_tradeoff(fast_cell, slow_cell, tolerance=0.02)
# {"speedup_x": ..., "regressed_fields": [...], "worth_it": bool}
```

**Does #111's ~8x token reduction hold?** `token_reduction_check` compares
two *measured* cells (typically `raw_table`/`whole_section` as the baseline
against `flattened_pairs`/`sub_block` as the reduced configuration) and
reports the measured ratio alongside #111's own claimed numbers (~7,500
prefill + ~600 decode per document, claimed to collapse to ~700 + ~200) —
side by side, not one asserted against the other.

**Does the prefix cache actually hit?** `prefix_cache_check` reads **measured
prefill tokens**, with and without #106's static-prefix ordering, for
otherwise-identical prompts — a hit shows up as fewer prompt tokens billed
for the same logical content, not as a rate change. This is the saving most
likely to be silently lost, so it is checked directly rather than assumed
from the design.

**Where is the real bottleneck?** `CellResult.decode_bound` compares measured
prefill vs. decode **wall-clock seconds** per document (not tok/s, which are
never directly comparable across prefill and decode — the rates differ by
design). `summarize_matrix` rolls this up per model. #111 predicts the run
becomes decode-bound once prompts tighten; if so, batch size and output size
matter more than prompt size, and effort should follow.

## Test

```bash
python tools/edgar_scrubber/test_throughput_bench.py
```

Stdlib only, no network, no GPU — `FakeGenerate` scripts Ollama's native
`/api/generate` response shape (the same fake-client convention as
`test_extraction_ladder.py`'s `FakeChatClient`, one level lower), and a fake
VRAM sampler scripts a reading sequence so `peak_vram_gb` is checked against
a real ramp, not just a before/after diff. Exit 0 = pass.

## References

- Issue #108 (this module, throughput half) — part of #95
- Issue #95 — every estimate this benchmark replaces or confirms
- Issue #103 — the KV-cache sizing arithmetic being checked
- Issue #107 — the sub-block warm path this benchmark measures the payoff of
- Issue #111 — the token-budget claims (~8x reduction, decode-bound) this benchmark settles
- [DOCUMENT_REDUCTION.md](DOCUMENT_REDUCTION.md) — the four-stage reduction `build_prompt_text` reuses
- [EVAL_HARNESS.md](EVAL_HARNESS.md) — the per-field accuracy `compare_accuracy_tradeoff` needs attached
- `config.py`'s `verify_server_runtime` — the measured-not-assumed pattern this module follows for `/api/generate` and KV-cache verification
