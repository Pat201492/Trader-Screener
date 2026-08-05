# Trader Screener — Architecture, Hosting & Data Interconnectivity

> Read this first. It explains how the **new** Trader Screener relates to the **old** Stock Tracker / Stock-App, how data flows between them, and the hosting constraints that drive every design decision below.

## Three repos (data-service + two clients)

The data pipeline has been **extracted into its own repo** so both apps read from one source instead of each running their own collector:

| Repo | Role | Writes data? |
|---|---|---|
| **[Stock-Data-Pipeline](https://github.com/Pat201492/Stock-Data-Pipeline)** | The single source of truth. Collects/enriches/scores/stores all data. Daily local runner `run.py` + Fly nightly `scheduler.py`. | **Yes — only writer** |
| **Stock-App** (old) | Web + mobile front-end | No — reads |
| **Trader-Screener** (this) | Trader screener + research + options-chain feed | No — reads |

Both clients are **readers**; the pipeline repo is the **only writer**. The daily update program (`run.py`) lives in the pipeline repo and follows it.

This repo has three pillars, each documented in its own folder:
- **[Education](Education/Education.md)** — learning the business of trading (reading path + recent examples).
- **[Project](Project%20folder/Project.md)** — the screener itself; closest sibling to the old stock screener.
- **[Research](Research/Research.md)** — the terminal that reads the freshest data on what we track.

And `old/Stock-App/` — a reference copy of the old project.

---

## The two projects

| | **Old** — Stock Tracker / Stock-App | **New** — Trader Screener |
|---|---|---|
| Purpose | Long-term stock valuation + screening | Trader-oriented screening across instruments |
| Data pipeline | now lives in **Stock-Data-Pipeline** repo (`run.py` daily / `scheduler.py` Fly nightly) | **Reads the same pipeline output** |
| Sources | NASDAQ FTP, yfinance, SEC EDGAR, FRED, Senate/House disclosures | **Same sources — collected once** |
| API | FastAPI `server.py` (`/api/stocks`, etc.) | Reads the same API / same data |
| Hosting | Fly.io (app + nightly cron machine) | Shares the Fly.io data layer |

---

## Core principle: collect once, serve both

**When data uploads into the old app, it must also be present in the new project.** The intended design is *not* two parallel copies of the pipeline — it is **one collection pipeline writing one datastore that both front-ends read.**

```
                 ┌─────────────────────────────────────────────┐
                 │   STOCK-DATA-PIPELINE repo (own GitHub repo) │
                 │   run.py daily (local) / scheduler.py (Fly)  │
                 │  universe → fundamentals → model → news →    │
                 │  etf_universe → options   + fred + ingest_*  │
                 │  (congress, insider, EDGAR)                  │
                 └───────────────┬─────────────────────────────┘
                                 │ writes once
                  ┌──────────────┴───────────────┐
                  ▼                               ▼
        universe.json / fundamentals.json /  DB tables (stocks, news,
        model.json / etf data               politicians, insiders, macro)
                  │                               │
        ┌─────────┴─────────┐         ┌───────────┴───────────┐
        ▼                   ▼         ▼                       ▼
   OLD Stock-App      NEW Trader Screener            Research terminal
   (web + mobile)     Project screener               (reads same data)
                                         Education feed (shared fetcher)
```

Rules that fall out of this:
1. **Don't fork the ingest scripts.** Both apps point at the same output (`*.json` + DB) or the same `/api/stocks` API.
2. **New metric → add it upstream** in the shared pipeline, so the old app, Project screener, and Research tab all get it for free.
3. **One source of truth.** A ticker's fundamentals/score/news exist in exactly one place.

### Exploratory tools stay local until they graduate

A tool that is still figuring out *what* to extract (the [EDGAR scrubber](tools/edgar_scrubber/OUTPUT_STORE.md), #95/#109) is **exploratory, human-supervised, and project-local** — it writes only to its own local datastore and Trader-Screener reads it like any other reader. It is **not** a second writer of shared data; the only-writer rule above is unchanged. When a field proves out inside a Research project it **graduates** upstream via the [graduation checklist](tools/edgar_scrubber/OUTPUT_STORE.md#graduation-checklist): the extraction ports into the pipeline's EDGAR ingest, the field joins the shared contract (rule 2), the manifest's `graduatedTo` is set, and the **local extractor is retired** — never left running in parallel (two extractors for one field is two answers). Age-since-last-use is surfaced on the tool manifest so a local field that has stopped being explored reads as drift, not silently as still-exploratory.

---

## Hosting (cost-driven)

The old pipeline already runs on **Fly.io**: a cron machine fires `scheduler.py → run.py` nightly (~2am UTC); the API + front-ends read the resulting data. Constraints:

- **Keep a single collector.** Do **not** stand up a second always-on machine to re-collect the same data for the new project — that doubles cost for zero new information. One nightly pipeline feeds everything.
- **Prefer free-tier + graceful degradation** over an always-on paid service where possible.
- **Shared datastore.** Both apps read the same Fly volume / DB / API. The new front-end can be a separate deploy, but it consumes the *same* data backend.
- **On-demand pulls** (Research "latest", Education articles) should be cheap, stateless fetch paths — not a persistent worker.
- **Deploy drift caution:** the live Fly source may be ahead of git. Pull from the live machine before deploying so shared-pipeline changes aren't clobbered.

---

## Interconnectivity checklist (what "shared" actually requires)
- [ ] Decide the sharing mechanism: **shared JSON/volume**, **shared DB**, or **new app calls old `/api/stocks`**. (Leaning: reuse the existing API + DB; cheapest.)
- [ ] Single schema for any metric tracked, so Project + Research + old app read identical fields.
- [ ] Pipeline ownership: the nightly Fly job stays the **only** writer; both apps are readers.
- [ ] One shared fetcher for "latest articles" used by both Education and Research.
- [ ] Confirm whether the new app needs its own auth or reuses old `auth.py` / `accounts_database.py`.

---

## ⚠️ Top-level Open Items
- [ ] **Screener spec** for the Project tab is still TBD ("we will iron out the project screener later").
- [ ] **Sharing mechanism + hosting topology** not yet finalized — this is the gating decision; everything else depends on it.
- [ ] **Cadence**: nightly is fine for fundamentals/valuation; trader signals (vol/liquidity/momentum) may want faster — decide without adding an always-on collector.
- [ ] **Instrument coverage** beyond equities/ETFs (options/futures/forex/crypto) needs a data-source decision; yfinance won't cover all.
- [ ] **Education/Research**: static vs living feed — if living, they share the fetch + cache infra.
- [ ] `old/Stock-App/` is currently the stale GitHub `master` layout (docs/mobile-app/web-dashboard), **not** the live pipeline code — point reuse at the actual live Stock Tracker, not this reference copy.
