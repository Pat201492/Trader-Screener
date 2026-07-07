# Risk Management in Trading (QuantInsti)

**Source:** *Risk Management in Trading: Everything that you should know* — QuantInsti blog — https://blog.quantinsti.com/trading-risk-management/

## What it is
A practical primer on protecting trading capital rather than maximizing any single trade. Its thesis: "Risk management is not about eliminating risk, but about making informed decisions and protecting your capital for the long haul." It frames risk management as a repeatable process — "identifying, and assessing, followed by implementing measures to monitor and control" risks — and ties the toolkit (stop-losses, fixed-fractional position sizing, diversification, hedging) together, bridging concept to code with a Python stop-loss/take-profit backtest.

## Core concepts
- **Capital preservation first.** Survival outranks return; traders should "commence cautiously" with modest size because "protecting your capital is key."
- **Fixed-fractional risk.** Cap the loss any one trade can inflict on total equity to a small fixed fraction (1–2%).
- **Stop-losses as discipline.** "Automated orders that sell a security if it dips below a set price, protecting you from emotional decisions" and keeping "losses contained within acceptable limits."
- **Volatility-adaptive stops.** "Consider using volatility-based stop-loss orders… These adjust automatically based on the stock's recent price movements" — the natural quant proxy for which is ATR.
- **Diversification.** "Spreading investments across different sectors or asset classes to mitigate risks"; the 2019 auto-vs-tech example shows one sector's gains offsetting another's losses for "more stable overall performance."
- **Hedging.** Offsetting exposure to "lock in" a price (e.g., a corn buyer using futures at $400/bushel).
- **Risk types.** Market, credit, and counterparty risk (illustrated by Lehman Brothers).

## Key metrics/signals it defines + how to read/compute them
- **Per-trade risk %.** The lever. At 2%, "you will have limited your single trade exposure to 100×2%… a chance to bet on 50 trades before you lose all your money" — the *2% ⇒ ~50 consecutive losses to ruin* intuition, and a lower bound on how deep a losing streak your equity can absorb.
- **Position size from stop distance.** The article makes position size a function of stop distance ("the impact of a single trade… is limited"). The standard mechanic that follows from the fixed-fractional rule: **shares = (equity × risk%) / (entry − stop)**. A wider stop ⇒ fewer shares; a tighter stop ⇒ more — risk-dollars stay constant.
- **Volatility-scaled stop.** Set stop = entry − (k × ATR), so the stop is a *volatility unit*, not an arbitrary price, and share count auto-scales to each name's noise.
- **Drawdown intuition.** No explicit max-drawdown formula, but the 50-losses framing is a compounding-of-losses / path-to-ruin gauge.

## How it maps to the Trader Screener
- **§2b ATR / ATR%** is the load-bearing input: the article's "volatility-based stop" resolves to ATR, which sets stop distance and therefore everything downstream. This is why §2b is flagged "sizing + stops."
- **§2g Stop distance (ATR multiple)** implements "stop = entry − k×ATR" directly — a per-row, volatility-scaled stop column.
- **§2g Suggested position size (ATR-based)** implements shares = (equity × risk%) / (k × ATR), turning the fixed-fractional rule into a concrete display column so equal *risk* (not equal dollars) is allocated per name.
- **§2g Max drawdown (historical)** operationalizes the "50 losses to ruin" intuition — a screenable estimate of how deep a losing streak the name/strategy has historically produced, cross-checked against the per-trade risk %.
- Supports **§3 Trading style**: ATR-based sizing/stops fit the swing/position horizon the nightly free OHLCV pipeline already serves.

## Actionable takeaways
1. Fix per-trade risk at 1–2% of equity and size *backwards* from the stop.
2. Anchor stops to ATR (a fixed multiple), not round numbers, so size adapts to volatility.
3. Surface Suggested position size, Stop distance, and Max drawdown as default §2g columns — they convert a screen hit into an executable, pre-sized trade plan.
4. Diversify across sectors/classes to keep portfolio-level drawdown shallow.

## Open questions
- Which ATR multiple `k` (e.g., 1.5×, 2×, 3×) and lookback should the screener default to?
- Should risk % be a user input or a fixed 2%?
- The article omits a formal max-drawdown definition and risk-reward ratio — define these (e.g., Calmar, min R:R filter) ourselves?
- Portfolio-level risk (correlation/heat across open positions) is only touched via diversification — worth a dedicated view?

## Sources
- QuantInsti — *Risk Management in Trading: Everything that you should know* — https://blog.quantinsti.com/trading-risk-management/