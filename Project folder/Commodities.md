# Commodities Dashboard

A dashboard view in Trader-Screener for tracking commodity markets (energy, metals, agriculture) — prices, momentum, and term structure. Data is collected **once** in the [Stock-Data-Pipeline](https://github.com/Pat201492/Stock-Data-Pipeline) repo (a new `commodities.py` collector) so the old app and Research tab get it too.

---

## Why
Commodities are a core trader asset class (see [Education](../Education/Education.md)) and a macro signal — oil/gold/copper move with inflation, rates, and risk. The pipeline already pulls macro from **FRED** (`fred.py`); commodities extend that with tradable front-month futures.

---

## Data sources (reuse existing deps — no new account needed)
- **yfinance futures** (front-month continuous contracts):
  - Energy: `CL=F` WTI crude, `BZ=F` Brent, `NG=F` nat gas, `RB=F` gasoline, `HO=F` heating oil
  - Metals: `GC=F` gold, `SI=F` silver, `PL=F` platinum, `HG=F` copper, `PA=F` palladium
  - Agriculture: `ZC=F` corn, `ZW=F` wheat, `ZS=F` soybeans, `KC=F` coffee, `SB=F` sugar, `CT=F` cotton, `LE=F` live cattle
- **FRED** (`fred.py`, already wired) for official/spot + macro context:
  - `DCOILWTICO` (WTI), `DCOILBRENTEU` (Brent), `GASREGW` (retail gas), `DHHNGSP` (Henry Hub gas), gold/PPI series
- **Term structure:** pull the futures curve (front vs deferred months) from yfinance per root to derive **contango/backwardation**.

> Caveat: yfinance futures are delayed snapshots and continuous-contract roll handling is approximate. Fine for a dashboard; not for execution.

---

## Collector (in the pipeline repo)
```
commodities.py   # in Stock-Data-Pipeline, added to run.py after etf_universe
                 #   for each commodity root:
                 #     front-month quote (yfinance)
                 #     curve (nearest N expiries) -> contango/backwardation
                 #     FRED spot/macro overlay
                 #   normalize -> store snapshot (append by date for history)
```
- Slots into the **shared** nightly pipeline (`run.py`) → collect once, serve both apps.
- Append-by-snapshot so % changes, 52w range, and vol are computable over time.

## Derived metrics (computed)
- Spot / front-month price + daily / weekly / monthly / YTD % change.
- 52-week high/low + position in range.
- Realized volatility (e.g. 20-day).
- **Term structure:** contango vs backwardation + roll yield estimate.
- Optional: correlation to SPX / DXY / 10Y (reuse FRED rates already collected).

## Dashboard UX
- Grouped cards: **Energy / Metals / Agriculture**, each card = price, % change, sparkline, range bar, contango/backwardation badge.
- Macro strip up top: WTI, Gold, Copper ("Dr. Copper"), DXY, 10Y — the at-a-glance risk read.
- Tap a commodity → detail: curve chart, history, related equities/ETFs (e.g. gold → GLD, miners), recent news (reuse `news.py`).

## API (mirror existing `/api/...` style, served from pipeline repo)
- `GET /api/commodities` → all roots + summary metrics, grouped.
- `GET /api/commodities/{root}` → detail: curve, history, derived metrics.

---

## ⚠️ Open Items
- [ ] **Roots list** — finalize which commodities to track (energy/metals/ags above is a starting set).
- [ ] **Term-structure depth** — how many deferred expiries to pull per root (cost vs signal).
- [ ] **Data source longevity** — yfinance futures good enough, or budget a real futures feed (CME/Barchart) later?
- [ ] **Continuous-contract roll** — define roll method (volume vs calendar) for clean history.
- [ ] **Cadence** — nightly snapshot to start (no new always-on machine); decide if intraday is needed.
- [ ] **Equity links** — map each commodity to related ETFs/equities for the detail view.
- [ ] **Placement confirm** — `commodities.py` lives in Stock-Data-Pipeline (shared), not in the app. See [CUTOVER.md](../CUTOVER.md).
