"""
Resolve the revenue concept per filer and build annual + quarterly series
(issue #184).

There is no single "revenue" tag, and the XBRL convenience fields lie about the
period, so a clean series cannot be read off any one column. Three things,
measured live against `companyconcept` for Apple (CIK 0000320193) on 2026-09-23,
shape every rule here:

  1. THREE tags, one per accounting era, all returning real facts:
     `SalesRevenueNet` (FY2009-2017), `Revenues` (FY2018), and
     `RevenueFromContractWithCustomerExcludingAssessedTax` (FY2019-2025). They
     PARTITION by ASC 606 adoption; they are not fallbacks for one another.
     Taking the first tag that has facts silently drops 2009-2018. So the series
     is the UNION across all three (`_resolved_facts`), keyed by period.

  2. `fp == "FY"` does NOT mean an annual period. 16 of 37 `fp == "FY"` facts on
     the current tag cover 100 days or fewer -- Q4 stubs reported inside the
     10-K. So a period is classified annual vs quarterly by its actual DURATION
     in days (`period_end` minus `period_start`), never by `fp` (`classify_period`).

  3. `fy` is the fiscal year of the FILING, not of the period: the one period
     `2022-09-25..2023-09-30` carries `fy` 2023, 2024 and 2025 -- once per 10-K
     that restated it. So the fiscal year of a fact is derived from its period
     `end` date (`fiscal_year_of`), never read from `fy`, and the three
     restatements collapse to one entry via the facts store's supersession.

The facts themselves come through the period-keyed facts store (#183), whose
`facts_for` already collapses supersession to the latest accession per
`(concept, period)` -- so "a period restated under a later accession resolves to
the later value" is the store's guarantee, reused rather than re-implemented.
Populate the store from `companyconcept` JSON with `facts_from_concept` /
`ingest_revenue`.

stdlib only. Run the self-check:  python tools/edgar_scrubber/revenue_series.py
"""

from dataclasses import dataclass, field as dc_field
from datetime import date

try:  # package import (from .revenue_series import ...)
    from .edgar_client import cik10, EdgarHTTPError
except ImportError:  # flat import (import revenue_series)
    from edgar_client import cik10, EdgarHTTPError


# ── The revenue tags, newest accounting standard first ───────────────────────
#
# Order is load-bearing: it is the collision-resolution priority (see
# `_resolved_facts`). When two tags report the SAME period at an era boundary,
# the tag earliest in this list -- the most recent accounting standard -- wins.
REVENUE_TAGS = (
    # ASC 606 (revenue from contracts with customers), FY2019-2025 on Apple.
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    # The interim `Revenues` era, FY2018 on Apple.
    "Revenues",
    # Pre-606 `SalesRevenueNet`, FY2009-2017 on Apple.
    "SalesRevenueNet",
)

_TAXONOMY = "us-gaap"

# Period classification thresholds, in days. A fiscal quarter is 13-14 weeks
# (<=98 days); a fiscal year is 52-53 weeks (~364-371 days). The gap between
# them is wide, so a Q4 stub (<=100 days) can never be mistaken for a year and a
# 53-week year is still caught. A duration between the two (a half-year stub) is
# classified "other" and appears in neither series.
_MAX_QUARTER_DAYS = 100
_MIN_ANNUAL_DAYS = 300
_MAX_ANNUAL_DAYS = 380


def _concept_for(tag):
    return f"{_TAXONOMY}:{tag}"


def _tag_of(concept):
    return concept.split(":", 1)[1] if ":" in concept else concept


def _parse_iso(d):
    return date.fromisoformat(d)


def period_days(period_start, period_end):
    """Duration of a period in days -- `end` minus `start`. This, not `fp`, is
    what decides annual vs quarterly (measured fact 2: `fp == "FY"` includes Q4
    stubs of 100 days or fewer)."""
    return (_parse_iso(period_end) - _parse_iso(period_start)).days


def classify_period(period_start, period_end):
    """"annual" | "quarterly" | "other", from the period's actual duration.

    Never reads `fp`: an `fp == "FY"` fact of 100 days or fewer is a Q4 stub and
    classifies as "quarterly", so it can never reach the annual series."""
    days = period_days(period_start, period_end)
    if days <= _MAX_QUARTER_DAYS:
        return "quarterly"
    if _MIN_ANNUAL_DAYS <= days <= _MAX_ANNUAL_DAYS:
        return "annual"
    return "other"


def fiscal_year_of(period_end):
    """The fiscal year a fact belongs to, derived from its period `end` date --
    never from the filing's `fy` field (measured fact 3: `fy` is the filing's
    year, and a restated period carries a different `fy` in each filing that
    restates it). The calendar year the period ends in is the fiscal-year label
    for a fiscal year that ends in the second half of the year (Apple: FY2023
    ends 2023-09-30); annual periods are grouped by exactly this."""
    return _parse_iso(period_end).year


# ── Facts in / out ───────────────────────────────────────────────────────────

def facts_from_concept(cik, concept_json, *, source="xbrl"):
    """Flatten a `companyconcept` JSON payload into `FactRecord`s the store
    accepts. Only duration facts (a `start` and an `end`) are revenue periods;
    an instant fact with no `start` is skipped. Imported lazily so this module
    does not pull the store in at import time when only the pure helpers are
    wanted."""
    try:
        from .facts_store import FactRecord
    except ImportError:
        from facts_store import FactRecord

    taxonomy = concept_json.get("taxonomy", _TAXONOMY)
    tag = concept_json["tag"]
    concept = f"{taxonomy}:{tag}"
    cik_padded = cik10(cik)
    out = []
    for unit, entries in (concept_json.get("units") or {}).items():
        for e in entries:
            start, end = e.get("start"), e.get("end")
            if not start or not end:
                continue  # instant fact (balance-sheet point), not a period
            out.append(FactRecord(
                cik=cik_padded, concept=concept, unit=unit,
                period_start=start, period_end=end,
                fiscal_year=e.get("fy"), fiscal_period=e.get("fp"),
                form=e.get("form"), accession=e.get("accn"),
                value=e.get("val"), source=source,
            ))
    return out


def ingest_revenue(store, client, cik):
    """Fetch every tag in `REVENUE_TAGS` from `companyconcept` and write it to
    `store`. A tag the filer never used returns 404 -- recorded as 0 facts, not
    an error, since a filer only ever reports under the tags of the eras it
    filed in. Returns `{tag: count}`."""
    counts = {}
    for tag in REVENUE_TAGS:
        try:
            data = client.company_concept(cik, _TAXONOMY, tag)
        except EdgarHTTPError as exc:
            if exc.status == 404:
                counts[tag] = 0
                continue
            raise
        recs = facts_from_concept(cik, data)
        store.put_facts(recs)
        counts[tag] = len(recs)
    return counts


# ── Series result ────────────────────────────────────────────────────────────

@dataclass
class RevenueSeriesResult:
    """A series plus the reason it looks the way it does. `reason` is always
    populated -- most importantly when `facts` is empty, so a company carrying
    none of `REVENUE_TAGS` reports WHY instead of raising or returning a bare
    ``[]`` a caller cannot distinguish from "not yet ingested"."""

    facts: list
    reason: str

    def __iter__(self):
        return iter(self.facts)

    def __len__(self):
        return len(self.facts)


def _entry(fact, *, fiscal_year, fiscal_period, derived=False,
           value=None, period_start=None, period_end=None):
    """One output row. Defaults copy the source fact; a derived Q4 overrides
    value/period and flags itself."""
    ps = period_start if period_start is not None else fact["period_start"]
    pe = period_end if period_end is not None else fact["period_end"]
    return {
        "concept": fact["concept"],
        "unit": fact["unit"],
        "period_start": ps,
        "period_end": pe,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "value": value if value is not None else fact["value"],
        "accession": fact["accession"],
        "form": fact["form"],
        "days": period_days(ps, pe),
        "derived": derived,
    }


class RevenueSeries:
    """Builds annual and quarterly revenue series for a CIK out of a facts
    store already populated (via `ingest_revenue`) with the `REVENUE_TAGS`."""

    def __init__(self, store):
        self.store = store

    # -- resolution --------------------------------------------------------

    def _resolved_facts(self, cik):
        """The UNION of facts across every `REVENUE_TAGS` tag, keyed by period.

        `facts_for` has already collapsed supersession (latest accession per
        concept+period), so restatement is handled by the store. Here we resolve
        the remaining collision: the SAME period reported under two DIFFERENT
        tags at an era boundary. The tag earliest in `REVENUE_TAGS` (the most
        recent accounting standard) wins -- deterministic, and stated once in the
        `REVENUE_TAGS` comment."""
        cik_padded = cik10(cik)
        priority = {tag: i for i, tag in enumerate(REVENUE_TAGS)}
        by_period = {}
        for tag in REVENUE_TAGS:
            for f in self.store.facts_for(cik_padded, concept=_concept_for(tag)):
                key = (f["period_start"], f["period_end"])
                cur = by_period.get(key)
                if cur is None or priority[tag] < priority[_tag_of(cur["concept"])]:
                    by_period[key] = f
        return list(by_period.values())

    def _empty_reason(self, cik):
        return (f"no facts under any of REVENUE_TAGS "
                f"{list(REVENUE_TAGS)} for CIK {cik10(cik)}")

    # -- annual ------------------------------------------------------------

    def annual_series(self, cik):
        """One fact per fiscal year, sorted by `period_end`.

        Fiscal year is derived from `period_end` (`fiscal_year_of`), so a period
        restated across several filings -- collapsed to one by the store -- is a
        single entry. Only annual-duration periods qualify; a Q4 `fp == "FY"`
        stub is classified quarterly and excluded."""
        resolved = self._resolved_facts(cik)
        if not resolved:
            return RevenueSeriesResult([], self._empty_reason(cik))

        by_fy = {}
        for f in resolved:
            if classify_period(f["period_start"], f["period_end"]) != "annual":
                continue
            fy = fiscal_year_of(f["period_end"])
            # If two annual periods somehow map to the same fiscal year, the one
            # ending later is the authoritative full-year period.
            cur = by_fy.get(fy)
            if cur is None or f["period_end"] > cur["period_end"]:
                by_fy[fy] = f

        rows = [_entry(f, fiscal_year=fy, fiscal_period="FY")
                for fy, f in by_fy.items()]
        rows.sort(key=lambda r: r["period_end"])
        reason = (f"{len(rows)} annual period(s) across "
                  f"{len({r['concept'] for r in rows})} tag(s)")
        return RevenueSeriesResult(rows, reason)

    # -- quarterly ---------------------------------------------------------

    def quarterly_series(self, cik):
        """Quarter-length facts, plus a Q4 DERIVED as the annual figure minus
        Q1+Q2+Q3 and flagged ``derived``.

        A quarter is assigned to the annual period that CONTAINS its dates, so
        Apple's Q1 (which ends in December, a calendar year before its fiscal
        year) still rolls into the right 10-K. When exactly three quarters sit
        inside an annual period, Q4 = annual - (Q1+Q2+Q3), spanning the day after
        the last quarter's end to the annual end. Fewer or more than three and
        the derivation is skipped (it cannot be done unambiguously)."""
        resolved = self._resolved_facts(cik)
        if not resolved:
            return RevenueSeriesResult([], self._empty_reason(cik))

        quarters, annuals = [], []
        for f in resolved:
            kind = classify_period(f["period_start"], f["period_end"])
            if kind == "quarterly":
                quarters.append(f)
            elif kind == "annual":
                annuals.append(f)

        rows = []
        used = set()
        for a in annuals:
            a_start, a_end = a["period_start"], a["period_end"]
            fy = fiscal_year_of(a_end)
            contained = sorted(
                (q for q in quarters
                 if q["period_start"] >= a_start and q["period_end"] <= a_end),
                key=lambda q: q["period_end"],
            )
            for q in contained:
                used.add((q["period_start"], q["period_end"]))
                rows.append(_entry(q, fiscal_year=fy,
                                   fiscal_period=_quarter_label(q, a_start)))
            if len(contained) == 3 and _is_number(a["value"]) and \
                    all(_is_number(q["value"]) for q in contained):
                q4_val = a["value"] - sum(q["value"] for q in contained)
                q4_start = _next_day(contained[-1]["period_end"])
                rows.append(_entry(
                    a, fiscal_year=fy, fiscal_period="Q4", derived=True,
                    value=q4_val, period_start=q4_start, period_end=a_end,
                ))

        # Quarters not inside any annual period (e.g. the most recent quarters,
        # whose 10-K has not been filed yet) still belong in the series; label
        # them by their own end-date fiscal year.
        for q in quarters:
            if (q["period_start"], q["period_end"]) in used:
                continue
            rows.append(_entry(q, fiscal_year=fiscal_year_of(q["period_end"]),
                               fiscal_period=_quarter_label(q, None)))

        rows.sort(key=lambda r: r["period_end"])
        reason = (f"{len(rows)} quarter(s) incl. "
                  f"{sum(1 for r in rows if r['derived'])} derived Q4")
        return RevenueSeriesResult(rows, reason)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _next_day(iso):
    return date.fromordinal(_parse_iso(iso).toordinal() + 1).isoformat()


def _quarter_label(q, annual_start):
    """Best-effort Q1..Q3 label from position within the fiscal year. Ordinal,
    not authoritative -- the annual/quarterly split is what the acceptance turns
    on; this is a convenience label. Falls back to "Q" when the anchor is
    unknown."""
    if annual_start is None:
        return "Q"
    months = (_parse_iso(q["period_end"]).toordinal()
              - _parse_iso(annual_start).toordinal()) // 91
    n = min(max(months + 1, 1), 3)
    return f"Q{n}"


if __name__ == "__main__":
    try:
        from facts_store import FactsStore
    except ImportError:
        from .facts_store import FactsStore

    # A tiny two-era company, entirely in-memory: pre-606 tag for FY2018,
    # ASC-606 tag for FY2019 with its three quarters, and a restatement.
    concept_old = {
        "taxonomy": "us-gaap", "tag": "SalesRevenueNet",
        "units": {"USD": [
            {"start": "2017-10-01", "end": "2018-09-29", "val": 100,
             "accn": "acc-2018", "fy": 2018, "fp": "FY", "form": "10-K"},
            # Same period, later accession -> supersedes (store keeps both,
            # facts_for returns the later value).
            {"start": "2017-10-01", "end": "2018-09-29", "val": 101,
             "accn": "acc-2019b", "fy": 2019, "fp": "FY", "form": "10-K"},
        ]},
    }
    concept_new = {
        "taxonomy": "us-gaap",
        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "units": {"USD": [
            {"start": "2018-09-30", "end": "2019-09-28", "val": 120,
             "accn": "acc-2019", "fy": 2019, "fp": "FY", "form": "10-K"},
            {"start": "2018-09-30", "end": "2018-12-29", "val": 40,
             "accn": "acc-2019q1", "fy": 2019, "fp": "Q1", "form": "10-Q"},
            {"start": "2018-12-30", "end": "2019-03-30", "val": 30,
             "accn": "acc-2019q2", "fy": 2019, "fp": "Q2", "form": "10-Q"},
            {"start": "2019-03-31", "end": "2019-06-29", "val": 25,
             "accn": "acc-2019q3", "fy": 2019, "fp": "Q3", "form": "10-Q"},
        ]},
    }
    store = FactsStore(":memory:")
    store.put_facts(facts_from_concept("0000320193", concept_old))
    store.put_facts(facts_from_concept("0000320193", concept_new))

    rs = RevenueSeries(store)
    annual = rs.annual_series("0000320193")
    print("annual:", [(r["fiscal_year"], r["value"]) for r in annual])
    q = rs.quarterly_series("0000320193")
    print("quarterly:", [(r["fiscal_period"], r["value"], r["derived"]) for r in q])
    empty = rs.annual_series("0000000001")
    print("empty reason:", empty.reason)
    assert [r["fiscal_year"] for r in annual] == [2018, 2019]
    assert [r["value"] for r in annual] == [101, 120]  # 101 = superseded FY2018
    q4 = next(r for r in q if r["derived"])
    assert q4["value"] == 120 - (40 + 30 + 25)
    assert len(empty) == 0 and empty.reason
    print("\nrevenue_series self-check: PASS")
