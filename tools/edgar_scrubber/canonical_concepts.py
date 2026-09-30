"""
Canonical concept map: resolve a filer's XBRL tags to standard fundamentals
fields (issue #235).

Filers tag the same line item under different us-gaap concepts, and the concept
changes with the accounting era, not just between filers. Apple's revenue alone
is `SalesRevenueNet` through FY2017, `Revenues` in FY2018, and
`RevenueFromContractWithCustomerExcludingAssessedTax` from FY2019 -- three
concepts that PARTITION the timeline, not fall back on one another (this is the
finding `revenue_series` (#184) is built on). Every field here has the same
shape: an ordered list of the concepts a filer might use for it, resolved to one
value per period so filers and years can be compared on a single axis.

`CANONICAL` maps each standard field to that ordered concept list. Order is
load-bearing: it is the collision-resolution priority. When two concepts report
the SAME period (an era boundary, or a filer tagging both a specific and a
general concept), the concept earliest in the list wins -- exactly the rule
`revenue_series._resolved_facts` applies. The result across concepts is the
UNION keyed by period, so a period reported only under a later-listed concept is
still kept.

`revenue` is not spelled out here: it is imported from `revenue_series.REVENUE_TAGS`
so the two lists cannot drift (there is one definition of "what counts as
revenue", and it lives with the revenue series that measured it).

`resolve(facts, field)` takes the fact dicts `FactsStore.facts_for(cik)` returns
and gives one entry per period for `field`. Supersession (a period restated
under a later accession) is already collapsed by the store, so resolution here
only settles the cross-concept collision.

stdlib only. Run the self-check:  python tools/edgar_scrubber/canonical_concepts.py
"""

try:  # package import (from .canonical_concepts import ...)
    from .revenue_series import REVENUE_TAGS
except ImportError:  # flat import (import canonical_concepts)
    from revenue_series import REVENUE_TAGS

_TAXONOMY = "us-gaap"


def _q(tag):
    """Taxonomy-qualify a bare concept name: ``Assets`` -> ``us-gaap:Assets``."""
    return f"{_TAXONOMY}:{tag}"


# ── The canonical concept map ─────────────────────────────────────────────────
#
# Each field maps to the concepts a filer might tag it under, MOST SPECIFIC /
# MOST RECENT FIRST -- that order is the collision priority (see `resolve`). Only
# the standard us-gaap concepts are listed; a filer using none of them resolves
# to an empty series, not an error.
#
# `revenue` is built from `revenue_series.REVENUE_TAGS` by import so the revenue
# vocabulary has a single definition; the other 16 fields are enumerated here.
CANONICAL = {
    # Income statement.
    "revenue": tuple(_q(t) for t in REVENUE_TAGS),
    "cost_of_revenue": (
        _q("CostOfGoodsAndServicesSold"),
        _q("CostOfRevenue"),
        _q("CostOfGoodsSold"),
    ),
    "gross_profit": (
        _q("GrossProfit"),
    ),
    "operating_income": (
        _q("OperatingIncomeLoss"),
    ),
    "net_income": (
        _q("NetIncomeLoss"),
        _q("ProfitLoss"),
    ),
    "eps_diluted": (
        _q("EarningsPerShareDiluted"),
        _q("IncomeLossFromContinuingOperationsPerDilutedShare"),
    ),
    # Balance sheet.
    "total_assets": (
        _q("Assets"),
    ),
    "total_liabilities": (
        _q("Liabilities"),
    ),
    "stockholders_equity": (
        _q("StockholdersEquity"),
        _q("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ),
    "cash": (
        _q("CashAndCashEquivalentsAtCarryingValue"),
        _q("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    "assets_current": (
        _q("AssetsCurrent"),
    ),
    "liabilities_current": (
        _q("LiabilitiesCurrent"),
    ),
    "ppe_net": (
        _q("PropertyPlantAndEquipmentNet"),
        _q("PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
    ),
    "long_term_debt": (
        _q("LongTermDebtNoncurrent"),
        _q("LongTermDebt"),
        _q("LongTermDebtAndCapitalLeaseObligations"),
    ),
    "short_term_debt": (
        _q("LongTermDebtCurrent"),
        _q("ShortTermBorrowings"),
        _q("DebtCurrent"),
        _q("LongTermDebtAndCapitalLeaseObligationsCurrent"),
    ),
    # Cash flow.
    "operating_cash_flow": (
        _q("NetCashProvidedByUsedInOperatingActivities"),
        _q("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ),
    "capex": (
        _q("PaymentsToAcquirePropertyPlantAndEquipment"),
        _q("PaymentsToAcquireProductiveAssets"),
    ),
}


def concepts_for(field):
    """The ordered concept list for a standard field. Raises ``KeyError`` for an
    unknown field -- a typo is a bug, not an empty result."""
    return CANONICAL[field]


def resolve(facts, field):
    """Resolve a filer's facts to one entry per period for ``field``.

    ``facts`` is the list of fact dicts ``FactsStore.facts_for(cik)`` returns
    (each carrying ``concept``, ``period_start``, ``period_end``, ``value`` and
    ``accession``). ``field`` names a standard field in ``CANONICAL``.

    The result is the UNION across the field's concepts, keyed by period: a
    period reported only under a later-listed concept is still returned. When two
    concepts cover the SAME period, the concept earliest in the field's list wins
    (the era-boundary collision), and ``concept`` names which one supplied the
    value. Entries are sorted by ``period_end`` then ``period_start``.

    Raises ``KeyError`` for an unknown ``field``. A known field the filer never
    tags returns an empty list, not an error."""
    concepts = CANONICAL[field]  # KeyError on an unknown field, by design.
    priority = {concept: i for i, concept in enumerate(concepts)}

    by_period = {}
    for f in facts:
        rank = priority.get(f["concept"])
        if rank is None:
            continue  # a concept this field does not claim
        key = (f["period_start"], f["period_end"])
        cur = by_period.get(key)
        # An earlier-listed concept (lower rank) wins the same period.
        if cur is None or rank < priority[cur["concept"]]:
            by_period[key] = f

    entries = [
        {
            "period_start": f["period_start"],
            "period_end": f["period_end"],
            "value": f["value"],
            "concept": f["concept"],
            "accession": f["accession"],
        }
        for f in by_period.values()
    ]
    # Sort with a sentinel for instant facts (period_start is None).
    entries.sort(key=lambda e: (e["period_end"], e["period_start"] or ""))
    return entries


if __name__ == "__main__":
    # Self-check against synthetic facts -- no store, no files, no network.
    # Two concepts for net_income: one covers a period the other does not
    # (union), and both cover a shared period (collision -> earlier wins).
    facts = [
        {"concept": "us-gaap:NetIncomeLoss", "period_start": "2022-01-01",
         "period_end": "2022-12-31", "value": 100, "accession": "a1"},
        # Shared period, also under the later-listed ProfitLoss -> must lose.
        {"concept": "us-gaap:ProfitLoss", "period_start": "2022-01-01",
         "period_end": "2022-12-31", "value": 999, "accession": "a2"},
        # A period ONLY the later-listed concept reports -> still kept (union).
        {"concept": "us-gaap:ProfitLoss", "period_start": "2021-01-01",
         "period_end": "2021-12-31", "value": 80, "accession": "a0"},
        # A concept net_income does not claim -> ignored.
        {"concept": "us-gaap:Assets", "period_start": None,
         "period_end": "2022-12-31", "value": 5, "accession": "a1"},
    ]

    out = resolve(facts, "net_income")
    assert [e["period_end"] for e in out] == ["2021-12-31", "2022-12-31"]
    shared = next(e for e in out if e["period_end"] == "2022-12-31")
    assert shared["value"] == 100 and shared["concept"] == "us-gaap:NetIncomeLoss"
    union = next(e for e in out if e["period_end"] == "2021-12-31")
    assert union["value"] == 80 and union["concept"] == "us-gaap:ProfitLoss"

    assert resolve(facts, "capex") == []  # a field the filer never tags
    try:
        resolve(facts, "nope")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown field must raise KeyError")

    # revenue must be REVENUE_TAGS, qualified, in order.
    assert CANONICAL["revenue"] == tuple(f"us-gaap:{t}" for t in REVENUE_TAGS)
    assert len(CANONICAL) == 17
    assert all(CANONICAL[f] for f in CANONICAL)
    assert all(c.startswith("us-gaap:") for cs in CANONICAL.values() for c in cs)

    print("canonical_concepts self-check: PASS")
