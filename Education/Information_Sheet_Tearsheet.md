# Instrument Information Sheet (Tear Sheet) — Format & Examples

> **⚠️ EDUCATIONAL / ILLUSTRATIVE ONLY.** The instruments, figures, and recommendations below are **hypothetical**, built to mirror the *format* a research/trading desk uses. **Not investment advice.** No live data.

A **tear sheet** is the one-page brief a desk produces per instrument: the snapshot metrics, the view, the catalysts/risks, and — the part most retail tools omit — a **specific derivative recommendation** with strikes, cost, and payoff. This doc gives the **reusable template** plus two worked examples (an equity and a commodity). It doubles as the **target output spec** for the Trader Screener's per-instrument detail view.

---

## THE TEMPLATE (what every sheet contains)

```
┌─ HEADER ──────────────────────────────────────────────────────────┐
│ Name / Ticker · Asset class · Sector · Date · Analyst              │
│ RATING (Buy/Hold/Sell or Long/Neutral/Short) · 12-mo Price Target  │
├─ SNAPSHOT (the screen columns) ───────────────────────────────────┤
│ Price · Mkt cap/Notional · 52-wk range · Avg daily $ volume        │
│ Valuation: P/E, EV/EBITDA, upside-to-target                        │
│ Risk: beta, realized vol, ATR, max drawdown                        │
│ Options: IV, IV Rank/percentile, skew, put/call, term structure    │
├─ THESIS (the view) ───────────────────────────────────────────────┤
│ 2-4 sentences: direction, horizon, why now                         │
├─ CATALYSTS & RISKS ───────────────────────────────────────────────┤
│ Upcoming events (earnings, data, supply) · key downside risks      │
├─ POSITIONING / SMART MONEY ───────────────────────────────────────┤
│ Insider / congress / fund flows · unusual options activity         │
├─ TECHNICALS ──────────────────────────────────────────────────────┤
│ Trend, support/resistance, momentum (RSI/MA), relative strength    │
├─ DERIVATIVE RECOMMENDATION ───────────────────────────────────────┤
│ Structure · strikes/expiry · net cost · max profit/loss/breakeven  │
│ Rationale tied to IV Rank + thesis · payoff chart                  │
└─ DISCLAIMERS ─────────────────────────────────────────────────────┘
```

The discipline: **thesis → metrics that support it → a structure whose payoff matches the thesis *and* the volatility regime.** A bullish view in *high* IV is expressed differently (spread) than the same view in *low* IV (outright call).

---

# EXAMPLE 1 — EQUITY TEAR SHEET (hypothetical)

## Northwind Semiconductor — NWS · Equity · Semiconductors
**Date:** illustrative · **Rating: BUY** · **12-mo target: $240** (+20% vs $200 spot)

### Snapshot
| Metric | Value | Metric | Value |
|---|---|---|---|
| Price | $200 | Avg daily $ vol | $1.8B (deep) |
| Market cap | $310B | Beta | 1.35 |
| 52-wk range | $128 – $214 | Realized vol (30d) | 34% |
| Fwd P/E | 28× | ATR (14d) | $6.20 |
| EV/EBITDA | 19× | **IV / IV Rank** | **38% / 72** (elevated) |
| Upside to target | +20% | Skew | moderate put skew |

### Thesis
Structural AI-datacenter demand + a product cycle into next year. Momentum strong (above rising 50/200-day MAs, RS in top decile). **But IV Rank is high (72)** ahead of earnings — options are *rich*, so we want a structure that **doesn't overpay for volatility.**

### Catalysts & risks
- **Catalysts:** earnings (≈3 wks), product launch, sector capex guides.
- **Risks:** earnings IV-crush, China export policy, high valuation (28× fwd) → sharp drawdown if growth disappoints.

### Positioning / smart money
- Net **insider buying** last quarter (illustrative); two committee-linked congressional buys flagged.
- **Unusual options activity:** upside call sweeps in the front month.

### Technicals
- Uptrend; support $188 (50-day), resistance $214 (prior high); RSI 61 (room before overbought).

### ▶ Derivative recommendation — **Bull Call Spread**
**Long 205 call / short 225 call, ~3-month expiry · net debit $7/share.**

| Metric | Value |
|---|---|
| Max profit | **$13/share** (at ≥ $225) |
| Max loss | **$7/share** (debit, at ≤ $205) |
| Breakeven | **$212** |
| Greek bias | +delta, reduced vega (sells the rich upper strike) |

**Why this structure:** bullish thesis, but **IV Rank 72 makes a lone long call expensive** (you'd overpay vega and get crushed post-earnings). Selling the 225 call offsets premium and caps vega — express the upside view at defined, lower cost. If IV Rank were *low*, we'd prefer an outright call.

![NWS bull call spread payoff](img/tear_equity_payoff.png)

---

# EXAMPLE 2 — COMMODITY TEAR SHEET (hypothetical)

## WTI Crude Oil — CL futures · Commodity · Energy
**Date:** illustrative · **View: NEUTRAL-to-LONG, hedge downside** · **Use case: producer risk desk**

### Snapshot
| Metric | Value | Metric | Value |
|---|---|---|---|
| Spot | $78/bbl | Front-month OI | high / liquid |
| 52-wk range | $66 – $94 | Realized vol (30d) | 41% |
| Contango/backwardation | mild backwardation | **IV / IV Rank** | **44% / 65** |
| Roll yield | positive (backwardation) | Skew | call-side (supply risk) |

### Thesis
A producer is **long physical crude** and wants to **protect a budget floor** without selling all upside. Near-term **backwardation + elevated IV (Rank 65)** mean selling an upside call is well-paid — fund a downside put with it.

### Catalysts & risks
- **Catalysts:** OPEC+ meeting, inventory prints (EIA), geopolitics, demand data.
- **Risks:** demand shock (recession) → price collapse; supply shock → spike (caps capped upside).

### Positioning
- Managed-money net length elevated; commercial hedgers adding shorts (illustrative COT read).

### ▶ Derivative recommendation — **Costless Producer Collar**
**Long crude + buy $72 put / sell $88 call, ~3-month expiry · ≈ $0 net cost.**

| Metric | Value |
|---|---|
| Downside floor | **−$6/bbl** (protected below $72) |
| Upside cap | **+$10/bbl** (gives up gains above $88) |
| Net premium | ≈ $0 (call funds the put — works *because* IV/skew rich) |
| Effect | locks a $72–$88 realized band |

**Why this structure:** the producer needs **certainty, not speculation.** A collar floors the downside; selling the rich upside call (high IV Rank) pays for it, so protection is ~free. Trade-off: capped gains above $88. This is the **hedger** use-case from Harris's taxonomy — risk transfer, not a directional bet.

![WTI producer collar payoff](img/tear_commodity_payoff.png)

---

# WHY THIS IS THE SCREENER'S TARGET OUTPUT
This tear sheet is essentially **what the Trader Screener's per-instrument detail view should generate.** Every block maps to data the project already has or plans to collect:

| Tear-sheet block | Screener data source |
|---|---|
| Snapshot valuation/fundamentals | `fundamentals.json`, `model.json` (✅ have) |
| Liquidity / ADV / beta / ATR | OHLCV-derived (🟡 free yfinance) |
| Options: IV, IV Rank, skew, term | options-chain feed (🔴 paid — shortlist §2h) |
| Positioning / smart money | congress + insider + UOA (✅ equity moat) |
| Technicals | OHLCV-derived (🟡) |
| **Derivative recommendation** | rules engine: thesis (score/momentum) × IV Rank → structure |

**The recommendation logic = the "matching action to signal" table** from the [Examples Gallery](Derivatives_Examples_Gallery.md): bullish + low IV → long call; bullish + high IV → call spread; hedge + rich skew → collar; range + high IV → condor. A screener that fuses your **smart-money moat** with **IV-regime-aware structure selection** is the differentiated, firm-grade output — exactly the legitimacy gap discussed for the project.

---

*Related: [Derivatives — Types & Signals](Derivatives_Types_and_Trading_Signals.md) · [Examples Gallery](Derivatives_Examples_Gallery.md) · [Screener Shortlist](../Project%20folder/SCREENER_SHORTLIST.md). All figures herein are hypothetical and for education only.*
