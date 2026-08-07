# edgar_scrubber — EDGAR client core (issue #99)

The foundation the whole EDGAR scrubber sits on (part of EPIC #95 "Research + Tools").
Every other scrubber issue — #100 (corpus counts), #102 (field spec), #106 (exemplars),
#107 (rules) — fetches SEC data **through this module**, so it lands first.

Same posture as [`lookahead-gate/`](../lookahead-gate/) and [`pipeline-mock/`](../pipeline-mock/):
a portable, **stdlib-only** reference implementation that runs today and is the concrete spec
for whoever folds EDGAR ingestion into the [Stock-Data-Pipeline](https://github.com/Pat201492/Stock-Data-Pipeline)
repo (ARCHITECTURE.md rule: *collect once, one writer*).

## No browser automation — on purpose

The SEC publishes JSON APIs. Search and fetch are **deterministic code**; the model only ever
touches *extraction* downstream. Driving the search page with a browser is a fragile
re-implementation of something already solved and makes runs non-reproducible, so it isn't done.

## What it wraps

| purpose | endpoint | method |
|---|---|---|
| full-text search, 2001→present | `efts.sec.gov/LATEST/search-index` | `full_text_search(...)`, `iter_hits(...)` |
| filing history for a CIK | `data.sec.gov/submissions/CIK##########.json` | `submissions(cik)` |
| document list for one accession | `www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json` | `filing_index(cik, acc)` |
| one document's bytes | `.../{cik}/{acc_nodash}/{file}` | `archive_document(cik, acc, file)` |
| pre-parsed XBRL facts | `data.sec.gov/api/xbrl/{companyfacts,companyconcept,frames}` | `company_facts` / `company_concept` / `frames` |
| quarterly form index (backfill) | `www.sec.gov/Archives/edgar/full-index/{yyyy}/QTR{n}/form.idx` | `form_index(year, qtr)` |

## Three things treated as load-bearing (not optional)

1. **SEC etiquette, or you get blocked.**
   - `User-Agent: <name> <email>` on every request — **configurable, with no default that ships
     someone else's address**. A UA without a contact email is rejected up front (`EdgarConfigError`).
   - **10 req/s ceiling** enforced by **one shared `RateLimiter`** across the whole process, not
     per-call-site. A parallel document fetcher can't exceed it just by adding threads.
   - `Accept-Encoding: gzip` + keep-alive connection reuse.
   - Retry-with-backoff on 429 / 5xx (honours `Retry-After`); a sustained **403 fails loud** as a
     config error (bad UA) instead of retrying into a longer ban.

2. **On-disk cache — the loop's iteration speed depends on it.** Content-addressed by URL, gzipped,
   sharded, atomic writes. The human-in-loop design (#102/#106/#107) re-processes the same documents
   dozens of times as the spec/exemplars/rules evolve — **re-extraction must never refetch.** A
   second identical run issues **zero** network requests.

3. **Issuer identification costs no extra request.** `display_names[0]` from a search hit already
   carries issuer + tickers + CIK in one string; `parse_display_name` / `parse_hit` split it out
   here, because issuer is the partition key for #106/#107.

## Use

```python
from edgar_client import EdgarClient, parse_hit

client = EdgarClient("Your Name you@example.com", cache_dir=".edgar-cache")

for hit in client.iter_hits(forms="424B2", startdt="2024-06-03", enddt="2024-06-07"):
    info = parse_hit(hit)                                  # issuer, cik, accession, document
    idx  = client.filing_index(info["cik"], info["accession"])
    primary = EdgarClient.primary_document(idx)
    doc  = client.archive_document(info["cik"], info["accession"], primary)   # bytes, cached
```

`hits.total.value` saturates at exactly **10000** (#100) — `search_total(page)` returns
`(value, saturated)` so callers narrow the date window instead of trusting a capped count. The efts
window is **100 pages of 100**, not 1000; `iter_hits` never asks past `from=10000`.

For a corpus that exceeds the window, see [`README_crawl.md`](README_crawl.md) (#100): a saved,
id'd query object plus a crawler that recursively date-bisects past the 10k cap and persists
resumable state to disk.

## Test

Canonical invocation, from the repo root (issue #138):

```
python -m pytest tools/edgar_scrubber/
```

`test_edgar_client.py` and its siblings still import their subjects flat (`import edgar_client as
ec`), same as `__init__.py`'s **relative** imports (`from .edgar_client import ...`) expect a
different `sys.path` state. `tools/edgar_scrubber/conftest.py` reconciles the two by putting this
directory on `sys.path` before collection, so pytest resolves the flat imports regardless of where
it's invoked from.

Script mode also still works, since Python already puts the script's own directory on `sys.path`:

```
python tools/edgar_scrubber/test_edgar_client.py
```

Stdlib only, exit 0 = pass (same convention as `lookahead-gate/ab_truncation_test.py`). Every #99
acceptance criterion is proven **offline** by injecting a fake transport that counts calls and
scripts status codes: cache → zero-network second run; the shared rate limiter verified **under
concurrency** (threads, not serially) and bounded to the ceiling; retry/backoff on 429/5xx; 403
fail-loud; issuer+CIK parsed from `display_names`; and every endpoint building its exact URL.

The live integration test #99 also names — search 424B2 over a one-week window → resolve an accession
→ fetch its primary document, under the rate cap, second run zero network — is gated so the suite
stays green offline. See [SETUP.md](SETUP.md#edgar_user_agent-sec-live-tests) for how to set
`EDGAR_USER_AGENT` on Windows/PowerShell; bash form:

```bash
EDGAR_LIVE=1 EDGAR_USER_AGENT="Your Name you@example.com" python tools/edgar_scrubber/test_edgar_client.py
```

```powershell
$env:EDGAR_LIVE = "1"
$env:EDGAR_USER_AGENT = "Your Name you@example.com"
python tools/edgar_scrubber/test_edgar_client.py
```

`EdgarClient(...)` also reads `EDGAR_USER_AGENT` itself when `user_agent` is omitted (not just in this
test), so any script can rely on the env var instead of hardcoding a UA in source.

## Porting into the real pipeline

The **response-shape handling and the four guarantees above** are what port unchanged. The actual
network transport (`HttpTransport`) is injectable (`transport=`) precisely so it can be swapped for
the pipeline's own HTTP stack while keeping the limiter, cache, retry, and parsing intact.
```
