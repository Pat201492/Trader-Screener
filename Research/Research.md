# Research — The Research Terminal

> The terminal used to pull the most up-to-date research on whatever we are tracking. It is **driven by the metrics the Project screener tracks** — so it reuses the same collected data rather than gathering its own. Think "Bloomberg-terminal-lite" sitting on top of the shared pipeline.

---

## Dashboard section: Research → Projects (issue #97)

`ARCHITECTURE.md` names Research a co-equal pillar; this is its first app surface. The `research` workspace appears on the dashboard home grid (`web-dashboard/index.html`, `SECTIONS.research`) with a single **Projects** tab rendered by `renderResearchProjects()`.

**A project is a hypothesis, not a folder.** Each card is one *falsifiable claim* and refuses to hide the fields that make it falsifiable:

| Field | Meaning |
|---|---|
| **Hypothesis** | One sentence, stated as a prediction. |
| **Falsifier** | The observation that would kill it. A project with no falsifier is not a project. |
| **Tools** | Which Data Gathering / Analysis Model modules feed it — chips link into the `tools` workspace. |
| **Trial count** | How many variants will be tested, **declared before the run** — the same pre-commitment the screener's promotion gate enforces (issue #47). Declared after the fact, the deflation correction is meaningless. |
| **Look-ahead posture** | Passes the `lookahead-gate/` A-vs-B truncation test (issue #42), or explicitly marked **n/a with a reason**. |
| **Status** | `proposed` / `running` / `supported` / `refuted` / `inconclusive`. |
| **Verdict** | Populated at the end — including the refuted and inconclusive ones. Especially those. |

A project missing its **falsifier** or **trial count** renders a loud ⚠ warning badge (and a banner in the detail view) rather than silently looking complete. The detail view surfaces trial count and look-ahead posture as prominent cards, not footnotes.

### Registry — `web-dashboard/research-projects.json`

Projects are **runtime-loaded** from `research-projects.json`, the same rule as the tools manifest — no catalog literals in `index.html`. Demo mode serves an in-memory mirror via `window.__DEMO_RESEARCH__` (`demo-data.js`) and ships one sample project end-to-end (`congress-buy-clusters`, refuted with a full verdict) plus one deliberately incomplete project (`uoa-direction`) to exercise the warning path.

Registry entry shape:

```json
{
  "id": "congress-buy-clusters",
  "title": "Congress buy-clusters precede 90-day outperformance",
  "hypothesis": "…one-sentence prediction…",
  "falsifier": "…the observation that refutes it…",
  "tools": [{ "id": "smart-money-score", "label": "Smart Money Score" }],
  "trialCount": 12,
  "lookahead": { "status": "passed", "note": "…truncation-test note…" },
  "status": "refuted",
  "evidence": "…optional supporting detail…",
  "verdict": "…written up at the end, refuted ones included…"
}
```

`lookahead.status` is one of `passed` / `failed` / `na` (an `na` posture should carry a `note` reason); a missing `lookahead` reads as **undeclared** (red). Add a project by appending an entry to `research-projects.json` (and the demo mirror in `demo-data.js` if it should show in demo mode).

---

## What it is
A view that, for any instrument (or the whole universe), surfaces the freshest research and data we have on it — fundamentals, valuation, news/sentiment, macro context, and smart-money (congress/insider) activity — all from the **same datastore** the Project screener and old app already populate.

The principle: **the Project tab decides *what* we track; the Research tab is *how we read it* up to date.** Any metric added to the Project screener automatically becomes researchable here, because both read the shared pipeline output.

---

## Data reuse (no new collection)
Everything the Research terminal shows already exists in the shared pipeline (see [Project.md](../Project%20folder/Project.md) and root [ARCHITECTURE.md](../ARCHITECTURE.md)):

| Research view | Backed by | Already collected by |
|---|---|---|
| Fundamentals snapshot | `fundamentals.json` | `fundamentals.py` (yfinance) |
| Valuation / score | `model.json` | `model.py` (DCF + Comps + EPV/Graham) |
| News + sentiment feed | News tables | `news.py` (yfinance + TextBlob) |
| Macro / rates context | FRED series | `fred.py` (St. Louis Fed) |
| Smart-money activity | Politician + insider tables | `ingest_*` (Senate/House/EDGAR) |
| Universe / sector context | `universe.json` | `universe.py` (NASDAQ Trader FTP) |
| Options chain + IV/Greeks/put-call | `option_contracts` table / options cache | `options.py` (yfinance chains + computed metrics) |

The terminal queries the **same** API (`/api/stocks`, `/api/stocks/{ticker}`, news, fed, politicians routes) the old app exposes — it does not re-scrape sources.

---

## Latest-and-greatest layer
Beyond cached pipeline data, Research is where "most up to date" lives:
- Pull newest news/sentiment per tracked ticker (reuse `news.py` output; refresh on demand).
- Pull newest macro prints (reuse `fred.py`).
- Optionally pull external research/articles per instrument (shared with the Education feed infra — same fetch-and-cache mechanism).

---

## ⚠️ Open Items
- [ ] **Define "research" scope** — internal metrics digest only, or also external article fetch (web research) per instrument?
- [ ] **Refresh model** — on-demand pull vs piggyback on the nightly pipeline. On-demand needs a cheap, stateless fetch path (hosting-cost aware).
- [ ] **Metric coupling** — formalize that Research auto-inherits any new Project metric (single schema, both tabs read it).
- [ ] **Terminal UX** — command/search-driven (Bloomberg-style) vs dashboard panels?
- [ ] **Shared fetch infra with Education** — both want "pull latest articles"; build one cached fetcher, not two.
- [ ] **Real-time vs cached** — decide which research data must be live vs nightly (cost driver — avoid a second always-on collector).
