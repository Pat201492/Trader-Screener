# Pseudo-Mathematics and Financial Charlatanism — Backtest Overfitting

**Source:** Bailey, Borwein, López de Prado & Zhu, "Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample Performance," *Notices of the American Mathematical Society*, 2014 — https://scholarworks.wmich.edu/math_pubs/40/

## What it is
A peer-reviewed proof that a backtest is nearly worthless unless you also disclose how many strategy variants were tried to produce it. The authors show that searching over enough configurations (indicators, thresholds, lookbacks, holding periods) almost guarantees an impressive *in-sample* Sharpe ratio that has zero or negative expectation *out-of-sample*. They label the common practice — citing a great historical Sharpe with no accounting for the search that produced it — as "pseudo-mathematics": rigorous-looking, statistically empty. It supplies the academic backbone for treating every historical performance number in the screener as a *selection-biased* estimate, not a forecast.

## Core concepts
- **Multiple-testing / selection bias.** Optimizing over N candidate configurations is N draws from a distribution of noise. The *maximum* of those draws is what gets reported, so the winner is fit to idiosyncratic history, not to a repeatable edge.
- **A Sharpe without its trial count N is uninterpretable.** The same Sharpe means "strong evidence" after 2 trials and "pure luck" after 1,000. Absent N, the number cannot be evaluated.
- **In-sample/out-of-sample inversion.** Under real overfitting, the *best* in-sample strategy tends to rank *below the median* out-of-sample — overfit optimization is not merely uninformative but actively perverse.
- **Probability of Backtest Overfitting (PBO).** Estimated via combinatorially-symmetric cross-validation (CSCV): repeatedly split history, pick the in-sample best, and measure how often it lands below the out-of-sample median. That frequency is the PBO.
- **Deflated / haircut Sharpe.** Discount a reported Sharpe by the number of trials and by non-normality (skew/kurtosis) before believing it.

## Key metrics/signals it defines + how to read/compute them
- **Expected maximum Sharpe from N skill-less trials:** grows roughly like `E[max_N] ≈ √(2·ln N)`. It rises without bound as you test more variants, so a high in-sample Sharpe is the *expected* outcome of a wide search, not evidence of skill.
- **Minimum Backtest Length (MinBTL, in years):** the sample you need so that testing N configurations does not, by chance alone, hand you a target in-sample Sharpe with a true out-of-sample Sharpe of 0. Upper bound `MinBTL < 2·ln(N) / E[max_N]²` — it grows with `ln(N)`. Concrete example from the paper: with only **5 years** of data, testing more than ~**45** independent configurations is expected to yield an in-sample annualized Sharpe of **1** whose out-of-sample expectation is **0**. It is necessary, not sufficient.
- **Deflated Sharpe Ratio (DSR):** the reported Sharpe adjusted for N trials, sample length, skew and kurtosis; read it as the probability the true Sharpe exceeds 0.

## How it maps to the Trader Screener
1. **Gate momentum/volatility/fundamental factors before they become defaults.** Any `§2a Price & momentum` signal (`Return 1/3/6/12-month`, `Relative strength rank (vs universe)`, `RSI`, `MACD` cross), `§2b Volatility` factor (`Beta (vs SPY)`, `Realized (historical) volatility`), or `§2d` `Composite score (model.json)` / `Upside % (DCF/comps)` weighting must clear an out-of-sample gate before it is promoted to a **section-4 "Top 10 screener columns," "Default sort," or "Default filters."** Log how many lookbacks/thresholds were tried to pick it; an untracked search silently overfits the default view.
2. **Deflate historical risk stats for search effort.** `§2g Risk / sizing` → `Max drawdown (historical)` and any Sharpe/`Suggested position size (ATR-based)` derived from a swept ATR multiple or stop distance should be shown as *deflated* (haircut) values, tagged with the trial count and sample length, never as raw optimized figures.
3. **Validate the smart-money moat out-of-sample, don't cherry-pick.** The `§2e Smart-money` edge (`Congress trades`, `Insider trades (EDGAR)`, `Committee-conflict cross-tab`, `News sentiment`) must be tested via walk-forward / CSCV on held-out dates rather than by reporting the single best committee/window combination — otherwise the "moat" is just the max of many noisy cross-tabs.
4. **Record N system-wide.** Store the number of configurations behind every ranked column so a Sharpe/return figure in the default view is never presented without its trial count.

## Actionable takeaways
- Treat every backtested Sharpe/return in the screener as an upper bound; never a default-sort column until deflated and validated out-of-sample.
- Keep a running count N of factor/threshold variants tested per candidate metric, and demand a track record ≥ MinBTL (grows with `ln N`) before trusting it.
- Prefer CSCV/walk-forward evaluation over single best-in-sample selection; flag any factor whose in-sample-best ranks below its out-of-sample median (high PBO).
- Publish N and the deflated Sharpe next to any performance figure — a Sharpe without N is not a result.

## Open questions
- What N budget and MinBTL should gate promotion to a **section-4 default sort/column**, given only nightly free-tier history?
- Can CSCV/PBO run over the existing pipeline's date range, and is that range ≥ MinBTL for the smart-money cross-tabs?
- How to log trial counts for `§2d Composite score (model.json)`, whose upstream search history is opaque?
- Should the screener surface a per-metric "confidence/deflation" badge instead of raw Sharpe/drawdown?

## Sources
- Bailey, Borwein, López de Prado & Zhu, "Pseudo-Mathematics and Financial Charlatanism," *Notices of the AMS*, 2014 — https://scholarworks.wmich.edu/math_pubs/40/ (PDF: https://www.ams.org/notices/201405/rnoti-p458.pdf)
- Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest Overfitting" (CSCV/PBO) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
- Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551