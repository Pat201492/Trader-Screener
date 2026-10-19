"""
Reference implementation of Stock-Data-Pipeline's insider-trades contract
(Stock-Data-Pipeline #57 /api/insider-trades, #58 /api/insider-trades/rollups),
so the dashboard's Insider Trades view (#314) can be built and tested before the
real endpoints exist. Same rules as the issues:

  * filters: from / to (transaction_date, default last 30 days), type (comma
    list of codes), ticker, insider (substring, case-insensitive), role
    (officer | director | ten_pct, substring on insider_title), min_value,
    sort (date | value), limit (default 200, max 2000), offset
  * a row is `suspect` when price_per_share > 100000, total_value > 5e9, or
    shares * price differs from total_value by more than 10x; suspect rows are
    excluded (and not counted in `total`) unless include_suspect=1
  * rollups: top_buys (25 largest P), most_sold (per-ticker S totals, top 25),
    clusters (3+ distinct insiders buying one ticker within any 14 days)
"""
from datetime import date, timedelta

ROLE_MATCH = {"officer": ("officer", "chief", "ceo", "cfo", "coo", "president", "vp", "evp", "svp"),
              "director": ("director",), "ten_pct": ("10%",)}


def _first(qs, key, default=None):
    v = qs.get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    return v if v not in (None, "") else default


def is_suspect(r):
    p, v, s = r.get("price_per_share"), r.get("total_value"), r.get("shares")
    if p is not None and p > 100_000:
        return True
    if v is not None and v > 5e9:
        return True
    if p and s and v:
        implied = p * s
        if implied > 0 and v > 0 and (v / implied > 10 or implied / v > 10):
            return True
    return False


def _sec_url(r):
    u = r.get("source_url") or ""
    return u[len("mirror:"):] if u.startswith("mirror:") else u


def _window(qs, today=None):
    today = today or date.today()
    to = _first(qs, "to") or today.isoformat()
    frm = _first(qs, "from") or (date.fromisoformat(to) - timedelta(days=30)).isoformat()
    return frm, to


def _matches(r, qs, frm, to):
    d = r.get("transaction_date") or ""
    if not (frm <= d <= to):
        return False
    types = _first(qs, "type")
    if types and r.get("transaction_type") not in {t.strip().upper() for t in types.split(",")}:
        return False
    t = _first(qs, "ticker")
    if t and (r.get("ticker") or "").upper() != t.upper():
        return False
    ins = _first(qs, "insider")
    if ins and ins.lower() not in (r.get("insider_name") or "").lower():
        return False
    role = _first(qs, "role")
    if role:
        title = (r.get("insider_title") or "").lower()
        if not any(w in title for w in ROLE_MATCH.get(role, (role,))):
            return False
    mv = _first(qs, "min_value")
    if mv is not None and (r.get("total_value") or 0) < float(mv):
        return False
    return True


def _rows(trades, qs, today=None):
    frm, to = _window(qs, today)
    include = _first(qs, "include_suspect") == "1"
    out = []
    for r in trades:
        if not _matches(r, qs, frm, to):
            continue
        sus = is_suspect(r)
        if sus and not include:
            continue
        out.append({**r, "sec_url": _sec_url(r), "suspect": sus})
    return out


def insider_trades(trades, qs, today=None):
    rows = _rows(trades, qs, today)
    if _first(qs, "sort") == "value":
        rows.sort(key=lambda r: r.get("total_value") or 0, reverse=True)
    else:
        rows.sort(key=lambda r: r.get("transaction_date") or "", reverse=True)
    limit = max(1, min(int(_first(qs, "limit", 200)), 2000))
    offset = max(0, int(_first(qs, "offset", 0)))
    page = rows[offset:offset + limit]
    return {"count": len(page), "total": len(rows), "rows": page}


def rollups(trades, qs, today=None):
    rows = _rows(trades, {k: v for k, v in qs.items() if k in ("from", "to", "role", "include_suspect")}, today)
    buys = [r for r in rows if r.get("transaction_type") == "P"]
    top_buys = sorted(buys, key=lambda r: r.get("total_value") or 0, reverse=True)[:25]
    sold = {}
    for r in rows:
        if r.get("transaction_type") != "S":
            continue
        s = sold.setdefault(r["ticker"], {"ticker": r["ticker"], "company_name": r.get("company_name"),
                                          "sell_value": 0.0, "sell_count": 0, "insiders": set()})
        s["sell_value"] += r.get("total_value") or 0
        s["sell_count"] += 1
        s["insiders"].add(r.get("insider_name"))
    most_sold = sorted(({**v, "insiders": len(v["insiders"])} for v in sold.values()),
                       key=lambda v: v["sell_value"], reverse=True)[:25]
    clusters = []
    by_ticker = {}
    for r in buys:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for t, rs in by_ticker.items():
        rs.sort(key=lambda r: r["transaction_date"])
        best = None
        for i, a in enumerate(rs):
            end = (date.fromisoformat(a["transaction_date"]) + timedelta(days=14)).isoformat()
            win = [b for b in rs[i:] if b["transaction_date"] <= end]
            people = {b.get("insider_name") for b in win}
            if len(people) >= 3 and (best is None or len(people) > best[0]):
                best = (len(people), win)
        if best:
            n, win = best
            clusters.append({"ticker": t, "company_name": win[0].get("company_name"), "insiders": n,
                             "buy_value": sum(b.get("total_value") or 0 for b in win),
                             "first_date": win[0]["transaction_date"], "last_date": win[-1]["transaction_date"]})
    clusters.sort(key=lambda c: (c["insiders"], c["buy_value"]), reverse=True)
    return {"top_buys": top_buys, "most_sold": most_sold, "clusters": clusters}
