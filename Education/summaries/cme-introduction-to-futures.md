# CME Institute — Introduction to Futures
**Source:** CME Institute — "Introduction to Futures," Futures & Options Curriculum (CME Group, the listing exchange; primary source) — https://www.cmegroup.com/education/courses

## What it is
A vendor-neutral, exchange-authored primer on how standardized, exchange-listed futures contracts actually work: how a contract is *specified*, how *margin* finances the position, how *volume and open interest* measure tradeability, and how *term structure and rolling* distort any continuous price series. The mechanics generalize cleanly to FX and crypto futures, so the framework compounds across instrument classes the screener may later add.

## Core concepts
- **A future is a leveraged claim on notional, not a share.** One contract controls a large notional defined by a **multiplier**, so the quoted price alone is economically meaningless.
- **Standardization:** exchange fixes contract unit, tick size, tick value, listed months, and settlement, so contracts are fungible and centrally cleared.
- **Margin as performance bond:** you post a good-faith deposit (a small fraction of notional), and positions are **marked-to-market** daily; gains/losses flow as **variation margin**.
- **Two-dimensional liquidity:** each contract *month* has its own **volume** (flow) and **open interest** (outstanding stock).
- **Term structure & roll:** contracts across months trade at different prices (**contango/backwardation**), forcing a periodic **roll** that injects **roll yield** and price discontinuities into any stitched series.

## Key metrics/signals it defines + how to read/compute them
- **Multiplier / notional:** notional = price × multiplier. E-mini S&P 500 (ES) multiplier = **$50** (a 1-point move = $50); Micro E-mini (MES) = **$5**. WTI crude (CL) unit = **1,000 barrels**.
- **Tick size × tick value:** the minimum price increment and its dollar worth. ES tick = **0.25 index pts = $12.50**; CL tick = **$0.01/bbl = $10.00**; Micro WTI (MCL) tick = **$1.00**. Dollar-risk = (stop distance in ticks) × tick value.
- **Initial vs maintenance margin:** *initial* is required to open; *maintenance* is the floor — breach it and a **margin call** tops you back to initial. E.g., standard WTI maintenance ≈ **$5,830**. Margin ÷ notional ≈ true leverage and capital-at-risk.
- **Volume + open interest:** volume = contracts traded/day (activity); OI = contracts still open (committed capital). Read together as the futures liquidity analog; near expiry, OI drains from the front month into the next — the roll signal.
- **Roll yield:** gain/loss from rolling. **Backwardation → positive** roll (roll down to cheaper far month); **contango → negative** roll (roll up).

## How it maps to the Trader Screener
1. **§2g Suggested position size (ATR-based)** and **§2g Stop distance (ATR multiple)** must run through **dollar-risk-per-tick**, not share count: size = (account risk $) ÷ (ATR-in-ticks × tick value × multiplier). This is exactly why a raw **§2a Last price** is uninterpretable for futures without the multiplier — 4,500 on ES is $225,000 of notional, not $4,500.
2. **§2c Average daily volume (ADV)** and **§2c Dollar volume (ADV × price)** should be redefined for futures as **contract volume + open interest of the active month**; OI is the tradeability filter that ADV alone misses, and it flags when the front month is dying (roll due). **§2c Bid/ask spread** and **§2c Market depth / book** remain 🔴 real-time-only, unchanged.
3. **§2h IV term structure (contango/backwardation)** is the direct futures-curve analog — near vs. far month pricing — and it interacts with **§2a Return 1/3/6/12-month** and **§2a Relative strength rank**: a naïvely stitched (unadjusted) continuous series injects **phantom gaps** at each roll, faking momentum/breakouts. Back-adjusting removes the gap but corrupts absolute levels (and can go negative), so **§2a Moving averages / RSI / MACD** computed on the wrong series mislead. Also, **§2h Open interest** and **§2b Realized volatility** carry over as first-class futures fields.

## Actionable takeaways
- Add a **multiplier** and **tick value** column per instrument; make every §2g sizing/stop calc consume them.
- For futures rows, replace ADV/dollar-volume with **active-month volume + OI**, and surface days-to-roll.
- Store a **continuous-series construction flag** (unadjusted / back-adjusted / return-spliced); default momentum/§2a metrics to a back-adjusted or return-spliced series and never to raw stitched prices.
- Compute **leverage = notional ÷ initial margin** and **capital-at-risk = maintenance margin** as display fields.

## Open questions
- Which contract (standard vs. Micro) is the screener's default row per market?
- Where do per-contract spec/margin tables come from (CME feed vs. broker)?
- One canonical roll rule (OI-crossover vs. calendar vs. N-days-before-expiry) for all continuous series?
- Do we screen only the active month, or aggregate OI across the strip?

## Sources
- CME Institute — Introduction to Futures course hub: https://www.cmegroup.com/education/courses
- CME — "Margin: Know What's Needed" (Introduction to Futures): https://www.cmegroup.com/education/courses/introduction-to-futures/margin-know-what-is-needed
- CME — Performance Bonds/Margins FAQ: https://www.cmegroup.com/solutions/risk-management/performance-bonds-margins/faq-performance-bonds-margins.html
- CME — E-mini S&P 500 contract specs: https://www.cmegroup.com/markets/equities/sp/e-mini-sandp500.contractSpecs.html
- CME — Light Sweet (WTI) Crude Oil contract specs: https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.contractSpecs.html
- CME — Micro WTI Crude Oil contract specs: https://www.cmegroup.com/markets/energy/crude-oil/micro-wti-crude-oil.contractSpecs.html
- CME — "Open Interest" lesson: https://www.cmegroup.com/education/lessons/open-interest
- CME — "Deconstructing Futures Returns: The Role of Roll Yield": https://www.cmegroup.com/education/files/deconstructing-futures-returns-the-role-of-roll-yield.pdf
- pfolio academy — Continuous futures contracts explained: https://www.pfolio.io/academy/continuous-futures-contracts