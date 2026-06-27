# Research — The Research Terminal

> The terminal used to pull the most up-to-date research on whatever we are tracking. It is **driven by the metrics the Project screener tracks** — so it reuses the same collected data rather than gathering its own. Think "Bloomberg-terminal-lite" sitting on top of the shared pipeline.

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
