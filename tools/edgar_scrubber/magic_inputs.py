"""
Magic Formula inputs from standardized XBRL, compared against the pipeline
(issue #237).

The screener's Magic Formula ranks on `roc_greenblatt` and `ebit_ev_yield`
(#248), and Stock-Data-Pipeline is the ONLY writer of those (ARCHITECTURE.md, #109). Before any XBRL-derived field is
allowed to graduate into the pipeline, we need to know how far a filer-tagged
computation drifts from the pipeline's own numbers. This module is that
measurement: it recomputes Greenblatt's two Magic Formula metrics locally from
the canonical facts (#235) and prints local / pipeline / delta side by side.

Exploratory and LOCAL ONLY. It opens no pipeline write path: its only output is
stdout, and the only store it touches is the scrubber's local facts store
(read). It never writes shared data.

Greenblatt's definitions, exactly as `compute` implements them:

    EBIT           = operating_income
    invested_capital = (assets_current - liabilities_current) + ppe_net
    ROC            = EBIT / invested_capital
    EV             = mkt_cap + long_term_debt + short_term_debt - cash
    earnings_yield = EBIT / EV

Both ratios are expressed in PERCENT, to match the pipeline contract in
`pipeline-mock/sample_data.py` (its `roc_greenblatt` and `ebit_ev_yield` are
percentages, e.g. 24.3). ROC is compared against the pipeline's
`roc_greenblatt`, NOT its `roic`: the pipeline's `roic` is NOPAT / invested
capital, a different formula, so a delta against it would measure the
definition gap rather than XBRL reliability (#248). A missing input yields `None` for the affected metric -- never a
zero substituted for a number the filer did not report -- and every missing
input is named in `missing`.

stdlib only. Run the self-check:  python tools/edgar_scrubber/magic_inputs.py
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

try:  # package import
    from . import canonical_concepts as cc
    from .facts_store import FactsStore
except ImportError:  # flat import (script / pytest via conftest)
    import canonical_concepts as cc
    from facts_store import FactsStore


# The canonical fields each metric consumes. `mkt_cap` is a caller-supplied
# input (from the pipeline row), not a canonical XBRL field, so it is tracked
# separately below.
_ROC_FIELDS = ("operating_income", "assets_current", "liabilities_current", "ppe_net")
_EV_FIELDS = ("operating_income", "long_term_debt", "short_term_debt", "cash")

# Concepts the two provable-derivation rules (#257, decided on #241) read
# directly, below the canonical-field layer. Each rule fires ONLY when the
# filer's own tags prove the value -- never a guessed zero.
_LONG_TERM_DEBT = "us-gaap:LongTermDebt"
_LONG_TERM_DEBT_NONCURRENT = "us-gaap:LongTermDebtNoncurrent"
_PRETAX_INCOME = (
    "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
    "ExtraordinaryItemsNoncontrollingInterest")
_INTEREST_NONOPERATING = "us-gaap:InterestExpenseNonoperating"

# Ticker -> CIK for the CLI's local computation. A stub roster (this is an
# exploratory tool); unknown tickers simply resolve to no local facts. Kept tiny
# and obvious rather than pulling in a full ticker map the tool does not need.
_TICKER_CIK = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
}


# ── The computation ───────────────────────────────────────────────────────────

def compute(facts_by_field, mkt_cap):
    """Greenblatt's ROC and earnings yield from standardized inputs.

    ``facts_by_field`` maps canonical field names (``operating_income``,
    ``ppe_net``, ...) to numeric values; ``mkt_cap`` is the market cap in the same
    units as the fact values. A ``"derived"`` key (a list of rule names), if
    present, is carried through unchanged. Returns
    ``{roc_greenblatt, ebit_ev_yield, inputs, missing, derived}``:

    * ``roc_greenblatt`` / ``ebit_ev_yield`` -- percentages, or ``None`` when any input the
      metric needs is missing (or its denominator is zero). Never a zero
      substituted for a missing number.
    * ``inputs`` -- the exact values used, ``mkt_cap`` included, for provenance.
    * ``missing`` -- the names of every input that was absent, sorted; a caller
      can see *why* a metric is ``None``.
    * ``derived`` -- the provable-derivation rules (#257) that supplied an input
      the filer did not tag directly; empty when none fired.
    """
    def val(name):
        v = facts_by_field.get(name)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    derived = list(facts_by_field.get("derived") or [])
    ebit = val("operating_income")
    inputs = {name: val(name) for name in set(_ROC_FIELDS) | set(_EV_FIELDS)}
    inputs["mkt_cap"] = (mkt_cap if isinstance(mkt_cap, (int, float))
                         and not isinstance(mkt_cap, bool) else None)

    missing = sorted(name for name, v in inputs.items() if v is None)

    # ROC = EBIT / ((assets_current - liabilities_current) + ppe_net)
    roc_greenblatt = None
    if all(inputs[f] is not None for f in _ROC_FIELDS):
        invested_capital = ((inputs["assets_current"] - inputs["liabilities_current"])
                            + inputs["ppe_net"])
        if invested_capital != 0:
            roc_greenblatt = ebit / invested_capital * 100

    # EV = mkt_cap + long_term_debt + short_term_debt - cash;  yield = EBIT / EV
    ebit_ev_yield = None
    if inputs["mkt_cap"] is not None and all(inputs[f] is not None for f in _EV_FIELDS):
        ev = (inputs["mkt_cap"] + inputs["long_term_debt"]
              + inputs["short_term_debt"] - inputs["cash"])
        if ev != 0:
            ebit_ev_yield = ebit / ev * 100

    return {
        "roc_greenblatt": roc_greenblatt,
        "ebit_ev_yield": ebit_ev_yield,
        "inputs": inputs,
        "missing": missing,
        "derived": derived,
    }


# ── Local facts -> field values ───────────────────────────────────────────────

def _period_days(entry):
    s, e = entry["period_start"], entry["period_end"]
    if not s or not e:
        return 0  # instant (balance-sheet) fact: no duration
    from datetime import date
    return (date.fromisoformat(e) - date.fromisoformat(s)).days


def _pick_latest(entries):
    """The most recent value for a field. An annual-duration period (income /
    cash-flow line) is preferred over a quarter; a balance-sheet instant (no
    duration) falls through to plain latest-by-end. ``None`` when the filer
    never tagged the field."""
    if not entries:
        return None
    annual = [e for e in entries if 300 <= _period_days(e) <= 380]
    pool = annual or entries
    return max(pool, key=lambda e: e["period_end"])["value"]


def _latest_instant(entries):
    """The most recent entry in a raw concept list, by ``period_end``. ``None``
    for an empty list."""
    return max(entries, key=lambda e: e["period_end"]) if entries else None


def _latest_annual(entries):
    """The most recent annual-duration entry (an income / cash-flow line),
    ``None`` when the concept has no annual period."""
    annual = [e for e in entries if 300 <= _period_days(e) <= 380]
    return max(annual, key=lambda e: e["period_end"]) if annual else None


def _concept_entries(all_facts, concept):
    return [f for f in all_facts if f.get("concept") == concept]


def apply_derivations(all_facts, out):
    """Apply the two provable-derivation rules (#257) to the resolved field map
    ``out``, in place, and return the list of rule names that fired.

    Each rule fires ONLY when the filer's own tags prove the value -- never a
    guessed zero:

    * ``short_term_debt=0`` -- when ``short_term_debt`` resolved to nothing and
      the filer's latest ``LongTermDebt`` equals its ``LongTermDebtNoncurrent``
      at the same ``period_end`` (so total debt is entirely noncurrent, i.e. the
      current portion is a provable zero).
    * ``ebit=pretax+interest`` -- when ``operating_income`` resolved to nothing,
      reconstruct EBIT as pre-tax income + nonoperating interest expense for the
      same annual period. Both inputs must be present, or the rule does not fire.
    """
    derived = []

    # Zero current debt: total == noncurrent at the same period_end -> current 0.
    if out.get("short_term_debt") is None:
        ltd = _latest_instant(_concept_entries(all_facts, _LONG_TERM_DEBT))
        if ltd is not None:
            noncur = next(
                (e for e in _concept_entries(all_facts, _LONG_TERM_DEBT_NONCURRENT)
                 if e["period_end"] == ltd["period_end"]), None)
            if noncur is not None and noncur["value"] == ltd["value"]:
                out["short_term_debt"] = 0
                derived.append("short_term_debt=0")

    # EBIT fallback: pre-tax income + nonoperating interest, same annual period.
    if out.get("operating_income") is None:
        pretax = _latest_annual(_concept_entries(all_facts, _PRETAX_INCOME))
        if pretax is not None:
            interest = next(
                (e for e in _concept_entries(all_facts, _INTEREST_NONOPERATING)
                 if e["period_end"] == pretax["period_end"]
                 and 300 <= _period_days(e) <= 380), None)
            if interest is not None:
                out["operating_income"] = pretax["value"] + interest["value"]
                derived.append("ebit=pretax+interest")

    return derived


def facts_by_field_for(store, cik):
    """Resolve every canonical field for one CIK to a single latest value, the
    shape ``compute`` wants, then apply the provable-derivation rules (#257).
    Read-only against the local facts store. The returned dict carries a
    ``"derived"`` list naming each rule that fired."""
    import sqlite3
    try:
        all_facts = store.facts_for(cik)
    except sqlite3.OperationalError:
        # An unpopulated local store (no facts table yet) is "no facts", not a
        # crash: every field resolves to None with a full `missing` list.
        all_facts = []
    out = {}
    for field in cc.CANONICAL:
        out[field] = _pick_latest(cc.resolve(all_facts, field))
    out["derived"] = apply_derivations(all_facts, out)
    return out


# ── Pipeline side ─────────────────────────────────────────────────────────────

def load_pipeline_rows(source):
    """Read the ``/api/stocks`` payload from a URL or a local JSON file and return
    ``{ticker: row}``. Accepts either the ``{"stocks": [...]}`` envelope the mock
    server returns or a bare list of rows."""
    if str(source).startswith(("http://", "https://")):
        with urllib.request.urlopen(source) as resp:  # noqa: S310 (explicit CLI URL)
            payload = json.loads(resp.read().decode("utf-8"))
    else:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    rows = payload.get("stocks", payload) if isinstance(payload, dict) else payload
    return {r["ticker"]: r for r in rows}


def _delta(local, pipeline):
    if local is None or pipeline is None:
        return None
    return local - pipeline


def _fmt(v):
    return "     n/a" if v is None else f"{v:8.2f}"


def compare(store, ticker, cik, pipeline_row):
    """Local vs. pipeline for one ticker: compute locally, read the pipeline's
    own numbers, return the two plus their deltas."""
    mkt_cap = pipeline_row.get("mkt_cap") if pipeline_row else None
    local = (compute(facts_by_field_for(store, cik), mkt_cap) if cik
             else {"roc_greenblatt": None, "ebit_ev_yield": None, "inputs": {},
                   "missing": ["cik"], "derived": []})
    p_roc = pipeline_row.get("roc_greenblatt") if pipeline_row else None
    p_yield = pipeline_row.get("ebit_ev_yield") if pipeline_row else None
    return {
        "ticker": ticker,
        "local_roc_greenblatt": local["roc_greenblatt"],
        "pipeline_roc_greenblatt": p_roc,
        "delta_roc_greenblatt": _delta(local["roc_greenblatt"], p_roc),
        "local_ebit_ev_yield": local["ebit_ev_yield"],
        "pipeline_ebit_ev_yield": p_yield,
        "delta_ebit_ev_yield": _delta(local["ebit_ev_yield"], p_yield),
        "missing": local["missing"],
        "derived": local.get("derived", []),
    }


def format_report(rows):
    """One line per ticker: local, pipeline and delta % for both metrics. The
    last column is the provable-derivation rules (#257) that fired when any did,
    otherwise the missing inputs."""
    lines = [
        "ticker    roc_greenblatt(local/pipe/delta%) ebit_ev_yield(local/pipe/delta%)   missing/derived",
    ]
    for r in rows:
        derived = r.get("derived") or []
        last = (",".join(derived) if derived
                else (",".join(r["missing"]) if r["missing"] else "-"))
        lines.append(
            f"{r['ticker']:<8}  "
            f"{_fmt(r['local_roc_greenblatt'])} {_fmt(r['pipeline_roc_greenblatt'])} "
            f"{_fmt(r['delta_roc_greenblatt'])}   "
            f"{_fmt(r['local_ebit_ev_yield'])} {_fmt(r['pipeline_ebit_ev_yield'])} "
            f"{_fmt(r['delta_ebit_ev_yield'])}   "
            f"{last}"
        )
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def run(pipeline, tickers, *, store=None, ticker_ciks=None, out=None):
    """The CLI body, with the store and ticker->CIK map injectable so it stays
    offline-testable. Prints the report and returns the row dicts."""
    out = out or sys.stdout
    ticker_ciks = ticker_ciks if ticker_ciks is not None else _TICKER_CIK
    own_store = store is None
    store = store or FactsStore(readonly=True)
    try:
        pipeline_rows = load_pipeline_rows(pipeline)
        rows = [compare(store, t, ticker_ciks.get(t), pipeline_rows.get(t))
                for t in tickers]
    finally:
        if own_store:
            store.close()
    print(format_report(rows), file=out)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Magic Formula inputs from XBRL vs. the pipeline (local only).")
    parser.add_argument("--pipeline", required=True,
                        help="URL or local JSON file in the /api/stocks shape")
    parser.add_argument("--tickers", required=True,
                        help="comma-separated tickers, e.g. AAPL,MSFT")
    args = parser.parse_args(argv)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    run(args.pipeline, tickers)
    return 0


def _self_check():
    # Hand-computed inputs, no store, no network.
    demo = {
        "operating_income": 114301000000,
        "assets_current": 143566000000,
        "liabilities_current": 145308000000,
        "ppe_net": 43715000000,
        "long_term_debt": 95281000000,
        "short_term_debt": 15807000000,
        "cash": 29965000000,
    }
    res = compute(demo, mkt_cap=2_800_000_000_000)
    assert res["missing"] == []
    assert res["derived"] == []  # nothing derived when every field is tagged
    # ROC = 114301 / ((143566 - 145308) + 43715) = 114301 / 41973 (millions)
    assert round(res["roc_greenblatt"], 4) == round(114301000000 / 41973000000 * 100, 4)
    assert round(res["ebit_ev_yield"], 4) == round(
        114301000000 / (2_800_000_000_000 + 95281000000 + 15807000000 - 29965000000)
        * 100, 4)

    gone = compute({k: v for k, v in demo.items() if k != "ppe_net"},
                   mkt_cap=2_800_000_000_000)
    assert gone["roc_greenblatt"] is None and "ppe_net" in gone["missing"]
    assert gone["ebit_ev_yield"] is not None
    print("magic_inputs self-check: PASS")


if __name__ == "__main__":
    # With CLI args, run the comparison; with none, run the offline self-check.
    if len(sys.argv) > 1:
        raise SystemExit(main())
    _self_check()
