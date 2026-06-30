# Catan Options — How to Play

A tiny terminal game that teaches options trading using the five Settlers-of-Catan commodities: **Brick, Lumber, Wool, Grain, Ore**. Educational only — not investment advice.

## Run it
```
cd Education/game
python catan_options_game.py
```
No dependencies (pure standard library).

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

## Pricing note
Premiums use Black-Scholes (r=0, one season = 0.25y), so they behave like real option prices: higher volatility, more time, and being in-the-money all raise the premium. Prices move by a lognormal random walk scaled by each commodity's volatility.

Related learning: [Derivatives Examples Gallery](../Derivatives_Examples_Gallery.md) · [Types & Signals](../Derivatives_Types_and_Trading_Signals.md).
