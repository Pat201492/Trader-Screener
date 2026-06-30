"""Payoff charts for the Information Sheet / Tear Sheet examples.

Two desk-style recommendation payoffs:
  - Equity: bull call spread (defined-risk bullish expression)
  - Commodity: producer costless collar (hedge a long physical position)
Run from this folder:  python _build_tearsheets.py   -> PNGs into ./img/
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(HERE, "img")
os.makedirs(IMG, exist_ok=True)
NAVY, TEAL, GREEN, RED, GREY, PURPLE = "#0b3d6b", "#1b5e8c", "#1b7f4b", "#b3261e", "#888888", "#6a3d9a"
plt.rcParams.update({"figure.dpi": 130, "font.size": 9.5, "axes.titlesize": 11,
                     "axes.titleweight": "bold", "axes.edgecolor": "#cccccc",
                     "text.parse_math": False})  # keep literal $ signs (no mathtext italics)


def save(fig, name):
    p = os.path.join(IMG, name); fig.tight_layout(); fig.savefig(p, bbox_inches="tight"); plt.close(fig); print("wrote", p)


def equity_bull_call_spread():
    # NWS @ 200; buy 205 call, sell 225 call, net debit 7
    S = np.linspace(170, 250, 500); kl, kh, debit = 205, 225, 7
    pl = np.maximum(S - kl, 0) - np.maximum(S - kh, 0) - debit
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axhline(0, color=GREY, lw=0.9); ax.axvline(200, color=GREY, ls=":", lw=0.9)
    ax.plot(S, pl, color=GREEN, lw=2.2)
    maxp = (kh - kl) - debit; be = kl + debit
    ax.axhline(maxp, color=GREEN, ls="--", lw=1); ax.axhline(-debit, color=RED, ls="--", lw=1)
    ax.axvline(be, color=NAVY, ls="--", lw=1)
    ax.set_title("NWS — Bull Call Spread  (long 205C / short 225C, debit $7)")
    ax.set_xlabel("NWS price at expiry ($)"); ax.set_ylabel("P / L ($/share)"); ax.grid(alpha=0.22)
    ax.annotate(f"Max profit = ${maxp}", (248, maxp + 0.3), color=GREEN, fontsize=9, ha="right", va="bottom")
    ax.annotate(f"Max loss = ${debit}", (172, -debit + 0.3), color=RED, fontsize=9, va="bottom")
    ax.annotate(f"Breakeven = ${be}", (be + 1, -5), color=NAVY, fontsize=9)
    ax.annotate("spot $200", (200, maxp + 1.5), color=GREY, fontsize=8, ha="center")
    save(fig, "tear_equity_payoff.png")


def commodity_collar():
    # Producer long crude @ 78; buy 72 put, sell 88 call (≈ zero cost)
    S = np.linspace(55, 105, 500); spot, kp, kc = 78, 72, 88
    pl = (S - spot) + np.maximum(kp - S, 0) - np.maximum(S - kc, 0)
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axhline(0, color=GREY, lw=0.9); ax.axvline(spot, color=GREY, ls=":", lw=0.9)
    ax.plot(S, S - spot, color=GREY, lw=1.4, ls=":", label="Unhedged long crude")
    ax.plot(S, pl, color=PURPLE, lw=2.2, label="Collared position")
    ax.axhline(kp - spot, color=RED, ls="--", lw=1); ax.axhline(kc - spot, color=GREEN, ls="--", lw=1)
    ax.set_title("WTI Producer Collar  (long crude + $72 put / short $88 call, ~$0 cost)")
    ax.set_xlabel("WTI price at expiry ($/bbl)"); ax.set_ylabel("P / L ($/bbl)"); ax.grid(alpha=0.22)
    ax.legend(fontsize=8.5, loc="upper left")
    ax.annotate(f"Floor: -${spot-kp}/bbl ($72 put)", (57, kp - spot + 0.4), color=RED, fontsize=8.5, va="bottom")
    ax.annotate(f"Cap: +${kc-spot}/bbl ($88 call)", (103, kc - spot + 0.4), color=GREEN, fontsize=8.5, ha="right", va="bottom")
    ax.annotate("spot $78", (spot, kc - spot + 3), color=GREY, fontsize=8, ha="center")
    save(fig, "tear_commodity_payoff.png")


def equity_bear_put_spread():
    # OVR @ 150; buy 150 put, sell 130 put, debit 7
    S = np.linspace(110, 180, 500); kh, kl, debit = 150, 130, 7
    pl = np.maximum(kh - S, 0) - np.maximum(kl - S, 0) - debit
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axhline(0, color=GREY, lw=0.9); ax.axvline(150, color=GREY, ls=":", lw=0.9)
    ax.plot(S, pl, color=RED, lw=2.2)
    maxp = (kh - kl) - debit; be = kh - debit
    ax.axhline(maxp, color=GREEN, ls="--", lw=1); ax.axhline(-debit, color=RED, ls="--", lw=1); ax.axvline(be, color=NAVY, ls="--", lw=1)
    ax.set_title("OVR — Bear Put Spread  (long 150P / short 130P, debit $7)")
    ax.set_xlabel("OVR price at expiry ($)"); ax.set_ylabel("P / L ($/share)"); ax.grid(alpha=0.22)
    ax.annotate(f"Max profit = ${maxp}", (112, maxp + 0.3), color=GREEN, fontsize=9, va="bottom")
    ax.annotate(f"Max loss = ${debit}", (178, -debit + 0.3), color=RED, fontsize=9, ha="right", va="bottom")
    ax.annotate(f"Breakeven = ${be}", (be - 1, -4), color=NAVY, fontsize=9, ha="right")
    save(fig, "tear_bear_put_payoff.png")


def equity_iron_condor():
    # RNG @ 100; long 88P/short 93P + short 107C/long 112C, credit 1.6, wing width 5
    S = np.linspace(80, 120, 600); credit = 1.6
    pl = credit - np.maximum(93 - S, 0) + np.maximum(88 - S, 0) - np.maximum(S - 107, 0) + np.maximum(S - 112, 0)
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axhline(0, color=GREY, lw=0.9); ax.axvline(100, color=GREY, ls=":", lw=0.9)
    ax.plot(S, pl, color=GREEN, lw=2.2)
    maxloss = 5 - credit
    ax.axhline(credit, color=GREEN, ls="--", lw=1); ax.axhline(-maxloss, color=RED, ls="--", lw=1)
    ax.set_title("RNG — Iron Condor  (88/93 put + 107/112 call spreads, credit $1.60)")
    ax.set_xlabel("RNG price at expiry ($)"); ax.set_ylabel("P / L ($/share)"); ax.grid(alpha=0.22)
    ax.annotate(f"Max profit = +${credit} (price stays 93-107)", (100, credit + 0.3), color=GREEN, fontsize=8.5, ha="center", va="bottom")
    ax.annotate(f"Max loss = -${maxloss:.1f}", (81, -maxloss + 0.15), color=RED, fontsize=9, va="bottom")
    ax.annotate("BE 91.4", (91.4, -1.6), color=NAVY, fontsize=8, ha="right")
    ax.annotate("BE 108.6", (108.6, -1.6), color=NAVY, fontsize=8)
    save(fig, "tear_iron_condor_payoff.png")


def equity_long_straddle():
    # BIO @ 50 pre-event, low IV; long 50C + 50P, total premium 4
    S = np.linspace(35, 65, 500); K, prem = 50, 4
    pl = np.maximum(S - K, 0) + np.maximum(K - S, 0) - prem
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axhline(0, color=GREY, lw=0.9); ax.axvline(K, color=GREY, ls=":", lw=0.9)
    ax.plot(S, pl, color=PURPLE, lw=2.2)
    ax.axhline(-prem, color=RED, ls="--", lw=1)
    for be in (K - prem, K + prem):
        ax.axvline(be, color=NAVY, ls="--", lw=1)
    ax.set_title("BIO — Long Straddle  (long 50C + 50P, total premium $4)")
    ax.set_xlabel("BIO price at expiry ($)"); ax.set_ylabel("P / L ($/share)"); ax.grid(alpha=0.22)
    ax.annotate(f"Max loss = -${prem} (no move)", (50, -prem - 1.4), color=RED, fontsize=9, ha="center")
    ax.annotate("BE $46", (46, 6), color=NAVY, fontsize=8.5, ha="right")
    ax.annotate("BE $54", (54, 6), color=NAVY, fontsize=8.5)
    ax.annotate("profit on a big move\neither direction", (50, 9), color=PURPLE, fontsize=8.5, ha="center")
    save(fig, "tear_straddle_payoff.png")


def etf_protective_put():
    # ETF @ 500; long ETF + buy 480 put, premium 8
    S = np.linspace(430, 560, 500); S0, kp, prem = 500, 480, 8
    pl = (S - S0) + np.maximum(kp - S, 0) - prem
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axhline(0, color=GREY, lw=0.9); ax.axvline(S0, color=GREY, ls=":", lw=0.9)
    ax.plot(S, S - S0, color=GREY, lw=1.4, ls=":", label="Unhedged ETF")
    ax.plot(S, pl, color=NAVY, lw=2.2, label="ETF + protective put")
    floor = (kp - S0) - prem
    ax.axhline(floor, color=RED, ls="--", lw=1)
    ax.set_title("BMKT ETF — Protective Put  (long ETF + 480 put, premium $8)")
    ax.set_xlabel("ETF price at expiry ($)"); ax.set_ylabel("P / L ($/share)"); ax.grid(alpha=0.22)
    ax.legend(fontsize=8.5, loc="upper left")
    ax.annotate(f"Loss floored at ${floor}/sh", (435, floor + 1.5), color=RED, fontsize=8.5, va="bottom")
    ax.annotate("upside intact\n(less premium)", (545, 30), color=GREEN, fontsize=8.5, ha="right")
    save(fig, "tear_protective_put_payoff.png")


if __name__ == "__main__":
    equity_bull_call_spread()
    commodity_collar()
    equity_bear_put_spread()
    equity_iron_condor()
    equity_long_straddle()
    etf_protective_put()
    print("done")
