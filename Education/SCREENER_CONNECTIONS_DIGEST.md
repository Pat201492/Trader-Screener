# Screener Connections — Digest of Every "How It Maps" Note

> **What this is.** Every education document in this folder ends with a note explaining how it feeds the
> **Trader Screener** (headed *"How it maps to the Trader Screener,"* *"What this means…,"* *"How this book
> shapes…,"* etc.). This file pulls all of those notes, dedupes them, and reorganizes them **by screener
> section** so each column/filter shows what the reading list says it should be and which source says it.
>
> Source of truth for the sections referenced: [`Project folder/SCREENER_SHORTLIST.md`](../Project%20folder/SCREENER_SHORTLIST.md).
> The `.pdf` files are generated copies of the `.md` — only the `.md` were read. The four books with both a
> short and a **Detailed** summary are merged (detailed = superset).
>
> **Feed legend** (from the shortlist): **✅** already collected / free · **🟡** free, computable from OHLCV
> (yfinance) · **🔴** needs a paid options-chain or real-time quote feed.

---

## The one-line thesis every doc points at

Fuse the **smart-money moat (§2e)** with **IV-regime-aware structure selection (§2h)**, gate everything by
**tradeable liquidity (§2c)**, and emit a **readable per-instrument tear sheet**, not a wall of numbers. That
fusion is the differentiated, firm-grade output. The edge is **research/screening — not order routing**
(Harris). *[How to Read a Tear Sheet, Information Sheet Tearsheet, Trading & Exchanges — Harris]*

---

## §2a — Price & momentum  🟡

- **Momentum factor, done right.** Prior **2–12 month return with the skip-the-most-recent-month rule**
  (t‑12 → t‑2), then a cross-sectional **percentile rank vs the universe** — that rank *is* French's
  winner/loser bucketing. Re-rank **monthly**; skipping the skip-a-month contaminates it with reversal.
  *[Fama-French]*
- **Momentum is a price-discovery signal** (informed flow drags price toward value → shows up as momentum),
  but it must be **liquidity-gated**: never surface a name whose §2c dollar volume fails the floor, or price
  impact eats the edge. *[Harris]*
- **Lag every §2a indicator** (returns, RS rank, MA cross, RSI, MACD) to the **prior close** — compute on data
  up to the previous period only, or you inject look-ahead bias. *[Chan]*
- **Gate before defaulting.** Any momentum signal must clear an **out-of-sample gate** and have its
  lookbacks/thresholds-tried logged before it earns a spot in §4 default columns/sort. *[Backtest Overfitting]*
- **Futures caveat.** A naïvely stitched continuous series injects **phantom roll gaps** that fake
  momentum/breakouts; default momentum to a **back-adjusted or return-spliced** series, never raw stitched
  prices. *[CME Futures]*

## §2b — Volatility  🟡 / 🔴

- **ATR is the load-bearing input** — it sets stop distance and therefore all §2g sizing downstream; this is
  why §2b is tagged "sizing + stops." *[QuantInsti Risk]*
- Realized vol / beta / ATR encode Harris's **fundamental-vs-transitory split** — use them for **sizing and
  regime context, not as standalone signals**. *[Harris]*
- ATR is the screener's practical **volatility proxy for Kelly sizing** (see §2g); lag it like any signal.
  *[Chan]*
- (IV / IV-rank also live here as the §2h mirror — see §2h.)

## §2c — Liquidity & microstructure  🟡 / 🔴  *(the honest free layer)*

- **Dollar volume (ADV × price) is the P1 tradeability filter** and the honest 🟡 stand-in for the missing
  quote feed: it proxies Harris's *depth* dimension. Ship it as both a **default column and a hard filter**.
  *[Harris — the anchor]*
- **Spread** = Harris's *width* / price-of-immediacy dimension; **depth/book** = *depth*, the only window onto
  *resiliency*. All 🔴 (no quote feed). Label them **"not in v1"**, citing the Ch. 21 price-benchmark formula
  as the future reference — so the un-measured liquidity dimensions are **explicit, not silent**. Dollar
  volume proxies depth *only* — it says nothing about spread or resiliency. *[Harris]*
- **Capacity is a first-class filter** — the independent trader's edge lives specifically at **low capacity**;
  screen out names too thin to trade at your size without slippage. *[Chan]*
- **ETFs need branched logic.** Do **not** filter an ETF on its own screen ADV — compute liquidity from the
  **underlying basket** (constituent dollar-ADV × weight); the achievable **spread (arbitrage band)**, not
  ADV, is the ETF gate. Never hard-reject a liquid-basket ETF on low screen volume. Basket liquidity is free —
  holdings already collected. *[CFA ETF]*
- **Futures redefinition.** Replace ADV/dollar-volume with **active-month contract volume + open interest**
  (OI is the tradeability signal ADV misses and flags a dying front month); surface **days-to-roll**.
  *[CME Futures]*
- **Set factor breakpoints from liquid names only** (high dollar-volume) so illiquid micro-caps don't warp
  the percentile cut points. *[Fama-French]*
- A dollar-volume gate also makes explicit the liquidity screen Greenblatt's cap-floor only handles
  implicitly. *[Greenblatt]*

## §2d — Fundamentals, valuation & composite score  ✅

- **Composite score (`model.json`) = a summed-ordinal-rank engine.** The Magic Formula is the cleanest
  template: combine factors via **`rank(A) + rank(B)`**, not blended raw values (sidesteps unit mismatch).
  *[Greenblatt — the anchor]*
- **ROIC** ≈ Return on Capital (ideally **EBIT / tangible capital**, stricter than a book ROIC field);
  add an **EBIT/EV earnings-yield** column beside **P/E** as the capital-structure-neutral cheapness rank.
  *[Greenblatt]*
- **Market cap** = the **cap-floor pre-rank filter**; **Sector** implements the **financials/utilities/ADR
  exclusions** as a pre-rank filter, not a display column. *[Greenblatt]*
- **DCF/comps (`Upside %`) vs the composite are complementary, not redundant:** DCF is *absolute* intrinsic
  value (forecasts, WACC); the Magic Formula is *relative, cross-sectional, trailing-only*. The composite can
  host both as separate factors — "what is it worth?" vs "which is best-ranked right now?" *[Greenblatt]*
- **Factor-model view:** SMB = **size sort** (median-cap split), HML = **value** (invert P/E → earnings-yield /
  book-to-market), RMW = **ROIC quality**, low-beta tilt from §2b. **Combine tilts as composite z-scores**,
  not stacked hard filters; match cadence to factor (monthly momentum, annual/quarterly value/quality/size).
  *[Fama-French]*
- **Discipline on the composite:** keep parameters ≤ 5, **validate out-of-sample** (older = train, recent =
  test; test length by sample-size rule, not a fixed ⅓), and treat all fundamentals as **point-in-time** —
  restated financials carry embedded look-ahead bias, and a value bent maximizes survivorship inflation.
  *[Chan]* · No factor/weighting becomes a §4 default until it clears an out-of-sample gate with its
  **trial-count N logged**. *[Backtest Overfitting]*
- **ETF rows:** P/E, ROIC, upside are meaningless for funds — **blank them and show expense ratio + total cost
  of ownership** instead; add a **data-quality flag** for persistent NAV premium/discount + tracking
  difference. *[CFA ETF]*
- Composite + max drawdown are **sample-level edge inputs** — judge them over **20+ trades**, never the last
  one or two. *[Douglas]* · They're also the screener's **value-trader engine** (flag price vs fundamental
  value), with §2e smart-money as the **timing/confirmation overlay**. *[Harris]*

## §2e — Smart-money moat  ✅  *(the differentiator)*

- **Theoretical basis = Harris's informed-trader theory.** Congress trades, insider (EDGAR), committee-
  conflict cross-tab, and news sentiment are the **adverse-selection component made observable** — the very
  order flow that widens spreads. This is the screener's **most defensible edge**; give it its **own scored
  view/filter**, not raw event flags. *[Harris]*
- **UOA is the options-side twin** of the equity moat — same informed-flow thesis; if options ship, surface
  **unusual options activity as a flagship signal**. Put/Call and skew extremes feed the same view as
  contrarian sentiment. *[Derivatives Types, IV-Rank/OIC]*
- **Validate out-of-sample** (walk-forward / CSCV on held-out dates) — reporting the single best
  committee/window combo turns the "moat" into the max of many noisy cross-tabs. *[Backtest Overfitting]*
- **Treat every signal as an edge, not a certainty** — a *higher probability*, not a guaranteed reversal.
  Act on it without hesitation when it fires, stop gathering evidence outside the defined signal, and expect a
  random win/loss distribution across many such signals. *[Douglas]*

## §2f — Macro context  ✅ (FRED)

- **VIX regime column.** Ingest FRED **VIXCLS** and bucket the level (**calm/normal/elevated/stress/panic**);
  use as a **filter** (e.g. suppress new longs when VIX > 30) and a **context banner**. Highest-value,
  zero-cost mapping. Store **VIX9D/VIX3M** too for a **term-structure inversion** flag (a stronger crisis
  tell than level). *[Cboe VIX]*
- **Recession-probability regime.** Ingest **T10Y3M** via the existing `fred.py`, compute the probit →
  monthly **P(recession)** headline with a color badge (green < 30% · amber 30–50% · red ≥ 50%). Treat
  inversion as a **12-month lead warning, not a sell trigger**. *[NY Fed Yield Curve]*
- **Regime gates the §4 default filters** (not a hard block, user-overridable): risk-off → bias to defensive
  §2d sectors + quality, away from high-§2b-beta / high-§2a-momentum; risk-on relaxes it. *[NY Fed, Cboe VIX]*
- Macro doubles as a **strategy-family switch** (mean-reversion vs momentum) — condition rankings on recent
  data because markets are non-stationary. *[Chan]* · Realized vol / VIX are a **regime switch, not a stock
  picker**. *[Harris]* · A VIX spike is a **non-negotiable de-risk trigger**, not a debatable one. *[Douglas]*
- **Pair the two on a shared macro tab:** slow/leading (yield curve) + fast/coincident (VIX).

## §2g — Risk / sizing  🟡

- **Fixed-fractional core:** risk **1–2% of equity per trade**, size **backwards from an ATR stop** —
  shares = (equity × risk %) / (k × ATR); stop = entry − k × ATR. Max drawdown operationalizes the
  "50-losses-to-ruin" intuition. Surface size, stop, and drawdown as **default §2g columns** so a screen hit
  becomes a pre-sized trade plan. *[QuantInsti Risk]*
- **Kelly formalization:** position size = **Kelly f = m/σ²** via ATR — but use **half-Kelly**, capped further
  by the **worst-historical-loss** rule (Gaussian Kelly under-estimates tail risk). Attach **Sharpe + max
  drawdown + MAR** to the equity curve before trusting any ranking; require backtest **Sharpe ≥ 1 over ≥ 681
  daily points**. **ATR stops only in a trending regime** — gate them on §2a trend, never apply to
  mean-reverting names (a stop there just realizes the loss). *[Chan]*
- ATR-scaled sizing **caps price impact per name** — "most active traders lose because they trade too much."
  *[Harris]*
- **Behavioral layer:** the §2g outputs are **pre-commitments** to be obeyed *after* a drawdown (predefine and
  fully accept the risk), not suggestions to widen or double up. Maps to **behavior, not a new column**.
  *[Douglas]*

## §2h — Options / derivatives signals  🔴 (all need an options-chain feed)

- **New data dimension = real cost.** IV, IV-Rank, skew, OI, Greeks, UOA all need a **paid options-chain feed**
  (Polygon / ORATS / Tradier / CBOE) — free yfinance won't cover them. **Decide instrument coverage (§1)
  before building.** *[Derivatives Types]*
- **IV Rank + IV Percentile are the must-have normalizer** — raw IV doesn't compare across names. **Ship
  both, never raw IV alone**; wire IVR thresholds to hints (**high → premium-selling, low → premium-buying**).
  **Warming-up caveat:** IVP needs ~3–6 months of nightly snapshots, true IVR ~1 year of high/low —
  flag "warming up" until then, and **start the nightly IV snapshot job now**. *[IV-Rank/OIC]*
- **Skew** = 25Δ put/call IV ratio (equities skew negative by default; a *flattening* skew is the notable
  signal). **Put/Call ratio** = contrarian sentiment — flag extremes as reversal signals feeding §2e.
  **Term structure** (VIX vs VIX3M) is a cheap contango/backwardation proxy without a per-name chain.
  *[IV-Rank/OIC, Cboe VIX]*
- **Payoffs derive from the same chain data.** Let users **filter by Greek exposure + IV Rank**, then have the
  rules engine **suggest the structure** — the "matching action to signal" table. *[Derivatives Gallery]*

## §3 — Trading style / horizon

- ATR-based sizing/stops fit the **swing/position horizon** the nightly free OHLCV pipeline already serves.
  *[QuantInsti Risk]*
- For swing/position, the **free ✅ + 🟡 pipeline covers the vast majority** of what matters; only **day
  trading** forces the 🔴 intraday feeds (spread, depth, VWAP, live options). *[Harris, IV-Rank/OIC]*

## §4 — Default view / output spec

- **Nothing is promoted** to top-10 columns / default sort / default filters until it is **deflated for search
  effort and validated out-of-sample**. **Publish trial-count N and the deflated Sharpe next to any
  performance figure — a Sharpe without N is not a result.** *[Backtest Overfitting]*
- **Macro regime flag pre-sets the default filters** (soft, user-overridable). *[NY Fed, Cboe VIX]*
- The truly Douglas-shaped §4 feature is a **per-trade rule-adherence log** (realized vs §2g-suggested size =
  discipline drift) and a drawdown **cool-down flag** — not another metric column. *[Douglas]*

---

## The target output = a tear sheet

The **per-instrument detail view** the screener generates *is* a firm-style tear sheet; every block maps to
data already held or planned. *[Information Sheet Tearsheet, How to Read a Tear Sheet, Metrics Field Guide]*

| Tear-sheet block | Screener data source |
|---|---|
| Snapshot valuation / fundamentals | `fundamentals.json`, `model.json` (✅) |
| Liquidity / ADV / beta / ATR | OHLCV-derived (🟡) |
| Options: IV, IV Rank, skew, term | options-chain feed (🔴, §2h) |
| Positioning / smart money | congress + insider + UOA (✅, §2e) |
| Technicals | OHLCV-derived (🟡) |
| **Derivative recommendation** | rules engine: thesis (score/momentum) × IV Rank → structure |

**The recommendation logic = the "matching action to signal" table:**

- bullish **+ low IV** → **long call**
- bullish **+ high IV** → **call (vertical) spread**
- hedge **+ rich skew** → **collar**
- range-bound **+ high IV** → **iron condor**

Every metric is *"a column the screener computes and a rule it applies"*; the decision recipe **is** the
recommendation engine. A screener that outputs a **readable, self-consistent argument** per instrument — not
just numbers — is the firm-grade product. *[Metrics Field Guide, How to Read a Tear Sheet]*

---

## Cross-cutting themes (what recurs across sources)

1. **The moat is the edge.** §2e smart-money — grounded in Harris's informed-trader theory, twinned by §2h
   UOA — is the differentiator. Give it a dedicated scored view; validate it out-of-sample.
2. **Liquidity gates everything.** A momentum (§2a) or value (§2d) rank is only real if §2c dollar volume
   clears a floor. *[Harris, Chan]*
3. **Overfitting is the default failure mode.** A Sharpe without its trial count is meaningless; deflate +
   validate before any §4 default; keep `universe.json` **point-in-time / delisting-inclusive** (survivorship).
   *[Backtest Overfitting, Chan]*
4. **Volatility is for sizing & regime, not signal.** ATR/realized vol/VIX drive §2g sizing and §2f regime,
   never a standalone buy. *[Harris, Chan, QuantInsti]*
5. **Psychology is a usage policy, not a column.** Douglas maps to the *behavior around* the risk tab —
   obeying pre-set sizing, judging over 20+ trades, auto-de-risking on VIX.
6. **Don't become an execution venue.** The screener's edge is research/screening; leave order routing alone.
   *[Harris]*
7. **Feed reality sets the roadmap.** ✅ moat + 🟡 OHLCV cover swing/position today; 🔴 options + real-time
   quotes are the paid frontier — commit deliberately (§1 coverage decision).

---

## Consolidated open questions (deduped across all docs)

- **Dollar-volume floor:** what threshold cleanly separates "tradeable" from "cost-prohibitive" at the
  intended size?
- **Survivorship:** which delisting-aware / point-in-time source feeds a clean `universe.json`, and at what
  startup cost?
- **Composite mechanics:** equal-weight vs weighted ranks — how do §2e smart-money and §2a momentum enter the
  same rank-sum without swamping the fundamental factors? How to log trial counts for `model.json`, whose
  upstream search history is opaque?
- **Sizing defaults:** which ATR multiple `k` and lookback; risk % as user input or fixed 2%; true Kelly
  (m/σ² across the book) vs the simpler ATR fixed-fractional column, and at what fraction?
- **Regime thresholds:** VIX bands and recession-prob 30/50% lines — fixed, rolling-percentile, or
  user-tunable? Does the flag gate longs outright or only rescale §2g size and §2d score?
- **Options feed ROI:** is a paid chain justified for a swing/position horizon, or is single-name IV a P2 until
  day-trading is in scope? Snapshot 30-day ATM (VIX-style) or the full surface?
- **ETF data:** source for iNAV/NAV and per-constituent ADV to compute premium/discount, tracking difference,
  and basket liquidity on the free nightly pipeline?
- **Futures data:** default contract per market (standard vs Micro); one canonical roll rule
  (OI-crossover / calendar / N-days-before-expiry) for all continuous series?
- **Confidence surfacing:** a per-metric "confidence / deflation" badge instead of raw Sharpe/drawdown?

---

*Condensed from the `Education/` reading list (screener-relationship notes only). Sources, in citation
shorthand: **Harris** = Trading and Exchanges (summary + detailed); **Chan** = Quantitative Trading (summary +
detailed); **Greenblatt** = Magic Formula (summary + detailed); **Douglas** = Trading in the Zone (summary +
detailed); **Fama-French**, **Backtest Overfitting**, **CFA ETF**, **CME Futures**, **Cboe VIX**, **NY Fed
Yield Curve**, **QuantInsti Risk**, **IV-Rank/OIC** = `summaries/`; **Derivatives Types**, **Derivatives
Gallery**, **Metrics Field Guide**, **How to Read a Tear Sheet**, **Information Sheet Tearsheet** = top-level
`Education/`. All figures in the source docs are hypothetical / educational.*
