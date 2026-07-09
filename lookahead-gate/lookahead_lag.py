"""
Reference implementation of every §2a/§2b/§2g OHLCV-derived indicator, lagged
one period to the prior close (issue #42; see Project folder/Project.md).

Chan (Quantitative Trading, Ch. 3): "Use lagged historical data... based on
data up to the close of the previous trading period only" — the cure for
look-ahead bias. Every function below enforces that mechanically: given the
index `i` of "today", each one reads only closes/bars at index < i. Index `i`
itself (today's own high/low/close) is NEVER read — that's what prevents the
classic "buy when within 1% of the day's low" bug, since you can't know the
day's low until it closes.

This module is the portable reference meant to be ported into the
Stock-Data-Pipeline repo's indicator-computation stage (ARCHITECTURE.md rule:
"new metric -> add upstream"). `ab_truncation_test.py` in this same folder is
the gate that proves the discipline holds (Chan's A-vs-B truncation test) and
that a same-day peek would be caught.

Bars: list of {"date": str, "open": float, "high": float, "low": float,
"close": float, "volume": float}, sorted ascending by date. Closes: a plain
list of close prices, same ordering. All functions return None when there
isn't enough prior history yet (no silent wraparound / zero-fill).
"""


def sma(closes, i, window):
    """Simple moving average of the `window` closes strictly before day i."""
    if i - window < 0:
        return None
    return sum(closes[i - window:i]) / window


def ma_cross(closes, i, fast=50, slow=200):
    """+1 fast>slow (bullish cross), -1 fast<slow (bearish), 0 equal."""
    f, s = sma(closes, i, fast), sma(closes, i, slow)
    if f is None or s is None:
        return None
    return 1 if f > s else (-1 if f < s else 0)


def trailing_return(closes, i, months, bars_per_month=21):
    """Trailing simple return over `months`, close-to-close, through the prior close."""
    window = months * bars_per_month
    if i - window - 1 < 0:
        return None
    start, end = closes[i - window - 1], closes[i - 1]
    if start == 0:
        return None
    return (end / start - 1) * 100


def momentum_factor(closes, i, bars_per_month=21):
    """Fama-French prior(2,12): cumulative return t-12->t-2, skipping t-1 (the
    most recent month) so short-term reversal doesn't contaminate the factor."""
    lookback, skip = 12 * bars_per_month, 1 * bars_per_month
    end_idx = i - 1 - skip
    start_idx = end_idx - lookback
    if start_idx < 0:
        return None
    start, end = closes[start_idx], closes[end_idx]
    if start == 0:
        return None
    return (end / start - 1) * 100


def cross_sectional_rank(values_by_key):
    """Percentile rank (0-100) of `values_by_key` {key: lagged_value} — computed
    FRESH at this single point in time, across keys only. Never rank against a
    fixed or time-pooled distribution (that would smuggle future dates in)."""
    elig = [(k, v) for k, v in values_by_key.items() if v is not None]
    n = len(elig)
    if n == 0:
        return {}
    ordered = sorted(elig, key=lambda kv: kv[1])
    return {k: (idx / (n - 1) * 100 if n > 1 else 50.0) for idx, (k, _v) in enumerate(ordered)}


def rsi(closes, i, window=14):
    """Wilder RSI over the `window` day-over-day changes strictly before day i."""
    if i - window - 1 < 0:
        return None
    gains = losses = 0.0
    for j in range(i - window, i):
        chg = closes[j] - closes[j - 1]
        if chg >= 0:
            gains += chg
        else:
            losses -= chg
    avg_gain, avg_loss = gains / window, losses / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema_series(closes, upto, span):
    """EMA over closes[:upto] only (exclusive of index `upto` == today)."""
    k = 2 / (span + 1)
    e = None
    out = []
    for c in closes[:upto]:
        e = c if e is None else c * k + e * (1 - k)
        out.append(e)
    return out


def macd(closes, i, fast=12, slow=26, signal=9):
    """MACD line/signal/histogram as of the prior close."""
    if i - slow < 0:
        return None
    fast_e, slow_e = _ema_series(closes, i, fast), _ema_series(closes, i, slow)
    macd_line = [f - s for f, s in zip(fast_e[-len(slow_e):], slow_e)]
    if len(macd_line) < signal:
        return None
    k = 2 / (signal + 1)
    sig = None
    for v in macd_line:
        sig = v if sig is None else v * k + sig * (1 - k)
    return {"macd": macd_line[-1], "signal": sig, "hist": macd_line[-1] - sig}


def _true_range(bar, prev_close):
    return max(bar["high"] - bar["low"], abs(bar["high"] - prev_close), abs(bar["low"] - prev_close))


def atr(bars, i, window=14):
    """Average True Range over the `window` bars strictly before day i."""
    if i - window - 1 < 0:
        return None
    trs = [_true_range(bars[j], bars[j - 1]["close"]) for j in range(i - window, i)]
    return sum(trs) / window


def realized_vol(closes, i, window=20):
    """Sample stdev of daily returns over the `window` days strictly before day i."""
    if i - window - 1 < 0:
        return None
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i - window, i)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    return var ** 0.5


def beta(asset_closes, mkt_closes, i, window=60):
    """OLS beta of asset vs. market daily returns, strictly before day i."""
    if i - window - 1 < 0:
        return None
    a_rets = [asset_closes[j] / asset_closes[j - 1] - 1 for j in range(i - window, i)]
    m_rets = [mkt_closes[j] / mkt_closes[j - 1] - 1 for j in range(i - window, i)]
    ma, mm = sum(a_rets) / len(a_rets), sum(m_rets) / len(m_rets)
    cov = sum((a - ma) * (m - mm) for a, m in zip(a_rets, m_rets)) / (len(a_rets) - 1)
    var = sum((m - mm) ** 2 for m in m_rets) / (len(m_rets) - 1)
    return cov / var if var else None


def max_drawdown(closes, i, window=252):
    """Historical max drawdown (%) over the `window` closes strictly before day
    i — peak-to-SUBSEQUENT-trough (time order matters, per Chan/QuantInsti),
    not just the largest high-low spread. window=252 (~1 trading year) is a
    deliberate, round default, not fitted to any name's history. Feeds the
    risk/sizing layer's worst-historical-loss cap (issue #44)."""
    start = i - window if window is not None else 0
    if start < 0:
        start = 0
    if i - start < 2:
        return None
    series = closes[start:i]
    peak = series[0]
    worst = 0.0
    for c in series:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak else 0.0
        if dd > worst:
            worst = dd
    return worst * 100
