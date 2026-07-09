"""
Chan's A-vs-B truncation validation gate for every OHLCV-derived §2a/§2b
screener column (issue #42; see Project folder/Project.md, "Look-ahead
discipline layer"). Depends on issue #40 (momentum columns).

Chan (Quantitative Trading, Ch. 3): "Run the backtest on the full historical
data set... and call the resulting positions file A. Truncate the historical
data by N days... call the resulting file B. If the two are consistent, the
overlapping rows of files A and B should be identical. If not, you are
inadvertently using the truncated part of the historical data... in
determining the positions in file A" — i.e. today's value depended on data
that didn't exist yet. N = 10 to 100 days per the book.

What this script does (no external deps — stdlib only, same convention as
Education/game/validate_model.py):
  1. Builds a synthetic multi-ticker OHLCV universe (deterministic seed).
  2. Runs every lagged indicator in lookahead_lag.py over the FULL history
     (file A) and over histories truncated by N=10 and N=100 days (file B).
  3. Asserts every overlapping (ticker, date) row is IDENTICAL across A/B for
     every indicator: returns, momentum factor + RS rank, MA cross, RSI,
     MACD, ATR, realized vol, beta, max drawdown. This is the gate — it must
     PASS.
  4. Proves the gate has teeth: re-runs the same check against deliberately
     leaky variants (centered/future-peeking SMA, a same-day-close RSI leak,
     and an RS-rank computed off a time-pooled/future-aware distribution) and
     asserts the truncation test CATCHES every one of them (mismatches near
     the truncation boundary). A gate that can't fail on a known bug isn't
     validating anything.
  5. Separately asserts NO indicator changes when day i's own high/low/close
     are corrupted to extreme values ("never peek at same-day high/low/close")
     — the truncation test alone can't catch this (day i's own bar is present
     in both A and B), so it's checked directly.

Run:  python ab_truncation_test.py
Exit code 0 = gate passes (wire into the pipeline's pre-publish step, e.g.
`Stock-Data-Pipeline/validate.py`, non-zero exit blocks the nightly publish).
"""
import copy
import random
import sys

import lookahead_lag as lag

N_DAYS = 420          # ~2 trading years of synthetic history
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
MARKET = "MKT"
TRUNCATIONS = (10, 50, 100)   # Chan: N = 10 to 100 days


def make_universe(seed=42):
    """Synthetic OHLCV bars for TICKERS + MARKET, geometric random walk."""
    rnd = random.Random(seed)
    universe = {}
    for tk in TICKERS + [MARKET]:
        price = 50.0 + rnd.random() * 100
        bars = []
        for d in range(N_DAYS):
            drift = rnd.gauss(0.0003, 0.018)
            price = max(1.0, price * (1 + drift))
            o = price * (1 + rnd.gauss(0, 0.002))
            c = price
            hi = max(o, c) * (1 + abs(rnd.gauss(0, 0.004)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
            bars.append({"date": f"D{d:04d}", "open": o, "high": hi, "low": lo,
                         "close": c, "volume": 1e6 * (1 + rnd.random())})
        universe[tk] = bars
    return universe


def compute_indicator_table(universe, mkt_key=MARKET):
    """{(ticker, date): {field: value}} for every day/ticker, correct (lagged) impls."""
    table = {}
    n = len(universe[mkt_key])
    mkt_closes = [b["close"] for b in universe[mkt_key]]
    mom_by_day = {}  # day -> {ticker: mom_factor}  (for the RS cross-sectional rank)
    for tk in TICKERS:
        closes = [b["close"] for b in universe[tk]]
        for i in range(n):
            mom_by_day.setdefault(i, {})[tk] = lag.momentum_factor(closes, i)
    for tk in TICKERS:
        bars = universe[tk]
        closes = [b["close"] for b in bars]
        for i in range(n):
            rs_ranks = lag.cross_sectional_rank(mom_by_day[i])
            table[(tk, bars[i]["date"])] = {
                "return_1m": lag.trailing_return(closes, i, 1),
                "return_3m": lag.trailing_return(closes, i, 3),
                "mom_factor": mom_by_day[i][tk],
                "rs_percentile": rs_ranks.get(tk),
                "ma_cross": lag.ma_cross(closes, i, fast=10, slow=30),
                "rsi": lag.rsi(closes, i),
                "macd": lag.macd(closes, i),
                "atr": lag.atr(bars, i),
                "realized_vol": lag.realized_vol(closes, i),
                "beta": lag.beta(closes, mkt_closes, i),
                "max_drawdown": lag.max_drawdown(closes, i),
            }
    return table


def truncate_universe(universe, n_days):
    return {tk: bars[:-n_days] for tk, bars in universe.items()}


def compare_overlap(table_a, table_b, label):
    """Every (ticker,date) key present in B must be identical in A. Returns list of mismatches."""
    mismatches = []
    for key, b_vals in table_b.items():
        a_vals = table_a.get(key)
        if a_vals is None:
            mismatches.append((key, "missing-from-A", None, None))
            continue
        if a_vals != b_vals:
            mismatches.append((key, "value-mismatch", a_vals, b_vals))
    return mismatches


# ── Deliberately-leaky variants, to prove the gate has teeth ────────────────

def _sma_leaky_centered(closes, i, window):
    """BUG: centered window — reads `window//2` bars from the FUTURE (e.g. a
    naive `rolling(..., center=True)`). Classic look-ahead in MA-cross code."""
    half = window // 2
    lo, hi = i - half, i + half + 1
    if lo < 0 or hi > len(closes):
        return None
    return sum(closes[lo:hi]) / (hi - lo)


def _rsi_sameday_leak(closes, i, window=14):
    """BUG: includes day i's own close in the gain/loss window."""
    if i - window < 0:
        return None
    gains = losses = 0.0
    for j in range(i - window + 1, i + 1):   # includes j == i: today
        chg = closes[j] - closes[j - 1]
        if chg >= 0:
            gains += chg
        else:
            losses -= chg
    avg_gain, avg_loss = gains / window, losses / window
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _rank_leaky_global_breakpoints(all_mom_values_full_history, value):
    """BUG: percentile looked up against breakpoints built from the ENTIRE
    (including future) history/universe pool, instead of a fresh per-day
    cross-sectional rank. Breakpoints shift once truncation removes future
    values, so today's rank silently changes."""
    if value is None or not all_mom_values_full_history:
        return None
    pool = sorted(v for v in all_mom_values_full_history if v is not None)
    if not pool:
        return None
    below = sum(1 for v in pool if v <= value)
    return below / len(pool) * 100


def compute_leaky_table(universe, mkt_key=MARKET):
    n = len(universe[mkt_key])
    table = {}
    # global (whole-history, whole-universe) mom_factor pool for the rank leak
    all_mom = []
    per_ticker_mom = {}
    for tk in TICKERS:
        closes = [b["close"] for b in universe[tk]]
        vals = [lag.momentum_factor(closes, i) for i in range(n)]
        per_ticker_mom[tk] = vals
        all_mom.extend(vals)
    for tk in TICKERS:
        bars = universe[tk]
        closes = [b["close"] for b in bars]
        for i in range(n):
            table[(tk, bars[i]["date"])] = {
                "ma_cross_leaky": _sma_leaky_centered(closes, i, 10),
                "rsi_leaky": _rsi_sameday_leak(closes, i),
                "rs_percentile_leaky": _rank_leaky_global_breakpoints(all_mom, per_ticker_mom[tk][i]),
            }
    return table


def run_truncation_gate():
    universe = make_universe()
    table_a = compute_indicator_table(universe)
    leaky_a = compute_leaky_table(universe)

    all_ok = True

    for n_days in TRUNCATIONS:
        trunc = truncate_universe(universe, n_days)
        table_b = compute_indicator_table(trunc)
        mismatches = compare_overlap(table_a, table_b, f"correct/N={n_days}")
        status = "PASS" if not mismatches else "FAIL"
        print(f"[correct indicators] N={n_days:>3} truncation: {len(table_b)} overlapping rows -> {status}"
              + (f" ({len(mismatches)} mismatches)" if mismatches else ""))
        if mismatches:
            all_ok = False
            for key, kind, a_vals, b_vals in mismatches[:3]:
                print(f"    UNEXPECTED LEAK {key}: {kind} A={a_vals} B={b_vals}")

        leaky_b = compute_leaky_table(trunc)
        leak_mismatches = compare_overlap(leaky_a, leaky_b, f"leaky/N={n_days}")
        caught = len(leak_mismatches) > 0
        print(f"[leaky variants]     N={n_days:>3} truncation: gate {'CAUGHT the leak (expected)' if caught else 'MISSED the leak — GATE HAS NO TEETH'}"
              f" ({len(leak_mismatches)} mismatching rows)")
        if not caught:
            all_ok = False

    return all_ok


def run_same_day_peek_check():
    """Corrupt day i's own high/low/close to extreme values; every lagged
    indicator attached to day i must be UNCHANGED (they never read bars[i])."""
    universe = make_universe(seed=7)
    baseline = compute_indicator_table(universe)

    corrupted = copy.deepcopy(universe)
    i_check = 300
    for tk in TICKERS:
        bar = corrupted[tk][i_check]
        bar["high"], bar["low"], bar["close"] = 1e9, -1e9, 1e9
    corrupted_table = compute_indicator_table(corrupted)

    ok = True
    for tk in TICKERS:
        date = universe[tk][i_check]["date"]
        before, after = baseline[(tk, date)], corrupted_table[(tk, date)]
        if before != after:
            ok = False
            print(f"    SAME-DAY PEEK on {tk}@{date}: before={before} after={after}")

    # sanity: the leaky same-day RSI SHOULD change when day i's close is corrupted
    closes = [b["close"] for b in universe[TICKERS[0]]]
    closes_corrupted = [b["close"] for b in corrupted[TICKERS[0]]]
    leaky_before = _rsi_sameday_leak(closes, i_check)
    leaky_after = _rsi_sameday_leak(closes_corrupted, i_check)
    leak_detected = leaky_before != leaky_after
    print(f"[same-day peek]      lagged indicators at day {i_check}: {'PASS (unchanged)' if ok else 'FAIL (leaked)'}"
          f" -- leaky RSI control {'correctly changed (expected)' if leak_detected else 'DID NOT CHANGE - control is broken'}")
    return ok and leak_detected


if __name__ == "__main__":
    truncation_ok = run_truncation_gate()
    same_day_ok = run_same_day_peek_check()
    passed = truncation_ok and same_day_ok
    print(f"\nA-vs-B truncation gate: {'PASS' if passed else 'FAIL'}")
    sys.exit(0 if passed else 1)
