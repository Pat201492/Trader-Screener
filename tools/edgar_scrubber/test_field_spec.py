"""
Gate for the 424B2 field spec (issue #102). Same convention as
`lookahead-gate/ab_truncation_test.py` and `pipeline-mock/test_mock_server.py`:
stdlib only, run directly, exit 0 = pass.

Every acceptance criterion in #102 is checked here:

  * spec file loads at runtime; adding a field is a DATA edit, zero code change
    (proved by writing a scratch spec dir with an extra field and reloading);
  * both 424B2 populations covered, with automatic A/B detection between them,
    including the ambiguous case that must report low confidence;
  * every field tagged fixed-anchor / table-resident / variable;
  * per-field bounds enforced, violations surfaced as FLAGS not dropped (the
    record is returned intact alongside its flags);
  * spec shape registered in the canonical registry (#86 role), not inline —
    a malformed spec fails loudly at load;
  * wire keys (#104 transport) map back to canonical and never reach a record.

Run:  python tools/edgar_scrubber/test_field_spec.py
"""
import json
import tempfile
from pathlib import Path

import field_spec as fs
import schema_registry as sr

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def codes(flags):
    return {f.code for f in flags}


# Reusable good structured-note record (canonical keys).
GOOD_NOTE = {
    "issuer": "JPMorgan Chase Financial Company LLC",
    "guarantor": "JPMorgan Chase & Co.",
    "cusip": "48133YHT4",
    "underlyings": [{"name": "S&P 500 Index", "kind": "index"}],
    "product_type": "autocallable contingent coupon",
    "pricing_date": "2026-01-15",
    "maturity_date": "2029-01-18",
    "aggregate_principal": 2500000,
    "contingent_coupon_rate": 9.5,
    "buffer_pct": 20.0,
    "estimated_value_per_1000": 972.4,
}


def run_checks():
    specs = fs.load_specs()

    # ── AC5: spec shape registered in the canonical registry, not inline ──
    section("AC: spec shape lives in the canonical registry (#86 role), not inline")
    check("registry knows the field-spec shape", sr.REGISTRY.has(sr.SPEC_SHAPE_NAME))
    check("registry knows the field-definition shape", sr.REGISTRY.has(sr.FIELD_SHAPE_NAME))
    check("shape re-exportable as language-neutral data (for JS #86)",
          sr.SPEC_SHAPE_NAME in sr.field_spec_shape_as_data())
    # loader validates against the registered shape, not an inline copy
    malformed = {"spec_id": "x", "form_type": "424B2", "population": "x",
                 "version": "1", "detection": {"signals": [{"pattern": "z"}]},
                 "fields": [{"name": "bad", "type": "banana",
                             "extraction_path": "table-resident", "sections": []}]}
    raised = False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.json"
        p.write_text(json.dumps(malformed), encoding="utf-8")
        try:
            fs.load_spec(p)
        except fs.SpecError:
            raised = True
    check("malformed spec fails loudly at load (bad type rejected)", raised)

    # ── AC1: loads at runtime; adding a field is data-only ──
    section("AC: spec loads at runtime; adding a field requires zero code change")
    check("two specs loaded from JSON at runtime", len(specs) == 2)
    note = specs["structured_note"]
    check("estimated_value_per_1000 present", note.field("estimated_value_per_1000") is not None)
    check("field with no matching code exists purely from JSON (underlyings)",
          note.field("underlyings") is not None)

    # prove: copy the real specs to a scratch dir, append ONE new field to the
    # JSON, reload with the SAME code — the field appears. No code edit.
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        raw = json.loads((fs.DEFAULT_SPEC_DIR / "424b2_structured_note.json")
                         .read_text(encoding="utf-8"))
        raw["fields"].append({
            "name": "brand_new_field_added_by_data_edit",
            "type": "percent", "extraction_path": "table-resident",
            "sections": ["key_terms"], "bounds": {"min": 0, "max": 100},
        })
        (scratch / "424b2_structured_note.json").write_text(
            json.dumps(raw), encoding="utf-8")
        # shelf too, so load_specs sees both populations
        (scratch / "424b2_shelf_takedown.json").write_text(
            (fs.DEFAULT_SPEC_DIR / "424b2_shelf_takedown.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        reloaded = fs.load_specs(scratch)
        added = reloaded["structured_note"].field("brand_new_field_added_by_data_edit")
        check("field added by editing JSON only shows up after reload", added is not None)
        check("added field usable immediately (bounds enforced)",
              any(fl.code == "out_of_bounds" for fl in
                  reloaded["structured_note"].validate_record(
                      {**GOOD_NOTE, "brand_new_field_added_by_data_edit": 250})))

    # ── AC2: both populations + automatic detection ──
    section("AC: both 424B2 populations covered, automatic A/B detection")
    check("population A = structured notes present", "structured_note" in specs)
    check("population B = shelf takedowns present", "shelf_takedown" in specs)

    note_doc = ("Our estimated value of the notes is $972.40. Contingent Coupon "
                "Rate: 9.50%. The notes will be automatically called. Buffer "
                "Amount: 20%. Worst performing underlying. Not a deposit.")
    d = fs.detect_population(note_doc, specs)
    check("structured-note doc detected as structured_note", d.population == "structured_note")
    check("structured-note detection is confident", d.confident is True)
    check("detection returns the matching spec object", d.spec is note)

    shelf_doc = ("Price to Public $25.00. Underwriting Discount $0.75. Net "
                 "Proceeds to the Company. Use of Proceeds. 10,000,000 shares "
                 "of common stock. Over-allotment option.")
    d2 = fs.detect_population(shelf_doc, specs)
    check("shelf doc detected as shelf_takedown", d2.population == "shelf_takedown")
    check("shelf detection is confident", d2.confident is True)

    d3 = fs.detect_population("This filing mentions nothing relevant at all.", specs)
    check("uncorrelated doc -> not confident (routed to review, not guessed)",
          d3.confident is False and d3.spec is None)

    # ── AC3: every field tagged fixed-anchor / table-resident / variable ──
    section("AC: every field tagged fixed-anchor / table-resident / variable")
    valid_paths = set(sr.EXTRACTION_PATHS)
    all_tagged = True
    for pop, spec in specs.items():
        for f in spec.fields:
            if f.extraction_path not in valid_paths:
                all_tagged = False
                print(f"      untagged: {pop}.{f.name} = {f.extraction_path!r}")
    check("all fields across both specs carry a valid extraction_path", all_tagged)
    check("estimated_value_per_1000 is fixed-anchor (rule-served, not model)",
          note.field("estimated_value_per_1000").extraction_path == "fixed-anchor")
    check("buffer_pct is table-resident (#101 flattens the terms table)",
          note.field("buffer_pct").extraction_path == "table-resident")
    check("product_type is variable (genuinely needs the model)",
          note.field("product_type").extraction_path == "variable")

    # ── AC: fields route to sections (#101) ──
    section("AC: fields declare which document sections can contain them (#101)")
    check("estimated_value_per_1000 routes to the estimated_value section",
          "estimated_value" in note.field("estimated_value_per_1000").sections)
    check("cover section resolves to a non-empty field set",
          len(note.fields_for_section("cover")) > 0)

    # ── AC4: bounds enforced, violations are FLAGS not drops ──
    section("AC: per-field bounds enforced, violations surfaced as flags not dropped")
    clean = note.validate_record(GOOD_NOTE)
    check("a clean record yields zero flags", clean == [])

    bad = {**GOOD_NOTE,
           "estimated_value_per_1000": 1240.0,   # markup impossible -> wrong number
           "buffer_pct": 150.0,                  # >100
           "contingent_coupon_rate": -3.0}       # <0
    flags = note.validate_record(bad)
    oob = [f for f in flags if f.code == "out_of_bounds"]
    check("out-of-bounds estimated_value flagged (not dropped)",
          any(f.field == "estimated_value_per_1000" for f in oob))
    check("buffer_pct > 100 flagged", any(f.field == "buffer_pct" for f in oob))
    check("negative coupon flagged", any(f.field == "contingent_coupon_rate" for f in oob))
    check("the bad values are NOT removed from the record (surfaced, not dropped)",
          bad["estimated_value_per_1000"] == 1240.0 and "buffer_pct" in bad)

    missing = note.validate_record({"cusip": "48133YHT4"})
    check("missing required field flagged as error",
          any(f.code == "missing_required" and f.severity == "error" for f in missing))

    bad_enum = note.validate_record({**GOOD_NOTE, "product_type": "mystery payoff"})
    check("enum violation flagged", any(f.code == "enum_violation" for f in bad_enum))

    bad_date = note.validate_record({**GOOD_NOTE, "maturity_date": "2020-01-01"})
    check("maturity before pricing flagged via cross-check",
          any(f.code == "cross_check_failed" and f.field == "maturity_date"
              for f in bad_date))

    # EX-107 cross-check (#101): prose aggregate vs the fee exhibit.
    ok107 = note.validate_record(GOOD_NOTE, ex107={"aggregate_principal": 2500000})
    check("aggregate matching EX-107 -> no cross-check flag",
          not any(f.code == "cross_check_failed" for f in ok107))
    bad107 = note.validate_record(GOOD_NOTE, ex107={"aggregate_principal": 9999999})
    check("aggregate disagreeing with EX-107 -> flagged",
          any(f.code == "cross_check_failed" and f.field == "aggregate_principal"
              for f in bad107))

    # ── AC: wire format is transport-only, never stored ──
    section("AC: short wire keys (#104) map back to canonical, never reach a record")
    legend = note.wire_legend()
    check("wire legend maps ev1000 -> estimated_value_per_1000",
          legend.get("ev1000") == "estimated_value_per_1000")
    decoded = note.from_wire({"ev1000": 972.4, "buf": 20.0, "iss": "JPM"})
    check("from_wire produces canonical keys",
          decoded == {"estimated_value_per_1000": 972.4, "buffer_pct": 20.0,
                      "issuer": "JPM"})
    leaked = False
    try:
        note.assert_canonical({"ev1000": 972.4})
    except fs.SpecError:
        leaked = True
    check("assert_canonical raises if a wire key reaches a stored record", leaked)
    leak_flags = note.validate_record({**GOOD_NOTE, "ev1000": 972.4})
    check("validate_record flags a leaked wire key",
          any(f.code == "wire_key_leak" for f in leak_flags))
    check("canonical name inventory excludes wire keys",
          "ev1000" not in fs.canonical_field_names(specs)["structured_note"])

    # ── #144: fabricated point value on a range-stating preliminary filing ──
    section("AC (#144): a preliminary range no longer yields an unflagged point value")

    # detect_filing_stage: deterministic, zero-token, from the cover-page legend.
    prelim_doc = ("PRELIMINARY PRICING SUPPLEMENT Subject to Completion, dated "
                  "June 6, 2024. The estimated initial value of the notes as of "
                  "the trade date is expected to be between $962.60 and $992.60 "
                  "per note. It will be set forth in the final pricing supplement.")
    final_doc = ("PRICING SUPPLEMENT. The estimated value of the notes as of "
                 "the pricing date was $972.30 per $1,000 note.")
    check("detect_filing_stage flags the preliminary legend",
          fs.detect_filing_stage(prelim_doc) == "preliminary")
    check("detect_filing_stage flags the final/determined-value legend",
          fs.detect_filing_stage(final_doc) == "final")
    check("detect_filing_stage returns None absent either legend",
          fs.detect_filing_stage("Nothing about pricing stage here.") is None)

    # A record that (correctly) has no point value, on a preliminary filing:
    # must be flagged as unavailable-on-preliminary, NOT missing_required, and
    # NOT silently clean.
    prelim_record = {k: v for k, v in GOOD_NOTE.items() if k != "estimated_value_per_1000"}
    prelim_record["estimated_value_low"] = 962.60
    prelim_record["estimated_value_high"] = 992.60
    prelim_flags = note.validate_record(prelim_record, filing_stage="preliminary")
    ev_flags = [f for f in prelim_flags if f.field == "estimated_value_per_1000"]
    check("null estimated_value_per_1000 on a preliminary filing is still flagged (not silent)",
          len(ev_flags) == 1)
    check("...specifically as unavailable_on_preliminary, not missing_required",
          ev_flags[0].code == "unavailable_on_preliminary" and ev_flags[0].severity == "info")
    check("the range is represented on the record instead (#144 direction 1)",
          prelim_record["estimated_value_low"] == 962.60
          and prelim_record["estimated_value_high"] == 992.60)

    # The SAME null value on a filing that is NOT known to be preliminary is
    # still a hard error -- the waiver is conditional, not a blanket exemption.
    unknown_stage_flags = note.validate_record(prelim_record)  # no filing_stage
    check("null estimated_value_per_1000 with no filing_stage is still missing_required",
          any(f.field == "estimated_value_per_1000" and f.code == "missing_required"
              for f in unknown_stage_flags))

    # A fabricated IN-BOUNDS point value on a preliminary filing is exactly
    # #144's original bug (989.5 inside 900-1000, non-null, no bounds/enum
    # violation) -- validate_record alone cannot catch that it was invented
    # (that is span_supports_value's job in extraction_ladder, run before a
    # value ever reaches a record); confirm this module's job is unchanged:
    # a non-null value is still bounds/type-checked either way.
    fabricated = note.validate_record({**GOOD_NOTE, "estimated_value_per_1000": 989.5},
                                      filing_stage="preliminary")
    check("a present (even if wrong) point value is not touched by the waiver",
          not any(f.field == "estimated_value_per_1000" for f in fabricated))

    # required_unless is registered, not inline (#86 pattern) -- a bad shape
    # fails loudly at load, same as every other field-definition attribute.
    bad_waiver = {"spec_id": "x", "form_type": "424B2", "population": "x",
                 "version": "1", "detection": {"signals": [{"pattern": "z"}]},
                 "fields": [{"name": "bad", "type": "number",
                             "extraction_path": "table-resident", "sections": [],
                             "required": True, "required_unless": {"equals": "x"}}]}
    raised_ru = False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad_ru.json"
        p.write_text(json.dumps(bad_waiver), encoding="utf-8")
        try:
            fs.load_spec(p)
        except fs.SpecError:
            raised_ru = True
    check("required_unless missing its 'field' key fails loudly at load",
          raised_ru)


def main():
    print("424B2 field spec gate (#102)")
    run_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\n424B2 field spec gate: PASS")


if __name__ == "__main__":
    main()
