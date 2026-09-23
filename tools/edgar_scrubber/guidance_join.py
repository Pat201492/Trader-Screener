"""
Join guidance to reported actuals and score beat / miss / in-range (issue #189).

The two halves of the earnings picture live in the same period-keyed facts store
(#183): what a company SAID it expected (`GuidanceRecord`, read from prose by
#188) and what it later REPORTED (`FactRecord`, lifted exact from XBRL by #184).
This module puts them together -- for each guided period it finds the reported
actual and scores the guidance against it.

Because the actual side is XBRL, half of every comparison is EXACT: the surprise
is measured against a number the filer reported, not one the model guessed.

Three things shape the join:

  * A guidance record names a metric in the filer's own words ("revenue", "EPS")
    and a period in the filer's own words ("FY2026", "Q4 2025"). A reported fact
    is keyed by an XBRL concept ("us-gaap:Revenues") and a period's actual dates.
    So the join needs a METRIC->concept map (`DEFAULT_METRIC_CONCEPTS`, override
    per call) and a period-LABEL parser (`parse_period_label`), and it matches on
    the resolved `(cik, concept-set, fiscal year, period kind)`.

  * Period kind is decided the way `revenue_series` (#184) decides it: by the
    fact's actual DURATION (`classify_period`) and its END-date fiscal year
    (`fiscal_year_of`), never by the unreliable `fp`/`fy` columns. So an annual
    guidance matches an annual-duration fact and a quarterly guidance a
    quarter-duration one -- a Q4 `fp=="FY"` stub can never satisfy annual
    guidance.

  * Guidance whose period has not yet been reported is KEPT, scored
    `no_actual_yet`, never dropped -- an open guided period is the normal state,
    not an error.

Supersession, on the guidance side this time. A period is guided more than once:
an initial number, a reaffirmation, a revision. All are retained, ordered by
FILING (accession order -- a later filing carries a later accession, the same
proxy `facts_store` uses), and the join reports which guidance was the LATEST one
issued before the period closed -- that is the number an actual should be scored
against.

stdlib only. Run the self-check:  python tools/edgar_scrubber/guidance_join.py
"""

import re
from dataclasses import dataclass

try:  # package import (from .guidance_join import ...)
    from .edgar_client import cik10
    from .revenue_series import REVENUE_TAGS, classify_period, fiscal_year_of
except ImportError:  # flat import (import guidance_join)
    from edgar_client import cik10
    from revenue_series import REVENUE_TAGS, classify_period, fiscal_year_of


# ── Metric -> XBRL concept ────────────────────────────────────────────────────
#
# Guidance names a metric in prose; a reported fact is keyed by a taxonomy
# concept. This maps the handful of metrics guidance is actually given in to the
# concepts they report under. Revenue reuses `revenue_series.REVENUE_TAGS` verbatim
# (the three accounting-era tags), so the join resolves revenue exactly as the
# series does rather than inventing a second list. Callers extend or replace this
# via the `metric_concepts` argument.
_EPS_CONCEPTS = ("us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasic")

DEFAULT_METRIC_CONCEPTS = {
    "revenue": tuple(f"us-gaap:{tag}" for tag in REVENUE_TAGS),
    "eps": _EPS_CONCEPTS,
    "earnings per share": _EPS_CONCEPTS,
}


def _normalize_metric(metric):
    return (metric or "").strip().lower()


def concepts_for_metric(metric, metric_concepts=None):
    """The XBRL concepts a prose metric reports under, or ``()`` when unknown.

    Exact (normalized) match first; failing that, a substring fallback so
    "total revenue" / "net revenues" resolve to the revenue concepts and
    "diluted EPS" / "earnings per share" to the EPS concepts. An unknown metric
    returns ``()`` -- the caller scores it `no_actual_yet` with a reason, never
    guesses a concept."""
    table = metric_concepts if metric_concepts is not None else DEFAULT_METRIC_CONCEPTS
    m = _normalize_metric(metric)
    if not m:
        return ()
    if m in table:
        return tuple(table[m])
    for key, concepts in table.items():
        if key in m:
            return tuple(concepts)
    if "revenue" in m or "sales" in m:
        return tuple(DEFAULT_METRIC_CONCEPTS["revenue"])
    if "eps" in m or "per share" in m:
        return _EPS_CONCEPTS
    return ()


# ── Period label -> (fiscal year, period kind) ────────────────────────────────

#: The period kinds the join matches on. "FY" is an annual period; "Q1".."Q4" a
#: quarter. `None` means the label named neither (it cannot be matched to a fact).
_ANNUAL = "FY"

_QUARTER_WORDS = {
    "first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4",
}
_ANNUAL_WORDS = ("full year", "full-year", "fiscal year", "annual")

# A four-digit year, not itself part of a longer number. No leading `\b`: in
# "FY2025" the "FY" is a word char, so `\b` before the year never fires -- a digit
# lookbehind/lookahead is what actually isolates the year.
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_FY2DIGIT_RE = re.compile(r"\bFY\s?['’]?(\d{2})(?!\d)", re.IGNORECASE)
_QNUM_RE = re.compile(r"\bQ\s?([1-4])\b", re.IGNORECASE)
_QORD_RE = re.compile(r"\b(first|second|third|fourth)\s+quarter\b", re.IGNORECASE)


def parse_period_label(label):
    """Turn a filer's period label into ``(fiscal_year, kind)``.

    ``fiscal_year`` is an ``int`` or ``None``; ``kind`` is "FY", "Q1".."Q4" or
    ``None``. Handles "FY2026", "FY26", "Q4 2025", "fourth quarter 2025",
    "full year 2026". A label with no resolvable year yields ``(None, kind)`` --
    which cannot be matched to a fact, since a fact is fixed to a fiscal year.

    A quarter marker wins over the annual default: "Q4 FY2025" is a quarter."""
    text = label or ""

    year = None
    m = _YEAR_RE.search(text)
    if m:
        year = int(m.group(1))
    else:
        m2 = _FY2DIGIT_RE.search(text)
        if m2:
            year = 2000 + int(m2.group(1))

    kind = None
    qn = _QNUM_RE.search(text)
    if qn:
        kind = f"Q{qn.group(1)}"
    else:
        qo = _QORD_RE.search(text)
        if qo:
            kind = _QUARTER_WORDS[qo.group(1).lower()]

    if kind is None:
        low = text.lower()
        # "FY", "FY2025", "FY 2025", "FY26" -- an "fy" token, whether or not a
        # year is glued to it -- or a spelled-out annual word, means annual.
        if any(w in low for w in _ANNUAL_WORDS) or re.search(r"\bfy", low):
            kind = _ANNUAL

    return year, kind


# ── Filing-date proxy from an accession ───────────────────────────────────────
#
# A `GuidanceRecord` carries no filing date, only an accession. Accessions are
# `NNNNNNNNNN-YY-NNNNNN`; the two-digit segment is the filing year, and the whole
# string sorts chronologically (the exact assumption `facts_store.latest` makes).
# So accession order IS filing order, and the year segment gives a coarse filing
# year -- enough to tell a guidance issued in the guided year from one issued
# after the period closed.
_ACCN_YEAR_RE = re.compile(r"^\d{10}-(\d{2})-\d{6}$")


def filing_year(accession):
    """The filing year encoded in an accession's middle segment, or ``None`` when
    it does not fit the `NNNNNNNNNN-YY-NNNNNN` shape. Two-digit years map into the
    2000s (EDGAR accessions in this form postdate 2000)."""
    if not accession:
        return None
    m = _ACCN_YEAR_RE.match(accession)
    if not m:
        return None
    return 2000 + int(m.group(1))


# ── Scoring ───────────────────────────────────────────────────────────────────

#: Verdicts. `no_actual_yet` is a first-class outcome, not a missing value: the
#: period simply has not been reported yet, and the guidance is kept.
BEAT, MISS, IN_RANGE, NO_ACTUAL_YET = "beat", "miss", "in_range", "no_actual_yet"


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _midpoint(low, high):
    if _is_number(low) and _is_number(high):
        return (low + high) / 2.0
    if _is_number(low):
        return float(low)
    if _is_number(high):
        return float(high)
    return None


def _pct(value, base):
    """`value` as a percentage of `base`, or None when `base` is zero, negative
    or non-numeric -- the same degenerate-base discipline `revenue_change` (#186)
    applies, so a surprise is never a ZeroDivisionError or a sign-flipped ratio."""
    if not _is_number(value) or not _is_number(base) or base <= 0:
        return None
    return value / base * 100.0


def _verdict(actual, low, high):
    """beat / miss / in_range from an actual against a guided range.

    Higher is better -- the metrics guidance is given in (revenue, EPS) are ones
    a company wants to exceed. An actual above the high is a `beat`, below the low
    a `miss`, within (inclusive) `in_range`. A point estimate has low == high, so
    an exact hit is `in_range`."""
    if actual > high:
        return BEAT
    if actual < low:
        return MISS
    return IN_RANGE


@dataclass
class GuidancePeriodJoin:
    """One guided period joined to its reported actual.

    * ``guidances`` -- every guidance record for this ``(metric, period_label)``,
      in filing (accession) order. Never pruned: an initial number, a
      reaffirmation and a revision are all here.
    * ``latest_before_close`` -- the guidance that was in force when the period
      closed (the latest one filed at or before the close year), or the latest
      overall when the period has not closed. This is the number scored.
    * ``actual`` -- the matched reported fact (a `facts_store` fact dict), or None.
    * ``verdict`` -- BEAT / MISS / IN_RANGE / NO_ACTUAL_YET.
    * ``surprise_*`` -- the actual minus the guidance midpoint / low / high, and
      the same as a percentage of each base (None where the base is not a
      positive number). All None when there is no actual yet.
    * ``reason`` -- always populated when ``actual`` is None, saying why.
    """

    cik: str
    metric: str
    period_label: str
    fiscal_year: object
    period_kind: object
    guidances: list
    latest_before_close: object
    actual: object
    verdict: str
    surprise_vs_midpoint: object = None
    surprise_vs_low: object = None
    surprise_vs_high: object = None
    surprise_pct_vs_midpoint: object = None
    surprise_pct_vs_low: object = None
    surprise_pct_vs_high: object = None
    reason: object = None


def _match_actual(store, cik, concepts, year, kind):
    """The reported fact matching a guided ``(year, kind)``, or None.

    Scans the store's latest-per-period facts for every candidate concept and
    keeps the one whose END-date fiscal year equals ``year`` and whose DURATION
    class matches ``kind`` -- annual guidance to an annual-duration fact, quarter
    guidance to a quarter-duration one. For a quarter, the fact's own
    ``fiscal_period`` must also name that quarter. When several qualify (an era
    boundary reports one period under two tags) the one ending latest wins, so the
    result is deterministic."""
    if year is None or kind is None or not concepts:
        return None
    want_annual = kind == _ANNUAL
    best = None
    for concept in concepts:
        for f in store.facts_for(cik, concept=concept):
            if fiscal_year_of(f["period_end"]) != year:
                continue
            cls = classify_period(f["period_start"], f["period_end"])
            if want_annual:
                if cls != "annual":
                    continue
            else:
                if cls != "quarterly":
                    continue
                # The fact must name the same quarter; `fp` is only trusted to
                # disambiguate WHICH quarter, never to decide annual vs quarterly.
                if (f.get("fiscal_period") or "").upper() != kind:
                    continue
            if best is None or f["period_end"] > best["period_end"]:
                best = f
    return best


def _pick_latest_before_close(guidances, close_year):
    """The guidance in force when the period closed.

    ``guidances`` is already in filing (accession) order. When the close year is
    known (an actual exists), take the latest guidance filed AT OR BEFORE it; a
    revision filed after the close does not count as the guided expectation. When
    no close year is known (no actual), the latest overall is the current
    expectation. Falls back to the latest overall if every record post-dates the
    close (so a group is never left without a representative)."""
    if not guidances:
        return None
    if close_year is None:
        return guidances[-1]
    eligible = [g for g in guidances
                if (filing_year(g["accession"]) or close_year) <= close_year]
    return eligible[-1] if eligible else guidances[-1]


def join_guidance_to_actual(store, cik, *, metric_concepts=None):
    """Join every guided period for ``cik`` to its reported actual and score it.

    Returns a list of `GuidancePeriodJoin`, one per ``(metric, period_label)``,
    ordered by metric then period label. Guidance records read from
    ``store.guidance_for(cik)``; actuals matched against ``store``'s facts through
    `concepts_for_metric` + `parse_period_label`. A period with no reported actual
    (not yet filed, or an unmapped metric) scores `no_actual_yet` and is kept.
    """
    cik_padded = cik10(cik)
    records = store.guidance_for(cik_padded)

    # Group by the guided period, preserving the store's filing (accession) order.
    groups = {}
    order = []
    for g in records:
        key = (g["metric"], g["period_label"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(g)

    out = []
    for metric, period_label in order:
        guidances = groups[(metric, period_label)]
        year, kind = parse_period_label(period_label)
        concepts = concepts_for_metric(metric, metric_concepts)
        actual = _match_actual(store, cik_padded, concepts, year, kind)

        close_year = fiscal_year_of(actual["period_end"]) if actual else None
        latest = _pick_latest_before_close(guidances, close_year)

        join = GuidancePeriodJoin(
            cik=cik_padded, metric=metric, period_label=period_label,
            fiscal_year=year, period_kind=kind,
            guidances=guidances, latest_before_close=latest,
            actual=actual, verdict=NO_ACTUAL_YET,
        )

        if actual is None:
            if not concepts:
                join.reason = f"no concept mapping for metric {metric!r}"
            elif year is None or kind is None:
                join.reason = f"period label {period_label!r} did not resolve to a fiscal period"
            else:
                join.reason = (f"no reported actual yet for {metric} "
                               f"{kind} FY{year}")
            out.append(join)
            continue

        low, high = latest["low"], latest["high"]
        actual_val = actual["value"]
        if not (_is_number(low) and _is_number(high) and _is_number(actual_val)):
            join.reason = "guidance range or actual is non-numeric"
            out.append(join)
            continue
        if low > high:
            low, high = high, low  # tolerate a swapped range rather than misjudge

        mid = _midpoint(low, high)
        join.verdict = _verdict(actual_val, low, high)
        join.surprise_vs_midpoint = actual_val - mid
        join.surprise_vs_low = actual_val - low
        join.surprise_vs_high = actual_val - high
        join.surprise_pct_vs_midpoint = _pct(join.surprise_vs_midpoint, mid)
        join.surprise_pct_vs_low = _pct(join.surprise_vs_low, low)
        join.surprise_pct_vs_high = _pct(join.surprise_vs_high, high)
        out.append(join)

    return out


if __name__ == "__main__":
    try:
        from facts_store import FactsStore, FactRecord, GuidanceRecord
    except ImportError:
        from .facts_store import FactsStore, FactRecord, GuidanceRecord

    store = FactsStore(":memory:")
    cik = "0000019617"

    # Reported FY2025 revenue: an annual-duration XBRL fact.
    store.put_facts([FactRecord(
        cik=cik, concept="us-gaap:Revenues", unit="USD",
        period_start="2024-10-01", period_end="2025-09-30",
        fiscal_year=2025, fiscal_period="FY", form="10-K",
        accession="0000019617-25-000200", value=9_300_000_000, source="xbrl",
    )])

    # Guided FY2025 twice: an initial range, then a revision that raised it.
    store.put_guidance([
        GuidanceRecord(cik=cik, metric="revenue", period_label="FY2025",
                       low=8_800_000_000, high=9_000_000_000, basis="non-GAAP",
                       accession="0000019617-24-000100", document="ex99_a.htm",
                       span=(10, 90), provenance="local:qwen", confidence=0.8),
        GuidanceRecord(cik=cik, metric="revenue", period_label="FY2025",
                       low=9_050_000_000, high=9_150_000_000, basis="non-GAAP",
                       accession="0000019617-25-000150", document="ex99_b.htm",
                       span=(10, 90), provenance="local:qwen", confidence=0.85),
        # A period with no reported actual yet -- kept, scored no_actual_yet.
        GuidanceRecord(cik=cik, metric="revenue", period_label="FY2026",
                       low=9_500_000_000, high=9_700_000_000, basis="non-GAAP",
                       accession="0000019617-25-000151", document="ex99_b.htm",
                       span=(10, 90), provenance="local:qwen", confidence=0.85),
    ])

    joins = {j.period_label: j for j in join_guidance_to_actual(store, cik)}

    fy25 = joins["FY2025"]
    print(f"FY2025 verdict={fy25.verdict} "
          f"scored against {fy25.latest_before_close['low']}-"
          f"{fy25.latest_before_close['high']} "
          f"surprise_vs_high={fy25.surprise_vs_high}")
    assert len(fy25.guidances) == 2                 # both retained
    assert fy25.latest_before_close["low"] == 9_050_000_000  # the revision
    assert fy25.verdict == BEAT                     # 9.30bn > 9.15bn high

    fy26 = joins["FY2026"]
    print(f"FY2026 verdict={fy26.verdict} reason={fy26.reason}")
    assert fy26.verdict == NO_ACTUAL_YET and fy26.actual is None

    print("\nguidance_join self-check: PASS")
