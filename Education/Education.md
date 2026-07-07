# Education — The Business of Trading: A Learning Path

> Goal: understand the *business* of trading financial instruments — the economics behind markets, the instruments themselves, the tools professionals use, and *why* people trade them. Curated toward current (2025–2026) material with dated real-world examples.
>
> Compiled 2026-06-27. Regulator + exchange links (SEC, CFTC, CME, Cboe) are the most durable — prioritize them. Commercial-blog links are flagged; vet for bias.

---

## 1. Fundamentals of Trading as a Business

What trading *is*: the continuous price-discovery and risk-transfer machine. Someone with risk they don't want (a hedger) pays someone willing to hold it (a speculator); a market maker stands in the middle quoting both sides and earning the spread for providing liquidity.

| Resource | Source | URL | Takeaway |
|---|---|---|---|
| Market microstructure (overview) | Wikipedia | https://en.wikipedia.org/wiki/Market_microstructure | The academic anchor: price formation, transaction cost, liquidity depth. |
| What is Financial Market Microstructure? | Bookmap | https://bookmap.com/blog/what-is-financial-market-microstructure | Practitioner intro to order books, bid/ask spread, depth-of-market. |
| Futures Fundamentals | CME Group | https://www.cmegroup.com/company/futures-fundamentals.html | Clearest "who trades and why" primer — risk transfer in the real economy. |
| Market Microstructure for Intra-Day Trading (Lehalle) | arXiv | https://arxiv.org/pdf/1302.4592 | Rigorous execution-focused treatment for the serious quant track. |

**Participant roles:**
- **Market makers** (Citadel Securities, Virtu, Jane Street, Susquehanna) — quote two-sided prices, earn the spread, provide liquidity.
- **Hedgers** (e.g. an airline hedging jet fuel) — use futures/options to offset real-world risk.
- **Speculators** (hedge funds, prop algos, retail) — take directional risk for return.
- **Institutions vs retail** — differ in size, information edge, execution quality, prime-brokerage access.

---

## 2. Instrument Classes — and Why People Trade Them

| Instrument | Best primer | Source | URL | Why trade it |
|---|---|---|---|---|
| **Options** | Introduction to Options / All About Options | CME Institute | https://www.cmegroup.com/education/courses/introduction-to-options | Leverage, defined-risk hedging, income (premium selling). |
| **Options (volatility)** | The Options Institute | Cboe | https://www.cboe.com/optionsinstitute/ | The exchange that lists SPX/VIX — authoritative on vol products. |
| **Futures** | CME Institute curriculum | CME Institute | https://www.cmegroup.com/education/courses | Standardized, leveraged exposure to commodities, rates, indices. |
| **ETFs** | Comprehensive Guide to ETFs (2nd ed., 2025) | CFA Institute RF | https://rpc.cfainstitute.org/sites/default/files/docs/research-reports/hill_rf_brief_2025_etfs-evolving_module-1_2ed_online.v2.pdf | US ETF assets >$10T; creation/redemption vs mutual funds. |
| **Crypto** | Cryptocurrencies (futures + education) | CME Group | https://www.cmegroup.com/markets/cryptocurrencies.html | Regulated BTC/ETH futures + Micro contracts; institutional on-ramp. |
| **Equities / Fixed income / Forex** | Category hubs | Investopedia | https://www.investopedia.com | Canonical plain-English glossary + "why trade" per asset. |

> **Before opening any leveraged account:** CFTC — [Learn to Trade Futures and Options Without Getting Scammed](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/learn_to_trade_without_scam.htm) and SEC [investor.gov](https://www.investor.gov).

> **Deep-dive:** [Derivatives — Types & The Signals People Trade Off Them](Derivatives_Types_and_Trading_Signals.md) ([PDF](Derivatives_Types_and_Trading_Signals.pdf)) — full taxonomy (forwards/futures/options/swaps) plus the options/volatility signal set (IV, IV Rank, skew, put/call, open interest, unusual activity, the Greeks, term structure, VIX).
>
> **Examples gallery:** [Derivatives Examples Gallery](Derivatives_Examples_Gallery.md) ([PDF](Derivatives_Examples_Gallery.pdf)) — visual catalog of ~14 strategies (single legs, spreads, straddle/strangle/condor/butterfly, covered call/collar) with payoff charts + key metrics, and the **Greeks** (delta/gamma/vega/theta) profiles professionals watch.
>
> **Tear sheet (firm-style):** [Instrument Information Sheet / Tear Sheet](Information_Sheet_Tearsheet.md) ([PDF](Information_Sheet_Tearsheet.pdf)) — the one-page desk brief format + **6 worked examples** (bull/bear spreads, producer collar, iron condor, long straddle, protective put), each ending in a specific **derivative recommendation** with payoff. Illustrative/educational. Doubles as the screener's per-instrument output spec.
>
> **How to read it:** [How to Read a Tear Sheet](How_to_Read_a_Tear_Sheet.md) ([PDF](How_to_Read_a_Tear_Sheet.pdf)) — line-by-line guide explaining what each block means and **why it matters** (why dollar-volume, beta, ATR, IV Rank, skew, positioning each earn their place), plus the 60-second pro scan.
>
> **Metrics field guide:** [Metrics Field Guide](Metrics_Field_Guide.md) ([PDF](Metrics_Field_Guide.pdf)) — every readout (price, history/sparkline, range, realized vol, IV Rank, trend, catalyst, moneyness/intrinsic, premium, breakeven, forward/fair-vol) with **what it means + how to trade off it**, and a step-by-step decision recipe combining them.
>
> **Play it:** Catan Options terminal game — trade calls/puts on the five Catan commodities (Brick/Lumber/Wool/Grain/Ore). [how to play](game/HOW_TO_PLAY.md).
> - [Plain edition](game/catan_options_game.py) — random walk; teaches mechanics (call/put, strike/moneyness, premium-as-max-loss, breakeven, IV pricing) **and** that trading with no edge is gambling.
> - [**Signals edition**](game/catan_options_signals.py) — adds market-news catalysts + an IV CHEAP/RICH tag, so reading the signal is your edge (catalyst = news, IV tag = IV Rank). Scores your edge trades vs coin flips. Run `python game/catan_options_signals.py`.

---

## 3. Tools of the Trade

| Tool / category | Source | URL | Takeaway |
|---|---|---|---|
| Best Advanced Trading Platforms (rankings) | StockBrokers.com | https://www.stockbrokers.com/guides/best-trading-platforms | IBKR, Schwab/thinkorswim, Tastytrade lead 2025. |
| IBKR vs thinkorswim (deep comparison) | TradeAlgo* | https://www.tradealgo.com/trading-guides/comparisons/interactive-brokers-vs-thinkorswim | IBKR: margin/global/API. thinkorswim: options analytics. |
| Charting platforms (top 15) | QuantVPS* | https://www.quantvps.com/blog/list-of-top-charting-platforms-for-trading | TradingView (Pine Script), thinkorswim (thinkScript), Bloomberg. |
| backtesting.py | Docs | https://kernc.github.io/backtesting.py/ | Lightest way to test a strategy on history — start here. |
| VectorBT | Docs | https://vectorbt.dev/ | Vectorized (Numba) — thousands of param combos in seconds. |
| Going Live with VectorBT in 2025 | Medium* | https://medium.com/@samuel.tinnerholm/from-backtest-to-live-going-live-with-vectorbt-in-2025-step-by-step-guide-681ff5e3376e | Backtest → live broker (Alpaca/IBKR). |
| Backtrader tutorial | QuantVPS* | https://www.quantvps.com/blog/backtrader-tutorial | Event-driven; `__init__`/`next`; pandas + live feeds. |
| Backtesting frameworks & biases | QuantStart | https://www.quantstart.com/articles/backtesting-systematic-trading-strategies-in-python-considerations-and-open-source-frameworks/ | Look-ahead bias, survivorship bias, framework trade-offs. |
| Event-Driven Backtesting (Part I) | QuantStart | https://www.quantstart.com/articles/Event-Driven-Backtesting-with-Python-Part-I/ | Build a realistic backtester vs vectorized shortcuts. |

> **Python stack:** `pandas`/`numpy` (data) · `yfinance` (free OHLCV) · `backtesting.py` / `backtrader` / `vectorbt` (backtest) · `Alpaca` / `ib_insync` (execution). yfinance is fine for learning; use a paid feed for live. **Note:** this is the same `yfinance` the Project screener already uses for fundamentals/prices — reuse it for personal backtests.

---

## 4. Strategy & Analysis

| Resource | Source | URL | Takeaway |
|---|---|---|---|
| Risk Management in Trading | QuantInsti | https://blog.quantinsti.com/trading-risk-management/ | Stops, diversification, drawdown control. |
| Position Sizing & Capital Protection | QuantVPS* | https://www.quantvps.com/blog/trading-risk-management | Position sizing > entry timing; protect capital first. |
| Quant vs Fundamental vs Technical | Wright Research* | https://www.wrightresearch.in/blog/guide-to-quant-investing-4-quantitative-investing-vs-fundamental-investing-vs-technical-investing/ | Clean framing of the three philosophies. |
| Mastering Position Sizing (Kelly, fixed-fractional) | QuantStrategy.io* | https://quantstrategy.io/blog/mastering-position-sizing-advanced-strategies-for-scaling/ | Fixed-fractional (1–2%/trade) vs Kelly; ATR sizing. |

> **Core rule to internalize:** risk a fixed small % (1–2%) of equity per trade; size by volatility (ATR) so dollar-risk stays constant; backtest *and* stress-test before deploying.

---

## 5. Recent Real-World Examples (2024–2026) — Each Teaches a Lesson

| Event | Date | Source | Lesson |
|---|---|---|---|
| **Yen carry-trade unwind** — Nikkei −12% (worst since 1987), BoJ hike + soft US jobs | **Aug 5, 2024** | [BIS Bulletin 90](https://www.bis.org/publ/bisbull90.pdf) | Crowded + leveraged funding trades unwind violently — and can dissipate within a week. |
| **DeepSeek shock** — Nvidia −17%, ~$600B cap erased on a cheap Chinese AI model | **Jan 27, 2025** | [CNBC](https://www.cnbc.com/2025/01/27/nvidia-falls-10percent-in-premarket-trading-as-chinas-deepseek-triggers-global-tech-sell-off.html) | Concentrated narrative trades carry single-headline risk. |
| **"Liberation Day" tariff crash** — US mkt −12.4%, VIX peaked 52.33 (Apr 8), then +9.5% on Apr 9 pause | **Apr 2–9, 2025** | [Wikipedia](https://en.wikipedia.org/wiki/2025_stock_market_crash) | Best and worst days cluster; being out for the rebound is catastrophic. |
| 8 Lessons from 2025 Turbulence | 2025 | [Morningstar](https://www.morningstar.com/portfolios/8-lessons-investors-market-turbulence-2025) | True downside risk is often unknowable; position to survive. |
| Spring 2025 vol (macro framing) | Jun 2025 | [St. Louis Fed](https://www.stlouisfed.org/on-the-economy/2025/jun/financial-market-volatility-spring-2025) | Central-bank-grade breakdown of the tariff-era vol regime. |
| **Bitcoin cycle break** — spot ETFs (Jan 2024) front-ran halving (Apr 2024); ATH **$126,198 Oct 6, 2025** | 2024–2025 | [Fidelity](https://www.fidelity.com/learning-center/trading-investing/four-year-bitcoin-and-crypto-cycles) · [CNBC](https://www.cnbc.com/2025/08/08/bitcoin-btc-price-cycle-might-be-breaking.html) | ETF flows + institutions now drive crypto more than the 4-year halving. Old patterns break when structure changes. |

---

## 6. Structured Learning Path (Beginner → Advanced)

1. **Literacy (free, regulator-backed)** — SEC [investor.gov](https://www.investor.gov) → CFTC anti-scam guide → CME Futures Fundamentals. Learn the instruments and how *not* to get scammed before risking money.
2. **Mechanics & one asset class** — CME Institute free courses (options/futures); Cboe Options Institute (vol); Investopedia (equities/forex/fixed income).
3. **Strategy & risk** — QuantInsti risk-management + position sizing. Pick a style: technical (day/swing), fundamental (value), or quant (systematic).
4. **Build & backtest** — Python (pandas + yfinance) → backtesting.py → backtrader/vectorbt. Study QuantStart on backtest biases.
5. **Advanced/quant** — QuantStart event-driven series; Ernie Chan's books; arXiv microstructure. Paper-trade, then go live small.

**Books (reputable canon):**
- *Trading and Exchanges: Market Microstructure for Practitioners* — Larry Harris (Oxford, 2003) — **start here for the business of trading**; how/who/why markets work. Detailed summary: [Trading_and_Exchanges_Harris_Summary.md](Trading_and_Exchanges_Harris_Summary.md) · full chapter breakdown PDF: [Trading_and_Exchanges_Harris_Detailed.pdf](Trading_and_Exchanges_Harris_Detailed.pdf)
- *Trading in the Zone* — Mark Douglas (psychology; read first)
- *Reminiscences of a Stock Operator* — Edwin Lefèvre (behavioral lessons)
- *Quantitative Trading* / *Algorithmic Trading* — Ernest Chan (systematic)
- *Options, Futures, and Other Derivatives* — John Hull (derivatives standard)

**Newsletters / ongoing:** Morningstar & Bloomberg/FT (context); QuantStart & QuantInsti (systematic); Cboe/CME research (derivatives).

\* Commercial blog — useful and current, but vet for bias. Reputable anchors: QuantStart, QuantInsti, BIS, St. Louis Fed, CFA Institute, Morningstar, CME, Cboe, SEC, CFTC.

---

## 7. Digested Summaries (repo)

Claude-digested one-pagers for the highest-value resources — each maps the resource to specific `Project folder/SCREENER_SHORTLIST.md` metrics. Filed from issues #20–#31; full files in [`summaries/`](summaries/). The 4 books also have long **Detailed »** companions (chapter walkthroughs, quotes, formulas).

**Business of trading & microstructure**
- [Trading and Exchanges — Harris](summaries/trading-and-exchanges-harris.md) — liquidity's 4 dimensions, the 2 spread components, why dollar-volume is the right free tradeability proxy (#20). [**Detailed »**](summaries/trading-and-exchanges-harris-detailed.md)

**Options & volatility**
- [IV Rank & IV Percentile — OIC/OCC](summaries/iv-rank-iv-percentile-oic.md) — the cross-name IV normalizer + skew/put-call, with formulas and the history-accrual caveat (#21).
- [Cboe VIX — methodology & term structure](summaries/cboe-vix-index.md) — model-free 30-day IV, regime buckets, contango/backwardation as a stress flag (#29).

**Instruments beyond equities**
- [CME — Introduction to Futures](summaries/cme-introduction-to-futures.md) — multiplier/tick, margin, volume+OI, roll-yield gotchas in stitched series (#22).
- [CFA Institute — Guide to ETFs](summaries/cfa-etf-guide.md) — creation/redemption, why ETF liquidity = underlying basket not screen ADV (#23).

**Quant & backtesting**
- [Quantitative Trading — Chan](summaries/quantitative-trading-chan.md) — ranked factors judged by Sharpe + max drawdown; look-ahead/survivorship guardrails; Kelly sizing (#24). [**Detailed »**](summaries/quantitative-trading-chan-detailed.md)
- [Backtest Overfitting — Bailey/Borwein/López de Prado/Zhu](summaries/backtest-overfitting-pseudo-mathematics.md) — a Sharpe without its trial count is meaningless; gate factors before they become defaults (#25).

**Risk management & psychology**
- [Risk Management in Trading — QuantInsti](summaries/risk-management-quantinsti.md) — fixed-fractional 1–2%, size backwards from an ATR stop, 50-losses-to-ruin (#26).
- [Trading in the Zone — Douglas](summaries/trading-in-the-zone-douglas.md) — the discipline layer: think in probabilities so the risk columns get obeyed (#27). [**Detailed »**](summaries/trading-in-the-zone-douglas-detailed.md)

**Macro & regime**
- [The Yield Curve as a Leading Indicator — NY Fed](summaries/yield-curve-leading-indicator-nyfed.md) — 10Y–3M spread → 12-month recession-probability regime flag from FRED (#28).

**Screener-factor / ranking methodology**
- [Fama-French / French Data Library](summaries/fama-french-data-library.md) — the cross-sectional recipe: rank → NYSE-breakpoint buckets; momentum's skip-a-month rule (#30).
- [The Magic Formula — Greenblatt](summaries/greenblatt-magic-formula.md) — the two-factor rank-and-combine composite; the closest analog to model.json (#31). [**Detailed »**](summaries/greenblatt-magic-formula-detailed.md)

---

## ⚠️ Open Items
- [ ] Decide whether Education stays a static reading list or becomes a **living feed** (auto-pull new articles — would share the news/ingest infra from the Project pipeline).
- [ ] If living: define refresh cadence + which sources to scrape vs RSS vs API.
- [ ] Tie example events to the metrics the **Project** screener tracks (e.g. show VIX spike on the macro tab when teaching tail risk) — reuse FRED data already collected.
- [ ] Confirm scope: pure self-education vs user-facing education tab in the app.
- [ ] Vet every commercial-blog link before surfacing to end users.
