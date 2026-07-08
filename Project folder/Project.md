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
