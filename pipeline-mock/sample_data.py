"""
Reference/mock data for every field issue #81 flags as missing from the real
Stock-Data-Pipeline: https://github.com/Pat201492/Stock-Data-Pipeline (a
separate repo, not checked out alongside this one). This module is the
sample-data half of the local dev fixture; `server.py` serves it over the
same routes `web-dashboard/index.html` calls. Same "portable reference for
the real pipeline" posture as `lookahead-gate/lookahead_lag.py` (issue #42)
-- stdlib only, deterministic (seeded), no network/DB.

Field provenance for every row: see "Upstream contract" tables in
`Project folder/Project.md` (issues #37, #39, #40, #41, #43, #44, #45, #46)
and the field list in issue #81 itself. Three deliberately distinct IV-history
tickers (NEWCO / RAMPUP / everything else) exercise the warming-up gate
(`ivWarmup()` in web-dashboard/index.html: IV %ile ready at 126 nightly
snapshots, IV Rank at 252) end-to-end instead of every row shipping "ready".
"""
import math
import random
from datetime import datetime, timedelta

SEED = 42
RF_RATE = 0.045  # risk-free rate used for the Black-Scholes greeks below


def _bs_greeks(spot, strike, years, sigma, is_call):
    """Black-Scholes price + greeks. Standard normal CDF via math.erf (stdlib,
    exact — no need for the polynomial approximation the front-end uses for
    its own client-side probit, see index.html's erf())."""
    if years <= 0 or sigma <= 0:
        return None
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (RF_RATE + 0.5 * sigma ** 2) * years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    pdf1 = math.exp(-d1 ** 2 / 2) / math.sqrt(2 * math.pi)
    disc = math.exp(-RF_RATE * years)
    gamma = pdf1 / (spot * sigma * sqrt_t)
    vega = spot * pdf1 * sqrt_t / 100
    if is_call:
        delta, nd2 = n(d1), n(d2)
        price = spot * n(d1) - strike * disc * nd2
        theta = (-(spot * pdf1 * sigma) / (2 * sqrt_t) - RF_RATE * strike * disc * nd2) / 365
        rho = strike * years * disc * nd2 / 100
    else:
        delta, nmd2 = -n(-d1), n(-d2)
        price = strike * disc * n(-d2) - spot * n(-d1)
        theta = (-(spot * pdf1 * sigma) / (2 * sqrt_t) + RF_RATE * strike * disc * nmd2) / 365
        rho = -strike * years * disc * nmd2 / 100
    return {
        "price": round(price, 2), "delta": round(delta, 3), "gamma": round(gamma, 4),
        "theta": round(theta, 3), "vega": round(vega, 3), "rho": round(rho, 3),
    }


# ── Ticker roster ────────────────────────────────────────────────────────
# Each entry is deliberately varied so every gate/badge in web-dashboard/
# index.html has at least one row that exercises it: liquidity floor,
# ETF-basket liquidity (two different upstream shapes), thin-chain gate,
# IV warming-up (two distinct partial-history stages), skew flattening,
# put/call extremes, UOA direction, smart-money presence vs. absence,
# committee-conflict, and "no options coverage at all" (below the
# liquidity-gated options universe, issue #45's stated scope).
_TICKERS = [
    # ticker, name, sector, industry, cap_size, is_etf, price, spot vol regime
    ("AAPL", "Apple Inc.", "Technology", "Consumer Electronics", "mega", False, 227.50),
    ("MSFT", "Microsoft Corp.", "Technology", "Software", "mega", False, 441.20),
    ("NVDA", "NVIDIA Corp.", "Technology", "Semiconductors", "mega", False, 132.80),
    ("TSLA", "Tesla Inc.", "Consumer Discretionary", "Auto Manufacturers", "large", False, 248.90),
    ("JPM", "JPMorgan Chase & Co.", "Financials", "Banks", "mega", False, 218.40),
    ("XOM", "Exxon Mobil Corp.", "Energy", "Oil & Gas", "large", False, 114.30),
    ("PFE", "Pfizer Inc.", "Healthcare", "Drug Manufacturers", "large", False, 27.60),
    ("KO", "Coca-Cola Co.", "Consumer Staples", "Beverages", "large", False, 63.10),
    ("DUK", "Duke Energy Corp.", "Utilities", "Utilities—Regulated Electric", "large", False, 112.70),
    ("TSM", "Taiwan Semiconductor Mfg (ADR)", "Technology", "Semiconductors", "mega", False, 178.20),
    ("SPY", "SPDR S&P 500 ETF Trust", "Diversified", "ETF", "mega", True, 556.30),
    ("QQQ", "Invesco QQQ Trust", "Technology", "ETF", "mega", True, 481.90),
    ("MICRO", "Microcap Mining Co.", "Materials", "Industrial Metals", "micro", False, 4.85),
    ("NEWCO", "Newco Robotics Inc.", "Industrials", "Automation", "small", False, 38.20),
    ("RAMPUP", "Rampup Biosciences Inc.", "Healthcare", "Biotechnology", "small", False, 21.40),
]

# tickers whose options chain clears the liquidity gate + is in the paid feed at all
_OPTIONED = {"AAPL", "MSFT", "NVDA", "TSLA", "JPM", "SPY", "QQQ", "NEWCO", "RAMPUP"}
# thin-chain tickers (present in the feed, below the >=500 OI / >=100 vol gate)
_THIN_CHAIN = {"NEWCO"}
# IV-history stage: everything not listed defaults to "fully warmed" (300 days)
_IV_HISTORY_DAYS = {"NEWCO": 45, "RAMPUP": 140}
# smart-money (congress/insider/news) coverage — deliberately partial, same as
# the real pipeline (Harris: filing-lagged, not every name has recent disclosures)
_SMART_MONEY = {
    "AAPL": dict(congress_buys_90d=3, congress_sells_90d=0, insider_buys_90d=1, insider_sells_90d=0,
                 committee_conflict=False, news_sentiment_30d=0.42),
    "NVDA": dict(congress_buys_90d=5, congress_sells_90d=1, insider_buys_90d=2, insider_sells_90d=0,
                 committee_conflict=False, news_sentiment_30d=0.55),
    "TSLA": dict(congress_buys_90d=0, congress_sells_90d=4, insider_buys_90d=0, insider_sells_90d=3,
                 committee_conflict=True, news_sentiment_30d=-0.18),
    "JPM": dict(congress_buys_90d=1, congress_sells_90d=1, insider_buys_90d=0, insider_sells_90d=2,
                 committee_conflict=False, news_sentiment_30d=-0.05),
}


def build_universe():
    rnd = random.Random(SEED)
    stocks = []
    options = {}
    today = datetime.utcnow().date()

    for ticker, name, sector, industry, cap_size, is_etf, price in _TICKERS:
        is_illiquid = ticker == "MICRO"
        adv = rnd.uniform(2e4, 8e4) if is_illiquid else rnd.uniform(3e6, 4e7)
        dollar_volume = round(adv * price, 0)
        return_1m = rnd.uniform(-8, 10)
        return_3m = return_1m + rnd.uniform(-6, 12)
        return_6m = return_3m + rnd.uniform(-8, 18)
        return_12m = return_6m + rnd.uniform(-10, 25)
        # TSLA/RAMPUP deliberately bearish-momentum rows (drives the tear-sheet
        # bearish-thesis + buy-premium-hint path); KO deliberately mid-range
        # (drives the range-bound / mean-revert stop-distance path).
        if ticker in ("TSLA", "RAMPUP"):
            mom_factor = rnd.uniform(-30, -12)
        elif ticker == "KO":
            mom_factor = rnd.uniform(-2, 2)
        else:
            mom_factor = rnd.uniform(-5, 35)
        atr = round(price * rnd.uniform(0.015, 0.045), 2)
        atr_pct = round(atr / price * 100, 2)
        realized_vol = round(rnd.uniform(18, 65), 1)
        beta = round(rnd.uniform(0.4, 1.8), 2)
        low52 = round(price * rnd.uniform(0.6, 0.85), 2)
        high52 = round(price * rnd.uniform(1.05, 1.35), 2)
        max_drawdown = round(rnd.uniform(8, 42), 1)
        roic = None if is_etf else round(rnd.uniform(4, 35), 1)
        ebit_ev_yield = None if is_etf else round(rnd.uniform(2, 14), 1)
        score = round(rnd.uniform(35, 95))

        row = {
            "ticker": ticker, "name": name, "sector": sector, "industry": industry,
            "cap_size": cap_size, "is_etf": is_etf, "rank": len(stocks) + 1,
            "price": price, "mkt_cap": round(rnd.uniform(0.3, 3200), 1),
            "pe": None if is_etf else round(rnd.uniform(9, 45), 1),
            "roic": roic, "ebit_ev_yield": ebit_ev_yield,
            "score": score, "score_composite": score,
            "score_label": "Attractive" if score >= 70 else ("Fair" if score >= 45 else "Unattractive"),
            "score_stars": round(score / 20, 1),
            "avg_upside": round(rnd.uniform(-10, 30), 1),
            "low52": low52, "high52": high52,
            "rsi": round(rnd.uniform(25, 75), 1),
            # ── liquidity layer (issue #37) ──
            "adv": round(adv, 0), "dollar_volume": dollar_volume,
            # ── momentum layer (issue #40), lagged-to-prior-close per issue #42 ──
            "return_1m": round(return_1m, 2), "return_3m": round(return_3m, 2),
            "return_6m": round(return_6m, 2), "return_12m": round(return_12m, 2),
            "mom_factor": round(mom_factor, 2),
            # ── volatility / sizing layer (issue #43) ──
            "atr": atr, "atr_pct": atr_pct, "realized_vol": realized_vol, "beta": beta,
            # ── risk / sizing layer (issue #44) ──
            "max_drawdown": max_drawdown,
        }

        if is_etf:
            row["expense_ratio"] = round(rnd.uniform(0.03, 0.20), 2)
            row["tco"] = round(row["expense_ratio"] + rnd.uniform(0, 0.05), 2)
            row["etf_dq_flag"] = False
            if ticker == "SPY":
                # upstream-precomputed basket field (preferred path in basketDollarVol())
                row["basket_dollar_volume"] = round(dollar_volume * 42, 0)
            else:
                # QQQ: no precomputed basket field -- ship raw holdings so the
                # front-end derives it (the *other* documented upstream shape,
                # basketDollarVol()'s holdings[] fallback)
                row["holdings"] = [
                    {"ticker": "AAPL", "weight": 0.088, "adv": 5.5e7, "price": 227.50},
                    {"ticker": "MSFT", "weight": 0.086, "adv": 2.1e7, "price": 441.20},
                    {"ticker": "NVDA", "weight": 0.079, "adv": 2.3e8, "price": 132.80},
                    {"ticker": "AMZN", "weight": 0.052, "adv": 3.4e7, "price": 186.40},
                    {"ticker": "META", "weight": 0.048, "adv": 1.6e7, "price": 512.30},
                ]

        sm = _SMART_MONEY.get(ticker)
        if sm:
            row.update(sm)

        if ticker in _OPTIONED:
            history_days = _IV_HISTORY_DAYS.get(ticker, 300)
            thin = ticker in _THIN_CHAIN
            atm_iv = round(rnd.uniform(0.22, 0.65), 4)
            iv_rank = round(rnd.uniform(15, 30) if ticker in ("TSLA", "RAMPUP") else rnd.uniform(55, 92), 1)
            iv_pct = round(max(0, min(100, iv_rank + rnd.uniform(-12, 12))), 1)
            call_oi = rnd.randint(1200, 9000) if not thin else rnd.randint(60, 150)
            put_oi = rnd.randint(1200, 9000) if not thin else rnd.randint(60, 150)
            call_vol = rnd.randint(400, 5000) if not thin else rnd.randint(10, 60)
            put_vol = rnd.randint(400, 5000) if not thin else rnd.randint(10, 60)
            # AAPL: engineered call-side UOA (volume far >= 2x that side's OI)
            if ticker == "AAPL":
                call_oi, call_vol = 3000, 9200
            skew_25d = 1.02 if ticker == "AAPL" else round(rnd.uniform(1.08, 1.35), 2)
            put_call_oi = 1.35 if ticker == "MSFT" else round(put_oi / call_oi, 2)
            magnet_strike = round(price / 5) * 5

            summary_row_fields = {
                "iv_rank": iv_rank, "iv_percentile": iv_pct, "atm_iv_30d": atm_iv,
                "iv_history_days": history_days,
                "skew_25d": skew_25d, "put_call_oi": put_call_oi,
                "total_oi": call_oi + put_oi, "total_volume": call_vol + put_vol,
                "call_oi": call_oi, "put_oi": put_oi, "call_volume": call_vol, "put_volume": put_vol,
                "oi_max_strike": magnet_strike, "oi_max_strike_oi": max(call_oi, put_oi),
            }
            row.update(summary_row_fields)

            exp1 = today + timedelta(days=30 - today.weekday() % 7 + 14)
            exp2 = exp1 + timedelta(days=28)
            expirations = [exp1.isoformat(), exp2.isoformat()]
            chains = {}
            for exp_date, dte_days in ((exp1, (exp1 - today).days), (exp2, (exp2 - today).days)):
                years = max(dte_days, 1) / 365
                contracts = []
                for k_off in range(-4, 5):
                    strike = round((price * (1 + k_off * 0.025)) / 0.5) * 0.5
                    moneyness = (strike - price) / price
                    call_sigma = atm_iv + max(0, moneyness) * 0.15
                    put_sigma = atm_iv + max(0, -moneyness) * 0.15 * skew_25d
                    for side, sigma in (("call", call_sigma), ("put", put_sigma)):
                        g = _bs_greeks(price, strike, years, sigma, side == "call")
                        if g is None:
                            continue
                        spread = max(0.02, g["price"] * 0.03)
                        oi_base = call_oi if side == "call" else put_oi
                        vol_base = call_vol if side == "call" else put_vol
                        near_magnet = abs(strike - magnet_strike) < 1
                        contracts.append({
                            "type": side, "strike": strike,
                            "bid": round(max(0.01, g["price"] - spread / 2), 2),
                            "ask": round(g["price"] + spread / 2, 2),
                            "iv": round(sigma, 4),
                            "delta": g["delta"], "gamma": g["gamma"], "theta": g["theta"],
                            "vega": g["vega"], "rho": g["rho"],
                            "open_interest": int(oi_base * (2.2 if near_magnet else rnd.uniform(0.3, 1.1))),
                            "volume": int(vol_base * rnd.uniform(0.2, 1.0)),
                        })
                chains[exp_date.isoformat()] = contracts

            options[ticker] = {
                "summary": {
                    "spot": price, "atm_iv": atm_iv, "atm_iv_30d": atm_iv,
                    "iv_mean": round(atm_iv * rnd.uniform(0.85, 1.1), 4),
                    **summary_row_fields,
                },
                "expirations": expirations,
                "chains": chains,
            }

        stocks.append(row)

    macro = {
        "vix": 16.8, "vixcls": 16.8, "vix9d": 15.9, "vix3m": 18.4,
        "t10y3m": -0.32, "t10y3m_monthly": -0.28,
        "asof": today.isoformat(),
    }
    return stocks, options, macro


STOCKS, OPTIONS, MACRO = build_universe()
