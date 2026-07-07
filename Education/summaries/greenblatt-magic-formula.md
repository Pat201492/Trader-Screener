# The Magic Formula (Joel Greenblatt)

**Source:** *The Little Book That Beats the Market* — Joel Greenblatt (Gotham Capital), official site — https://www.magicformulainvesting.com/

## What it is

The Magic Formula is a mechanical, rules-based value strategy that ranks an entire equity universe on just two factors — one for business **quality**, one for **cheapness** — sums the two ordinal ranks, and buys the top names. Greenblatt's premise: systematically own "good companies at bargain prices." It is deliberately simple, backtest-validated (the book reports ~30% annualized 1988–2004; independent replications are more modest but still market-beating: ~11.4% vs. S&P 8.7% for US 2003–2015), and designed to be run by an individual without discretion. The official site is a durable front-end; the method itself lives in the book.

## Core concepts

- **Two orthogonal factors, equal weight.** Quality (return on capital) and value (earnings yield) capture different information; combining them avoids value traps (cheap-but-bad) and overpaying (great-but-expensive).
- **Ordinal rank-and-combine, not raw-value blending.** Each stock gets a rank 1..N on each factor; the two ranks are **summed** and sorted **ascending** (lowest combined rank wins). Ranking sidesteps unit/scale mismatch between a percentage and a yield.
- **Universe hygiene before ranking.** A market-cap floor plus sector exclusions (financials, utilities) and foreign/ADR exclusions define a clean, comparable pool.
- **EBIT-centric, capital-structure-neutral.** Using EBIT (not net income) and enterprise value (not price) normalizes across differing debt and tax situations so companies compare apples-to-apples.
- **Discipline over conviction.** Hold ~20–30 equally-weighted names, rebalance annually, and expect multi-year (5–10+) horizons; the edge shows up only across many holdings and cycles.

## Key metrics/signals it defines + how to read/compute them

1. **Return on Capital (quality)** = `EBIT / (Net Working Capital + Net Fixed Assets)`. Uses *tangible* capital employed (excludes goodwill/intangibles and excess cash), so it measures how efficiently the operating business turns invested dollars into operating profit. Higher is better.
2. **Earnings Yield (cheapness)** = `EBIT / Enterprise Value`, where EV = market cap + debt − cash. The inverse of an EV/EBIT multiple; it answers "what operating return does the whole enterprise buy?" Higher is better.
3. **Combined rank** = `rank(ReturnOnCapital) + rank(EarningsYield)`, sorted ascending; take top N (site offers 30 or 50; book suggests 20–30).
4. **Universe filters:** market cap above an adjustable floor (site lets you set it; book uses ≥ ~$50M), exclude **financials** and **utilities** (their balance sheets break the capital/EBIT math), exclude foreign/ADRs. The cap floor doubles as an implicit liquidity/tradeability screen.

## How it maps to the Trader Screener

- **§2d "Composite score (model.json)" is the direct analog of the whole rank-and-combine engine.** The Magic Formula *is* a two-factor composite score; it's the cleanest template for how model.json should normalize disparate factors into one sortable number — via **ordinal ranks summed**, rather than blending raw values.
- **§2d "ROIC" ≈ Return on Capital, and §2d valuation (P/E, "Upside % (DCF/comps)") ≈ Earnings Yield.** The screener already collects both a quality axis (ROIC) and a value axis; Greenblatt shows the minimal viable pairing. Note his ROC uses tangible capital and EBIT, a stricter definition than a generic ROIC field.
- **§2d "Market cap / cap size" is exactly the market-cap floor filter**, and Greenblatt's exclusion of financials/utilities maps onto **§2d "Sector"** as a filter, not just a display column.
- **§2c "Dollar volume (ADV × price)" / "Average daily volume (ADV)" serve as the explicit liquidity screen** that the formula only handles implicitly through its cap floor — an upgrade the Trader Screener can make.

**Contrast with the existing DCF/Comps/EPV model.** The current pipeline produces an *absolute* intrinsic value ("Upside % (DCF/comps)") requiring forecasts, discount rates, and assumptions. The Magic Formula is purely *relative and cross-sectional* — no cash-flow projections, no WACC, just current-financials ranks. They're complementary: EPV/DCF answers "what is it worth?"; Magic Formula answers "which is the best-ranked right now?" The composite score can host both as separate factors.

## Actionable takeaways

- Implement §2d "Composite score" as **summed ordinal ranks**, starting with ROIC + earnings yield, before adding more factors.
- Compute a Greenblatt-style **EBIT/EV** column alongside P/E; it's more capital-structure-robust for cross-name ranking.
- Enforce sector exclusions and a cap floor **as pre-rank filters**, and add §2c dollar-volume as an explicit liquidity gate.
- Treat the formula as a **portfolio-of-many** signal (20–30 names, annual rebalance), not a single-stock buy trigger.

## Open questions

- Does the pipeline's "ROIC" use tangible capital + EBIT, or a looser definition? Alignment matters for fidelity.
- How should smart-money (§2e) and momentum (§2a) enter the same rank-sum without one factor dominating — equal weight, or weighted ranks?
- What's the right rebalance cadence given a nightly free-data pipeline vs. Greenblatt's annual, tax-timed cycle?
- Should financials/utilities be excluded globally, or only down-weighted for factors where their accounting distorts the math?

## Sources

- Magic Formula Investing — official site: https://www.magicformulainvesting.com/
- Joel Greenblatt, *The Little Book That Beats the Market* (Wiley).
- Magic formula investing — Wikipedia: https://en.wikipedia.org/wiki/Magic_formula_investing
- "The Reasoning Behind Return On Capital in the Magic Formula" — Old School Value: https://www.oldschoolvalue.com/stock-valuation/the-magic-formula-return-on-capital/
- "A critical look at Greenblatt's Magic Formula" — Reasonable Deviations: https://reasonabledeviations.com/2020/06/08/greenblatt-magic-formula/
- "Greenblatt's Magic Formula for Beating the Market" — AAII (Medium): https://aaii.medium.com/greenblatts-magic-formula-for-beating-the-market-ccfc429287ec