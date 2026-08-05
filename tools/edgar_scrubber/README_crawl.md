# edgar_scrubber — date-chunked resumable crawl (issue #100)

Built on [`edgar_client`](README_client.md) (#99). Turns "enter EDGAR search criteria" into
a stored, id'd `SavedQuery`, and turns a full-corpus `iter_hits` call into a crawl that
survives the 10,000-result window and a killed process.

## The problem

`efts` full-text search caps pagination at 10,000 hits, and `hits.total.value` **saturates**
at exactly 10000 — the API returns HTTP 200 with a silently truncated result set, no error.
Measured live 2026-08-04: `forms=424B2` hit the 10000 cap for **2023, 2024, and 2025 full
years**, and a single month (March 2025: 8,472) can get close enough that a hardcoded
monthly loop is one busy month from silent data loss.

So chunking here is **recursive on the measured total**, not a fixed calendar loop: issue
the query, read `search_total`, and if it reads as "unknown, must split" (`>= 10000`, never
trusted as a count), bisect the date range and recurse. It bottoms out wherever the data
requires — weekly for a busy range, daily for a narrow one.

## Saved queries

```json
{
  "id": "424b2-structured-notes",
  "q": "\"contingent coupon\"",
  "forms": ["424B2"],
  "startdt": "2024-01-01",
  "enddt": "2026-08-04",
  "ciks": [],
  "excludeCiks": []
}
```

`ciks` is sent to `efts` as a server-side filter — the pilot scope (`queries/424b2-jpm-2025-pilot.json`)
uses it to keep the first crawl to **one issuer, one year** rather than the ~91k/year full
424B2 population. `excludeCiks` has no `efts` equivalent, so it's applied client-side when
hits are collected.

```python
from crawl import SavedQuery, Crawler, load_query
from edgar_client import EdgarClient

query = load_query("queries/424b2-jpm-2025-pilot.json")
client = EdgarClient("Your Name you@example.com", cache_dir=".edgar-cache")
result = Crawler(client, query).run()
print(result["summary"])
# {"query_id": "...", "done": True, "accessions": 1234, "chunks_issued": 52,
#  "chunks_pending": 0, "requests_made": 210, "cache_hit_rate": 0.34,
#  "failures": 0, "warnings": []}
```

Or from the command line:

```
python tools/edgar_scrubber/crawl.py queries/424b2-jpm-2025-pilot.json \
    --user-agent "Your Name you@example.com"
```

## Re-running a query id: incremental by default

State persists under `crawl_state/<query id>.json`. Re-running the **same id**:

- with the **same `q`/`forms`/`ciks`/`excludeCiks`** and an extended `startdt`/`enddt`
  — only the newly-uncovered date span is fetched; `run()`'s `new_accessions` is the diff
  against the prior run, so incremental crawls are the normal case;
- with **different search criteria** under the same id — the id was repurposed, so the
  state resets clean instead of silently mixing two incompatible result sets.

## Resumable

State saves to disk after every leaf chunk resolves — success or logged failure — and
**never mid-chunk**. Kill the process and restart: the frontier (`pending`) on disk is
always consistent, so a resumed crawl never skips a chunk (no gap) and, at worst, re-does
the one chunk that was in flight when it died (harmless — accessions are deduped by id).

## Test

```
python tools/edgar_scrubber/test_crawl.py
```

Stdlib only, no network, exit 0 = pass. Covers every #100 acceptance criterion: a saturated
query's result set equals the union of its manually-bisected halves; `total == 10000` always
triggers a split and is never trusted as a count; a hand-built partial state file proves
resume issues zero duplicate requests and closes the gap; and the run summary's accession /
chunk / request / cache-hit-rate fields.

## References

- Issue #100 (this module) — part of #95, built on #99
- `edgar_client.py` / [`README_client.md`](README_client.md) — the fetch/cache/limiter foundation
