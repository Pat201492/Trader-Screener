"""Note-pricing dataset (#365). No network: EDGAR, prices and rates are stubs.

Run:  python -m pytest Research/note_pricing/test_data.py
"""
import json
import math
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import data as npd  # noqa: E402


def _days(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = _days(date(2026, 6, 1), 90)
CLOSES = {"NVDA": [(d, 100.0 * math.exp(0.01 * ((i % 5) - 2))) for i, d in enumerate(DAYS)],
          "IWM": [(d, 200.0 + i * 0.1) for i, d in enumerate(DAYS)]}
RATES = {"2026-06-01": 3.9, "2026-08-03": 3.7}
MARKET = npd.Market(closes=CLOSES, rates=RATES)


def _terms(**kw):
    base = {"issuer": {"value": "JPMorgan Chase Financial Company LLC"},
            "guarantor": {"value": "JPMorgan Chase & Co."},
            "selling_agents": {"value": ["J.P. Morgan Securities LLC"]},
            "agent_commission_pct": {"value": 2.325},
            "pricing_date": {"value": "2026-09-01"}, "maturity_date": {"value": "2027-09-01"},
            "underlyings": {"value": [{"name": "NVIDIA Corporation"}]},
            "barrier_pct": {"value": 60.0}, "contingent_coupon_rate": {"value": 12.0},
            "estimated_value_per_1000": {"value": 965.0}, "filing_stage": {"value": "final"}}
    for k, v in kw.items():
        base[k] = None if v is None else {"value": v}
    return base


LOOKUP = lambda name: {"NVIDIA Corporation": "NVDA", "Russell 2000® Index": "IWM"}.get(name)


def test_row_has_the_slim_columns_and_market_inputs_on_the_pricing_date():
    row, why = npd.note_row("JPM", "0001665650", "a-1", "2026-09-03", _terms(), MARKET, LOOKUP)
    assert why is None and set(row) == set(npd.COLUMNS)
    assert row["tenor_years"] == pytest.approx(1.0, abs=0.01)
    assert json.loads(row["selling_agents"]) == ["J.P. Morgan Securities LLC"]
    assert row["spot"] == MARKET.on("NVDA", "2026-09-01")[0]       # the close on/before pricing
    assert row["vol20_max"] is not None and row["vol60_max"] is not None
    assert row["rate_1y"] == 3.7                      # the latest yield on or before 2026-09-01


def test_market_inputs_never_look_past_the_pricing_date():
    s1, v1, _ = MARKET.on("NVDA", DAYS[40])
    bumped = npd.Market(closes={"NVDA": CLOSES["NVDA"][:41] + [(d, 1e6) for d in DAYS[41:]]}, rates=RATES)
    s2, v2, _ = bumped.on("NVDA", DAYS[40])
    assert (s1, v1) == (s2, v2)


def test_preliminary_and_non_notes_are_skipped():
    assert npd.note_row("JPM", "c", "a", "2026-09-03", _terms(filing_stage="preliminary"),
                        MARKET, LOOKUP) == (None, "preliminary")
    assert npd.note_row("JPM", "c", "a", "2026-08-25", _terms(), MARKET, LOOKUP)[1] == "preliminary"
    assert npd.note_row("JPM", "c", "a", "2026-09-03",
                        _terms(barrier_pct=None, contingent_coupon_rate=None), MARKET,
                        LOOKUP) == (None, "not a structured note")


def test_worst_of_keeps_the_max_vol_and_no_single_spot():
    t = _terms(underlyings=[{"name": "NVIDIA Corporation"}, {"name": "Russell 2000® Index"}])
    row, _ = npd.note_row("JPM", "c", "a", "2026-09-03", t, MARKET, LOOKUP)
    assert row["n_underlyings"] == 2 and row["spot"] is None
    assert row["vol60_max"] == max(MARKET.on("NVDA", "2026-09-01")[2], MARKET.on("IWM", "2026-09-01")[2])


class StubClient:
    """full_text_search by CIK + week; records calls."""
    TICKERS_URL = "x"

    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def full_text_search(self, forms=None, startdt=None, enddt=None, ciks=None, **kw):
        self.calls.append((ciks, startdt, enddt))
        return {"hits": {"total": {"value": 0}, "hits": self.hits.get((ciks, startdt), [])}}

    def get_json(self, url):
        return {}


def _hit(acc, day):
    return {"_id": f"{acc}:d.htm", "_source": {"adsh": acc, "ciks": ["0001665650"], "file_date": day,
                                                "display_names": ["JPM (CIK 0001665650)"], "form": "424B2"}}


def test_week_sampling_by_issuer_cik_resumes_and_caps(tmp_path):
    w0, w1 = date(2026, 8, 31), date(2026, 9, 6)
    hits = {("0001665650", w0.isoformat()): [_hit(f"0001-26-{i:06d}", "2026-09-03") for i in range(12)]}
    c = StubClient(hits)
    conn = npd.connect(tmp_path)
    kept, skipped = npd.sample_week(c, conn, "JPM", "0001665650", w0, w1, MARKET, LOOKUP, per_week=4,
                                    extract=lambda cl, cik, acc: _terms())
    assert kept == 4 and conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 4
    assert c.calls == [("0001665650", w0.isoformat(), w1.isoformat())]
    assert npd.sample_week(c, conn, "JPM", "0001665650", w0, w1, MARKET, LOOKUP, per_week=4,
                           extract=lambda *a: _terms()) is None          # done: not re-searched
    assert len(c.calls) == 1


def test_week_with_only_preliminaries_stops_after_bounded_tries(tmp_path):
    w0, w1 = date(2026, 8, 31), date(2026, 9, 6)
    hits = {("0001665650", w0.isoformat()): [_hit(f"0002-26-{i:06d}", "2026-09-03") for i in range(50)]}
    calls = []

    def extract(cl, cik, acc):
        calls.append(acc)
        return _terms(filing_stage="preliminary")
    kept, skipped = npd.sample_week(StubClient(hits), npd.connect(tmp_path), "JPM", "0001665650", w0, w1,
                                    MARKET, LOOKUP, per_week=4, extract=extract)
    assert kept == 0 and skipped == {"preliminary": 24} and len(calls) == 24


def test_weeks_are_complete_and_oldest_first():
    ws = npd._weeks(3, today=date(2026, 10, 21))          # a Wednesday
    assert ws[-1] == (date(2026, 10, 12), date(2026, 10, 18))
    assert [w[0].weekday() for w in ws] == [0, 0, 0] and ws[0][0] < ws[1][0]


def test_note_without_underlyings_is_skipped():
    assert npd.note_row("JPM", "c", "a", "2026-09-03", _terms(underlyings=None), MARKET,
                        LOOKUP) == (None, "no underlyings extracted")


def test_rates_unavailable_leave_nulls_then_fill_rates(tmp_path):
    conn = npd.connect(tmp_path)
    down = npd.Market(closes=CLOSES, rates={})
    row, _ = npd.note_row("JPM", "c", "a-9", "2026-09-03", _terms(), down, LOOKUP)
    assert row["rate_1y"] is None
    conn.execute(f"INSERT INTO notes ({', '.join(npd.COLUMNS)}) VALUES ({', '.join('?' * len(npd.COLUMNS))})",
                 [row[c] for c in npd.COLUMNS])
    assert npd.fill_rates(conn, MARKET) == 1
    assert conn.execute("SELECT rate_1y FROM notes").fetchone()[0] == 3.7


def test_issuer_list_is_the_issuing_entities():
    assert len(npd.ISSUERS) == 17 and all(len(c) == 10 for c in npd.ISSUERS.values())
    assert npd.ISSUERS["JPMorgan Chase Financial"] == "0001665650"     # not the parent 0000019617


# -- #376 top-up ------------------------------------------------------------
def test_higher_target_tops_up_a_sampled_week_without_rereading(tmp_path):
    w0, w1 = date(2026, 8, 31), date(2026, 9, 6)
    hits = {("0001665650", w0.isoformat()): [_hit(f"0003-26-{i:06d}", "2026-09-03") for i in range(20)]}
    c, conn, read = StubClient(hits), npd.connect(tmp_path), []

    def extract(cl, cik, acc):
        read.append(acc)
        return _terms(filing_stage="preliminary") if acc.endswith(("0", "5")) else _terms()
    npd.sample_week(c, conn, "JPM", "0001665650", w0, w1, MARKET, LOOKUP, per_week=4, extract=extract)
    first = list(read)
    kept, _ = npd.sample_week(c, conn, "JPM", "0001665650", w0, w1, MARKET, LOOKUP, per_week=10, extract=extract)
    assert conn.execute("SELECT sampled FROM weeks").fetchone()[0] == 4 + kept
    assert not set(first) & set(read[len(first):])               # nothing read twice
    assert conn.execute("SELECT COUNT(*) FROM tried").fetchone()[0] == len(read)


def test_exhausted_week_is_not_searched_again(tmp_path):
    w0, w1 = date(2026, 8, 31), date(2026, 9, 6)
    hits = {("0001665650", w0.isoformat()): [_hit("0004-26-000001", "2026-09-03")]}
    c, conn = StubClient(hits), npd.connect(tmp_path)
    npd.sample_week(c, conn, "JPM", "0001665650", w0, w1, MARKET, LOOKUP, per_week=4, extract=lambda *a: _terms())
    assert conn.execute("SELECT sampled, exhausted FROM weeks").fetchone() == (1, 1)
    assert npd.sample_week(c, conn, "JPM", "0001665650", w0, w1, MARKET, LOOKUP, per_week=10,
                           extract=lambda *a: _terms()) is None
    assert len(c.calls) == 1


def test_old_weeks_table_is_migrated(tmp_path):
    import sqlite3
    old = sqlite3.connect(str(tmp_path / "notes.sqlite"))
    old.execute("CREATE TABLE weeks (issuer_cik TEXT, week TEXT, sampled INTEGER, skipped TEXT, PRIMARY KEY (issuer_cik, week))")
    old.execute("INSERT INTO weeks VALUES ('c', '2026-08-31', 4, '{}')")
    old.commit(); old.close()
    conn = npd.connect(tmp_path)
    assert conn.execute("SELECT sampled, exhausted FROM weeks").fetchone() == (4, 0)


# -- #377 pricing more underlyings ------------------------------------------
def test_unpriced_and_refresh_market(tmp_path):
    conn = npd.connect(tmp_path)
    t = _terms(underlyings=[{"name": "Gen Digital Inc."}])
    lookup = lambda n: {"Gen Digital Inc.": "GEN"}.get(n)
    row, _ = npd.note_row("JPM", "c", "a-1", "2026-09-03", t, MARKET, lookup)
    assert row["vol60_max"] is None                        # GEN has no prices yet
    conn.execute(f"INSERT INTO notes ({', '.join(npd.COLUMNS)}) VALUES ({', '.join('?' * len(npd.COLUMNS))})",
                 [row[c] for c in npd.COLUMNS])
    no_price, no_ticker = npd.unpriced(conn, MARKET)
    assert no_price == {"GEN": 1} and no_ticker == {}
    assert npd.usable_share(conn)["usable"] == 0
    priced = npd.Market(closes={**CLOSES, "GEN": CLOSES["NVDA"]}, rates=RATES)   # after a backfill
    assert npd.refresh_market(conn, priced) == 1
    assert npd.usable_share(conn)["usable"] == 1
    assert conn.execute("SELECT vol60_max FROM notes").fetchone()[0] == priced.on("GEN", "2026-09-01")[2]
