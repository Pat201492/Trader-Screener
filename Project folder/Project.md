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
- `GET /api/options/{ticker}` → expirations + summary metrics (ATM IV, IV Rank, IV Percentile, put/call).
- `GET /api/options/{ticker}/{expiration}` → full calls/puts grid for one expiry.
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
