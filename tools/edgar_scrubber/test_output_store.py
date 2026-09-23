"""
Gate for the EDGAR scrubber output store (issue #109). Same convention as
`test_field_spec.py`: stdlib only, run directly, exit 0 = pass.

Every acceptance criterion in #109 is checked here:

  * record per (accession, document, field) carries value, unit, span offsets,
    provenance, confidence and flags, and round-trips;
  * queryable by underlying, issuer, product type and date — the first Research
    project's shape;
  * append-only with run stamps: a re-extraction under a NEW spec version does not
    destroy the prior read, and the two are comparable across spec versions;
  * the scrubber writes ONLY to its local store — pointing it at a pipeline
    location raises OwnershipError (no pipeline write path exists in the code);
  * a graduated field's local extractor is retired, not left running in parallel
    (write refuses it);
  * the store surfaces last-used so the manifest can show age since last use.

Run:  python tools/edgar_scrubber/test_output_store.py
"""
import output_store as os_mod
from output_store import (
    OutputStore, DocumentExtraction, FieldValue,
    OwnershipError, GraduationError,
)

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    except Exception:
        return False
    return False


NOTE_RECORD = {
    "issuer": "JPMorgan Chase Financial Company LLC",
    "product_type": "autocallable contingent coupon",
    "pricing_date": "2026-01-15",
    "maturity_date": "2029-01-18",
    "underlyings": [{"name": "S&P 500 Index", "kind": "index"},
                    {"name": "Russell 2000 Index", "kind": "index"}],
    "estimated_value_per_1000": 972.4,
    "aggregate_principal": 2_500_000,
}


def fresh_store():
    return OutputStore(":memory:")


def seed(store, run_stamp="2026-08-05T10:00:00Z", spec_version="1.0.0",
         accession="0001-24-000001", ev=972.4):
    rid = store.start_run("424b2.structured_note", spec_version, run_stamp)
    rec = {**NOTE_RECORD, "estimated_value_per_1000": ev}
    doc = DocumentExtraction.from_record(
        accession, "424b2.htm", rec,
        units={"estimated_value_per_1000": "usd_per_1000",
               "aggregate_principal": "usd"},
        spans={"estimated_value_per_1000": (10432, 10440)},
        provenance={"estimated_value_per_1000": "rule:anchor 'our estimated value'",
                    "product_type": "model:qwen2.5-7b"},
        confidence={"estimated_value_per_1000": 0.98, "product_type": 0.71},
        flags={"aggregate_principal": [{"field": "aggregate_principal",
                                        "code": "out_of_bounds", "severity": "warn",
                                        "message": "demo flag"}]},
    )
    store.write_document(rid, doc)
    return rid


def run_checks():
    # ── AC: record grain + all carried attributes round-trip ──
    section("AC: record per (accession, document, field) carries value/unit/span/"
            "provenance/confidence/flags")
    store = fresh_store()
    seed(store)
    fields = {f["field"]: f for f in store.fields("0001-24-000001", "424b2.htm")}
    ev = fields.get("estimated_value_per_1000", {})
    check("value round-trips", ev.get("value") == 972.4)
    check("unit round-trips", ev.get("unit") == "usd_per_1000")
    check("span offsets round-trip", ev.get("span") == (10432, 10440))
    check("provenance round-trips",
          ev.get("provenance") == "rule:anchor 'our estimated value'")
    check("confidence round-trips", ev.get("confidence") == 0.98)
    agg = fields.get("aggregate_principal", {})
    check("flags round-trip (list of dicts)",
          isinstance(agg.get("flags"), list) and agg["flags"]
          and agg["flags"][0]["code"] == "out_of_bounds")
    check("array value (underlyings) round-trips as a list",
          isinstance(fields.get("underlyings", {}).get("value"), list))

    # ── AC: queryable by underlying / issuer / product_type / date ──
    section("AC: queryable by underlying, issuer, product type, date")
    check("query by issuer",
          len(store.query(issuer="JPMorgan Chase Financial Company LLC")) == 1)
    check("query by product_type",
          len(store.query(product_type="autocallable contingent coupon")) == 1)
    check("query by underlying (index membership)",
          len(store.query(underlying="S&P 500 Index")) == 1)
    check("query by second underlying",
          len(store.query(underlying="Russell 2000 Index")) == 1)
    check("query by unknown underlying -> empty",
          store.query(underlying="Nikkei 225") == [])
    check("query by date window (inclusive)",
          len(store.query(date_from="2026-01-01", date_to="2026-01-31")) == 1)
    check("query by date window that excludes it -> empty",
          store.query(date_from="2027-01-01") == [])
    hit = store.query(issuer="JPMorgan Chase Financial Company LLC")[0]
    check("query row carries the document's underlyings",
          {u["name"] for u in hit["underlyings"]} ==
          {"S&P 500 Index", "Russell 2000 Index"})

    # ── AC: append-only with run stamps; comparable across spec versions ──
    section("AC: append-only — a re-extraction under a new spec version keeps the "
            "prior read")
    store2 = fresh_store()
    seed(store2, run_stamp="2026-08-05T10:00:00Z", spec_version="1.0.0", ev=972.4)
    seed(store2, run_stamp="2026-09-01T10:00:00Z", spec_version="1.1.0", ev=940.0)
    check("both runs are retained", len(store2.runs()) == 2)
    hist = store2.field_history("0001-24-000001", "424b2.htm",
                                "estimated_value_per_1000")
    check("field history spans both spec versions",
          [h["spec_version"] for h in hist] == ["1.0.0", "1.1.0"])
    check("history preserves the prior value (not overwritten)",
          [h["value"] for h in hist] == [972.4, 940.0])
    latest = {f["field"]: f for f in store2.fields("0001-24-000001", "424b2.htm")}
    check("latest read returns the newest run's value",
          latest["estimated_value_per_1000"]["value"] == 940.0)
    check("query(latest=True) collapses to one row per document",
          len(store2.query(issuer="JPMorgan Chase Financial Company LLC")) == 1)
    check("query(latest=False) exposes both runs",
          len(store2.query(issuer="JPMorgan Chase Financial Company LLC",
                           latest=False)) == 2)
    check("append-only: no update/delete method on the public surface",
          not any(hasattr(store2, m) for m in ("update", "delete", "overwrite")))

    # ── AC: local-only — no pipeline write path ──
    section("AC: scrubber writes ONLY to its local store (ownership boundary)")
    check("store under a Stock-Data-Pipeline tree is refused",
          raises(OwnershipError, OutputStore,
                 r"C:\repos\Stock-Data-Pipeline\data\edgar.sqlite"))
    check("store named like a pipeline artifact is refused",
          raises(OwnershipError, OutputStore, "/tmp/universe.json"))
    check("in-memory + ordinary local path are allowed",
          isinstance(fresh_store(), OutputStore))
    src = open(os_mod.__file__, encoding="utf-8").read()
    import_lines = [ln.strip() for ln in src.splitlines()
                    if ln.strip().startswith(("import ", "from "))]
    check("no import of any pipeline / upstream module",
          not any(("pipeline" in ln or "stock_data" in ln) for ln in import_lines))
    check("store exposes no upstream/pipeline write method",
          not any(hasattr(store, m) for m in
                  ("write_pipeline", "push_upstream", "to_pipeline", "publish")))

    # ── AC: graduation retires the local extractor (no parallel second answer) ──
    section("AC: a graduated field is retired locally, not run in parallel")
    store3 = fresh_store()
    seed(store3)
    store3.graduate_field("estimated_value_per_1000",
                          "Stock-Data-Pipeline: ingest_edgar.py::estimated_value",
                          "2026-10-01T00:00:00Z")
    check("graduated field is recorded with its upstream location",
          store3.graduated_fields().get("estimated_value_per_1000", "")
          .startswith("Stock-Data-Pipeline"))
    rid = store3.start_run("424b2.structured_note", "1.2.0",
                           "2026-10-02T10:00:00Z")
    graduated_doc = DocumentExtraction.from_record(
        "0002-24-000002", "424b2.htm", NOTE_RECORD)
    check("re-extracting a graduated field locally raises GraduationError",
          raises(GraduationError, store3.write_document, rid, graduated_doc))
    check("single-field record path also refuses a graduated field",
          raises(GraduationError, store3.record, rid, "0002", "d.htm",
                 FieldValue("estimated_value_per_1000", 900.0)))
    ok_doc = DocumentExtraction(
        "0002-24-000002", "424b2.htm", issuer="X",
        fields=[FieldValue("cusip", "48133YHT4")])
    store3.write_document(rid, ok_doc)
    check("a non-graduated field still writes after another graduated",
          len(store3.fields("0002-24-000002", "424b2.htm")) == 1)

    # ── AC: manifest can show status + age since last use ──
    section("AC: manifest surfacing — local-vs-graduated status + last-used age")
    stats = store.manifest_stats()
    check("local store reports status 'local'", stats["status"] == "local")
    check("last_used is the newest run stamp",
          stats["last_used"] == "2026-08-05T10:00:00Z")
    check("graduated store reports status 'graduated'",
          store3.manifest_stats()["status"] == "graduated")
    check("manifest stats count documents and runs",
          stats["document_count"] == 1 and stats["run_count"] == 1)

    # ── AC (#179): candidate_count survives; the candidate list never does ──
    section("AC (#179): the chosen value's candidate_count is stored; no list is")
    cc = fresh_store()
    ccrun = cc.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")
    cc.write_document(ccrun, DocumentExtraction(
        "0003-24-000003", "424b2.htm", issuer="X",
        fields=[
            # A value chosen from five deterministic candidates (#178).
            FieldValue("estimated_value_per_1000", 972.0, candidate_count=5),
            # A free-form field: no enumeration ran, so the count is 0.
            FieldValue("issuer", "X", candidate_count=0),
        ]))
    got = {f["field"]: f for f in cc.fields("0003-24-000003", "424b2.htm")}
    check("candidate_count round-trips for the chosen value",
          got["estimated_value_per_1000"]["candidate_count"] == 5)
    check("candidate_count is 0 exactly on the free-form field",
          got["issuer"]["candidate_count"] == 0)
    check("FieldValue defaults candidate_count to 0 (the free-form value)",
          FieldValue("x", 1).candidate_count == 0)
    check("from_record threads candidate_counts and defaults the rest to 0",
          [f.candidate_count for f in DocumentExtraction.from_record(
              "a", "d", {"barrier_pct": 70.0, "issuer": "X"},
              candidate_counts={"barrier_pct": 3}).fields] == [3, 0])
    cols = {r[1] for r in cc._conn.execute("PRAGMA table_info(extractions)")}
    check("no column exists that could hold a candidate LIST",
          not any("candidate" in c and c != "candidate_count" for c in cols))
    # field_history carries the count too, so it survives across spec versions.
    hist = cc.field_history("0003-24-000003", "424b2.htm", "estimated_value_per_1000")
    check("field_history carries candidate_count", hist[0]["candidate_count"] == 5)


def main():
    print("EDGAR scrubber output-store gate (#109)")
    run_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\noutput-store gate: PASS")


if __name__ == "__main__":
    main()
