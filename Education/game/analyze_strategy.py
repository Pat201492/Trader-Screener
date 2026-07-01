"""
Sanity-check that following the SUGGESTED MOVE pays, under the weekly model.

Method: many independent samples. Each sample: a fresh commodity state + a random
catalyst -> apply the recipe (recommend) -> open an 8-week position -> inject the
news bump week 1, advance 8 weeks -> settle at intrinsic. Report win/flat/loss
shape and mean P/L, split by buy vs sell.

Run:  python analyze_strategy.py
"""
import copy
import math
import random
import statistics as st
import catan_options_signals as g

U = g.UNITS
T = 8 / 52.0
STEPS = max(1, round(T * g.SPY))

def decide(catalyst, com):
    _, _, cdir, _ = catalyst
    r = com.iv_rank()
    if cdir == "none":
        return ("sell_csp", com) if r >= 66 else None
    if cdir == "bull":
        if r <= 40: return ("buy_call", com)
        if r >= 66: return ("sell_csp", com)
        return ("bull_put", com)
    else:
        if r <= 40: return ("buy_put", com)
        return ("bear_call", com)

def build(action, com):
    S = com.price()
    F, sig_true = g.forward_stats(com, T, sims=100)
    sig = sig_true * g.iv_markup(com.iv_rank())
    P = lambda k, K: g.black_premium(k, F, K, sig, T)
    if action == "buy_call":
        K = round(S); return (-P("call", K) * U, [("call", K, +1)])
    if action == "buy_put":
        K = round(S); return (-P("put", K) * U, [("put", K, +1)])
    if action == "sell_csp":
        K = max(1, round(S * 0.95)); return (+P("put", K) * U, [("put", K, -1)])
    if action == "bull_put":
        Kh, Kl = round(S * 0.97), round(S * 0.90)
        if Kl >= Kh: Kl = Kh - 1
        c = max(P("put", Kh) - P("put", Kl), 0.05)
        return (+c * U, [("put", Kh, -1), ("put", Kl, +1)])
    Kl, Kh = round(S * 1.03), round(S * 1.10)
    if Kh <= Kl: Kh = Kl + 1
    c = max(P("call", Kl) - P("call", Kh), 0.05)
    return (+c * U, [("call", Kl, -1), ("call", Kh, +1)])

def settle_pl(entry, legs, S):
    add = sum(sign * (max(S - K, 0) if k == "call" else max(K - S, 0)) * U for k, K, sign in legs)
    return entry + add

def run(samples=2500):
    random.seed(7)
    per = {"buy": [], "sell": []}
    for _ in range(samples):
        name = random.choice(list(g.COMMODITIES))
        com = g.Commodity(name, dict(g.COMMODITIES[name]))
        # advance a random number of weeks so states vary
        for _s in range(random.randint(0, 20)):
            com.advance(0.0, market=random.gauss(0, 1))
        cat = random.choice(g.CATALYSTS)
        _, _, cdir, strength = cat
        dec = decide(cat, com)
        if not dec:
            continue
        action, _c = dec
        entry, legs = build(action, com)
        side = "buy" if action.startswith("buy") else "sell"
        priced_in = com.iv_rank() >= 66 or random.random() < 0.25
        bump = 0.0
        if cdir != "none":
            bump = (1 if cdir == "bull" else -1) * (strength / 12) * (0.3 if priced_in else 1.0)
        com.advance(bump, market=random.gauss(0, 1))
        for _s in range(STEPS - 1):
            com.advance(0.0, market=random.gauss(0, 1))
        per[side].append(settle_pl(entry, legs, com.price()))

    def shape(pls):
        n = len(pls)
        if not n: return "no trades"
        w = sum(1 for x in pls if x > 0.5); f = sum(1 for x in pls if -0.5 <= x <= 0.5)
        l = sum(1 for x in pls if x < -0.5)
        return (f"n={n}  mean={st.mean(pls):+.2f}  win={100*w/n:.0f}%  "
                f"flat={100*f/n:.0f}%  loss={100*l/n:.0f}%  best={max(pls):+.1f} worst={min(pls):+.1f}")

    print("=" * 70)
    print(f" FOLLOWING THE SUGGESTED MOVE (8-week holds, {samples} samples, weekly model)")
    print("=" * 70)
    print(" BUY  : " + shape(per["buy"]))
    print(" SELL : " + shape(per["sell"]))
    allpl = per["buy"] + per["sell"]
    print(" ALL  : " + shape(allpl))
    if allpl:
        print(f" mean P/L per trade = {st.mean(allpl):+.2f}  ->  "
              f"{'EDGE CONFIRMED (+EV)' if st.mean(allpl) > 0 else 'NOT +EV'}")
    print("=" * 70)

if __name__ == "__main__":
    run()
