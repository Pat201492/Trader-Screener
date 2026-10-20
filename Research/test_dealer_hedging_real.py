"""The dealer-hedging test on real-shaped data (#341): the universe builder,
its look-ahead gates, the shifted-issuance placebo. No network, no pipeline:
prices are injected.

Run:  python -m pytest Research/test_dealer_hedging_real.py
"""
import copy
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import dealer_hedging_causal_test as ct  # noqa: E402
import dealer_hedging_data as dh  # noqa: E402

TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE"]


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2025, 1, 6), 200)


def _dataset(end=None):
    end = end or DAYS[-1]
    tickers = {}
    for k, tk in enumerate(TICKERS):
        counts = {d: (3 if (i + k) % 7 == 0 else 0) for i, d in enumerate(DAYS) if d <= end}
        notes = [{"accession": f"{tk}-{j}", "file_date": DAYS[20 + 30 * j], "pricing_date": DAYS[18 + 30 * j],
                  "barrier_pct": 70.0, "barrier": 70.0, "start_close": 100.0}
                 for j in range(5) if DAYS[20 + 30 * j] <= end]
        tickers[tk] = {"counts": counts, "notes": notes, "mkt_cap": 1e10 * (k + 1)}
    tickers["EEE"]["mkt_cap"] = None          # an ETF proxy: no cap in the pipeline
    return {"start": DAYS[0], "end": end, "tickers": tickers}


def _prices(skip_day=None):
    rows = {}
    for k, tk in enumerate(TICKERS):
        rows[tk] = [(d, 100.0 + ((i * (k + 3)) % 17) - 8, 1e6 * (k + 1))
                    for i, d in enumerate(DAYS) if d != skip_day]
    return dh.PriceSource(rows=rows)


def test_universe_shape_calendar_and_imputed_cap():
    u, meta = ct.real_universe(_dataset(), _prices(skip_day=DAYS[50]))
    assert meta["tickers"] == 5 and meta["days"] == 199          # one day missing for every ticker
    assert meta["matched_on"].startswith("size+ADV")
    assert meta["mkt_cap_imputed"] == ["EEE"] and u["EEE"]["mkt_cap"] > 0
    a = u["AAA"]
    assert len(a["closes"]) == len(a["adv"]) == len(a["issuance"]) == 199
    assert a["issuance_cum"][-1] == sum(a["issuance"])
    assert all(n["pricing_idx"] <= n["filing_idx"] for n in a["notes"])


def test_weekend_filing_counts_on_the_next_trading_day():
    ds = _dataset()
    saturday = (date.fromisoformat(DAYS[100]) - timedelta(days=(date.fromisoformat(DAYS[100]).weekday() - 5) % 7)).isoformat()
    ds["tickers"]["AAA"]["counts"] = {saturday: 9}
    u, _ = ct.real_universe(ds, _prices())
    k = next(i for i, d in enumerate(DAYS) if d >= saturday)
    assert u["AAA"]["issuance"][k] == 9 and sum(u["AAA"]["issuance"]) == 9


def test_matching_uses_oi_only_when_point_in_time_oi_exists():
    u, meta = ct.real_universe(_dataset(), _prices())
    assert len(ct.matching_covariates(u, "AAA", 100)) == 2
    full = {tk: {d: 1000 for d in DAYS} for tk in TICKERS}
    u2, meta2 = ct.real_universe(_dataset(), _prices(), oi_by_ticker=full)
    assert meta2["matched_on"] == "size+ADV+OI" and len(ct.matching_covariates(u2, "AAA", 100)) == 3
    partial = {tk: {d: 1000 for d in DAYS[150:]} for tk in TICKERS}   # history starts late
    _, meta3 = ct.real_universe(_dataset(), _prices(), oi_by_ticker=partial)
    assert meta3["matched_on"].startswith("size+ADV (")


def test_a_filing_is_known_only_the_day_after_it():
    u, _ = ct.real_universe(_dataset(), _prices())
    k = next(i for i, n in enumerate(u["AAA"]["issuance"]) if n and i > 80)
    on = ct.issuance_intensity_percentile(u, TICKERS, k, 10)["AAA"]["sum"]
    after = ct.issuance_intensity_percentile(u, TICKERS, k + 1, 10)["AAA"]["sum"]
    assert after - on == u["AAA"]["issuance"][k]


def test_truncation_gate_on_the_real_builder():
    full, _ = ct.real_universe(_dataset(), _prices())
    cut, _ = ct.real_universe(_dataset(end=DAYS[150]), _prices())
    # The cut universe keeps only days up to DAYS[150]: same calendar prefix.
    cut_days = len(cut["AAA"]["closes"])
    assert cut_days < len(full["AAA"]["closes"])
    a = ct._signal_table(full, TICKERS)
    b = ct._signal_table(cut, TICKERS)
    assert b and all(a[k] == v for k, v in b.items())


def test_same_day_close_never_moves_that_days_signal():
    u, _ = ct.real_universe(_dataset(), _prices())
    before = ct._signal_table(u, TICKERS)
    bad = copy.deepcopy(u)
    for tk in TICKERS:
        bad[tk]["closes"][120] = 1e9
    after = ct._signal_table(bad, TICKERS)
    assert all(before[(tk, 120)] == after[(tk, 120)] for tk in TICKERS)


def test_placebo_shift_keeps_each_series_but_moves_it():
    u, _ = ct.real_universe(_dataset(), _prices())
    p = ct.real_placebo_universe(u, "shift-0")
    for tk in TICKERS:
        assert sorted(p[tk]["issuance"]) == sorted(u[tk]["issuance"])
        assert p[tk]["issuance_cum"][-1] == u[tk]["issuance_cum"][-1]
        assert p[tk]["closes"] is u[tk]["closes"] and p[tk]["notes"] is u[tk]["notes"]
    assert any(p[tk]["issuance"] != u[tk]["issuance"] for tk in TICKERS)
    assert ct.real_placebo_universe(u, "shift-0")["AAA"]["issuance"] == p["AAA"]["issuance"]


def test_run_real_runs_the_declared_grid_and_its_placebo():
    res = ct.run_real(_dataset(), _prices(), seeds=("shift-0", "shift-1"), log=lambda *a: None)
    assert res["ok"] and len(res["trials"]) == ct.TRIAL_COUNT == 27
    assert len(res["placebo_confirming"]) == 2
    assert res["summary"]["verdict"] in ("confirmed", "refuted", "inconclusive")
    assert res["universe"]["matched_on"].startswith("size+ADV")


def test_too_few_scored_trials_is_inconclusive_never_refuted():
    results = [{"underpowered": True} for _ in range(27)]
    assert ct.determine_verdict(results, [(0, False)] * 20)["verdict"] == "inconclusive"
    half = [{"underpowered": True}] * 14 + [{"underpowered": False, "confirms_difference": False,
                                            "direction": "none"}] * 13
    assert ct.determine_verdict(half, [(0, False)] * 20)["verdict"] == "inconclusive"


def test_low_tercile_controls_include_low_but_nonzero_issuers():
    u, _ = ct.real_universe(_dataset(), _prices())
    zero = ct.run_trial(u, TICKERS, 10, 0.05, 5, control="zero")
    low = ct.run_trial(u, TICKERS, 10, 0.05, 5, control="low_tercile")
    assert low["n_control"] >= zero["n_control"]
    assert low["n_treated"] == zero["n_treated"]


def test_exclusions_are_recorded_and_applied():
    ds = _dataset()
    res = ct.run_real(ds, _prices(), seeds=(), log=lambda *a: None, exclude={"EEE": "test"})
    assert res["universe"]["excluded_by_design"] == {"EEE": "test"}
    assert res["universe"]["tickers"] == 4            # EEE never entered the universe
    # Since #354 nothing is excluded by default, and the index rule is stated.
    full = ct.run_real(ds, _prices(), seeds=(), log=lambda *a: None)
    assert full["universe"]["excluded_by_design"] == {} and "title" in full["universe"]["index_counts"]


def test_run_real_refuses_a_tiny_universe():
    ds = _dataset()
    ds["tickers"] = {k: v for k, v in ds["tickers"].items() if k in ("AAA", "BBB")}
    res = ct.run_real(ds, _prices(), seeds=(), log=lambda *a: None)
    assert res["ok"] is False and "only 2" in res["reason"]
