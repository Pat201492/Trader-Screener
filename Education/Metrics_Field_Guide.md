# Metrics Field Guide — What Every Readout Means & How to Trade Off It

> Plain-English guide to the metrics you see on the game board, the buy screen, and a real trading terminal. For each: **what it is → why it matters → how to actually trade off it → game vs. real difference.** Then a step-by-step recipe that combines them into a decision.

> Educational only — not investment advice.

---

# PART A — THE COMMODITY (underlying) READOUTS

## 1. Price (spot)
- **What:** the current market price of the commodity/stock.
- **Why it matters:** the reference everything else keys off — strikes, moneyness, breakevens are all measured *relative to spot*.
- **How to trade off it:** by itself, almost useless. Price alone tells you nothing about direction or value — you need it *in context* (vs the range, the trend, the catalyst). Never trade on price level alone ("it's cheap!" is not a thesis).
- **Game vs real:** identical concept.

## 2. 5-year history / sparkline (the chart)
- **What:** the price path over time. The sparkline (`▁▂▅▇█`) compresses 5 years into a glance.
- **Why it matters:** it shows **trend, seasonality, and regime**. A steadily rising line (Brick) is a different animal from a boom/bust sawtooth (Lumber) or a seasonal cycle (Grain). The chart is the single richest free signal.
- **How to trade off it:**
  - **Trend up + pullback to support** → favorable spot to buy calls (buy the dip in an uptrend).
  - **Repeating seasonal pattern** → anticipate the cycle (e.g., grain weak after harvest, firm into planting).
  - **Near the top of its range after a vertical spike** → don't chase; spikes mean-revert.
  - **Volatile, jumpy history** → expect fat tails; size smaller, prefer defined-risk.
- **Game vs real:** real charts add volume, indicators (RSI, moving averages), and multiple timeframes. Same reading skill.

## 3. 5-year (or 52-week) range — low / high
- **What:** the lowest and highest price over the window.
- **Why it matters:** instant **context for "cheap" vs "expensive."** Price near the low = potential value or a falling knife; near the high = strength/momentum or overextension.
- **How to trade off it:** use range edges as **support/resistance** — natural places for stops (just beyond a level) and targets (into the opposite level). "% of range" tells you how much room is left before resistance caps a move.
- **Game vs real:** real desks watch the **52-week** range most; same idea, shorter window.

## 4. Realized volatility (rVol)
- **What:** how much the price has *actually* moved (annualized standard deviation of returns).
- **Why it matters:** it's the **baseline for risk and for option pricing.** High rVol = bigger swings = wider stops needed, larger position risk, and more expensive options. It's the yardstick that **IV is compared against**.
- **How to trade off it:**
  - **Size by volatility:** risk a fixed % of capital; a high-rVol name needs a *smaller* position and *wider* stop so a normal wiggle doesn't stop you out (the ATR idea).
  - **Compare to IV:** if options' implied vol >> realized vol, options are pricey (favor selling); if implied < realized, they're cheap (favor buying).
- **Game vs real:** real terminals show both realized *and* implied vol; the game uses rVol as the IV proxy.

## 5. Trend arrow (↑ → ↓)
- **What:** direction of the recent move (here, last ~6 months).
- **Why it matters:** **"the trend is your friend."** Most strategies work better aligned with the prevailing trend than fighting it.
- **How to trade off it:** prefer **calls in an uptrend, puts in a downtrend.** A bullish catalyst into an *existing* uptrend is higher-confidence than one fighting a downtrend. Counter-trend trades need a stronger catalyst and tighter risk.
- **Game vs real:** real traders define trend with moving averages (price above rising 50/200-day) and relative strength.

---

# PART B — THE VOLATILITY / SENTIMENT SIGNALS

## 6. IV Rank — CHEAP / FAIR / RICH (0–100)
- **What:** where today's implied volatility sits within its **own past-year range.** 0 = cheapest IV all year, 100 = most expensive. (The game derives it from each commodity's 5-year vol history.)
- **Why it matters:** this is **the pivot metric for options.** Raw IV is meaningless across names (a biotech's 60% vs a utility's 18%). IV Rank normalizes it: *is volatility expensive for THIS thing, right now?* It decides whether you should **buy or sell** premium.
- **How to trade off it:**
  - **CHEAP IV (low rank):** options underpriced → **buy** premium. Long calls/puts, straddles before a catalyst.
  - **RICH IV (high rank):** options expensive → **sell** premium or use spreads. Covered calls, cash-secured puts, iron condors, credit spreads. Buying outright here usually overpays and gets crushed.
  - **Caution:** high IV Rank often means a move is **already priced in** — the easy edge is gone.
- **Game vs real:** real value; in markets it's literally called *IV Rank / IV percentile* on every options platform.

## 7. The catalyst / market news
- **What:** an event that changes supply/demand or expectations (earnings, supply shock, weather, policy). In the game: "Drought ruins the harvest — GRAIN scarce."
- **Why it matters:** a catalyst is the **"why now."** Without one, a thesis can sit unrealized while time decay bleeds your option. Catalysts are what *force* the market to re-price.
- **How to trade off it:**
  - **Match direction:** bullish catalyst → call; bearish → put.
  - **Check if it's priced in (IV Rank):** bullish news + *cheap* IV = the gold case (move coming, options still cheap). Bullish news + *rich* IV = likely already priced; expected follow-through is small and the premium is fat — usually skip.
  - **No catalyst = no edge.** Sitting out is a valid, often correct, move.
- **Game vs real:** real catalysts come from news, earnings calendars, economic data, supply reports. The game's "smart-money moat" (insider/congress/unusual options activity) is another catalyst source.

---

# PART C — THE OPTION CONTRACT READOUTS

## 8. Strike & moneyness (ITM / ATM / OTM)
- **What:** the strike is the agreed exercise price. Moneyness = where the strike sits vs spot:
  - **ITM (in-the-money):** call strike *below* spot, or **put strike *above* spot** — already has exercise value.
  - **ATM (at-the-money):** strike ≈ spot.
  - **OTM (out-of-the-money):** call strike above spot, or put strike below spot — no intrinsic value yet.
- **Why it matters:** moneyness sets the **risk/reward and the odds**:
  - **OTM** = cheap, low probability, big percentage payoff if it hits (lottery-ish).
  - **ATM** = balanced; most sensitive to a move starting now (highest gamma/theta).
  - **ITM** = expensive, high probability, behaves almost like the underlying (high delta) with less time-value risk.
- **How to trade off it:**
  - **High conviction + want leverage on a big move:** OTM or ATM.
  - **Want stock-like exposure with defined risk and high odds:** ITM.
  - **Selling premium:** sell OTM options (collect time value, low assignment odds).
- **Game vs real:** identical. (And yes — *a put with a strike above spot is a normal ITM put*; it just already has intrinsic value, so it costs more.)

## 9. Intrinsic value vs time value
- **What:** **Intrinsic** = what the option is worth if exercised now (ITM amount; zero for OTM). **Time value** = premium − intrinsic = what you pay for the *chance* of further movement before expiry.
- **Why it matters:** time value is the part that **decays to zero (theta)** by expiry. The more time value you pay, the more the price has to move just to break even.
- **How to trade off it:** **buyers** want to minimize time value paid relative to the expected move (or buy when IV cheap); **sellers** want to collect rich time value (sell when IV rich). OTM options are *all* time value → they decay to nothing if the move doesn't come.
- **Game vs real:** same. Real platforms show the split; the game labels intrinsic on each strike.

## 10. Premium (and premium = max loss for buyers)
- **What:** the price you pay (buyer) or collect (seller) for the option.
- **Why it matters:** for a **buyer**, the premium is the **entire max loss** — defined risk, no matter how far the underlying moves against you. For a **seller**, the premium is the **max profit** (and the risk can be large/undefined).
- **How to trade off it:** premium scales with volatility, time, and moneyness. **Don't overpay:** a great direction call still loses if the premium was too rich for the move that came. Always compare premium to the *realistic* expected move.
- **Game vs real:** same. Real markets add a bid/ask spread (a real cost) the game omits.

## 11. Breakeven
- **What:** the underlying price where the trade's P/L = 0 at expiry. **Call:** strike + premium. **Put:** strike − premium.
- **Why it matters:** being right on **direction isn't enough** — the move must clear the breakeven. This is the single most common beginner mistake (the game flags "ITM but didn't beat breakeven").
- **How to trade off it:** before entering, ask *"is a move past breakeven realistic given this commodity's volatility and the catalyst's size?"* If breakeven is beyond a normal move, the strike/premium is wrong — pick a closer strike or skip.
- **Game vs real:** identical.

## 12. Forward price & "fair vol"
- **What:** the **forward** is the market's expected price at expiry (drift baked in). **Fair vol** is the volatility used to price the option so that, with no special information, the trade is roughly break-even.
- **Why it matters:** it's *why* options aren't free money. Markets price in the expected drift, so **direction alone has ~zero edge** — your profit must come from information the price doesn't yet reflect (the catalyst, mispriced IV).
- **How to trade off it:** internalize that **the premium already assumes the obvious.** Your edge is only the part the market *hasn't* priced: an under-priced IV, or a catalyst not yet reflected. No such edge → expect to lose the premium over time.
- **Game vs real:** the game prices on the simulated forward; real markets do this via risk-neutral pricing. Same lesson: no edge = no profit.

---

# PART D — PUTTING IT TOGETHER (the decision recipe)

Read the metrics **in this order** — each gates the next:

1. **Catalyst?** No catalyst / no edge → **sit out.** (Most months.)
2. **Trend + chart** → does the setup *agree* with the catalyst direction? (Aligned = higher confidence.)
3. **IV Rank** → decides **buy vs sell** premium:
   - Cheap IV + catalyst → **buy** (call if bullish, put if bearish).
   - Rich IV → **sell/spread**, or skip (move likely priced in).
4. **Pick structure & strike** (moneyness) to match conviction:
   - Big expected move, cheap IV → ATM/OTM long option.
   - High-odds, stock-like → ITM.
   - Rich IV, neutral → sell OTM premium / spread / condor.
5. **Check breakeven** → is clearing it realistic given rVol and the catalyst size? If not, fix the strike or pass.
6. **Size by rVol** → risk a small fixed % of capital; remember premium = max loss (buyer).

> **The one-sentence summary:** *the catalyst + trend set the direction, IV Rank sets whether you buy or sell premium, moneyness sets the risk/reward, and breakeven vs. realistic move is the final sanity check.*

---

## How this maps to the Trader Screener
Every metric above is a **column the screener should compute and a rule it should apply**: chart/range/trend + rVol (🟡 from OHLCV), IV Rank/skew/Greeks (🔴 options feed), catalyst/smart-money (✅ the moat). The decision recipe **is** the recommendation engine (see [How to Read a Tear Sheet](How_to_Read_a_Tear_Sheet.md) and the [Examples Gallery](Derivatives_Examples_Gallery.md)).

*Related: [Derivatives Types & Signals](Derivatives_Types_and_Trading_Signals.md) · [Examples Gallery](Derivatives_Examples_Gallery.md) · [Tear Sheet](Information_Sheet_Tearsheet.md) · play the [signals game](game/catan_options_signals.py).*
