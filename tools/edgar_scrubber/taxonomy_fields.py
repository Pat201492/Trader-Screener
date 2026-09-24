"""
Derive a filing's fields from its own XBRL tags (issue #210).

For a form whose facts are XBRL-tagged, the fields a scrubber pulls should be
what the FILING ITSELF tags -- "relevant as determined by the SEC" -- not a
hand-authored list someone maintains. A 424B2 needs a written field spec
because its terms are prose; a 10-K does not: every number in its financial
statements carries an SEC-taxonomy tag, so the filing states both what each
value IS and where it sits.

The tags a filer actually uses are DISCOVERABLE FROM THE FILING, not guessable
in advance -- measured on Apple (CIK 0000320193) on 2026-09-24, a single
`companyconcept` pull returned 338 revenue facts across three era-partitioned
tags. So the fields are read off the filing's own facts here, never authored.

What is NOT in `companyfacts` is the statement each concept belongs to: the
income-statement / balance-sheet / cash-flow grouping lives in the us-gaap
taxonomy's presentation linkbases, not in the facts payload. `STATEMENT_AREAS`
carries that grouping -- it is the taxonomy's, keyed by concept, applied to
whatever concepts the filing turns out to tag. A concept the grouping does not
cover is returned under an explicit "ungrouped" bucket, never dropped.

Reads `companyfacts` through the existing client (`EdgarClient.company_facts`);
no new HTTP path. stdlib only. Run the self-check:

    python tools/edgar_scrubber/taxonomy_fields.py
"""

from dataclasses import dataclass, field as dc_field


# ── The taxonomy's statement grouping ────────────────────────────────────────
#
# The us-gaap taxonomy assigns each concept to a financial statement through its
# presentation linkbases; `companyfacts` does not carry that assignment, so it
# is reproduced here, keyed by the bare concept name (no `us-gaap:` prefix). The
# groups are the taxonomy's standard statement areas; only the common line items
# are enumerated -- anything a filing tags that is not listed falls to the
# explicit "ungrouped" bucket rather than being invented into an area.
STATEMENT_AREAS = {
    "income_statement": {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "GrossProfit",
        "OperatingExpenses",
        "ResearchAndDevelopmentExpense",
        "SellingGeneralAndAdministrativeExpense",
        "OperatingIncomeLoss",
        "NonoperatingIncomeExpense",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeTaxExpenseBenefit",
        "NetIncomeLoss",
        "EarningsPerShareBasic",
        "EarningsPerShareDiluted",
    },
    "balance_sheet": {
        "CashAndCashEquivalentsAtCarryingValue",
        "AssetsCurrent",
        "Assets",
        "PropertyPlantAndEquipmentNet",
        "LiabilitiesCurrent",
        "Liabilities",
        "LiabilitiesAndStockholdersEquity",
        "CommonStockValue",
        "RetainedEarningsAccumulatedDeficit",
        "StockholdersEquity",
    },
    "cash_flow": {
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "DepreciationDepletionAndAmortization",
        "ShareBasedCompensation",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsOfDividends",
    },
}

# The bucket a concept lands in when the taxonomy's grouping above does not
# cover it -- returned explicitly, never silently dropped.
UNGROUPED = "ungrouped"

# The stable order groups are reported in; `UNGROUPED` always sorts last.
AREA_ORDER = ("income_statement", "balance_sheet", "cash_flow", UNGROUPED)

# Concept -> statement area, inverted from STATEMENT_AREAS once at import.
_AREA_OF = {
    concept: area
    for area, concepts in STATEMENT_AREAS.items()
    for concept in concepts
}


def area_of(concept):
    """The statement area a concept belongs to, or ``UNGROUPED`` if the
    taxonomy's grouping does not cover it. Accepts either a bare concept name
    (``NetIncomeLoss``) or a taxonomy-qualified one (``us-gaap:NetIncomeLoss``);
    only us-gaap concepts are grouped, everything else is ungrouped."""
    taxonomy, _, bare = concept.rpartition(":")
    if taxonomy and taxonomy != "us-gaap":
        return UNGROUPED
    return _AREA_OF.get(bare, UNGROUPED)


# ── Period + occurrence shapes ───────────────────────────────────────────────

def _period_of(entry):
    """The period a fact covers, as a ``(start, end)`` pair. A duration fact
    (income-statement / cash-flow line) carries both a `start` and an `end`; an
    instant fact (balance-sheet point) carries only an `end`, so its `start` is
    None. Never guesses a start the filing did not state."""
    return (entry.get("start"), entry["end"])


@dataclass
class TaxonomyField:
    """One concept a filing tags, with everything the filing states about it:
    its taxonomy-qualified name, the taxonomy's human label, and every
    (unit, period, value) occurrence it reported. `area` is the statement group
    the concept belongs to. `filing_count` is only meaningful for a form-level
    union (`fields_for_form`); for a single filing it is 1."""

    concept: str
    label: str
    area: str
    occurrences: list = dc_field(default_factory=list)
    filing_count: int = 1

    @property
    def units(self):
        """The distinct units this concept reported in (usually one, e.g. USD)."""
        return sorted({o["unit"] for o in self.occurrences})

    @property
    def periods(self):
        """The distinct periods this concept reported, as (start, end) pairs."""
        return sorted({(o["period_start"], o["period_end"]) for o in self.occurrences})


@dataclass
class FieldsResult:
    """Concepts grouped by statement area, plus the reason the result looks the
    way it does. `reason` is ALWAYS populated -- most importantly when the result
    is empty (a company with no XBRL facts), so a caller can tell "no facts" from
    "not yet fetched" without the call ever raising. `groups` always carries all
    four area keys, each a (possibly empty) list."""

    groups: dict
    reason: str

    def __iter__(self):
        """Iterate every field across all areas, in `AREA_ORDER`."""
        for area in AREA_ORDER:
            yield from self.groups[area]

    def __len__(self):
        return sum(len(self.groups[a]) for a in AREA_ORDER)

    def area(self, name):
        """The fields in one statement area (`[]` if none tagged)."""
        return self.groups[name]

    def concepts(self):
        """Every concept name in the result, across all areas."""
        return [f.concept for f in self]


def _empty_groups():
    return {area: [] for area in AREA_ORDER}


def _facts_block(company_facts):
    """The ``facts`` mapping of a `companyfacts` payload, or ``None`` when it is
    absent or empty -- the signal for "no XBRL facts", handled by callers as an
    empty result with a reason rather than an exception."""
    facts = (company_facts or {}).get("facts")
    if not facts:
        return None
    # A payload whose taxonomies are all empty is as good as no facts.
    if not any(facts.get(tax) for tax in facts):
        return None
    return facts


def _iter_concepts(facts):
    """Yield ``(qualified_concept, label, unit, entry)`` for every fact in the
    payload, across every taxonomy. The concept is taxonomy-qualified
    (``us-gaap:NetIncomeLoss``) exactly as elsewhere in this package."""
    for taxonomy, concepts in facts.items():
        for name, body in concepts.items():
            qualified = f"{taxonomy}:{name}"
            label = body.get("label") or name
            for unit, entries in (body.get("units") or {}).items():
                for entry in entries:
                    yield qualified, label, unit, entry


# ── Filing-level: the fields ONE filing tags ─────────────────────────────────

def fields_for_filing(company_facts, form, accession):
    """The taxonomy concepts a single filing actually reports, grouped by
    statement area.

    A concept is included only if the filing (matched by both `form` and
    `accession`) tags at least one fact under it -- never a concept the filing
    does not tag. Each returned `TaxonomyField` carries the taxonomy's label and
    every (unit, period, value) the filing stated for that concept. Concepts the
    taxonomy's grouping does not cover land in the explicit ``UNGROUPED`` bucket.

    A payload with no XBRL facts returns an empty `FieldsResult` with a stated
    reason and never raises."""
    facts = _facts_block(company_facts)
    if facts is None:
        return FieldsResult(_empty_groups(),
                            f"no XBRL facts in payload for form {form} "
                            f"accession {accession}")

    by_concept = {}
    for concept, label, unit, entry in _iter_concepts(facts):
        if entry.get("form") != form or entry.get("accn") != accession:
            continue
        start, end = _period_of(entry)
        field = by_concept.get(concept)
        if field is None:
            field = TaxonomyField(concept=concept, label=label,
                                  area=area_of(concept))
            by_concept[concept] = field
        field.occurrences.append({
            "unit": unit,
            "period_start": start,
            "period_end": end,
            "value": entry.get("val"),
            "fiscal_year": entry.get("fy"),
            "fiscal_period": entry.get("fp"),
        })

    groups = _group(by_concept.values())
    n = sum(len(v) for v in groups.values())
    if n == 0:
        reason = (f"payload has facts but none tagged by form {form} "
                  f"accession {accession}")
    else:
        reason = (f"{n} concept(s) tagged by form {form} accession {accession} "
                  f"across {sum(1 for a in groups if groups[a])} area(s)")
    return FieldsResult(groups, reason)


# ── Form-level: the union across a form's filings ────────────────────────────

def fields_for_form(company_facts, form):
    """The union of concepts across every filing of `form`, each carrying the
    count of filings it appeared in.

    Union so the full vocabulary of the form is seen; the per-concept
    `filing_count` (distinct accessions of this form that tagged the concept) is
    what tells a rare one-off tag from a standing line item. Occurrences and
    label are merged across those filings. Grouped and empty-safe exactly like
    `fields_for_filing`."""
    facts = _facts_block(company_facts)
    if facts is None:
        return FieldsResult(_empty_groups(), f"no XBRL facts in payload for form {form}")

    by_concept = {}
    accns = {}  # concept -> set of accessions of this form that tagged it
    for concept, label, unit, entry in _iter_concepts(facts):
        if entry.get("form") != form:
            continue
        start, end = _period_of(entry)
        field = by_concept.get(concept)
        if field is None:
            field = TaxonomyField(concept=concept, label=label,
                                  area=area_of(concept), filing_count=0)
            by_concept[concept] = field
            accns[concept] = set()
        field.occurrences.append({
            "unit": unit,
            "period_start": start,
            "period_end": end,
            "value": entry.get("val"),
            "fiscal_year": entry.get("fy"),
            "fiscal_period": entry.get("fp"),
        })
        if entry.get("accn"):
            accns[concept].add(entry["accn"])

    for concept, field in by_concept.items():
        field.filing_count = len(accns[concept])

    groups = _group(by_concept.values())
    n = sum(len(v) for v in groups.values())
    reason = (f"{n} concept(s) across {len(set().union(*accns.values())) if accns else 0} "
              f"filing(s) of form {form}") if n else f"no facts tagged by form {form}"
    return FieldsResult(groups, reason)


# ── Grouping ─────────────────────────────────────────────────────────────────

def _group(fields):
    """Sort fields into the statement-area buckets, each area's fields ordered
    by concept name for a stable result. Every area key is present."""
    groups = _empty_groups()
    for field in fields:
        groups[field.area].append(field)
    for area in groups:
        groups[area].sort(key=lambda f: f.concept)
    return groups


# ── Convenience: fetch + derive in one call ──────────────────────────────────

def fields_from_client(client, cik, form, accession=None):
    """Fetch `companyfacts` for `cik` through the existing client (no new HTTP
    path) and derive the fields. With `accession`, the single filing's fields;
    without, the union across the form. Split out so the pure derivation above
    stays offline-testable against a fixture."""
    company_facts = client.company_facts(cik)
    if accession is not None:
        return fields_for_filing(company_facts, form, accession)
    return fields_for_form(company_facts, form)


if __name__ == "__main__":
    import json
    from pathlib import Path

    fixture = (Path(__file__).resolve().parent / "fixtures"
               / "companyfacts_320193.json")
    company_facts = json.loads(fixture.read_text(encoding="utf-8"))

    fy23 = "0000320193-23-000106"
    filing = fields_for_filing(company_facts, "10-K", fy23)
    print(f"10-K {fy23}: {filing.reason}")
    for area in AREA_ORDER:
        for f in filing.area(area):
            print(f"  [{area:16}] {f.concept}  ({', '.join(f.units)})")

    print()
    form = fields_for_form(company_facts, "10-K")
    print(f"10-K union: {form.reason}")
    for f in form:
        print(f"  {f.concept}: seen in {f.filing_count} filing(s)")

    empty = fields_for_filing({"cik": 1, "facts": {}}, "10-K", "x")
    print(f"\nempty: len={len(empty)} reason={empty.reason!r}")

    # A revenue concept must land in the income statement, never ungrouped.
    rev = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert rev in [f.concept for f in filing.area("income_statement")]
    assert "us-gaap:Assets" in [f.concept for f in filing.area("balance_sheet")]
    assert "us-gaap:StandardProductWarrantyAccrual" in \
        [f.concept for f in filing.area(UNGROUPED)]
    assert len(empty) == 0 and empty.reason
    print("\ntaxonomy_fields self-check: PASS")
