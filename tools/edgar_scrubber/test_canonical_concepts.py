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
from facts_store import FactRecord, FactsStore
from revenue_series import REVENUE_TAGS

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

def test_canonical_defines_all_seventeen_fields():
    expected = {
        "revenue", "cost_of_revenue", "gross_profit", "operating_income",
        "net_income", "eps_diluted", "total_assets", "total_liabilities",
        "stockholders_equity", "cash", "assets_current", "liabilities_current",
        "ppe_net", "long_term_debt", "short_term_debt", "operating_cash_flow",
        "capex",
    }
    assert set(cc.CANONICAL) == expected
    assert len(cc.CANONICAL) == 17


def test_every_field_is_a_nonempty_ordered_qualified_list():
    for field, concepts in cc.CANONICAL.items():
        assert isinstance(concepts, tuple), field
        assert concepts, f"{field} must be non-empty"
        assert all(c.startswith("us-gaap:") for c in concepts), field


def test_revenue_is_built_from_revenue_tags_by_import():
    # Must MATCH REVENUE_TAGS, qualified and in order -- so the two cannot drift.
    assert cc.CANONICAL["revenue"] == tuple(f"us-gaap:{t}" for t in REVENUE_TAGS)


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("\ncanonical_concepts gate: PASS")
