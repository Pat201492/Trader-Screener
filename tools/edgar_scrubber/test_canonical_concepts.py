"""
Gate for the canonical concept map (issue #235). Offline throughout: the facts
come from the same checked-in real Apple `companyfacts` fixture that
`test_taxonomy_fields.py` loads (CIK 0000320193); no network, no model.

Runnable two ways, and #235 requires BOTH to pass:
  * as a script  -- python tools/edgar_scrubber/test_canonical_concepts.py
  * under pytest  -- pytest tools/edgar_scrubber/test_canonical_concepts.py

so every check lives in a `test_*` function pytest collects, and the
`__main__` block runs the same functions and prints a PASS line.
"""
import json
from pathlib import Path

import pytest

import canonical_concepts as cc
import frames_ingest
from facts_store import FactRecord, FactsStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
APPLE = "0000320193"
FY23_END = "2023-09-30"


def load_facts():
    """The committed real Apple `companyfacts` fixture -- no network."""
    return json.loads(
        (FIXTURES / "companyfacts_320193.json").read_text(encoding="utf-8"))


def _fact_records(company_facts):
    """Flatten a `companyfacts` payload into `FactRecord`s, INCLUDING instant
    (balance-sheet) facts -- for those the store's convention is period_start ==
    period_end, so `Assets`/`StockholdersEquity` survive into the facts store the
    way duration facts do."""
    cik = str(company_facts["cik"]).zfill(10)
    out = []
    for taxonomy, concepts in company_facts["facts"].items():
        for name, body in concepts.items():
            concept = f"{taxonomy}:{name}"
            for unit, entries in (body.get("units") or {}).items():
                for e in entries:
                    end = e["end"]
                    start = e.get("start") or end  # instant -> start == end
                    out.append(FactRecord(
                        cik=cik, concept=concept, unit=unit,
                        period_start=start, period_end=end,
                        fiscal_year=e.get("fy"), fiscal_period=e.get("fp"),
                        form=e.get("form"), accession=e.get("accn"),
                        value=e.get("val"), source="xbrl",
                    ))
    return out


def apple_facts():
    """Ingest the Apple fixture into an in-memory store and return the list
    `FactsStore.facts_for(cik)` yields -- exactly `resolve`'s input."""
    store = FactsStore(":memory:")
    store.put_facts(_fact_records(load_facts()))
    return store.facts_for(APPLE)


def _fy23(field):
    facts = apple_facts()
    entries = cc.resolve(facts, field)
    return next(e for e in entries if e["period_end"] == FY23_END)


# --------------------------------------------------------------------------- #
# CANONICAL shape
# --------------------------------------------------------------------------- #

def test_canonical_defines_all_concrete_fields():
    expected = {
        "revenue", "sales_revenue", "cost_of_revenue", "gross_profit",
        "operating_income", "net_income", "eps_diluted", "total_assets",
        "total_liabilities", "stockholders_equity", "cash", "assets_current",
        "liabilities_current", "ppe_net", "long_term_debt", "short_term_debt",
        "operating_cash_flow", "capex",
    }
    assert set(cc.CANONICAL) == expected
    assert len(cc.CANONICAL) == 18


def test_every_field_is_a_nonempty_ordered_qualified_list():
    for field, concepts in cc.CANONICAL.items():
        assert isinstance(concepts, tuple), field
        assert concepts, f"{field} must be non-empty"
        assert all(c.startswith("us-gaap:") for c in concepts), field


def test_revenue_is_total_sales_concepts_behind_the_revenues_total():
    # `revenue` = total: the general `Revenues` first, then the sales concepts.
    assert cc.CANONICAL["revenue"][0] == "us-gaap:Revenues"
    assert cc.CANONICAL["revenue"] == (
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap:SalesRevenueNet",
    )


def test_sales_revenue_is_the_sales_concepts_without_the_total():
    # `sales_revenue` = the net-sales concepts only; it is `revenue`'s tail.
    assert cc.CANONICAL["sales_revenue"] == (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap:SalesRevenueNet",
    )
    assert cc.CANONICAL["revenue"][1:] == cc.CANONICAL["sales_revenue"]


def test_other_revenue_is_derived_not_a_canonical_concept():
    assert "other_revenue" in cc.DERIVED
    assert "other_revenue" not in cc.CANONICAL


# --------------------------------------------------------------------------- #
# resolve(): union + collision priority
# --------------------------------------------------------------------------- #

def test_resolve_unions_across_concepts_later_only_period_kept():
    # net_income = (NetIncomeLoss, ProfitLoss). A period reported ONLY under the
    # later-listed ProfitLoss must still be returned (the union).
    facts = [
        {"concept": "us-gaap:NetIncomeLoss", "period_start": "2022-01-01",
         "period_end": "2022-12-31", "value": 100, "accession": "a2"},
        {"concept": "us-gaap:ProfitLoss", "period_start": "2021-01-01",
         "period_end": "2021-12-31", "value": 80, "accession": "a1"},
    ]
    out = cc.resolve(facts, "net_income")
    assert [e["period_end"] for e in out] == ["2021-12-31", "2022-12-31"]
    later_only = next(e for e in out if e["period_end"] == "2021-12-31")
    assert later_only["value"] == 80
    assert later_only["concept"] == "us-gaap:ProfitLoss"


def test_resolve_collision_earlier_listed_concept_wins():
    # The SAME period under two concepts -> the earlier-listed one wins, and
    # `concept` names which was used.
    facts = [
        {"concept": "us-gaap:ProfitLoss", "period_start": "2022-01-01",
         "period_end": "2022-12-31", "value": 999, "accession": "b1"},
        {"concept": "us-gaap:NetIncomeLoss", "period_start": "2022-01-01",
         "period_end": "2022-12-31", "value": 100, "accession": "b2"},
    ]
    out = cc.resolve(facts, "net_income")
    assert len(out) == 1
    assert out[0]["value"] == 100
    assert out[0]["concept"] == "us-gaap:NetIncomeLoss"


def test_resolve_ignores_concepts_the_field_does_not_claim():
    facts = [
        {"concept": "us-gaap:Assets", "period_start": "2022-12-31",
         "period_end": "2022-12-31", "value": 5, "accession": "c1"},
    ]
    assert cc.resolve(facts, "net_income") == []


def test_resolve_unknown_field_raises_keyerror():
    with pytest.raises(KeyError):
        cc.resolve([], "not_a_field")


def test_resolve_known_but_untagged_field_returns_empty_not_error():
    # capex is a valid field, but the Apple fixture never tags it.
    assert cc.resolve(apple_facts(), "capex") == []


# --------------------------------------------------------------------------- #
# resolve(): the large-filer aliases (issue #256, from the #241 comparison)
# --------------------------------------------------------------------------- #
# GOOGL/META/HD tag PP&E under the finance-lease concept, and KO/HD tag debt
# under the capital-lease concepts -- neither was in the map, so the fields did
# not resolve. Synthetic facts shaped like `FactsStore.facts_for` rows.

_PPE_FL = ("us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUse"
           "AssetAfterAccumulatedDepreciationAndAmortization")
_LTD_CL = "us-gaap:LongTermDebtAndCapitalLeaseObligations"
_STD_CL = "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent"


def test_resolve_ppe_net_finance_lease_alias():
    # A filer tagging ONLY the finance-lease PP&E concept resolves, and `concept`
    # names it.
    facts = [
        {"concept": _PPE_FL, "period_start": "2023-12-31",
         "period_end": "2023-12-31", "value": 134345, "accession": "g1"},
    ]
    out = cc.resolve(facts, "ppe_net")
    assert len(out) == 1
    assert out[0]["value"] == 134345
    assert out[0]["concept"] == _PPE_FL


def test_resolve_capital_lease_debt_aliases():
    # A filer tagging ONLY the capital-lease debt concepts resolves for both the
    # long-term and short-term fields.
    facts = [
        {"concept": _LTD_CL, "period_start": "2023-12-31",
         "period_end": "2023-12-31", "value": 38000, "accession": "k1"},
        {"concept": _STD_CL, "period_start": "2023-12-31",
         "period_end": "2023-12-31", "value": 1200, "accession": "k1"},
    ]
    ltd = cc.resolve(facts, "long_term_debt")
    assert len(ltd) == 1
    assert ltd[0]["value"] == 38000
    assert ltd[0]["concept"] == _LTD_CL

    std = cc.resolve(facts, "short_term_debt")
    assert len(std) == 1
    assert std[0]["value"] == 1200
    assert std[0]["concept"] == _STD_CL


def test_resolve_ppe_net_prefers_plain_concept_over_finance_lease():
    # A filer tagging BOTH PropertyPlantAndEquipmentNet and the finance-lease
    # concept for one period still resolves to the plain (earlier-listed) one.
    facts = [
        {"concept": _PPE_FL, "period_start": "2023-12-31",
         "period_end": "2023-12-31", "value": 999, "accession": "h2"},
        {"concept": "us-gaap:PropertyPlantAndEquipmentNet",
         "period_start": "2023-12-31", "period_end": "2023-12-31",
         "value": 42000, "accession": "h1"},
    ]
    out = cc.resolve(facts, "ppe_net")
    assert len(out) == 1
    assert out[0]["value"] == 42000
    assert out[0]["concept"] == "us-gaap:PropertyPlantAndEquipmentNet"


# --------------------------------------------------------------------------- #
# revenue split: total, sales, and derived other_revenue (issue #260)
# --------------------------------------------------------------------------- #
# Walmart FY2025 shape: a `Revenues` total of 680.985B over a net-sales line of
# 674.538B. Synthetic facts shaped like `FactsStore.facts_for` rows.

_RFCC_EX = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
_WMT_START, _WMT_END = "2024-02-01", "2025-01-31"


def _walmart_facts():
    return [
        {"concept": "us-gaap:Revenues", "period_start": _WMT_START,
         "period_end": _WMT_END, "value": 680985000000, "accession": "wmt-10k"},
        {"concept": _RFCC_EX, "period_start": _WMT_START,
         "period_end": _WMT_END, "value": 674538000000, "accession": "wmt-10k"},
    ]


def test_walmart_revenue_is_the_total_and_names_revenues():
    out = cc.resolve(_walmart_facts(), "revenue")
    assert len(out) == 1
    assert out[0]["value"] == 680985000000
    assert out[0]["concept"] == "us-gaap:Revenues"


def test_walmart_sales_revenue_is_the_net_sales_line():
    out = cc.resolve(_walmart_facts(), "sales_revenue")
    assert len(out) == 1
    assert out[0]["value"] == 674538000000
    assert out[0]["concept"] == _RFCC_EX


def test_walmart_other_revenue_is_the_difference_from_different_concepts():
    out = cc.resolve(_walmart_facts(), "other_revenue")
    assert len(out) == 1
    e = out[0]
    assert e["value"] == 6447000000  # 680.985B - 674.538B
    assert e["concept"] == "derived:revenue-sales_revenue"
    assert e["period_start"] == _WMT_START and e["period_end"] == _WMT_END


def test_sales_revenue_net_only_resolves_both_levels_to_the_same_value():
    # A pre-ASC 606 filer tagging only SalesRevenueNet: both revenue and
    # sales_revenue resolve to it, and other_revenue is NOT emitted (same
    # concept on both sides -> no separate total).
    facts = [
        {"concept": "us-gaap:SalesRevenueNet", "period_start": "2016-01-01",
         "period_end": "2016-12-31", "value": 215639000000, "accession": "p1"},
    ]
    assert cc.resolve(facts, "revenue")[0]["value"] == 215639000000
    assert cc.resolve(facts, "sales_revenue")[0]["value"] == 215639000000
    assert cc.resolve(facts, "other_revenue") == []


def test_other_revenue_not_emitted_when_sales_side_missing():
    # Only the `Revenues` total: sales_revenue has nothing, so no difference.
    facts = [
        {"concept": "us-gaap:Revenues", "period_start": _WMT_START,
         "period_end": _WMT_END, "value": 680985000000, "accession": "r1"},
    ]
    assert cc.resolve(facts, "sales_revenue") == []
    assert cc.resolve(facts, "other_revenue") == []


def test_other_revenue_not_emitted_when_difference_is_negative():
    # A sales line larger than the total would make a negative "other" -- drop it.
    facts = [
        {"concept": "us-gaap:Revenues", "period_start": _WMT_START,
         "period_end": _WMT_END, "value": 100, "accession": "n1"},
        {"concept": _RFCC_EX, "period_start": _WMT_START,
         "period_end": _WMT_END, "value": 150, "accession": "n2"},
    ]
    assert cc.resolve(facts, "other_revenue") == []


def test_ingest_frames_refuses_derived_field_with_zero_client_calls():
    class ExplodingClient:
        def frames(self, *a, **k):
            raise AssertionError("frames must not be called for a derived field")

    store = FactsStore(":memory:")
    with pytest.raises(ValueError):
        frames_ingest.ingest_frames(ExplodingClient(), "other_revenue",
                                    "CY2024", store)


# --------------------------------------------------------------------------- #
# resolve() against the real Apple fixture -- FY ending 2023-09-30
# --------------------------------------------------------------------------- #

def test_resolve_apple_fy2023_values():
    assert _fy23("revenue")["value"] == 383285000000
    assert _fy23("operating_income")["value"] == 114301000000
    assert _fy23("net_income")["value"] == 96995000000
    assert _fy23("total_assets")["value"] == 352583000000


def test_resolve_apple_revenue_uses_asc606_concept():
    entry = _fy23("revenue")
    assert entry["concept"] == (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
    assert entry["accession"] == "0000320193-23-000106"


def test_resolve_apple_sales_revenue_equals_revenue_no_other_for_fy2023():
    # Apple tags only RFCC (no separate `Revenues` total) for FY2023, so revenue
    # and sales_revenue are the same value and concept, and other_revenue has no
    # entry for that period.
    rev, sales = _fy23("revenue"), _fy23("sales_revenue")
    assert rev["value"] == 383285000000
    assert sales["value"] == 383285000000
    assert rev["concept"] == sales["concept"]
    other = cc.resolve(apple_facts(), "other_revenue")
    assert not any(e["period_end"] == FY23_END for e in other)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("\ncanonical_concepts gate: PASS")


def test_all_fields_and_input_concepts_cover_derived():
    # Readers list fields with all_fields(); CANONICAL alone hid other_revenue
    # from /standardized and /compare on live data.
    fields = cc.all_fields()
    assert set(cc.CANONICAL) <= set(fields) and "other_revenue" in fields
    assert fields[: len(cc.CANONICAL)] == tuple(cc.CANONICAL)
    assert cc.input_concepts("other_revenue") == set(cc.CANONICAL["revenue"]) | set(cc.CANONICAL["sales_revenue"])
    assert cc.input_concepts("revenue") == set(cc.CANONICAL["revenue"])
    try:
        cc.input_concepts("nope")
        raise AssertionError("unknown field must raise KeyError")
    except KeyError:
        pass
