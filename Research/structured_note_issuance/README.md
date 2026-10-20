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

## Part B -- causal: moved to one test (#341)

Part B used to live here as `causal_test.py` (18 trials, |t| >= 2.7) next to
a second implementation of the same hypothesis,
[`Research/dealer_hedging_causal_test.py`](../dealer_hedging_causal_test.py)
(27 trials, placebo-calibrated). The two disagreed on status and grid, so
#341 kept one: **`dealer_hedging_causal_test.py`** is the single module, with
one declared grid (`TRIAL_COUNT = 27`) and one registry entry
(`dealer-hedging-barrier-causal-test`).

What the retired module contributed is kept there:

- the knowledge date is the EDGAR acceptance (file) date, never
  `pricing_date` -- `run_knowledge_date_check()`;
- the A-vs-B truncation gate -- `run_truncation_gate()` on the synthetic
  fixture, and `test_truncation_gate_on_the_real_builder` in
  [`Research/test_dealer_hedging_real.py`](../test_dealer_hedging_real.py)
  on the real-data universe builder.

Real inputs come from [`Research/dealer_hedging_data.py`](../dealer_hedging_data.py)
(#340): 424B2 filing counts per underlying per EDGAR file date (search
metadata only), sampled barrier levels via the key-terms extractor, and the
pipeline's prices.

```bash
python Research/dealer_hedging_data.py build        # real inputs (cached)
python Research/dealer_hedging_causal_test.py --real  # declared grid -> Research/dealer_hedging_results.json
```

## Graduation

If a field proves out here it graduates to Stock-Data-Pipeline per the
checklist in
[`tools/edgar_scrubber/OUTPUT_STORE.md`](../../tools/edgar_scrubber/OUTPUT_STORE.md#graduation-checklist)
(#109). Nothing has graduated yet -- there's no real verdict to graduate on.

## Files

Part A lives in `tools/edgar_scrubber/` (#130) -- see that project's own
Files table in [`RESEARCH_MAPS.md`](../../tools/edgar_scrubber/RESEARCH_MAPS.md).
Part B is `Research/dealer_hedging_causal_test.py` (see above); this
directory now holds only this README.

## References

- Issue #95 -- EDGAR scrubber (parent)
- Issue #97 -- Research registry / dashboard section
- Issue #100 -- resumable crawl
- Issue #107 -- rule induction (feeds the extraction ladder this project reads from)
- Issue #109 -- output store + tool interface + graduation checklist
- Issue #130 -- Part A of this issue, shipped separately: [`tools/edgar_scrubber/research_maps.py`](../../tools/edgar_scrubber/research_maps.py) / [`RESEARCH_MAPS.md`](../../tools/edgar_scrubber/RESEARCH_MAPS.md)
- [`Research/Research.md`](../Research.md) -- the Research terminal's dashboard section doc
- [`lookahead-gate/`](../../lookahead-gate/) -- the A-vs-B truncation test this project's look-ahead posture is built on
