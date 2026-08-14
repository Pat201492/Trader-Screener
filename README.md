# Trader Screener

Trader-oriented screener + research terminal, built on the shared data pipeline of the old Stock Tracker / Stock-App.

**Start here:** [ARCHITECTURE.md](ARCHITECTURE.md) — hosting, and how data is shared between the old app and this one (collect once, serve both).

## Live demo (no backend)

The dashboard runs fully in the browser on **synthetic sample data** — no pipeline or API needed. It exercises the real UI (magic-formula ranks, smart-money moat, sizing, IV rank, options-flow columns, macro regime, data-integrity, plus the **Research** hypothesis-project cards and the **Tools** module catalog — Data Gathering / Analysis Models, with the EDGAR 424B2 scrubber as the first tool) and is banner-labelled "synthetic data, not real markets."

- **Locally:** open [`web-dashboard/index.html?demo=1`](web-dashboard/index.html?demo=1), or serve the repo root and visit `/` (redirects into the demo).

## Running the tools from the dashboard (local)

The demo above is a **catalog** — Tools cards are links. To actually *run* a tool
from the page, start the local runner (stdlib only, loopback only):

```powershell
$env:EDGAR_USER_AGENT = "Your Name you@example.com"   # SEC fair-access requirement
$env:OLLAMA_MODEL     = "edgar-qwen7b:latest"          # the 8k-context build — see SETUP.md Step 2b
python tools/server.py                                 # http://127.0.0.1:8137/
```

It serves the same dashboard plus a small JSON API on one origin, and the Tools
workspace picks it up automatically:

- **Data Gathering → ▶ Open** gives the scrubber its own query surface:
  - **Search EDGAR** — your own phrase, form types, date range and optional CIKs
    (the saved `queries/*.json` load in as presets). Every hit links out to the
    filing on sec.gov, and the whole query opens in EDGAR's own full-text UI.
  - **By day (time series)** — sample *K filings per day* across the last *N days
    that actually have filings* (weekends and empty days are skipped, not counted).
    That is what makes an average an average over **time** rather than over filing
    volume.
  - **open + highlight** — read the filing's normalized text in-app, find within it,
    then **select any text and label it as a field**. The span is stored as a
    `correct` verdict plus a `corrected` exemplar, which is exactly what the
    extraction ladder reads on rung 3 (#105/#106) — hand-marked spans are training
    data for the next run, not notes.
  - **check the first filing** — a dry run on ONE filing before committing to a
    sample: every value it would pull, highlighted in the sentence it came from,
    with ✓ to confirm each one. Nothing is written to the output store.
  - **Extract selected** — runs the ladder over the filings *you* ticked, with the
    fields *you* ticked (all 33 spec fields are selectable). The progress bar moves
    per **field** (a filing is minutes wide), reports what is in flight and the
    measured time remaining, and **stop** ends the run at the next field boundary —
    the document in flight is still written, so stopping loses nothing. Local rungs
    only — Claude escalation stays off, so a field the confidence gate wanted to
    escalate returns flagged `gated_no_claude` rather than silently resolved.

  **Spans are earned, not asserted.** The local model's own span is unreliable
  (measured against real filings it returns empty ranges and ones thousands of
  characters wide), so both the preview and the stored record locate the value's
  own text in the document and highlight *that* — recording whether the span came
  from the model or from a value match. A value stated nowhere in the text gets no
  span and a `span_unlocatable` flag rather than a highlight pointing at the wrong
  sentence.
- **Results** reads the scrubber's local sqlite store, so past runs outlive the tab,
  and charts **average over time**: per-filing-day mean/min/max of any numeric field
  (grouped on the filing's own date, so re-extracting old filings never moves the series).
- **Docs** renders the tool's markdown in-app instead of sending you to GitHub.

Needs Ollama running with `qwen2.5:7b-instruct-q4_K_M` (see
[tools/edgar_scrubber/SETUP.md](tools/edgar_scrubber/SETUP.md)); the banner at the top
of the Tools tab reports exactly which prerequisite is missing. Ownership boundary is
unchanged (#109): the runner writes only the tool's own local store, never pipeline data.
- **On the web (optional):** enable GitHub Pages → served at `https://pat201492.github.io/Trader-Screener/`. ⚠️ Pages on a **private** repo needs a paid plan and makes the served static files public (including `Project folder/`, `Research/`, the `.md`s). To publish only the demo, use a `gh-pages` branch (or a separate public repo) containing just `web-dashboard/`.

## Pillars
- **[Education](Education/Education.md)** — learning the business of trading: economics, instruments, tools, recent examples, a structured path.
- **[Project](Project%20folder/Project.md)** — the screener; closest sibling to the old stock screener, reusing its data-collection pipeline.
- **[Research](Research/Research.md)** — terminal for the most up-to-date research on the metrics the Project tab tracks.

**Plans:**
- **[CUTOVER.md](CUTOVER.md)** — repoint both apps at the Stock-Data-Pipeline repo (phased checklist).
- **[Project/Commodities.md](Project%20folder/Commodities.md)** — commodities dashboard (energy/metals/ags), data collected in the shared pipeline.
- **[lookahead-gate/](lookahead-gate/)** — reference lagged-indicator implementation + Chan's A-vs-B truncation gate (issue #42; see `Project.md` § Look-ahead discipline layer).
- **[pipeline-mock/](pipeline-mock/)** — reference/mock Stock-Data-Pipeline API (issue #81; see `Project.md` § Pipeline gap) so the screener + Options tab can be run and tested locally against real-shaped data.

> `old/` (a reference copy of the old project, gitignored) is **not** tracked in this repo.

Status: early planning — see the **Open Items** at the bottom of each MD.
