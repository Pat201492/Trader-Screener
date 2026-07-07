# Kenneth R. French Data Library — Fama-French Factors & Construction
**Source:** Kenneth R. French Data Library — Fama-French Factor Portfolios & Construction Methodology (Tuck School of Business, Dartmouth) — https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html

## What it is
The freely downloadable source of record for academic equity factors — market/beta, SMB (size), HML (value), RMW (profitability), CMA (investment), and momentum (Mom) — plus the exact construction notes for how a stock universe is ranked into factor portfolios. It ships factor return series and the underlying sorted portfolios (6 = 2x3, 25 = 5x5, 100 = 10x10, plus univariate sorts on E/P, beta, etc.) at daily, weekly, and monthly frequencies. For a screener the value is less the return series than the *recipe*: the precise cross-sectional ranking and breakpoint mechanics that turn one characteristic into comparable quantile buckets.

## Core concepts
- **Cross-sectional sort → quantile buckets.** Rank the whole universe on one characteristic, cut at percentile breakpoints, and assign each name to a bucket. A factor is then a long-short spread: top bucket minus bottom bucket.
- **NYSE-breakpoint convention.** "Although the portfolios include all NYSE, AMEX, and NASDAQ firms with the necessary data, the breakpoints use only NYSE firms." Breakpoints are computed from the larger, more liquid names so a flood of tiny illiquid tickers can't distort the cut points; all names are then *classified* against those cuts.
- **2x3 independent double-sort.** Size (NYSE median market cap) crossed independently with a second characteristic split at the 30th/70th NYSE percentiles → six value-weighted portfolios.
- **Fixed rebalance cadence.** Size/BM, Size/OP, Size/Investment sorts rebalance **annually in June**; **momentum and reversals rebalance monthly**. Value-weighting within buckets keeps portfolios investable.

## Key metrics/signals it defines + how to read/compute them
- **Momentum (Mom).** Rank on **prior (2-12) returns** — cumulative return from t-12 to t-2, **skipping the most recent month** (t-1) to dodge short-term reversal. Size split at NYSE median; prior-return split at 30th/70th NYSE percentiles. `Mom = ½(Small High + Big High) − ½(Small Low + Big Low)`. Rebalanced monthly.
- **HML (value).** Book-to-market ranked; `HML = ½(Small Value + Big Value) − ½(Small Growth + Big Growth)`. Breakpoints = 30th/70th NYSE percentiles of B/M.
- **SMB (size).** `SMB = ⅓(SMB_B/M + SMB_OP + SMB_INV)` — small minus big averaged across the value, profitability, and investment sorts. Size breakpoint = NYSE median market equity.
- **RMW (profitability).** Operating profitability = annual revenues − COGS − interest expense − SG&A, divided by book equity (fiscal year ending t-1). `RMW = ½(Small Robust + Big Robust) − ½(Small Weak + Big Weak)`. Robust = high profitability.
- **CMA (investment).** Investment = growth in total assets (t-2 → t-1). `CMA = ½(Small Conservative + Big Conservative) − ½(Small Aggressive + Big Aggressive)`. Conservative = low asset growth.
- **Market beta.** Univariate beta-sorted portfolios; the market factor is the value-weight return of all CRSP US names minus the risk-free rate.

## How it maps to the Trader Screener
- **§2a Return 1/3/6/12-month + §2a Relative strength rank (vs universe):** implement the momentum factor directly — compute the prior 2-12 return with the **skip-the-most-recent-month** rule, then convert to a cross-sectional percentile rank over the universe. That rank *is* French's winner/loser bucketing.
- **§2d Market cap / cap size:** the SMB size sort — split the universe at a median cap breakpoint to build a size tilt/filter.
- **§2d P/E (valuation):** HML analog — rank on a value ratio (invert P/E toward earnings-yield/book-to-market) and bucket into cheap-vs-expensive quantiles.
- **§2d ROIC (quality):** RMW analog — ROIC stands in for operating profitability; sort robust-minus-weak to isolate a quality tilt.
- **§2b Beta (vs SPY):** beta-sorted buckets enable a low-volatility tilt (favor low-beta quantiles).
- **§2c Dollar volume (ADV × price):** the NYSE-breakpoint idea generalizes — use the **liquid names (high dollar volume) to set the percentile breakpoints**, then classify the whole universe against them so illiquid micro-caps don't warp the cut points.

## Actionable takeaways
- Standardize on a **single ranking engine**: percentile-rank → quantile buckets, breakpoints from liquid (high dollar-volume) names only.
- Always apply the **skip-a-month** rule to momentum (t-12 to t-2), or you contaminate it with reversal.
- Match cadence to factor: **monthly** re-rank for momentum, **annual/quarterly** for value/quality/size to cut turnover.
- Combine tilts (momentum + quality + value + low-beta) as composite z-scores rather than stacking hard filters.

## Open questions
- What liquidity threshold (dollar-volume percentile) should define the "breakpoint universe"?
- Value-weight or equal-weight the screener's buckets given a much smaller universe than CRSP?
- ROIC vs French's exact operating-profitability formula — how much does the proxy drift?
- Book-to-market data availability for HML, or settle for earnings-yield/P-E as the value proxy?

## Sources
- Kenneth R. French Data Library — https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
- Description of Fama/French Factors (5-factor, Developed) — https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_5developed.html
- Detail for Monthly Momentum Factor (Mom) — https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library/det_mom_factor.html
- Description of Fama/French Benchmark Portfolios — https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_portfolios.html