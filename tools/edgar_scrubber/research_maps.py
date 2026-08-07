"""
Structured-note issuance maps -- Part A of issue #110, carved out as #130.

Three DESCRIPTIVE outputs, no modeling / controls / p-values:

  1. `issuance_by_underlying`     -- aggregate_principal grouped by
                                      underlyings[] and month.
  2. `barrier_clustering`         -- barrier_pct / coupon_barrier_pct /
                                      autocall_barrier_pct mapped to absolute
                                      price levels via initial_underlying_value.
  3. `issuer_markup_league`       -- estimated_value_per_1000 vs the $1,000
                                      issue price, aggregated by issuer and
                                      product_type over time.

Each ships as a chart (a self-contained SVG string -- no plotting dependency,
same stdlib-only posture as the rest of this tool) plus a short written
finding (a markdown paragraph built from the actual aggregated numbers, never
a template with blanks left in).

This module reads the scrubber's `OutputStore` ONLY through its public
interface -- `query()` and `fields()` (#109) -- never the sqlite file
directly, per #130's scope note: "Consumes scrubber output through the tool
interface only." Any field that proves out here is a graduation candidate to
Stock-Data-Pipeline per the same boundary (#109's graduation checklist).

stdlib only. Run the self-check:  python tools/edgar_scrubber/test_research_maps.py
"""

import json
import os
from collections import defaultdict

# --------------------------------------------------------------------------- #
# Reading the store -- tool interface only (#109)
# --------------------------------------------------------------------------- #

BARRIER_FIELDS = ("barrier_pct", "coupon_barrier_pct", "autocall_barrier_pct")


def load_documents(store, **query_kwargs):
    """Every document matching `query_kwargs`, with its field values attached.

    Uses only `store.query()` (document dimensions + underlyings) and
    `store.fields()` (per-field values) -- the store's public read surface.
    Returns a list of dicts: the query row plus a `values` dict of
    `{field_name: value}` for convenient lookup.
    """
    docs = store.query(**query_kwargs)
    out = []
    for d in docs:
        rows = store.fields(d["accession"], d["document"])
        values = {r["field"]: r["value"] for r in rows}
        out.append({**d, "values": values})
    return out


def _month(date_str):
    """ISO YYYY-MM-DD -> YYYY-MM, or None. filing_date is always ISO (#109)."""
    if not date_str or len(date_str) < 7:
        return None
    return date_str[:7]


# --------------------------------------------------------------------------- #
# 1. Issuance by underlying over time
# --------------------------------------------------------------------------- #

def issuance_by_underlying(docs, *, top_n=8):
    """aggregate_principal grouped by underlyings[] and month.

    A basket / worst-of note lists more than one underlying for the SAME
    principal -- splitting it evenly across every listed name means the sum
    across underlyings never double-counts a dollar of aggregate_principal,
    at the cost of not knowing which name in the basket actually drove the
    hedge. That trade is stated in the finding, not hidden in the numbers.

    Returns: {"months": [...], "series": {name: [total_per_month...]},
              "totals_by_underlying": {name: total}}
    Series keeps the `top_n` underlyings by total principal; everything else
    is folded into an "Other" series if it is non-zero.
    """
    totals = defaultdict(lambda: defaultdict(float))  # name -> month -> $
    skipped_no_month = 0
    for d in docs:
        principal = d["values"].get("aggregate_principal")
        if principal is None:
            continue
        month = _month(d.get("filing_date"))
        if month is None:
            skipped_no_month += 1
            continue
        names = [u["name"] for u in (d.get("underlyings") or []) if u.get("name")]
        if not names:
            continue
        share = principal / len(names)
        for name in names:
            totals[name][month] += share

    months = sorted({m for per_month in totals.values() for m in per_month})
    ranked = sorted(totals.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
    top, rest = ranked[:top_n], ranked[top_n:]

    series = {name: [vals.get(m, 0.0) for m in months] for name, vals in top}
    if rest:
        other = [sum(vals.get(m, 0.0) for _, vals in rest) for m in months]
        if any(other):
            series["Other"] = other

    return {
        "months": months,
        "series": series,
        "totals_by_underlying": {name: sum(vals.values()) for name, vals in totals.items()},
        "skipped_no_month": skipped_no_month,
    }


# --------------------------------------------------------------------------- #
# 2. Barrier / autocall level clustering vs spot
# --------------------------------------------------------------------------- #

def barrier_clustering(docs, *, bucket_width=5.0):
    """barrier_pct / coupon_barrier_pct / autocall_barrier_pct -> absolute
    price levels via initial_underlying_value, plus a clustering read.

    `absolute_level = initial_underlying_value * pct / 100` -- the actual
    index/stock level a barrier sits at, not just its percent-of-initial.
    Plotting `absolute_level` against `spot` (the same initial_underlying_value)
    is what shows clustering: points falling on a common diagonal ray from the
    origin are notes that share the same barrier_pct regardless of the
    underlying's price scale.

    Returns: {"points": [...], "clusters": {field: {bucket_pct: count}}}
    Buckets `barrier_pct` to the nearest `bucket_width` so "notes cluster at
    70%" is a count, not something a reader has to eyeball off a scatter.
    """
    points = []
    for d in docs:
        spot = d["values"].get("initial_underlying_value")
        if spot is None:
            continue
        underlying_name = None
        if d.get("underlyings"):
            underlying_name = d["underlyings"][0].get("name")
        for field in BARRIER_FIELDS:
            pct = d["values"].get(field)
            if pct is None:
                continue
            points.append({
                "accession": d["accession"],
                "document": d["document"],
                "issuer": d.get("issuer"),
                "underlying": underlying_name,
                "field": field,
                "barrier_pct": pct,
                "spot": spot,
                "absolute_level": spot * pct / 100.0,
            })

    clusters = defaultdict(lambda: defaultdict(int))
    for p in points:
        bucket = round(p["barrier_pct"] / bucket_width) * bucket_width
        clusters[p["field"]][bucket] += 1

    return {
        "points": points,
        "clusters": {f: dict(sorted(b.items())) for f, b in clusters.items()},
    }


# --------------------------------------------------------------------------- #
# 3. Issuer markup league table
# --------------------------------------------------------------------------- #

def issuer_markup_league(docs):
    """estimated_value_per_1000 vs the $1,000 issue price, by (issuer,
    product_type), over time.

    markup_pct = (1000 - estimated_value_per_1000) / 1000 * 100 -- the
    SEC-mandated disclosure read as issuer markup against the fixed $1,000
    issue price. Rows are ranked descending by average markup: the league
    table.

    Returns a list of dicts, sorted by avg_markup_pct descending:
      {issuer, product_type, n, avg_markup_pct, total_principal,
       first_month, last_month}
    """
    groups = defaultdict(list)
    for d in docs:
        ev = d["values"].get("estimated_value_per_1000")
        if ev is None:
            continue
        issuer = d.get("issuer") or "Unknown issuer"
        ptype = d.get("product_type") or "Unknown product_type"
        month = _month(d.get("filing_date"))
        markup_pct = (1000.0 - ev) / 1000.0 * 100.0
        principal = d["values"].get("aggregate_principal") or 0.0
        groups[(issuer, ptype)].append(
            {"month": month, "markup_pct": markup_pct, "principal": principal}
        )

    rows = []
    for (issuer, ptype), items in groups.items():
        n = len(items)
        avg_markup = sum(i["markup_pct"] for i in items) / n
        months = sorted(i["month"] for i in items if i["month"])
        rows.append({
            "issuer": issuer,
            "product_type": ptype,
            "n": n,
            "avg_markup_pct": avg_markup,
            "total_principal": sum(i["principal"] for i in items),
            "first_month": months[0] if months else None,
            "last_month": months[-1] if months else None,
        })
    rows.sort(key=lambda r: r["avg_markup_pct"], reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# SVG charts -- no plotting dependency, stdlib only
# --------------------------------------------------------------------------- #

_PALETTE = (
    "#2b6cb0", "#dd6b20", "#38a169", "#805ad5", "#d53f8c",
    "#718096", "#d69e2e", "#319795", "#c53030", "#4c51bf",
)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _svg(width, height, body, *, title=None):
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Helvetica,Arial,sans-serif">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="white"/>',
    ]
    if title:
        parts.append(
            f'<text x="{width / 2}" y="22" font-size="15" font-weight="bold" '
            f'text-anchor="middle">{_esc(title)}</text>'
        )
    parts.append(body)
    parts.append("</svg>")
    return "\n".join(parts)


def svg_stacked_bar(months, series, *, title=None, width=760, height=420, unit="$"):
    """Stacked bar chart: one bar per month, one stack segment per series."""
    if not months or not series:
        return _svg(width, height, "", title=title or "no data")

    margin_l, margin_r, margin_t, margin_b = 70, 20, 50, 70
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    stacked_max = max(
        sum(series[name][i] for name in series) for i in range(len(months))
    ) or 1.0

    bar_w = plot_w / len(months) * 0.7
    gap = plot_w / len(months)

    body = []
    # axes
    body.append(
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" '
        f'stroke="#333" stroke-width="1"/>'
    )
    body.append(
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{margin_l + plot_w}" '
        f'y2="{margin_t + plot_h}" stroke="#333" stroke-width="1"/>'
    )
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = margin_t + plot_h * (1 - frac)
        val = stacked_max * frac
        body.append(
            f'<text x="{margin_l - 8}" y="{y + 4}" font-size="10" text-anchor="end">'
            f'{unit}{val:,.0f}</text>'
        )

    names = list(series.keys())
    for i, month in enumerate(months):
        x = margin_l + i * gap + (gap - bar_w) / 2
        y_cursor = margin_t + plot_h
        for j, name in enumerate(names):
            val = series[name][i]
            if val <= 0:
                continue
            seg_h = plot_h * (val / stacked_max)
            y_cursor -= seg_h
            color = _PALETTE[j % len(_PALETTE)]
            body.append(
                f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{seg_h:.1f}" fill="{color}"><title>{_esc(name)} '
                f'{month}: {unit}{val:,.0f}</title></rect>'
            )
        label_x = x + bar_w / 2
        body.append(
            f'<text x="{label_x:.1f}" y="{margin_t + plot_h + 16}" font-size="9" '
            f'text-anchor="middle" transform="rotate(45 {label_x:.1f} '
            f'{margin_t + plot_h + 16})">{_esc(month)}</text>'
        )

    legend_x = margin_l
    legend_y = height - 14
    for j, name in enumerate(names):
        lx = legend_x + j * 110
        color = _PALETTE[j % len(_PALETTE)]
        body.append(f'<rect x="{lx}" y="{legend_y - 9}" width="9" height="9" fill="{color}"/>')
        body.append(f'<text x="{lx + 13}" y="{legend_y}" font-size="9">{_esc(name)[:14]}</text>')

    return _svg(width, height, "\n".join(body), title=title)


def svg_scatter(points, *, x_key, y_key, group_key, title=None, width=760, height=420):
    """Scatter chart, one color per `group_key` value."""
    if not points:
        return _svg(width, height, "", title=title or "no data")

    margin_l, margin_r, margin_t, margin_b = 70, 20, 50, 40
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    xs = [p[x_key] for p in points]
    ys = [p[y_key] for p in points]
    x_min, x_max = min(xs) * 0.95, max(xs) * 1.05
    y_min, y_max = min(0, min(ys) * 0.95), max(ys) * 1.05
    x_span = (x_max - x_min) or 1.0
    y_span = (y_max - y_min) or 1.0

    def px(x):
        return margin_l + (x - x_min) / x_span * plot_w

    def py(y):
        return margin_t + plot_h - (y - y_min) / y_span * plot_h

    body = [
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" '
        f'stroke="#333" stroke-width="1"/>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{margin_l + plot_w}" '
        f'y2="{margin_t + plot_h}" stroke="#333" stroke-width="1"/>',
    ]

    groups = sorted({p[group_key] for p in points})
    color_by_group = {g: _PALETTE[i % len(_PALETTE)] for i, g in enumerate(groups)}
    for p in points:
        cx, cy = px(p[x_key]), py(p[y_key])
        color = color_by_group[p[group_key]]
        body.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{color}" '
            f'fill-opacity="0.7"><title>{_esc(p.get("issuer") or "")} '
            f'{p[group_key]}={p[y_key]:.2f} spot={p[x_key]:.2f}</title></circle>'
        )

    legend_y = margin_t
    for i, g in enumerate(groups):
        ly = legend_y + i * 14
        body.append(
            f'<circle cx="{width - margin_r - 90}" cy="{ly - 3}" r="4" '
            f'fill="{color_by_group[g]}"/>'
        )
        body.append(
            f'<text x="{width - margin_r - 80}" y="{ly}" font-size="10">{_esc(g)}</text>'
        )

    body.append(
        f'<text x="{margin_l + plot_w / 2}" y="{height - 8}" font-size="10" '
        f'text-anchor="middle">initial_underlying_value (spot)</text>'
    )
    body.append(
        f'<text x="14" y="{margin_t + plot_h / 2}" font-size="10" text-anchor="middle" '
        f'transform="rotate(-90 14 {margin_t + plot_h / 2})">absolute barrier level</text>'
    )

    return _svg(width, height, "\n".join(body), title=title)


def svg_hbar(rows, *, label_key, value_key, title=None, width=760, row_height=26, max_rows=15):
    """Horizontal bar chart -- the league-table read, ranked as given."""
    rows = rows[:max_rows]
    height = 60 + row_height * max(len(rows), 1)
    if not rows:
        return _svg(width, height, "", title=title or "no data")

    margin_l, margin_r, margin_t = 180, 60, 40
    plot_w = width - margin_l - margin_r
    val_max = max(r[value_key] for r in rows) or 1.0

    body = []
    for i, r in enumerate(rows):
        y = margin_t + i * row_height
        bar_w = plot_w * (r[value_key] / val_max) if val_max else 0
        color = _PALETTE[i % len(_PALETTE)]
        label = f'{r[label_key]}'[:26]
        body.append(
            f'<text x="{margin_l - 6}" y="{y + row_height * 0.65:.1f}" font-size="10" '
            f'text-anchor="end">{_esc(label)}</text>'
        )
        body.append(
            f'<rect x="{margin_l}" y="{y + 3}" width="{bar_w:.1f}" '
            f'height="{row_height - 8}" fill="{color}">'
            f'<title>{_esc(r[label_key])}: {r[value_key]:.2f}</title></rect>'
        )
        body.append(
            f'<text x="{margin_l + bar_w + 6:.1f}" y="{y + row_height * 0.65:.1f}" '
            f'font-size="10">{r[value_key]:.2f}</text>'
        )

    return _svg(width, height, "\n".join(body), title=title)


# --------------------------------------------------------------------------- #
# Written findings -- short, descriptive, numbers plugged from the real data
# --------------------------------------------------------------------------- #

def finding_issuance(agg):
    if not agg["months"]:
        return (
            "**Issuance by underlying.** No document in the store has both a "
            "dated `aggregate_principal` and a non-empty `underlyings[]]` yet "
            "-- nothing to aggregate."
        )
    total = sum(agg["totals_by_underlying"].values())
    top_name, top_total = max(agg["totals_by_underlying"].items(), key=lambda kv: kv[1])
    span = f"{agg['months'][0]} to {agg['months'][-1]}"
    n_underlyings = len(agg["totals_by_underlying"])
    share = (top_total / total) if total else 0.0
    note = ""
    if agg.get("skipped_no_month"):
        note = (
            f" ({agg['skipped_no_month']} document(s) with aggregate_principal "
            f"had no filing_date and were excluded.)"
        )
    return (
        f"**Issuance by underlying, {span}.** ${total:,.0f} of aggregate_principal "
        f"across {n_underlyings} distinct underlyings. {top_name} is the largest "
        f"single underlying at ${top_total:,.0f} ({share:.0%} of the total shown). "
        f"Basket/worst-of notes split their principal evenly across every listed "
        f"underlying, so the sum across underlyings never double-counts a dollar "
        f"of `aggregate_principal` -- at the cost of not knowing which name in the "
        f"basket actually drove the hedge.{note} Descriptive only: this is where "
        f"issuance concentrated, not a claim about why."
    )


def finding_barrier(agg):
    points = agg["points"]
    if not points:
        return (
            "**Barrier/autocall level clustering.** No document has both a "
            "barrier field (`barrier_pct` / `coupon_barrier_pct` / "
            "`autocall_barrier_pct`) and `initial_underlying_value` yet."
        )
    lines = [f"**Barrier/autocall level clustering vs spot.** {len(points)} "
             f"barrier observations mapped to absolute price levels via "
             f"`initial_underlying_value` (`absolute_level = spot * pct / 100`)."]
    for field in BARRIER_FIELDS:
        buckets = agg["clusters"].get(field)
        if not buckets:
            continue
        mode_bucket, mode_count = max(buckets.items(), key=lambda kv: kv[1])
        field_n = sum(buckets.values())
        lines.append(
            f"`{field}`: {field_n} observations, densest at {mode_bucket:.0f}% "
            f"of spot ({mode_count} of {field_n})."
        )
    lines.append(
        "Descriptive only: this shows where levels cluster, not what that "
        "clustering means for the underlying going forward."
    )
    return " ".join(lines)


def finding_markup(rows):
    if not rows:
        return (
            "**Issuer markup league table.** No document has "
            "`estimated_value_per_1000` yet -- nothing to rank."
        )
    top = rows[0]
    bottom = rows[-1]
    n_pairs = len(rows)
    total_n = sum(r["n"] for r in rows)
    span = ""
    months = sorted({r["first_month"] for r in rows if r["first_month"]} |
                    {r["last_month"] for r in rows if r["last_month"]})
    if months:
        span = f" ({months[0]} to {months[-1]})"
    return (
        f"**Issuer markup league table{span}.** {total_n} priced notes across "
        f"{n_pairs} (issuer, product_type) pairs, ranked by average markup "
        f"against the $1,000 issue price (`markup_pct = (1000 - "
        f"estimated_value_per_1000) / 1000`). Highest average markup: "
        f"{top['issuer']} / {top['product_type']} at {top['avg_markup_pct']:.2f}% "
        f"over {top['n']} note(s). Lowest: {bottom['issuer']} / "
        f"{bottom['product_type']} at {bottom['avg_markup_pct']:.2f}% over "
        f"{bottom['n']} note(s). Descriptive only: a ranking of disclosed "
        f"markup, not a claim that any issuer is mispricing."
    )


# --------------------------------------------------------------------------- #
# Write all three outputs to disk
# --------------------------------------------------------------------------- #

def write_outputs(store, out_dir, *, query_kwargs=None, top_n_underlyings=8,
                  max_league_rows=15):
    """Build all three outputs from `store` and write chart + data + finding
    to `out_dir`. Returns a dict summarizing what was written (paths + the
    three finding strings), so a caller (CLI or test) can assert on it
    without re-reading the files."""
    os.makedirs(out_dir, exist_ok=True)
    docs = load_documents(store, **(query_kwargs or {}))

    issuance = issuance_by_underlying(docs, top_n=top_n_underlyings)
    barrier = barrier_clustering(docs)
    league = issuer_markup_league(docs)

    issuance_svg = svg_stacked_bar(
        issuance["months"], issuance["series"],
        title="Structured-note issuance by underlying, by month", unit="$",
    )
    barrier_svg = svg_scatter(
        barrier["points"], x_key="spot", y_key="absolute_level", group_key="field",
        title="Barrier/autocall levels vs spot (initial_underlying_value)",
    )
    league_svg = svg_hbar(
        league, label_key="issuer", value_key="avg_markup_pct",
        title="Issuer markup league table (avg % below $1,000 issue price)",
        max_rows=max_league_rows,
    )

    finding_1 = finding_issuance(issuance)
    finding_2 = finding_barrier(barrier)
    finding_3 = finding_markup(league)

    paths = {
        "issuance_json": os.path.join(out_dir, "issuance_by_underlying.json"),
        "issuance_svg": os.path.join(out_dir, "issuance_by_underlying.svg"),
        "barrier_json": os.path.join(out_dir, "barrier_clustering.json"),
        "barrier_svg": os.path.join(out_dir, "barrier_clustering.svg"),
        "league_json": os.path.join(out_dir, "issuer_markup_league.json"),
        "league_svg": os.path.join(out_dir, "issuer_markup_league.svg"),
        "findings_md": os.path.join(out_dir, "findings.md"),
    }

    with open(paths["issuance_json"], "w", encoding="utf-8") as f:
        json.dump({"data": issuance, "finding": finding_1}, f, indent=2, sort_keys=True)
    with open(paths["issuance_svg"], "w", encoding="utf-8") as f:
        f.write(issuance_svg)

    with open(paths["barrier_json"], "w", encoding="utf-8") as f:
        json.dump({"data": barrier, "finding": finding_2}, f, indent=2, sort_keys=True)
    with open(paths["barrier_svg"], "w", encoding="utf-8") as f:
        f.write(barrier_svg)

    with open(paths["league_json"], "w", encoding="utf-8") as f:
        json.dump({"data": league, "finding": finding_3}, f, indent=2, sort_keys=True)
    with open(paths["league_svg"], "w", encoding="utf-8") as f:
        f.write(league_svg)

    findings_md = (
        "# Structured-note issuance maps -- findings (#130)\n\n"
        f"{len(docs)} document(s) read from the scrubber store.\n\n"
        f"## 1. Issuance by underlying\n\n{finding_1}\n\n"
        f"## 2. Barrier/autocall level clustering vs spot\n\n{finding_2}\n\n"
        f"## 3. Issuer markup league table\n\n{finding_3}\n"
    )
    with open(paths["findings_md"], "w", encoding="utf-8") as f:
        f.write(findings_md)

    return {
        "n_documents": len(docs),
        "paths": paths,
        "findings": {"issuance": finding_1, "barrier": finding_2, "markup": finding_3},
        "data": {"issuance": issuance, "barrier": barrier, "league": league},
    }


if __name__ == "__main__":
    # Self-check against an in-memory store -- no files, no clock, no network.
    try:
        from .output_store import OutputStore, DocumentExtraction
    except ImportError:
        from output_store import OutputStore, DocumentExtraction

    store = OutputStore(":memory:")
    rid = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")
    store.write_document(rid, DocumentExtraction.from_record(
        "0001-24-000001", "424b2.htm",
        {
            "issuer": "JPMorgan Chase Financial Company LLC",
            "product_type": "autocallable contingent coupon",
            "pricing_date": "2026-01-15",
            "underlyings": [{"name": "S&P 500 Index", "kind": "index"}],
            "aggregate_principal": 2_500_000,
            "estimated_value_per_1000": 972.4,
            "barrier_pct": 70.0,
            "initial_underlying_value": 5000.0,
        },
    ))
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = write_outputs(store, tmp)
        print(f"wrote {len(result['paths'])} files for {result['n_documents']} document(s)")
        print(result["findings"]["issuance"][:80] + "...")
    print("\nresearch_maps self-check: PASS")
