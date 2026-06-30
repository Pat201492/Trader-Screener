# Stats Explained — How Every Number Is Derived

Precise definitions for the statistics shown in the signals game (`catan_options_signals.py`): **how each is computed, its possible range, and what a value's position in that range implies.** These mirror the real-world metrics they stand in for.

> Educational only — not investment advice.

---

## The underlying price process (so the rest makes sense)
Each commodity's monthly price is built as:
```
logP_t = log(start) + (drift/12)·t  +  seas_amp·sin(2π·month/12 + phase)  +  c_t
c_t    = rho·c_{t-1} + sigma_t·z      (+ occasional jump)
sigma_t = vol_m · exp(0.5·h_t),   h_t = 0.9·h_{t-1} + 0.3·w      (stochastic vol)
P_t    = exp(logP_t)
```
- **drift** = slow annual trend · **seas_amp/phase** = annual seasonality · **rho** = how persistent swings are (momentum vs mean-reversion) · **vol_m** = base volatility · **jumps** = shocks.
- The 5-year **history** is generated with a *fixed per-commodity seed*, so each commodity always has its characteristic shape. The forward (played) months use live randomness.

---

## 1. Price
- **Derivation:** `P_t = exp(logP_t)` from the process above — the latest value.
- **Range:** theoretically `(0, ∞)`; in practice each commodity oscillates around its trend within roughly its 5-year band.
- **Position implies:** nothing on its own. Only meaningful **relative** to the 5y range, the trend, and a catalyst.

## 2. 5-year range (lo–hi)
- **Derivation:** `lo = min(last 60 prices)`, `hi = max(last 60 prices)` — a rolling window.
- **Range:** any positive lo < hi.
- **Position implies (where current price sits in it):**
  - `%position = (Price − lo) / (hi − lo)`
  - **Near hi (>80%):** strength/momentum — or overextended (chase risk).
  - **Near lo (<20%):** weakness — or value (or a falling knife).
  - **Mid:** no edge from level alone.

## 3. rVol — realized volatility
- **Derivation:** annualized standard deviation of the **last 12 monthly log-returns**:
  ```
  r_i = ln(P_i / P_{i-1})
  rVol = stdev(r_last12) × √12
  ```
- **Range:** `0 → ∞`. For these commodities, ≈ **15%–80%** (Grain/Brick low, Lumber/Ore high).
- **Position implies:**
  - **High rVol:** big swings → trade smaller, set wider stops, options are **more expensive**.
  - **Low rVol:** calm → tighter stops viable, cheaper options.
  - rVol is the **baseline you compare implied vol against** (see IV Rank).

## 4. IV Rank (0–100)  ← the key options stat
- **Derivation:** built from the commodity's **own history of volatility**:
  1. Compute the trailing **3-month** realized vol at every point in the 5-year history → a distribution.
  2. Compute today's trailing 3-month realized vol.
  3. **IV Rank = % of historical readings ≤ today's.**
  ```
  iv_rank = 100 × (count of historical 3m-vols ≤ current 3m-vol) / (total)
  ```
- **Range:** **exactly 0–100** (it's a percentile).
  - `0` = the calmest vol in 5 years · `100` = the most volatile in 5 years · `50` = median.
  - Tags: **CHEAP ≤ 40 · FAIR 41–65 · RICH ≥ 66.**
- **Position implies (the whole buy-vs-sell decision):**
  - **Low (CHEAP):** options under-priced for this name → **buy** premium (long calls/puts/straddles).
  - **High (RICH):** options expensive → **sell** premium (cash-secured puts, covered calls, credit spreads). A high reading often means a move is **already priced in**.

## 5. Trend (↑ → ↓)
- **Derivation:** 6-month price change with a dead-band:
  ```
  chg = Price_now / Price_6mo_ago − 1
  ↑ if chg > +4% ,  ↓ if chg < −4% ,  else →
  ```
- **Range:** the arrow is one of three states; the underlying `chg` is unbounded.
- **Position implies:** directional bias. Prefer **calls in ↑**, **puts in ↓**; counter-trend needs a stronger catalyst.

---

## Option-pricing stats (shown on the buy/sell screen)

## 6. Implied volatility (the priced vol)
- **Derivation:** the **fair forward dispersion × an IV-Rank markup**:
  ```
  sigma_true  = stdev of simulated next-month log-returns (no catalyst) × √12
  markup      = 1 + 0.15 × (IV_Rank − 50) / 50          # ranges 0.85 … 1.15
  implied_vol = sigma_true × markup
  ```
- **Range:** ≈ `0.85×` to `1.15×` the true dispersion (so implied vol deliberately ≠ realized vol — that gap is the **variance risk premium**).
- **Position implies:** implied **above** realized → options rich (favor selling); **below** → cheap (favor buying). This is *why* the IV-Rank rule pays off.

## 7. Forward price (F)
- **Derivation:** Monte-Carlo **mean of ~500 simulated next-month prices** with no catalyst — the market's fair expectation.
- **Range:** near the current price, nudged by drift/seasonality/mean-reversion.
- **Position implies:** options are priced off F, so **direction alone ≈ zero-EV**. Your edge is only what F/implied-vol *don't* already include — the catalyst and the IV mispricing.

## 8. Premium
- **Derivation:** **Black model** on the forward, `T = 1/12` (one month):
  ```
  d1 = [ln(F/K) + 0.5·σ²·T] / (σ·√T);   d2 = d1 − σ·√T
  call = F·N(d1) − K·N(d2);   put = K·N(−d2) − F·N(−d1)
  ```
  (σ = implied vol above; `N` = normal CDF.) Floored at 0.05.
- **Range:** `≥ intrinsic value`, rising with vol, time, and moneyness.
- **Position implies:** for a **buyer** it's the **max loss**; for a **seller** it's the **max profit**. Pay too much premium and a correct direction still loses.

## 9. Breakeven
- **Derivation:** call `K + premium`; put `K − premium`; spreads `short strike ± net credit`.
- **Range:** offset from the strike by the premium/credit.
- **Position implies:** the price the move must **clear** to profit. Compare it to the **realistic move** (rVol × the catalyst) — if breakeven is beyond a normal move, the strike/premium is wrong.

---

## Quick reference

| Stat | Formula (short) | Range | High end means | Low end means |
|---|---|---|---|---|
| 5y %position | (P−lo)/(hi−lo) | 0–100% | strength / overextended | weak / value |
| rVol | stdev(12 log-rets)×√12 | ~15–80% | big swings, pricey options | calm, cheap options |
| **IV Rank** | percentile of 3m vol in 5y | **0–100** | RICH → **sell** premium | CHEAP → **buy** premium |
| Trend | 6m chg, ±4% band | ↑/→/↓ | uptrend → calls | downtrend → puts |
| Implied vol | σ_true × (0.85–1.15) | ~0.85–1.15× rVol | rich (sell) | cheap (buy) |
| Premium | Black(F,K,σ,1/12) | ≥ intrinsic | buyer max loss / seller max profit | — |

> **The chain of logic:** rVol sets the baseline → IV Rank says if vol is cheap/rich *for this name* (buy vs sell) → trend + catalyst set direction → forward/implied-vol mean direction alone is ~free, so your edge is the catalyst + the IV mispricing → premium and breakeven are the final cost/feasibility check.

*Related: [Metrics Field Guide](../Metrics_Field_Guide.md) (how to trade each) · [How to Read a Tear Sheet](../How_to_Read_a_Tear_Sheet.md) · [HOW_TO_PLAY](HOW_TO_PLAY.md).*
