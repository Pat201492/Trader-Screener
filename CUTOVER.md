# Cutover — Repoint apps at Stock-Data-Pipeline

Tracking checklist for moving from "pipeline lives inside the old app" to the **3-repo** design: [Stock-Data-Pipeline](https://github.com/Pat201492/Stock-Data-Pipeline) (only writer) + Stock-App + Trader-Screener (readers).

**Guiding rule:** do NOT delete pipeline code from the old app until both apps read from the new source. Delete-before-repoint breaks the old app.

---

## Phase 0 — Safety / pre-flight
- [ ] Commit or stash uncommitted work on Stock Tracker V3 (`feat/mobile-fed-pol-stockdetail-v2` has changes — don't lose it).
- [ ] **Deploy drift:** pull live Fly source before anything — the running machine may be ahead of git. Reconcile into Stock-Data-Pipeline so the extracted copy isn't stale.
- [ ] **Secret rotation:** check if the Google service-account key (`stock-stracker-*.json`) was ever committed to the old Stock-App history. If yes → rotate the key, scrub history.
- [ ] Tag the old app pre-cutover (`git tag pre-cutover`) for rollback.

## Phase 1 — Stand up the data service (in Stock-Data-Pipeline)
- [ ] Migrate the **read-only** data API out of the old `server.py` into the pipeline repo:
  - `GET /api/stocks` (+ all filters/sort/paging)
  - `GET /api/stocks/{ticker}`, `/score`
  - news / fed / politicians / insider read routes
  - ETF read routes
- [ ] Leave **app-only** routes in Stock-App: auth, accounts, static HTML, notifications.
- [ ] Decide read access: HTTP API (recommended) vs shared DB/volume vs published artifacts.
- [ ] Deploy the pipeline service once on Fly (single collector + API). Confirm one cron only.
- [ ] Smoke-test every migrated endpoint against current prod responses (diff JSON).

## Phase 2 — Repoint Stock-App (old)
- [ ] Point the old web + mobile front-ends at the pipeline API base URL (config/env, not hardcoded).
- [ ] Remove the now-duplicated pipeline imports from `server.py` (it stops owning `database.py`/`data_utils.py` writes).
- [ ] Run old app against the service; verify screener, stock detail, news, politicians, ETF all load.
- [ ] Mobile: bump `config.ts` API base; regression test.

## Phase 3 — Repoint Trader-Screener (new)
- [ ] Trader-Screener client reads the same API base.
- [ ] Wire the Project screener to the pipeline `/api/stocks` filters (currently mobile uses none).
- [ ] Confirm options-chain (`options.py`) + commodities (`commodities.py`) collectors live in the pipeline repo, served to both apps.

## Phase 4 — Delete duplicated pipeline code from old app
> Only after Phases 1–3 are green.
- [ ] Remove from Stock-App: `universe.py fundamentals.py model.py news.py etf_universe.py fred.py ingest_*.py pol_refresh.py backfill_eps.py validate.py deepdive.py sheets.py run.py scheduler.py data_utils.py database.py politicians_database.py`.
- [ ] Keep in Stock-App: `server.py` (app routes only), `auth.py`, `accounts_database.py`, `notifications.py`, `cors_proxy.py`.
- [ ] Update old app README + Fly config (it no longer runs the cron — pipeline repo does).

## Phase 5 — Verify & decommission
- [ ] One collector running (pipeline repo). Confirm the old app's cron machine is OFF (no double-collect, no double cost).
- [ ] Daily `run.py` produces data; both apps read it; end-to-end green for 1 full daily cycle.
- [ ] Update all three READMEs + this checklist to "done".

---

## Open questions
- [ ] Read mechanism: **API** (clean, recommended) vs shared DB vs artifacts?
- [ ] Where does the DB physically live so both apps reach it (if not API)?
- [ ] Auth — does the data API need any, or is it private-network only?
- [ ] Local dev: how do contributors run the pipeline + both apps together?
