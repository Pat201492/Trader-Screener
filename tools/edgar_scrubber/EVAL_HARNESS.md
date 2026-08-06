# Extraction eval harness (issue #108)

Part of #95, built on #105 (blocked on it). Two jobs: prove extraction
quality is not regressing, and replace every throughput estimate in the
epic with a measurement. This doc covers quality; see
[THROUGHPUT_BENCHMARK.md](THROUGHPUT_BENCHMARK.md) for the benchmark half.

```
tools/edgar_scrubber/
  eval_harness.py       # held-out selection, per-field metrics, regression gate, report store
  test_eval_harness.py  # every #108 quality acceptance criterion
```

## Why an aggregate score is decorative

`issuer` is trivial and will sit near 1.0; `coupon_barrier_pct` is where the
errors live. A single blended number hides that, and the natural failure
mode of an exemplar store (#106) — adding examples that help one issuer and
hurt another — is invisible without a per-field, per-issuer read. This
module never reports one number for "quality."

## The held-out set

A slice of #105-validated documents that **never** enters the exemplar
store (#106) or seeds a rule (#107). This is the only honest read available;
if it leaks into training the number becomes decorative.

The ordering matters more than it looks: #105 writes an exemplar the
**instant** a verdict is recorded (`ValidationSession._write_exemplar`), so
a document can only be honestly held out if it is marked *before* its first
field gets a verdict, not after the fact. `eval_harness.py` owns the
**selection policy**; `validation.py` owns the actual **exclusion
mechanism**, because it is the module that writes verdicts and exemplars in
the first place:

- `ValidationStore.mark_held_out(session_id, accession, document, ...)` /
  `.is_held_out(...)` — a `held_out_docs` table, checked at open time under
  the same local-only ownership boundary as everything else in this
  scrubber.
- `ValidationSession._write_exemplar` skips a held-out document's verdict
  entirely — no exemplar is ever written for it.
- `ValidationStore.anchor_verdicts` (the rule-seed stability input #107
  reads) excludes held-out documents at the query, not by filtering
  downstream — a held-out anchor cannot count toward a rule's support.

```python
from eval_harness import select_held_out, reserve_held_out_set

# Stratified by (issuer, product_type) so the read isn't dominated by
# whichever issuer got validated most. Deterministic for a fixed seed, so
# two runs pick the same slice (needed for the regression gate below).
selected = select_held_out(candidates, fraction=0.15, seed=0)

# Or select-and-mark in one step, against a live ValidationSession -- the
# usual entry point, and what validate_ui.py's --reserve-held-out flag calls:
selected = reserve_held_out_set(session, candidates, fraction=0.15, seed=0)
```

`validate_ui.py --reserve-held-out 0.15` reserves a slice of the crawl
frontier before the validation loop starts. Reserved documents still get
validated normally (their verdicts are the eval harness's ground truth) —
they just never teach the exemplar store or a rule.

## Per-field metrics

For every field, over the held-out set:

| metric | what it answers |
|---|---|
| **precision** | of values produced, how many correct |
| **recall** | of values present, how many found |
| **span accuracy** | of the correct values, how many came from the right span (a right value from the wrong span is still a defect — #107 induces regex anchors from spans) |
| **escalation rate** | share of predictions that reached the Claude rung |
| **absence-handling** | false-positive rate on fields genuinely not present (hallucination vs. a correctly declined absence) |

A wrong value (gold present, prediction present but different) counts
against **both** precision and recall — the standard information-extraction
convention, and the one the metric definitions above describe: it was
"produced" (so it can be wrong) and it was not "found" (the true value is
still missing).

```python
from eval_harness import HeldOutCase, run_eval

cases = [HeldOutCase(session_id="jpm-2025", accession="0001...", document="424b2.htm",
                     issuer="JPMorgan...", render_doc=render_doc, sections=sections)]

report = run_eval(spec, extractor, cases, store, report_id="run-42",
                  generated_at="2026-08-06T10:00:00Z",
                  exemplar_set_version="jpm-ev1000-v3", rule_set_version="rules-v2",
                  local_model="qwen2.5:7b-instruct-q4_K_M")
```

`extractor` is anything shaped like `validation.LadderExtractor` — the same
`.propose(render_doc, issuer=, ex107=, accession=, document=, fields=) ->
[FieldProposal]` interface #105 already uses, so grading the CURRENT
extraction ladder (with its current rules/exemplars/spec) needs no adapter.

`run_eval` **refuses** to score a case that is not marked held-out in the
store — checked, not assumed. A report can never be quietly built from a
document that may also have trained the exemplars or rules it is grading.

## The regression gate

No spec edit (#102), exemplar change (#106), rule promotion (#107), or
model bump (#103) ships without a run. Any field dropping past threshold
blocks it.

```python
from eval_harness import evaluate_regression

gate = evaluate_regression(baseline_report, candidate_report)
if gate.blocked:
    print(gate.explain())
    raise SystemExit(1)
```

Default thresholds (absolute change, tunable via `thresholds=`):

| metric | direction | default |
|---|---|---|
| precision / recall / span_accuracy | max allowed **drop** | 0.05 |
| absence_fp_rate | max allowed **rise** | 0.05 |
| escalation_rate | max allowed **rise** | 0.15 (a cost signal, not necessarily quality — looser on purpose) |

Two things block unconditionally, not just past-threshold:

- **A field present in the baseline but missing from the candidate** — the
  worst possible regression, and not something a threshold can catch (it
  was not evaluated at all, not evaluated-and-fine).
- Nothing else is a hard block; `min_support` (default 3) skips a metric
  comparison when either report's denominator for it is too small to trust
  — a rate computed from one or two held-out examples swings on noise
  alone, and a real regression will still show up once support is enough
  to see it.

## Reports are stamped and comparable

Every `EvalReport` carries `spec_id`, `spec_version`, `exemplar_set_version`,
`rule_set_version`, `local_model`, `claude_model`, and the held-out
session(s) it was scored against — a report with no idea what produced it
cannot gate anything. `EvalReportStore` persists reports local-only (same
ownership boundary as `output_store.py` / `validation.py`):

```python
from eval_harness import EvalReportStore

store = EvalReportStore()
store.save(report)
store.accept_as_baseline(report.report_id)   # promote once it ships
baseline = store.latest_baseline(spec.spec_id)
gate = evaluate_regression(baseline, new_report)
```

## Test

```bash
python tools/edgar_scrubber/test_eval_harness.py
```

Stdlib only, no network, no model — a `FakeExtractor` returns scripted
`FieldProposal`s directly, isolating this module's own scoring/gate logic
from the extraction ladder (#104) and validation loop (#105) machinery,
which their own gates already cover. Exit 0 = pass.

## References

- Issue #108 (this module, quality half) — part of #95, blocked on #105
- [VALIDATION_UI.md](VALIDATION_UI.md) — the loop whose output this grades, and the exemplar-write-on-verdict design the held-out ordering works around
- [EXTRACTION_LADDER.md](EXTRACTION_LADDER.md) — the ladder `run_eval` re-runs against the held-out set
- [FIELD_SPEC.md](FIELD_SPEC.md) — field types/bounds `_values_match` reads
- [THROUGHPUT_BENCHMARK.md](THROUGHPUT_BENCHMARK.md) — the throughput half of #108, and where per-field accuracy from this module feeds `compare_accuracy_tradeoff`
- Issue #106 (exemplars) / #107 (rule promotion) — the two things a held-out document must never train
