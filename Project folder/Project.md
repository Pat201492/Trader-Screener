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

---

## ⚠️ Open Items
- [ ] **Define the screener spec** — which instruments, which columns, which filters/sorts. ("We will have to iron out the project screener — figure out later.")
- [ ] **Decide the data-sharing mechanism** — shared JSON/volume, shared DB, or new app calls old app's `/api/stocks`. Drives the hosting design.
- [ ] **Confirm hosting model** — single Fly.io pipeline feeding both apps vs separate. Cost-driven (prefer one collector). See ARCHITECTURE.md.
- [ ] **Cadence** — does the trader screener need faster-than-nightly data for any metric? If so, where does that fit without a second always-on machine?
- [ ] **New metrics → shared pipeline** — agree that trader-specific metrics get added upstream so old app + Research reuse them.
- [ ] **Instrument coverage** — yfinance covers equities/ETFs/some crypto; futures/options/forex need a data source decision.
- [ ] **Auth / accounts** — reuse old `auth.py` / `accounts_database.py` or separate?
