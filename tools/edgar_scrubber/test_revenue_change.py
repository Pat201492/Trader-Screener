"""
Gate for year-over-year / quarter-over-quarter percent change (issue #186).
Same convention as `test_revenue_series.py` / `test_field_spec.py`: stdlib only,
run directly, exit 0 = pass. Offline -- no network, no model.

Every acceptance criterion in #186 is checked here:

  * `yoy_change` returns `{period_end, value, prior_value, pct_change}` comparing
    each fiscal year to the one before it;
  * `qoq_change` does the same for consecutive quarters;
  * a zero or negative prior value yields `pct_change` None with a stated reason,
    never a ZeroDivisionError and never a sign-flipped percentage;
  * a missing intermediate period is reported as a gap, not bridged across;
  * it consumes a series built by `revenue_series` (#184) unchanged.

Run:  python tools/edgar_scrubber/test_revenue_change.py
"""

import revenue_change as rc

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def by_end(records):
    return {r["period_end"]: r for r in records}


_REQUIRED_KEYS = {"period_end", "value", "prior_value", "pct_change"}


# --------------------------------------------------------------------------- #
section("yoy_change compares each fiscal year to the one before it")
# --------------------------------------------------------------------------- #

annual = [
    {"fiscal_year": 2018, "period_end": "2018-09-29", "value": 100},
    {"fiscal_year": 2019, "period_end": "2019-09-28", "value": 120},
    {"fiscal_year": 2020, "period_end": "2020-09-26", "value": 90},
]
yoy = by_end(rc.yoy_change(annual))
check("every record carries period_end, value, prior_value, pct_change",
      all(_REQUIRED_KEYS <= set(r) for r in yoy.values()))
check("first year has no prior (reason stated, not dropped)",
      yoy["2018-09-29"]["pct_change"] is None
      and yoy["2018-09-29"]["reason"] == "no prior period")
check("100 -> 120 is +20%",
      yoy["2019-09-28"]["pct_change"] == 20.0
      and yoy["2019-09-28"]["prior_value"] == 100)
check("120 -> 90 is -25%", yoy["2020-09-26"]["pct_change"] == -25.0)

# --------------------------------------------------------------------------- #
section("records carry fiscal_year from the series, including the first (#207)")
# --------------------------------------------------------------------------- #

# Apple-shaped annual series: FY labels present, first record's pct_change None.
apple_annual = [
    {"fiscal_year": 2016, "period_end": "2016-09-24", "value": 215639},
    {"fiscal_year": 2017, "period_end": "2017-09-30", "value": 229234},
    {"fiscal_year": 2018, "period_end": "2018-09-29", "value": 265595},
    {"fiscal_year": 2019, "period_end": "2019-09-28", "value": 260174},
]
apple = rc.yoy_change(apple_annual)
check("every record carries fiscal_year, not re-derived from period_end",
      all("fiscal_year" in r for r in apple))
check("fiscal_year matches the series entry, in period order",
      [r["fiscal_year"] for r in apple] == [2016, 2017, 2018, 2019])
first = apple[0]
check("the first record carries fiscal_year even though its pct_change is None",
      first["fiscal_year"] == 2016 and first["pct_change"] is None)
check("pre-existing record keys are all still present (additive change)",
      all(_REQUIRED_KEYS <= set(r) and "reason" in r for r in apple))
check("FY2019 -2.04% still correct alongside the new label",
      round(by_end(apple)["2019-09-28"]["pct_change"], 2) == -2.04
      and by_end(apple)["2019-09-28"]["fiscal_year"] == 2019)

# --------------------------------------------------------------------------- #
section("a missing intermediate fiscal year is a gap, not a bridge")
# --------------------------------------------------------------------------- #

gapped = [
    {"fiscal_year": 2018, "period_end": "2018-09-29", "value": 100},
    # 2019 missing
    {"fiscal_year": 2020, "period_end": "2020-09-26", "value": 200},
]
g = by_end(rc.yoy_change(gapped))["2020-09-26"]
check("gap year has pct_change None", g["pct_change"] is None)
check("gap year does not expose a bridged prior_value", g["prior_value"] is None)
check("gap year states it is a gap", "gap" in (g["reason"] or ""))
check("the bridged +100% is NOT what comes back",
      g["pct_change"] != 100.0)

# --------------------------------------------------------------------------- #
section("zero and negative prior values are handled, not raised or sign-flipped")
# --------------------------------------------------------------------------- #

zero_prior = [
    {"fiscal_year": 2019, "period_end": "2019-09-28", "value": 0},
    {"fiscal_year": 2020, "period_end": "2020-09-26", "value": 50},
]
try:
    z = by_end(rc.yoy_change(zero_prior))["2020-09-26"]
    raised = False
except ZeroDivisionError:
    raised = True
check("a zero prior value raises no ZeroDivisionError", not raised)
check("zero prior -> pct_change None with a stated reason",
      not raised and z["pct_change"] is None and z["reason"] == "prior value is zero")

neg_prior = [
    {"fiscal_year": 2019, "period_end": "2019-09-28", "value": -100},
    {"fiscal_year": 2020, "period_end": "2020-09-26", "value": -50},
]
n = by_end(rc.yoy_change(neg_prior))["2020-09-26"]
check("negative prior -> pct_change None (never the sign-flipped +50%)",
      n["pct_change"] is None and n["reason"] == "prior value is negative")

# --------------------------------------------------------------------------- #
section("qoq_change over consecutive quarters, with a gap")
# --------------------------------------------------------------------------- #

quarters = [
    {"fiscal_year": 2019, "fiscal_period": "Q1",
     "period_start": "2018-09-30", "period_end": "2018-12-29", "value": 40},
    {"fiscal_year": 2019, "fiscal_period": "Q2",
     "period_start": "2018-12-30", "period_end": "2019-03-30", "value": 30},
    # Q3 (ends ~2019-06) missing -> the next quarter is a gap
    {"fiscal_year": 2019, "fiscal_period": "Q4",
     "period_start": "2019-06-30", "period_end": "2019-09-28", "value": 60},
]
q = by_end(rc.qoq_change(quarters))
check("consecutive quarters 40 -> 30 is -25%",
      q["2019-03-30"]["pct_change"] == -25.0)
check("the quarter after a missing one is a gap, not bridged",
      q["2019-09-28"]["pct_change"] is None
      and q["2019-09-28"]["prior_value"] is None
      and "gap" in (q["2019-09-28"]["reason"] or ""))
check("qoq records carry fiscal_year and fiscal_period from the series (#207)",
      q["2019-03-30"]["fiscal_year"] == 2019
      and q["2019-03-30"]["fiscal_period"] == "Q2")
check("qoq keeps every pre-existing key (additive change)",
      all(_REQUIRED_KEYS <= set(r) and "reason" in r
          for r in rc.qoq_change(quarters)))

# --------------------------------------------------------------------------- #
section("consumes a real revenue_series (#184) result unchanged")
# --------------------------------------------------------------------------- #

import revenue_series as rvs
from facts_store import FactsStore

concept = {
    "taxonomy": "us-gaap",
    "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "units": {"USD": [
        {"start": "2018-09-30", "end": "2019-09-28", "val": 100,
         "accn": "a19", "fy": 2019, "fp": "FY", "form": "10-K"},
        {"start": "2019-09-29", "end": "2020-09-26", "val": 120,
         "accn": "a20", "fy": 2020, "fp": "FY", "form": "10-K"},
    ]},
}
store = FactsStore(":memory:")
store.put_facts(rvs.facts_from_concept("0000320193", concept))
annual_result = rvs.RevenueSeries(store).annual_series("0000320193")
real = rc.yoy_change(annual_result)  # RevenueSeriesResult is iterable of rows
check("yoy over the real annual series gives 100 -> 120 = +20%",
      by_end(real)["2020-09-26"]["pct_change"] == 20.0)

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nrevenue_change gate: PASS")
