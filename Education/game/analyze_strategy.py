"""
Auto-play the signals game following the SUGGESTED MOVE every turn, over many
games, and report what actually happens — so we can sanity-check that the advice
pays, and see the win/loss shape (is it mostly break-even? small wins? big losses?).

Run:  python analyze_strategy.py
"""
import math
import random
import statistics as st
import catan_options_signals as g

U = g.UNITS
T = 1.0 / 12

def decide(catalyst, coms):
    """Mirror recommend(): return (kind, name, structure) to execute, or None to sit out."""
    head, ccom, cdir, strength = catalyst
    if cdir == "none":
        rich = max(coms, key=lambda n: coms[n].iv_rank())
        if coms[rich].iv_rank() >= 66:
            return ("sell_csp", rich)
        return None
    r = coms[ccom].iv_rank()
    if cdir == "bull":
        if r <= 40: return ("buy_call", ccom)
        if r >= 66: return ("sell_csp", ccom)
        return ("bull_put", ccom)
    else:
        if r <= 40: return ("buy_put", ccom)
        if r >= 66: return ("bear_call", ccom)
        return ("bear_call", ccom)

def build(action, com):
    """Return (entry_cashflow, legs) where legs=[(kind,K,sign)]."""
    S = com.price()
    F, sig_true = g.forward_stats(com, sims=120)
    sig = sig_true * g.iv_markup(com.iv_rank())
    P = lambda k, K: g.black_premium(k, F, K, sig, T)
    a = action
    if a == "buy_call":
        K = round(S); p = P("call", K); return (-p*U, [("call", K, +1)])
    if a == "buy_put":
        K = round(S); p = P("put", K); return (-p*U, [("put", K, +1)])
    if a == "sell_csp":
        K = max(1, round(S*0.95)); p = P("put", K); return (+p*U, [("put", K, -1)])
    if a == "bull_put":
        Kh, Kl = round(S*0.97), round(S*0.90)
        if Kl >= Kh: Kl = Kh-1
        c = max(P("put", Kh)-P("put", Kl), 0.05); return (+c*U, [("put", Kh, -1), ("put", Kl, +1)])
    if a == "bear_call":
        Kl, Kh = round(S*1.03), round(S*1.10)
        if Kh <= Kl: Kh = Kl+1
        c = max(P("call", Kl)-P("call", Kh), 0.05); return (+c*U, [("call", Kl, -1), ("call", Kh, +1)])
    return (0.0, [])

def settle_pl(entry, legs, S):
    add = sum(sign * (max(S-K, 0) if k == "call" else max(K-S, 0)) * U for k, K, sign in legs)
    return entry + add

def run(games=1500, months=12):
    per_trade = {"buy": [], "sell": []}
    game_nets = []
    random.seed(2024)
    for _ in range(games):
        coms = {n: g.Commodity(n, dict(g.COMMODITIES[n])) for n in g.COMMODITIES}
        net = 0.0
        for _m in range(months):
            cat = random.choice(g.CATALYSTS)
            _, ccom, cdir, strength = cat
            priced_in = (cdir != "none" and (coms[ccom].iv_rank() >= 66 or random.random() < 0.25))
            dec = decide(cat, coms)
            pos = None
            if dec:
                action, name = dec
                entry, legs = build(action, coms[name])
                side = "buy" if action.startswith("buy") else "sell"
                pos = (name, entry, legs, side)
            # advance
            market = random.gauss(0, 1)
            for n, c in coms.items():
                d = 0.0
                if n == ccom and cdir != "none":
                    sgn = 1 if cdir == "bull" else -1
                    d = sgn * (strength/12) * (0.3 if priced_in else 1.0)
                c.advance(d, market=market)
            if pos:
                name, entry, legs, side = pos
                pl = settle_pl(entry, legs, coms[name].price())
                per_trade[side].append(pl)
                net += pl
        game_nets.append(net)

    def shape(pls):
        n = len(pls)
        if not n: return "no trades"
        wins = sum(1 for x in pls if x > 0.5)
        flat = sum(1 for x in pls if -0.5 <= x <= 0.5)
        loss = sum(1 for x in pls if x < -0.5)
        return (f"n={n}  mean={st.mean(pls):+.2f}  median={st.median(pls):+.2f}  "
                f"win={100*wins/n:.0f}%  flat={100*flat/n:.0f}%  loss={100*loss/n:.0f}%  "
                f"best={max(pls):+.1f} worst={min(pls):+.1f}")

    print("="*70)
    print(" AUTO-PLAY FOLLOWING THE SUGGESTED MOVE  ({} games x {} months)".format(games, months))
    print("="*70)
    print(" BUY trades : " + shape(per_trade["buy"]))
    print(" SELL trades: " + shape(per_trade["sell"]))
    all_pl = per_trade["buy"] + per_trade["sell"]
    print(" ALL trades : " + shape(all_pl))
    print("-"*70)
    wins_g = sum(1 for x in game_nets if x > 0)
    print(f" PER GAME net: mean={st.mean(game_nets):+.2f}  median={st.median(game_nets):+.2f}  "
          f"profitable games={100*wins_g/len(game_nets):.0f}%")
    print("="*70)

if __name__ == "__main__":
    run()
