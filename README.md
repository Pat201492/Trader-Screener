# Trader Screener

Trader-oriented screener + research terminal, built on the shared data pipeline of the old Stock Tracker / Stock-App.

**Start here:** [ARCHITECTURE.md](ARCHITECTURE.md) — hosting, and how data is shared between the old app and this one (collect once, serve both).

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
