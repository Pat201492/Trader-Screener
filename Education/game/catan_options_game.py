#!/usr/bin/env python3
"""
CATAN OPTIONS — a tiny terminal game that teaches options trading.

You trade calls and puts on the five Settlers-of-Catan commodities:
    Brick, Lumber, Wool, Grain, Ore
Each has a different volatility, so option premiums differ — that teaches you
implied volatility without the jargon. Buy options, let a season pass, prices
move, and your options settle. Grow your gold over ~10 seasons.

No dependencies. Run:  python catan_options_game.py
Educational only — not investment advice.
"""
import math
import random
import sys
import textwrap

# Windows consoles default to cp1252; force UTF-8 so any non-ASCII prints cleanly.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ----- standard normal CDF (no scipy) -----
def N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

# ----- the five commodities: start price (gold) + annual volatility -----
COMMODITIES = {
    "Brick":  {"price": 10.0, "vol": 0.45},
    "Lumber": {"price": 9.0,  "vol": 0.35},
    "Wool":   {"price": 8.0,  "vol": 0.30},
    "Grain":  {"price": 7.0,  "vol": 0.25},
    "Ore":    {"price": 14.0, "vol": 0.40},
}

T = 0.25            # one season = quarter-year (time to expiry)
UNITS = 10          # one contract covers 10 units
START_GOLD = 100.0
SEASONS = 10

def wrap(s, indent=""):
    print(textwrap.fill(s, width=78, initial_indent=indent, subsequent_indent=indent))

def bs_premium(kind, S, K, sigma, T):
    """Black-Scholes premium per unit, r=0."""
    if T <= 0:
        intrinsic = max(S - K, 0) if kind == "call" else max(K - S, 0)
        return round(intrinsic, 2)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "call":
        val = S * N(d1) - K * N(d2)
    else:
        val = K * N(-d2) - S * N(-d1)
    return max(round(val, 2), 0.05)

def step_price(S, sigma):
    """Lognormal one-season move."""
    z = random.gauss(0, 1)
    return round(S * math.exp(-0.5 * sigma**2 * T + sigma * math.sqrt(T) * z), 2)

def ask(prompt, options):
    """Ask until the user picks a valid option key (case-insensitive)."""
    options = {str(k).lower(): v for k, v in options.items()}
    while True:
        choice = input(prompt).strip().lower()
        if choice in options:
            return options[choice]
        print("   (type one of: %s)" % ", ".join(options))

def strikes_for(S):
    """Three integer strikes: ~OTM-low, ATM, ~OTM-high."""
    lo = max(1, round(S * 0.9))
    mid = round(S)
    hi = round(S * 1.1)
    return sorted({lo, mid, hi})

# ---------------------------------------------------------------- tutorial
def intro():
    print("\n" + "=" * 60)
    print("        CATAN OPTIONS  —  learn to trade options")
    print("=" * 60)
    wrap("You start with %d gold. Each season you may BUY an option on a "
         "commodity, then time passes and prices move." % START_GOLD)
    print()
    wrap("CALL  = the right to BUY at the strike. You profit if the price "
         "rises ABOVE the strike. Buy a call when you think a commodity will go UP.")
    wrap("PUT   = the right to SELL at the strike. You profit if the price "
         "falls BELOW the strike. Buy a put when you think it will go DOWN.")
    print()
    wrap("You pay a PREMIUM up front. That premium is the MOST you can lose "
         "(defined risk) — if the option expires worthless, you just lose the premium.")
    wrap("PROFIT at expiry = payoff - premium paid.  "
         "BREAKEVEN: a call needs price > strike + premium; a put needs price < strike - premium.")
    print()
    wrap("Notice premiums differ by commodity: Brick & Ore swing more "
         "(high volatility) so their options cost MORE. That extra cost IS "
         "implied volatility — you pay more when a bigger move is expected.")
    print("-" * 60)

# ---------------------------------------------------------------- one season
def show_board(prices, season, gold):
    print("\n" + "-" * 60)
    print(f"SEASON {season}/{SEASONS}     Gold: {gold:.2f}")
    print("-" * 60)
    print(f"{'Commodity':<10}{'Price':>8}{'Volatility':>14}")
    for name, p in prices.items():
        vol = COMMODITIES[name]["vol"]
        print(f"{name:<10}{p:>8.2f}{vol*100:>12.0f}%")

def buy_phase(prices, gold):
    do = ask("\nBuy an option this season? (y/n): ", {"y": True, "n": False})
    if not do:
        return None, gold

    # pick commodity
    names = list(prices.keys())
    menu = {str(i + 1): n for i, n in enumerate(names)}
    for k, n in menu.items():
        print(f"   {k}) {n}  @ {prices[n]:.2f}")
    name = ask("Pick a commodity (number): ", menu)
    S = prices[name]; sigma = COMMODITIES[name]["vol"]

    kind = ask("Call (up) or Put (down)? (c/p): ", {"c": "call", "p": "put"})

    # show strike chain with premiums
    ks = strikes_for(S)
    print(f"\n   {name} @ {S:.2f}   {kind.upper()} premiums (per unit):")
    chain = {}
    for i, K in enumerate(ks, 1):
        prem = bs_premium(kind, S, K, sigma, T)
        moneyness = "ITM" if ((kind == "call" and S > K) or (kind == "put" and S < K)) else \
                    ("ATM" if K == round(S) else "OTM")
        chain[str(i)] = (K, prem)
        print(f"   {i}) strike {K:<4}  premium {prem:>5.2f}/unit  "
              f"contract {prem*UNITS:>6.2f}  [{moneyness}]")
    K, prem = ask("Pick a strike (number): ", chain)

    cost = prem * UNITS
    if cost > gold:
        print(f"   Not enough gold ({gold:.2f}) for {cost:.2f}. Skipping.")
        return None, gold

    be = K + prem if kind == "call" else K - prem
    gold -= cost
    print(f"\n   BOUGHT {kind} on {name}: strike {K}, paid {cost:.2f} "
          f"({UNITS} units). Breakeven price = {be:.2f}.")
    wrap("Max loss on this position = the %.2f premium. You profit if %s." % (
        cost, f"price ends ABOVE {be:.2f}" if kind == "call" else f"price ends BELOW {be:.2f}"),
        indent="   ")
    pos = {"name": name, "kind": kind, "K": K, "prem": prem, "cost": cost, "be": be}
    return pos, gold

def settle(pos, new_price, gold):
    S = new_price; K = pos["K"]
    if pos["kind"] == "call":
        payoff = max(S - K, 0) * UNITS
    else:
        payoff = max(K - S, 0) * UNITS
    pl = payoff - pos["cost"]
    gold += payoff
    print(f"\n   SETTLE {pos['kind']} on {pos['name']}: "
          f"price moved to {S:.2f}, strike {K}.")
    if payoff == 0:
        print(f"   Expired worthless. Lost the {pos['cost']:.2f} premium "
              f"(that was the max loss — defined risk).")
    else:
        print(f"   Payoff {payoff:.2f}  -  premium {pos['cost']:.2f}  =  "
              f"P/L {pl:+.2f}.")
        if pl < 0:
            print(f"   In-the-money, but not past breakeven {pos['be']:.2f} — "
                  f"the move didn't cover the premium.")
    return gold

# ---------------------------------------------------------------- main loop
def play():
    random.seed()  # real randomness for play
    intro()
    prices = {n: d["price"] for n, d in COMMODITIES.items()}
    gold = START_GOLD

    for season in range(1, SEASONS + 1):
        show_board(prices, season, gold)
        pos, gold = buy_phase(prices, gold)

        input("\n(press Enter to let the season pass...)")
        # move all prices
        prices = {n: step_price(p, COMMODITIES[n]["vol"]) for n, p in prices.items()}

        if pos:
            gold = settle(pos, prices[pos["name"]], gold)
        print(f"\n   Gold now: {gold:.2f}")
        if gold < 1:
            print("\n   Out of gold. The trading post closes its doors. Game over.")
            break

    print("\n" + "=" * 60)
    net = gold - START_GOLD
    print(f"FINAL GOLD: {gold:.2f}   (started {START_GOLD:.0f}, net {net:+.2f})")
    if net > 0:
        print("You grew the treasury. Longest road to riches!")
    elif net == 0:
        print("Broke even — the house edge (premium) is real.")
    else:
        print("The premiums bled you. Lesson: buying options needs a move "
              "bigger than the premium to win.")
    print("=" * 60)
    print("\nWhat you just practiced: call vs put, strike & moneyness (ITM/ATM/OTM),")
    print("premium as max loss, breakeven, and why high-vol commodities cost more (IV).")

if __name__ == "__main__":
    try:
        play()
    except (KeyboardInterrupt, EOFError):
        print("\nLeft the trading post. Farewell.")
