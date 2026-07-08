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
| `atr` | Average True Range | not yet shipped — SCREENER_SHORTLIST §2b; feeds §2g sizing |
| `realized_vol` | trailing return stdev | not yet shipped — SCREENER_SHORTLIST §2b |
| `beta` | vs. SPY | not yet shipped — SCREENER_SHORTLIST §2b |

- **Wire the gate.** Port `lookahead_lag.py`'s functions (or the equivalent pandas/numpy computation, same
  invariant) into the Stock-Data-Pipeline indicator stage, and run `ab_truncation_test.py`'s pattern as a
  pre-publish assertion in that repo's `validate.py` / `run.py` — a non-zero exit blocks the nightly publish,
  same as any other data-quality gate.
- **Front-end disclosure.** The Screener footer carries a standing **"as-of prior close"** note (next to the
  liquidity `lqnote`) so every OHLCV-derived column is explicitly labeled as lagged, not live-quote.

---

## Options-Chain Feed (first feature)

The first trader-specific feature: for any ticker already in the shared universe, pull and serve its **options chain** — every listed expiration, with the full grid of calls/puts per strike.

### Data source
- **Primary:** `yfinance` — already a dependency of the shared pipeline. Provides chains with no extra account/key:
  - `yf.Ticker(sym).options` → list of expiration dates (`YYYY-MM-DD`).
  - `yf.Ticker(sym).option_chain(exp)` → `.calls` and `.puts` DataFrames.
- **Per-contract fields** yfinance returns: `contractSymbol`, `strike`, `lastPrice`, `bid`, `ask`, `change`, `percentChange`, `volume`, `openInterest`, `impliedVolatility`, `inTheMoney`, `lastTradeDate`.
- **Caveat:** yfinance options are delayed/snapshot and Greeks beyond IV are **not** provided — we compute Greeks (delta/gamma/theta/vega) ourselves (Black-Scholes) or pull a paid feed later. Quote freshness ≠ real-time; fine for screening, not for execution.

### Where it lives in the pipeline
New collector script in the **shared** pipeline (so the old app can surface it too — collect once, serve both):

```
options.py   # iterate universe (or a watchlist subset) → for each ticker:
             #   expirations = yf.Ticker(t).options
             #   for exp in expirations (cap N nearest): pull calls+puts
             #   normalize → store rows; derive metrics (see below)
```

- Slots into `run.py` after `fundamentals.py` (needs the universe; independent of model/news).
- **Cadence:** options data moves intraday, but the shared pipeline is nightly. Start nightly snapshot (cheap, no new always-on machine — respects the hosting constraint). Flag faster/on-demand refresh as an open item.
- **Scope control:** full chains for ~all tickers is large. Start with a **watchlist / top-N subset** and a cap on expirations per ticker (e.g. nearest 4–6) to keep storage + fetch time bounded.

### Derived metrics (computed, not fetched)
Layer trader signals on top of raw chains — these become screener columns and feed the Research tab:
- **IV rank / IV percentile** (needs IV history — accumulate over nightly snapshots).
- **Put/call ratio** (volume + open interest) per ticker.
- **ATM IV / IV skew** (term + strike skew).
- **Greeks** via Black-Scholes (delta/gamma/theta/vega) using the risk-free rate already pulled from **FRED** (`fred.py`) and dividend yield from `fundamentals.json` — reuse existing data, don't refetch.
- **Unusual activity** — volume ≫ open interest flags.

### Storage / schema
- New table(s): `options_expirations` (ticker, exp dates) + `option_contracts` (one row per contract per snapshot date) — or a per-ticker JSON cache mirroring the `fundamentals.json` pattern.
- Keep a **history** of snapshots so IV-rank / OI-trend metrics are possible (don't overwrite — append by snapshot date).

### API (mirror the old `server.py` style)
- `GET /api/options/{ticker}` → expirations + summary metrics (ATM IV, put/call, IV rank).
- `GET /api/options/{ticker}/{expiration}` → full calls/puts grid for one expiry.
- Optional screener filter additions on `/api/stocks`: `min_iv_rank`, `min_put_call`, `unusual_only`.

---

## ⚠️ Open Items
- [ ] **Define the screener spec** — which instruments, which columns, which filters/sorts. ("We will have to iron out the project screener — figure out later.")
- [ ] **Decide the data-sharing mechanism** — shared JSON/volume, shared DB, or new app calls old app's `/api/stocks`. Drives the hosting design.
- [ ] **Confirm hosting model** — single Fly.io pipeline feeding both apps vs separate. Cost-driven (prefer one collector). See ARCHITECTURE.md.
- [ ] **Cadence** — does the trader screener need faster-than-nightly data for any metric? If so, where does that fit without a second always-on machine?
- [ ] **New metrics → shared pipeline** — agree that trader-specific metrics get added upstream so old app + Research reuse them.
- [ ] **Instrument coverage** — yfinance covers equities/ETFs/some crypto; futures/options/forex need a data source decision.
- [ ] **Auth / accounts** — reuse old `auth.py` / `accounts_database.py` or separate?

### Options-Chain Feed — open items
- [ ] **Scope** — full universe vs watchlist/top-N; how many expirations per ticker to cap.
- [ ] **Data source longevity** — is yfinance (delayed snapshot, no Greeks) good enough, or do we budget a paid options feed (Polygon/Tradier/ORATS) later?
- [ ] **Cadence** — nightly snapshot to start; decide if/when intraday or on-demand refresh is needed (without an always-on machine — hosting cost).
- [ ] **Greeks** — confirm Black-Scholes-in-house using FRED risk-free rate + fundamentals dividend yield; American-style options approximation acceptable?
- [ ] **History / storage** — append-by-snapshot schema so IV-rank and OI trends are computable; storage growth bound.
- [ ] **Shared-pipeline placement** — confirm `options.py` runs in the same nightly Fly.io job so the old app gets it too (collect once).
- [ ] **API shape** — finalize `/api/options/...` routes + which derived metrics surface as screener columns/filters.
