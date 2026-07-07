# Cboe VIX Index — methodology & term structure

**Source:** Cboe Global Markets — VIX hub, methodology whitepaper & term-structure indices — https://www.cboe.com/tradable_products/vix/

## What it is
The Cboe Volatility Index (VIX) is the market's benchmark gauge of **30-day expected volatility of the S&P 500**, distilled from SPX index-option prices. It is quoted in annualized percentage points (e.g. VIX = 20 implies a ~20% annualized standard deviation of SPX returns, roughly ±5.8% over the coming month). Crucially, it is **not** the implied volatility of any single option: it is a **model-free** synthesis of the *entire* SPX option chain. Introduced in its current form in 2003 (Cboe with Goldman Sachs), it is the "premier barometer of investor sentiment," widely read as the market's fear gauge.

## Core concepts
- **Model-free implied volatility.** The VIX price-averages weighted out-of-the-money SPX/SPXW puts and calls across a wide strip of strikes, adapting the model-free variance theory of Britten-Jones & Neuberger (2000) and the replication logic of Demeterfi et al. (1999). No Black-Scholes assumption is needed.
- **Constant 30-day maturity.** Two expirations bracketing 30 days (near-term <30d, next-term >30d) are each converted to a variance, then **linearly interpolated** to a rolling 30-day horizon.
- **Mean reversion.** Volatility spikes on stress and decays toward a long-run average of ~19-20. This decay drives the shape of the VIX futures term structure.
- **Negative correlation with SPX.** Mathematically VIX has no directional bias, but it usually rises as SPX falls (leverage effect + panic put demand), making it a hedge-like, contrarian signal.

## Key metrics/signals it defines + how to read/compute them
- **VIX level → regime bucket.** Rule-of-thumb bands: **calm <15**, **normal ~15-20**, **elevated ~20-30**, **stress >30**, **panic/crisis >40** (2008 peaked ~80.9). Extremes (>30) historically precede the best forward equity returns; <13 flags complacency.
- **Computation inputs.** Market prices of SPX + SPXW options, plus U.S. Treasury yields as the risk-free rate; a forward index level **F**, the first OTM strike **K₀**, and per-strike contributions ΔK/K²·e^{rT}·Q(K). Variance → volatility = √(variance) × 100.
- **Term structure (VIX9D → VIX → VIX3M → VIX6M):** same methodology at 9, 30, 93, 184 days — a volatility "yield curve." **Contango** (upward slope, VIX9D<VIX<VIX3M<VIX6M) ~85% of the time = calm/complacency. **Backwardation** (near > far) = acute stress; the **VIX/VIX3M ratio >1.0** flags inversion, **>1.10** = deep backwardation/dislocation. Curve inversion is a more reliable crisis signal than VIX level alone.

## How it maps to the Trader Screener
- **§2f "VIX" (Macro context, ✅ FRED-collected).** Ingest FRED series **VIXCLS** (daily close, history since 1990) and derive a **regime column** by bucketing the level (calm/normal/elevated/stress/panic). Use as a Filter (e.g. suppress new longs when VIX >30) and Display-only context banner. This is the highest-value, zero-cost mapping.
- **§2h "VIX / macro vol" (Options/derivatives signals).** The same VIXCLS feed populates the options tab's regime filter — the spec explicitly notes it "shares FRED macro tab." Lets options-oriented signals be gated by market-wide vol without any paid options feed.
- **§2h "IV term structure (contango/backwardation)."** Compute a **curve-sign flag** from VIX vs VIX3M (and VIX9D vs VIX) as a proxy for near- vs far-dated event risk — a red "backwardation/stress" flag column when the ratio crosses 1.0/1.10. Cheaper than a per-name option chain yet captures the same near-vs-far signal.
- **§2f "Rates / yield curve" + §2g risk/sizing.** Blend the VIX regime with the yield-curve regime into a composite macro filter; scale **ATR-based position size** and **stop distance** down as the regime worsens.

## Actionable takeaways
- Add **VIXCLS** to the nightly FRED pull and expose a **regime column + composite macro banner** — a P1, free win.
- Store **VIX9D/VIX3M** too so the screener can flag **term-structure inversion**, a stronger crisis tell than level alone.
- Treat VIX as **contrarian/regime context**, not a stock filter: tighten sizing and demand higher conviction in stress; harvest quality names when panic (>30-40) mean-reverts.

## Open questions
- Does FRED carry VIX9D/VIX3M/VIX6M, or must those come from Cboe/yfinance for the term-structure flag?
- Exact regime thresholds — fixed bands vs a rolling **VIX percentile** normalizer (parallels §2h "IV Rank/percentile")?
- Should the regime gate longs outright, or only rescale §2g position size and §2d composite score?

## Sources
- [Cboe — VIX Index hub](https://www.cboe.com/tradable_products/vix/)
- [S&P DJI — A Practitioner's Guide to Reading VIX (whitepaper)](https://cdn.cboe.com/resources/vix/SandP%20A%20Practitioners%20Guide%20to%20Reading%20VIX.pdf)
- [Cboe — Volatility Index Methodology (PDF)](https://cdn.cboe.com/resources/indices/Volatility_Index_Methodology_Cboe_Volatility_Index.pdf)
- [SimTrade — CBOE Volatility Index (methodology & interpretation)](https://www.simtrade.fr/blog_simtrade/cboe-volatility-index/)
- [FlashAlpha — VIX Term Structure](https://flashalpha.com/concepts/vix-term-structure)
- [FRED — CBOE Volatility Index: VIX (VIXCLS)](https://fred.stlouisfed.org/series/VIXCLS)