"""
Gate for the Magic Formula inputs comparison (issue #237). Offline throughout:
the facts come from the committed Apple `companyfacts` fixture (CIK 0000320193),
the pipeline side from a `pipeline-mock`-style JSON written to a temp file. No
network, no shared-data write.

Runnable two ways, both required to pass:
  * as a script  -- python tools/edgar_scrubber/test_magic_inputs.py
  * under pytest  -- pytest tools/edgar_scrubber/test_magic_inputs.py
"""
import io
import json
import tempfile
from pathlib import Path

import pytest

import magic_inputs as mi
from facts_store import FactRecord, FactsStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
APPLE = "0000320193"


def _fact_records(company_facts):
    """Flatten a `companyfacts` payload into `FactRecord`s, instants included
    (period_start == period_end), matching the store's balance-sheet convention."""
    cik = str(company_facts["cik"]).zfill(10)
    out = []
    for taxonomy, concepts in company_facts["facts"].items():
        for name, body in concepts.items():
            concept = f"{taxonomy}:{name}"
            for unit, entries in (body.get("units") or {}).items():
                for e in entries:
                    end = e["end"]
                    out.append(FactRecord(
                        cik=cik, concept=concept, unit=unit,
                        period_start=e.get("start") or end, period_end=end,
                        fiscal_year=e.get("fy"), fiscal_period=e.get("fp"),
                        form=e.get("form"), accession=e.get("accn"),
                        value=e.get("val"), source="xbrl",
                    ))
    return out


def apple_store():
    store = FactsStore(":memory:")
    company_facts = json.loads(
        (FIXTURES / "companyfacts_320193.json").read_text(encoding="utf-8"))
    store.put_facts(_fact_records(company_facts))
    return store


# --------------------------------------------------------------------------- #
# compute(): exact formulas, to 4 decimal places
# --------------------------------------------------------------------------- #

def test_compute_follows_greenblatt_formulas_to_4dp():
    facts = {
        "operating_income": 100.0,
        "assets_current": 300.0,
        "liabilities_current": 100.0,
        "ppe_net": 50.0,
        "long_term_debt": 200.0,
        "short_term_debt": 50.0,
        "cash": 100.0,
    }
    res = mi.compute(facts, mkt_cap=1000.0)
    # ROC = 100 / ((300 - 100) + 50) = 100 / 250 = 40%
    assert round(res["roc_greenblatt"], 4) == 40.0000
    # EV = 1000 + 200 + 50 - 100 = 1150 ; yield = 100 / 1150 = 8.695652...%
    assert round(res["ebit_ev_yield"], 4) == round(100 / 1150 * 100, 4)
    assert round(res["ebit_ev_yield"], 4) == 8.6957
    assert res["missing"] == []


def test_missing_ppe_nulls_roc_only_yield_survives():
    facts = {
        "operating_income": 100.0,
        "assets_current": 300.0,
        "liabilities_current": 100.0,
        # ppe_net absent
        "long_term_debt": 200.0,
        "short_term_debt": 50.0,
        "cash": 100.0,
    }
    res = mi.compute(facts, mkt_cap=1000.0)
    assert res["roc_greenblatt"] is None
    assert "ppe_net" in res["missing"]
    assert res["ebit_ev_yield"] is not None  # its inputs are all present


def test_never_substitutes_zero_for_missing():
    # A missing operating_income must NOT be read as EBIT 0 (which would make
    # both metrics a spurious 0.0); both go None instead.
    res = mi.compute({"assets_current": 300.0, "liabilities_current": 100.0,
                      "ppe_net": 50.0, "long_term_debt": 200.0,
                      "short_term_debt": 50.0, "cash": 100.0}, mkt_cap=1000.0)
    assert res["roc_greenblatt"] is None and res["ebit_ev_yield"] is None
    assert "operating_income" in res["missing"]


def test_missing_mkt_cap_nulls_only_yield():
    facts = {
        "operating_income": 100.0, "assets_current": 300.0,
        "liabilities_current": 100.0, "ppe_net": 50.0,
        "long_term_debt": 200.0, "short_term_debt": 50.0, "cash": 100.0,
    }
    res = mi.compute(facts, mkt_cap=None)
    assert res["roc_greenblatt"] is not None
    assert res["ebit_ev_yield"] is None
    assert "mkt_cap" in res["missing"]


# --------------------------------------------------------------------------- #
# CLI: reads /api/stocks-shaped local JSON, prints local/pipeline/delta
# --------------------------------------------------------------------------- #

def _write_pipeline_json(dirpath):
    payload = {"stocks": [
        {"ticker": "AAPL", "mkt_cap": 2800.0, "roic": 41.0, "roc_greenblatt": 55.5, "ebit_ev_yield": 6.1},
        {"ticker": "MSFT", "mkt_cap": 3100.0, "roic": 22.0, "roc_greenblatt": 30.2, "ebit_ev_yield": 4.4},
    ], "total": 2}
    p = Path(dirpath) / "stocks.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_cli_reads_local_json_and_prints_per_ticker():
    with tempfile.TemporaryDirectory() as d:
        pipeline = _write_pipeline_json(d)
        store = apple_store()
        buf = io.StringIO()
        rows = mi.run(str(pipeline), ["AAPL", "MSFT"], store=store,
                      ticker_ciks={"AAPL": APPLE, "MSFT": "9999999999"}, out=buf)
        store.close()

    text = buf.getvalue()
    # A header plus one line per ticker.
    assert "AAPL" in text and "MSFT" in text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 3  # header + 2 tickers

    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    # Pipeline values are read straight from the JSON row.
    assert aapl["pipeline_roc_greenblatt"] == 55.5
    # ROC compares against the pipeline's roc_greenblatt, never its NOPAT-based
    # roic (41.0), which is a different formula (#248).
    assert "pipeline_roic" not in aapl
    assert aapl["pipeline_ebit_ev_yield"] == 6.1
    # The Apple fixture lacks the balance-sheet inputs, so local ROC is None and
    # the missing fields are named -- never a fabricated zero.
    assert aapl["local_roc_greenblatt"] is None
    assert "ppe_net" in aapl["missing"]


def test_cli_load_pipeline_accepts_bare_list_and_envelope():
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / "env.json"
        env.write_text(json.dumps({"stocks": [{"ticker": "AAPL", "mkt_cap": 1}]}),
                       encoding="utf-8")
        bare = Path(d) / "bare.json"
        bare.write_text(json.dumps([{"ticker": "AAPL", "mkt_cap": 1}]),
                        encoding="utf-8")
        assert "AAPL" in mi.load_pipeline_rows(str(env))
        assert "AAPL" in mi.load_pipeline_rows(str(bare))


# --------------------------------------------------------------------------- #
# Local only: no write path outside the scrubber's store root
# --------------------------------------------------------------------------- #

def test_module_writes_nothing_to_disk():
    with tempfile.TemporaryDirectory() as d:
        pipeline = _write_pipeline_json(d)
        before = set(Path(d).iterdir())
        store = apple_store()  # :memory: -- no file
        mi.run(str(pipeline), ["AAPL"], store=store,
               ticker_ciks={"AAPL": APPLE}, out=io.StringIO())
        store.close()
        after = set(Path(d).iterdir())
    # The run created no new file: its only output is stdout.
    assert before == after


def test_facts_store_refuses_a_pipeline_write_path():
    # The module reuses FactsStore, whose ownership guard forbids opening a
    # pipeline-shaped path -- so this tool can never become a second writer.
    from output_store import OwnershipError
    with pytest.raises(OwnershipError):
        FactsStore("/data/stock-data-pipeline/facts.sqlite")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("\nmagic_inputs gate: PASS")
