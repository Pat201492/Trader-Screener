# Catan Options — How to Play

A tiny terminal game that teaches options trading using the five Settlers-of-Catan commodities: **Brick, Lumber, Wool, Grain, Ore**. Educational only — not investment advice.

## Run it
```
cd Education/game
python catan_options_game.py
```
No dependencies (pure standard library).

**Desktop shortcut (Windows):** run once to drop a "Catan Options" icon on your Desktop that launches the signals game:
```
powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1
```
(or double-click `run_game.bat` in this folder). The shortcut runs the repo script, so updates apply automatically.

**Scorecard:** every game ends with a scorecard — net P/L & ROI, win rate, best/worst trade, your **edge trades vs coin-flip trades**, and a letter grade with tailored feedback.

## The idea
You start with **100 gold**. Over **10 seasons** you buy options on commodities; each season prices move and your option settles.

- **Call** = right to buy at the strike → profits if price goes **up** past it.
- **Put** = right to sell at the strike → profits if price goes **down** past it.
- You pay a **premium** up front — that is the **most you can lose** (defined risk).
- **Profit at expiry = payoff − premium.**
- **Breakeven:** call needs price > strike + premium; put needs price < strike − premium.

## What it teaches
| Concept | How the game shows it |
|---|---|
| Call vs put | pick direction each season |
| Strike & moneyness | three strikes tagged ITM / ATM / OTM |
| Premium = max loss | OTM expiries print "lost the premium (max loss)" |
| Breakeven | shown at purchase; ITM-but-losing trades flagged |
| **Implied volatility** | Brick/Ore (high vol) options cost **more** than Grain (low vol) — same idea as IV: you pay more when a bigger move is priced in |

## Tips
- Compare premiums across commodities — high-volatility names (Brick 45%, Ore 40%) are pricier. That cost is the "implied volatility" tax.
- Buying options needs a move **bigger than the premium** to win — being right on direction isn't enough if the move is small.
- OTM options are cheap but usually expire worthless; ITM options cost more but start with intrinsic value.

## Two editions

### `catan_options_game.py` — plain (random walk)
Prices move randomly with **no information**. Every trade is a coin flip, so over time the premium bleeds you. **That's the lesson:** trading with no edge is gambling.

### `catan_options_signals.py` — signals edition (recommended)
A **call/put reference banner stays at the top** of every turn. Each turn is **one month** and **real dates advance** (e.g. 2026-01 → 2026-02 …).

**Real-world-shaped data.** Every commodity is modeled after a real analog, so its **5-year history has a characteristic shape** (the shape, not real prices):

| Commodity | Analog | Shape |
|---|---|---|
| Grain | agricultural grain | strong annual **seasonality**, mean-reverting |
| Lumber | lumber | **boom/bust** cyclical, momentum, high vol |
| Brick | construction block | steady **up-trend**, low vol |
| Wool | soft commodity | mild seasonality, slow trends |
| Ore | industrial metal | cyclical, trending, high vol, supply shocks |

**What you read each month** (shown per commodity): current price, a **5-year sparkline**, the 5y range, **realized volatility**, **IV Rank** (how high its vol is vs its own 5y history → CHEAP/FAIR/RICH), and a **trend arrow**. Premiums are priced off each commodity's realized vol, so RICH-IV options genuinely cost more.

**Two ways to trade (the action menu):**
- **Buy premium** (long call/put) — cheap, max loss = premium. Best when **IV is CHEAP + a catalyst** is coming.
- **Sell premium** — you *collect* premium and win if the move **doesn't** happen. Best when **IV is RICH**. Choices:
  - **Cash-secured put** — bullish/neutral income; needs strike×size as collateral.
  - **Covered call** — buys the stock + sells a call against it; caps upside for income.
  - **Bull put / bear call credit spread** — defined-risk; capped profit and capped loss.
  Selling requires capital to back the risk (the game enforces a collateral check).

**Your edge:** **buy** when IV is CHEAP + a catalyst (bullish→call, bearish→put); **sell** when IV is RICH (and don't sell *against* a catalyst). No catalyst + fair IV = sit out. Options carry an IV premium scaled to IV Rank, so this buy-cheap / sell-rich rule is genuinely +EV in the sim (verified by Monte Carlo), not just flavor.

The end screen scores your **edge trades** separately so information visibly beats luck:
```
Edge trades (with catalyst, cheap/fair IV): 3   net P/L +53.10
```
Real-world parallel: sparkline = price chart; realized vol & IV Rank = volatility signals; catalyst = news/earnings/supply data. The Trader Screener's job is to surface all of these so your decisions have an edge.

```
python catan_options_signals.py
```

## Stats reference
For exact derivations, ranges, and what each value's position implies (rVol, IV Rank, implied vol, forward, premium, breakeven), see **[STATS_EXPLAINED.md](STATS_EXPLAINED.md)**.

## Pricing note
Premiums use Black-Scholes (r=0, one season = 0.25y), so they behave like real option prices: higher volatility, more time, and being in-the-money all raise the premium. Prices move by a lognormal random walk scaled by each commodity's volatility.

Related learning: [Derivatives Examples Gallery](../Derivatives_Examples_Gallery.md) · [Types & Signals](../Derivatives_Types_and_Trading_Signals.md).
