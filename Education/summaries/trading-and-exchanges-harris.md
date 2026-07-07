# Trading and Exchanges — Market Microstructure for Practitioners (Larry Harris)
**Source:** *Trading and Exchanges: Market Microstructure for Practitioners* — Larry Harris — read from the full text (Oxford University Press, 2003).

## What it is
The canonical practitioner reference on the *business* of trading: who trades and why, how orders become executions, how prices are discovered, and what "liquidity" actually means. It is descriptive and institutional rather than mathematical, so its frameworks translate directly into screener vocabulary. Harris states the book's aim plainly in the liquidity chapter: "Understanding liquidity is one of the primary objectives of this book." The material builds from trader types and market structures up through dealers, spreads, value traders, volatility, and transaction-cost measurement.

## Core concepts
- **Trader taxonomy (Ch. 8).** Traders are "profit-motivated traders, utilitarian traders, or futile traders." *Utilitarian traders* (investors/borrowers, asset exchangers, hedgers, gamblers) "trade to obtain some benefit besides trading profits." *Profit-motivated* traders are speculators and dealers. Orthogonally, traders are *informed* or *uninformed* — informed traders "form reliable opinions about whether instruments are fundamentally undervalued or overvalued." Part III (Speculators) splits speculators into informed traders (Ch. 10), order anticipators/front-runners (Ch. 11), and bluffers (Ch. 12).
- **Market structures (Ch. 5–6).** "In quote-driven systems, dealers arrange most trades… In order-driven systems, all traders" arrange trades among themselves via order-precedence rules (limit-order books, continuous or call auctions); large size uses brokered/search markets. Most venues are hybrids.
- **Liquidity is multi-dimensional (Ch. 19).** "Liquidity is the ability to trade large size quickly at low cost when you want to trade… the most important characteristic of well-functioning markets." Harris warns "liquidity means different things to different people" — its dimensions are *immediacy* (speed — "one of several dimensions of liquidity"), the *cost/price of immediacy* (the bid/ask spread), *depth* (size available), and *resiliency* (Ch. 16.1.3: "When uninformed traders cannot change prices substantially, the market is resilient… Value traders make markets resilient by standing ready to trade when prices move away from fundamental values"). Liquidity is the outcome of a *bilateral search* — buyers seeking sellers and vice versa.
- **Dealers sell immediacy (Ch. 13).** "The liquidity service they sell—immediacy—is valuable to impatient traders." Value traders are "the ultimate suppliers of market liquidity… They trade when no one else will" (Ch. 16).
- **Spread components (Ch. 14) — a correction to the common gloss.** Harris breaks the spread into *two* components, not three: the *transaction-cost (transitory) component* — normal costs of doing business, which "also funds any monopoly profits… and any risk premium… for bearing inventory risk" (inventory risk is folded *in here*, causing bid/ask bounce) — and the *adverse-selection component*, "that part of the bid/ask spread that compensates dealers for the losses that they suffer when trading with well-informed traders."

## Key metrics/signals it defines + how to read/compute them
- **Bid/ask spread (Ch. 13–14).** "The difference between the ask and the bid." Narrow = "immediacy is cheap"; wide = expensive. It is "the price impatient traders pay for immediacy." Requires a 🔴 quote feed.
- **Quoted vs. realized spread (Ch. 13.2).** Quoted = ask − bid; *realized* = "the difference between the prices at which dealers actually buy and sell," usually smaller because dealers give price improvement and adjust quotes between trades.
- **Depth / resiliency.** Displayed size at/near the quote, and speed of price recovery after an imbalance — both 🔴 real-time-only.
- **Transaction-cost measurement (Ch. 21).** Costs are *explicit* (commissions, fees, taxes), *implicit* (spread paid + price impact), and *missed-trade opportunity* costs. The book's core estimator is the **price-benchmark method**: `Estimated Cost = TradeSize × TradeSign × (TradePrice − BenchmarkPrice)` (sign +1 buy, −1 sell). Costs are highest for frequent/large traders and in illiquid names, where "the price impacts of implementing them in large size may cause them to lose on net." Harris distinguishes these specific-trade benchmark methods from *econometric* methods used for whole-market average costs.
- **Volatility (Ch. 20).** *Fundamental volatility* (unanticipated value changes) vs. *transitory volatility* (uninformed trading activity); traders must separate them "to accurately predict future volatility, the profitability of dealing strategies, and transaction costs." Free proxy: realized vol = stdev(log returns) × √252.

## How it maps to the Trader Screener
1. **§2c Dollar volume (ADV × price)** is the correct 🟡 tradeability filter precisely because **§2c Bid/ask spread** and **§2c Market depth / book** are 🔴 (no quote feed). Harris's Ch. 21 point — implicit costs (spread + price impact) scale with size and blow up in illiquid names — means dollar volume is the honest free stand-in for "can I get size in and out without the price impact eating my edge."
2. **§2c Bid/ask spread** is Harris's *price-of-immediacy* dimension and **§2c Market depth / book** is his *depth* dimension (Ch. 13–14, 19). Documenting both as 🔴 tells v1 exactly which liquidity dimensions it is *not* measuring; the Ch. 21 price-benchmark cost formula is the reference for any later effective-cost work.
3. **§2e Congress trades**, **§2e Insider trades (EDGAR)**, and **§2e Committee-conflict cross-tab** are the *adverse-selection component* made observable. Harris's informed-trader theory (Ch. 10, 13–14) is the moat's basis: these ✅ free signals flag the very order flow that widens spreads — "uninformed traders lose to well-informed traders."
4. **§2b Realized (historical) volatility** and **§2f VIX** operationalize Ch. 20's fundamental-vs-transitory split — regime context for when liquidity thins and transaction costs spike.

## Actionable takeaways
- Ship **§2c Dollar volume (ADV × price)** as a P1 default column and hard liquidity filter — the highest-value 🟡 metric and the honest substitute for the missing quote feed.
- Treat **§2e** smart-money signals as the adverse-selection lens; Harris's informed-trader theory justifies a dedicated view. His blunt lesson: "Uninformed traders lose simply because they trade… you should minimize your trading."
- Label 🔴 spread/depth columns "not in v1," noting the Ch. 21 price-benchmark method as the reference, so the gap is explicit.
- For swing/position horizons, nightly 🟡 OHLCV + ✅ pipeline covers most needs; intraday 🔴 feeds are only forced by day trading.
- Correct the record: Harris's spread has **two** components (transaction-cost + adverse-selection), with inventory risk inside the first — not a separate three-way split.

## Open questions
- Would a benchmark-method effective-cost estimate from daily data add signal over raw dollar volume, or not enough to justify a 🟡 column?
- What dollar-volume floor cleanly separates "tradeable" from "cost-prohibitive" for the intended size?
- Should §2e adverse-selection intensity become its own scored dimension rather than raw event flags?

## Sources
- Larry Harris, *Trading and Exchanges: Market Microstructure for Practitioners* (Oxford University Press, 2003) — **primary source, summarized from the full text.**
- Chapters referenced: Ch. 5–6 (Market Structures / Order-driven Markets), Ch. 8 & Part III (Trader taxonomy / Speculators), Ch. 10–12 (Informed Traders, Order Anticipators, Bluffers), Ch. 13 (Dealers), Ch. 14 (Bid/Ask Spreads), Ch. 16 (Value Traders, incl. 16.1.3 Market Resiliency), Ch. 19 (Liquidity), Ch. 20 (Volatility), Ch. 21 (Liquidity and Transaction Cost Measurement).
