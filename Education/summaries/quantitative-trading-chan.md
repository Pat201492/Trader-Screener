# Quantitative Trading — Ernest Chan
**Source:** *Quantitative Trading: How to Build Your Own Algorithmic Trading Business*, Ernest P. Chan, PhD — read from the full text (Wiley; 2nd Edition, © 2021, ISBN 9781119800064).

## What it is
A practitioner playbook for taking a systematic strategy from idea to a running business: idea sourcing (Ch. 1–2), backtesting and honest performance measurement (Ch. 3), business and execution setup (Ch. 4–5), money and risk management (Ch. 6), and special topics — mean-reversion vs. momentum, regime change, cointegration, factor models, exits (Ch. 7). The 2nd edition adds Python/R/MATLAB code and a machine-learning "Conditional Parameter Optimization" method. Its throughline: "any strategy that has a Sharpe ratio of less than 1 is not suitable as a stand-alone strategy" (Ch. 2), and no candidate signal is real until it survives a bias-free, cost-inclusive backtest and is sized by an explicit risk rule.

## Core concepts
- **Judge by risk-adjusted return.** Chan's three standard measures are "the Sharpe ratio, maximum drawdown, and MAR ratio" (Ch. 3).
- **Backtest honesty.** Three biases silently inflate results — survivorship, look-ahead, and data-snooping — and guarding against them is the discipline. "Simple models are often the ones that will stand the test of time" (Ch. 2).
- **Costs are decisive.** "No backtest performance is realistic without incorporating transaction costs" (Ch. 3).
- **Sizing is separate from signal.** Kelly leverage governs survival; "risk always decreases long-term growth rate" (Ch. 6).
- **Regimes shift.** Financial series are nonstationary, so weight recent performance and match strategy type to regime (Ch. 2, Ch. 7).

## Key metrics/signals it defines + how to read/compute them
- **Sharpe ratio** — annualized mean excess return ÷ annualized volatility (a special case of the information ratio). Rules of thumb (Ch. 2): profitable almost every month → Sharpe typically > 2; almost every day → > 3; infrequent traders score low.
- **Statistical significance of Sharpe** (Ch. 3): to be 95% confident true Sharpe ≥ 0 you need a backtest Sharpe of 1 over **681 data points (2.71 years daily)**; a backtest Sharpe of 2 needs only 174 points; to be confident true Sharpe ≥ 1 you need backtest Sharpe ≥ 1.5 over 2,739 points (10.87 years).
- **Maximum drawdown & drawdown duration** — largest peak-to-trough equity decline below the high watermark, and the longest time to recover (Ch. 2–3; Example 3.4 = 10.53% max DD, 497-day duration).
- **MAR ratio** = CAGR ÷ maximum drawdown (Ch. 3).
- **Kelly optimal leverage** (Ch. 6): full vector form F\* = C⁻¹M (inverse covariance matrix × mean-return vector); for independent strategies it collapses to **f = m/σ² (mean ÷ variance)**. SPY example: excess return 7.231%, σ 16.91% → f = 0.07231/0.1691² = 2.528. Chan urges **half-Kelly** "for safety" against estimation error and non-Gaussian returns, using "the smaller of the half-Kelly leverage and" a stress-survivable cap, re-estimated continuously on a rolling lookback.
- **Transaction costs** — commission + liquidity/market impact + slippage; one worked case falls from Sharpe ≈ 3 to ≈ –3 after just 1 basis point.

## How it maps to the Trader Screener
1. **§2g "Max drawdown (historical)"** is one of Chan's three headline risk measures — compute it (plus a MAR ratio, CAGR/maxDD) alongside Sharpe on the equity curve of any ranked composite so the screener sorts on survivability, not raw return.
2. **§2g "Suggested position size (ATR-based)"** operationalizes Kelly f = m/σ²: risk scales inversely with variance, and ATR (§2b "ATR / ATR%") is the screener's practical volatility proxy — divide a **half-Kelly** risk budget by ATR for share count. Never full Kelly.
3. **§2g "Stop distance (ATR multiple)"** implements Chan's exit discipline (Ch. 7), but with his caveat: "For stop loss to be beneficial, we must believe that we are in a momentum, or trending, regime" — so gate ATR stops on §2a trend signals ("Moving averages (50/200d) + cross") rather than applying them to mean-reverting names.
4. **Look-ahead & survivorship guardrails on every §2a/§2b indicator** — "Return 1/3/6/12-month", "Relative strength rank (vs universe)", MA cross, "RSI", "MACD", "ATR / ATR%", "Realized (historical) volatility", "Beta (vs SPY)" must be computed on data "up to the close of the previous trading period only" (lag every signal), and **universe.json must retain delisted/merged/bankrupt names** (point-in-time) or momentum backtests inflate. Use Chan's A-vs-B truncation test to catch subtle leakage.
5. **§2d "Composite score (model.json)"** is Chan's ranked-factor composite — validate it out-of-sample (older = training, recent = test; test-set length set by the same 681-point rule, **not** a fixed 1/3). Treat **§2d "Upside % (DCF/comps)"** as point-in-time: fundamental databases carry "embedded look-ahead bias" from restated financials, and a value bent maximizes survivorship inflation.
6. **§2f macro (VIX / rates)** doubles as Chan's regime filter — condition rankings on recent-data performance given nonstationarity.

## Actionable takeaways
- Attach Sharpe + max drawdown + MAR to §2d before trusting any ranking; require Sharpe ≥ 1 and adequate sample length.
- Lag every §2a/§2b indicator to the prior close; point universe.json at a delisting-inclusive (point-in-time) source; run the A/B truncation check.
- Size §2g from half-Kelly (m/σ²) translated through ATR; gate ATR stops on trend/regime.
- Split history older→recent, hold out per the sample-size rule, paper-trade, and always net out realistic costs.

## Open questions
- Which delisting-aware source feeds a survivorship-clean universe.json cheaply?
- Do we size by true Kelly (m/σ² across the whole book) or the simpler ATR fixed-fractional column — and at what fraction?
- Are point-in-time fundamentals available for §2d, or is some look-ahead unavoidable in DCF inputs?
- What rolling lookback and re-estimation cadence for drawdown / Sharpe / Kelly, and how do we detect regime shifts?

## Sources
- **Primary:** Ernest P. Chan, *Quantitative Trading: How to Build Your Own Algorithmic Trading Business*, 2nd Edition, John Wiley & Sons, © 2021 (ISBN 9781119800064). Summarized directly from the full text.
- Chapters referenced: Ch. 2 (Fishing for Ideas — Sharpe rules of thumb, drawdown, survivorship/data-snooping intros); Ch. 3 (Backtesting — performance measurement, MAR ratio, statistical significance of Sharpe, look-ahead/survivorship/data-snooping pitfalls, out-of-sample split, transaction costs); Ch. 6 (Money and Risk Management — Kelly formula, half-Kelly, growth rate, stop-loss); Ch. 7 (Special Topics — mean-reversion vs. momentum, regime change, exit strategy).
