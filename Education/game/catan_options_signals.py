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
import copy
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

def _downsample(vals, width):
    if len(vals) <= width:
        return vals
    step = len(vals) / width
    return [vals[min(len(vals) - 1, int(i * step))] for i in range(width)]

def sparkline(vals, width=32):
    vals = _downsample(vals, width)
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1.0
    return "".join(SPARK[min(7, int((v - lo) / rng * 7.999))] for v in vals)

def area_chart(vals, width=56, height=11):
    """Tall filled ASCII chart of a price series — far easier to read than the
    one-line sparkline (trend, seasonality, swings all visible)."""
    vals = _downsample(vals, width)
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1.0
    levels = [int(round((v - lo) / rng * (height - 1))) for v in vals]
    lines = []
    for row in range(height - 1, -1, -1):
        cells = "".join("█" if levels[c] >= row else " " for c in range(len(levels)))
        if row == height - 1:
            label = f"{hi:7.2f} ┤"
        elif row == 0:
            label = f"{lo:7.2f} ┤"
        else:
            label = "        │"
        lines.append(label + cells)
    lines.append("        └" + "─" * len(levels))
    return "\n".join(lines)

def show_charts(coms):
    print("\n" + "=" * 72)
    print("  5-YEAR HISTORY (trailing 60 months) — full charts")
    print("=" * 72)
    for n, c in coms.items():
        lo, hi = c.lo_hi(HIST_MONTHS)
        print(f"\n{n}  ~ {c.p['analog']}")
        print(f"  now {c.price():.2f}   5y range {lo:.2f}-{hi:.2f}   rVol {c.realized_vol()*100:.0f}%   "
              f"IV {iv_tag(c.iv_rank())}   trend {c.trend()}")
        print(area_chart(c.prices[-HIST_MONTHS:]))
    print("=" * 72)

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
def forward_stats(com, sims=500):
    """Simulate next month with NO catalyst -> fair forward F (mean) AND the true
    one-step dispersion as an annualized sigma. Pricing off BOTH (mean and the real
    spread, which includes jumps/vol-clustering) makes a no-edge trade ~zero-EV, so
    only the catalyst (unknown to the pricer) is an edge."""
    base = com.price()
    prices, rets = [], []
    for _ in range(sims):
        c = copy.deepcopy(com)
        c.advance(0.0)
        p = c.price()
        prices.append(p)
        rets.append(math.log(p / base))
    F = sum(prices) / sims
    m = sum(rets) / sims
    var = sum((r - m) ** 2 for r in rets) / (sims - 1)
    sigma = math.sqrt(var) * math.sqrt(12)
    return F, max(sigma, 0.05)

def iv_markup(rank):
    """Implied vol carries a rank-based premium over true dispersion (the variance
    risk premium). RICH IV (high rank) -> options priced ABOVE fair (selling harvests
    it); CHEAP IV -> priced BELOW fair (buying gets a bargain); FAIR (~50) -> ~fair."""
    rank = max(0, min(100, rank))
    return 1 + 0.15 * (rank - 50) / 50

def black_premium(kind, F, K, sigma, T):
    """Black model: option priced on the forward F (r=0). No-drift-edge baked out."""
    sigma = max(sigma, 0.05)
    if T <= 0 or F <= 0:
        return round(max(F - K, 0) if kind == "call" else max(K - F, 0), 2)
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    val = (F * Ncdf(d1) - K * Ncdf(d2)) if kind == "call" else (K * Ncdf(-d2) - F * Ncdf(-d1))
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
    wrap("TWO WAYS TO TRADE:")
    wrap("- BUY premium (long call/put): cheap and best when IV is CHEAP + a catalyst is "
         "coming. Max loss = the premium.", "  ")
    wrap("- SELL premium (cash-secured put, covered call, credit spreads): you COLLECT the "
         "premium and win if the move DOESN'T happen. Best when IV is RICH (premium fat). "
         "Needs capital as collateral; spreads cap the risk.", "  ")
    wrap("EDGE RULE: buy when IV CHEAP + catalyst; sell when IV RICH. No catalyst and low "
         "IV = sit out.")
    print()
    wrap("STRIKES & MONEYNESS: each option offers three strikes. A CALL with strike "
         "BELOW spot — or a PUT with strike ABOVE spot — is IN-THE-MONEY: it already "
         "has intrinsic value, so it costs more but moves almost dollar-for-dollar with "
         "the commodity. (Yes, a put above the current price is normal — it can already "
         "be exercised at a gain.) OUT-OF-THE-MONEY options are cheap but need a real "
         "move to pay off. Options are priced on the fair forward, so with NO catalyst "
         "the expected profit is ~zero — your only edge is the news + IV.")
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
    print(f"{'Commodity':<9}{'Price':>7}  {'5y history':<34}{'5y range':>13}  "
          f"{'rVol':>5}  {'IV Rank':<10}{'trend':>4}")
    for n, c in coms.items():
        lo, hi = c.lo_hi(HIST_MONTHS)
        spark = sparkline(c.prices[-HIST_MONTHS:])
        print(f"{n:<9}{c.price():>7.2f}  {spark:<34}{f'{lo:.1f}-{hi:.1f}':>13}  "
              f"{c.realized_vol()*100:>4.0f}%  {iv_tag(c.iv_rank()):<10}{c.trend():>4}")
    print("   tip: choose 'c' at the prompt to study full-size 5-year charts.")

def intrinsic(kind, K, S):
    return max(S - K, 0) if kind == "call" else max(K - S, 0)

def _build_long(com, F, sigma, gold, cdir, name, cheap, rich):
    """Buy a single call or put (long premium). Returns pos or None."""
    S = com.price(); T = 1 / 12
    kind = ask("Call (up) or Put (down)? (c/p): ", {"c": "call", "p": "put"})
    ks = strikes_for(S); chain = {}
    print(f"\n   {name} @ {S:.2f}   LONG {kind.upper()}   IV {iv_tag(com.iv_rank())}  (implied vol {sigma*100:.0f}%)")
    print("   (ITM = already exercisable, has intrinsic value, costs more; OTM = needs a move, cheap)")
    for i, K in enumerate(ks, 1):
        prem = black_premium(kind, F, K, sigma, T)
        intr = intrinsic(kind, S, K) if False else intrinsic(kind, K, S)
        mny = "ITM" if intr > 0 else ("ATM" if K == round(S) else "OTM")
        note = f"{intr:.2f} intrinsic" if intr > 0 else f"needs {'rise' if kind=='call' else 'fall'} past {K}"
        chain[str(i)] = (K, prem)
        print(f"   {i}) strike {K:<4} premium {prem:>5.2f}/u  contract {prem*UNITS:>6.2f}  [{mny}: {note}]")
    K, prem = ask("Pick a strike (number): ", chain)
    cost = prem * UNITS
    if cost > gold:
        print(f"   Not enough gold for {cost:.2f}."); return None
    bias = "bull" if kind == "call" else "bear"
    aligned = (name == cdir[0] and cdir[1] != "none" and
               ((cdir[1] == "bull" and kind == "call") or (cdir[1] == "bear" and kind == "put")))
    be = K + prem if kind == "call" else K - prem
    print(f"\n   BOUGHT long {kind} on {name} @ {K}, paid {cost:.2f} (max loss). Breakeven {be:.2f}.")
    return dict(name=name, label=f"long {kind} @ {K}",
                legs=[(kind, K, +1, prem)], stock=None, entry=-cost,
                max_profit=None, max_loss=cost, be=(K + prem if kind == "call" else K - prem),
                bias=bias, vol="long", good=aligned and cheap)

def _build_sell(com, F, sigma, gold, cdir, name, cheap, rich, choice):
    """Build a premium-SELLING structure. choice in 1..4. Returns pos or None."""
    S = com.price(); T = 1 / 12
    def P(kind, K): return black_premium(kind, F, K, sigma, T)

    if choice == "1":   # cash-secured put (income, bullish/neutral)
        K = max(1, round(S * 0.95)); p = P("put", K)
        legs = [("put", K, -1, p)]; entry = p * UNITS
        max_profit = p * UNITS; max_loss = K * UNITS - p * UNITS; be = K - p
        label = f"cash-secured put @ {K}"; bias = "bull"
    elif choice == "2": # covered call (own stock + short call)
        Kc = round(S * 1.05); c = P("call", Kc)
        legs = [("call", Kc, -1, c)]; stock = (UNITS, S)
        entry = c * UNITS - S * UNITS
        max_profit = (Kc - S) * UNITS + c * UNITS; max_loss = S * UNITS - c * UNITS; be = S - c
        label = f"covered call @ {Kc} (own stock @ {S:.2f})"; bias = "neutral"
        pos = dict(name=name, label=label, legs=legs, stock=stock, entry=entry,
                   max_profit=max_profit, max_loss=max_loss, be=be, bias=bias, vol="short")
        return _finish_sell(pos, gold, cdir, name, rich)
    elif choice == "3": # bull put credit spread (defined risk, bullish/neutral)
        Kh = round(S * 0.97); Kl = round(S * 0.90)
        if Kl >= Kh: Kl = Kh - 1
        ph, pl = P("put", Kh), P("put", Kl); credit = max(ph - pl, 0.05)
        legs = [("put", Kh, -1, ph), ("put", Kl, +1, pl)]; entry = credit * UNITS
        width = Kh - Kl
        max_profit = credit * UNITS; max_loss = (width - credit) * UNITS; be = Kh - credit
        label = f"bull put spread {Kl}/{Kh}"; bias = "bull"
    else:               # bear call credit spread (defined risk, bearish/neutral)
        Kl = round(S * 1.03); Kh = round(S * 1.10)
        if Kh <= Kl: Kh = Kl + 1
        cl, ch = P("call", Kl), P("call", Kh); credit = max(cl - ch, 0.05)
        legs = [("call", Kl, -1, cl), ("call", Kh, +1, ch)]; entry = credit * UNITS
        width = Kh - Kl
        max_profit = credit * UNITS; max_loss = (width - credit) * UNITS; be = Kl + credit
        label = f"bear call spread {Kl}/{Kh}"; bias = "bear"

    pos = dict(name=name, label=label, legs=legs, stock=None, entry=entry,
               max_profit=max_profit, max_loss=max_loss, be=be, bias=bias, vol="short")
    return _finish_sell(pos, gold, cdir, name, rich)

def _finish_sell(pos, gold, cdir, name, rich):
    if pos["max_loss"] > gold:
        print(f"   Need {pos['max_loss']:.2f} gold as collateral to sell this; you have "
              f"{gold:.2f}. (Selling premium requires capital to back the risk.)")
        return None
    cd = cdir[1]
    not_against = (cd == "none") or not ((pos["bias"] == "bull" and cd == "bear") or
                                         (pos["bias"] == "bear" and cd == "bull"))
    pos["good"] = rich and not_against
    print(f"\n   SOLD {pos['label']} on {name}: collected {pos['entry']:+.2f} now. "
          f"Max profit {pos['max_profit']:.2f}, max loss {pos['max_loss']:.2f}, breakeven {pos['be']:.2f}.")
    return pos

def trade_phase(coms, gold, catalyst, priced_in):
    head, ccom, cdir, _ = catalyst
    print("\n  MARKET NEWS:  " + head)
    if cdir != "none":
        rank = coms[ccom].iv_rank()
        note = "RICH (likely already priced in -> favor SELLING premium)" if priced_in \
               else "CHEAP/FAIR (room to run -> favor BUYING premium)"
        print(f"  -> {ccom} ({cdir}ish).  Its IV Rank = {rank} -> {note}.")
    else:
        print("  -> no clear catalyst; no directional edge. Selling premium in RICH IV is still valid.")

    while True:
        action = ask("\nAction: (b)uy option, (s)ell premium, (c)harts to study, or (n)othing? ",
                     {"b": "buy", "s": "sell", "c": "charts", "n": "none"})
        if action == "charts":
            show_charts(coms)
            input("\n(press Enter to return...)")
            continue
        break
    if action == "none":
        return None, gold

    names = list(coms.keys()); menu = {str(i+1): n for i, n in enumerate(names)}
    for k, n in menu.items():
        flag = "   <- in the news" if n == ccom and cdir != "none" else ""
        print(f"   {k}) {n}  @ {coms[n].price():.2f}   IV {iv_tag(coms[n].iv_rank())}{flag}")
    name = ask("Pick a commodity (number): ", menu)
    com = coms[name]
    F, sigma_true = forward_stats(com)
    rank = com.iv_rank(); rich = rank >= 66; cheap = rank <= 40
    sigma = sigma_true * iv_markup(rank)         # implied vol = fair dispersion + rank-based premium
    cd = (ccom, cdir)

    if action == "buy":
        pos = _build_long(com, F, sigma, gold, cd, name, cheap, rich)
    else:
        print("\n   SELL premium — pick a structure:")
        print("   1) Cash-secured put   (income; bullish/neutral; profit if price stays up)")
        print("   2) Covered call       (own stock + sell call; neutral/mild-bull; capped upside)")
        print("   3) Bull put spread    (defined-risk credit; bullish/neutral)")
        print("   4) Bear call spread   (defined-risk credit; bearish/neutral)")
        choice = ask("Structure (1-4): ", {"1": 1, "2": 2, "3": 3, "4": 4})
        pos = _build_sell(com, F, sigma, gold, cd, name, cheap, rich, str(choice))

    if pos is None:
        return None, gold
    gold += pos["entry"]
    return pos, gold

def settle(pos, S, gold, scored):
    add = 0.0
    if pos["stock"]:
        qty, _ = pos["stock"]; add += qty * S
    for kind, K, sign, _ in pos["legs"]:
        add += sign * intrinsic(kind, K, S) * UNITS
    gold += add
    pl = pos["entry"] + add
    print(f"\n   SETTLE {pos['label']} on {pos['name']}: price -> {S:.2f}.  P/L {pl:+.2f}.")
    if pos["vol"] == "long" and pl <= -pos["max_loss"] + 0.01:
        print(f"   Expired worthless — lost the premium ({pos['max_loss']:.2f}, the max loss).")
    if pos.get("good"):
        scored["n"] += 1; scored["pl"] += pl
        side = "bought CHEAP IV with the catalyst" if pos["vol"] == "long" else "sold RICH IV (collected premium)"
        print(f"   (Edge play: {side}.)")
    elif pos["vol"] == "long":
        print("   (No cheap-IV + catalyst edge here — closer to a coin flip.)")
    else:
        print("   (Sold premium without a RICH-IV edge — thin reward for the risk.)")
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
        pos, gold = trade_phase(coms, gold, catalyst, priced_in)

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
