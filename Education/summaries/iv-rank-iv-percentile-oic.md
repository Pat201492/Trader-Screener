# IV Rank & IV Percentile (Options Industry Council / OCC)

**Source:** Options Industry Council / OCC — *IV Metrics: IV Rank & IV Percentile* (with companion webinar *Understanding Volatility and Options Skew*) — https://www.optionseducation.org/videolibrary/implied-volatility-metrics

## What it is

Implied volatility (IV) is the market's forward-looking estimate of how much an underlying will move, backed out of live option prices. The problem: raw IV is not comparable across names — a 45% IV is cheap for a biotech and expensive for a utility. IV Rank and IV Percentile are the two normalizers that answer "is IV high *for this specific name*, right now, versus its own recent history?" They convert an absolute IV number into a 0–100 relative reading, which is what makes IV usable as a screener column.

## Core concepts

- **IV vs. realized (historical) volatility:** IV is expectation derived from option prices; realized vol measures what already happened. Higher expected movement → higher option premiums.
- **Normalization:** both metrics reference a trailing ~52-week window (≈252 trading days), so each ticker is judged against itself.
- **Mean reversion:** IV tends to revert, so extreme high/low relative readings are the actionable ones.
- **Volatility skew:** IV is not uniform across strikes. In equities/indexes, out-of-the-money puts persistently carry higher IV than equidistant calls — a structural "smirk" since the October 1987 crash (Dow −23% in a session), when protective-put demand surged and market makers began charging more to warehouse downside tail risk. That negative skew has never reverted to the pre-1987 flat surface.

## Key metrics/signals it defines + how to read/compute them

- **IV Rank (IVR)** = (Current IV − 52-wk Low IV) / (52-wk High IV − 52-wk Low IV) × 100. Uses only two data points (year's high and low IV); everything between is ignored. IVR 80 = IV near the top of its annual range.
- **IV Percentile** = (number of trading days over the past year IV closed *below* today's level) / 252 × 100. Uses the full distribution, so it's more robust when a single spike inflates the annual high. IVP 80 = IV was lower than today on 80% of the past year's days.
- **Interpretation:** high (>~70) → options are expensive for this name → favors *selling* premium (credit spreads, covered calls). Low (<~30) → options cheap → favors *buying* premium / long options. IVR and IVP can diverge sharply after one-off spikes — that gap is itself information.
- **Skew ratio** = 25-delta put IV ÷ 25-delta call IV; >1.0 confirms negative (put-side) skew and rising crash-pricing/fear.
- **Put/Call ratio** = put volume ÷ call volume; a contrarian sentiment gauge that flags bullish reversals at extreme highs (~>1.2 equity PCR = capitulation-level fear) and complacency at lows.

## How it maps to the Trader Screener

- **§2h "IV Rank / IV Percentile — the normalizer" (P1 must-have)** and **§2b "IV rank / percentile"** are exactly this resource: implement both formulas above as computed columns off nightly IV snapshots, and let IVR/IVP be the primary Filter/Sort so users can rank the universe by "cheapest/most-expensive vol for its own name."
- **§2h "Implied volatility (IV)" and §2b "Implied volatility (IV)"** supply the raw input; IVR/IVP are the display-layer that makes that raw IV cross-sectionally sortable.
- **§2h "Volatility skew (put vs call IV)"** maps to the companion webinar: store the 25Δ put/call IV ratio as a fear/crash-pricing column; expect equity names to skew negative by default.
- **§2h "Put/Call ratio"** maps to the contrarian sentiment reading — flag extremes (CBOE equity PCR) as reversal signals, tying into the §2e smart-money moat.
- **§2f "VIX / macro vol"** is the index-level analogue and regime filter that these single-name vol metrics sit beneath.

**History-accrual caveat:** all of the above are marked 🔴 (need an options-chain feed — Polygon/ORATS/Tradier/CBOE). Beyond feed cost, IVP needs ~3–6 months of nightly IV snapshots before it's meaningful, and true IVR needs a full ~1 year of high/low history. Until that backlog accrues, these columns should be flagged "warming up," not trusted.

## Actionable takeaways

- Ship **both** IVR and IVP — never raw IV alone — as the default vol columns; surface them side-by-side so divergences are visible.
- Wire IVR thresholds to strategy hints: high → premium-selling candidate, low → premium-buying candidate.
- Treat put-side skew as the equity default, not an anomaly; a *flattening* skew is the notable signal.
- Start the nightly IV snapshot job **now** so the trailing window is populated by the time the screener needs it.

## Open questions

- Which IV do we snapshot — 30-day ATM (VIX-style) or a full surface? Consistency matters for the trailing window.
- Do we use 252 or the exact count of available days while history is still accruing?
- Is a paid options feed justified for a swing/position horizon, or is single-name IV a P2 until day-trading is in scope?
- Should skew and PCR extremes feed the smart-money moat view (§2e) as standalone sentiment alerts?

## Sources

- Options Industry Council / OCC — *IV Metrics: IV Rank & IV Percentile* — https://www.optionseducation.org/videolibrary/implied-volatility-metrics
- Options Industry Council / OCC — *Understanding Volatility and Options Skew* (April webinar key takeaways) — https://www.optionseducation.org/news/april-webinar-key-takeaways-understanding-volatility-and-options-skew
- tastytrade — *Volatility Metrics* — https://support.tastytrade.com/support/s/solutions/articles/43000539059
- Wall Street Courier — *The CBOE Put-Call Ratio: A Greed & Fear Contrarian Indicator* — https://www.wallstreetcourier.com/spotlights/the-cboe-put-call-ratio-a-useful-greed-fear-contrarian-indicator/
- Ryan O'Connell, CFA — *Volatility Smile & Volatility Skew: Why IV Varies by Strike* — https://ryanoconnellfinance.com/volatility-smile-skew/