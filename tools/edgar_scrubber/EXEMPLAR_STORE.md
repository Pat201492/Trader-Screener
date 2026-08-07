# EDGAR Scrubber — Exemplar Store: Few-Shot Prompt Assembly (issue #106)

Validated spans → few-shot prompt assembly, keyed by `(form, issuer, field)`.

> Part of #95. Blocked on #105 (human-in-loop validation — every verdict it
> records lands here immediately). Feeds rung 3 of #104's extraction ladder.

## The mechanism that actually learns

Not fine-tuning. A 7B does not weight-update from ten validated documents —
but it conditions hard on well-chosen exemplars, and that works **from
example #1**. When #95 says "the model learns what I want it to grab", this
is the machinery that delivers it.

```python
from tools.edgar_scrubber.validation import ValidationStore
from tools.edgar_scrubber.exemplars import ExemplarProvider
from tools.edgar_scrubber.extraction_ladder import ExtractionLadder

store = ValidationStore()                       # #105's local SQLite store
provider = ExemplarProvider(store, form="424B2")  # bound to one form
ladder = ExtractionLadder(spec, exemplars=provider, local_client=..., local_model=...)

result = ladder.extract("barrier_pct", text=doc_text, issuer="JPMorgan Chase "
                        "Financial Company LLC", accession=accession, document=doc)
# result's provenance carries the SPECIFIC exemplar-set version actually used
```

## Keyed by (form, issuer, field)

Issuer is in the key deliberately: 424B2 templates are near-identical
*within* an issuer and materially different *across* them. JPM, Citi, BofA
Finance and Bank of Montreal — all confirmed present in a single January 2025
result page (#99) — share neither heading structure nor vocabulary. Pooled
exemplars make the model worse on each. `form` keeps the pooled fallback tier
from mixing unrelated form types that happen to share a field name.

## Fallback chain

`ExemplarProvider.resolve(issuer, field)` never raises:

```
(form, issuer, field)         exact issuer, exact field
    -> (form, *, field)       pooled across every OTHER issuer, same form
        -> field description only   empty exemplar block; FIELD/DESCRIPTION/
                                     ANCHORS/BOUNDS (always present) is all
                                     that's left
```

A key is **thin** when the issuer has fewer than `thin_below` raw validated
verdicts (default 2) — not when *selection* happens to dedup several real
corrections into one rendered line. An unseen issuer degrades a tier; an
unseen field bottoms out at empty. Either way a prompt is always produced.

## Selection, not accumulation

Context is 8k on the local 7B (#103), shared with the document chunk — a
prompt padded with twenty near-identical accepts is worse than one with four
well-chosen ones. `select_exemplars()`:

- **prefers corrections over accepts** — a correction encodes a boundary the
  model got wrong; an accept only confirms what it already knew.
- **prefers diversity** — collapses exemplars that teach the same shape
  (same anchor + same value type) so a different product type or value
  format survives instead of being crowded out by near-duplicates.
- **keeps at least one negative** when one exists — a reserved slot, not
  competing on recency, since absence is a valid answer and this is the
  exemplar that suppresses a hallucinated value.
- **caps the count** per field; eviction is by this ranking, not recency.

## Prompt ordering is load-bearing

llama.cpp and Ollama reuse KV cache for identical prompt **prefixes**.
`extraction_ladder.build_messages()` assembles:

```
[ static: system prompt ]                    <- identical across all calls
[ static: field spec ]                       <- identical for one field
[ static: exemplars for (issuer, field) ]    <- identical within an issuer
[ VARYING: document chunk ]                  <- must be last
```

With this ordering ~600 tokens of exemplars prefill **once per issuer**
instead of once per document. Putting the document anywhere earlier breaks
prefix reuse and roughly triples prefill per call.

`ExtractionLadder._check_static_prefix()` enforces the real invariant: **the
static prefix is a pure function of the exemplar-set version** — same
version, same prefix, always. A version bump (a validation session
compounding exemplars mid-run — see #105) is allowed to change the prefix;
the *same* version silently rendering different text raises `AssertionError`
immediately, instead of the KV-cache saving disappearing silently in some
future refactor (#111).

## Versioning

Every `ExemplarSet` carries a `version` — a short content hash of the exact
rendered lines selected, in order, plus which fallback tier answered the
key. `extraction_ladder._lookup_exemplars()` reads it off the specific set
actually used for that call and stamps it into `Provenance.exemplar_set` —
#104's provenance log — on every extraction. When quality moves, the log
tells you whether exemplars, model, or documents changed: three different
fixes. Rungs that never touch exemplars (rule, XBRL) carry `exemplar_set:
null`.

## Backlog: LoRA

At ~500+ validated labels per form a LoRA on the 7B becomes viable and would
cut prompt size and latency further. **Not in scope** — few-shot has to be
working and measured first (#108), and the labels do not exist yet. The
store (#105's SQLite table) already carries every field a training-set
export would need (kind, value, anchor, span_text, accession, document), so
it does not need reshaping later.

## Files

| File | Purpose |
|---|---|
| `exemplars.py` | `ExemplarProvider` — fallback chain, selection policy, versioning |
| `test_exemplars.py` | stdlib gate — run directly, exit 0 = pass, no network |
| `validation.py` | `ValidationStore.exemplar_rows()` / `write_exemplar()` — the #105 handoff, storage layer |
| `extraction_ladder.py` | `static_prefix()`, `build_messages()` ordering, the runtime prefix guard, provenance stamping |

Run the gate:

```bash
python tools/edgar_scrubber/test_exemplars.py
```

## References

- Issue #95 — EDGAR scrubber (parent)
- Issue #105 — human-in-loop validation loop (blocks this issue; the store this reads)
- Issue #104 — extraction provider ladder (`exemplars=` plug point, provenance log)
- Issue #99 — issuer diversity within a single 424B2 result page (why issuer is in the key)
- Issue #108 — held-out eval set (measures whether assembled exemplars actually help)
- Issue #111 — prompt minimization / KV-cache reuse this ordering depends on
