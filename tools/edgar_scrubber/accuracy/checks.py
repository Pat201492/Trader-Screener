"""The four checks. Each returns a Report: counts plus the findings to read."""
import collections
import gzip
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"
HOME = Path(os.environ.get("EDGAR_SCRUBBER_HOME", Path.home() / ".edgar-scrubber"))
PIPELINE_POL_DB = Path(os.environ.get(
    "POL_DB_PATH", Path.home() / ".stock-data-pipeline" / "data" / "politicians.db"))


class Report:
    def __init__(self, name):
        self.name = name
        self.counts = collections.Counter()
        self.findings = []            # (severity, where, message)

    def ok(self, key="ok"):
        self.counts[key] += 1

    def bad(self, where, message, key="wrong", severity="FAIL"):
        self.counts[key] += 1
        self.findings.append((severity, where, message))

    def note(self, where, message, key="note"):
        self.bad(where, message, key=key, severity="note")

    @property
    def failed(self):
        return any(s == "FAIL" for s, _, _ in self.findings)

    def render(self, limit=60):
        lines = [f"== {self.name}: " + ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))]
        for sev, where, msg in self.findings[:limit]:
            lines.append(f"   [{sev}] {where}: {msg}")
        if len(self.findings) > limit:
            lines.append(f"   ... {len(self.findings) - limit} more")
        return "\n".join(lines)


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return round(float(a), 4) == round(float(b), 4)
        except (TypeError, ValueError):
            return False
    return a == b


# -- 1. golden ----------------------------------------------------------------

def golden():
    """Every extractor against filings whose values were checked by hand.
    A field expected absent (None) must stay absent: a wrong value is worse
    than a missing one, because it skips the model."""
    from key_terms import extract_key_terms
    import xml_forms
    rep = Report("golden")

    g = json.load(open(GOLDEN / "424b2" / "expected.json", encoding="utf-8"))
    per_field = collections.Counter()
    for case in g["cases"]:
        text = gzip.open(GOLDEN / "424b2" / case["doc"], "rt", encoding="utf-8").read()
        got = extract_key_terms(text)
        for field, want in case["expected"].items():
            k = got.get(field)
            have = k.value if k else None
            if _same(have, want):
                rep.ok()
                per_field[(field, "ok")] += 1
            else:
                per_field[(field, "wrong")] += 1
                rep.bad(f"424B2 {case['accession']} {field}", f"expected {want!r}, got {have!r}")
            if k is not None and k.raw is not None and k.span and text[k.span[0]:k.span[1]] != k.raw:
                rep.bad(f"424B2 {case['accession']} {field}", "span does not point at the raw value",
                        key="bad_span")

    x = json.load(open(GOLDEN / "xml" / "expected.json", encoding="utf-8"))
    for case in x["cases"]:
        files = {n: gzip.open(GOLDEN / "xml" / f, "rt", encoding="utf-8").read()
                 for n, f in case["files"].items()}
        primary = files.get("primary_doc.xml")
        try:
            rows = xml_forms.read_filing(case["form"], files[case["data_doc"]], primary_text=primary)
        except Exception as exc:
            rep.bad(f"{case['form']} {case['accession']}", f"reader raised {type(exc).__name__}: {exc}")
            continue
        want = case["expected_rows"]
        if len(rows) != len(want):
            rep.bad(f"{case['form']} {case['accession']}", f"expected {len(want)} rows, got {len(rows)}")
            continue
        for i, (r, w) in enumerate(zip(rows, want)):
            diffs = [k for k in set(w) | set(r) if not _same(r.get(k), w.get(k))]
            if diffs:
                rep.bad(f"{case['form']} {case['accession']} row {i}",
                        "; ".join(f"{k}: expected {w.get(k)!r}, got {r.get(k)!r}" for k in sorted(diffs)[:4]))
            else:
                rep.ok()
    rep.per_field = per_field
    return rep


# -- 2. cross-checks ----------------------------------------------------------

def _13f_cover_totals(primary_text):
    """(tableEntryTotal, tableValueTotal) from a 13F-HR cover page, or (None, None)."""
    try:
        root = ET.fromstring(primary_text)
    except ET.ParseError:
        return None, None
    def find(tag):
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == tag and el.text:
                return el.text.strip()
        return None
    try:
        n = find("tableEntryTotal")
        v = find("tableValueTotal")
        return (int(n) if n else None), (int(float(v)) if v else None)
    except ValueError:
        return None, None


def _name_words(name):
    """A person's name as a set of words: case, punctuation and order ignored."""
    return frozenset(w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if w)


def cross():
    """The same fact from two independent sources must agree."""
    import xml_forms
    rep = Report("cross")

    # 13F-HR: the holdings the reader returns vs the totals the filer states on
    # its own cover page (two different documents in the same filing).
    x = json.load(open(GOLDEN / "xml" / "expected.json", encoding="utf-8"))
    for case in x["cases"]:
        if not case["form"].startswith("13F-HR"):
            continue
        files = {n: gzip.open(GOLDEN / "xml" / f, "rt", encoding="utf-8").read()
                 for n, f in case["files"].items()}
        rows = xml_forms.read_filing(case["form"], files[case["data_doc"]],
                                     primary_text=files.get("primary_doc.xml"))
        n, total = _13f_cover_totals(files.get("primary_doc.xml") or "")
        where = f"13F-HR {case['accession']}"
        if n is not None and n != len(rows):
            rep.bad(where, f"cover says {n} entries, reader returned {len(rows)}")
        else:
            rep.ok()
        s = sum(r.get("value") or 0 for r in rows)
        if total is not None and s != total:
            rep.bad(where, f"cover tableValueTotal {total:,} != sum of holdings {s:,}")
        else:
            rep.ok()

    # Form 4: the scrubber's rows vs the pipeline's insider_trades (a different
    # collector: the daily mirror plus the EDGAR backfill).
    forms_db = HOME / "store" / "forms.sqlite"
    if forms_db.exists() and PIPELINE_POL_DB.exists():
        f = sqlite3.connect(f"file:{forms_db}?mode=ro", uri=True)
        p = sqlite3.connect(f"file:{PIPELINE_POL_DB}?mode=ro", uri=True)
        seen = set()
        for acc, data in f.execute(
                "SELECT d.accession, r.data_json FROM rows r JOIN documents d "
                "ON d.run_id = r.run_id AND d.accession = r.accession "
                "WHERE d.form IN ('4','4/A')"):
            row = json.loads(data)
            if row.get("row_kind") == "holding" or not row.get("transaction_date") or row.get("shares") is None:
                continue
            key = (row.get("issuer_ticker"), row["transaction_date"], row.get("owner_name"), row.get("shares"))
            if key in seen:
                continue
            seen.add(key)
            # SEC's XML writes the owner last-first ("Newstead Jennifer"); the
            # pipeline stores first-last ("Jennifer Newstead"). Same person.
            want = _name_words(row.get("owner_name"))
            hit = next((h for h in p.execute(
                "SELECT price_per_share, transaction_type, insider_name FROM insider_trades "
                "WHERE ticker = ? AND transaction_date = ? AND shares = ?",
                (row.get("issuer_ticker"), row["transaction_date"], float(row["shares"])))
                if _name_words(h[2]) == want), None)
            where = f"Form 4 {acc} {row.get('issuer_ticker')} {row.get('owner_name')}"
            if hit is None:
                rep.note(where, "not in the pipeline's insider_trades (it may not have collected it)",
                         key="pipeline_missing")
            elif row.get("price") is not None and hit[0] is not None and abs(hit[0] - row["price"]) > 0.01:
                rep.bad(where, f"price {row['price']} vs pipeline {hit[0]}")
            elif row.get("transaction_code") and hit[1] and hit[1] != row["transaction_code"]:
                rep.bad(where, f"code {row['transaction_code']} vs pipeline {hit[1]}")
            else:
                rep.ok("pipeline_agrees")
    else:
        rep.note("Form 4", "forms.sqlite or the pipeline's politicians.db not found -- skipped",
                 key="skipped")
    return rep


# -- 3. sanity sweep ----------------------------------------------------------

def _spec_ranges():
    """{field: (min, max)} from the 424B2 field spec -- the one source of truth for
    what a value can be, so this sweep and the extractor's own bounds check can
    never disagree."""
    import field_spec
    spec = field_spec.load_spec(HERE.parent / "field_specs" / "424b2_structured_note.json")
    out = {}
    for f in spec.fields:
        b = getattr(f, "bounds", None) or {}
        if b.get("min") is not None or b.get("max") is not None:
            out[f.name] = (b.get("min", float("-inf")), b.get("max", float("inf")))
    return out


def _cusip_ok(v):
    from key_terms import valid_cusip
    return valid_cusip(v)


def sanity():
    """Impossible values in what the stores currently show."""
    rep = Report("sanity")
    ex = HOME / "store" / "extractions.sqlite"
    if ex.exists():
        import sys
        sys.path.insert(0, str(HERE.parent))
        from output_store import OutputStore
        s = OutputStore(str(ex), readonly=True)
        ranges = _spec_ranges()
        for d in s.query():
            vals = {r["field"]: r["value"] for r in s.fields(d["accession"], d["document"])}
            where = f"424B2 {d['accession']}"
            for f, (lo, hi) in ranges.items():
                v = vals.get(f)
                if isinstance(v, (int, float)) and not (lo <= v <= hi):
                    rep.bad(where, f"{f}={v} outside [{lo}, {hi}]", key="out_of_range")
            if vals.get("cusip") and not _cusip_ok(vals["cusip"]):
                rep.bad(where, f"cusip {vals['cusip']!r} fails its check digit", key="bad_cusip")
            m, p_ = vals.get("maturity_date"), vals.get("pricing_date")
            if isinstance(m, str) and isinstance(p_, str) and m[:4].isdigit() and p_[:4].isdigit() and m <= p_:
                rep.bad(where, f"maturity {m} not after pricing {p_}", key="date_order")
            cb, b = vals.get("coupon_barrier_pct"), vals.get("barrier_pct")
            if isinstance(cb, (int, float)) and isinstance(b, (int, float)) and cb < b:
                rep.bad(where, f"coupon barrier {cb} below downside barrier {b}", key="barrier_order")
            for u in vals.get("underlyings") or []:
                if str(u).lower() in ("single_stock", "etf", "index", "kind", "common stock"):
                    rep.bad(where, f"underlying {u!r} is a kind word, not a name", key="junk_underlying")
            rep.ok("documents")
    forms_db = HOME / "store" / "forms.sqlite"
    if forms_db.exists():
        f = sqlite3.connect(f"file:{forms_db}?mode=ro", uri=True)
        for form, acc, data in f.execute(
                "SELECT d.form, d.accession, r.data_json FROM rows r JOIN documents d "
                "ON d.run_id = r.run_id AND d.accession = r.accession"):
            row = json.loads(data)
            where = f"{form} {acc}"
            for k, v in row.items():
                if (k.endswith("_date") or k == "report_period") and isinstance(v, str) and v \
                        and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    rep.bad(where, f"{k}={v!r} is not an ISO date", key="non_iso_date")
            price = row.get("price")
            if isinstance(price, (int, float)) and price > 100_000:
                rep.bad(where, f"price {price} per share is implausible", key="implausible_price")
            for k in ("shares", "value", "shares_or_principal", "total_offering_amount"):
                v = row.get(k)
                if isinstance(v, (int, float)) and v < 0:
                    rep.bad(where, f"{k}={v} is negative", key="negative")
            rep.ok("rows")
    if PIPELINE_POL_DB.exists():
        p = sqlite3.connect(f"file:{PIPELINE_POL_DB}?mode=ro", uri=True)
        n = p.execute("SELECT count(*) FROM insider_trades WHERE price_per_share > 100000 "
                      "OR total_value > 5e9").fetchone()[0]
        if n:
            rep.note("pipeline insider_trades", f"{n} rows with price > $100k/share or value > $5B "
                                                f"(source data errors; #57's guard hides them)",
                     key="pipeline_suspect")
    return rep


# -- 4. canary (live) -----------------------------------------------------------

CANARY_FORMS = ("424B2", "4", "13F-HR", "SCHEDULE 13G", "D", "10-K")


def canary(per_form=5, days=7):
    """LIVE: read N of the newest filings per form with the no-model readers and
    report coverage, errors and sanity breaks. Nothing is written to the stores.
    New issuer formats show up here first."""
    import sys
    from datetime import timedelta
    sys.path.insert(0, str(HERE.parent.parent))
    import server
    from edgar_client import EdgarClient
    from document_expand import expand_accession
    from key_terms import extract_key_terms
    rep = Report("canary")
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        rep.bad("canary", "EDGAR_USER_AGENT is not set")
        return rep
    client = EdgarClient(user_agent=ua, cache_dir=str(HOME / "cache"))
    end = date.today()
    start = end - timedelta(days=days)
    core = ("issuer", "cusip", "maturity_date", "contingent_coupon_rate", "barrier_pct", "underlyings")
    for form in CANARY_FORMS:
        q = '"contingent coupon"' if form == "424B2" else ""
        res = server.edgar_search({"q": q, "forms": [form], "startdt": start.isoformat(),
                                   "enddt": end.isoformat(), "max_results": per_form})
        for h in (res.get("hits") or [])[:per_form]:
            where = f"{form} {h['accession']} {h.get('issuer') or ''}"[:80]
            try:
                b = expand_accession(client, h["cik"], h["accession"])
                if form == "424B2":
                    got = extract_key_terms(b.primary().normalized.text)
                    missing = [f for f in core if f not in got]
                    if missing:
                        rep.note(where, f"key terms missed {missing}", key="coverage_gap")
                    else:
                        rep.ok("read_fully")
                    v = got.get("barrier_pct")
                    if v and not (10 <= v.value <= 100):
                        rep.bad(where, f"barrier {v.value} out of range")
                elif form == "10-K":
                    rep.ok("taxonomy_skipped")      # companyfacts per filer is heavy; golden covers it
                else:
                    doc, rows = server.pick_xml_rows(b, h.get("form") or form)
                    if doc is None:
                        rep.bad(where, "no XML document the reader accepts")
                    elif not rows:
                        rep.note(where, "reader returned 0 rows", key="empty")
                    else:
                        rep.ok("read_fully")
            except Exception as exc:
                rep.bad(where, f"{type(exc).__name__}: {exc}", key="error")
    return rep
