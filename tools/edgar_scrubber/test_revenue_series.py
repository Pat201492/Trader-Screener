"""
Gate for the revenue-concept resolver + annual/quarterly series (issue #184).
Same convention as `test_facts_store.py`: stdlib only, run directly, exit 0 =
pass. Offline throughout -- facts come from checked-in `companyconcept` fixture
JSON covering both accounting eras, loaded into an in-memory facts store; no
network, no model.

Every acceptance criterion in #184 is checked here:

  * REVENUE_TAGS lists the three tags, each commented with its measured FY range;
  * the series is the UNION across tags keyed by period -- a two-era company
    yields one continuous series spanning both;
  * a same-period collision across two tags resolves deterministically;
  * a period is classified annual/quarterly by DURATION, never `fp` -- an
    `fp == "FY"` stub of <=100 days is a quarter and never reaches the annual series;
  * fiscal year is derived from `period_end`, never `fy` -- a period restated
    across three filings collapses to one entry;
  * annual_series is one fact per fiscal year sorted by period_end; quarterly_series
    carries a derived, flagged Q4 = annual - (Q1+Q2+Q3);
  * a period restated under a later accession resolves to the later value;
  * a company carrying none of the tags returns an empty series with a reason,
    and never raises.

Run:  python tools/edgar_scrubber/test_revenue_series.py
"""
import json
from pathlib import Path

import revenue_series as rs
from facts_store import FactsStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
APPLE = "0000320193"

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


def load_store():
    """An in-memory facts store loaded from the three era fixtures -- no network."""
    store = FactsStore(":memory:")
    for name in ("revenue_salesrevenuenet_320193.json",
                 "revenue_revenues_320193.json",
                 "revenue_rfcc_320193.json"):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        store.put_facts(rs.facts_from_concept(APPLE, data))
    return store


store = load_store()
series = rs.RevenueSeries(store)
annual = series.annual_series(APPLE)
quarterly = series.quarterly_series(APPLE)

# --------------------------------------------------------------------------- #
section("REVENUE_TAGS names the three tags, newest-standard first")
# --------------------------------------------------------------------------- #
check("all three tags present",
      set(rs.REVENUE_TAGS) == {
          "RevenueFromContractWithCustomerExcludingAssessedTax",
          "Revenues", "SalesRevenueNet"})
check("ordered newest accounting standard first",
      rs.REVENUE_TAGS[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
      and rs.REVENUE_TAGS[-1] == "SalesRevenueNet")
check("each tag documented with a fiscal-year range",
      all(str(y) in rs.__doc__ for y in (2009, 2017, 2018, 2019, 2025))
      and "FY2009-2017" in Path(rs.__file__).read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- #
section("union across eras is one continuous series")
# --------------------------------------------------------------------------- #
years = [r["fiscal_year"] for r in annual]
check("fiscal years span both eras, contiguous 2016..2020",
      years == [2016, 2017, 2018, 2019, 2020])
check("sorted by period_end",
      [r["period_end"] for r in annual] == sorted(r["period_end"] for r in annual))
concepts = {rs._tag_of(r["concept"]) for r in annual}
check("series draws from both eras' tags (not the first tag that had facts)",
      {"SalesRevenueNet",
       "RevenueFromContractWithCustomerExcludingAssessedTax"} <= concepts)
check("one fact per fiscal year (no duplicates)",
      len(years) == len(set(years)))

# --------------------------------------------------------------------------- #
section("same-period collision across two tags resolves deterministically")
# --------------------------------------------------------------------------- #
# FY2018 (2017-10-01..2018-09-29) is reported under BOTH `Revenues` (265595e6)
# and the ASC-606 tag (265596e6). The newest-standard tag wins.
fy2018 = next(r for r in annual if r["fiscal_year"] == 2018)
check("collision won by the newest-standard tag",
      rs._tag_of(fy2018["concept"])
      == "RevenueFromContractWithCustomerExcludingAssessedTax")
check("collision took the 606 value, not the Revenues value",
      fy2018["value"] == 265596000000)

# --------------------------------------------------------------------------- #
section("classification by duration, not fp")
# --------------------------------------------------------------------------- #
# The SalesRevenueNet fixture carries an fp=="FY" fact of ~90 days
# (2016-06-26..2016-09-24): a Q4 stub reported inside the 10-K.
check("an fp=='FY' fact of <=100 days classifies as a quarter",
      rs.classify_period("2016-06-26", "2016-09-24") == "quarterly")
check("...and its ~90-day duration is indeed <=100",
      rs.period_days("2016-06-26", "2016-09-24") <= 100)
check("the FY-labelled stub never appears in the annual series",
      not any(r["period_start"] == "2016-06-26" for r in annual))
check("a full 52-week period classifies as annual",
      rs.classify_period("2018-09-30", "2019-09-28") == "annual")

# --------------------------------------------------------------------------- #
section("fiscal year from period_end; restatement collapses to one entry")
# --------------------------------------------------------------------------- #
# FY2020 (2019-09-29..2020-09-26) is restated across three 10-Ks (accn 20/21/22).
check("fiscal_year derived from period_end, not fy",
      rs.fiscal_year_of("2020-09-26") == 2020)
fy2020_rows = [r for r in annual if r["fiscal_year"] == 2020]
check("three restatements collapse to a single annual entry",
      len(fy2020_rows) == 1)
check("the surviving entry is the latest accession",
      fy2020_rows[0]["accession"] == "0000320193-22-000108")
hist = store.history(
    APPLE, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "2019-09-29", "2020-09-26")
check("the store still keeps all three versions in history",
      len(hist) == 3)

# --------------------------------------------------------------------------- #
section("quarterly series with derived, flagged Q4")
# --------------------------------------------------------------------------- #
q4s = [r for r in quarterly if r["derived"]]
check("exactly one derived Q4 (for FY2019, the year with 3 quarters + annual)",
      len(q4s) == 1)
q4 = q4s[0]
check("Q4 is flagged derived and labelled Q4",
      q4["derived"] is True and q4["fiscal_period"] == "Q4")
check("Q4 == annual - (Q1+Q2+Q3)",
      q4["value"] == 260174000000 - (84310000000 + 58015000000 + 53809000000))
check("Q4 belongs to fiscal year 2019", q4["fiscal_year"] == 2019)
check("quarterly series carries the three real quarters plus the derived Q4",
      sum(1 for r in quarterly if r["fiscal_year"] == 2019) == 4)
check("quarterly series sorted by period_end",
      [r["period_end"] for r in quarterly] == sorted(r["period_end"] for r in quarterly))
check("no annual-length period leaked into the quarterly series",
      all(r["days"] <= rs._MAX_QUARTER_DAYS for r in quarterly))

# --------------------------------------------------------------------------- #
section("empty series with a reason, never raises")
# --------------------------------------------------------------------------- #
empty_store = FactsStore(":memory:")
empty_series = rs.RevenueSeries(empty_store)
check("annual_series never raises for an unknown CIK",
      not raises(Exception, empty_series.annual_series, "0000000001"))
res = empty_series.annual_series("0000000001")
check("empty series is empty", len(res) == 0 and res.facts == [])
check("empty series carries a stated reason",
      isinstance(res.reason, str) and res.reason
      and "REVENUE_TAGS" in res.reason)
check("quarterly is likewise empty with a reason",
      len(empty_series.quarterly_series("0000000001")) == 0)

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nrevenue_series gate: PASS")
