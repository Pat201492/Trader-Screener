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

Revenue is split into two levels on purpose (issue #260). A filer that reports
both net sales and a wider total -- Walmart FY2025 tags `Revenues` 680.985B
(net sales 674.538B + 6.447B membership and other income) -- used to collapse to
one `revenue` value, whichever concept came first. Now:

  * `revenue` means TOTAL revenue: `Revenues` first, then the ASC-606 sales
    concepts. It is its own tuple here, no longer imported from
    `revenue_series.REVENUE_TAGS` (that series measures a single revenue line and
    its priority is era-partition, not total-vs-sales; the two definitions are
    allowed to differ now, so neither is imported into the other).
  * `sales_revenue` means the net-sales line only (the ASC-606 concepts and
    pre-606 `SalesRevenueNet`), WITHOUT the general `Revenues` total.
  * `other_revenue` is DERIVED: `revenue - sales_revenue` for each period where
    both resolve from DIFFERENT concepts (see `DERIVED` / `resolve`).

`resolve(facts, field)` takes the fact dicts `FactsStore.facts_for(cik)` returns
and gives one entry per period for `field`. Supersession (a period restated
under a later accession) is already collapsed by the store, so resolution here
only settles the cross-concept collision. A derived field is computed from the
concrete fields it is built on, not read from any concept.

stdlib only. Run the self-check:  python tools/edgar_scrubber/canonical_concepts.py
"""

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
# The two sales concepts (ASC 606) and the pre-606 net-sales concept, shared by
# `revenue` (as its tail, behind the general `Revenues` total) and by
# `sales_revenue` (the whole of it). Most specific / most recent first.
_SALES_CONCEPTS = (
    _q("RevenueFromContractWithCustomerExcludingAssessedTax"),
    _q("RevenueFromContractWithCustomerIncludingAssessedTax"),
    _q("SalesRevenueNet"),
)

CANONICAL = {
    # Income statement.
    # TOTAL revenue: the general `Revenues` total wins over any sales line, so a
    # filer tagging both (Walmart: Revenues 680.985B and RFCC 674.538B) resolves
    # `revenue` to the total. A filer with no `Revenues` tag falls through to the
    # sales concepts, so `revenue` is still populated.
    "revenue": (_q("Revenues"),) + _SALES_CONCEPTS,
    # NET SALES only -- the sales concepts, without the `Revenues` total.
    "sales_revenue": _SALES_CONCEPTS,
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


# ── Derived fields ────────────────────────────────────────────────────────────
#
# A derived field is COMPUTED from concrete fields, not tagged by any filer, so
# it has no entry in `CANONICAL` and no concept to fetch from `companyconcept` or
# frames. `resolve` computes it; `frames_ingest` refuses it. Each derived entry
# names its origin in `concept` as "derived:<a>-<b>".
OTHER_REVENUE_CONCEPT = "derived:revenue-sales_revenue"
DERIVED = frozenset({"other_revenue"})
# The concrete fields each derived field is computed from.
DERIVED_FROM = {"other_revenue": ("revenue", "sales_revenue")}


def concepts_for(field):
    """The ordered concept list for a standard field. Raises ``KeyError`` for an
    unknown field -- a typo is a bug, not an empty result. A derived field has no
    concept list and raises too (it is not in ``CANONICAL``)."""
    return CANONICAL[field]


def all_fields():
    """Every standard field a reader can ask for: the concrete ones in
    ``CANONICAL`` order, then the derived ones. Readers that list fields must
    use this, not ``CANONICAL`` alone, or derived fields silently never show."""
    return (*CANONICAL, *sorted(DERIVED))


def input_concepts(field):
    """The set of concepts ``resolve(facts, field)`` reads -- a derived field's
    is the union of its inputs'. Raises ``KeyError`` for an unknown field."""
    if field in DERIVED:
        return {c for f in DERIVED_FROM[field] for c in CANONICAL[f]}
    return set(CANONICAL[field])


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

    A DERIVED field (``field in DERIVED``) is computed from the concrete fields
    it is built on, not read from any concept.

    Raises ``KeyError`` for an unknown ``field``. A known field the filer never
    tags returns an empty list, not an error."""
    if field in DERIVED:
        return _resolve_derived(facts, field)
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


def _resolve_derived(facts, field):
    """Compute a derived field from its concrete inputs. Only ``other_revenue``
    exists today: ``revenue - sales_revenue`` per period."""
    if field == "other_revenue":
        return _resolve_other_revenue(facts)
    raise KeyError(field)  # named in DERIVED but not implemented -- a bug.


def _resolve_other_revenue(facts):
    """``revenue - sales_revenue`` for each period where BOTH resolve and they
    resolve from DIFFERENT concepts. The difference is the revenue a filer
    reports in its total (`Revenues`) beyond its net-sales line -- Walmart's
    membership and other income. Not emitted when either side is missing, when
    both resolve from the SAME concept (a filer with no separate total -- the
    difference is a meaningless zero), or when the difference is negative.

    `concept` is ``derived:revenue-sales_revenue``; `accession` carries the
    revenue fact's accession, the authoritative total the difference is read
    against. Entries are sorted like `resolve`'s."""
    sales = {(e["period_start"], e["period_end"]): e
             for e in resolve(facts, "sales_revenue")}

    entries = []
    for r in resolve(facts, "revenue"):
        key = (r["period_start"], r["period_end"])
        s = sales.get(key)
        if s is None:
            continue  # sales_revenue missing for this period
        if r["concept"] == s["concept"]:
            continue  # same concept -> no separate total, difference is 0
        diff = r["value"] - s["value"]
        if diff < 0:
            continue  # a negative "other" is not a real residual; drop it
        entries.append({
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "value": diff,
            "concept": OTHER_REVENUE_CONCEPT,
            "accession": r["accession"],
        })

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

    # revenue = total (Revenues first, then the sales concepts); sales_revenue =
    # the sales concepts only, so revenue is sales_revenue behind the total.
    assert CANONICAL["revenue"][0] == "us-gaap:Revenues"
    assert CANONICAL["revenue"][1:] == CANONICAL["sales_revenue"]
    assert CANONICAL["sales_revenue"] == (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap:SalesRevenueNet",
    )
    assert len(CANONICAL) == 18
    assert all(CANONICAL[f] for f in CANONICAL)
    assert all(c.startswith("us-gaap:") for cs in CANONICAL.values() for c in cs)
    assert "other_revenue" in DERIVED and "other_revenue" not in CANONICAL

    # other_revenue: Revenues total over a sales line -> the difference, from
    # different concepts. Same concept, missing side, negative -> not emitted.
    rev_facts = [
        {"concept": "us-gaap:Revenues", "period_start": "2024-02-01",
         "period_end": "2025-01-31", "value": 680985, "accession": "w1"},
        {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
         "period_start": "2024-02-01", "period_end": "2025-01-31",
         "value": 674538, "accession": "w2"},
    ]
    other = resolve(rev_facts, "other_revenue")
    assert len(other) == 1
    assert other[0]["value"] == 680985 - 674538
    assert other[0]["concept"] == "derived:revenue-sales_revenue"

    # One concept only -> revenue and sales_revenue share it, no other_revenue.
    one = [{"concept": "us-gaap:SalesRevenueNet", "period_start": "2016-01-01",
            "period_end": "2016-12-31", "value": 500, "accession": "o1"}]
    assert resolve(one, "revenue")[0]["value"] == 500
    assert resolve(one, "sales_revenue")[0]["value"] == 500
    assert resolve(one, "other_revenue") == []

    print("canonical_concepts self-check: PASS")
