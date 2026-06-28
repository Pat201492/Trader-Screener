"""Generate the chart set for the Derivatives Examples Gallery.

Payoff diagrams (at expiration) for the common strategies, plus Black-Scholes
Greek-profile charts (delta/gamma/theta/vega). Pure numpy + matplotlib; normal
CDF/PDF implemented locally so scipy isn't required. Run from this folder:
    python _build_examples.py
Outputs PNGs into ./img/.
"""
import os
import math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(HERE, "img")
os.makedirs(IMG, exist_ok=True)

NAVY, TEAL, GREEN, RED, GREY, PURPLE = "#0b3d6b", "#1b5e8c", "#1b7f4b", "#b3261e", "#888888", "#6a3d9a"
plt.rcParams.update({"figure.dpi": 130, "font.size": 9.5, "axes.titlesize": 11,
                     "axes.titleweight": "bold", "axes.edgecolor": "#cccccc"})

_erf = np.vectorize(math.erf)
def N(x):  # standard normal CDF
    return 0.5 * (1 + _erf(np.asarray(x, float) / math.sqrt(2)))
def n(x):  # standard normal PDF
    return np.exp(-np.asarray(x, float) ** 2 / 2) / math.sqrt(2 * math.pi)


def save(fig, name):
    path = os.path.join(IMG, name)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print("wrote", path)


def payoff_ax(ax, S, K, title):
    ax.axhline(0, color=GREY, lw=0.9)
    ax.axvline(K, color=GREY, ls=":", lw=0.9)
    ax.set_title(title)
    ax.set_xlabel("Price at expiry")
    ax.set_ylabel("P / L")
    ax.grid(alpha=0.22)


# ---------- Black-Scholes Greeks ----------
def bs_greeks(S, K, r, sigma, T):
    S = np.asarray(S, float)
    d1 = (np.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    call_delta = N(d1)
    put_delta = N(d1) - 1
    gamma = n(d1) / (S * sigma * math.sqrt(T))
    vega = S * n(d1) * math.sqrt(T) / 100          # per 1% IV
    call_theta = (-S * n(d1) * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * N(d2)) / 365
    return dict(d1=d1, call_delta=call_delta, put_delta=put_delta, gamma=gamma, vega=vega, call_theta=call_theta)


# ============ FIGURE 1: single-leg payoffs ============
def fig_single_legs():
    S = np.linspace(60, 140, 400); K, p = 100, 5
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    payoff_ax(ax[0,0], S, K, "Long Call (bullish)")
    ax[0,0].plot(S, np.maximum(S-K,0)-p, color=GREEN, lw=2)
    ax[0,0].annotate("max loss = -prem", (62,-p+0.5), color=RED, fontsize=8)
    ax[0,0].annotate(f"BE={K+p}", (K+p,3), color=NAVY, fontsize=8)

    payoff_ax(ax[0,1], S, K, "Long Put (bearish/hedge)")
    ax[0,1].plot(S, np.maximum(K-S,0)-p, color=RED, lw=2)
    ax[0,1].annotate("max loss = -prem", (120,-p+0.5), color=RED, fontsize=8, ha="right")
    ax[0,1].annotate(f"BE={K-p}", (K-p,3), color=NAVY, fontsize=8, ha="right")

    payoff_ax(ax[1,0], S, K, "Short Call (bearish income, undefined risk)")
    ax[1,0].plot(S, p-np.maximum(S-K,0), color=RED, lw=2)
    ax[1,0].annotate("max profit = +prem", (62,p-0.5), color=GREEN, fontsize=8, va="top")
    ax[1,0].annotate("loss unlimited", (132,-22), color=RED, fontsize=8, ha="right")

    payoff_ax(ax[1,1], S, K, "Short Put (bullish income)")
    ax[1,1].plot(S, p-np.maximum(K-S,0), color=GREEN, lw=2)
    ax[1,1].annotate("max profit = +prem", (130,p-0.5), color=GREEN, fontsize=8, va="top", ha="right")
    ax[1,1].annotate("big loss if crash", (62,-22), color=RED, fontsize=8)
    save(fig, "ex_single_legs.png")


# ============ FIGURE 2: stock + option ============
def fig_stock_combos():
    S = np.linspace(60,140,400); S0,K,p = 100,100,5
    fig, ax = plt.subplots(1, 3, figsize=(13, 4.3))
    payoff_ax(ax[0], S, K, "Covered Call")
    ax[0].plot(S, (S-S0)+p-np.maximum(S-K,0), color=TEAL, lw=2)
    ax[0].annotate("upside capped at K", (118,p+0.3), color=GREEN, fontsize=8, ha="right")

    Kp=95
    payoff_ax(ax[1], S, Kp, "Protective Put (insured stock)")
    ax[1].plot(S, (S-S0)-p+np.maximum(Kp-S,0), color=NAVY, lw=2)
    ax[1].annotate("downside floored", (66,-12), color=GREEN, fontsize=8)

    Kc=110
    payoff_ax(ax[2], S, S0, "Collar (put floor + call cap)")
    ax[2].plot(S, (S-S0)+np.maximum(Kp-S,0)-np.maximum(S-Kc,0), color=PURPLE, lw=2)
    ax[2].annotate("range-bound P/L", (100,-14), color=PURPLE, fontsize=8, ha="center")
    save(fig, "ex_stock_combos.png")


# ============ FIGURE 3: vertical spreads ============
def fig_verticals():
    S = np.linspace(60,140,400)
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    Kl,Kh,d = 95,110,6
    payoff_ax(ax[0], S, Kl, "Bull Call Spread (debit, defined)")
    ax[0].plot(S, np.maximum(S-Kl,0)-np.maximum(S-Kh,0)-d, color=GREEN, lw=2)
    ax[0].annotate(f"max +{(Kh-Kl)-d}", (138,(Kh-Kl)-d+0.3), color=GREEN, fontsize=8, ha="right")
    ax[0].annotate(f"max -{d}", (62,-d+0.3), color=RED, fontsize=8)

    Kl2,Kh2,c = 90,105,6
    payoff_ax(ax[1], S, Kh2, "Bear Put Spread (debit, defined)")
    ax[1].plot(S, np.maximum(Kh2-S,0)-np.maximum(Kl2-S,0)-c, color=RED, lw=2)
    ax[1].annotate(f"max +{(Kh2-Kl2)-c}", (62,(Kh2-Kl2)-c+0.3), color=GREEN, fontsize=8)
    ax[1].annotate(f"max -{c}", (138,-c+0.3), color=RED, fontsize=8, ha="right")
    save(fig, "ex_verticals.png")


# ============ FIGURE 4: volatility strategies ============
def fig_vol_strategies():
    S = np.linspace(60,140,400); K=100; p=5
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    payoff_ax(ax[0,0], S, K, "Long Straddle (long vol)")
    ax[0,0].plot(S, np.maximum(S-K,0)+np.maximum(K-S,0)-2*p, color=PURPLE, lw=2)
    ax[0,0].annotate("profit on a big move\neither way", (100,12), color=PURPLE, fontsize=8, ha="center")

    Kp,Kc=92,108
    payoff_ax(ax[0,1], S, K, "Long Strangle (cheaper long vol)")
    ax[0,1].plot(S, np.maximum(S-Kc,0)+np.maximum(Kp-S,0)-2*(p-2), color=PURPLE, lw=2)
    ax[0,1].annotate("wider breakevens", (100,10), color=PURPLE, fontsize=8, ha="center")

    # Iron condor: short put spread + short call spread (credit)
    a,b,c,d = 85,92,108,115; cr=4
    ic = cr - np.maximum(b-S,0)+np.maximum(a-S,0) - np.maximum(S-c,0)+np.maximum(S-d,0)
    payoff_ax(ax[1,0], S, K, "Iron Condor (short vol, defined)")
    ax[1,0].plot(S, ic, color=GREEN, lw=2)
    ax[1,0].annotate("profit if price stays\nin the range", (100,cr-1.5), color=GREEN, fontsize=8, ha="center", va="top")

    # Long call butterfly
    l,m,h,deb = 90,100,110,3
    bf = np.maximum(S-l,0)-2*np.maximum(S-m,0)+np.maximum(S-h,0)-deb
    payoff_ax(ax[1,1], S, m, "Long Butterfly (pin to strike)")
    ax[1,1].plot(S, bf, color=TEAL, lw=2)
    ax[1,1].annotate("peak profit if price\npins the middle strike", (100,(m-l)-deb-1), color=TEAL, fontsize=8, ha="center", va="top")
    save(fig, "ex_vol_strategies.png")


# ============ FIGURE 5: Greeks vs underlying ============
def fig_greeks_price():
    S = np.linspace(60,140,400); K,r,sig,T = 100,0.04,0.25,0.25
    g = bs_greeks(S,K,r,sig,T)
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0,0].plot(S, g["call_delta"], color=GREEN, lw=2, label="call")
    ax[0,0].plot(S, g["put_delta"], color=RED, lw=2, label="put")
    ax[0,0].axvline(K, color=GREY, ls=":"); ax[0,0].axhline(0, color=GREY, lw=0.8)
    ax[0,0].set_title("Delta vs price (direction exposure)"); ax[0,0].legend(fontsize=8); ax[0,0].grid(alpha=0.22)
    ax[0,0].set_xlabel("Underlying"); ax[0,0].set_ylabel("Delta")
    ax[0,0].annotate("0 OTM -> 1 ITM (call)", (104,0.55), fontsize=8, color=GREEN)

    ax[0,1].plot(S, g["gamma"], color=NAVY, lw=2)
    ax[0,1].axvline(K, color=GREY, ls=":"); ax[0,1].set_title("Gamma vs price (delta's speed)")
    ax[0,1].grid(alpha=0.22); ax[0,1].set_xlabel("Underlying"); ax[0,1].set_ylabel("Gamma")
    ax[0,1].annotate("peaks ATM —\nhedging risk highest", (100,g["gamma"].max()*0.6), fontsize=8, color=NAVY, ha="center")

    ax[1,0].plot(S, g["vega"], color=PURPLE, lw=2)
    ax[1,0].axvline(K, color=GREY, ls=":"); ax[1,0].set_title("Vega vs price (IV sensitivity)")
    ax[1,0].grid(alpha=0.22); ax[1,0].set_xlabel("Underlying"); ax[1,0].set_ylabel("Vega ($/1% IV)")
    ax[1,0].annotate("peaks ATM —\nmost exposed to IV", (100,g["vega"].max()*0.55), fontsize=8, color=PURPLE, ha="center")

    ax[1,1].plot(S, g["call_theta"], color=RED, lw=2)
    ax[1,1].axvline(K, color=GREY, ls=":"); ax[1,1].axhline(0, color=GREY, lw=0.8)
    ax[1,1].set_title("Theta vs price (time decay/day)")
    ax[1,1].grid(alpha=0.22); ax[1,1].set_xlabel("Underlying"); ax[1,1].set_ylabel("Theta ($/day)")
    ax[1,1].annotate("most negative ATM —\ndecay worst for buyers", (100,g["call_theta"].min()*0.6), fontsize=8, color=RED, ha="center")
    save(fig, "ex_greeks_price.png")


# ============ FIGURE 6: theta decay over time + value vs IV ============
def fig_greeks_time_iv():
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    # ATM call value vs days to expiry (decay accelerates)
    days = np.linspace(90, 0.5, 200); K,r,sig,S0 = 100,0.04,0.25,100
    T = days/365
    d1 = (np.log(S0/K)+(r+sig**2/2)*T)/(sig*np.sqrt(T)); d2 = d1-sig*np.sqrt(T)
    val = S0*N(d1)-K*np.exp(-r*T)*N(d2)
    ax[0].plot(days, val, color=RED, lw=2)
    ax[0].invert_xaxis(); ax[0].set_title("Time decay: ATM call value vs days left")
    ax[0].set_xlabel("Days to expiry (-> 0)"); ax[0].set_ylabel("Option value"); ax[0].grid(alpha=0.22)
    ax[0].annotate("decay ACCELERATES\nin final weeks", (20, val[np.argmin(abs(days-20))]+1.2), fontsize=8.5, color=RED, ha="center")

    # value vs IV (linear-ish via vega) at fixed T
    iv = np.linspace(0.10, 0.60, 200); T2 = 0.25
    d1b = (np.log(S0/K)+(r+iv**2/2)*T2)/(iv*np.sqrt(T2)); d2b = d1b-iv*np.sqrt(T2)
    valb = S0*N(d1b)-K*np.exp(-r*T2)*N(d2b)
    ax[1].plot(iv*100, valb, color=PURPLE, lw=2)
    ax[1].set_title("IV sensitivity: ATM call value vs implied vol")
    ax[1].set_xlabel("Implied volatility (%)"); ax[1].set_ylabel("Option value"); ax[1].grid(alpha=0.22)
    ax[1].annotate("higher IV -> richer premium\n(why you sell high IV Rank)", (40, valb[np.argmin(abs(iv-0.40))]-2.5), fontsize=8.5, color=PURPLE, ha="center", va="top")
    save(fig, "ex_greeks_time_iv.png")


if __name__ == "__main__":
    fig_single_legs()
    fig_stock_combos()
    fig_verticals()
    fig_vol_strategies()
    fig_greeks_price()
    fig_greeks_time_iv()
    print("done")
