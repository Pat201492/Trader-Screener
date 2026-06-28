"""Generate annotated chart PNGs for the Derivatives education doc.

Each payoff chart is annotated with the metrics a trader actually reads off it
(breakeven, max profit, max loss) plus the IV/volatility signal you'd screen for
before taking the trade. Run from this folder:
    python _build_charts.py
Outputs PNGs into ./img/.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(HERE, "img")
os.makedirs(IMG, exist_ok=True)

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


def _base(ax, S, title):
    ax.axhline(0, color=GREY, lw=1)
    ax.axvline(S, color=GREY, ls=":", lw=1)
    ax.set_title(title)
    ax.set_xlabel("Underlying price at expiration")
    ax.set_ylabel("Profit / Loss ($ per share)")
    ax.grid(alpha=0.25)


def save(fig, name):
    path = os.path.join(IMG, name)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print("wrote", path)


def fig_long_call_put():
    S = np.linspace(50, 150, 400)
    K, prem = 100, 5
    call = np.maximum(S - K, 0) - prem
    put = np.maximum(K - S, 0) - prem
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))

    ax = axes[0]
    _base(ax, K, "Long Call  —  bullish, leveraged")
    ax.plot(S, call, color=GREEN, lw=2)
    ax.axhline(-prem, color=RED, ls="--", lw=1)
    be = K + prem
    ax.axvline(be, color=NAVY, ls="--", lw=1)
    ax.annotate(f"Max loss = premium (${prem})", (52, -prem), color=RED, fontsize=8.5, va="bottom")
    ax.annotate(f"Breakeven = K+prem = {be:.0f}", (be + 1, 8), color=NAVY, fontsize=8.5)
    ax.annotate("Upside\nunlimited", (140, 30), color=GREEN, fontsize=9, ha="center")
    ax.text(52, 34, "SCREEN: enter when IV Rank LOW\n(options cheap)", fontsize=8, color=TEAL,
            bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))

    ax = axes[1]
    _base(ax, K, "Long Put  —  bearish / hedge")
    ax.plot(S, put, color=RED, lw=2)
    ax.axhline(-prem, color=RED, ls="--", lw=1)
    be = K - prem
    ax.axvline(be, color=NAVY, ls="--", lw=1)
    ax.annotate(f"Max loss = premium (${prem})", (120, -prem), color=RED, fontsize=8.5, va="bottom", ha="right")
    ax.annotate(f"Breakeven = K-prem = {be:.0f}", (be - 1, 8), color=NAVY, fontsize=8.5, ha="right")
    ax.annotate("Profit grows\nas price falls", (60, 30), color=RED, fontsize=9, ha="center")
    ax.text(95, 34, "SCREEN: buy protection when\nskew/VIX still low (cheap)", fontsize=8, color=TEAL,
            bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))
    save(fig, "payoff_long_call_put.png")


def fig_income():
    S = np.linspace(50, 150, 400)
    K, prem = 100, 5
    short_put = prem - np.maximum(K - S, 0)
    # covered call = own stock (S - S0) + short call premium
    S0 = 100
    covered = (S - S0) + prem - np.maximum(S - K, 0)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))

    ax = axes[0]
    _base(ax, K, "Short Put  —  income, bullish-neutral")
    ax.plot(S, short_put, color=GREEN, lw=2)
    ax.axhline(prem, color=GREEN, ls="--", lw=1)
    be = K - prem
    ax.axvline(be, color=NAVY, ls="--", lw=1)
    ax.annotate(f"Max profit = premium (${prem})", (120, prem), color=GREEN, fontsize=8.5, va="bottom", ha="right")
    ax.annotate(f"Breakeven = {be:.0f}", (be - 1, -20), color=NAVY, fontsize=8.5, ha="right")
    ax.annotate("Large loss if\nprice collapses", (60, -34), color=RED, fontsize=9, ha="center")
    ax.text(92, 12, "SCREEN: sell when IV Rank HIGH\n(premium rich)", fontsize=8, color=TEAL,
            bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))

    ax = axes[1]
    _base(ax, K, "Covered Call  —  income on a holding")
    ax.plot(S, covered, color=TEAL, lw=2)
    capped = prem + (K - S0)
    ax.axhline(capped, color=GREEN, ls="--", lw=1)
    ax.annotate(f"Max profit capped = ${capped:.0f}", (115, capped), color=GREEN, fontsize=8.5, va="bottom", ha="right")
    ax.annotate("Still exposed to\ndownside of stock", (60, -34), color=RED, fontsize=9, ha="center")
    ax.text(70, 12, "SCREEN: write when IV Rank HIGH\n+ neutral/mild-bull view", fontsize=8, color=TEAL,
            bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))
    save(fig, "payoff_income.png")


def fig_spread():
    S = np.linspace(50, 150, 400)
    Kl, Kh = 95, 110
    debit = 6
    bull = (np.maximum(S - Kl, 0) - np.maximum(S - Kh, 0)) - debit
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    _base(ax, Kl, "Bull Call Spread  —  defined risk & reward")
    ax.plot(S, bull, color=NAVY, lw=2)
    maxp = (Kh - Kl) - debit
    ax.axhline(-debit, color=RED, ls="--", lw=1)
    ax.axhline(maxp, color=GREEN, ls="--", lw=1)
    be = Kl + debit
    ax.axvline(be, color=GREY, ls="--", lw=1)
    ax.annotate(f"Max loss = debit (${debit})", (52, -debit + 0.4), color=RED, fontsize=9, va="bottom")
    ax.annotate(f"Max profit = ${maxp} (spread - debit)", (148, maxp + 0.3), color=GREEN, fontsize=9, ha="right", va="bottom")
    ax.annotate(f"Breakeven = {be:.0f}", (be + 1, -8), color=NAVY, fontsize=9)
    ax.text(118, -7, "SCREEN: use vs a long call when\nIV high (caps cost of vega)", fontsize=8, color=TEAL,
            ha="center", bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))
    save(fig, "payoff_bull_call_spread.png")


def fig_skew():
    K = np.linspace(70, 130, 200)
    atm = 100
    # downward-sloping equity skew: OTM puts rich, OTM calls cheaper
    iv = 0.22 + 0.0016 * (atm - K) + 0.000015 * (K - atm) ** 2
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    ax.plot(K, iv * 100, color=NAVY, lw=2)
    ax.axvline(atm, color=GREY, ls=":", lw=1)
    ax.set_title("Volatility Skew  —  IV by strike (equity)")
    ax.set_xlabel("Strike price")
    ax.set_ylabel("Implied volatility (%)")
    ax.grid(alpha=0.25)
    ax.annotate("OTM PUTS richer\n(hedging demand)", (80, iv[np.argmin(abs(K - 80))] * 100 + 1),
                color=RED, fontsize=9, ha="center")
    ax.annotate("OTM CALLS cheaper", (120, iv[np.argmin(abs(K - 120))] * 100 + 0.6),
                color=GREEN, fontsize=9, ha="center")
    ax.annotate("ATM", (atm, iv[np.argmin(abs(K - atm))] * 100 - 1.4), color=GREY, fontsize=9, ha="center")
    ax.text(95, iv.max() * 100 - 0.5,
            "METRIC: skew steepness = fear / crash-pricing gauge.\nSteepening put skew -> market buying protection.",
            fontsize=8, color=TEAL, ha="center",
            bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))
    save(fig, "vol_skew.png")


def fig_term_structure():
    months = np.array([1, 2, 3, 6, 9, 12])
    contango = np.array([18, 19, 20, 21, 21.5, 22])
    backward = np.array([34, 30, 27, 24, 22.5, 22])
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    ax.plot(months, contango, "o-", color=GREEN, lw=2, label="Contango (calm) — far IV > near")
    ax.plot(months, backward, "o-", color=RED, lw=2, label="Backwardation (stress) — near IV > far")
    ax.set_title("IV Term Structure  —  IV by expiration")
    ax.set_xlabel("Months to expiration")
    ax.set_ylabel("Implied volatility (%)")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8.5)
    ax.annotate("Near-term event risk\nspikes front of curve", (1, 34.5), color=RED, fontsize=8.5)
    ax.text(7, 33, "METRIC: backwardation = acute near-term\nrisk (earnings, macro shock, selloff)",
            fontsize=8, color=TEAL, ha="center",
            bbox=dict(boxstyle="round", fc="#eef3f8", ec=TEAL, lw=0.8))
    save(fig, "iv_term_structure.png")


def fig_futures_vs_option():
    S = np.linspace(50, 150, 400)
    entry = 100
    fut = S - entry
    prem = 5
    call = np.maximum(S - entry, 0) - prem
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    _base(ax, entry, "Linear vs Asymmetric Payoff")
    ax.plot(S, fut, color=NAVY, lw=2, label="Future (linear, both ways)")
    ax.plot(S, call, color=GREEN, lw=2, label="Long call (capped loss)")
    ax.axhline(-prem, color=RED, ls="--", lw=1)
    ax.legend(fontsize=8.5, loc="upper left")
    ax.annotate("Future: symmetric,\nunlimited both directions", (130, fut[np.argmin(abs(S - 130))] - 6),
                color=NAVY, fontsize=8.5, ha="center")
    ax.annotate("Option: loss floored\nat premium", (70, -prem - 9), color=RED, fontsize=8.5, ha="center")
    save(fig, "futures_vs_option.png")


if __name__ == "__main__":
    fig_long_call_put()
    fig_income()
    fig_spread()
    fig_skew()
    fig_term_structure()
    fig_futures_vs_option()
    print("done")
