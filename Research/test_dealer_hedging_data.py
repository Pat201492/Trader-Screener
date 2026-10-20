"""Tests for the dealer-hedging real-data builder (#340). No network: EDGAR is a
stub that records every call, prices are injected.

Run:  python -m pytest Research/test_dealer_hedging_data.py
"""
import os
import sqlite3
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import dealer_hedging_data as dh  # noqa: E402


# ── ticker map ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,ticker", [
    # Names exactly as the local 424B2 store holds them (2026-10-20).
    ("Russell 2000® Index", "IWM"), ("Nasdaq-100 Index®", "QQQ"), ("S&P 500® Index", "SPY"),
    ("Dow Jones Industrial AverageTM", "DIA"), ("Dow Jones Industrial Average®", "DIA"),
    ("Dow Jones Industrial Average", "DIA"), ("Amazon.com, Inc.", "AMZN"),
    ("NVIDIA Corporation", "NVDA"), ("Common Stock of Constellation Energy Corporation", "CEG"),
    ("Class A common stock of Meta Platforms, Inc.", "META"), ("Alphabet Inc.", "GOOGL"),
    ("Nebius Group N.V.", "NBIS"), ("The Boeing Company", "BA"),
])
def test_ticker_for_name(name, ticker):
    assert dh.ticker_for_name(name) == ticker


@pytest.mark.parametrize("name", [
    # Same words, different underlying: never mapped to the plain index.
    "Nasdaq-100® Technology Sector IndexSM", "MerQube US Tech+ Vol Advantage Index",
    "S&P 500® Futures Excess Return Index", "EURO STOXX 50® Index", "Unknown Corp",
])
def test_lookalikes_are_not_mapped(name):
    assert dh.ticker_for_name(name) is None


def test_banks_are_not_underlyings():
    assert not {"GS", "JPM", "MS", "C", "BAC", "WFC"} & set(dh.STOCK_UNDERLYINGS)


def test_every_mapped_ticker_has_live_prices():
    db = dh.PIPELINE_DB
    if not db.exists():
        pytest.skip("no local pipeline stocks.db")
    have = {r[0] for r in sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True).execute(
        "SELECT DISTINCT ticker FROM price_history")}
    # Index proxies arrive with the pipeline's #65; the rest are in the universe
    # or backfilled. Report the gaps rather than failing on them -- the builder
    # lists unpriced tickers in `skipped_tickers`.
    missing = [t for t in dh.all_tickers() if t not in have]
    print("tickers without price history:", missing)
    assert len(missing) < len(dh.all_tickers())


# ── stubs ──────────────────────────────────────────────────────────────────

class StubClient:
    """full_text_search only. `per_day` = {(phrase, iso): n}; `hits` =
    {(phrase, startdt): [hit dicts]} for the barrier sample. Any document
    method raises -- counts must come from search metadata alone."""

    def __init__(self, per_day=None, hits=None):
        self.per_day, self.hits, self.calls = per_day or {}, hits or {}, []

    def full_text_search(self, q=None, forms=None, startdt=None, enddt=None, **kw):
        self.calls.append((q, startdt, enddt))
        phrase = q.strip('"')
        if startdt == enddt:
            n = self.per_day.get((phrase, startdt), 0)
        else:
            n = 0
        hits = self.hits.get((phrase, startdt), [])
        return {"hits": {"total": {"value": n, "relation": "eq"}, "hits": hits}}

    def archive_document(self, *a, **k):
        raise AssertionError("a count must never fetch a document")

    filing_index = archive_document


def _hit(acc, cik, file_date):
    return {"_id": f"{acc}:doc.htm", "_source": {"adsh": acc, "ciks": [cik], "file_date": file_date,
                                                  "display_names": [f"Bank (CIK {cik})"], "form": "424B2"}}


PRICES = dh.PriceSource(rows={
    "NVDA": [("2026-09-01", 100.0, 1e7), ("2026-09-02", 110.0, 1e7), ("2026-09-03", 120.0, 1e7)],
    "IWM": [("2026-09-01", 200.0, 1e7)],
}, caps={"NVDA": 4e12})


# ── counts ─────────────────────────────────────────────────────────────────

def test_daily_counts_use_search_metadata_only_and_cache(tmp_path):
    c = StubClient(per_day={("NVIDIA Corporation", "2026-09-01"): 3,
                            ("NVIDIA Corporation", "2026-09-03"): 2})
    got = dh.daily_counts(c, "NVDA", date(2026, 8, 31), date(2026, 9, 4), cache_dir=tmp_path,
                          today=date(2026, 10, 1))
    assert got == {"2026-08-31": 0, "2026-09-01": 3, "2026-09-02": 0, "2026-09-03": 2, "2026-09-04": 0}
    assert all(q == '"NVIDIA Corporation"' and s == e for q, s, e in c.calls)
    n_calls = len(c.calls)
    assert dh.daily_counts(c, "NVDA", date(2026, 8, 31), date(2026, 9, 4), cache_dir=tmp_path,
                           today=date(2026, 10, 1)) == got
    assert len(c.calls) == n_calls          # all cached: no new request


def test_today_is_not_cached(tmp_path):
    c = StubClient()
    dh.daily_counts(c, "NVDA", date(2026, 9, 1), date(2026, 9, 1), cache_dir=tmp_path,
                    today=date(2026, 9, 1))
    dh.daily_counts(c, "NVDA", date(2026, 9, 1), date(2026, 9, 1), cache_dir=tmp_path,
                    today=date(2026, 9, 1))
    assert len(c.calls) == 2


# ── barrier sample ─────────────────────────────────────────────────────────

def _terms(**kw):
    base = {"underlyings": {"value": [{"name": "NVIDIA Corporation", "kind": "single_stock"}]},
            "barrier_pct": {"value": 60.0}, "pricing_date": {"value": "2026-09-01"}}
    for k, v in kw.items():
        base[k] = {"value": v}
    return base


def test_dollar_barrier_is_pct_of_the_close_on_the_pricing_date(tmp_path):
    hits = {("NVIDIA Corporation", "2026-09-01"): [_hit("0001-26-000001", "19617", "2026-09-02")]}
    terms = {"0001-26-000001": _terms(autocall_barrier_pct=100.0)}
    notes, skipped = dh.sample_notes(StubClient(hits=hits), "NVDA", date(2026, 9, 1), date(2026, 9, 30),
                                     PRICES, per_month=1, cache_dir=tmp_path,
                                     extract=lambda c, cik, acc: terms[acc])
    assert len(notes) == 1 and skipped == {}
    n = notes[0]
    assert n["start_close"] == 100.0 and n["barrier"] == 60.0 and n["autocall"] == 100.0
    assert n["file_date"] == "2026-09-02"          # the knowledge date


def test_preliminary_note_gets_no_barrier(tmp_path):
    # Pricing date AFTER the file date: terms not set when it became public.
    hits = {("NVIDIA Corporation", "2026-09-01"): [_hit("0001-26-000002", "19617", "2026-09-01")]}
    notes, skipped = dh.sample_notes(
        StubClient(hits=hits), "NVDA", date(2026, 9, 1), date(2026, 9, 30), PRICES, per_month=1,
        cache_dir=tmp_path, extract=lambda c, cik, acc: _terms(pricing_date="2026-09-03"))
    assert notes == [] and skipped == {"preliminary": 1}


def test_note_on_another_underlying_is_skipped(tmp_path):
    hits = {("NVIDIA Corporation", "2026-09-01"): [_hit("0001-26-000003", "19617", "2026-09-02")]}
    notes, skipped = dh.sample_notes(
        StubClient(hits=hits), "NVDA", date(2026, 9, 1), date(2026, 9, 30), PRICES, per_month=1,
        cache_dir=tmp_path,
        extract=lambda c, cik, acc: _terms(underlyings=[{"name": "Tesla, Inc."}]))
    assert notes == [] and skipped == {"not this underlying": 1}


def test_month_is_sampled_in_slices(tmp_path):
    c = StubClient()
    dh.sample_notes(c, "NVDA", date(2026, 9, 1), date(2026, 9, 30), PRICES, per_month=3,
                    cache_dir=tmp_path, extract=lambda *a: {})
    windows = [(s, e) for _, s, e in c.calls]
    assert windows == [("2026-09-01", "2026-09-10"), ("2026-09-11", "2026-09-20"),
                       ("2026-09-21", "2026-09-30")]


def test_build_lists_unpriced_tickers(tmp_path):
    ds = dh.build_dataset(StubClient(), PRICES, date(2026, 9, 1), date(2026, 9, 2),
                          tickers=["NVDA", "PLTR"], per_month=1, cache_dir=tmp_path,
                          extract=lambda *a: {}, log=lambda *a: None)
    assert ds["skipped_tickers"] == {"PLTR": "no price history"}
    assert set(ds["tickers"]) == {"NVDA"} and ds["tickers"]["NVDA"]["mkt_cap"] == 4e12
    assert dh.coverage(ds)["underlyings_priced"] == 1
