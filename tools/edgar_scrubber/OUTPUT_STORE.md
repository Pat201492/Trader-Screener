# EDGAR Scrubber — Output Store & Graduation Boundary (issue #109)

Where scrubber extractions land, and the rule that keeps this tool from quietly
becoming a second EDGAR ingest pipeline.

> Part of #95. Enforces the boundary [`ARCHITECTURE.md`](../../ARCHITECTURE.md)
> states: **the Stock-Data-Pipeline is the only writer of shared data, and "don't
> fork the ingest scripts."**

## The rule

**The scrubber is exploratory, human-supervised, and project-local. It is not a
production feed.**

- Output is a **local tool datastore**, never a pipeline write. The scrubber does
  not get write access to shared data — not once, not as a convenience.
- Trader-Screener reads scrubber output the same way it reads everything else: as
  a **reader**.
- A field is allowed to stay local **only while it is being explored.**

Stock-Data-Pipeline already ingests EDGAR. Without a stated rule the scrubber
becomes a second permanent EDGAR ingest path with its own datastore — and then
calcifies the way the shared-pipeline artifacts did (#90/#91 proposed that cleanup
and were closed not-planned, so nothing else is guarding this).

## The store

`output_store.py` is the **only** place a scrubber extraction is written, and it
writes to exactly one thing: a local SQLite file under `~/.edgar-scrubber/store/`
(override the home with `EDGAR_SCRUBBER_HOME`).

```python
from tools.edgar_scrubber import OutputStore, DocumentExtraction

store = OutputStore()                      # local SQLite; refuses a pipeline path
run = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")
store.write_document(run, DocumentExtraction.from_record(accession, doc, record))

store.query(underlying="S&P 500 Index")    # the first Research project's shape
```

### Boundary enforced in code, not just documented

- **No pipeline write path exists.** The module imports nothing from the pipeline
  and exposes no `publish` / `push_upstream` / `to_pipeline` method. Its whole
  write surface is an *insert* into the local SQLite.
- **`OwnershipError` on a shared destination.** `OutputStore(path)` refuses a path
  under a `Stock-Data-Pipeline` tree, a path named like a pipeline artifact
  (`universe.json`, `fundamentals.json`, …), or anything under
  `$STOCK_PIPELINE_DATA`. The boundary is a check, not a comment.

### Output format

One row per **`(run, accession, document, field)`** carrying:

| Column | Meaning |
|---|---|
| `value` / `value_num` | the extracted value (JSON; numeric mirror for range queries) |
| `unit` | the field's unit (`usd_per_1000`, `percent`, …) |
| `span_start` / `span_end` | character offsets into the source document — points back at the exact text |
| `provenance` | how it was pulled: `rule:anchor 'Issuer:'` (#107) or `model:qwen2.5-7b` (#104) |
| `confidence` | the model's self-report; the field-spec **flags** are the *checkable* signal (#102) |
| `flags` | field-spec validation flags — a bad extraction travels flagged, never dropped |

Document-level dimensions (`issuer`, `product_type`, `filing_date`, `underlyings[]`)
are denormalized so the store is **queryable by underlying, issuer, product type,
and date** — the shape the first Research project (#110, issuance-by-underlying)
needs.

### Append-only, with run stamps

Every write is stamped with a **run** (`spec_id`, `spec_version`, `started_at`). A
re-extraction under a new spec version lands as a **new run beside the old one**,
never an overwrite:

- `store.fields(accession, document)` → the latest run's values (the default read).
- `store.field_history(accession, document, field)` → that field across **every**
  spec version, oldest first. This is how you catch a spec change that **silently
  altered history** — the whole reason the store refuses to overwrite.

## Graduation checklist

A field that has been local for a long time with no project consuming it is not
exploratory anymore — it is **drift.** When a field proves out inside a Research
project (the hypothesis resolved, the field mattered) it **graduates.** Run this
checklist; do not skip a step:

1. **Port the extraction upstream.** Move the field spec + induced rules (#107)
   into the pipeline's EDGAR ingest. The pipeline is the writer from now on.
2. **Add it to the shared field contract.** So the screener, the Research tab, and
   the old Stock-App all see it — per the `ARCHITECTURE.md` rule that a new metric
   goes upstream (collect once, serve both).
3. **Set `graduatedTo` on the tool manifest entry** (the schema issue's field,
   #98) to the pipeline location the field now lives at.
4. **Retire the local extractor — do not leave it running in parallel.** Call
   `store.graduate_field(field, graduated_to, at)`. After that the store **refuses**
   to re-extract that field locally (`GraduationError`): two extractors for one
   field is two answers for one field.

### Drift is made visible, not inferred

The tool manifest surfaces **status (local vs graduated)** and **age since last
use** (`store.last_used()` / `store.manifest_stats()`). A local field that has gone
stale shows its age on the card so the drift is visible on sight rather than
discovered later.

## Files

| File | Purpose |
|---|---|
| `output_store.py` | the local, append-only, queryable store + ownership guard + graduation |
| `test_output_store.py` | stdlib gate — run directly, exit 0 = pass |
| `field_spec.py` | produces the canonical records this store persists (#102) |

Run the gate:

```bash
python tools/edgar_scrubber/test_output_store.py
```

## References

- Issue #95 — EDGAR scrubber (parent)
- Issue #102 — field spec (produces the records stored here)
- Issue #98 — tool manifest schema (`graduatedTo`, status)
- Issue #110 — first Research project (issuance-by-underlying), the store's first reader — [`Research/structured_note_issuance/`](../../Research/structured_note_issuance/README.md)
- [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — the only-writer / collect-once boundary
