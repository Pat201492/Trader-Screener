# *Trading and Exchanges: Market Microstructure for Practitioners* — Detailed Summary

> **Larry Harris**, Oxford University Press, **2003** (sole edition; never revised). ISBN 0195144708.
> The standard practitioner text on market microstructure — *how* trading actually works, *who* trades, and *why*. Plain prose, minimal math. HFT-era specifics are dated; the conceptual core is timeless.
>
> Summary compiled 2026-06-27. Organized by the book's major themes (Harris's own chapter grouping). Relevance notes tie each part back to the **Trader Screener** Project spec.

---

## The one big idea
Trading is a **zero-sum game before costs, negative-sum after costs.** Every trade has two sides; one side is usually better-informed than the other. Harris's relentless question, repeated throughout the book:

> **"Why do you expect to profit from this trade? Who is on the other side, and why are they willing to lose?"**

If you can't name the trader you're taking money from — and why they'll keep showing up to lose — **you are the sucker.** Everything else in the book is machinery built around this question.

---

## Part I — Introduction: trading, instruments, and markets

**What trading is.** Markets do two jobs: **price discovery** (finding the value at which buyers and sellers clear) and **risk transfer / liquidity provision** (letting people exchange when they need to). Trading is not investing; it is the *act of exchanging*, and the microstructure determines how cheaply and fairly that exchange happens.

**Instruments.** Equities, debt, currencies, commodities, and derivatives (forwards, futures, options, swaps). Each exists because someone needs to move value through time, place, or risk-state.

**Markets vs. instruments.** A single instrument can trade in many market structures simultaneously (an exchange, a dealer network, a dark pool). Structure — not just the asset — determines your cost and execution quality.

> **Screener relevance:** the screener covers *instruments*; this part is the menu (equities/ETFs vs options/futures/forex/crypto) and the reminder that *where* a thing trades affects its liquidity column.

---

## Part II — Orders and market structures

### Orders (the atoms of trading)
- **Market order** — demands immediacy; executes now at the best available price. You **pay the spread** and bear market-impact cost. You *consume* liquidity.
- **Limit order** — offers to trade at a price or better; may not execute. You *supply* liquidity and (hope to) **earn the spread**, but you take two risks: **non-execution** (price runs away) and **adverse selection** (you only get filled when you're on the wrong side of new information).
- **Stop order** — dormant until a trigger price; then becomes a market (or limit) order. Used for loss-cutting and breakout entry. Note: stops *demand* liquidity exactly when liquidity is scarce (in a fast move), so they fill at bad prices.
- **Order properties** — validity (day, GTC), quantity (incl. hidden/iceberg), price and execution instructions (all-or-none, etc.).

**The core trade-off:** *immediacy vs. price.* Market orders buy certainty of execution at a worse price; limit orders chase a better price at the risk of no fill. This single trade-off underlies all execution strategy.

### Market structures
- **Quote-driven (dealer) markets** — dealers post bid/ask; you trade *against the dealer*. Dealer provides immediacy and earns the spread.
- **Order-driven (auction) markets** — buyers and sellers trade with each other via a limit order book. Two flavors:
  - **Continuous** — trades happen any time orders cross.
  - **Call (batch) auctions** — orders collect, then clear at one price at a set time (opens/closes).
- **Brokered markets** — brokers search for counterparties (big illiquid blocks, real estate).
- **Hybrid** — most real markets mix these.

**Order precedence rules** (how an order book decides who trades): **price priority first** (best price wins), then secondary rules — **time** (first in line), **display** (visible beats hidden), **size**. Understanding precedence tells you whether your limit order will actually get filled.

> **Screener relevance:** order/limit/stop mechanics define what a "liquidity" or "spread" column *means* and why a thin-volume stock is dangerous to trade — feeds §2c of the shortlist (ADV, dollar volume, spread).

---

## Part III — The players (who's in the market)

Harris's signature **taxonomy of traders.** Split first by *motive*:

### A. Utilitarian traders — trade for reasons *other than* trading profit
They accept expected trading losses because trading serves another purpose:
- **Investors** — move money through time (save now, spend later).
- **Borrowers** — the opposite; raise money now.
- **Hedgers** — offload unwanted real-world risk (the airline hedging jet fuel).
- **Asset exchangers** — need the asset itself (a currency to pay a bill).
- **Gamblers** — trade for entertainment/utility of the bet.
- **Cross-subsidizers / forced traders** — index funds, those trading for tax or liquidity reasons.

These traders are the **source of profits** for everyone else — they trade for needs, not edge.

### B. Profit-motivated traders — trade *to make money trading*
- **Informed traders** (make prices informative):
  - **Value traders** — estimate fundamental value, buy below / sell above. The deepest, most stabilizing liquidity source.
  - **News traders** — trade on new information faster than others.
  - **Arbitrageurs** — exploit price differences between related instruments; enforce the law of one price.
  - **Information-oriented technical traders** — infer information from price/volume patterns.
- **Dealers / market makers** — supply liquidity and immediacy; profit from the spread, not from direction.
- **Parasitic / order anticipators** (profit from *other traders' orders*, add no information):
  - **Front runners** — trade ahead of known large orders.
  - **Sentiment-oriented technical traders** — try to predict and ride other uninformed traders.
  - **Squeezers** — exploit traders who *must* trade (corner/short-squeeze).
- **Bluffers / manipulators** — push prices with deceptive orders to fool others.

**Brokers** (agents) sit alongside: they trade *for* clients. Harris details their conflicts of interest — soft dollars, payment for order flow, agency vs. principal — and why broker incentives don't always align with yours.

> **Screener relevance:** this taxonomy *is* the "who's on the other side" answer. Your **smart-money moat** (congress + insider data) is literally a tool for detecting *informed traders* (corporate insiders, well-connected politicians) — Harris explains exactly why following informed flow is valuable: they're the ones who profit, so piggybacking them is an edge. Strong justification for §2e of the shortlist.

---

## Part IV — Origins of liquidity, the spread, and volatility

### Why the bid/ask spread exists — its three components
Dealers don't quote a spread to be greedy; the spread compensates three real costs:
1. **Order-processing costs** — the plumbing (clearing, settlement, technology, the dealer's time).
2. **Inventory-holding costs** — the dealer takes the other side and is now exposed to price risk until they offload; they charge for bearing that risk.
3. **Adverse-selection costs** — the killer. Dealers *lose* to informed traders (who only trade when they know something). To survive, dealers widen the spread and recoup those losses from **uninformed** traders.

**Consequence:** spreads widen with information asymmetry and uncertainty, and narrow with competition and uninformed volume. A wide spread is a warning sign of either thin competition or high adverse-selection risk.

### Adverse selection & the winner's curse
If you get filled instantly and fully, ask *why someone was so eager to take your price* — maybe they know something. Dealers and limit-order traders constantly face this **winner's curse**: you transact most easily exactly when you're wrong.

### Liquidity — the central concept (Harris's four dimensions)
Liquidity is not one number. It has four parts:
- **Immediacy** — how fast you can trade.
- **Width** — the bid/ask spread (cost of immediacy).
- **Depth** — how much size you can trade without moving the price.
- **Resiliency** — how fast price recovers after a liquidity-demanding trade.

A "liquid" stock scores well on all four. Your screener's liquidity column should respect that one ADV number doesn't capture depth or resiliency.

### Volatility
Two kinds: **fundamental volatility** (real information arriving) and **transitory volatility** (bid/ask bounce, temporary order imbalances, liquidity demand). Microstructure adds noise to observed prices; not all volatility is "real."

> **Screener relevance:** directly specs §2b (volatility) and §2c (liquidity/spread). Harris's four-dimension liquidity model is the reason "dollar volume" (depth proxy) beats raw share volume as a tradeability filter.

---

## Part V — Transaction costs and performance

**Transaction costs** are explicit + implicit:
- **Explicit** — commissions, fees, taxes (easy to see, often small).
- **Implicit** — the spread, **market impact** (your own order moving the price), and **opportunity/delay cost** (the price moving while you wait). These dwarf commissions for large or impatient traders.

**Implementation shortfall** — Harris's preferred cost measure: the gap between the price when you *decided* to trade (the "paper" portfolio) and the price you *actually got*, including the cost of trades you failed to complete. It captures delay and missed-trade costs that simpler measures hide.

**VWAP** (volume-weighted average price) — a common benchmark, but Harris notes its limits (gameable, ignores opportunity cost).

**Why it matters:** since trading is negative-sum after costs, **minimizing transaction cost is often the single largest controllable edge** for most traders. You can't control the market; you can control how much you bleed getting in and out.

> **Screener relevance:** this is the argument for ADV / dollar-volume / spread filters — they're proxies for *what a trade will cost you*. A great signal on an illiquid name is worthless if impact eats the edge.

---

## Part VI — Market efficiency, regulation, and pathologies

- **Informative prices & the Grossman-Stiglitz paradox** — prices are informative *because* informed traders trade. But informed traders need **uninformed (liquidity) traders to hide behind** — if everyone were informed, no one could profit on information, so no one would gather it, and prices would stop being informative. Markets need a mix.
- **Market efficiency** — efficient markets reflect information; microstructure frictions determine *how fast and how fully*.
- **Bubbles, crashes, manipulation** — Harris covers price manipulation tactics, short squeezes, and why crashes cluster (liquidity evaporates exactly when everyone demands it at once).
- **Insider trading & regulation** — why it's restricted (it's the ultimate adverse selection; it drives liquidity traders away), and the regulatory architecture.
- **Block trading & the upstairs market** — how large orders are negotiated off the public book to avoid impact, and hidden/iceberg orders.

> **Screener relevance:** the insider-trading discussion is the academic backbone for treating insider/congress data as *informed-trader signal* — Harris explains both why it's powerful and why it's regulated.

---

## Harris's practical lessons (the takeaways traders quote)
1. **Know why you trade.** Have a concrete reason you expect to profit and who you profit *from*. No answer → you're the liquidity.
2. **If you don't know who the sucker is, it's you.** (The book's most famous line, paraphrased.)
3. **Minimize transaction costs** — the most reliable, controllable edge. Costs compound against you every round-trip.
4. **Use limit orders when patient, market orders when you need immediacy** — and price both decisions against non-execution and adverse-selection risk.
5. **Liquidity is multi-dimensional** — don't confuse a tight spread with depth, or volume with resiliency.
6. **Respect adverse selection** — easy fills are suspicious; the market accommodates you most when you're wrong.
7. **Information edges decay** — value-based edges are durable; speed/anticipation edges get competed away.

---

## How this maps to the Trader Screener (build implications)
- **The screener's job** is to help answer Harris's question — *who's informed, and can I ride them?* → lean hard into the **smart-money moat** (congress/insider/committee), which is literally informed-trader detection.
- **Liquidity & cost columns** (ADV, dollar volume, spread) aren't decoration — Harris shows they decide whether *any* signal is tradeable. Make them first-class filters.
- **Volatility columns** (ATR, realized vol, beta) follow Harris's fundamental-vs-transitory split; use them for sizing/stops, not as signals by themselves.
- **Don't pretend to be an execution venue.** Harris makes clear execution quality is its own deep discipline (brokers, order routing, impact). Your edge is *research/screening*, not order routing — consistent with [ARCHITECTURE.md](../ARCHITECTURE.md).

> **Next:** fill [SCREENER_SHORTLIST.md](../Project%20folder/SCREENER_SHORTLIST.md) using this vocabulary — §2c (liquidity) and §2e (smart-money) are where Harris most directly justifies your build.
