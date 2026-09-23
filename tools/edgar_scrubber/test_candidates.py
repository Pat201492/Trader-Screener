"""
Gate for deterministic value-candidate enumeration (issue #177). Same
convention as `test_normalize.py` / `test_field_spec.py`: stdlib only, run
directly, exit 0 = pass.

Every acceptance criterion in #177 is checked here:

  * `candidates_for` returns each candidate's parsed value, its label, its
    offsets into the given text, and the literal string as printed;
  * every returned span, sliced out of the input text, is exactly that
    candidate's printed form -- asserted for EVERY candidate in EVERY fixture;
  * number/percent fields enumerate `70`, `70.00%`, `1,000`, `$979.00`; date
    fields enumerate via `normalize.parse_date_prose` and return ISO values;
  * string, enum and array fields return an empty list;
  * on the four adjacent dates a 424B2 states (strike, pricing, issue,
    maturity) four candidates come back, each with its own distinct label;
  * the module imports neither a model client nor a network library, and this
    gate runs with neither.

Run:  python tools/edgar_scrubber/test_candidates.py
"""
from pathlib import Path

import candidates as cand
import field_spec as fs

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def field(name, ftype):
    """A minimal real FieldDefinition -- `candidates_for` reads only `.type`,
    but building the actual spec type keeps the gate honest about the contract."""
    return fs.FieldDefinition.from_dict(
        {"name": name, "type": ftype, "extraction_path": "variable"})


def spans_slice_to_raw(text, cands):
    """The invariant `candidates_for` exists to provide: every span, sliced out
    of the input text, is exactly that candidate's printed form."""
    return all(text[c["span"][0]:c["span"][1]] == c["raw"] for c in cands)


# --------------------------------------------------------------------------- #
section("number/percent fields enumerate the printed forms")
# --------------------------------------------------------------------------- #

_NUM_TEXT = (
    "Participation Rate: 70\n"
    "Coupon Barrier: 70.00%\n"
    "Denomination: 1,000\n"
    "Estimated Value: $979.00 per note"
)
num = cand.candidates_for(field("participation_rate", "percent"), _NUM_TEXT)
raws = [c["raw"] for c in num]
check("'70' enumerated", "70" in raws)
check("'70.00%' enumerated", "70.00%" in raws)
check("'1,000' enumerated", "1,000" in raws)
check("'$979.00' enumerated", "$979.00" in raws)

by_raw = {c["raw"]: c for c in num}
check("'70' parses to 70", by_raw["70"]["value"] == 70)
check("'70.00%' parses to 70.0 (% is notation, not magnitude)",
      by_raw["70.00%"]["value"] == 70.0)
check("'1,000' parses to 1000, comma dropped", by_raw["1,000"]["value"] == 1000)
check("'$979.00' parses to 979.0, $ dropped", by_raw["$979.00"]["value"] == 979.0)
check("every span slices back to its printed form",
      spans_slice_to_raw(_NUM_TEXT, num))
check("each candidate carries value, label, span and raw",
      all({"value", "label", "span", "raw"} <= set(c) for c in num))
check("'number' type enumerates the same forms as 'percent'",
      [c["raw"] for c in cand.candidates_for(field("denomination", "number"),
                                             _NUM_TEXT)] == raws)

# --------------------------------------------------------------------------- #
section("date fields enumerate via parse_date_prose and return ISO")
# --------------------------------------------------------------------------- #

_DATE_TEXT = "Maturity Date: August 31, 2028 (or 8/31/2028, ISO 2028-08-31)"
dates = cand.candidates_for(field("maturity_date", "date"), _DATE_TEXT)
check("every date candidate value is a real ISO string",
      all(c["value"] == "2028-08-31" for c in dates) and len(dates) == 3)
check("ISO value matches parse_date_prose exactly",
      all(c["value"] == cand.parse_date_prose(c["raw"]) for c in dates))
check("every date span slices back to its printed form",
      spans_slice_to_raw(_DATE_TEXT, dates))

_BAD_DATE = "Observation Date: September 31, 2028"
check("a date-shaped run that is not a real date is dropped, not guessed",
      cand.candidates_for(field("final_valuation_date", "date"), _BAD_DATE) == [])

# --------------------------------------------------------------------------- #
section("the four adjacent dates a 424B2 states, each under its own label")
# --------------------------------------------------------------------------- #

_FOUR = (
    "Strike Date: August 27, 2026\n"
    "Pricing Date: August 28, 2026\n"
    "Issue Date: September 2, 2026\n"
    "Maturity Date: August 31, 2028"
)
four = cand.candidates_for(field("maturity_date", "date"), _FOUR)
check("four candidates come back", len(four) == 4)
check("each carries its own distinct label",
      len({c["label"] for c in four}) == 4)
check("the labels are the four the filing prints",
      {c["label"] for c in four} ==
      {"Strike Date", "Pricing Date", "Issue Date", "Maturity Date"})
check("maturity's label sits over the latest date",
      next(c for c in four if c["label"] == "Maturity Date")["value"]
      == max(c["value"] for c in four))
check("every span slices back to its printed form",
      spans_slice_to_raw(_FOUR, four))

# --------------------------------------------------------------------------- #
section("string, enum and array fields return an empty list")
# --------------------------------------------------------------------------- #

_MIXED = "Issuer: Bank 70.00% August 31, 2028 with 1,000 notes"
check("string field is empty", cand.candidates_for(field("issuer", "string"), _MIXED) == [])
check("enum field is empty", cand.candidates_for(field("product_type", "enum"), _MIXED) == [])
check("array field is empty", cand.candidates_for(field("underlyings", "array"), _MIXED) == [])

# --------------------------------------------------------------------------- #
section("no model, no network")
# --------------------------------------------------------------------------- #

_src = Path(cand.__file__).read_text(encoding="utf-8")
_forbidden = ("requests", "urllib", "http.client", "httpx", "socket",
              "ollama_client", "edgar_client")
check("module source imports no network or model library",
      not any(f"import {m}" in _src or f"from {m}" in _src for m in _forbidden))

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ncandidates gate: PASS")
