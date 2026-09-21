#!/usr/bin/env python3
"""The EDGAR source: filings in, raw rows out.

This fetches through `tools/edgar_scrubber/edgar_client.EdgarClient` and defines
no HTTP of its own. There is already one SEC-compliant client in this repo, with
the shared rate limiter and the on-disk cache behind it; a second one would be a
second answer to "have we been polite to the SEC today".

What lands in the store is what EDGAR returned -- accession, CIK, form, filing
date, document URL -- and nothing derived. No field extraction, no scoring, no
interpretation. The rows are the evidence a brief is later checked against, so
anything inferred here would be an inference the brief could no longer be
audited against.

    rows = pull(store, client, forms=["8-K"], since="2025-07-01",
                until="2026-01-01", ciks=["0000320193"])
"""

import json

from tools.edgar_scrubber.edgar_client import cik10, cik_bare

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
SOURCE_ID = "edgar"


class EdgarSourceError(Exception):
    """The pull could not be run as asked."""


def resolve_tickers(client, tickers):
    """Map tickers to zero-padded CIKs through the EDGAR ticker file.

    Uses the client's own `get_json`, so this goes through the same limiter and
    cache as every other request.
    """
    wanted = {t.strip().upper() for t in tickers if t and t.strip()}
    if not wanted:
        return {}, []
    table = client.get_json(TICKER_MAP_URL)
    rows = table.values() if isinstance(table, dict) else table
    found = {}
    for row in rows:
        sym = str(row.get("ticker", "")).upper()
        if sym in wanted:
            found[sym] = cik10(row["cik_str"])
    missing = sorted(wanted - set(found))
    return found, missing


def _recent(sub):
    """The parallel-array `filings.recent` block, or an empty one."""
    return (sub or {}).get("filings", {}).get("recent", {}) or {}


def iter_rows(client, cik, forms=None, since=None, until=None):
    """Yield one row per filing for a CIK, filtered by form and filing date.

    Rows carry what the submissions feed states. Nothing is computed from them.
    """
    sub = client.submissions(cik)
    blk = _recent(sub)
    n = len(blk.get("accessionNumber", []) or [])
    company = sub.get("name", "")
    want = {f.upper() for f in forms} if forms else None

    for i in range(n):
        form = blk["form"][i]
        if want and form.upper() not in want:
            continue
        filing_date = blk["filingDate"][i]
        if since and filing_date < since:
            continue
        if until and filing_date > until:
            continue

        accession = blk["accessionNumber"][i]
        doc = (blk.get("primaryDocument") or [""] * n)[i]
        bare = cik_bare(cik)
        acc_nodash = accession.replace("-", "")
        yield {
            "accession": accession,
            "cik": cik10(cik),
            "company": company,
            "form": form,
            "filing_date": filing_date,
            "doc_url": (ARCHIVE.format(cik=bare, acc=acc_nodash, doc=doc)
                        if doc else ""),
        }


def pull(store, client, forms=None, since=None, until=None, ciks=None,
         tickers=None):
    """Run an EDGAR pull into the store. Returns the pull record.

    `query` on the record is exactly what was asked for, so a brief built from
    these rows can always be traced back to the request that produced them.
    """
    ciks = list(ciks or [])
    tickers = list(tickers or [])
    if not ciks and not tickers:
        raise EdgarSourceError("an EDGAR pull needs at least one CIK or ticker")

    resolved, missing = resolve_tickers(client, tickers)
    targets = [cik10(c) for c in ciks] + [resolved[t] for t in sorted(resolved)]
    # dedupe, first mention wins, so the row order follows the request
    seen, ordered = set(), []
    for c in targets:
        if c not in seen:
            seen.add(c)
            ordered.append(c)

    query = {
        "forms": list(forms or []),
        "since": since,
        "until": until,
        "ciks": list(ciks),
        "tickers": list(tickers),
    }
    record = store.create_pull(source=SOURCE_ID, query=query)

    written = 0
    for cik in ordered:
        rows = list(iter_rows(client, cik, forms=forms, since=since, until=until))
        written += store.write_rows(record["id"], rows)

    record["row_count"] = written
    record["unresolved_tickers"] = missing
    return record
