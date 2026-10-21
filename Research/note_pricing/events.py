"""
Company event calendar per underlying (#366): the company's own price-moving
events, shown beside the note filings when the charts filter to one stock.

From EDGAR's submissions feed only -- form, filing date and (for 8-Ks) the
item codes are metadata; no document is fetched:

  annual_report       10-K, 10-K/A, 20-F, 40-F
  quarterly_report    10-Q, 10-Q/A
  earnings            8-K reporting item 2.02 (Results of Operations)
  shareholder_meeting 8-K reporting item 5.07 (vote results: the meeting happened)
  proxy               DEF 14A (the proxy that announces a meeting)
  presentation        8-K reporting item 7.01 (Reg FD: investor days, conferences)

Indices and ETF proxies have no company events. Cached per CIK for a day.
"""
import json
import os
import time
from datetime import date
from pathlib import Path

from data import HOME

EVENT_TYPES = ("annual_report", "quarterly_report", "earnings", "shareholder_meeting", "proxy", "presentation")
_FORM_TYPES = {"10-K": "annual_report", "10-K/A": "annual_report", "20-F": "annual_report",
               "40-F": "annual_report", "10-Q": "quarterly_report", "10-Q/A": "quarterly_report",
               "DEF 14A": "proxy"}
_ITEM_TYPES = {"2.02": "earnings", "5.07": "shareholder_meeting", "7.01": "presentation"}
NO_EVENTS = {"SPY", "QQQ", "IWM", "DIA"}


def classify(form, items):
    """Event types for one filing (an 8-K can be both earnings and a
    presentation)."""
    form = (form or "").strip().upper()
    if form in _FORM_TYPES:
        return [_FORM_TYPES[form]]
    if form in ("8-K", "8-K/A"):
        codes = [c.strip() for c in (items or "").split(",") if c.strip()]
        return [_ITEM_TYPES[c] for c in codes if c in _ITEM_TYPES]
    return []


def _rows(recent):
    keys = ("form", "filingDate", "accessionNumber", "items")
    cols = [recent.get(k) or [] for k in keys]
    n = len(cols[0])
    for i in range(n):
        yield {k: (c[i] if i < len(c) else None) for k, c in zip(keys, cols)}


def company_events(client, cik, start, end, home=HOME, max_age_s=86400):
    """[{date, type, form, accession}] for one company CIK, oldest first,
    between `start` and `end` (ISO dates). Reads the submissions feed's
    recent filings plus its older pages until they predate `start`."""
    cik = str(cik).lstrip("0")
    path = Path(home) / "events" / f"{cik}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - cached.get("fetched", 0) < max_age_s:
            return [e for e in cached["events"] if start <= e["date"] <= end]
    except (OSError, ValueError, KeyError):
        pass
    sub = client.submissions(cik)
    filings = (sub or {}).get("filings") or {}
    rows = list(_rows(filings.get("recent") or {}))
    for f in filings.get("files") or []:
        # Older pages, newest first: stop once a page ends before the window.
        if (f.get("filingTo") or "9999") < start:
            break
        page = client.get_json("https://data.sec.gov/submissions/" + f["name"])
        rows.extend(_rows(page or {}))
    events = []
    for r in rows:
        d = r.get("filingDate") or ""
        for t in classify(r.get("form"), r.get("items")):
            events.append({"date": d, "type": t, "form": r.get("form"), "accession": r.get("accessionNumber")})
    events.sort(key=lambda e: (e["date"], e["type"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": time.time(), "events": events}), encoding="utf-8")
    return [e for e in events if start <= e["date"] <= end]


def events_for_ticker(client, ticker, start, end, home=HOME):
    """Events for a ticker, or [] for an index/ETF proxy or an unknown one."""
    t = (ticker or "").upper()
    if not t or t in NO_EVENTS:
        return []
    try:
        cik = client.ticker_to_cik(t)
    except Exception:
        return []
    return company_events(client, cik, start, end, home)
