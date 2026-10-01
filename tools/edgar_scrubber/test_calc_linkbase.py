"""
Gate for the calculation linkbase (issue #261). Same convention as
`test_facts_store.py`: stdlib only, run directly, exit 0 = pass. A
`test_calc_linkbase` wrapper lets `pytest tools/edgar_scrubber` run it too.

Every acceptance criterion in #261 is checked here:

  * parse_cal on the fixture returns the two added revenue arcs (weight 1) with
    the role URI from the file;
  * a weight -1 arc parses as -1.0 and check_rollups subtracts it;
  * check_rollups is ok for 674538000000 + 6447000000 vs 680985000000, not ok
    past 0.5%, and skips a parent missing any child;
  * find_cal_url returns the _cal.xml URL from the fixture index.json and None
    for an index without one (fake client, no network);
  * put_arcs twice leaves one row per arc; a read-only open of a store without
    calc_arcs returns [] from arcs_for.

Run:  python tools/edgar_scrubber/test_calc_linkbase.py
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from calc_linkbase import find_cal_url, parse_cal, check_rollups
from facts_store import FactsStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cal"

REVENUES = "us-gaap:Revenues"
RFCC = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
OTHER = "us-gaap:OtherIncome"
GROSS = "us-gaap:GrossProfit"
COGS = "us-gaap:CostOfGoodsAndServicesSold"
ROLE = "http://www.walmart.com/role/ConsolidatedStatementsOfIncome"

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


class FakeClient:
    """A stand-in for EdgarClient: serves one canned index.json, no network."""

    def __init__(self, index):
        self._index = index
        self.calls = 0

    def filing_index(self, cik, accession):
        self.calls += 1
        return self._index


def run_checks():
    cal_text = (FIXTURES / "wmt-20250131_cal.xml").read_text(encoding="utf-8")
    arcs = parse_cal(cal_text)
    by_child = {a["child"]: a for a in arcs}

    # ── AC: parse_cal returns the two added revenue arcs with the file's role ──
    section("AC: parse_cal returns the Walmart revenue roll-up arcs (weight 1)")
    check("Revenues <- RevenueFromContract... parsed",
          RFCC in by_child and by_child[RFCC]["parent"] == REVENUES)
    check("...with weight 1.0", by_child[RFCC]["weight"] == 1.0)
    check("Revenues <- OtherIncome parsed",
          OTHER in by_child and by_child[OTHER]["parent"] == REVENUES)
    check("...with weight 1.0", by_child[OTHER]["weight"] == 1.0)
    check("the role URI is read from the file",
          by_child[RFCC]["role"] == ROLE and by_child[OTHER]["role"] == ROLE)
    check("concepts are normalised to <prefix>:Name",
          by_child[RFCC]["child"] == RFCC)

    # ── AC: a weight -1 arc parses as -1.0 and is subtracted ──
    section("AC: a negative-weight arc parses as -1.0 and check_rollups subtracts it")
    check("GrossProfit <- CostOfGoodsAndServicesSold is weight -1.0",
          COGS in by_child and by_child[COGS]["weight"] == -1.0
          and by_child[COGS]["parent"] == GROSS)
    gross_vals = {
        GROSS: 157983000000,
        REVENUES: 680985000000,
        COGS: 523002000000,   # 680985 - 523002 == 157983
    }
    gross_res = {r["parent"]: r for r in check_rollups(gross_vals, arcs)}
    check("the cost is subtracted, not added (GrossProfit rolls up ok)",
          GROSS in gross_res and gross_res[GROSS]["ok"]
          and gross_res[GROSS]["expected"] == 157983000000)

    # ── AC: check_rollups ok / not-ok / skip-on-missing-child ──
    section("AC: check_rollups ok within tolerance, not ok past 0.5%, skip missing")
    values = {REVENUES: 680985000000, RFCC: 674538000000, OTHER: 6447000000}
    res = {r["parent"]: r for r in check_rollups(values, arcs)}
    check("674538000000 + 6447000000 vs 680985000000 is ok",
          REVENUES in res and res[REVENUES]["ok"])
    check("expected is the exact sum of children",
          res[REVENUES]["expected"] == 680985000000)
    check("reported is the parent value", res[REVENUES]["reported"] == 680985000000)
    check("children are the two addends",
          set(res[REVENUES]["children"]) == {RFCC, OTHER})

    off = {REVENUES: 690000000000, RFCC: 674538000000, OTHER: 6447000000}
    res_off = {r["parent"]: r for r in check_rollups(off, arcs)}
    check("a parent off by more than 0.5% is not ok",
          REVENUES in res_off and not res_off[REVENUES]["ok"])

    within = {REVENUES: 683000000000, RFCC: 674538000000, OTHER: 6447000000}
    # 683000000000 vs 680985000000: diff 2.015e9, tol 0.005*683e9 = 3.415e9 -> ok
    res_in = {r["parent"]: r for r in check_rollups(within, arcs)}
    check("a parent within 0.5% is ok",
          REVENUES in res_in and res_in[REVENUES]["ok"])

    missing = {REVENUES: 680985000000, RFCC: 674538000000}  # OtherIncome absent
    res_miss = {r["parent"]: r for r in check_rollups(missing, arcs)}
    check("a parent missing any child is skipped, not reported",
          REVENUES not in res_miss)

    # ── AC: find_cal_url from the fixture index.json, None without one ──
    section("AC: find_cal_url returns the _cal.xml URL, None for an index without one")
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    client = FakeClient(index)
    url = find_cal_url(client, "104169", "0000104169-25-000012")
    check("find_cal_url returns the fixture's _cal.xml Archives URL",
          url == ("https://www.sec.gov/Archives/edgar/data/104169/"
                  "000010416925000012/wmt-20250131_cal.xml"))
    check("...via the (cache-first) client, no network", client.calls == 1)
    no_cal = {"directory": {"name": "x", "item": [
        {"name": "wmt-20250131.htm", "type": "10-K", "size": "1"},
        {"name": "wmt-20250131_lab.xml", "type": "EX-101.LAB", "size": "1"},
    ]}}
    check("find_cal_url returns None for an index with no _cal.xml",
          find_cal_url(FakeClient(no_cal), "104169", "0000104169-25-000012") is None)

    # ── AC: put_arcs idempotent; arcs_for round-trips ──
    section("AC: put_arcs twice leaves one row per arc; arcs_for round-trips")
    store = FactsStore(":memory:")
    store.put_arcs("104169", "0000104169-25-000012", arcs)
    store.put_arcs("104169", "0000104169-25-000012", arcs)  # idempotent
    stored = store.arcs_for("104169", "0000104169-25-000012")
    check("writing the same arcs twice leaves one row per arc",
          len(stored) == len(arcs))
    stored_by_child = {a["child"]: a for a in stored}
    check("a stored arc round-trips parent/child/weight/role",
          stored_by_child[OTHER]["parent"] == REVENUES
          and stored_by_child[OTHER]["weight"] == 1.0
          and stored_by_child[OTHER]["role"] == ROLE)
    check("the negative weight round-trips as -1.0",
          stored_by_child[COGS]["weight"] == -1.0)
    check("arcs_for an unknown accession is empty",
          store.arcs_for("104169", "9999999999-99-999999") == [])

    # ── AC: a read-only open of a store without calc_arcs returns [] ──
    section("AC: a read-only open of a store without calc_arcs returns [] from arcs_for")
    tmpdir = tempfile.mkdtemp(prefix="calc-arcs-")
    old_db = os.path.join(tmpdir, "facts.sqlite")
    # A store written before #261: facts table only, no calc_arcs.
    conn = sqlite3.connect(old_db)
    conn.execute(
        "CREATE TABLE facts ("
        " ingested_seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " cik TEXT NOT NULL, concept TEXT NOT NULL, unit TEXT,"
        " period_start TEXT NOT NULL, period_end TEXT NOT NULL,"
        " fiscal_year INTEGER, fiscal_period TEXT, form TEXT,"
        " accession TEXT NOT NULL, value_json TEXT, value_num REAL,"
        " source TEXT NOT NULL,"
        " UNIQUE (cik, concept, period_start, period_end, accession))")
    conn.commit()
    conn.close()
    ro = FactsStore(old_db, readonly=True)
    try:
        check("a read-only open with no calc_arcs table returns [] (no raise)",
              ro.arcs_for("104169", "0000104169-25-000012") == [])
        check("...and the read-only open did not create the table", not any(
            r["name"] == "calc_arcs" for r in
            ro._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")))
    finally:
        ro.close()


def test_calc_linkbase():
    """pytest entrypoint: run every check and fail on the first problem."""
    run_checks()
    assert not failures, f"{len(failures)} check(s) failed: {failures}"


def main():
    print("EDGAR scrubber calc-linkbase gate (#261)")
    run_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\ncalc-linkbase gate: PASS")


if __name__ == "__main__":
    main()
