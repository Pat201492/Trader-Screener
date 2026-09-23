"""
Year-over-year and quarter-over-quarter percent change (issue #186).

Pure functions over a revenue series produced by `revenue_series` (#184): its
`annual_series` / `quarterly_series` rows (dicts carrying `period_end`,
`period_start`, `fiscal_year`, `value`, ...). Nothing here fetches or parses a
filing; it only differences an already-built series.

The point of the module is the DEGENERATE cases, handled explicitly instead of
by exception:

  * A zero prior value would be a `ZeroDivisionError`; a NEGATIVE prior value
    would silently flip the sign of the percentage (revenue "growing" from -100
    to -50 is a +50% move by the formula, which reads as growth when it is the
    opposite). Both yield `pct_change = None` with a stated `reason`, never a
    raised exception and never a sign-flipped number.
  * A missing intermediate period (fiscal year 2020 after 2018, or Q3 after Q1)
    is reported as a GAP -- `pct_change = None`, `prior_value = None`, a `reason`
    naming the gap -- rather than bridged silently, which would compare 2020 to
    2018 and label it a one-year change.

Each function returns one record per period, in period order:
``{period_end, value, prior_value, pct_change, reason}``. `reason` is None
exactly when `pct_change` is a real number; otherwise it says why there is none.

stdlib only. Run the self-check:  python tools/edgar_scrubber/revenue_change.py
"""

from datetime import date

# Two quarters are consecutive when the later one begins right after the earlier
# one ends. Contiguous filings do this to the day (Q1 ends 2018-12-29, Q2 starts
# 2018-12-30), so a small slack absorbs reporting-calendar jitter while a whole
# missing quarter (~91 days later) still reads as a gap.
_QUARTER_GAP_TOLERANCE_DAYS = 20


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pct(value, prior):
    return (value - prior) / prior * 100.0


def _yoy_consecutive(prev, cur):
    """Adjacent fiscal years: the current is exactly one year after the prior.
    A jump of two or more means an intervening year is missing (a gap)."""
    return cur["fiscal_year"] - prev["fiscal_year"] == 1


def _qoq_consecutive(prev, cur):
    """Adjacent quarters: the current quarter starts within a few days of the
    prior quarter's end. A larger gap means a quarter between them is missing."""
    gap = (date.fromisoformat(cur["period_start"])
           - date.fromisoformat(prev["period_end"])).days
    return 0 <= gap <= _QUARTER_GAP_TOLERANCE_DAYS


def _gap_reason(prev, cur):
    return (f"gap: one or more periods missing between "
            f"{prev['period_end']} and {cur['period_end']}")


def _record(cur, prev, consecutive):
    """One output row for `cur` measured against `prev`.

    Populates `pct_change`/`prior_value` only when there is a real, comparable
    immediate prior; every other outcome sets both to None and states the
    reason, so a caller never has to distinguish "not computed" from "computed
    zero" or catch an exception."""
    value = cur.get("value")
    rec = {
        "period_end": cur["period_end"],
        "value": value,
        "prior_value": None,
        "pct_change": None,
        "reason": None,
    }

    if prev is None:
        rec["reason"] = "no prior period"
        return rec
    if not consecutive:
        rec["reason"] = _gap_reason(prev, cur)  # never bridge across it
        return rec

    prior = prev.get("value")
    if not _is_number(value) or not _is_number(prior):
        rec["reason"] = "non-numeric value"
        return rec
    if prior == 0:
        rec["reason"] = "prior value is zero"      # would be ZeroDivisionError
        return rec
    if prior < 0:
        rec["reason"] = "prior value is negative"  # would flip the sign
        return rec

    rec["prior_value"] = prior
    rec["pct_change"] = _pct(value, prior)
    return rec


def _series_change(rows, consecutive_fn):
    """Difference `rows` (sorted by `period_end`) against the period before each,
    one record per row. The first row has no prior and is reported as such rather
    than dropped, so every period in the series is represented."""
    rows = sorted(rows, key=lambda r: r["period_end"])
    out = []
    prev = None
    for cur in rows:
        consecutive = prev is not None and consecutive_fn(prev, cur)
        out.append(_record(cur, prev, consecutive))
        prev = cur
    return out


def yoy_change(series):
    """Year-over-year percent change: each fiscal year against the one before it.

    `series` is an annual series (`RevenueSeries.annual_series`, or any iterable
    of rows with `fiscal_year`, `period_end`, `value`). A fiscal-year jump of
    more than one is a gap and is not bridged."""
    return _series_change(list(series), _yoy_consecutive)


def qoq_change(series):
    """Quarter-over-quarter percent change: each quarter against the one before
    it. `series` is a quarterly series (`RevenueSeries.quarterly_series`, or any
    iterable of rows with `period_start`, `period_end`, `value`). Consecutiveness
    is decided by date adjacency, so a missing quarter reads as a gap."""
    return _series_change(list(series), _qoq_consecutive)


if __name__ == "__main__":
    annual = [
        {"fiscal_year": 2018, "period_end": "2018-09-29", "value": 100},
        {"fiscal_year": 2019, "period_end": "2019-09-28", "value": 120},
        # 2020 missing -> the 2021 row is a gap, not a two-year bridge.
        {"fiscal_year": 2021, "period_end": "2021-09-25", "value": 90},
    ]
    for r in yoy_change(annual):
        print(f"  {r['period_end']}  pct={r['pct_change']}  reason={r['reason']}")

    quarterly = [
        {"period_start": "2018-09-30", "period_end": "2018-12-29", "value": 40},
        {"period_start": "2018-12-30", "period_end": "2019-03-30", "value": 30},
        # Q3 missing -> the next quarter is a gap.
        {"period_start": "2019-06-30", "period_end": "2019-09-28", "value": 50},
    ]
    print()
    for r in qoq_change(quarterly):
        print(f"  {r['period_end']}  pct={r['pct_change']}  reason={r['reason']}")

    degenerate = [
        {"fiscal_year": 2019, "period_end": "2019-09-28", "value": 0},
        {"fiscal_year": 2020, "period_end": "2020-09-26", "value": 50},
        {"fiscal_year": 2021, "period_end": "2021-09-25", "value": 60},
    ]
    recs = yoy_change(degenerate)
    assert recs[1]["pct_change"] is None and recs[1]["reason"] == "prior value is zero"
    assert recs[2]["pct_change"] == _pct(60, 50)

    print("\nrevenue_change self-check: PASS")
