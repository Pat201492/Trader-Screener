"""
Real inputs for the dealer-hedging causal test (#340), built without a model.

`dealer_hedging_causal_test.py` has only ever run on a synthetic universe.
This module builds the real one from two local sources:

  * EDGAR (through the scrubber's `EdgarClient`):
      - issuance intensity: the number of 424B2 filings naming an underlying,
        per EDGAR file date, from full-text-search METADATA only (`total` for
        a one-day window -- no document is fetched for a count);
      - barrier levels: a sample of notes per underlying per month, read with
        the deterministic key-terms extractor (no model).
  * Stock-Data-Pipeline's `stocks.db` (read-only): daily closes and volume,
    market cap. Index underlyings are priced through their ETF proxies
    (Russell 2000 -> IWM, Nasdaq-100 -> QQQ, S&P 500 -> SPY, Dow -> DIA),
    which the pipeline keeps since its #65.

A barrier stated as a percent of the note's initial level becomes a price on
the proxy as pct x the proxy's close on the note's pricing date: a percent of
initial is scale-free, so the ETF stands in for the index exactly.

LOOK-AHEAD. Every event is keyed to its EDGAR file date (the public knowledge
date), never the pricing date. A note whose pricing date is AFTER its file
date (a preliminary supplement, terms not yet set) gets no barrier -- its
initial level is not known on the day it becomes public -- and is counted in
`skipped["preliminary"]` instead.

Cached under ~/.edgar-scrubber/research/dealer_hedging/ (past days never
change, so a rebuild only fetches new days).

  python Research/dealer_hedging_data.py build [--months 36] [--per-month 4]
  python Research/dealer_hedging_data.py coverage
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools" / "edgar_scrubber"))

CACHE_DIR = Path(os.environ.get("DEALER_HEDGING_CACHE")
                 or Path.home() / ".edgar-scrubber" / "research" / "dealer_hedging")
PIPELINE_DB = Path(os.environ.get("PIPELINE_STOCKS_DB")
                   or Path.home() / ".stock-data-pipeline" / "data" / "stocks.db")

# ── Underlyings: name as filings print it -> priceable ticker ───────────────

# Index underlyings and the ETF each is priced through. The phrase is what the
# 424B2 full-text search matches; the variants are other ways filings print
# the same index (registered marks are stripped before matching).
INDEX_UNDERLYINGS = {
    "IWM": ("Russell 2000", ("russell 2000",)),
    "QQQ": ("Nasdaq-100", ("nasdaq-100", "nasdaq 100")),
    "SPY": ("S&P 500", ("s&p 500",)),
    "DIA": ("Dow Jones Industrial Average", ("dow jones industrial average",)),
}

# Single stocks: ticker -> the company name as filings print it. Banks that
# issue or guarantee notes are left out on purpose (Goldman Sachs, JPMorgan,
# Morgan Stanley, Citigroup, Bank of America, Wells Fargo): their names appear
# in every note they issue, so a phrase count would measure their own
# issuance, not notes linked to their stock.
STOCK_UNDERLYINGS = {
    "NVDA": "NVIDIA Corporation", "TSLA": "Tesla, Inc.", "AAPL": "Apple Inc.",
    "AMZN": "Amazon.com, Inc.", "MSFT": "Microsoft Corporation",
    "META": "Meta Platforms, Inc.", "GOOGL": "Alphabet Inc.",
    "AMD": "Advanced Micro Devices, Inc.", "AVGO": "Broadcom Inc.",
    "NFLX": "Netflix, Inc.", "PLTR": "Palantir Technologies Inc.",
    "COIN": "Coinbase Global, Inc.", "MSTR": "MicroStrategy Incorporated",
    "INTC": "Intel Corporation", "MU": "Micron Technology, Inc.",
    "ORCL": "Oracle Corporation", "UBER": "Uber Technologies, Inc.",
    "CRM": "Salesforce, Inc.", "NOW": "ServiceNow, Inc.", "BA": "The Boeing Company",
    "XOM": "Exxon Mobil Corporation", "WMT": "Walmart Inc.",
    "COST": "Costco Wholesale Corporation", "LLY": "Eli Lilly and Company",
    "UNH": "UnitedHealth Group Incorporated", "HOOD": "Robinhood Markets, Inc.",
    "RDDT": "Reddit, Inc.", "CEG": "Constellation Energy Corporation",
    "DELL": "Dell Technologies Inc.", "SMCI": "Super Micro Computer, Inc.",
    "SHOP": "Shopify Inc.", "CRWD": "CrowdStrike Holdings, Inc.",
    "NEE": "NextEra Energy, Inc.", "DIS": "The Walt Disney Company",
    "NKE": "NIKE, Inc.", "PYPL": "PayPal Holdings, Inc.", "QCOM": "QUALCOMM Incorporated",
    "ADBE": "Adobe Inc.", "ARM": "Arm Holdings plc", "CVX": "Chevron Corporation",
    "V": "Visa Inc.", "MA": "Mastercard Incorporated", "NBIS": "Nebius Group N.V.",
    "APP": "AppLovin Corporation", "VST": "Vistra Corp.", "SOFI": "SoFi Technologies, Inc.",
    "RIVN": "Rivian Automotive, Inc.",
}

_MARKS = re.compile(r"[®™℠]|\b(?:TM|SM)\b|(?<=[a-z0-9])(?:TM|SM)\b")
_LEAD = re.compile(r"^(?:the\s+)?(?:class\s+[a-z]\s+)?(?:common\s+stock|ordinary\s+shares|"
                   r"american\s+depositary\s+shares)\s+of\s+(?:the\s+)?", re.I)


def normalize_name(name):
    """Lowercase, registered marks and the "common stock of" lead-in removed,
    whitespace collapsed: "Class A common stock of Meta Platforms, Inc." and
    "Meta Platforms, Inc." compare equal; "Russell 2000® Index" contains
    "russell 2000"."""
    s = _MARKS.sub("", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip()
    s = _LEAD.sub("", s)
    return s.lower().rstrip(" .")


def _stock_key(name):
    s = normalize_name(name)
    s = re.sub(r"^the ", "", s)
    return re.sub(r"[^a-z0-9&]+", " ", s).strip()


_STOCK_KEYS = {_stock_key(n): t for t, n in STOCK_UNDERLYINGS.items()}


def ticker_for_name(name):
    """The priceable ticker for an underlying name as a filing prints it, or
    None. Index names map to their ETF proxy; single stocks match their
    company name exactly once marks and lead-ins are stripped -- a partial
    match ("Nasdaq-100 Technology Sector Index") is NOT the Nasdaq-100."""
    n = normalize_name(name)
    for tk, (_, variants) in INDEX_UNDERLYINGS.items():
        for v in variants:
            if n == v or n == v + " index" or n == "the " + v + " index":
                return tk
    return _STOCK_KEYS.get(_stock_key(name))


def search_phrase(ticker):
    if ticker in INDEX_UNDERLYINGS:
        return INDEX_UNDERLYINGS[ticker][0]
    return STOCK_UNDERLYINGS[ticker]


# The plain index name matches boilerplate in nearly every 424B2 ("S&P 500":
# ~4,861 of September 2026's ~10,000), so an index is counted by the phrases a
# note's TITLE uses when it is linked to that index (#354; ~1,270 for the S&P
# 500 that month). A worst-of basket's title names its first member only, so
# an index listed second or third is not counted -- a consistent undercount.
INDEX_TITLE_PHRASES = ("Linked to the {}", "Linked to the Performance of the {}", "Performing of the {}")


def search_query(ticker):
    """The full-text-search query that finds 424B2s on `ticker`: a stock's
    quoted company name, or the OR of an index's note-title phrases."""
    if ticker in INDEX_UNDERLYINGS:
        name = INDEX_UNDERLYINGS[ticker][0]
        return " OR ".join('"' + p.format(name) + '"' for p in INDEX_TITLE_PHRASES)
    return '"' + STOCK_UNDERLYINGS[ticker] + '"'


def all_tickers():
    return list(INDEX_UNDERLYINGS) + list(STOCK_UNDERLYINGS)


# ── Prices (pipeline stocks.db, read-only) ──────────────────────────────────

class PriceSource:
    """Daily closes/volume and market cap from the pipeline's stocks.db,
    opened read-only. `rows` lets tests inject {ticker: [(date, close, vol)]}."""

    def __init__(self, db_path=PIPELINE_DB, rows=None, caps=None):
        self._rows, self._caps = rows, caps
        self._conn = None
        if rows is None:
            self._conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)

    def series(self, ticker):
        """[(iso_date, close, volume)] oldest first."""
        if self._rows is not None:
            return sorted(self._rows.get(ticker, []))
        return self._conn.execute(
            "SELECT date, close, volume FROM price_history WHERE ticker = ? "
            "AND close IS NOT NULL ORDER BY date", (ticker,)).fetchall()

    def mkt_cap(self, ticker):
        if self._caps is not None:
            return self._caps.get(ticker)
        r = self._conn.execute("SELECT mkt_cap FROM stocks WHERE ticker = ?", (ticker,)).fetchone()
        return r[0] if r else None

    def close_on(self, ticker, iso):
        """The close on `iso`, or the last close before it (a pricing date
        can be a holiday); None when the series starts after it."""
        best = None
        for d, c, _ in self.series(ticker):
            if d > iso:
                break
            best = c
        return best


def oi_history(db_path=PIPELINE_DB):
    """{ticker: {iso_date: total_oi}} from the pipeline's options_oi_history
    (its #66), or {} before that table exists."""
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        rows = conn.execute("SELECT ticker, date, total_oi FROM options_oi_history").fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for tk, d, oi in rows:
        out.setdefault(tk, {})[d] = oi
    return out


# ── EDGAR: issuance counts (metadata only) and sampled barrier levels ───────

def _days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _with_query(cache, q):
    """(cache, changed): the cache if it answers query `q`, else an empty one
    tagged with it. A cache from before query tags was built with the quoted
    phrase, which is still a stock's query -- so it is kept and tagged; an
    index's old cache (plain phrase, #354) is discarded."""
    if cache.get("_query") == q:
        return cache, False
    legacy_ok = "_query" not in cache and " OR " not in q
    cache = dict(cache) if legacy_ok else {}
    cache["_query"] = q
    return cache, True


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def daily_counts(client, ticker, start, end, cache_dir=CACHE_DIR, today=None):
    """{iso_file_date: n_424B2_filings naming the underlying}, one search-
    metadata request per weekday (`total` only; no document fetched). Cached
    per ticker; a day is cached only once it is in the past, so today's
    still-growing count is refetched next time."""
    from edgar_client import search_total
    today = today or date.today()
    path = Path(cache_dir) / "counts" / f"{ticker}.json"
    q = search_query(ticker)
    cache, changed = _with_query(_load(path), q)
    for d in _days(start, end):
        iso = d.isoformat()
        if iso in cache:
            continue
        page = client.full_text_search(q=q, forms=["424B2"], startdt=iso, enddt=iso)
        n, _ = search_total(page)
        if d < today:
            cache[iso] = n
            changed = True
    if changed:
        _save(path, cache)
    return {k: v for k, v in cache.items() if k != "_query" and start.isoformat() <= k <= end.isoformat()}


def _month_ranges(start, end):
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        yield max(d, start), min(nxt - timedelta(days=1), end)
        d = nxt


def _slices(d0, d1, n):
    """`n` contiguous, non-overlapping date windows covering [d0, d1]."""
    span = (d1 - d0).days + 1
    n = max(1, min(n, span))
    edges = [d0 + timedelta(days=round(k * span / n)) for k in range(n + 1)]
    return [(edges[k], edges[k + 1] - timedelta(days=1)) for k in range(n)]


def _default_extract(client, cik, accession):
    import research_digest
    return research_digest.key_terms_for_filing(client, cik, accession)


def sample_notes(client, ticker, start, end, prices, per_month=4, cache_dir=CACHE_DIR,
                 extract=_default_extract):
    """Notes on `ticker` with their barrier as a price, sampled `per_month`
    per calendar month spread across that month's newest-first hits. Each:
      {accession, file_date, pricing_date, barrier_pct, start_close, barrier,
       autocall_pct, autocall}
    plus `skipped` counts by reason. A note is kept only when its own
    underlyings name this ticker and its pricing date is not after its file
    date (see LOOK-AHEAD in the module docstring). Cached per accession."""
    from edgar_client import parse_hit
    path = Path(cache_dir) / "notes" / f"{ticker}.json"
    q = search_query(ticker)
    cache, _ = _with_query(_load(path), q)
    seen = cache.setdefault("accessions", {})
    for m0, m1 in _month_ranges(start, end):
        key = f"month:{m0.isoformat()}"
        if key in cache and m1 < date.today():
            continue
        # One note from each of `per_month` equal slices of the month: search
        # returns the newest hits first, so a single query for the month would
        # sample only its last days on a busy underlying.
        picked = []
        for w0, w1 in _slices(m0, m1, per_month):
            page = client.full_text_search(q=q, forms=["424B2"], startdt=w0.isoformat(),
                                            enddt=w1.isoformat())
            hits = [parse_hit(h) for h in ((page.get("hits") or {}).get("hits") or [])]
            hit = next((h for h in hits if h.get("accession") and h.get("cik")
                        and h["accession"] not in seen
                        and h["accession"] not in {p["accession"] for p in picked}), None)
            if hit:
                picked.append(hit)
        for h in picked:
            acc = h["accession"]
            if acc in seen:
                continue
            try:
                terms = extract(client, h["cik"], acc) or {}
            except Exception as exc:
                seen[acc] = {"skip": f"extract failed: {type(exc).__name__}"}
                continue
            seen[acc] = _note_from_terms(ticker, acc, h.get("file_date"), terms)
        cache[key] = True
        _save(path, cache)
    notes, skipped = [], {}
    for acc, n in seen.items():
        if "skip" in n:
            reason = n["skip"].split(":")[0]
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        if not (start.isoformat() <= (n.get("file_date") or "") <= end.isoformat()):
            continue
        close = prices.close_on(ticker, n["pricing_date"])
        if close is None:
            skipped["no price"] = skipped.get("no price", 0) + 1
            continue
        out = dict(n, start_close=close, barrier=round(close * n["barrier_pct"] / 100, 4))
        if n.get("autocall_pct"):
            out["autocall"] = round(close * n["autocall_pct"] / 100, 4)
        notes.append(out)
    notes.sort(key=lambda n: (n["file_date"], n["accession"]))
    return notes, skipped


def _val(terms, f):
    v = terms.get(f)
    return v.get("value") if isinstance(v, dict) else v


def _note_from_terms(ticker, accession, file_date, terms):
    """The cacheable part of a note (no prices yet), or {"skip": reason}."""
    unds = _val(terms, "underlyings") or []
    if not unds:
        # An extractor miss, not evidence the note is on something else (#349:
        # Citi's bare lists read as nothing before the fix).
        return {"skip": "no underlyings extracted"}
    if not any(ticker_for_name(u.get("name") if isinstance(u, dict) else u) == ticker for u in unds):
        return {"skip": "not this underlying"}
    pct = _val(terms, "barrier_pct") or _val(terms, "coupon_barrier_pct")
    if not pct:
        return {"skip": "no barrier"}
    pricing = _val(terms, "pricing_date") or file_date
    if not file_date:
        return {"skip": "no file date"}
    if pricing > file_date:
        return {"skip": "preliminary"}
    return {"accession": accession, "file_date": file_date, "pricing_date": pricing,
            "barrier_pct": float(pct), "autocall_pct": _val(terms, "autocall_barrier_pct")}


# ── The dataset ─────────────────────────────────────────────────────────────

def build_dataset(client, prices, start, end, tickers=None, per_month=4,
                  cache_dir=CACHE_DIR, extract=_default_extract, log=print):
    """{"start", "end", "tickers": {tk: {"counts", "notes", "skipped", "days",
    "mkt_cap"}}, "skipped_tickers": {tk: reason}}. A ticker with no price
    history is listed in skipped_tickers, never silently dropped."""
    out = {"start": start.isoformat(), "end": end.isoformat(), "tickers": {}, "skipped_tickers": {}}
    for tk in tickers or all_tickers():
        series = prices.series(tk)
        if not series:
            out["skipped_tickers"][tk] = "no price history"
            log(f"{tk}: no price history -- skipped")
            continue
        counts = daily_counts(client, tk, start, end, cache_dir=cache_dir)
        notes, skipped = sample_notes(client, tk, start, end, prices, per_month=per_month,
                                      cache_dir=cache_dir, extract=extract)
        out["tickers"][tk] = {"counts": counts, "notes": notes, "skipped": skipped,
                              "days": len(series), "mkt_cap": prices.mkt_cap(tk)}
        log(f"{tk}: {sum(counts.values())} filings over {len(counts)} days, "
            f"{len(notes)} notes with barriers, skipped {skipped}")
    _save(Path(cache_dir) / "dataset.json", out)
    return out


def coverage(dataset):
    """A short summary for the dashboard and the results file."""
    t = dataset.get("tickers") or {}
    return {
        "start": dataset.get("start"), "end": dataset.get("end"),
        "underlyings_priced": len(t),
        "underlyings_skipped": len(dataset.get("skipped_tickers") or {}),
        "filings_counted": sum(sum(v["counts"].values()) for v in t.values()),
        "notes_with_barriers": sum(len(v["notes"]) for v in t.values()),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the dealer-hedging real-data inputs.")
    ap.add_argument("cmd", choices=["build", "coverage"])
    ap.add_argument("--months", type=int, default=36)   # ~3 years (#355)
    ap.add_argument("--per-month", type=int, default=4)
    ap.add_argument("--tickers", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "coverage":
        print(json.dumps(coverage(_load(CACHE_DIR / "dataset.json")), indent=1))
        return
    from edgar_client import EdgarClient
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise SystemExit("EDGAR_USER_AGENT is not set (SEC fair-access policy)")
    client = EdgarClient(user_agent=ua, cache_dir=str(Path.home() / ".edgar-scrubber" / "cache"))
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=round(a.months * 30.4))
    ds = build_dataset(client, PriceSource(), start, end, tickers=a.tickers, per_month=a.per_month)
    print(json.dumps(coverage(ds), indent=1))


if __name__ == "__main__":
    main()
