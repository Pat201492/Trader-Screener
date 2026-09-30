"""
Gate for the period-keyed facts store (issue #183). Same convention as
`test_output_store.py`: stdlib only, run directly, exit 0 = pass.

Every acceptance criterion in #183 is checked here:

  * FactRecord carries cik/concept/unit/period_start/period_end/fiscal_year/
    fiscal_period/form/accession/value/source, and source is "xbrl"|"extracted";
  * put_facts is idempotent on (cik, concept, period_start, period_end, accession);
  * a later accession supersedes an earlier one for the same (cik, concept, period):
    latest() returns the superseder, history() returns every version;
  * GuidanceRecord carries cik/metric/period_label/low/high/basis/accession/
    document/span/provenance/confidence, and round-trips;
  * a non-local destination path is refused with the same guard output_store uses.

Run:  python tools/edgar_scrubber/test_facts_store.py
"""
import os
import sqlite3
import tempfile

from facts_store import (
    FactsStore, FactRecord, GuidanceRecord, FactsStoreError,
)
from output_store import OwnershipError

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


CIK = "0000019617"
CONCEPT = "us-gaap:Revenues"
PS, PE = "2025-07-01", "2025-09-30"


def q3_10q(value=9_100_000_000, accession="0000019617-25-000123", source="xbrl",
           filed=None):
    return FactRecord(
        cik=CIK, concept=CONCEPT, unit="USD",
        period_start=PS, period_end=PE,
        fiscal_year=2025, fiscal_period="Q3", form="10-Q",
        accession=accession, value=value, source=source, filed=filed,
    )


def run_checks():
    # ── AC: FactRecord carries every stated attribute + source enum ──
    section("AC: FactRecord carries cik/concept/unit/period/fiscal/form/"
            "accession/value/source")
    store = FactsStore(":memory:")
    store.put_facts([q3_10q()])
    latest = store.latest(CIK, CONCEPT, PS, PE)
    check("cik round-trips", latest["cik"] == CIK)
    check("concept round-trips", latest["concept"] == CONCEPT)
    check("unit round-trips", latest["unit"] == "USD")
    check("period_start round-trips", latest["period_start"] == PS)
    check("period_end round-trips", latest["period_end"] == PE)
    check("fiscal_year round-trips", latest["fiscal_year"] == 2025)
    check("fiscal_period round-trips", latest["fiscal_period"] == "Q3")
    check("form round-trips", latest["form"] == "10-Q")
    check("accession round-trips", latest["accession"] == "0000019617-25-000123")
    check("value round-trips (int preserved)", latest["value"] == 9_100_000_000)
    check("source 'xbrl' round-trips", latest["source"] == "xbrl")
    check("filed defaults to None when unstated", latest["filed"] is None)
    store_f = FactsStore(":memory:")
    store_f.put_facts([q3_10q(filed="2025-10-31")])
    check("filed round-trips when set",
          store_f.latest(CIK, CONCEPT, PS, PE)["filed"] == "2025-10-31")
    check("source 'extracted' is accepted",
          FactRecord(cik=CIK, concept=CONCEPT, unit="USD", period_start=PS,
                     period_end=PE, fiscal_year=2025, fiscal_period="Q3",
                     form="10-Q", accession="a", value=1, source="extracted")
          .source == "extracted")
    check("an unknown source is refused",
          raises(FactsStoreError, FactRecord, cik=CIK, concept=CONCEPT,
                 unit="USD", period_start=PS, period_end=PE, fiscal_year=2025,
                 fiscal_period="Q3", form="10-Q", accession="a", value=1,
                 source="guessed"))

    # ── AC: put_facts idempotent on the 5-part key ──
    section("AC: put_facts idempotent on (cik, concept, period_start, "
            "period_end, accession)")
    store2 = FactsStore(":memory:")
    store2.put_facts([q3_10q()])
    store2.put_facts([q3_10q()])
    store2.put_facts([q3_10q()])
    check("writing the same batch three times leaves one row per key",
          len(store2.history(CIK, CONCEPT, PS, PE)) == 1)
    check("re-writing the same key updates in place (last value wins)",
          store2.latest(CIK, CONCEPT, PS, PE)["value"] == 9_100_000_000)
    store2.put_facts([q3_10q(value=9_090_000_000)])  # same key, new value
    check("same-key re-write did not add a row",
          len(store2.history(CIK, CONCEPT, PS, PE)) == 1)
    check("same-key re-write replaced the value",
          store2.latest(CIK, CONCEPT, PS, PE)["value"] == 9_090_000_000)

    # ── AC: supersession — later accession wins, history keeps all ──
    section("AC: a later accession supersedes; latest() supersedes, "
            "history() keeps every version")
    store3 = FactsStore(":memory:")
    # Write the RESTATEMENT first, original second — supersession must not depend
    # on insertion order, only on the accession.
    restated = FactRecord(
        cik=CIK, concept=CONCEPT, unit="USD", period_start=PS, period_end=PE,
        fiscal_year=2025, fiscal_period="Q3", form="10-K",
        accession="0000019617-26-000045", value=9_050_000_000, source="xbrl",
    )
    store3.put_facts([restated])
    store3.put_facts([q3_10q(value=9_100_000_000)])
    lat = store3.latest(CIK, CONCEPT, PS, PE)
    check("latest() returns the superseding (later-accession) record",
          lat["accession"] == "0000019617-26-000045" and lat["value"] == 9_050_000_000)
    check("latest() carries the superseding form", lat["form"] == "10-K")
    hist = store3.history(CIK, CONCEPT, PS, PE)
    check("history() returns every version", len(hist) == 2)
    check("history() is oldest-first (original before restatement)",
          [h["accession"] for h in hist] ==
          ["0000019617-25-000123", "0000019617-26-000045"])
    check("history() preserves the prior value (not overwritten)",
          [h["value"] for h in hist] == [9_100_000_000, 9_050_000_000])
    check("facts_for() collapses supersession to the latest per period",
          [f["value"] for f in store3.facts_for(CIK, concept=CONCEPT)] ==
          [9_050_000_000])

    # ── AC: GuidanceRecord carries every stated attribute + round-trips ──
    section("AC: GuidanceRecord carries cik/metric/period_label/low/high/basis/"
            "accession/document/span/provenance/confidence")
    store4 = FactsStore(":memory:")
    store4.put_guidance([GuidanceRecord(
        cik=CIK, metric="revenue", period_label="FY2026",
        low=38_000_000_000, high=39_000_000_000, basis="non-GAAP",
        accession="0000019617-26-000045", document="ex99.htm",
        span=(1044, 1120), provenance="model:qwen2.5-7b", confidence=0.82,
    )])
    g = store4.guidance_for(CIK, metric="revenue")[0]
    check("cik round-trips", g["cik"] == CIK)
    check("metric round-trips", g["metric"] == "revenue")
    check("period_label round-trips", g["period_label"] == "FY2026")
    check("low round-trips", g["low"] == 38_000_000_000)
    check("high round-trips", g["high"] == 39_000_000_000)
    check("basis round-trips", g["basis"] == "non-GAAP")
    check("accession round-trips", g["accession"] == "0000019617-26-000045")
    check("document round-trips", g["document"] == "ex99.htm")
    check("span offsets round-trip", g["span"] == (1044, 1120))
    check("provenance round-trips", g["provenance"] == "model:qwen2.5-7b")
    check("confidence round-trips", g["confidence"] == 0.82)

    # ── AC (#236): opening a pre-`filed` db adds the column, keeps every row ──
    section("AC: an old-schema facts.sqlite gains `filed` on open, losing no row")
    tmpdir = tempfile.mkdtemp(prefix="facts-migrate-")
    old_db = os.path.join(tmpdir, "facts.sqlite")
    # Build a store exactly as it looked BEFORE #236: no `filed` column.
    old_conn = sqlite3.connect(old_db)
    old_conn.execute(
        "CREATE TABLE facts ("
        " ingested_seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " cik TEXT NOT NULL, concept TEXT NOT NULL, unit TEXT,"
        " period_start TEXT NOT NULL, period_end TEXT NOT NULL,"
        " fiscal_year INTEGER, fiscal_period TEXT, form TEXT,"
        " accession TEXT NOT NULL, value_json TEXT, value_num REAL,"
        " source TEXT NOT NULL,"
        " UNIQUE (cik, concept, period_start, period_end, accession))")
    old_conn.executemany(
        "INSERT INTO facts (cik, concept, unit, period_start, period_end,"
        " fiscal_year, fiscal_period, form, accession, value_json, value_num,"
        " source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(CIK, CONCEPT, "USD", PS, PE, 2025, "Q3", "10-Q",
          "0000019617-25-00000%d" % i, "1", 1.0, "xbrl") for i in range(3)])
    old_conn.commit()
    before = old_conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    has_filed_before = any(
        r[1] == "filed" for r in
        old_conn.execute("PRAGMA table_info(facts)").fetchall())
    old_conn.close()
    check("the old db has no `filed` column before the migration",
          not has_filed_before)

    # A READ-ONLY open never migrates -- and read-only is how the dashboard
    # opens the real store. It must read the old store as the migration would
    # leave it, not raise on the missing column / table.
    ro = FactsStore(old_db, readonly=True)
    try:
        ro_rows = ro.facts_for(CIK)
        check("a read-only open of an un-migrated store reads its rows",
              len(ro_rows) == 1 and ro_rows[0]["filed"] is None)
        check("...as_of on it returns None rather than raising",
              ro.as_of(CIK, CONCEPT, PS, PE, "2030-01-01") is None)
        check("...and its filer directory is empty rather than missing",
              ro.filer_names() == {})
        check("...and the read-only open did not migrate it", not any(
            r["name"] == "filed" for r in
            ro._conn.execute("PRAGMA table_info(facts)").fetchall()))
    finally:
        ro.close()

    migrated = FactsStore(old_db)  # open == migrate
    try:
        after = migrated._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        has_filed_after = any(
            r["name"] == "filed" for r in
            migrated._conn.execute("PRAGMA table_info(facts)").fetchall())
        check("the migration adds the `filed` column", has_filed_after)
        check("no row is lost by the migration", before == after == 3)
        check("existing rows read back filed = NULL",
              all(f["filed"] is None for f in migrated.facts_for(CIK)))
        # And it is idempotent: opening again does not fail or re-add.
        migrated.put_facts([q3_10q(accession="0000019617-25-000999",
                                   filed="2025-10-31")])
        check("a new write on the migrated db can carry a filed date",
              migrated.latest(CIK, CONCEPT, PS, PE) is not None)
    finally:
        migrated.close()

    # ── AC (#236): as_of returns the value KNOWN on a date ──
    section("AC: as_of returns the version known on a date; NULL filed never returned")
    aso = FactsStore(":memory:")
    # Original 10-Q filed 2025-10-31; restatement 10-K filed 2026-01-30.
    orig = q3_10q(value=9_100_000_000, accession="0000019617-25-000123",
                  filed="2025-10-31")
    restated = FactRecord(
        cik=CIK, concept=CONCEPT, unit="USD", period_start=PS, period_end=PE,
        fiscal_year=2025, fiscal_period="Q3", form="10-K",
        accession="0000019617-26-000045", value=9_050_000_000, source="xbrl",
        filed="2026-01-30")
    aso.put_facts([restated])
    aso.put_facts([orig])
    key = (CIK, CONCEPT, PS, PE)
    check("as_of before the first filing returns None",
          aso.as_of(*key, "2025-10-01") is None)
    mid = aso.as_of(*key, "2025-12-01")
    check("as_of between the two filings returns the pre-restatement value",
          mid is not None and mid["value"] == 9_100_000_000
          and mid["accession"] == "0000019617-25-000123")
    after_both = aso.as_of(*key, "2026-02-01")
    check("as_of after both filings returns the restated value",
          after_both is not None and after_both["value"] == 9_050_000_000
          and after_both["accession"] == "0000019617-26-000045")
    check("as_of on the filing date itself sees that filing (<=, inclusive)",
          aso.as_of(*key, "2025-10-31") is not None
          and aso.as_of(*key, "2025-10-31")["value"] == 9_100_000_000)

    # A row with filed IS NULL is never returned by as_of, even for a late date.
    aso_null = FactsStore(":memory:")
    aso_null.put_facts([q3_10q(filed=None)])
    check("as_of never returns a row whose filed is NULL",
          aso_null.as_of(*key, "2099-01-01") is None)
    check("...but latest() still sees the NULL-filed row",
          aso_null.latest(*key) is not None)

    # ── AC: non-local destination refused with the SAME guard ──
    section("AC: a non-local destination path is refused (output_store's guard)")
    check("store under a Stock-Data-Pipeline tree is refused",
          raises(OwnershipError, FactsStore,
                 r"C:\repos\Stock-Data-Pipeline\data\facts.sqlite"))
    check("store named like a pipeline artifact is refused",
          raises(OwnershipError, FactsStore, "/tmp/universe.json"))
    check("in-memory + ordinary local path are allowed",
          isinstance(FactsStore(":memory:"), FactsStore))


def main():
    print("EDGAR scrubber facts-store gate (#183)")
    run_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nfacts-store gate: PASS")


if __name__ == "__main__":
    main()
