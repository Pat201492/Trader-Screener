# Running the validation pilot (issue #165)

#105 built the human-in-loop loop, #106 the exemplar store, #108 the eval
harness -- and [VALIDATION_UI.md](VALIDATION_UI.md) calls the loop "the
highest-leverage issue in the chain," estimating "ten validated minutes removes
hours of GPU time downstream." **None of it had been run.** The store held 0
graduations across 18 runs and 126 documents, so for 31 of 33 fields there was
no way to tell a correct extraction from a confident wrong one except by reading
the filing.

This is the runbook for the pilot that finds out whether the claim holds:
validate ~20 filings across >=4 issuers and let the system keep the labels.

## What this run needs that a PR cannot supply

The numbers are empirical outputs of a live sitting. Three things gate them and
none exists in CI or a sandbox, so the *numbers themselves* are produced by a
human at a real terminal, not by an automated change:

- **Network** to fetch the filings from EDGAR (`crawl.py` / `edgar_client.py`).
- **A local model** (`qwen2.5:7b` via Ollama) to produce the proposals the
  human rules on -- the extraction ladder's rungs 1-3.
- **A human** to give every spec field a verdict. Fabricating verdicts would
  poison the gold set -- exactly the failure #161 hit, where worked examples in
  the system prompt made 22 of 25 filings return the example value and it looked
  like an improvement until the outputs were checked against the filings.
  Without a gold set that class of regression is invisible, so the labels must
  be real.

What a PR *can* supply, and what this branch adds, is the tooling that makes the
sitting produce the report the acceptance criteria ask for -- so the read is a
by-product of the ten minutes, not a second manual pass:

- `queries/424b2-multi-issuer-2025-pilot.json` -- a 6-issuer 424B2 query, so
  the sample is not JPM-concentrated (the store is 96/101 JPMorgan, which is why
  the first out-of-distribution test, Citigroup, scored 2/9).
- `session_report.py` -- reads a session's recorded verdicts and emits the run
  report: per-field accept rate, the first-half/second-half compounding split,
  and per-document wall-clock. Derivable from the labels alone, no model.
- `eval_harness.run_eval` (#108) -- the authoritative per-field precision, by
  re-running the extractor against the held-out gold.

## Steps

### 1. Reserve a held-out slice, then validate 20 filings across >=4 issuers

`--reserve-held-out` sets aside a stratified slice of the frontier *before* the
loop starts, so those documents are graded by the eval harness but never teach
the exemplar store or seed a rule (#108). The rest train the loop as you go.

```bash
python tools/edgar_scrubber/validate_ui.py \
    --user-agent "Your Name you@example.com" \
    --query tools/edgar_scrubber/queries/424b2-multi-issuer-2025-pilot.json \
    --n 20 --session pilot-165 \
    --reserve-held-out 0.15
```

Give **every spec field a verdict** on each document: `a` accept, `c` correct
(mark the true value/span), `r` reject (field absent). The session is resumable
-- a killed run picks up at the last field ruled on.

> Criterion 1 (20 filings, >=4 issuers, every field verdicted) is satisfied by
> this step. The verdicts are the gold set every later step reads.

### 2. Produce the run report (accept rate, compounding, wall-clock)

```bash
python tools/edgar_scrubber/session_report.py \
    --session pilot-165 --spec structured_note
```

This prints markdown covering three criteria straight from the recorded
verdicts:

- **Per-field accept rate** -- an upper bound on precision (a reject records
  absence, not whether the model hallucinated there; the authoritative
  precision is step 3). *Criterion 2.*
- **Compounding split** -- accept rate over the first half of the validated
  documents vs the second half. Positive delta = later documents accepted more,
  the compounding VALIDATION_UI.md claims. *Criterion 4.*
- **Per-document wall-clock** -- first-verdict-to-last per document, so "ten
  validated minutes" can be confirmed or corrected. *Criterion 5.*

### 3. Run the eval harness against the held-out slice for a baseline

```python
from field_spec import load_specs
from validation import ValidationStore, LadderExtractor
from eval_harness import HeldOutCase, run_eval, EvalReportStore
from session_report import render_eval_markdown

spec = load_specs()["structured_note"]
store = ValidationStore()
# Build one HeldOutCase per reserved document (render_doc/sections come from the
# same crawl the session validated); then:
report = run_eval(spec, extractor, cases, store, report_id="pilot-165-baseline",
                  generated_at="...", local_model="qwen2.5:7b-instruct-q4_K_M")
EvalReportStore().save(report)              # persist it
print(render_eval_markdown(report))         # per-field precision/recall/span
```

`run_eval` refuses any case not marked held-out, so a baseline can never be
built from a document that also trained the exemplars it grades. Feed the
report back into `render_session_report(..., eval_report=report)` to fold the
held-out precision into the single run report. *Criterion 3.*

## Acceptance criteria -> where each is satisfied

| criterion | produced by |
|---|---|
| 20 filings, >=4 issuers, every field verdicted | step 1 (live sitting) |
| per-field precision against the labels | step 2 (accept-rate upper bound) + step 3 (authoritative) |
| eval harness baseline over the held-out slice | step 3 |
| extraction improves on docs 11-20 vs 1-10 | step 2 compounding split |
| wall-clock per document | step 2 timings |

Steps 2 and 3 are automated and tested (`test_session_report.py`,
`test_eval_harness.py`); step 1 is the human sitting the whole chain was built
to enable.
