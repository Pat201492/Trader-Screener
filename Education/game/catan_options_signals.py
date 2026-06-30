#!/usr/bin/env python3
"""
CATAN OPTIONS — SIGNALS EDITION

The plain game (catan_options_game.py) used a pure random walk: with no
information, every trade is a coin flip and you just bleed premium. That is
the point — trading with no edge is gambling.

This edition gives you DATA to read. Each season the herald announces market
news (a catalyst) that biases a commodity's coming move, and each option is
tagged IV: CHEAP or IV: RICH. Your edge is reading the signal and the IV:
  - Bullish catalyst + CHEAP IV  -> buy a call (cheap exposure to a likely up-move)
  - Bearish catalyst + CHEAP IV  -> buy a put
  - News already priced in (RICH IV) -> the edge is gone; the rich premium
    usually eats your profit even if direction is right.

Skill should now beat luck. The end screen scores how well you traded WITH the
signal. Pure stdlib. Run:  python catan_options_signals.py
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

COMMODITIES = {
    "Brick":  {"price": 10.0, "vol": 0.45},
    "Lumber": {"price": 9.0,  "vol": 0.35},
    "Wool":   {"price": 8.0,  "vol": 0.30},
    "Grain":  {"price": 7.0,  "vol": 0.25},
    "Ore":    {"price": 14.0, "vol": 0.40},
}

# Catalysts: (headline, commodity, direction, strength). Strength = annualized
# drift the news adds to that commodity's next move. A few are red herrings
# (weak/none) so the signal is an EDGE, not a guarantee.
CATALYSTS = [
    ("Building boom across the island — settlers demand BRICK", "Brick", "bull", 1.0),
    ("Kilns flooded — BRICK output collapses, scarcity bites",  "Brick", "bull", 0.9),
    ("New brickworks opens — BRICK glut floods the market",     "Brick", "bear", 1.0),
    ("Wildfire razes the forests — LUMBER scarce",              "Lumber", "bull", 0.9),
    ("Bumper logging season — LUMBER piles up unsold",          "Lumber", "bear", 0.9),
    ("Sheep blight spreads — WOOL supply dwindles",             "Wool", "bull", 0.8),
    ("Mild winter — herds thrive, WOOL everywhere",             "Wool", "bear", 0.7),
    ("Drought ruins the harvest — GRAIN scarce",                "Grain", "bull", 0.8),
    ("Record harvest — GRAIN silos overflow",                   "Grain", "bear", 0.8),
    ("Rich vein struck in the hills — ORE floods in",           "Ore", "bear", 1.0),
    ("Mine cave-in halts digging — ORE supply chokes",          "Ore", "bull", 1.0),
    ("Traders gossip, but nothing moves the market this season","None", "none", 0.0),
    ("A quiet season on the island — no clear catalyst",        "None", "none", 0.0),
]

T = 0.25
UNITS = 10
START_GOLD = 100.0
SEASONS = 10

def wrap(s, indent=""):
    print(textwrap.fill(s, width=78, initial_indent=indent, subsequent_indent=indent))

def bs_premium(kind, S, K, sigma, T, iv_mult=1.0):
    sigma = sigma * iv_mult
    if T <= 0:
        return round(max(S - K, 0) if kind == "call" else max(K - S, 0), 2)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    val = (S * Ncdf(d1) - K * Ncdf(d2)) if kind == "call" else (K * Ncdf(-d2) - S * Ncdf(-d1))
    return max(round(val, 2), 0.05)

def step_price(S, sigma, drift):
    z = random.gauss(0, 1)
    return round(S * math.exp((drift - 0.5 * sigma**2) * T + sigma * math.sqrt(T) * z), 2)

def ask(prompt, options):
    options = {str(k).lower(): v for k, v in options.items()}
    while True:
        c = input(prompt).strip().lower()
        if c in options:
            return options[c]
        print("   (type one of: %s)" % ", ".join(options))

def strikes_for(S):
    return sorted({max(1, round(S * 0.9)), round(S), round(S * 1.1)})

def intro():
    print("\n" + "=" * 62)
    print("     CATAN OPTIONS — SIGNALS EDITION   (read the data, get an edge)")
    print("=" * 62)
    wrap("Plain random prices = gambling. Here you get DATA each season:")
    wrap("1) MARKET NEWS — a catalyst that biases one commodity up or down.", "  ")
    wrap("2) An IV tag on each option — CHEAP (move not yet priced in) or "
         "RICH (already priced; premium inflated).", "  ")
    print()
    wrap("Your edge: trade WITH a catalyst when IV is CHEAP. Bullish news -> "
         "call; bearish news -> put. Avoid RICH IV — the inflated premium "
         "usually eats the profit even when you're right on direction.")
    wrap("Some seasons have no real catalyst (a red herring). Sitting out is a "
         "valid move — no edge, no trade.")
    print("-" * 62)

def show_board(prices, season, gold):
    print("\n" + "-" * 62)
    print(f"SEASON {season}/{SEASONS}     Gold: {gold:.2f}")
    print("-" * 62)
    print(f"{'Commodity':<10}{'Price':>8}{'Volatility':>14}")
    for name, p in prices.items():
        print(f"{name:<10}{p:>8.2f}{COMMODITIES[name]['vol']*100:>12.0f}%")

def buy_phase(prices, gold, catalyst, priced_in):
    head, ccom, cdir, _ = catalyst
    print("\n  MARKET NEWS:  " + head)
    if cdir != "none":
        tag = "RICH (already priced in)" if priced_in else "CHEAP (not yet priced in)"
        hint = "call" if cdir == "bull" else "put"
        print(f"  -> affects {ccom} ({cdir}ish).  IV on {ccom}: {tag}.")
        print(f"     Textbook play: {cdir}ish + {'CHEAP' if not priced_in else 'RICH'} IV "
              f"-> {'buy a ' + hint if not priced_in else 'avoid — premium too rich'}.")
    else:
        print("  -> no clear catalyst. No edge this season; sitting out is fine.")

    if not ask("\nBuy an option? (y/n): ", {"y": True, "n": False}):
        return None, gold

    names = list(prices.keys())
    menu = {str(i+1): n for i, n in enumerate(names)}
    for k, n in menu.items():
        flag = "   <- in the news" if n == ccom and cdir != "none" else ""
        print(f"   {k}) {n}  @ {prices[n]:.2f}{flag}")
    name = ask("Pick a commodity (number): ", menu)
    S = prices[name]; sigma = COMMODITIES[name]["vol"]
    kind = ask("Call (up) or Put (down)? (c/p): ", {"c": "call", "p": "put"})

    iv_mult = 1.5 if (name == ccom and priced_in) else 1.0
    ks = strikes_for(S); chain = {}
    iv_label = "RICH" if iv_mult > 1 else "fair"
    print(f"\n   {name} @ {S:.2f}   {kind.upper()} premiums   [IV: {iv_label}]")
    for i, K in enumerate(ks, 1):
        prem = bs_premium(kind, S, K, sigma, T, iv_mult)
        mny = "ITM" if ((kind=="call" and S>K) or (kind=="put" and S<K)) else ("ATM" if K==round(S) else "OTM")
        chain[str(i)] = (K, prem)
        print(f"   {i}) strike {K:<4} premium {prem:>5.2f}/u  contract {prem*UNITS:>6.2f}  [{mny}]")
    K, prem = ask("Pick a strike (number): ", chain)

    cost = prem * UNITS
    if cost > gold:
        print(f"   Not enough gold for {cost:.2f}. Skipping.")
        return None, gold
    be = K + prem if kind == "call" else K - prem
    gold -= cost
    # was this a "good" trade (with signal, cheap IV, right direction)?
    aligned = (name == ccom and cdir != "none" and
               ((cdir == "bull" and kind == "call") or (cdir == "bear" and kind == "put")))
    good = aligned and not priced_in
    print(f"\n   BOUGHT {kind} on {name} @ {K}, paid {cost:.2f}. Breakeven {be:.2f}.")
    pos = {"name": name, "kind": kind, "K": K, "cost": cost, "be": be,
           "aligned": aligned, "good": good, "priced_in": priced_in and name == ccom}
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
    # teaching feedback tied to the signal
    if pos["good"]:
        scored["with_signal"] += 1
        scored["with_signal_pl"] += pl
        print("   (Traded WITH the catalyst at cheap IV — the edge play.)")
    elif pos["priced_in"]:
        print("   (You paid RICH IV — note how the inflated premium hurt the result.)")
    elif not pos["aligned"]:
        print("   (No signal behind this trade — that's a coin flip.)")
    return gold

def play():
    random.seed()
    intro()
    prices = {n: d["price"] for n, d in COMMODITIES.items()}
    gold = START_GOLD
    scored = {"with_signal": 0, "with_signal_pl": 0.0}

    for season in range(1, SEASONS + 1):
        show_board(prices, season, gold)
        catalyst = random.choice(CATALYSTS)
        priced_in = (catalyst[2] != "none" and random.random() < 0.30)  # 30% already priced
        pos, gold = buy_phase(prices, gold, catalyst, priced_in)

        input("\n(press Enter to let the season pass...)")
        _, ccom, cdir, strength = catalyst
        drift_map = {}
        if cdir == "bull":
            drift_map[ccom] = strength * (0.3 if priced_in else 1.0)
        elif cdir == "bear":
            drift_map[ccom] = -strength * (0.3 if priced_in else 1.0)
        prices = {n: step_price(p, COMMODITIES[n]["vol"], drift_map.get(n, 0.0))
                  for n, p in prices.items()}
        if pos:
            gold = settle(pos, prices[pos["name"]], gold, scored)
        print(f"\n   Gold now: {gold:.2f}")
        if gold < 1:
            print("\n   Out of gold. Game over."); break

    print("\n" + "=" * 62)
    net = gold - START_GOLD
    print(f"FINAL GOLD: {gold:.2f}   (started {START_GOLD:.0f}, net {net:+.2f})")
    if scored["with_signal"]:
        print(f"Edge trades (with catalyst, cheap IV): {scored['with_signal']}  "
              f"net P/L {scored['with_signal_pl']:+.2f}")
        wrap("Compare that to your coin-flip / rich-IV trades. The lesson: an "
             "information edge — reading the catalyst and the IV — is what "
             "separates trading from gambling.")
    else:
        wrap("You made no edge trades. With no signal behind a position, this "
             "is the same coin flip as the plain game — that's the point.")
    print("=" * 62)
    print("\nReal-world parallel: the catalyst = news/earnings/supply data; the IV "
          "tag = IV Rank. The Trader Screener's job is to surface exactly these "
          "signals so your decisions have an edge.")

if __name__ == "__main__":
    try:
        play()
    except (KeyboardInterrupt, EOFError):
        print("\nLeft the trading post. Farewell.")
