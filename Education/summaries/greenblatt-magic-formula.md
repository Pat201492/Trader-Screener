# The Magic Formula (Joel Greenblatt)

**Source:** Joel Greenblatt, *The Little Book That Still Beats the Market* (John Wiley & Sons, 2010 updated edition; copyright © 2010 Joel Greenblatt, ISBN 978-0-470-62415-9) — read from the full text. This is the "Still"/2010 revision, confirmed internally by its "Introduction to the Original Edition," "Afterword to the 2010 Edition," and results updated through 2009.

## What it is

The Magic Formula is a mechanical, rules-based value strategy that ranks an entire stock universe on just two factors — one for business **quality** (return on capital) and one for **cheapness** (earnings yield) — sums the two ordinal ranks, and buys the top ~20-30 names. Greenblatt's thesis: systematically own "above-average companies at below-average prices." It is deliberately simple and back-test-validated — over the 17 years 1988-2004 a ~30-stock portfolio from the largest 3,500 US companies returned **30.8% per year vs. ~12.3% for the market** ($11,000 → over $1 million, ~96×). The formula, not the website, is the method; magicformulainvesting.com is only the free front-end Greenblatt built to reproduce the study.

## Core concepts

- **Two orthogonal factors, equal weight.** Quality and value carry different information; combining them avoids value traps (cheap-but-bad) and overpaying (great-but-expensive). The best *combination* wins, not the leader in either column.
- **Ordinal rank-and-combine, not raw blending.** Each stock is ranked 1..N on each factor and the two ranks are *summed*, lowest total wins (Greenblatt's example: 232nd on ROC + 153rd on yield = combined 385). Ranking sidesteps unit/scale mismatch between a percentage and a yield.
- **EBIT-centric, capital-structure-neutral.** Using EBIT (not net income) and enterprise value (not price) strips out differences in debt and tax rates so businesses compare like-for-like.
- **Universe hygiene before ranking.** A market-cap floor plus exclusion of financials, utilities, and foreign/ADR names produce a clean, comparable pool.
- **Discipline over conviction.** Hold ~20-30 equal-weight names, one-year holding period, minimum 3-5 year commitment — the edge only shows up across many names and cycles.

## Key metrics/signals it defines + how to read/compute them

Both definitions are stated precisely in the Appendix ("The Magic Formula"):

1. **Return on Capital (quality) = EBIT / (Net Working Capital + Net Fixed Assets).** The denominator is *tangible capital employed*: excess cash is excluded from working capital, short-term interest-bearing debt is excluded from current liabilities, and goodwill/intangibles are excluded entirely. Chosen over ROE/ROA because it measures how efficiently the operating business turns invested capital into pretax operating profit. Higher is better.
2. **Earnings Yield (cheapness) = EBIT / Enterprise Value**, where EV = market value of equity + net interest-bearing debt (i.e., market cap + debt − excess cash). It is the inverse of EV/EBIT; chosen over P/E or E/P because EV accounts for debt financing and puts different capital structures on equal footing. Higher is better.
3. **Combined rank = rank(Return on Capital) + rank(Earnings Yield)**, sorted ascending; take the top ~20-30.
4. **Universe filters:** market-cap floor (the study's largest-3,500 cutoff was market value > **$50M**; alternate tiers **$200M** and **$1B**; the Step-by-Step suggests $50M-$100M for individuals); **exclude all utilities and financial stocks** (banks, insurers, mutual funds) whose balance sheets break the capital/EBIT math; **exclude foreign companies/ADRs**.

Robustness the book reports: at higher cap floors the formula still beat the market — **23.7% vs. 12.4%** for the largest 2,500 (>$200M) and ~**23%** for the largest 1,000 (>$1B). A decile test (2,500 names ranked, held one year) was monotonic — Group 1 **17.9%** down through Group 10, Group 1 beating Group 10 by >15%/yr (1988-2004). Updated through 2009, the large-cap formula returned **+255% (13.5% annualized) over 1999-2009 while the S&P fell 0.9%/yr**, and the 1988-2009 decile ran 15.2% (Group 1) down to −0.2% (Group 10) — yet included ~4 years of underperformance inside that window.

## How it maps to the Trader Screener

- **§2d "Composite score (model.json)" is the direct analog of the whole rank-and-combine engine.** The Magic Formula *is* a two-factor composite; it is the cleanest template for how model.json should fold disparate factors into one sortable number — via **summed ordinal ranks**, not blended raw values.
- **§2d "ROIC" ≈ Return on Capital; §2d "P/E" / "Upside % (DCF/comps)" ≈ Earnings Yield.** The screener already carries a quality axis and a value axis; Greenblatt shows the minimal viable pairing. Caveat: his ROC is EBIT/tangible-capital and his value metric is EBIT/EV — stricter than a generic ROIC field or raw P/E.
- **§2d "Market cap / cap size" is exactly the cap-floor filter** ($50M/$200M/$1B), and **§2d "Sector"** implements his financials/utilities exclusions as a *pre-rank filter*, not a display column.
- **§2c "Dollar volume (ADV × price)" / "Average daily volume (ADV)"** can make explicit the liquidity screen the formula handles only implicitly through its cap floor.

**Contrast with the existing DCF/Comps/EPV model.** §2d "Upside % (DCF/comps)" is an *absolute* intrinsic value requiring forecasts, discount rates, and WACC. The Magic Formula is purely *relative and cross-sectional* — current financials only, no projections. They are complementary: EPV/DCF answers "what is it worth?"; the Magic Formula answers "which is best-ranked right now?" The composite score can host both as separate factors.

## Actionable takeaways

- Build §2d "Composite score" as **summed ordinal ranks**, seeded with ROIC + an EBIT/EV earnings-yield column before adding more factors.
- Add a Greenblatt-style **EBIT/EV** field alongside P/E — more capital-structure-robust for cross-name ranking.
- Enforce the cap floor and financials/utilities/ADR exclusions **as pre-rank filters**; add §2c dollar-volume as an explicit liquidity gate.
- Treat the output as a **portfolio-of-many** signal (20-30 names, one-year rolling holds, 3-5 year commitment), never a single-stock trigger.

## Open questions

- Does the pipeline's "ROIC" use EBIT/tangible-capital, or a looser book definition? Fidelity depends on it.
- How should smart-money (§2e) and momentum (§2a) enter the same rank-sum without one factor dominating — equal-weight ranks, or weighted?
- What rebalance cadence fits a nightly free-data pipeline vs. Greenblatt's annual, tax-timed, rolling 5-7-per-quarter cycle?
- Exclude financials/utilities globally, or only down-weight them for factors their accounting distorts?

## Sources

- **Primary:** Joel Greenblatt, *The Little Book That Still Beats the Market*, updated edition (John Wiley & Sons, 2010; ISBN 978-0-470-62415-9) — summarized directly from the full book text.
- Sections referenced: the formula and the largest-3,500 ranking with the 30.8% result (Ch. 6, Table 6.1); the $200M/$1B size tiers and the monotonic decile test (Ch. 7, Tables 7.1-7.2); the **Step-by-Step Instructions** (20-30 holdings, one-year holds, financials/utilities/ADR exclusions, cap floors); the **Afterword to the 2010 Edition** and updated-through-2009 results (Tables A.1-A.2); and the **Appendix ("The Magic Formula")** for the exact EBIT-based definitions of return on capital and earnings yield.
