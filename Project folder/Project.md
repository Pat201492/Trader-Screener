# Project — Trader Screener

> The core build. Closest sibling to the **old** Stock Tracker / Stock-App screener. Reuses its data-collection pipeline so that any data ingested into the old app is **duplicated** into this one (and vice-versa), keeping a single source of truth and minimizing hosting cost.

---

## What it is
A screener for trading financial instruments — same shape as the old stock screener (filter + sort + score a universe of tickers, drill into a detail view), but oriented toward the *trader* use-case rather than long-term valuation alone.

The old app already does the hard part: discovering a universe, enriching it, scoring it, and serving it via an API + mobile/web front-ends. We **reuse that data layer wholesale** rather than rebuild it.

---

## Reuse from the old pipeline (`old/Stock-App` → really the live Stock Tracker)

The old project runs a nightly incremental pipeline on **Fly.io** (cron machine, ~2am UTC, `scheduler.py → run.py`). Pipeline order:

```
universe.py → fundamentals.py → model.py → news.py → etf_universe.py
```

| Stage | Script | Source | Output | Reuse for Trader Screener |
|---|---|---|---|---|
| **Discover** | `universe.py` | NASDAQ Trader FTP (`nasdaqlisted.txt`, `otherlisted.txt`); fallback Wikipedia S&P/iShares CSV | `universe.json` (top N by mkt cap) | Same universe — no need to rediscover. |
| **Enrich** | `fundamentals.py` | `yfinance` (batched `yf.Tickers`) | `fundamentals.json` (+ `data_quality` score) | Same fundamentals feed the screener columns. |
| **Value/Score** | `model.py` | DCF + Comps + EPV/Graham | `model.json` (`score_composite`, label, stars, upside) | Score column carries over; may add trader-specific scores. |
| **News/Sentiment** | `news.py` | `yfinance` news + TextBlob sentiment | News rows + sentiment | Reuse for the news badge + research feed. |
| **ETFs** | `etf_universe.py` | yfinance / holdings | ETF universe + holdings | Reuse for ETF screening. |
| **Macro** | `fred.py` | FRED (St. Louis Fed) API | Macro series (rates, etc.) | Powers a macro/health tab; shared with Research + Education. |
| **Congress / Insider** | `ingest_congress.py`, `ingest_senate.py`, `ingest_house.py`, `ingest_senate_efd.py`, `ingest_edgar.py`, `ingest_insider_mirror.py`, `ingest_committees.py` | Senate/House.gov disclosures, SEC EDGAR | Politician + insider trade tables, conflict cross-tab | Reuse for "smart money" signals in the screener. |

**API already exposed by the old server** (`server.py`, FastAPI):
`GET /api/stocks` supports `sector`, `cap_size`, `search`, `sort` (rank/mkt_cap/price/name/score/upside/pe/roic), `order`, `limit`, `offset`, `min_score`, `max_score`, `conflicts_only`. The new screener can call this directly instead of re-querying raw data.

---

## Data interconnectivity (the important constraint)
> **When data uploads into the old app, it must also land in this project — and ideally only be collected once.** See the root [ARCHITECTURE.md](../ARCHITECTURE.md) for the shared-data + hosting plan. The two projects are meant to share one collection pipeline and one datastore, not run two copies.

Implications:
- Don't fork the ingest scripts — point both apps at the **same** output (`universe.json`, `fundamentals.json`, `model.json`, DB tables) or the same API.
- Anything the Trader Screener adds (new metric, new instrument class) should be added to the **shared** pipeline so the old app and Research tab get it for free.
- Hosting: keep one Fly.io pipeline machine; both front-ends read the same data volume / API. Avoid a second always-on collector (cost).

---

## What's different from the old screener (to design)
- Trader-oriented instruments beyond equities (options/futures/forex/crypto — see Education for the landscape).
- Trader-oriented metrics (volatility, liquidity, momentum) layered on top of the existing fundamental/valuation scores.
- Possibly intraday vs nightly cadence for some signals (the old pipeline is nightly).
- **Options-chain feed** — first new feature being built. See below.

---

## Liquidity layer (issue #37) — the first-class gate

Tradeability is the gate every other surfaced signal passes through. The front-end (`web-dashboard/index.html`) ships ADV + dollar volume as **default columns and a hard dollar-volume floor**, branches ETF liquidity to the underlying basket, and discloses that only **1 of Harris's 4 liquidity dimensions (depth)** is proxied in v1 (spread/immediacy/resiliency need a paid quote feed).

**Upstream contract (add to the shared pipeline — ARCHITECTURE.md rule 2 "new metric → add upstream").** So the old app + Research reuse it, compute these from the **nightly OHLCV** already collected and expose them on each `/api/stocks` row:

| Field | Type | Meaning | Source |
|---|---|---|---|
| `adv` | number | average daily **share** volume (e.g. 20-/30-day mean) | OHLCV volume |
| `dollar_volume` | number | `adv × price` — the depth proxy / tradeability measure | OHLCV |
| `is_etf` | bool | instrument-type flag so the front-end branches liquidity logic | universe / `etf_universe.py` |
| `basket_dollar_volume` | number \| null | **ETF only** — `Σ (constituent adv × price × weight)` from collected holdings; the ETF's true capacity | `etf_universe.py` holdings |

- The front-end degrades gracefully: if `dollar_volume` is absent it derives `adv × price`; if `basket_dollar_volume` is absent it computes the basket sum from an inline `holdings:[{ticker,weight,adv,price|dollar_volume}]` array when present; otherwise it falls back to screen $vol and **never hard-rejects an ETF on screen ADV**.
- **ETF rule:** screen ADV is a **floor, never a ceiling** — a low-screen-volume ETF on a liquid basket is not rejected (CFA ETF Guide; see `Education/summaries/cfa-etf-guide.md`).
- Set factor breakpoints (later momentum/value work) from **liquid names only** — apply `passesLiquidityFloor()` first (Fama-French).
- **Not in v1 (need a paid quote feed):** bid/ask spread (width), market depth/book, resiliency. Columns are present but flagged `v1✗` so the un-measured dimensions are explicit, not silent (Harris Ch. 13–21).

---

## Macro / regime layer (issue #39) — VIX + yield-curve context

A macro tab pairs a **fast, coincident** vol gauge (VIX) with a **slow, leading** recession-probability
gauge (yield curve), both FRED-only. The front-end (`web-dashboard/index.html`) computes the VIX regime
bucket, term-structure inversion flag, and Estrella–Mishkin recession probit **client-side** from raw FRED
values — the same "raw upstream, derive downstream" split the liquidity layer uses for `effDollarVol()`.

**Upstream contract (add to the shared pipeline).** New `GET /api/macro` endpoint, sourced from `fred.py`:

| Field | Type | Meaning | Source |
|---|---|---|---|
| `vix` | number | latest VIXCLS close | FRED `VIXCLS` |
| `vix9d` | number \| null | 9-day VIX | FRED if carried, else Cboe/yfinance fallback |
| `vix3m` | number \| null | 3-month VIX | FRED if carried, else Cboe/yfinance fallback |
| `t10y3m` | number | latest daily 10Y−3M spread, percentage points (already the spread) | FRED `T10Y3M` |
| `t10y3m_monthly` | number \| null | spread resampled to a monthly average — preferred input to the probit | derived from `T10Y3M` |
| `asof` | string | as-of date for the snapshot | — |

- The front-end degrades gracefully: prefers `t10y3m_monthly`, falls back to the raw daily `t10y3m` (labeled
  "daily approximation" in the UI) when the monthly figure isn't available yet; if `/api/macro` is
  unreachable the macro tab shows the standard API-unreachable error and the screener simply skips the
  regime nudge (both filters keep their normal unfiltered defaults).
- **VIX regime bucket:** calm &lt;15 / normal 15–20 / elevated 20–30 / stress &gt;30 / panic &gt;40 (Cboe VIX
  methodology rule-of-thumb bands).
- **Term-structure inversion:** VIX/VIX3M ratio &gt;1.0 flags inversion, &gt;1.10 = deep backwardation — a
  stronger crisis tell than the VIX level alone.
- **Recession probability:** `P = Φ(−0.5333 − 0.6629 × spread)` (Estrella–Mishkin 1998, fit on monthly US
  data ~1959–1995). **⚠ Constants not re-verified against a current NY Fed refit** — re-check before
  shipping any decision that leans on a precise threshold. Badge: green &lt;30% · amber 30–50% · red ≥50%.
- **Regime gate is soft, never a hard block:** risk-off (VIX &gt;30 or P(recession) ≥50%) nudges the
  screener's default **$-volume floor** and **cap floor** tighter (applied once per session load) — every
  filter stays user-editable via the existing controls. Inversion is labeled a **12-month leading warning,
  not a sell trigger** in the UI (recessions historically arrive after the curve re-steepens; one known
  false positive, 1966–67).

---

## Momentum layer (issue #40) — trailing returns + skip-month RS rank

Depends on #35 (column registry) and #37 (liquidity gate). The front-end ships trailing return columns and the academically-correct momentum factor: cumulative return **t-12 → t-2**, skipping the most recent month (t-1). That skip is French's actual winner/loser bucketing rule — including t-1 contaminates the factor with short-term reversal, a *different* (and opposite-signed) effect. The raw factor is converted to a **cross-sectional percentile RS rank** against the universe so it's comparable across names regardless of scale.

**Upstream contract (add to the shared pipeline).** Compute from the **nightly OHLCV** already collected and expose on each `/api/stocks` row:

| Field | Type | Meaning | Source |
|---|---|---|---|
| `return_1m` | number \| null | trailing 1-month simple return, % | OHLCV close-to-close |
| `return_3m` | number \| null | trailing 3-month simple return, % | OHLCV |
| `return_6m` | number \| null | trailing 6-month simple return, % | OHLCV |
| `return_12m` | number \| null | trailing 12-month simple return, % | OHLCV |
| `mom_factor` | number \| null | cumulative return **t-12 → t-2** (skip t-1), % — the Fama-French prior(2,12) momentum factor | OHLCV, re-ranked monthly |

- **`rs_percentile` is NOT an upstream field** — it's a cross-sectional statistic computed **front-end** (`computeMomentumRanks()`), because the ranking pool depends on the user's live liquidity-floor selection (`LIQ_FLOOR`), which only exists client-side. Recomputed on every `drawScreener()` pass, same as `magic_rank`.
- **Liquidity gate (Fama-French):** the RS-percentile ranking pool is `passesLiquidityFloor()` names only — an illiquid micro-cap must never warp the percentile breakpoints for the liquid names around it. Rows failing the floor get `rs_percentile=null` and are dropped entirely by the hard gate before render, so no gated-out name can ever surface via a momentum/RS sort.
- **Harris:** momentum is a genuine price-discovery signal, but price impact eats the edge without the same liquidity gate every other view uses — momentum gets no special exemption.
- **Chan:** momentum (with mean-reversion) is one of the only two profitable factor families worth building a screener column for.
- Documented per-column in the header tooltip (`tip` field on the `SCREENER_COLS` entry), not just here.

---

## Look-ahead discipline layer (issue #42) — lag every OHLCV indicator to the prior close

Depends on #40 (momentum columns). Cross-cutting gate on the **entire ohlcv metric block** — every §2a/§2b
column derived from price/volume history (returns, momentum/RS rank, MA cross, RSI, MACD, ATR, realized vol,
beta), current and future. Chan (Ch. 3): "Use lagged historical data... based on data up to the close of the
previous trading period only," or the backtest silently trades on information that didn't exist yet.

**The rule, mechanically:** for the value attached to trading day *i*, the computation may read bars/closes at
index < *i* only. Day *i*'s own high/low/close is never read — that's what stops the classic "buy when within
1% of the day's low" bug (you can't know the day's low until it closes), distinct from the truncation check
below (see [`Education/summaries/quantitative-trading-chan-detailed.md`](../Education/summaries/quantitative-trading-chan-detailed.md)).

**Reference implementation + gate (this repo, portable to the shared pipeline):** [`lookahead-gate/lookahead_lag.py`](../lookahead-gate/lookahead_lag.py)
implements every listed indicator with that invariant enforced by construction (each reads `closes[:i]`,
never `closes[i]`). [`lookahead-gate/ab_truncation_test.py`](../lookahead-gate/ab_truncation_test.py) is
**Chan's A-vs-B truncation test**: run the full history through the indicators (file A), truncate the last
N days (N = 10, 50, 100) and re-run (file B) — every overlapping row must be identical, or the "full" run was
peeking at data that hadn't happened yet at that point. It also proves the gate isn't vacuous by re-running the
same check against deliberately leaky variants (a centered/future-peeking moving average, a same-day RSI leak,
an RS-rank computed off a time-pooled/future-aware distribution) and asserting the mismatches are caught, plus
a direct same-day-peek check (corrupt day *i*'s own H/L/C, confirm every lagged indicator at *i* is unchanged).
Run: `python lookahead-gate/ab_truncation_test.py` — exit 0 = pass.

**Upstream contract (add to the shared pipeline).** Every field below must be computed from OHLCV **through
the prior close only**:

| Field | Meaning | Status |
|---|---|---|
| `return_1m/3m/6m/12m` | trailing close-to-close returns | shipped (issue #40) — must be re-verified lagged |
| `mom_factor` | Fama-French prior(2,12), skip t-1 | shipped (issue #40) — must be re-verified lagged |
| `rs_percentile` | cross-sectional rank of `mom_factor` | computed front-end, per-render — inherits the lag from `mom_factor`; never rank against a fixed/time-pooled distribution (see the RS-rank leak case in `ab_truncation_test.py`) |
| `ma_cross` (50/200d) | fast/slow SMA cross | not yet shipped — SCREENER_SHORTLIST §2a |
| `rsi` | Wilder RSI | not yet shipped — SCREENER_SHORTLIST §2a |
| `macd` | MACD line/signal/hist | not yet shipped — SCREENER_SHORTLIST §2a |
| `atr` | Average True Range | screener column shipped (issue #43) — front-end + contract only; pipeline computation lands in Stock-Data-Pipeline (reference impl: `lookahead-gate/lookahead_lag.py` `atr()`); feeds §2g sizing |
| `realized_vol` | trailing return stdev | screener column shipped (issue #43) — same status as `atr`; reference impl: `lookahead_lag.py` `realized_vol()` |
| `beta` | vs. SPY | screener column shipped (issue #43) — promoted from the stock-detail-only field (data already collected); reference impl: `lookahead_lag.py` `beta()` |
| `max_drawdown` | historical peak-to-subsequent-trough decline, % | screener column shipped (issue #44) — front-end + contract only, same status as `atr`; reference impl: `lookahead_lag.py` `max_drawdown()`; feeds §2g's worst-historical-loss size cap |

- **Wire the gate.** Port `lookahead_lag.py`'s functions (or the equivalent pandas/numpy computation, same
  invariant) into the Stock-Data-Pipeline indicator stage, and run `ab_truncation_test.py`'s pattern as a
  pre-publish assertion in that repo's `validate.py` / `run.py` — a non-zero exit blocks the nightly publish,
  same as any other data-quality gate.
- **Front-end disclosure.** The Screener footer carries a standing **"as-of prior close"** note (next to the
  liquidity `lqnote`) so every OHLCV-derived column is explicitly labeled as lagged, not live-quote.

---

## Volatility / sizing layer (issue #43) — ATR/ATR%, realized vol, beta, 52w range %

Depends on #35 (column registry) and #42 (look-ahead discipline — `lookahead-gate/lookahead_lag.py` already
implements lagged `atr()`, `realized_vol()` and `beta()`). Adds the risk/sizing block from SCREENER_SHORTLIST
§2b/§2g. QuantInsti / Chan: ATR is the **load-bearing input** — it sets stop distance and cascades into every
downstream sizing decision (shares = risk% × equity / (k×ATR)), and is the practical volatility proxy for
Kelly. Harris: realized vol, beta and the 52-week range encode the **fundamental-vs-transitory split** in a
price move — they size positions and read the regime, they do **not** pick stocks on their own.

**Upstream contract (add to the shared pipeline, OHLCV-derived, lagged to the prior close per issue #42):**

| Field | Type | Meaning | Source |
|---|---|---|---|
| `atr` | number \| null | 14d Average True Range, $ | OHLCV — see `lookahead-gate/lookahead_lag.py` `atr()` |
| `atr_pct` | number \| null | ATR as a % of price — comparable across names regardless of share price | derived: `atr / price × 100`, both prior-close-lagged |
| `realized_vol` | number \| null | 30d annualized stdev of daily returns, % | OHLCV — see `lookahead_lag.py` `realized_vol()` |
| `beta` | number \| null | trailing 60d beta vs. SPY | OHLCV — see `lookahead_lag.py` `beta()`; **already collected**, promoted from a stock-detail-only field to a screener column |

- **`range52_pct` is NOT an upstream field** — like `rs_percentile`, it's computed **front-end** from the
  already-collected `low52`/`high52`/`price` fields (today surfaced only on the stock detail page):
  `(price − low52) / (high52 − low52) × 100` — 0% = at the 52w low, 100% = at the 52w high.
- **Front-end disclosure.** A dedicated note (`.volnote`, next to the liquidity/lag notes) states these columns
  are sizing/regime inputs, not standalone picks — same never-a-stock-picker framing as the macro VIX/regime
  layer (issue #39).
- Documented per-column in the header tooltip (`tip` field on the `SCREENER_COLS` entry), same as every other
  layer.

---

## Risk / sizing layer (issue #44) — ATR stop distance, fixed-fractional + half-Kelly size, max drawdown

Depends on #43 (ATR/ATR%/mom_factor already shipped) and #40 (momentum block — `rs_percentile` is the
trending-regime proxy). Turns a screen hit into a **pre-sized trade plan**: stop distance, suggested position
size, and the historical max drawdown that caps it. Adds the §2g "Risk / sizing" block from
SCREENER_SHORTLIST.md.

**Upstream contract (add to the shared pipeline, OHLCV-derived, lagged to the prior close per issue #42):**

| Field | Type | Meaning | Source |
|---|---|---|---|
| `max_drawdown` | number \| null | historical max drawdown, % — largest peak-to-**subsequent**-trough decline (time order matters), trailing ~1yr window | OHLCV — see `lookahead-gate/lookahead_lag.py` `max_drawdown()` |

- **`stop_distance` and `suggested_shares` are NOT upstream fields** — both are computed **front-end**
  (`stopDistance()`/`suggestedShares()`), same pattern as `range52_pct`, because they depend on the
  session's user-editable sizing inputs (equity, risk%, k), which only exist client-side.
- **Fixed-fractional (QuantInsti).** `stop_distance = k × ATR`; `suggested_shares = (equity × risk%) / stop_distance`
  — allocates **equal risk, not equal dollars** per name. At 2% risk you can absorb ~50 consecutive losses
  before ruin.
- **Half-Kelly cap (Chan).** `f = m/σ²` using `mom_factor` as the trailing-return proxy (m) and `atr_pct` as
  the volatility proxy (σ), **halved** — Gaussian Kelly underestimates tail risk, so full Kelly is never
  used. Negative momentum clamps the Kelly leg to 0 (no long-side allocation), not a short suggestion.
- **Worst-historical-loss cap.** The shipped `max_drawdown` column caps size directly: if the worst
  peak-to-trough decline ever observed recurred while holding the suggested shares, the dollar loss must
  still stay inside risk% of equity. `suggested_shares` is the **tightest** of the fixed-fractional,
  half-Kelly, and worst-loss-cap legs — never trusts a missing leg as "uncapped".
- **Trending-regime gate.** ATR stops only help when a name is actually trending (Chan) — on a
  mean-reverting/range-bound name a stop just realizes the loss. `ma_cross` (50/200DMA) isn't shipped
  upstream yet, so the front-end proxy is the already-shipped momentum block: `rs_percentile` in the 70+/30-
  band (the same threshold the RS %ile and IV Rank cells already color by) reads as trending; the noisy
  30-70 middle reads as range-bound. Fail-safe like the options chain-liquidity gate — unknown
  `rs_percentile` gates **closed** (no stop shown), never an assumed trend. `stop_distance`/`suggested_shares`
  render `mean-revert`/`–` instead of a number when the gate is closed.
- **Deliberate, non-fitted defaults (open-parameter caveat).** Equity $100,000, risk% 1% (QuantInsti: 1-2%),
  k=2 (2×ATR) — round, illustrative defaults, **not optimized against any name's history**, same posture as
  the VIX-regime bucket thresholds. All three are user-editable inputs (equity number field, risk%/k selects)
  in the Screener controls, persisted across re-renders same as `LIQ_FLOOR`/`CAP_FLOOR`.
- **Behavioral framing (Douglas).** The sizing panel disclosure (`.risknote`) states these outputs are
  **pre-commitments to be obeyed after a drawdown**, not suggestions to widen the stop or double up.
- Documented per-column in the header tooltip (`tip` field on the `SCREENER_COLS` entry), same as every other
  layer.

---

## Options-Chain Feed (first feature)

The first trader-specific feature: for any ticker already in the shared universe, pull and serve its **options chain** — every listed expiration, with the full grid of calls/puts per strike.

### Data source — **decided: paid feed (issue #45)**
`yfinance` doesn't cover this reliably (delayed/snapshot chains, no Greeks) — confirmed a paid options-chain feed is a real, ongoing cost commitment, not a free add-on.

**Primary: Polygon.io (Options Starter/Developer tier).** Reasons:
- Ships **IV and Greeks precomputed per contract** — removes the in-house Black-Scholes step yfinance forced on us.
- Full US equity/ETF listed-options coverage via one REST API — fits the existing "one collector, JSON/API output" pipeline shape (`options.py` stays a script that writes rows, same as every other collector).
- Priced for a single nightly-snapshot workload, not per-quote/real-time billing — matches the hosting constraint (no new always-on machine, ARCHITECTURE.md).

**Alternatives considered, not chosen for v1:**
- **ORATS** — best-in-class vol-surface analytics (smoothed IV surface, skew, historical IV percentiles built in) but priced/scoped for professional vol trading; revisit if/when the screener needs a full smoothed surface rather than a fixed single-point (30d ATM) snapshot.
- **Tradier** — cheaper, brokerage-native; a reasonable fallback if a brokerage integration is added later for execution, but its options-chain endpoint is a secondary feature there, not the core product.
- **CBOE DataShop** — the authoritative exchange-direct source, but pricier and heavier to integrate than justified for v1; the macro VIX layer already gets Cboe-methodology data for free via FRED (issue #39).

**Instrument coverage decision:** options chains are pulled only for tickers that are (a) already in the shared equity/ETF universe and (b) pass the existing hard liquidity gate (`passesLiquidityFloor()`, issue #37) — an illiquid underlying's option chain is wide/worthless and not worth the paid-feed call budget. No separate "options" instrument class is screened in v1 (SCREENER_SHORTLIST.md §1) — options stay a **per-underlying overlay**: IV Rank/Percentile columns joined onto the existing equity/ETF screener rows, plus the existing per-ticker chain lookup tab.

### Where it lives in the pipeline
Collector script in the **shared** pipeline (so the old app can surface it too — collect once, serve both):

```
options.py   # iterate universe (liquidity-gated subset) → for each ticker:
             #   pull chain from the paid feed (Polygon)
             #   normalize → store snapshot rows; derive metrics (see below)
```

- Slots into `run.py` after `fundamentals.py` (needs the universe; independent of model/news).
- **Cadence:** options data moves intraday, but the shared pipeline is nightly. Flag faster/on-demand refresh as an open item.
- **Scope control:** liquidity-gated subset (not the full universe) + a cap on expirations per ticker (e.g. nearest 4–6) to keep storage + fetch time bounded.

### Nightly IV-snapshot job — started now (issue #45)
IV Rank needs ~1 year of history and IV Percentile ~3–6 months before either is trustworthy (see the layer below) — that clock only starts once snapshots exist, so the job starts accruing **immediately**, ahead of every downstream column being "ready."

**Snapshot definition — fixed, not re-derived later:** 30-day ATM IV. For each ticker, each night: take the two listed expirations bracketing 30 calendar days to expiry, interpolate their IV at the strike nearest spot (ATM), linearly interpolate across the two expirations to the 30-day point. This mirrors the Cboe VIX methodology (same "30-day, interpolated, near-the-money" recipe used for the index itself — Education/summaries/cboe-vix-index.md) so single-name and macro vol reads are conceptually consistent. **Once chosen this definition must not change** — redefining it resets the trailing window and invalidates every IV Rank/Percentile computed against the old one.

### Derived metrics (computed, not fetched)
Layer trader signals on top of raw chains — these become screener columns and feed the Research tab:
- **IV Rank / IV Percentile** — see the dedicated layer section below; the must-have normalizer, shipped as of issue #45.
- **Put/call ratio** (volume + open interest) per ticker.
- **IV skew** (term + strike skew).
- **Greeks** — sourced directly from Polygon per contract (no in-house Black-Scholes needed for v1).
- **Unusual activity** — volume ≫ open interest flags.

### Storage / schema
- New table(s): `options_expirations` (ticker, exp dates) + `option_contracts` (one row per contract per snapshot date) — or a per-ticker JSON cache mirroring the `fundamentals.json` pattern.
- **`iv_snapshots`** (new, issue #45): one row per ticker per trading day — `ticker, snapshot_date, atm_iv_30d`. Append-only, never overwrite — this is the trailing window IV Rank/Percentile are computed from. Never redefine `atm_iv_30d`'s calculation once the table has history (see snapshot definition above).
- Keep a **history** of chain snapshots too so OI-trend metrics are possible (don't overwrite — append by snapshot date).

### API (mirror the old `server.py` style)
- `GET /api/options/{ticker}` → expirations + summary metrics (ATM IV, IV Rank, IV Percentile, put/call, skew, OI-by-strike, UOA inputs — see the options-flow layer below).
- `GET /api/options/{ticker}/{expiration}` → full calls/puts grid for one expiry, each contract carrying `delta`, `gamma`, `theta`, `vega`, `rho` (Greeks, sourced directly from Polygon — no in-house Black-Scholes).
- `/api/stocks` rows carry `iv_rank`, `iv_percentile`, `atm_iv_30d`, `iv_history_days` for liquidity-gated tickers (see layer contract below) so the screener joins options data without a second round-trip per row.
- Optional screener filter additions on `/api/stocks`: `min_iv_rank`, `min_put_call`, `unusual_only`.

---

## IV Rank / IV Percentile layer (issue #45) — the options normalizer

Depends on #35 (column registry) and #39 (macro VIX regime). Raw IV is meaningless cross-name (a biotech's 60% vs. a utility's 18% — Education/summaries/iv-rank-iv-percentile-oic.md) — IV Rank and IV Percentile are the must-have normalizer that make IV comparable at all, and the pivot that flips the recommendation structure (high IVR → sell premium, low IVR → buy premium). The front-end (`web-dashboard/index.html`) joins both as **default screener columns**, side-by-side, and **never displays raw IV as a standalone column or pill** — every place IV appears (screener, options tab, stock detail) it's alongside its Rank/Percentile.

**Upstream contract (add to the shared pipeline).** Computed from the nightly `iv_snapshots` history (see above) and exposed on each liquidity-gated `/api/stocks` row + the `/api/options/{ticker}` summary:

| Field | Type | Meaning | Source |
|---|---|---|---|
| `iv_rank` | number \| null | `(atm_iv_30d − 52wk low) / (52wk high − 52wk low) × 100` | derived from `iv_snapshots` |
| `iv_percentile` | number \| null | % of the past year's trading days `atm_iv_30d` closed below today's level | derived from `iv_snapshots` |
| `atm_iv_30d` | number \| null | today's fixed-definition snapshot (30-day ATM IV, fraction e.g. 0.34) | `options.py` / Polygon |
| `iv_history_days` | integer \| null | count of nightly snapshots accrued for this ticker since the job started | derived from `iv_snapshots` |

- Both `iv_rank` and `iv_percentile` are computed **upstream** (unlike `rs_percentile`), because the trailing window is a single-name time series, not a cross-sectional statistic that depends on the user's live filter state — no reason to ship a year of daily snapshots to the client just to recompute two numbers.
- **Warming-up gate, per row:** `iv_history_days < 126` (~3–6mo) flags IV Percentile "warming," `< 252` (~1yr) flags IV Rank "warming" (`ivWarmup()`). Each ticker's clock starts independently — one added to the universe later is not held to the same calendar as one tracked since the job began.
- **Premium hint (`ivHint()`):** IVR ≥70 → "sell prem," IVR ≤30 → "buy prem." **Gated under the macro VIX regime** (`regimeVerdict()`, issue #39): a risk-off regime doesn't suppress the sell-premium hint (a single name's IVR can be idiosyncratically rich even in a calm tape, and vice versa) but attaches an explicit tail-risk caveat — Douglas: a VIX spike is a non-negotiable de-risk trigger, so a "sell premium" hint during one gets a caution, not silence. Same soft-nudge-never-a-hard-block pattern as the liquidity/cap-floor macro defaults.
- Documented per-column in the header tooltip (`tip` field) plus a dedicated disclosure box (`.ivnote`) in the Screener controls, same convention as the liquidity/macro layers.

## Options flow & structure layer (issue #46) — skew / put-call / OI magnets / UOA / Greeks

Depends on #45 (options feed + IV Rank/Percentile) and #41 (smart-money moat). Derivatives — Types & Signals + OIC: puts trading richer than calls (equities run a negative skew by default — structural since the 1987 crash) prices crash/downside-protection demand; a **flattening** skew is the notable event, not the negative skew itself. Put/call extremes are a contrarian sentiment gauge read only at the edges. Large single-strike OI acts as a support/resistance magnet (dealer-hedging flow). UOA (today's volume dwarfing the standing OI book) is the **options-side twin of the smart-money moat** (issue #41) — same informed-order-flow thesis as congress/insider, expressed through derivatives. Douglas: an extreme raises reversal odds, it never removes the randomness of a single trade.

**Upstream contract.** Joined onto liquidity-gated `/api/stocks` rows AND the `/api/options/{ticker}` summary, same dual-placement pattern as `iv_rank`/`iv_percentile`:

| Field | Type | Meaning | Source |
|---|---|---|---|
| `skew_25d` | number \| null | 25Δ put IV / 25Δ call IV ratio | `options.py` / Polygon |
| `put_call_oi` | number \| null | put/call ratio, OI-based (existing issue #45 field, now also joined onto stocks rows) | `options.py` / Polygon |
| `total_oi` | integer \| null | total open interest across the collected chain — the chain-liquidity gate's OI leg | `options.py` / Polygon |
| `total_volume` | integer \| null | total contract volume across the chain, today — the gate's volume leg + UOA numerator | `options.py` / Polygon |
| `call_oi` / `put_oi` / `call_volume` / `put_volume` | integer \| null | optional per-side breakdown — gives UOA a directional lean; degrades to an undirected flag if absent | `options.py` / Polygon |
| `oi_max_strike` / `oi_max_strike_oi` | number/integer \| null | the single-strike OI "magnet" + its OI | `options.py` / Polygon |
| `delta`/`gamma`/`theta`/`vega`/`rho` | number \| null | per-contract Greeks (chain endpoint only — inherently per-position, not per-underlying) | `options.py` / Polygon |

- **Chain-liquidity gate (`optionsChainLiquid()`):** ≥500 total OI and ≥100 total volume across the collected chain. Fail-safe — unknown OI/volume gates **closed** (thin-chain badge shown) rather than trusting an unverifiable reading, same posture as the rest of the liquidity layer. Skew/put-call/OI-magnet/UOA all route through this gate before rendering a value.
- **Skew (`skewCell()`):** ratio ≤1.05 flags a "flattening" note — a heuristic band, not a snapshot-history percentile like IV Rank (no dedicated skew-history table exists yet).
- **Put/call ratio (`putCallCell()`):** ≥1.2 flags capitulation/fear (contrarian bullish tell), ≤0.5 flags complacency (contrarian bearish tell).
- **OI-by-strike (`oiMagnetCell()`):** the row-level column shows the chain-wide magnet strike + distance from spot; the per-expiry chain table (Options tab) additionally highlights the in-expiry OI peak per contract.
- **UOA (`uoaCell()`/`uoaRatio()`/`uoaDirection()`):** volume ≥2× total OI flags "UOA"; direction (call-side vs put-side) is best-effort from the per-side volume/OI split, degrading to an undirected flag if the feed doesn't split by side.
- **Smart-money twin (`computeSmartMoneyScores()`, issue #41):** UOA direction + put/call extreme fold in as a 4th weighted leg (weights rebalanced to congress 0.35 / insider 0.30 / news 0.15 / options 0.20, still summing to 1). Agreeing legs (UOA + P/C both bullish or both bearish) reach full ±1 conviction on the options leg; opposing legs cancel to 0. Only counted when the chain clears the liquidity gate.
- **Greeks (Δ/Γ/Θ/V/ρ):** shipped as a **per-position risk dashboard**, not a screener column — the full per-contract grid in the Options tab chain table, plus a nearest-ATM call/put Greeks card on the stock detail page (`openStock()`'s `detOpt` block). Never aggregated into `SCREENER_COLS`.
- Documented per-column in the header tooltip (`tip` field) plus a dedicated disclosure box (`.oflownote`) in the Screener controls, same convention as the liquidity/IV/macro layers.

## Default view / promotion gate layer (issue #47) — top-10 columns, default sort, discipline log

Depends on #38, #39, #40, #41, #43, #44 (orchestrates already-registered columns + the macro flag — **collected** feed tier, no new upstream data). Bailey et al. (Backtest Overfitting): nothing becomes a default column, default sort, or default filter until it is **deflated for search effort and validated out-of-sample**, and a Sharpe/edge without its trial-count N is not a result — so this issue is a **gate**, not just a column pick.

**Promotion gate (`PROMOTION_GATE`, `web-dashboard/index.html`).** The single registry that logs, per candidate default column: `n` (trial count — parameter/threshold combinations actually tried), `params` (free-parameter count, Chan: keep ≤5), `oos` (walk-forward/CSCV verdict), and `deflatedSharpe` where a backtest applies. `gatePassed(key)` is the enforced rule:

| Column kind | Rule | Why |
|---|---|---|
| `exempt:true` | always passes | raw/observed field (ticker, name, sector, price, `dollar_volume`) — not a fitted signal, nothing to overfit |
| `formula:true` | passes iff `params<=5` | deterministic sizing/risk formula (issue #44's `suggested_shares`) or an observed historical stat (`max_drawdown`) — deliberately not fitted to any history, so there's no trial count to deflate |
| everything else | passes iff `oos===true && n!=null && params<=5` | a fitted/ranked signal — must show its work |

**Default top-10 columns** (`DEFAULT_CANDIDATES`, filtered through `gatePassed` into `DEFAULT_COLS`): `ticker, name, sector, price, dollar_volume, score, ebit_ev_yield, rs_percentile, suggested_shares, max_drawdown`. `smart_money_score` (issue #41) is **deliberately excluded** — its own `oosPending` flag means it fails the same gate (`oos:false`, 37 weight/threshold combinations already tried and logged, none validated) — this is the concrete case the gate exists to catch, not an oversight. Every other non-default column stays reachable via **Columns ▾**, never deleted.

**Default sort:** `score` descending (the Magic-Formula composite) — passes the gate (`n:1`, one fixed formula, no in-house sweep; the published Greenblatt/Fama-French out-of-sample record is the deflation evidence). A `console.warn` fires at load if `sortKey` is ever pointed at a column that fails its own logged gate, so the invariant can't silently drift.

**Publishing N + deflated Sharpe (`gateBadge()`):** every gated default column carries a green `N=…` badge (with deflated Sharpe in its tooltip) in both the table header and the column picker; a column that fails carries a red `gate✗` badge instead (same visual language as the existing `t-oos`/`t-v1` badges). `formula`-tier columns carry a `formula` badge explaining there's nothing to deflate. Same "never a performance figure without its N" rule the Education tab (issue #34) already documented in prose — this makes it enforced, not just asserted.

**Macro regime pre-set (issue #39, already shipped):** `applyRegimeDefaults()` nudges `LIQ_FLOOR`/`CAP_FLOOR` tighter on a risk-off VIX/yield-curve read, soft and user-overridable — unchanged by this issue, listed here because it's the "default filters" leg of the acceptance criteria.

**Rule-adherence log + drawdown cool-down flag** (`TRADE_LOG`, new **Discipline** tab under Stock) — the Douglas-shaped feature the issue calls for explicitly as a **usage/discipline layer, not a metric column**:
- Per-trade log (ticker, `suggestedShares` — auto-filled from the live Size column, issue #44 — `realizedShares`, outcome, note, date), persisted to `localStorage` only (no backend, no ranking).
- **Discipline drift** (`driftPct()`): `(realized − suggested) / suggested`, flagged at ≥20% either direction — the concrete "realized vs suggested size" comparison the issue specifies.
- **Cool-down flag** (`cooldownActive()`): trips after `COOLDOWN_LOSS_STREAK` (3, a deliberate round default, not fitted — same posture as `SIZING_K`/`SIZING_RISK_PCT`) consecutive logged losses; a win resets the streak, open/scratch trades are skipped. Surfaces as a `.cooldownnote` banner on both the Discipline tab and the Screener tab (linking back to Discipline), reminding the user to reduce risk% or sit out rather than widen a stop after a loss.

## ⚠️ Open Items
- [ ] **Define the screener spec** — which instruments, which columns, which filters/sorts. ("We will have to iron out the project screener — figure out later.")
- [ ] **Decide the data-sharing mechanism** — shared JSON/volume, shared DB, or new app calls old app's `/api/stocks`. Drives the hosting design.
- [ ] **Confirm hosting model** — single Fly.io pipeline feeding both apps vs separate. Cost-driven (prefer one collector). See ARCHITECTURE.md.
- [ ] **Cadence** — does the trader screener need faster-than-nightly data for any metric? If so, where does that fit without a second always-on machine?
- [ ] **New metrics → shared pipeline** — agree that trader-specific metrics get added upstream so old app + Research reuse them.
- [x] **Instrument coverage (options)** — decided (issue #45): liquidity-gated subset of the existing equity/ETF universe, not a separate options instrument class. Futures/forex options coverage still undecided.
- [ ] **Auth / accounts** — reuse old `auth.py` / `accounts_database.py` or separate?

### Options-Chain Feed — open items
- [x] **Scope** — decided (issue #45): liquidity-gated subset (`passesLiquidityFloor()`), not the full universe; nearest 4–6 expirations per ticker.
- [x] **Data source longevity** — decided (issue #45): paid feed, Polygon.io primary (see "Data source" above). yfinance ruled out (no Greeks, delayed).
- [ ] **Cadence** — nightly snapshot to start; decide if/when intraday or on-demand refresh is needed (without an always-on machine — hosting cost).
- [x] **Greeks** — decided (issue #45): sourced directly from Polygon per contract; in-house Black-Scholes no longer needed for v1.
- [x] **History / storage** — decided (issue #45): append-only `iv_snapshots` table (ticker, snapshot_date, atm_iv_30d), fixed 30-day-ATM definition, started now so the ~1yr IV Rank window populates.
- [ ] **Shared-pipeline placement** — confirm `options.py` runs in the same nightly Fly.io job so the old app gets it too (collect once).
- [ ] **API shape** — finalize `/api/options/...` routes + which derived metrics surface as screener columns/filters beyond `iv_rank`/`iv_percentile` (already specified above).
