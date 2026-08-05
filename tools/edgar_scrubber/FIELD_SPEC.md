# 424B2 Field Spec (issue #102)

The **"specific information that is designatable"** half of #95: *what* to pull
from a filing is **data, not code**. Field definitions live in
[`field_specs/*.json`](field_specs). Adding a field is a JSON edit — no code
change, no redeploy.

```
tools/edgar_scrubber/
  field_specs/
    424b2_structured_note.json   # population A — structured / market-linked notes
    424b2_shelf_takedown.json    # population B — plain debt / equity takedowns
  schema_registry.py             # canonical shape registry (#86 role)
  field_spec.py                  # runtime: load, detect, validate, wire-map
  test_field_spec.py             # stdlib gate — run directly, exit 0 = pass
```

## Why two specs

Both file under Rule 424(b)(2) but need different field sets:

| Population | File | Signal |
|---|---|---|
| **A — structured notes** | `424b2_structured_note.json` | High. `estimated_value_per_1000` is the SEC-mandated issuer markup; `underlyings[] + aggregate_principal` aggregate into issuance-by-underlying, a dealer-hedging proxy (#110). |
| **B — shelf takedowns** | `424b2_shelf_takedown.json` | Lower individually, but repeat equity takedowns off one shelf are a dilution pattern. The default when the note markers are absent. |

`field_spec.detect_population(text)` scores each spec's detection signals and
returns the matching spec. Structured-note markers are specific and weighted
high; shelf takedown is the low-signal default. When nothing clears threshold or
two populations tie, `Detection.confident` is `False` — route those to review or
the model rather than trust a guess.

## Anatomy of a field

```json
{
  "name": "estimated_value_per_1000",   // canonical name — the ONLY name that may be stored
  "type": "number",
  "unit": "usd_per_1000",
  "required": true,
  "extraction_path": "fixed-anchor",    // fixed-anchor | table-resident | variable
  "sections": ["cover", "estimated_value"],   // which doc sections can hold it — routing per #101
  "wire_key": "ev1000",                 // #104 transport key — mapped back in code, NEVER stored
  "anchors": ["our estimated value", "estimated value of the notes"],
  "bounds": { "min": 900, "max": 1000 },   // sanity range — a bad read is caught before a chart
  "description": "..."
}
```

### `extraction_path` — where #107 should look

Every field is tagged, because #107 is regex-first and the spec is where that
intent lives:

- **`fixed-anchor`** — a literal label precedes the value in nearly every filing
  (issuer, CUSIP, dates, `estimated_value_per_1000`, agent commission). Should be
  served by rules, not the model.
- **`table-resident`** — the barrier / buffer / participation / cap cluster. #101
  flattens the terms table to `label: value` pairs first, making these
  rule-friendly too.
- **`variable`** — `product_type`, worst-of basket parsing, irregular observation
  schedules. Genuinely needs the model.

### `bounds` — validation travels with the spec

Per-field sanity bounds catch a bad extraction *at extraction time*, before it
reaches a chart. These are also #104's primary confidence signal — checkable,
unlike a model's self-reported confidence. `validate_record()` returns a list of
`Flag`s and **never drops or mutates the record** — a bad value is surfaced,
flagged, and held back, not silently discarded.

Cross-field / cross-exhibit checks live in `cross_checks[]`:

- `maturity_date > pricing_date`
- `aggregate_principal` vs the EX-107 fee exhibit (#101) — an exact value to check
  the prose read against (`external_equals`, pass `ex107=` to `validate_record`).

## Wire format vs stored format

#104 uses short output keys (`ev1000`) on the model boundary to cut decode
tokens. **Canonical names live in the spec and the registry (#86); the short keys
are a transport detail and must never reach a stored record.**

```python
spec.wire_legend()            # {"ev1000": "estimated_value_per_1000", ...} — #104's decode map
spec.from_wire(model_payload) # short keys -> canonical, before storage
spec.assert_canonical(record) # raises if a wire key leaked into a stored record
```

`validate_record` also raises a `wire_key_leak` flag if a short key is found in a
record, as a second line of defense.

## The shape is registered, not inline (#86)

The meta-schema — what a valid field definition and a valid form-type spec look
like — is registered once in `schema_registry.py` (the pipeline-side embodiment
of #86's `schema-registry.js`). Each JSON spec is validated against that
registered shape at load time (`load_spec`), so:

- a malformed spec fails **loudly at load**, not silently at extraction;
- the structure is defined in exactly one place;
- `schema_registry.field_spec_shape_as_data()` re-exports the shape as
  language-neutral JSON so the JS #86 registry can adopt the identical
  definition instead of re-typing it.

## Usage

```python
import field_spec as fs

specs = fs.load_specs()                     # loads every field_specs/*.json at runtime
d = fs.detect_population(document_text)      # -> Detection(spec, population, confident, ...)
record = d.spec.from_wire(model_payload)     # short keys -> canonical
flags  = d.spec.validate_record(record, ex107=fee_exhibit)
d.spec.assert_canonical(record)              # belt-and-suspenders before storing
```

## Test

```bash
python tools/edgar_scrubber/test_field_spec.py   # exit 0 = pass
python tools/edgar_scrubber/field_spec.py        # loader/detector self-check
python tools/edgar_scrubber/schema_registry.py   # shape self-check + JSON export
```

## Left for downstream issues

`exemplars[]` (#105) and `rule` (#107) are already accepted by the field shape;
they are simply absent until those issues populate them. Adding them is — as with
every other field attribute — a data edit to the JSON.
