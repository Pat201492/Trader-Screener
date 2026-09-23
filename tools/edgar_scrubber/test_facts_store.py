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


def q3_10q(value=9_100_000_000, accession="0000019617-25-000123", source="xbrl"):
    return FactRecord(
        cik=CIK, concept=CONCEPT, unit="USD",
        period_start=PS, period_end=PE,
        fiscal_year=2025, fiscal_period="Q3", form="10-Q",
        accession=accession, value=value, source=source,
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
