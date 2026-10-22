"""
Note pricing (#365): an issuer-sampled sample of structured-note terms, one
slim row per note, for fitting each bank's pricing function (#367).

Assumption under test (user, 2026-10-21): each issuing bank prices its notes
with ONE desk running ONE algorithm. If so, a note's terms are a stable
function of the market on its pricing date. This module builds the rows; the
model decides whether the notes bear the assumption out.

Sampling: for every issuer in ISSUERS and every week, `per_week` notes spread
across that week's newest-first search hits (filtered by the issuing entity's
CIK -- search metadata, then one document per sampled note), read with the
deterministic key-terms extractor. No model anywhere. Finals only: a
preliminary pricing supplement has no terms yet. A 424B2 that is not a
structured note (no coupon, barrier or call premium) is skipped.

Market inputs on the pricing date, never after it (no look-ahead): spot and
20/60-day realized volatility of each underlying from Stock-Data-Pipeline's
prices (worst-of notes: each underlying; the row keeps the max), and the
1-year Treasury yield (FRED DGS1, fetched once and cached).

Store: ~/.edgar-scrubber/research/note_pricing/notes.sqlite (table `notes`,
plus `weeks` marking each issuer-week done, so a build resumes).

  python Research/note_pricing/data.py build [--weeks 104] [--per-week 4]
  python Research/note_pricing/data.py coverage
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools" / "edgar_scrubber"))
sys.path.insert(0, str(HERE.parent))

HOME = Path(os.environ.get("NOTE_PRICING_HOME") or Path.home() / ".edgar-scrubber" / "research" / "note_pricing")
PIPELINE_DB = Path(os.environ.get("PIPELINE_STOCKS_DB")
                   or Path.home() / ".stock-data-pipeline" / "data" / "stocks.db")

# The entities that issue the notes (not their parents: a filing lists both),
# from EDGAR's own tally of September 2026's 424B2 filers.
ISSUERS = {
    "JPMorgan Chase Financial": "0001665650",
    "Morgan Stanley Finance": "0001666268",
    "UBS AG": "0001114446",
    "Citigroup Global Markets Holdings": "0000200245",
    "BofA Finance": "0001682472",
    "GS Finance": "0001419828",
    "Barclays Bank": "0000312070",
    "Bank of Nova Scotia": "0000009631",
    "Royal Bank of Canada": "0001000275",
    "Toronto-Dominion Bank": "0000947263",
    "Bank of Montreal": "0000927971",
    "HSBC USA": "0000083246",
    "Wells Fargo Finance": "0001738143",
    "Nomura America Finance": "0001383951",
    "Jefferies Financial Group": "0000096223",
    "Deutsche Bank": "0001159508",
    "CIBC": "0001045520",
}

COLUMNS = ("accession", "issuer", "issuer_cik", "guarantor", "selling_agents", "agent_commission_pct",
           "filing_date", "pricing_date", "maturity_date", "tenor_years", "product_type",
           "n_underlyings", "underlyings", "tickers", "barrier_pct", "coupon_barrier_pct",
           "autocall_pct", "coupon_rate", "coupon_frequency", "call_premium", "buffer_pct",
           "estimated_value", "spot", "vol20_max", "vol60_max", "rate_1y")
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS notes ({", ".join(c + (" TEXT PRIMARY KEY" if c == "accession" else "") for c in COLUMNS)});
CREATE TABLE IF NOT EXISTS weeks (issuer_cik TEXT, week TEXT, sampled INTEGER, skipped TEXT,
                                  PRIMARY KEY (issuer_cik, week));
CREATE TABLE IF NOT EXISTS tried (accession TEXT PRIMARY KEY, issuer_cik TEXT, week TEXT, outcome TEXT);
"""


def connect(home=HOME):
    Path(home).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(Path(home) / "notes.sqlite"))
    conn.executescript(SCHEMA)
    # #376: a week whose candidates ran out is not searched again.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(weeks)")}
    if "exhausted" not in cols:
        conn.execute("ALTER TABLE weeks ADD COLUMN exhausted INTEGER DEFAULT 0")
        conn.commit()
    return conn


# ── Market inputs ──────────────────────────────────────────────────────────

class Market:
    """Closes from the pipeline (read-only) and the 1-year Treasury yield.
    `closes`/`rates` let tests inject data: {ticker: [(iso, close)]},
    {iso: pct}."""

    def __init__(self, db_path=PIPELINE_DB, closes=None, rates=None, home=HOME):
        self._closes, self._rates, self._home = closes, rates, Path(home)
        self._series = {}
        self._conn = None
        if closes is None and Path(db_path).exists():
            self._conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)

    def series(self, tk):
        if tk not in self._series:
            if self._closes is not None:
                rows = sorted(self._closes.get(tk, []))
            elif self._conn is not None:
                rows = self._conn.execute("SELECT date, close FROM price_history WHERE ticker = ? AND close "
                                          "IS NOT NULL ORDER BY date", (tk,)).fetchall()
            else:
                rows = []
            self._series[tk] = rows
        return self._series[tk]

    def on(self, tk, iso):
        """(spot, vol20, vol60) as of `iso` -- the last close on or before it
        and annualised realized vol of the daily returns up to it -- or Nones."""
        rows = [r for r in self.series(tk) if r[0] <= iso]
        if not rows:
            return None, None, None
        closes = [c for _, c in rows]

        def vol(n):
            if len(closes) < n + 1:
                return None
            r = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
            m = sum(r) / n
            return round(math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1)) * math.sqrt(252) * 100, 4)
        return closes[-1], vol(20), vol(60)

    def rate(self, iso):
        """The 1-year Treasury yield (%) on or before `iso`, or None."""
        rates = self._rates if self._rates is not None else self._fred_dgs1()
        best = None
        for d in sorted(rates):
            if d > iso:
                break
            best = rates[d]
        return best

    def _fred_dgs1(self):
        if self._rates is not None:
            return self._rates
        path = self._home / "fred_dgs1.json"
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("fetched") == date.today().isoformat():
                self._rates = cached["rates"]
                return self._rates
        except (OSError, ValueError):
            pass
        key = os.environ.get("FRED_API_KEY") or ""
        rates = {}
        if key:
            from urllib.parse import urlencode
            from urllib.request import urlopen
            url = "https://api.stlouisfed.org/fred/series/observations?" + urlencode(
                {"series_id": "DGS1", "api_key": key, "file_type": "json", "observation_start": "2015-01-01"})
            try:
                data = json.loads(urlopen(url, timeout=30).read().decode("utf-8"))
                rates = {o["date"]: float(o["value"]) for o in data.get("observations", [])
                         if o["value"] not in (".", "")}
            except Exception as exc:
                # FRED down (it served a "Site Maintenance" page on
                # 2026-10-21): rows keep rate_1y null, nothing is cached, and
                # `data.py fill-rates` fills them later.
                print(f"FRED DGS1 unavailable ({type(exc).__name__}); rate_1y left null -- run fill-rates later")
                rates = {}
            if rates:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fetched": date.today().isoformat(), "rates": rates}), encoding="utf-8")
        self._rates = rates
        return rates


# ── Underlying -> ticker ───────────────────────────────────────────────────

# Index / ETF names -> the ETF that prices them (#379). Matched on the name
# lower-cased with registered marks removed. A proxy stands in for its index's
# volatility, which is what the model reads. Proprietary indices (MerQube's
# vol-advantage family) have no public price and are deliberately absent.
PROXIES = (
    (r"nasdaq-?\s?100.*technology sector", "QTEC"),
    (r"nasdaq-?\s?100.*futures excess return", "QQQ"),
    (r"s&p 500.*futures excess return", "SPY"),
    (r"euro\s*stoxx 50", "FEZ"),
    (r"msci eafe", "EFA"),
    (r"msci brazil", "EWZ"),
    (r"msci emerging markets", "EEM"),
    (r"20\+ year treasury|treasury 20\+ year", "TLT"),
    (r"regional banking", "KRE"),
    (r"gold miners", "GDX"),
    (r"technology select sector", "XLK"),
    (r"utilities select sector", "XLU"),
    (r"financial select sector", "XLF"),
    (r"consumer discretionary select sector", "XLY"),
    (r"energy select sector", "XLE"),
    (r"health care select sector", "XLV"),
    (r"industrial select sector", "XLI"),
    (r"s&p.*biotech", "XBI"),
    (r"ishares.*biotechnology", "IBB"),
    (r"invesco qqq", "QQQ"),
    (r"semiconductor etf", "SMH"),
)
_PROXY_RES = [(re.compile(pat), tk) for pat, tk in PROXIES]
# Two index / ETF names glued into one list item by a table layout:
# "Nasdaq-100 Index® Russell 2000® Index".
_GLUED = re.compile(r"(?:(?<=Index)|(?<=Fund)|(?<=Trust))(?:®|SM|TM)?(?:®|SM|TM)?\s+(?=[A-Z])|(?<=ETF)\s+(?=[A-Z])")


_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def clean_names(names):
    """Whitespace collapsed (a line break inside "EURO \\n\\nSTOXX 50"),
    invisible bidi / zero-width characters stripped (#381: Morgan Stanley's
    names end in U+202C), and glued index/ETF names split apart."""
    out = []
    for n in names:
        n = " ".join(_INVISIBLE.sub("", str(n or "")).split())
        if not n:
            continue
        parts = [x.strip() for x in _GLUED.split(n) if x.strip()]
        if len(parts) > 1 and all(re.search(r"(?:Index|ETF|Fund|Trust)(?:®|SM|TM)*$", x) for x in parts):
            out.extend(parts)
        else:
            out.append(n)
    return out


def _ticker_map(client=None):
    """name -> ticker: the dealer-hedging map (indices -> ETF proxies), then
    SEC's company tickers file for any other company name."""
    import dealer_hedging_data as dh
    # SEC prints "NVIDIA CORP" where a note prints "NVIDIA Corporation": key
    # both on the name without its corporate suffix. The first (largest)
    # listing for a key wins -- the file is ordered by market value.
    suffix = re.compile(r"\b(?:incorporated|inc|corporation|corp|company|co|plc|ltd|limited|n v|s a|"
                        r"ag|se|holdings|group|the)\b")
    state_tag = re.compile(r"\s*/[A-Za-z]{2,3}/?\s*$")         # "DANAHER CORP /DE/"
    short = lambda s: re.sub(r"\s+", " ", suffix.sub("", dh._stock_key(s))).strip()
    titles = {}
    if client is not None:
        try:
            raw = client.get_json(client.TICKERS_URL)
            for row in (raw.values() if isinstance(raw, dict) else raw):
                k = short(state_tag.sub("", row.get("title", "")))
                if k and k not in titles:
                    titles[k] = (row.get("ticker") or "").upper() or None
        except Exception:
            titles = {}

    def lookup(name):
        name = " ".join(str(name or "").split())
        tk = dh.ticker_for_name(name)
        if tk:
            return tk
        norm = dh.normalize_name(name)
        for rx, proxy in _PROXY_RES:
            if rx.search(norm):
                return proxy
        return titles.get(short(name))
    return lookup


# ── One note ───────────────────────────────────────────────────────────────

def _v(terms, f):
    x = terms.get(f)
    return x.get("value") if isinstance(x, dict) else x


def note_row(issuer, cik, accession, file_date, terms, market, lookup):
    """The `notes` row for one filing, or (None, reason)."""
    if (_v(terms, "filing_stage") or "") == "preliminary":
        return None, "preliminary"
    coupon, barrier, call = _v(terms, "contingent_coupon_rate"), _v(terms, "barrier_pct"), _v(terms, "call_premium")
    if coupon is None and barrier is None and call is None:
        return None, "not a structured note"
    pricing = _v(terms, "pricing_date") or file_date
    if pricing > file_date:
        return None, "preliminary"
    names = clean_names([u.get("name") if isinstance(u, dict) else u for u in (_v(terms, "underlyings") or [])])
    if not names:
        return None, "no underlyings extracted"      # a partial read, not a usable note
    tickers = [lookup(n) for n in names]
    spots, v20, v60 = [], [], []
    for tk in tickers:
        if not tk:
            continue
        s, a, b = market.on(tk, pricing)
        if s is not None:
            spots.append(s)
        if a is not None:
            v20.append(a)
        if b is not None:
            v60.append(b)
    maturity = _v(terms, "maturity_date")
    tenor = None
    if maturity:
        tenor = round((date.fromisoformat(maturity) - date.fromisoformat(pricing)).days / 365.25, 4)
    row = {
        "accession": accession, "issuer": _v(terms, "issuer") or issuer, "issuer_cik": cik,
        "guarantor": _v(terms, "guarantor"), "selling_agents": json.dumps(_v(terms, "selling_agents") or []),
        "agent_commission_pct": _v(terms, "agent_commission_pct"), "filing_date": file_date,
        "pricing_date": pricing, "maturity_date": maturity, "tenor_years": tenor,
        "product_type": _v(terms, "product_type"), "n_underlyings": len(names) or None,
        "underlyings": json.dumps(names), "tickers": json.dumps(tickers),
        "barrier_pct": barrier, "coupon_barrier_pct": _v(terms, "coupon_barrier_pct"),
        "autocall_pct": _v(terms, "autocall_barrier_pct"), "coupon_rate": coupon,
        "coupon_frequency": _v(terms, "coupon_frequency"), "call_premium": call,
        "buffer_pct": _v(terms, "buffer_pct"), "estimated_value": _v(terms, "estimated_value_per_1000"),
        "spot": spots[0] if len(spots) == 1 else None,
        "vol20_max": max(v20) if v20 else None, "vol60_max": max(v60) if v60 else None,
        "rate_1y": market.rate(pricing),
    }
    return row, None


def _default_extract(client, cik, accession):
    """Key terms from the primary document alone (#372: fetching every
    exhibit cost ~5 s a note)."""
    import research_digest
    from document_expand import expand_accession
    return research_digest.key_terms_for_filing(
        client, cik, accession, expand=lambda c, k, a: expand_accession(c, k, a, primary_only=True))


def _weeks(n_weeks, today=None):
    """[(monday, sunday)] for the last `n_weeks` complete weeks, oldest first."""
    today = today or date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    return [(last_monday - timedelta(weeks=k), last_monday - timedelta(weeks=k) + timedelta(days=6))
            for k in range(n_weeks - 1, -1, -1)]


PRELIM_QUERY = '"Preliminary Pricing Supplement" OR "Subject to Completion"'


def sample_week(client, conn, issuer, cik, w0, w1, market, lookup, per_week=4,
                extract=_default_extract):
    """Bring one issuer-week up to `per_week` kept notes (#376: a re-run with a
    higher target tops up weeks already sampled). Every accession read is
    recorded in `tried`, kept or skipped, and never read again; a week whose
    candidates run out is marked exhausted. Returns (kept_now, skipped_now),
    or None when the week needs nothing."""
    from edgar_client import parse_hit
    week = w0.isoformat()
    prev = conn.execute("SELECT sampled, skipped, exhausted FROM weeks WHERE issuer_cik = ? AND week = ?",
                        (cik, week)).fetchone()
    have = prev[0] if prev else 0
    if prev and (prev[2] or have >= per_week):
        return None
    need = per_week - have
    page = client.full_text_search(forms=["424B2"], startdt=week, enddt=w1.isoformat(), ciks=cik.zfill(10))
    hits = [parse_hit(h) for h in ((page.get("hits") or {}).get("hits") or [])]
    # Preliminaries carry no final terms, and were ~70% of all reads (#381).
    # Search ignores negation, but a positive phrase search lists them, so
    # they are skipped before anything is downloaded.
    prelim_page = client.full_text_search(q=PRELIM_QUERY, forms=["424B2"], startdt=week, enddt=w1.isoformat(),
                                          ciks=cik.zfill(10))
    prelims = {h.get("accession") for h in (parse_hit(x) for x in ((prelim_page.get("hits") or {}).get("hits") or []))}
    hits = [h for h in hits if h.get("accession") not in prelims]
    hits = sorted({h["accession"]: h for h in hits if h.get("accession")}.values(),
                  key=lambda h: (h.get("file_date") or "", h["accession"]))
    seen = {r[0] for r in conn.execute("SELECT accession FROM tried WHERE issuer_cik = ?", (cik,))}
    seen |= {r[0] for r in conn.execute("SELECT accession FROM notes WHERE issuer_cik = ?", (cik,))}
    kept, skipped, tries = 0, {}, 0
    # Spread across the week: walk candidates in an order that alternates
    # through it rather than taking the newest first.
    order = [i for i in sorted(range(len(hits)), key=lambda i: ((i * 7919) % max(1, len(hits)), i))
             if hits[i]["accession"] not in seen]
    ran_out = True
    for i in order:
        # Up to 6 reads per wanted note: some issuers file several
        # preliminaries for every final (Barclays, 2026-10: 12 of 14 sampled).
        if kept >= need or tries >= need * 6:
            ran_out = False
            break
        h = hits[i]
        tries += 1
        try:
            terms = extract(client, h.get("cik") or cik, h["accession"]) or {}
            row, why = note_row(issuer, cik, h["accession"], h.get("file_date") or week, terms, market, lookup)
        except Exception:
            row, why = None, "extract failed"
        if row is None:
            skipped[why] = skipped.get(why, 0) + 1
            conn.execute("INSERT OR REPLACE INTO tried VALUES (?, ?, ?, ?)", (h["accession"], cik, week, why))
            continue
        conn.execute(f"INSERT OR REPLACE INTO notes ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                     [row[c] for c in COLUMNS])
        conn.execute("INSERT OR REPLACE INTO tried VALUES (?, ?, ?, ?)", (h["accession"], cik, week, "kept"))
        kept += 1
    total = dict(json.loads(prev[1] or "{}")) if prev else {}
    for k, v in skipped.items():
        total[k] = total.get(k, 0) + v
    exhausted = 1 if (ran_out and have + kept < per_week) else 0
    conn.execute("INSERT OR REPLACE INTO weeks (issuer_cik, week, sampled, skipped, exhausted) VALUES (?, ?, ?, ?, ?)",
                 (cik, week, have + kept, json.dumps(total), exhausted))
    conn.commit()
    return kept, skipped


def build(client, market, n_weeks=104, per_week=4, issuers=None, home=HOME, extract=_default_extract,
          log=print, today=None):
    conn = connect(home)
    lookup = _ticker_map(client)
    weeks = _weeks(n_weeks, today)
    for name, cik in (issuers or ISSUERS).items():
        before = conn.execute("SELECT COUNT(*) FROM notes WHERE issuer_cik = ?", (cik,)).fetchone()[0]
        for w0, w1 in weeks:
            sample_week(client, conn, name, cik, w0, w1, market, lookup, per_week, extract)
        after = conn.execute("SELECT COUNT(*) FROM notes WHERE issuer_cik = ?", (cik,)).fetchone()[0]
        log(f"{name}: {after} notes ({after - before} new)")
    return coverage(conn)


REEXTRACT_OUTCOMES = ("no underlyings extracted", "not a structured note", "extract failed")


def reextract(client, conn, market, lookup, extract=_default_extract, log=print, limit=None):
    """Re-read with the current extractor (#383): sampled notes that have no
    coupon, and filings skipped as one of REEXTRACT_OUTCOMES. A row is
    replaced in place; a skipped filing that now qualifies becomes a note
    and counts toward its week. Returns {issuer_cik: {"updated", "added"}}."""
    stats = {}

    def bump(cik, k):
        stats.setdefault(cik, {"updated": 0, "added": 0})[k] += 1

    todo = [(acc, cik, fdate, None) for acc, cik, fdate in conn.execute(
        "SELECT accession, issuer_cik, filing_date FROM notes WHERE coupon_rate IS NULL")]
    todo += [(acc, cik, None, week) for acc, cik, week in conn.execute(
        f"SELECT accession, issuer_cik, week FROM tried WHERE outcome IN ({','.join('?' * len(REEXTRACT_OUTCOMES))})",
        REEXTRACT_OUTCOMES)]
    names = {v: k for k, v in ISSUERS.items()}
    for n, (acc, cik, fdate, week) in enumerate(todo[:limit] if limit else todo):
        try:
            terms = extract(client, cik, acc) or {}
        except Exception:
            continue
        # A skipped filing's own date was not kept: bound it by its week's end
        # (the knowledge date stays within the week it was sampled from).
        fd = fdate or (date.fromisoformat(week) + timedelta(days=6)).isoformat()
        row, why = note_row(names.get(cik, cik), cik, acc, fd, terms, market, lookup)
        if row is None:
            continue
        conn.execute(f"INSERT OR REPLACE INTO notes ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                     [row[c] for c in COLUMNS])
        if week is None:
            bump(cik, "updated")
        else:
            conn.execute("UPDATE tried SET outcome = 'kept (reextract)' WHERE accession = ?", (acc,))
            conn.execute("UPDATE weeks SET sampled = sampled + 1 WHERE issuer_cik = ? AND week = ?", (cik, week))
            bump(cik, "added")
        if n % 200 == 0:
            conn.commit()
            log(f"reextract {n}/{len(todo)}")
    conn.commit()
    return stats


def unpriced(conn, market):
    """(ticker -> notes, unmapped name -> notes): underlyings with a known
    ticker but no price history, and names no ticker was found for (#377)."""
    no_price, no_ticker = {}, {}
    for names, tickers in conn.execute("SELECT underlyings, tickers FROM notes"):
        for name, tk in zip(json.loads(names or "[]"), json.loads(tickers or "[]")):
            if not tk:
                no_ticker[name] = no_ticker.get(name, 0) + 1
            elif not market.series(tk):
                no_price[tk] = no_price.get(tk, 0) + 1
    order = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1]))
    return order(no_price), order(no_ticker)


def usable_share(conn):
    n = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    u = conn.execute("SELECT COUNT(*) FROM notes WHERE coupon_rate IS NOT NULL AND "
                     "COALESCE(vol60_max, vol20_max) IS NOT NULL").fetchone()[0]
    return {"notes": n, "usable": u, "share": round(u / n, 4) if n else None}


def refresh_market(conn, market, lookup=None):
    """Recompute spot and 20/60-day vol on every row from the current price
    history -- still only closes on or before each pricing date. With
    `lookup`, the names are cleaned and re-mapped to tickers first (#379), so
    rows collected before a proxy existed benefit too. Returns the number of
    rows whose volatility changed."""
    changed = 0
    for acc, pricing, names, tickers, old in conn.execute(
            "SELECT accession, pricing_date, underlyings, tickers, vol60_max FROM notes").fetchall():
        if lookup is not None:
            names_l = clean_names(json.loads(names or "[]"))
            tickers = json.dumps([lookup(n) for n in names_l])
            conn.execute("UPDATE notes SET underlyings = ?, tickers = ?, n_underlyings = ? WHERE accession = ?",
                         (json.dumps(names_l), tickers, len(names_l) or None, acc))
        spots, v20, v60 = [], [], []
        for tk in json.loads(tickers or "[]"):
            if not tk:
                continue
            sp, a, b = market.on(tk, pricing)
            if sp is not None:
                spots.append(sp)
            if a is not None:
                v20.append(a)
            if b is not None:
                v60.append(b)
        new = max(v60) if v60 else None
        conn.execute("UPDATE notes SET spot = ?, vol20_max = ?, vol60_max = ? WHERE accession = ?",
                     (spots[0] if len(spots) == 1 else None, max(v20) if v20 else None, new, acc))
        changed += new != old
    conn.commit()
    return changed


def fill_rates(conn, market):
    """Set rate_1y on rows built while the rate source was unavailable.
    Returns the number of rows filled."""
    n = 0
    for acc, pricing in conn.execute("SELECT accession, pricing_date FROM notes WHERE rate_1y IS NULL").fetchall():
        r = market.rate(pricing)
        if r is not None:
            conn.execute("UPDATE notes SET rate_1y = ? WHERE accession = ?", (r, acc))
            n += 1
    conn.commit()
    return n


def coverage(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {"notes": q("SELECT COUNT(*) FROM notes"),
            "issuers": q("SELECT COUNT(DISTINCT issuer_cik) FROM notes"),
            "first_pricing": q("SELECT MIN(pricing_date) FROM notes"),
            "last_pricing": q("SELECT MAX(pricing_date) FROM notes"),
            "with_coupon": q("SELECT COUNT(*) FROM notes WHERE coupon_rate IS NOT NULL"),
            "with_vol": q("SELECT COUNT(*) FROM notes WHERE vol60_max IS NOT NULL"),
            "with_rate": q("SELECT COUNT(*) FROM notes WHERE rate_1y IS NOT NULL"),
            "issuer_weeks_done": q("SELECT COUNT(*) FROM weeks")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the note-pricing sample (#365).")
    ap.add_argument("cmd", choices=["build", "coverage", "fill-rates", "unpriced", "refresh-market", "reextract"])
    ap.add_argument("--weeks", type=int, default=104)
    ap.add_argument("--per-week", type=int, default=4)
    a = ap.parse_args(argv)
    if a.cmd == "coverage":
        print(json.dumps(coverage(connect()), indent=1))
        return
    if a.cmd == "fill-rates":
        print(f"filled rate_1y on {fill_rates(connect(), Market())} row(s)")
        return
    if a.cmd == "unpriced":
        no_price, no_ticker = unpriced(connect(), Market())
        print(json.dumps({"usable": usable_share(connect()), "ticker_without_prices": no_price,
                          "no_ticker": dict(list(no_ticker.items())[:40])}, indent=1))
        return
    if a.cmd == "reextract":
        from edgar_client import EdgarClient
        client = EdgarClient(user_agent=os.environ.get("EDGAR_USER_AGENT", "").strip(),
                             cache_dir=str(Path.home() / ".edgar-scrubber" / "cache"))
        conn = connect()
        before = usable_share(conn)
        stats = reextract(client, conn, Market(), _ticker_map(client))
        print(json.dumps({"by_issuer": stats, "before": before, "after": usable_share(conn)}, indent=1))
        return
    if a.cmd == "refresh-market":
        conn = connect()
        before = usable_share(conn)
        from edgar_client import EdgarClient
        ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
        client = EdgarClient(user_agent=ua, cache_dir=str(Path.home() / ".edgar-scrubber" / "cache")) if ua else None
        n = refresh_market(conn, Market(), lookup=_ticker_map(client))
        print(json.dumps({"rows_changed": n, "before": before, "after": usable_share(conn)}, indent=1))
        return
    from edgar_client import EdgarClient
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise SystemExit("EDGAR_USER_AGENT is not set (SEC fair-access policy)")
    client = EdgarClient(user_agent=ua, cache_dir=str(Path.home() / ".edgar-scrubber" / "cache"))
    print(json.dumps(build(client, Market(), a.weeks, a.per_week), indent=1))


if __name__ == "__main__":
    main()
