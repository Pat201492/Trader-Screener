# Trader Screener

Trader-oriented screener + research terminal, built on the shared data pipeline of the old Stock Tracker / Stock-App.

**Start here:** [ARCHITECTURE.md](ARCHITECTURE.md) — hosting, and how data is shared between the old app and this one (collect once, serve both).

## Live demo (no backend)

The dashboard runs fully in the browser on **synthetic sample data** — no pipeline or API needed. It exercises the real UI (magic-formula ranks, smart-money moat, sizing, IV rank, options-flow columns, macro regime, data-integrity) and is banner-labelled "synthetic data, not real markets."

- **Locally:** open [`web-dashboard/index.html?demo=1`](web-dashboard/index.html?demo=1), or serve the repo root and visit `/` (redirects into the demo).
- **On the web (optional):** enable GitHub Pages → served at `https://pat201492.github.io/Trader-Screener/`. ⚠️ Pages on a **private** repo needs a paid plan and makes the served static files public (including `Project folder/`, `Research/`, the `.md`s). To publish only the demo, use a `gh-pages` branch (or a separate public repo) containing just `web-dashboard/`.

## Pillars
- **[Education](Education/Education.md)** — learning the business of trading: economics, instruments, tools, recent examples, a structured path.
- **[Project](Project%20folder/Project.md)** — the screener; closest sibling to the old stock screener, reusing its data-collection pipeline.
- **[Research](Research/Research.md)** — terminal for the most up-to-date research on the metrics the Project tab tracks.

**Plans:**
- **[CUTOVER.md](CUTOVER.md)** — repoint both apps at the Stock-Data-Pipeline repo (phased checklist).
- **[Project/Commodities.md](Project%20folder/Commodities.md)** — commodities dashboard (energy/metals/ags), data collected in the shared pipeline.
- **[lookahead-gate/](lookahead-gate/)** — reference lagged-indicator implementation + Chan's A-vs-B truncation gate (issue #42; see `Project.md` § Look-ahead discipline layer).

> `old/` (a reference copy of the old project, gitignored) is **not** tracked in this repo.

Status: early planning — see the **Open Items** at the bottom of each MD.
