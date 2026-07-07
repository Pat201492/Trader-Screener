# Quantitative Trading — Ernest Chan
**Source:** *Quantitative Trading: How to Build Your Own Algorithmic Trading Business*, 2nd ed. (Wiley, 2021) — Ernest P. Chan, PhD — https://books.google.com/books/about/Quantitative_Trading.html?id=CZALzgEACAAJ

## What it is
A practitioner playbook for taking a systematic strategy from idea to a running business: idea sourcing (Ch. 1–2), backtesting and performance measurement with its pitfalls (Ch. 3), business and execution setup (Ch. 4–5), money and risk management (Ch. 6), and special topics — mean-reversion vs. momentum, regime change, factor models, exits (Ch. 7). The 2nd edition adds Python/R backtests and machine-learning parameter optimization. Its throughline: a candidate signal is worthless until it survives an honest backtest and is sized by an explicit risk rule.

## Core concepts
- **Ranked-factor thinking.** A strategy is a rule that ranks a universe and holds the extremes; momentum and fundamental factors are blended into a composite score, then backtested as a portfolio equity curve.
- **Judge by risk-adjusted return, not return.** The two headline KPIs are the annualized Sharpe ratio and maximum drawdown (both depth and duration).
- **Backtest honesty.** Three biases silently inflate results — survivorship, look-ahead, and data-snooping — and guarding against them is the core discipline.
- **Sizing is a decision separate from signal.** Kelly / fixed-fractional sizing and leverage govern survival; a good signal badly sized still blows up.
- **A staged path to capital.** Backtest → out-of-sample → paper trade → small live → scale.

## Key metrics/signals it defines + how to read/compute them
- **Sharpe ratio** = annualized mean excess return ÷ annualized volatility. Chan's rule of thumb: a tradeable strategy typically clears Sharpe ≈ 1+; for a backtest Sharpe of 1 to be statistically distinguishable from 0 you need ~681 daily bars (~2.7 years).
- **Maximum drawdown & drawdown duration** — largest peak-to-trough equity decline and time to recover; sets the pain and capital you must endure.
- **Kelly optimal leverage** — for continuous (Gaussian) returns, f\* = mean return ÷ variance (μ/σ²); for a portfolio it uses the covariance matrix. Chan strongly recommends **half-Kelly** to blunt estimation error, computed on a rolling ~500-day window and re-estimated as conditions shift. Full Kelly maximizes long-run compound growth but produces intolerable drawdowns.
- **Fixed-fractional sizing** — risk a constant fraction of equity per position; simpler and more robust than full Kelly.
- **In-sample vs. out-of-sample split** — reserve ≥ 1/3 of history out-of-sample; sensitivity-test parameters instead of optimizing to a single peak.
- **Transaction costs / capacity** — subtract realistic commissions, slippage, and market impact before believing any Sharpe.

## How it maps to the Trader Screener
1. **§2g "Max drawdown (historical)"** is Chan's primary risk KPI — compute it alongside Sharpe on the equity curve of any ranked composite so the screener sorts on survivability, not just return.
2. **§2g "Suggested position size (ATR-based)"** operationalizes Chan's Kelly/fixed-fractional sizing: his f\* = μ/σ² scales inversely with variance, and ATR is the screener's practical volatility proxy, so a half-Kelly (or fixed-fractional) risk budget divided by ATR yields the share count.
3. **§2g "Stop distance (ATR multiple)"** is where Chan's variance-based risk becomes a per-trade exit — an ATR-multiple stop is the concrete implementation of his exit-strategy discipline (Ch. 7).
4. **Look-ahead & survivorship guardrails on every §2a/§2b indicator** — "Return 1/3/6/12-month", "Relative strength rank (vs universe)", "Moving averages (50/200d) + cross", "RSI", "MACD", "ATR / ATR%", "Realized (historical) volatility", and "Beta (vs SPY)" must use only data available *at the bar* (no forward-fill, point-in-time fundamentals).
5. **universe.json must retain delisted names.** A survivorship-clean universe is Chan's non-negotiable; screening only currently-listed tickers overstates momentum and composite backtests.
6. **§2d "Composite score (model.json)"** is exactly Chan's momentum/fundamentals composite-as-ranked-factor — validate it out-of-sample on Sharpe + max drawdown, and treat "Upside % (DCF/comps)" as point-in-time to avoid look-ahead.

## Actionable takeaways
- Attach Sharpe + max drawdown to the composite (§2d) before trusting any ranking.
- Point universe.json at a delisting-inclusive source; timestamp every §2a/§2b indicator to its bar.
- Populate §2g sizing/stops from half-Kelly (μ/σ²) translated through ATR; never full Kelly.
- Hold out ≥ 1/3 of history; paper-trade before live; always net out costs.

## Open questions
- Which delisting-aware data source feeds a survivorship-clean universe.json cheaply?
- Do we size by true Kelly (μ/σ² across the whole book) or the simpler ATR fixed-fractional column — and at what fraction?
- What look-back and re-estimation cadence for rolling drawdown / Sharpe / Kelly?
- Are point-in-time fundamentals available for §2d, or is some look-ahead unavoidable there?

## Sources
- [Google Books — *Quantitative Trading*, 2nd ed. (Chan, 2021)](https://books.google.com/books/about/Quantitative_Trading.html?id=CZALzgEACAAJ)
- [Wiley — product page, 2nd ed. (9781119800064)](https://www.wiley.com/en-us/Quantitative+Trading:+How+to+Build+Your+Own+Algorithmic+Trading+Business,+2nd+Edition-p-9781119800064)
- [O'Reilly — *Quantitative Trading*, 2nd ed. table of contents](https://www.oreilly.com/library/view/quantitative-trading-2nd/9781119800064/)
- [Ernest Chan — "Backtesting and Its Pitfalls" (epchan.com PDF)](https://epchan.com/img/links/Backtesting-and-its-Pitfalls.pdf)
- [Ernest Chan — "Kelly Formula Revisited" (epchan.blogspot.com)](http://epchan.blogspot.com/2009/02/kelly-formula-revisited.html)
- [Bookey — *Quantitative Trading* chapter summary](https://www.bookey.app/book/quantitative-trading)