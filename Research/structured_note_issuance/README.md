# Structured-note issuance (issue #110)

Part of #95. The Research project that proves the Research + Tools loop
closes end-to-end: crawl (#100) -> validate (#105) -> extract (#102/#104/
#107) -> hypothesis (this) -> verdict (this). Registered in the Research
registry: [`web-dashboard/research-projects.json`](../../web-dashboard/research-projects.json),
id `structured-note-issuance`.

Reframed from the original issue on purpose: the causal claim in Part B is
engineering-cheap but inference-hard, and staking the whole section on it
risked looking like a failure when the pipeline actually worked fine. So the
**descriptive map (Part A) is the deliverable** and ships regardless of what
Part B finds. The causal dealer-hedging test (Part B) is the stretch goal on
top of it.

Both parts read the EDGAR scrubber's local `OutputStore` through its public
`query()` / `fields()` interface only (#109) -- neither ever touches the
sqlite file directly, so the ownership boundary `output_store.py` enforces
stays the single place that boundary is checked.

## Part A -- descriptive (the deliverable)

Three outputs, all pure extraction. No modeling, no controls, no p-values.

1. **Issuance by underlying, over time.** `aggregate_principal` grouped by
   `underlyings[]` and month. A map of where structured-note exposure is
   being created.
2. **Barrier and autocall level clustering vs spot.** `barrier_pct`,
   `coupon_barrier_pct`, `autocall_barrier_pct` mapped to an absolute price
   via `initial_underlying_value` -- where protection levels pile up on
   each underlying, stated without any claim about what causes it.
3. **Issuer markup league table.** `estimated_value_per_1000` against the
   $1,000 issue price, aggregated by issuer and product type over time.
   Costs nothing extra -- the field is already extracted for Part B.

**Already shipped, as #130** -- carved out of this issue by scope triage and
merged ahead of Part B: [`tools/edgar_scrubber/research_maps.py`](../../tools/edgar_scrubber/research_maps.py)
(the three aggregations + SVG charts + written findings),
[`generate_research_maps.py`](../../tools/edgar_scrubber/generate_research_maps.py)
(the CLI runner), gated by
[`test_research_maps.py`](../../tools/edgar_scrubber/test_research_maps.py).
Full writeup: [`tools/edgar_scrubber/RESEARCH_MAPS.md`](../../tools/edgar_scrubber/RESEARCH_MAPS.md).
This project (the rest of #110) is Part B only -- the causal test below.

```bash
# 1. crawl (issue #100) -- the saved query already exists:
python tools/edgar_scrubber/crawl.py tools/edgar_scrubber/queries/424b2-structured-notes.json

# 2. run the extraction ladder over the crawled accessions (issue #104/#107)
#    -- see tools/edgar_scrubber/EXTRACTION_LADDER.md. This is what calls
#    OutputStore.write_document() and populates the local store.

# 3. build the maps
python tools/edgar_scrubber/generate_research_maps.py --out-dir tools/edgar_scrubber/research_output
```

## Part B -- causal (the stretch)

Code: [`causal_test.py`](causal_test.py) (hypothesis, falsifier, declared
trial grid, feature computation, matched controls, verdict logic),
[`test_causal_test.py`](test_causal_test.py) (the gate -- includes the
look-ahead A-vs-B truncation test).

### Hypothesis

> Aggregate 424B2 structured-note issuance on an underlying creates
> measurable dealer hedging exposure. Issuers sell notes with embedded
> short-put/short-call profiles, hedge the book in the underlying and its
> options, and that hedging leaves a footprint -- concentrated near barrier
> and autocall levels, and around observation dates when hedges roll.

**Prediction:** underlyings with elevated recent issuance show different
realized-vol and price behavior near clustered barrier levels than matched
controls with little issuance.

### Falsifier

> No difference between high-issuance and matched low-issuance underlyings
> after controlling for size, liquidity and options open interest.

Stated up front, because this hypothesis is attractive enough to rationalize
a null away.

### Why it is hard

- **N is ~100 underlyings, not 30,000 filings.** Issuance concentrates hard.
  Filing count is not sample size, and treating it as such is the easiest
  way to manufacture a false result here.
- **Confounded at the root.** The heavily-noted names (SPX, NDX, RTY, AAPL,
  NVDA, TSLA) have distinct vol behavior for reasons unrelated to note
  hedging.
- **Matched controls barely exist.** The heavily-noted universe is small and
  unlike everything else -- `match_controls()` returns *fewer* than the
  requested `k` (or none) rather than padding with a bad match.
- **Issuance is not net exposure.** Dealers hedge a whole book and offset
  against other flow.

### Trial count -- declared before the run

Per the repo's deflation discipline (issue #47), the number of variants is
declared in [`causal_test.py`](causal_test.py) before any of them run:

| Axis | Values | Count |
|---|---|---|
| Issuance window (trailing trading days) | 30, 90, 180 | 3 |
| Barrier-proximity definition (% of level) | 2, 5 | 2 |
| Forward horizon (trading days) | 5, 20, 60 | 3 |

**`TRIAL_COUNT = 3 x 2 x 3 = 18`**, asserted in the module at import time. If
this grid grows mid-project, the project restarts -- that is the point of
committing to it.

A trial only "clears the bar" at `|t| >= CRITICAL_T = 2.7`, a Bonferroni-style
threshold for 18 declared two-sided trials at a nominal alpha of 0.05 (no
scipy dependency -- stdlib `statistics` computes Welch's t-statistic, and
`CRITICAL_T` is compared against directly rather than an exact p-value). The
verdict requires a **majority** of the scoreable trials to clear that bar **in
the same direction**; no single trial can carry it.

### Look-ahead posture

Real risk, and subtle. A 424B2 filing carries a `pricing_date` (what the
document itself says) and an EDGAR full-text-search `file_date` (when EDGAR
actually indexed/accepted it), and they **differ** -- `pricing_date` can
predate public disclosure by several days.

**The knowledge date is the acceptance timestamp.** `causal_test.py`'s
`trailing_issuance()` reads only a caller-supplied `Filing.acceptance_date`
(sourced from the crawler's `file_date` -- crawl-time metadata, not an
extracted field) and never `pricing_date`. `research_maps.py` (Part A, #130)
uses `pricing_date`/`filing_date` on purpose: it makes no forward
prediction, so it has no look-ahead exposure to guard.

`test_causal_test.py` adapts `lookahead-gate/`'s A-vs-B truncation
methodology from a price-history signal to this event-stream (filing)
signal: it builds a point-in-time snapshot of "which filings EDGAR had
actually accepted by day N," compares the feature computed from the full
history against that snapshot for every overlapping day, and -- per that
file's own "prove the gate has teeth" discipline -- runs the same comparison
against a deliberately buggy sibling that keys off `pricing_date` instead.
The shipped implementation has zero A-vs-B mismatches; the leaky one is
caught (see the test's "look-ahead A-vs-B truncation gate" section). Passes
the gate:

```bash
python Research/structured_note_issuance/test_causal_test.py
```

### Data required

From the scrubber (via `research_maps.load_documents()` plus a
caller-supplied accession -> acceptance-date map): `underlyings[]`, `aggregate_principal`,
`barrier_pct`, `autocall_barrier_pct`, `observation_dates[]`, `pricing_date`,
`issuer`, `product_type`, `estimated_value_per_1000`,
`initial_underlying_value`.

From the existing pipeline (Stock-Data-Pipeline, not this tool --
`OUTPUT_STORE.md`'s ownership boundary): prices, realized vol, options open
interest, ADV. `causal_test.py` defines the data contract
(`UnderlyingUniverse`, `PriceBar`) and never fetches this data itself.

### Verdict

**Not yet run against real data.** No live crawl exists in this checkout, so
there is no real underlying universe, filing set, or price/OI/ADV data to
run the declared 18 trials against. `test_causal_test.py`'s end-to-end
section runs the full pipeline -- feature computation, matched controls, all
18 trials, verdict logic -- against a synthetic universe purely to prove the
mechanism is wired correctly; that run is explicitly labeled synthetic and
is **not** a finding about any real underlying.

Per the acceptance criteria this project's verdict is published whatever it
is, so the honest current verdict is:

> **Inconclusive -- no data yet.** Zero real trials have been scored because
> the local `OutputStore` is empty (no crawl has been run) and no real
> price/vol/OI/ADV universe has been wired in. This is the same
> `"inconclusive"` outcome `verdict()` returns when fewer than half the
> declared trials can be scored, applied honestly rather than dressed up
> with fabricated numbers. It becomes a real supported/refuted/inconclusive
> verdict the moment the crawl (step 1 above) has run and a real
> `UnderlyingUniverse` per symbol is supplied to `causal_test.run_all_trials()`
> -- no code changes required to get there.

## Graduation

If a field proves out here it graduates to Stock-Data-Pipeline per the
checklist in
[`tools/edgar_scrubber/OUTPUT_STORE.md`](../../tools/edgar_scrubber/OUTPUT_STORE.md#graduation-checklist)
(#109). Nothing has graduated yet -- there's no real verdict to graduate on.

## Files

Part A lives in `tools/edgar_scrubber/` (#130) -- see that project's own
Files table in [`RESEARCH_MAPS.md`](../../tools/edgar_scrubber/RESEARCH_MAPS.md).
This directory carries Part B only:

| File | Purpose |
|---|---|
| `causal_test.py` | Part B: hypothesis, falsifier, declared trial grid, feature computation, matched controls, verdict |
| `test_causal_test.py` | Part B gate, including the look-ahead A-vs-B truncation test |

## References

- Issue #95 -- EDGAR scrubber (parent)
- Issue #97 -- Research registry / dashboard section
- Issue #100 -- resumable crawl
- Issue #107 -- rule induction (feeds the extraction ladder this project reads from)
- Issue #109 -- output store + tool interface + graduation checklist
- Issue #130 -- Part A of this issue, shipped separately: [`tools/edgar_scrubber/research_maps.py`](../../tools/edgar_scrubber/research_maps.py) / [`RESEARCH_MAPS.md`](../../tools/edgar_scrubber/RESEARCH_MAPS.md)
- [`Research/Research.md`](../Research.md) -- the Research terminal's dashboard section doc
- [`lookahead-gate/`](../../lookahead-gate/) -- the A-vs-B truncation test this project's look-ahead posture is built on
