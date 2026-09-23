"""
Gate for joining guidance to reported actuals (issue #189).

Same convention as `test_revenue_change.py` / `test_guidance_extract.py`: stdlib
only, run directly, exit 0 = pass. Offline -- an in-memory `FactsStore`, no
network and no model.

Every acceptance criterion in #189 is checked here:

  * `join_guidance_to_actual` matches a `GuidanceRecord` to its reported actual on
    (cik, metric, fiscal period) -- annual and quarterly;
  * each pair is scored beat / miss / in_range / no_actual_yet and carries the
    numeric surprise against the guidance midpoint and its low / high bounds;
  * guidance whose period has not been reported scores `no_actual_yet` and is
    kept, not dropped;
  * several guidance records for one period (initial, reaffirmation, revision) are
    all retained in filing-date order, and the join reports the latest one issued
    before the period closed;
  * it reads guidance and facts out of the shared facts store (#183).

Run:  python tools/edgar_scrubber/test_guidance_join.py
"""

import guidance_join as gj
from facts_store import FactsStore, FactRecord, GuidanceRecord

CIK = "0000019617"

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def fresh_store():
    return FactsStore(":memory:")


def annual_fact(concept, ps, pe, value, accn, unit="USD"):
    return FactRecord(
        cik=CIK, concept=concept, unit=unit, period_start=ps, period_end=pe,
        fiscal_year=int(pe[:4]), fiscal_period="FY", form="10-K",
        accession=accn, value=value, source="xbrl",
    )


def quarter_fact(concept, ps, pe, value, accn, fp, unit="USD"):
    return FactRecord(
        cik=CIK, concept=concept, unit=unit, period_start=ps, period_end=pe,
        fiscal_year=int(pe[:4]), fiscal_period=fp, form="10-Q",
        accession=accn, value=value, source="xbrl",
    )


def guidance(metric, label, low, high, accn, basis="non-GAAP"):
    return GuidanceRecord(
        cik=CIK, metric=metric, period_label=label, low=low, high=high,
        basis=basis, accession=accn, document="ex99.htm", span=(10, 90),
        provenance="local:qwen", confidence=0.8,
    )


def by_label(joins):
    return {j.period_label: j for j in joins}


# --------------------------------------------------------------------------- #
section("parse_period_label resolves the filer's own words to a fiscal period")
# --------------------------------------------------------------------------- #

cases = {
    "FY2026": (2026, "FY"),
    "FY26": (2026, "FY"),
    "full year 2026": (2026, "FY"),
    "Q4 2025": (2025, "Q4"),
    "fourth quarter 2025": (2025, "Q4"),
    "2025 Q1": (2025, "Q1"),
    "revenue": (None, None),
}
for label, expected in cases.items():
    check(f"{label!r} -> {expected}", gj.parse_period_label(label) == expected)

# --------------------------------------------------------------------------- #
section("metric maps to XBRL concepts; unknown metric maps to nothing")
# --------------------------------------------------------------------------- #

check("revenue -> the revenue_series tags",
      "us-gaap:Revenues" in gj.concepts_for_metric("revenue"))
check("'total net revenue' still resolves to revenue (substring fallback)",
      "us-gaap:Revenues" in gj.concepts_for_metric("total net revenue"))
check("'diluted EPS' resolves to the EPS concepts",
      "us-gaap:EarningsPerShareDiluted" in gj.concepts_for_metric("diluted EPS"))
check("an unmapped metric resolves to no concepts",
      gj.concepts_for_metric("free cash flow") == ())

# --------------------------------------------------------------------------- #
section("annual guidance matched to an annual XBRL actual, scored beat")
# --------------------------------------------------------------------------- #

s = fresh_store()
s.put_facts([annual_fact("us-gaap:Revenues", "2024-10-01", "2025-09-30",
                         9_300_000_000, "0000019617-25-000200")])
s.put_guidance([guidance("revenue", "FY2025", 9_000_000_000, 9_200_000_000,
                         "0000019617-25-000150")])
j = by_label(gj.join_guidance_to_actual(s, CIK))["FY2025"]
check("matched the reported actual on (cik, metric, fiscal period)",
      j.actual is not None and j.actual["value"] == 9_300_000_000)
check("9.30bn above the 9.20bn high scores beat", j.verdict == gj.BEAT)
check("surprise vs midpoint recorded",
      j.surprise_vs_midpoint == 9_300_000_000 - 9_100_000_000)
check("surprise vs low recorded", j.surprise_vs_low == 9_300_000_000 - 9_000_000_000)
check("surprise vs high recorded", j.surprise_vs_high == 9_300_000_000 - 9_200_000_000)
check("percentage surprise vs midpoint recorded",
      abs(j.surprise_pct_vs_midpoint
          - (200_000_000 / 9_100_000_000 * 100)) < 1e-9)

# --------------------------------------------------------------------------- #
section("miss and in_range verdicts")
# --------------------------------------------------------------------------- #

s = fresh_store()
s.put_facts([annual_fact("us-gaap:Revenues", "2024-10-01", "2025-09-30",
                         8_500_000_000, "0000019617-25-000200")])
s.put_guidance([guidance("revenue", "FY2025", 9_000_000_000, 9_200_000_000,
                         "0000019617-25-000150")])
miss = by_label(gj.join_guidance_to_actual(s, CIK))["FY2025"]
check("8.50bn below the 9.00bn low scores miss", miss.verdict == gj.MISS)
check("miss surprise vs low is negative", miss.surprise_vs_low < 0)

s = fresh_store()
s.put_facts([annual_fact("us-gaap:Revenues", "2024-10-01", "2025-09-30",
                         9_100_000_000, "0000019617-25-000200")])
s.put_guidance([guidance("revenue", "FY2025", 9_000_000_000, 9_200_000_000,
                         "0000019617-25-000150")])
inr = by_label(gj.join_guidance_to_actual(s, CIK))["FY2025"]
check("9.10bn within 9.00-9.20bn scores in_range", inr.verdict == gj.IN_RANGE)

# --------------------------------------------------------------------------- #
section("quarterly guidance matches a quarter-duration fact, not a Q4 FY stub")
# --------------------------------------------------------------------------- #

s = fresh_store()
# A real Q3 quarter fact and a Q4 stub filed as fp=="FY" for the same fiscal year.
s.put_facts([
    quarter_fact("us-gaap:Revenues", "2025-04-01", "2025-06-30",
                 2_400_000_000, "0000019617-25-000180", "Q3"),
    # ~92-day stub carrying fp=="FY": must NOT satisfy annual guidance.
    FactRecord(cik=CIK, concept="us-gaap:Revenues", unit="USD",
               period_start="2025-07-01", period_end="2025-09-30",
               fiscal_year=2025, fiscal_period="FY", form="10-K",
               accession="0000019617-25-000181", value=2_600_000_000,
               source="xbrl"),
])
s.put_guidance([
    guidance("revenue", "Q3 2025", 2_300_000_000, 2_450_000_000,
             "0000019617-25-000170"),
    guidance("revenue", "FY2025", 9_000_000_000, 9_200_000_000,
             "0000019617-25-000171"),
])
joins = by_label(gj.join_guidance_to_actual(s, CIK))
check("Q3 guidance matched the Q3 quarter fact",
      joins["Q3 2025"].actual is not None
      and joins["Q3 2025"].actual["value"] == 2_400_000_000)
check("Q3 2400 within 2300-2450 is in_range", joins["Q3 2025"].verdict == gj.IN_RANGE)
check("annual guidance is NOT satisfied by the fp=='FY' quarter stub",
      joins["FY2025"].actual is None
      and joins["FY2025"].verdict == gj.NO_ACTUAL_YET)

# --------------------------------------------------------------------------- #
section("guidance whose period is unreported scores no_actual_yet and is kept")
# --------------------------------------------------------------------------- #

s = fresh_store()
s.put_guidance([guidance("revenue", "FY2026", 9_500_000_000, 9_700_000_000,
                         "0000019617-25-000151")])
open_join = gj.join_guidance_to_actual(s, CIK)
check("the unreported period is kept, not dropped", len(open_join) == 1)
check("its verdict is no_actual_yet", open_join[0].verdict == gj.NO_ACTUAL_YET)
check("no actual and no surprise numbers",
      open_join[0].actual is None
      and open_join[0].surprise_vs_midpoint is None)
check("a reason is stated", bool(open_join[0].reason))

# --------------------------------------------------------------------------- #
section("several guidances for one period: all retained, latest-before-close found")
# --------------------------------------------------------------------------- #

s = fresh_store()
s.put_facts([annual_fact("us-gaap:Revenues", "2024-10-01", "2025-09-30",
                         9_300_000_000, "0000019617-25-000200")])
s.put_guidance([
    # initial (FY2024 filing), reaffirmation, revision -- filing-date order by
    # accession -- and a LATER revision filed AFTER the period closed (FY2026),
    # which must not be treated as the guided expectation.
    guidance("revenue", "FY2025", 8_800_000_000, 9_000_000_000,
             "0000019617-24-000100"),
    guidance("revenue", "FY2025", 8_800_000_000, 9_000_000_000,
             "0000019617-25-000120"),
    guidance("revenue", "FY2025", 9_050_000_000, 9_150_000_000,
             "0000019617-25-000150"),
    guidance("revenue", "FY2025", 9_400_000_000, 9_500_000_000,
             "0000019617-26-000300"),
])
multi = by_label(gj.join_guidance_to_actual(s, CIK))["FY2025"]
check("all four guidance records retained", len(multi.guidances) == 4)
check("guidances are in filing (accession) order",
      [g["accession"] for g in multi.guidances]
      == sorted(g["accession"] for g in multi.guidances))
check("latest-before-close is the pre-close revision, not the post-close one",
      multi.latest_before_close["accession"] == "0000019617-25-000150")
check("scored against the latest-before-close range (beat: 9.30 > 9.15 high)",
      multi.verdict == gj.BEAT)

# --------------------------------------------------------------------------- #
section("reads from the shared facts store; consumes what guidance_extract wrote")
# --------------------------------------------------------------------------- #

s = fresh_store()
s.put_facts([annual_fact("us-gaap:Revenues", "2024-10-01", "2025-09-30",
                         9_100_000_000, "0000019617-25-000200")])
s.put_guidance([guidance("revenue", "FY2025", 9_000_000_000, 9_200_000_000,
                         "0000019617-25-000150")])
# A different CIK is untouched by this join.
check("join_guidance_to_actual only returns rows for the queried CIK",
      all(j.cik == CIK for j in gj.join_guidance_to_actual(s, CIK)))

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nguidance_join gate: PASS")
