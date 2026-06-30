#!/usr/bin/env python3
"""
CATAN OPTIONS — SIGNALS EDITION (with real-world-shaped data & history)

Each commodity is modeled after a real-world analog, so its 5-year history has
a characteristic SHAPE (not real prices — the shape):
    Grain  ~ agricultural grain : strong annual SEASONALITY, mean-reverting
    Lumber ~ lumber             : big cyclical BOOM/BUST, momentum, high vol
    Brick  ~ construction block : steady up-trend, low vol
    Wool   ~ soft commodity     : mild seasonality, slow trends
    Ore    ~ industrial metal   : cyclical, trending, high vol, supply shocks

You read each commodity's 5y sparkline, realized volatility, and IV Rank
(derived from its own history), plus a market-news catalyst, then trade
calls/puts. Time advances one MONTH per turn with real dates. Pricing uses each
commodity's realized vol, so IV is genuine — RICH IV options really do cost more.

Pure stdlib. Run:  python catan_options_signals.py
Educational only — not investment advice.
"""
import math
import random
import sys
import textwrap

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

def Ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

# ---- commodity models: real-world-shaped parameters (monthly) ----
# drift: annual log-trend | seas_amp/phase: annual seasonality | rho: cyclical
# persistence (momentum vs mean-reversion) | vol_m: base monthly vol | jump_p/size:
# shock probability/magnitude | analog: the real instrument it mimics.
COMMODITIES = {
    "Grain":  dict(start=7.0,  drift=0.015, seas_amp=0.13, phase=1.6, rho=0.70,
                   vol_m=0.060, jump_p=0.04, jump=0.14, analog="agricultural grain (seasonal)"),
    "Lumber": dict(start=9.0,  drift=0.020, seas_amp=0.03, phase=0.0, rho=0.93,
                   vol_m=0.110, jump_p=0.05, jump=0.20, analog="lumber (boom/bust cyclical)"),
    "Brick":  dict(start=10.0, drift=0.035, seas_amp=0.02, phase=0.0, rho=0.85,
                   vol_m=0.050, jump_p=0.02, jump=0.10, analog="construction block (steady trend)"),
    "Wool":   dict(start=8.0,  drift=0.005, seas_amp=0.07, phase=2.4, rho=0.80,
                   vol_m=0.080, jump_p=0.03, jump=0.12, analog="soft commodity (mild seasonal)"),
    "Ore":    dict(start=14.0, drift=0.012, seas_amp=0.02, phase=0.0, rho=0.90,
                   vol_m=0.110, jump_p=0.05, jump=0.22, analog="industrial metal (cyclical)"),
}

CATALYSTS = [
    ("Building boom across the island — settlers demand BRICK", "Brick", "bull", 1.0),
    ("New brickworks opens — BRICK glut floods the market",     "Brick", "bear", 1.0),
    ("Wildfire razes the forests — LUMBER scarce",              "Lumber", "bull", 1.1),
    ("Bumper logging season — LUMBER piles up unsold",          "Lumber", "bear", 1.0),
    ("Sheep blight spreads — WOOL supply dwindles",             "Wool", "bull", 0.8),
    ("Mild winter — herds thrive, WOOL everywhere",             "Wool", "bear", 0.7),
    ("Drought ruins the harvest — GRAIN scarce",                "Grain", "bull", 0.9),
    ("Record harvest — GRAIN silos overflow",                   "Grain", "bear", 0.9),
    ("Rich vein struck in the hills — ORE floods in",           "Ore", "bear", 1.1),
    ("Mine cave-in halts digging — ORE supply chokes",          "Ore", "bull", 1.1),
    ("Traders gossip, but nothing moves the market",            "None", "none", 0.0),
    ("A quiet month on the island — no clear catalyst",         "None", "none", 0.0),
]

HIST_MONTHS = 60          # 5 years of history
PLAY_MONTHS = 12          # play one year forward
START_YEAR = 2021         # history begins here; play starts at +5y
UNITS = 10
START_GOLD = 100.0
SPARK = "▁▂▃▄▅▆▇█"

def wrap(s, indent=""):
    print(textwrap.fill(s, width=80, initial_indent=indent, subsequent_indent=indent))

def month_label(idx):
    """idx 0 = START_YEAR-01; returns 'YYYY-MM'."""
    y = START_YEAR + idx // 12
    m = idx % 12 + 1
    return f"{y}-{m:02d}"

def sparkline(vals, width=28):
    if len(vals) > width:                      # downsample
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1.0
    return "".join(SPARK[min(7, int((v - lo) / rng * 7.999))] for v in vals)

def annualized_vol(logrets):
    if len(logrets) < 2:
        return 0.0
    m = sum(logrets) / len(logrets)
    var = sum((r - m) ** 2 for r in logrets) / (len(logrets) - 1)
    return math.sqrt(var) * math.sqrt(12)

# ---------------------------------------------------------------- simulation
class Commodity:
    def __init__(self, name, p):
        self.name = name; self.p = p
        self.prices = []         # full price series (history + played)
        self.logrets = []        # log returns
        self.c = 0.0; self.h = 0.0; self.t = 0   # cyclical, vol-state, time index
        self._rng = random.Random(hash(name) & 0xFFFF)  # FIXED -> stable shape
        self._build_history()
        self._iv_distribution()

    def _emit(self, rng, catalyst_drift=0.0):
        pp = self.p
        self.h = 0.9 * self.h + 0.30 * rng.gauss(0, 1)
        sigma = pp["vol_m"] * math.exp(0.5 * self.h)
        jump = 0.0
        if rng.random() < pp["jump_p"]:
            jump = pp["jump"] * (1 if rng.random() < 0.5 else -1)
        self.c = pp["rho"] * self.c + sigma * rng.gauss(0, 1) + jump + catalyst_drift
        self.t += 1
        month = self.t % 12
        seas = pp["seas_amp"] * math.sin(2 * math.pi * month / 12 + pp["phase"])
        logP = math.log(pp["start"]) + (pp["drift"] / 12) * self.t + seas + self.c
        price = round(math.exp(logP), 2)
        if self.prices:
            self.logrets.append(math.log(price / self.prices[-1]))
        self.prices.append(price)
        return price

    def _build_history(self):
        for _ in range(HIST_MONTHS):
            self._emit(self._rng)

    def _iv_distribution(self):
        """Distribution of trailing-3-month realized vol across history (for IV Rank)."""
        self.iv_hist = []
        for i in range(3, len(self.logrets) + 1):
            self.iv_hist.append(annualized_vol(self.logrets[i - 3:i]))
        self.iv_hist.sort()

    # ---- live readings ----
    def price(self):           return self.prices[-1]
    def realized_vol(self):    return annualized_vol(self.logrets[-12:])
    def current_3m_vol(self):  return annualized_vol(self.logrets[-3:])

    def iv_rank(self):
        v = self.current_3m_vol()
        if not self.iv_hist:
            return 50
        below = sum(1 for x in self.iv_hist if x <= v)
        return round(100 * below / len(self.iv_hist))

    def trend(self):
        if len(self.prices) < 7:
            return "→"
        chg = self.prices[-1] / self.prices[-7] - 1
        return "↑" if chg > 0.04 else ("↓" if chg < -0.04 else "→")

    def lo_hi(self, n):
        w = self.prices[-n:]
        return min(w), max(w)

    def advance(self, catalyst_drift):
        return self._emit(random, catalyst_drift)   # forward uses global RNG

# ---------------------------------------------------------------- pricing
def bs_premium(kind, S, K, sigma, T):
    sigma = max(sigma, 0.05)
    if T <= 0:
        return round(max(S - K, 0) if kind == "call" else max(K - S, 0), 2)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    val = (S * Ncdf(d1) - K * Ncdf(d2)) if kind == "call" else (K * Ncdf(-d2) - S * Ncdf(-d1))
    return max(round(val, 2), 0.05)

def strikes_for(S):
    return sorted({max(1, round(S * 0.92)), round(S), round(S * 1.08)})

def ask(prompt, options):
    options = {str(k).lower(): v for k, v in options.items()}
    while True:
        c = input(prompt).strip().lower()
        if c in options:
            return options[c]
        print("   (type one of: %s)" % ", ".join(options))

# ---------------------------------------------------------------- UI
REF = ("[ CALL = right to BUY @ strike  -> profit if price ends ABOVE strike (bet UP) | "
       "PUT = right to SELL @ strike -> profit if price ends BELOW strike (bet DOWN) | "
       "premium = max loss ]")

def intro(coms):
    print("\n" + "=" * 84)
    print("        CATAN OPTIONS — SIGNALS EDITION  (read the data, get an edge)")
    print("=" * 84)
    print(REF)
    print("-" * 84)
    wrap("CALL: you pay a premium for the right to buy at the strike. If the price rises "
         "above strike+premium you profit; the most you can lose is the premium.")
    wrap("PUT:  you pay a premium for the right to sell at the strike. If the price falls "
         "below strike-premium you profit; the most you can lose is the premium.")
    print()
    wrap("Each turn = ONE MONTH (real dates advance). You see 5 years of history per "
         "commodity (a sparkline), its realized volatility, and its IV RANK (how high its "
         "vol is vs its own history). CHEAP IV (low rank) = options underpriced; RICH IV "
         "(high rank) = options expensive and the move is likely already priced in.")
    wrap("EDGE: trade WITH a catalyst when IV is CHEAP (bullish->call, bearish->put). "
         "Avoid RICH IV. No catalyst = no edge = sit out.")
    print("\nThe five commodities (each shaped like a real-world analog):")
    for n, c in coms.items():
        print(f"   {n:<7} ~ {c.p['analog']}")
    print("-" * 84)

def iv_tag(rank):
    if rank >= 66: return f"RICH({rank})"
    if rank <= 40: return f"CHEAP({rank})"
    return f"FAIR({rank})"

def show_board(coms, month_idx, turn, gold):
    print("\n" + "-" * 84)
    print(f"{month_label(month_idx)}   (month {turn}/{PLAY_MONTHS})        Gold: {gold:.2f}")
    print(REF)
    print("-" * 84)
    print(f"{'Commodity':<9}{'Price':>7}  {'5y history':<30}{'5y range':>14}  "
          f"{'rVol':>5}  {'IV Rank':<10}{'trend':>5}")
    for n, c in coms.items():
        lo, hi = c.lo_hi(HIST_MONTHS)
        spark = sparkline(c.prices[-HIST_MONTHS:])
        print(f"{n:<9}{c.price():>7.2f}  {spark:<30}{f'{lo:.1f}-{hi:.1f}':>14}  "
              f"{c.realized_vol()*100:>4.0f}%  {iv_tag(c.iv_rank()):<10}{c.trend():>5}")

def buy_phase(coms, gold, catalyst, priced_in):
    head, ccom, cdir, _ = catalyst
    print("\n  MARKET NEWS:  " + head)
    if cdir != "none":
        rank = coms[ccom].iv_rank()
        note = "RICH (likely already priced in)" if priced_in else "CHEAP/FAIR (room to run)"
        hint = "call" if cdir == "bull" else "put"
        print(f"  -> {ccom} ({cdir}ish).  Its IV Rank = {rank} -> {note}.")
        print(f"     Textbook: {cdir}ish + {'CHEAP' if not priced_in else 'RICH'} IV -> "
              f"{'buy a ' + hint if not priced_in else 'AVOID (premium too rich, move priced in)'}.")
    else:
        print("  -> no clear catalyst; no edge this month. Sitting out is fine.")

    if not ask("\nBuy an option? (y/n): ", {"y": True, "n": False}):
        return None, gold

    names = list(coms.keys()); menu = {str(i+1): n for i, n in enumerate(names)}
    for k, n in menu.items():
        flag = "   <- in the news" if n == ccom and cdir != "none" else ""
        print(f"   {k}) {n}  @ {coms[n].price():.2f}   IV {iv_tag(coms[n].iv_rank())}{flag}")
    name = ask("Pick a commodity (number): ", menu)
    com = coms[name]; S = com.price()
    sigma = max(com.realized_vol(), 0.10)        # IV proxy = realized vol
    kind = ask("Call (up) or Put (down)? (c/p): ", {"c": "call", "p": "put"})

    T = 1 / 12                                   # 1-month option
    ks = strikes_for(S); chain = {}
    print(f"\n   {name} @ {S:.2f}   {kind.upper()}   IV {iv_tag(com.iv_rank())}  (vol {sigma*100:.0f}%)")
    for i, K in enumerate(ks, 1):
        prem = bs_premium(kind, S, K, sigma, T)
        mny = "ITM" if ((kind=="call" and S>K) or (kind=="put" and S<K)) else ("ATM" if K==round(S) else "OTM")
        chain[str(i)] = (K, prem)
        print(f"   {i}) strike {K:<4} premium {prem:>5.2f}/u  contract {prem*UNITS:>6.2f}  [{mny}]")
    K, prem = ask("Pick a strike (number): ", chain)

    cost = prem * UNITS
    if cost > gold:
        print(f"   Not enough gold for {cost:.2f}. Skipping."); return None, gold
    be = K + prem if kind == "call" else K - prem
    gold -= cost
    aligned = (name == ccom and cdir != "none" and
               ((cdir == "bull" and kind == "call") or (cdir == "bear" and kind == "put")))
    print(f"\n   BOUGHT {kind} on {name} @ {K}, paid {cost:.2f}. Breakeven {be:.2f}.")
    pos = dict(name=name, kind=kind, K=K, cost=cost, be=be,
               aligned=aligned, good=aligned and not priced_in,
               priced=priced_in and name == ccom)
    return pos, gold

def settle(pos, S, gold, scored):
    K = pos["K"]
    payoff = (max(S - K, 0) if pos["kind"] == "call" else max(K - S, 0)) * UNITS
    pl = payoff - pos["cost"]; gold += payoff
    print(f"\n   SETTLE {pos['kind']} on {pos['name']}: price -> {S:.2f}, strike {K}.")
    if payoff == 0:
        print(f"   Worthless. Lost the {pos['cost']:.2f} premium (max loss).")
    else:
        print(f"   Payoff {payoff:.2f} - premium {pos['cost']:.2f} = P/L {pl:+.2f}.")
    if pos["good"]:
        scored["n"] += 1; scored["pl"] += pl
        print("   (Edge play: with the catalyst at cheap/fair IV.)")
    elif pos["priced"]:
        print("   (Paid RICH IV — premium inflated and the move was largely priced in.)")
    elif not pos["aligned"]:
        print("   (No signal behind this — a coin flip.)")
    return gold

# ---------------------------------------------------------------- main
def play():
    random.seed()
    coms = {n: Commodity(n, dict(p)) for n, p in COMMODITIES.items()}
    intro(coms)
    gold = START_GOLD
    scored = {"n": 0, "pl": 0.0}
    month_idx = HIST_MONTHS            # first played month = right after history

    for turn in range(1, PLAY_MONTHS + 1):
        show_board(coms, month_idx, turn, gold)
        catalyst = random.choice(CATALYSTS)
        _, ccom, cdir, strength = catalyst
        priced_in = (cdir != "none" and (coms[ccom].iv_rank() >= 66 or random.random() < 0.25))
        pos, gold = buy_phase(coms, gold, catalyst, priced_in)

        input("\n(press Enter to advance one month...)")
        # forward step: catalyst adds monthly log-drift to its commodity (damped if priced in)
        for n, c in coms.items():
            d = 0.0
            if n == ccom and cdir != "none":
                sign = 1 if cdir == "bull" else -1
                d = sign * (strength / 12) * (0.3 if priced_in else 1.0)
            c.advance(d)
        month_idx += 1
        if pos:
            gold = settle(pos, coms[pos["name"]].price(), gold, scored)
        print(f"\n   Gold now: {gold:.2f}")
        if gold < 1:
            print("\n   Out of gold. Game over."); break

    print("\n" + "=" * 84)
    net = gold - START_GOLD
    print(f"FINAL GOLD: {gold:.2f}   (started {START_GOLD:.0f}, net {net:+.2f})")
    if scored["n"]:
        print(f"Edge trades (with catalyst, cheap/fair IV): {scored['n']}   net P/L {scored['pl']:+.2f}")
        wrap("That edge P/L is your skill; the rest is noise. Information — reading the "
             "history, the IV Rank, and the catalyst — is what separates trading from gambling.")
    else:
        wrap("No edge trades made. With no signal behind a position it's a coin flip — "
             "exactly the plain game's point.")
    print("=" * 84)
    print("Real parallel: history/sparkline = price chart; rVol & IV Rank = volatility "
          "signals; catalyst = news/earnings/supply data. The Trader Screener surfaces all "
          "of these so your decisions carry an edge.")

if __name__ == "__main__":
    try:
        play()
    except (KeyboardInterrupt, EOFError):
        print("\nLeft the trading post. Farewell.")
