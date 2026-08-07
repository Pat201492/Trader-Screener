# Structured-note issuance maps (issue #130, Part A of #110)

`#110` was scope-triaged into two parts. This is **Part A**: three
**descriptive** outputs built straight from what the scrubber already
extracted -- no modeling, no controls, no p-values. Part B (whatever
hypothesis-testing work follows) ships or doesn't independently of Part A.

```
tools/edgar_scrubber/
  research_maps.py          # aggregation + SVG charts + written findings
  generate_research_maps.py # CLI: --demo (synthetic) or a real OutputStore
  test_research_maps.py     # every #130 acceptance criterion
  research_output/          # generated chart/data/finding files land here
```

## The three outputs

Each one is a chart (self-contained SVG, no plotting dependency) plus a
short written finding (a markdown paragraph built from the real aggregated
numbers -- never a template with blanks left in).

### 1. Issuance by underlying over time

`aggregate_principal` grouped by `underlyings[]` and month
(`research_maps.issuance_by_underlying`). A stacked bar per month, one
segment per underlying (`svg_stacked_bar`).

A basket/worst-of note lists more than one underlying against the SAME
`aggregate_principal`. Splitting that principal evenly across every listed
name is the only way the sum across underlyings doesn't double-count a
dollar -- the trade-off (you lose which single name in the basket actually
drove the hedge) is stated in the finding text, not hidden in the number.

### 2. Barrier / autocall level clustering vs spot

`barrier_pct`, `coupon_barrier_pct`, `autocall_barrier_pct` mapped to
absolute price levels via `initial_underlying_value`
(`research_maps.barrier_clustering`):

```
absolute_level = initial_underlying_value * pct / 100
```

A scatter of `absolute_level` (y) against `spot` == `initial_underlying_value`
(x), one color per barrier field (`svg_scatter`), is what actually shows
clustering: notes sharing the same `barrier_pct` fall on a common ray from
the origin regardless of the underlying's price scale, which a plain
percent histogram across mixed underlyings would not surface. `bucket_width`
(default 5 points) turns "clusters near 70%" into a count instead of
something a reader has to eyeball off the scatter.

### 3. Issuer markup league table

`estimated_value_per_1000` vs the fixed $1,000 issue price, aggregated by
`(issuer, product_type)` over time (`research_maps.issuer_markup_league`):

```
markup_pct = (1000 - estimated_value_per_1000) / 1000 * 100
```

Ranked descending by average markup -- the league table -- rendered as a
horizontal bar chart (`svg_hbar`). `estimated_value_per_1000` is the
SEC-mandated "our estimated value of the notes" disclosure (#102's
`FIELD_SPEC.md`), so this ranks disclosed markup as filed, not an inferred
or modeled fair value.

## Why no modeling, controls, or p-values

Because #130 says not to. This is a first pass over what the extractor
already produced (dollars, percents, dates) with grouping and arithmetic --
nothing here estimates a fair value, controls for a confound, or claims a
finding is statistically distinguishable from noise. Every finding string
ends with an explicit "descriptive only" line so the output can't be
mistaken for more than it is. A field that proves out enough to justify
modeling is a **graduation candidate**, following the same checklist
[`OUTPUT_STORE.md`](OUTPUT_STORE.md#graduation-checklist) already defines --
not a reason to sneak modeling into this module.

## Tool-interface boundary

`research_maps.load_documents` reads the store through `OutputStore.query()`
and `OutputStore.fields()` only -- the public interface #109 exposes -- never
the sqlite file directly. That is #130's stated scope ("Consumes scrubber
output through the tool interface only (#109)") and it is also just correct
layering: if the store's schema changes, this module doesn't need to know.

## Run it

Offline, synthetic data (no crawl, no store needed) -- every number in its
`findings.md` is clearly synthetic, not a measurement:

```bash
python tools/edgar_scrubber/generate_research_maps.py --demo
```

Against a real local store, once a crawl + validation loop (#105) has
populated it:

```bash
python tools/edgar_scrubber/generate_research_maps.py \
    --out-dir tools/edgar_scrubber/research_output \
    [--issuer "..."] [--product-type "..."] [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]
```

If the store is empty, the findings say so explicitly ("nothing to
aggregate" / "nothing to rank") rather than fabricating a number -- the same
posture [`THROUGHPUT_BENCHMARK.md`](THROUGHPUT_BENCHMARK.md) takes for
axes it hasn't measured yet.

Both paths write the same six files plus `findings.md` into `--out-dir`:
`issuance_by_underlying.{json,svg}`, `barrier_clustering.{json,svg}`,
`issuer_markup_league.{json,svg}`.

## Test

```bash
python tools/edgar_scrubber/test_research_maps.py
```

Stdlib only, no network, no model -- an in-memory `OutputStore` seeded with a
handful of notes (including one multi-underlying basket note, to exercise
the even-split rule, and one note missing `aggregate_principal`, to exercise
graceful exclusion). Exit 0 = pass.

## References

- Issue #130 (this module) -- Part A carved out of #110 by scope triage
- Issue #110 -- the parent: first Research project, issuance-by-underlying
- [`OUTPUT_STORE.md`](OUTPUT_STORE.md) -- the store's public read interface (#109)
  this module consumes, and the graduation checklist a proven-out field follows
- [`FIELD_SPEC.md`](FIELD_SPEC.md) -- the fields aggregated here
  (`aggregate_principal`, `underlyings[]`, `barrier_pct`, `coupon_barrier_pct`,
  `autocall_barrier_pct`, `initial_underlying_value`, `estimated_value_per_1000`)
