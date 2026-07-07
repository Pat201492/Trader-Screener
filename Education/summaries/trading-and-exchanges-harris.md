# Trading and Exchanges — Market Microstructure for Practitioners (Larry Harris)
**Source:** *Trading and Exchanges: Market Microstructure for Practitioners* — Larry Harris (Oxford University Press, 2003) — https://global.oup.com/academic/product/trading-and-exchanges-9780195144703

## What it is
The canonical practitioner reference on the *business* of trading: who trades and why, how orders become executions, how prices are discovered, and what "liquidity" actually means. Its ~600 pages run seven parts — Structure of Trading, Benefits of Trade, Speculators, Liquidity Suppliers, Origins of Liquidity and Volatility, Evaluation and Prediction, and Market Structures — building from order types and matching engines up to transaction-cost measurement and crashes. It is descriptive and institutional rather than mathematical, which makes its frameworks directly usable as screener design vocabulary.

## Core concepts
- **Trader taxonomy.** Two roots: *utilitarian traders* (invest/borrow across time, hedge, exchange assets, gamble) trade for reasons beyond profit; *profit-motivated traders* split into speculators and liquidity suppliers. Speculators divide into *informed/value traders* (trade on fundamental value) and *parasitic order anticipators* (front-runners, bluffers) who profit from others' order flow. Liquidity suppliers are *dealers/market makers* and passive *value-motivated traders* posting limit orders away from the quote.
- **Market structures.** *Order-driven* (limit-order books, continuous or call auctions) vs *quote-driven* (dealers post bid/ask) vs *brokered* (search markets for illiquid size). Most real venues are hybrids.
- **Four dimensions of liquidity:** *immediacy* (how fast a given size fills), *width* (the bid/ask spread — cost per share), *depth* (quantity available at or near the quote), and *resiliency* (how quickly price/book recover after a liquidity shock). Harris's shorthand: liquidity is the ability to trade large size quickly at low cost when you want to.
- **Spread components.** The bid/ask spread compensates dealers for three costs: *order-processing* (fixed handling/operational), *inventory-holding* (risk of carrying an unwanted position), and *adverse selection* (systematic losses to better-informed counterparties). The adverse-selection piece is the "informed-trading tax" that widens spreads in names where smart money is active.

## Key metrics/signals it defines + how to read/compute them
- **Quoted spread** = ask − bid; **% spread** = spread ÷ midpoint (the *width* dimension). Requires a 🔴 quote feed.
- **Effective spread** = 2 × |trade price − midpoint at order arrival|; captures what you actually paid, including price improvement or paying through the book.
- **Depth** = displayed size at best quotes / through the book; **resiliency** = refill speed after a shock. Both are 🔴 real-time-only.
- **Transaction-cost measurement (Ch. 21):** implementation shortfall (paper vs realized return) and VWAP benchmarking decompose cost into temporary market impact and permanent (information) impact. Ch. 21 also surveys estimators that back *effective spreads* out of trade-price series (serial-covariance / Roll-type methods) when quotes are unavailable — the bridge to 🟡 daily data.
- **Free proxies:** ADV = mean(volume, N); dollar volume = ADV × price; realized volatility = stdev(log returns) × √252.

## How it maps to the Trader Screener
1. **§2c Dollar volume (ADV × price)** is the right 🟡 tradeability filter precisely because §2c *Bid/ask spread* and §2c *Market depth / book* are 🔴 (no quote feed). Harris shows width and depth co-move with turnover: high dollar-volume names carry tight spreads and deep books, so ADV × price is a monotone stand-in for "can I get size in and out cheaply" without intraday quotes. It also flags the opposite end — thin names where effective costs dwarf the modeled edge.
2. **§2c Bid/ask spread + Market depth / book** are the width and depth dimensions themselves; documenting them as 🔴 (and Ch. 21's Roll-type close-to-close spread estimator as the 🟡 approximation) tells v1 exactly what it is *not* measuring and how to fake it nightly.
3. **§2e Congress trades (recent buy/sell)**, **§2e Insider trades (EDGAR)**, and **§2e Committee-conflict cross-tab** are the *adverse-selection component* made observable. Harris's informed-trader framework is the theory behind the moat: these ✅ free signals identify the very order flow that widens spreads, so §2e is a direct edge, not a curiosity.
4. **§2b Realized (historical) volatility** and **§2f VIX** operationalize Harris's Ch. 20 volatility / resiliency link — regime context for when liquidity evaporates and transaction costs spike.

## Actionable takeaways
- Ship **§2c Dollar volume (ADV × price)** as a P1 default column and hard liquidity filter; it is the highest-value 🟡 metric and the honest substitute for the missing quote feed.
- Treat **§2e** smart-money signals as the adverse-selection lens — a dedicated view is justified by Harris's informed-trader theory.
- Label 🔴 spread/depth columns as "not in v1" with the Ch. 21 estimator noted, so the gap is explicit rather than silent.
- For swing/position horizons, nightly 🟡 OHLCV + ✅ pipeline covers ~80%; intraday 🔴 feeds are only forced by day trading.

## Open questions
- Does a Roll/serial-covariance **effective-spread estimate from daily closes** add enough signal over raw dollar volume to be worth a 🟡 column?
- What dollar-volume floor cleanly separates "tradeable" from "cost-prohibitive" for the intended size?
- Should §2e adverse-selection intensity be surfaced as its own scored dimension rather than raw event flags?

## Sources
- https://global.oup.com/academic/product/trading-and-exchanges-9780195144703
- https://www.turtletrader.com/larry-harris-review/
- https://www.acsu.buffalo.edu/~keechung/MGF743/Readings/Trading-Exchanges-Market-Microstructure-Practitioners%20Draft%20Copy.pdf
- https://msbfile03.usc.edu/digitalmeasures/lharris/intellcont/Harris%20WFE%20Chapter%20Market%20Microstructure%20and%20Exchanges-1.doc
- https://www.bookey.app/book/trading-and-exchanges
- https://books.google.com/books/about/Trading_and_Exchanges.html?id=xNfnCwAAQBAJ