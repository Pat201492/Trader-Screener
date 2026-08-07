"""
Part A charts (issue #110) -- one PNG per descriptive output in analysis.py.

matplotlib is already a repo dependency (Education/_build_charts.py uses it
the same way); Agg backend so this runs headless. These functions are pure
renderers: they take analysis.py's output and a path, and write a PNG. No
data loading here -- see build_report.py for the OutputStore -> analysis ->
charts pipeline.

Run the self-check (renders all three against synthetic data):
python Research/structured_note_issuance/test_analysis.py
"""

from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

NAVY = "#0b3d6b"
TEAL = "#1b5e8c"
GREEN = "#1b7f4b"
RED = "#b3261e"
GREY = "#888888"

plt.rcParams.update({
    "figure.dpi": 130,
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.edgecolor": "#cccccc",
})


def issuance_chart(issuance_by_month, out_path, top_n=8):
    """Stacked bar of aggregate_principal ($MM) by underlying, by month. Only
    the top_n underlyings by total issuance get their own series; everything
    else folds into 'other' so the legend stays readable."""
    months = sorted({m for _, m in issuance_by_month})
    totals = defaultdict(float)
    for (u, _m), v in issuance_by_month.items():
        totals[u] += v
    top = [u for u, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top_n]]

    fig, ax = plt.subplots(figsize=(9, 5))
    if not months:
        ax.set_title("Structured-note issuance by underlying, by month (no data)")
        fig.tight_layout()
        fig.savefig(out_path)
        plt.close(fig)
        return out_path

    bottom = [0.0] * len(months)
    for u in top:
        vals = [issuance_by_month.get((u, m), 0.0) / 1e6 for m in months]
        ax.bar(months, vals, bottom=bottom, label=u)
        bottom = [b + v for b, v in zip(bottom, vals)]
    other = [
        sum(v for (uu, m), v in issuance_by_month.items() if uu not in top and m == mm) / 1e6
        for mm in months
    ]
    if any(other):
        ax.bar(months, other, bottom=bottom, label="other", color=GREY)

    ax.set_title("Structured-note issuance by underlying, by month")
    ax.set_ylabel("Aggregate principal ($MM)")
    ax.legend(fontsize=7, ncol=2)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def barrier_chart(barrier_levels, out_path, top_n=4):
    """One histogram subplot per top_n underlying (by barrier-level count),
    absolute barrier/coupon-barrier/autocall-barrier levels overlaid."""
    by_underlying = defaultdict(list)
    for b in barrier_levels:
        by_underlying[b.underlying].append(b)
    top = sorted(by_underlying, key=lambda u: -len(by_underlying[u]))[:top_n]
    n_panels = max(len(top), 1)

    fig, axes = plt.subplots(1, n_panels, figsize=(4 * n_panels, 4), squeeze=False)
    colors = {"barrier_pct": RED, "coupon_barrier_pct": TEAL, "autocall_barrier_pct": GREEN}
    if not top:
        axes[0][0].set_title("no barrier data")
    for ax, u in zip(axes[0], top):
        for bt, color in colors.items():
            levels = [b.absolute_level for b in by_underlying[u] if b.barrier_type == bt]
            if levels:
                ax.hist(levels, bins=min(12, max(3, len(levels))), alpha=0.6,
                         color=color, label=bt)
        ax.set_title(u, fontsize=9)
        ax.set_xlabel("Absolute level")
        ax.legend(fontsize=6)
    fig.suptitle("Barrier / autocall levels vs initial underlying value")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def markup_chart(markup_buckets, out_path, top_n=10):
    """Horizontal bar of principal-weighted issuer markup (%), issuer-level
    (collapsed across product_type/month), top_n issuers by markup."""
    by_issuer = defaultdict(lambda: [0.0, 0.0])   # issuer -> [weighted_num, principal_den]
    for b in markup_buckets:
        acc = by_issuer[b.issuer]
        acc[0] += b.principal_weighted_markup_pct * b.total_principal
        acc[1] += b.total_principal
    rows = [(iss, (num / den if den else 0.0)) for iss, (num, den) in by_issuer.items()]
    rows.sort(key=lambda r: -r[1])
    rows = rows[:top_n]

    fig, ax = plt.subplots(figsize=(8, 5))
    if rows:
        ax.barh([r[0] for r in rows][::-1], [r[1] for r in rows][::-1], color=NAVY)
    ax.set_xlabel("Issuer markup vs $1,000 issue price (%)")
    ax.set_title("Issuer markup league table")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path
