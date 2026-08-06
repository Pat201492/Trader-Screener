# Document expansion + four-stage reduction (issue #101)

Part of #95, built on #99's `edgar_client` and blocked on #99 (both satisfied).
Implements the preprocessing half of #111's token budget: turns one accession
(150k-400k+ characters, spread across a dozen documents) into the ~8k-context
prompt a local 7B model (#103) can actually read.

```
tools/edgar_scrubber/
  document_expand.py   # "open EVERY document" + EX-107 inline XBRL
  normalize.py          # HTML -> text w/ span offsets + stage 1 (table pre-parse)
  reduce.py              # stages 2-4: boilerplate dedup, section split, sub-block
  test_document_expand.py
  test_normalize.py
  test_reduce.py
```

## "Open EVERY document"

A full-text search hit is a **filing**, and a filing is a bundle. The primary
424B2 is one item in `index.json`'s `directory.item[]`, alongside exhibits and
R-files. `document_expand.expand_accession(client, cik, accession)` fetches
and classifies **every** item in the manifest, not just the one
`EdgarClient.primary_document()` would guess at:

```python
from edgar_client import EdgarClient
from document_expand import expand_accession

client = EdgarClient("Your Name you@example.com", cache_dir=".edgar-cache")
bundle = expand_accession(client, cik, accession)

bundle.manifest()     # [{"name", "type", "size", "category", "text_length", ...}, ...]
bundle.primary()      # the ExpandedDocument classified "primary" (the 424B2 itself)
bundle.by_name("ex107.htm")
bundle.ex107          # Ex107Facts, or None if this accession has no fee exhibit
```

Every item gets a `category` (`primary | ex107 | exhibit | graphic | xml |
xbrl_data | other`) and, when it's prose, a `normalized` `NormalizedDocument`.
Graphics are classified but not fetched by default (`fetch_graphics=True` to
include them) -- they carry no extractable text and fetching them would burn
the 10 req/s ceiling (#99) for nothing.

### EX-107 (EX-FILING FEES) -- free, exact ground truth

Since 2022 the fee exhibit is inline-XBRL tagged with the SEC's `ffd:`
namespace. `parse_ex107_xbrl` scans `ix:nonFraction`/`ix:nonNumeric` facts by
**element localname** (a filer can bind any prefix to the `ffd` URI, so the
prefix itself is never trusted) and returns an `Ex107Facts`:

```python
facts.aggregate_principal   # canonical fields field_spec's cross_checks reference
facts.total_fee_amount
facts.fee_rate
facts.raw                   # EVERY parsed ffd: fact, verbatim -- nothing dropped
```

This is what `424b2_structured_note.json`'s `external_equals` cross-check
compares the model's prose read against (`aggregate_principal`, source
`"ex107"`) -- a mismatch is a high-signal flag that extraction went wrong on
that filing (#104). Absence (pre-2022 filing, or no fee exhibit) returns an
`Ex107Facts` with every field `None` rather than `None` itself -- absence is
informative, not an error.

## Normalization with span offsets

`normalize.normalize_html(html)` returns a `NormalizedDocument`:

```python
doc = normalize_html(raw_html)
doc.text          # plain text, tags stripped, entities decoded
doc.offset_map     # OffsetMap: normalized-text offset -> exact source offset
doc.tables         # FlatTable objects pulled out along the way (stage 1)
doc.source         # the original HTML, for resolve()'d spans to slice into
```

`OffsetMap.resolve(text_start, text_end)` returns the exact `(source_start,
source_end)` a normalized-text range came from -- through HTML entity
decoding, whitespace collapsing, and stripped tags. This is not an add-on:
#105 highlights the source span during exemplar review and #107 induces regex
anchors from where a span landed, so extraction that returns bare strings
with no provenance kills both. The reverse direction,
`OffsetMap.text_offset_for_source(source_pos)`, is what lets stage 4 center a
window on a **source**-offset anchor (see below).

`OffsetMap.compose(upstream)` is how a span survives being reduced multiple
times: each stage below builds its own map from *its* input text to *its*
output text, then composes against the previous stage's map, so a value found
in a boilerplate-stripped, section-split, sub-blocked fragment still resolves
straight back to the original document bytes.

## The four-stage reduction

A 424B2 runs 150k-400k+ characters; the extraction model is a 7B at 8k
context (#103), so the document has to shrink roughly 50x before it reaches a
prompt. Every stage is deterministic and **token-free**.

### 1. Table pre-parse (`normalize.py`, `parse_tables`)

Key terms live in HTML `<table>` elements. Raw table markup costs ~2,000
tokens and lands exactly where a 7B is weakest: numeric values in nested/
merged cells. `parse_tables` grid-parses each top-level `<table>` --
resolving colspan/rowspan into a full grid so a merged cell's value still
lands under every row/column it spans, and recursing into a nested `<table>`
inside a cell (routine in SEC filings as a layout wrapper or a sub-schedule)
-- into flat pairs:

```
Contingent Coupon Rate: 9.15% per annum
Coupon Barrier: 70.00% of Initial Value
Buffer Amount: 10.00%
```

~200 tokens instead of ~2,000. `normalize_html` pulls every top-level table
out of the linear text and substitutes its flattened form, so `doc.text`
already contains the cheap version -- there is no separate "raw vs. reduced"
document to keep in sync. Each `TablePair` carries its own `source_span`
(exact HTML offset of its value cell) independent of `doc.offset_map`, so a
value survives even inside a coarse-grained non-literal text span.

### 2. Boilerplate strip by cross-filing dedup (`reduce.py`)

```python
model = build_boilerplate_model(issuer, normalized_docs, threshold=0.8)
reduced = strip_boilerplate(doc, model)
reduced.report.reduction_pct   # MEASURED, per issuer
```

Paragraphs are hashed (lowercased, whitespace-collapsed) and counted **at
most once per document** -- a boilerplate line repeated three times within
one filing must not look like it came from three filings. Present in
`>= threshold` (default 80%) of the corpus makes it boilerplate. A
single-document corpus never flags anything: nothing is cross-filing yet.
Computed once per issuer; refresh the model (re-run `build_boilerplate_model`)
when #107 detects a template change.

### 3. Section split (`reduce.py`, `split_sections`)

```python
sections = split_sections(doc)       # {"key_terms": [SectionSpan, ...], ...}
text_for_sections(doc, sections, ["coupon_terms"])   # cold-path context for a field
sections_for_spec(sections, spec.sections)            # narrow to one FieldSpec's vocab
```

Headings are detected by matching a **short, standalone line** (the
`_heading_candidate` guard: no trailing sentence punctuation, under ~100
chars -- so a body sentence that happens to mention "the estimated value of
the notes" mid-paragraph is never mistaken for a section boundary) against a
keyword vocabulary (`SECTION_PATTERNS`) built to be robust across issuers
that share no heading structure: JPM's "Key Terms" / Citi's "Summary of
Terms" / GS's "General Terms" / Barclays' "Indicative Terms" all classify as
`key_terms`. `test_reduce.py` validates all four independently.

This is an accuracy mechanism, not just a cost one: a barrier percentage
pulled from `Hypothetical Examples` instead of `Key Terms` is a classic
silent wrong answer. Because a field's `sections[]` (see `field_spec.py`)
can only ever be matched against text this stage bucketed under that name,
that failure mode is structurally impossible, not just unlikely.

Content before the first recognized heading is always `cover` (SEC cover-page
terms routinely precede any heading). A section name can recur (summarized up
top, detailed again later) -- every occurrence is kept.

### 4. Sub-block routing (`reduce.py`, `sub_block`) -- warm path

```python
sub_block(doc, text_offset=1234, window=500)          # anchor already in doc.text
sub_block(doc, source_offset=98765, window=500)        # anchor from a #107 rule / stored FieldValue.span
```

Once #107 has induced an anchor for a field, this sends +/-500 characters
around it instead of the whole section -- section routing is the cold path,
sub-block is the warm path. `source_offset` (the shape `output_store.
FieldValue.span` persists) is converted through `OffsetMap.
text_offset_for_source` first.

## Hard constraint: everything composes back to the source

Every stage's output carries an `OffsetMap` composed onto the previous
stage's -- `normalize_html` -> `strip_boilerplate` -> `split_sections` /
`sub_block` -- so a span resolved after all four stages still slices the
**original** document bytes exactly. `test_reduce.py`'s end-to-end case
chains all four and asserts the final resolved range is both correct and
strictly smaller than the whole document (proof the reduction actually
narrowed things, not just relabeled them).

## Test

```
python tools/edgar_scrubber/test_normalize.py         # stage 1 + span offsets
python tools/edgar_scrubber/test_document_expand.py    # every doc + EX-107
python tools/edgar_scrubber/test_reduce.py              # stages 2-4 + 4-issuer split + end-to-end chain
```

Stdlib only, no network (document_expand's gate scripts a `FakeTransport`,
same convention as `test_edgar_client.py`), exit 0 = pass.

## References

- Issue #101 (this module) -- part of #95, built on #99
- `edgar_client.py` / [`README_client.md`](README_client.md) -- fetch/cache/limiter foundation
- `field_spec.py` / [`FIELD_SPEC.md`](FIELD_SPEC.md) -- the `sections[]` vocabulary stage 3 routes into, and the `external_equals` cross-check EX-107 feeds
- Issue #103 (7B at 8k context -- the reduction target)
- Issue #104 (Claude escalation -- the EX-107/prose cross-check flag)
- Issue #105 / #107 (exemplar review + rule induction -- consumers of span offsets)
- Issue #111 (token budget -- this module is the preprocessing half)
