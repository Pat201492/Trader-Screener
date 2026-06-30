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

# Enable ANSI colors on Windows consoles (VT processing).
if sys.platform == "win32":
    try:
        import ctypes
        _k = ctypes.windll.kernel32
        _k.SetConsoleMode(_k.GetStdHandle(-11), 7)
    except Exception:
        pass

GREEN, YELLOW, RED, RESET = "\033[92m", "\033[93m", "\033[91m", "\033[0m"

def Ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

# ---- commodity models: real-world-shaped parameters (monthly) ----
# drift: annual log-trend | seas_amp/phase: annual seasonality | rho: cyclical
# persistence (momentum vs mean-reversion) | vol_m: base monthly vol | jump_p/size:
# shock probability/magnitude | analog: the real instrument it mimics.
# Realism params (added):
#   beta   = loading on a shared MACRO factor -> commodities co-move (real markets do)
#   mom_k  = short-term MOMENTUM gain on recent 3-month return (trend then reverts)
#   jbias  = probability a jump is UPWARD (supply shocks skew ag/metal spikes up)
COMMODITIES = {
    "Grain":  dict(start=7.0,  drift=0.015, seas_amp=0.13, phase=1.6, rho=0.70,
                   vol_m=0.060, jump_p=0.04, jump=0.14, beta=0.45, mom_k=0.25, jbias=0.65,
                   analog="agricultural grain (seasonal)",
                   exporters="Russia, USA, Canada, Ukraine, Australia, EU",
                   importers="Egypt, China, Indonesia, Algeria, Turkey",
                   drivers="weather/harvest, planting cycles, export bans, fuel & fertilizer costs"),
    "Lumber": dict(start=9.0,  drift=0.020, seas_amp=0.03, phase=0.0, rho=0.93,
                   vol_m=0.110, jump_p=0.05, jump=0.20, beta=0.55, mom_k=0.35, jbias=0.55,
                   analog="lumber (boom/bust cyclical)",
                   exporters="Canada, Russia, Sweden, Finland, Germany",
                   importers="USA, China, Japan, UK",
                   drivers="housing starts, interest rates, wildfires, sawmill capacity"),
    "Brick":  dict(start=10.0, drift=0.035, seas_amp=0.02, phase=0.0, rho=0.85,
                   vol_m=0.050, jump_p=0.02, jump=0.10, beta=0.50, mom_k=0.20, jbias=0.50,
                   analog="construction material (steady trend)",
                   exporters="Vietnam, Turkey, Thailand, UAE, China",
                   importers="USA, Bangladesh, Philippines, Australia",
                   drivers="construction activity, energy costs, infrastructure spending"),
    "Wool":   dict(start=8.0,  drift=0.005, seas_amp=0.07, phase=2.4, rho=0.80,
                   vol_m=0.080, jump_p=0.03, jump=0.12, beta=0.40, mom_k=0.20, jbias=0.55,
                   analog="soft commodity (mild seasonal)",
                   exporters="Australia, New Zealand, China, South Africa",
                   importers="China, India, Italy, Czechia",
                   drivers="flock sizes, textile/fashion demand, synthetic substitutes, drought"),
    "Ore":    dict(start=14.0, drift=0.012, seas_amp=0.02, phase=0.0, rho=0.90,
                   vol_m=0.110, jump_p=0.05, jump=0.22, beta=0.65, mom_k=0.30, jbias=0.60,
                   analog="industrial metal / iron ore (cyclical)",
                   exporters="Australia, Brazil, South Africa, India",
                   importers="China, Japan, South Korea, Germany",
                   drivers="Chinese steel demand, mine outages/supply, infrastructure cycles"),
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
START_GOLD = 1000.0
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

# ============================== PAGE: HISTORY ==============================
def show_history(coms):
    print("\n" + "#" * 72)
    print("#  HISTORY — price history & real-world trade for each commodity")
    print("#" * 72)
    for n, c in coms.items():
        p = c.p
        lo, hi = c.lo_hi(HIST_MONTHS)
        print(f"\n{'='*72}\n{n}  ~ {p['analog']}")
        print(f"  now {c.price():.2f}   5y range {lo:.2f}-{hi:.2f}   "
              f"rVol {c.realized_vol()*100:.0f}%   IV {iv_tag(c.iv_rank())}   trend {c.trend()}")
        print()
        print(area_chart(c.prices[-HIST_MONTHS:]))
        print(f"\n  Top EXPORTERS:  {p['exporters']}")
        print(f"  Top IMPORTERS:  {p['importers']}")
        print(f"  Price drivers:  {p['drivers']}")
    print("#" * 72)
    wrap("Why exporters/importers matter: a shock in a big EXPORTER (drought, mine outage, "
         "export ban) cuts supply -> price up; weakness in a big IMPORTER (recession, less "
         "construction) cuts demand -> price down. That's where real catalysts come from.")

# ============================== PAGE: GUIDE (plain-English) ==============================
def show_guide():
    print("\n" + "#" * 72)
    print("#  GUIDE — start here. Plain English: what to do and why.")
    print("#" * 72)
    wrap("THE GOAL: grow your gold by betting on where commodity prices go, using options.")
    print()
    wrap("AN OPTION is just a bet with a fee (the 'premium'):")
    wrap("- BUY a call  = pay a small fee to bet the price goes UP.   You risk only the fee.", "   ")
    wrap("- BUY a put   = pay a small fee to bet the price goes DOWN. You risk only the fee.", "   ")
    wrap("- SELL an option = you are the bookie. You COLLECT the fee now and keep it if the "
         "move does NOT happen. You need cash set aside, and can lose more than you collected.", "   ")
    print()
    print("  EVERY TURN, ANSWER 3 QUESTIONS:")
    wrap("1) Which way will it move?  -> the NEWS tells you. 'scarce / boom / demand' = UP "
         "(bullish). 'glut / record / floods' = DOWN (bearish).", "   ")
    wrap("2) Are options cheap or pricey right now?  -> IV RANK. Low (CHEAP) = cheap to BUY. "
         "High (RICH) = pricey, so better to SELL.", "   ")
    wrap("3) So what do I do?  -> read the table:", "   ")
    print()
    print("   NEWS up   + options CHEAP  ->  BUY A CALL        (cheap bet it rises)")
    print("   NEWS down + options CHEAP  ->  BUY A PUT         (cheap bet it falls)")
    print("   NEWS up   + options RICH   ->  SELL A PUT        (get paid; win if it rises/stays)")
    print("                                 or a bull put spread (same idea, capped risk)")
    print("   NEWS down + options RICH   ->  SELL A CALL/bear call spread (win if it falls/stays)")
    print("   NO clear news              ->  usually SIT OUT   (no edge = don't trade)")
    print()
    wrap("WHY buy when cheap / sell when pricey: the fee (premium) is bigger when options are "
         "'RICH'. If you're BUYING, you want to pay little (CHEAP). If you're SELLING, you want "
         "to collect a lot (RICH). Same as buying low / selling high — but on the option fee.")
    print()
    print("  WORKED EXAMPLE")
    wrap("News: 'Drought ruins the harvest — GRAIN scarce'  => grain prices likely UP (bullish).", "   ")
    wrap("Grain IV Rank shows CHEAP(25)  => options are cheap.", "   ")
    wrap("Table says: UP + CHEAP -> BUY A CALL on Grain. Pick a strike near the price. If grain "
         "rises past (strike + fee) you profit; if it doesn't, you only lose the small fee.", "   ")
    wrap("On the screen: choose (b)uy -> pick Grain -> (c)all -> pick a strike. Done.", "   ")
    print()
    wrap("Tip: during play, a 'SUGGESTED MOVE' line shows the textbook play each turn — follow "
         "it while learning, then start deciding on your own. The end-of-game scorecard shows "
         "the suggested move vs what you did, every month.")
    print("#" * 72)

# ============================== PAGE: INFORMATION ==============================
INFO = [
    ("Price", "Latest market price of the commodity.",
     "The reference for strikes, moneyness, and breakevens. Meaningless alone — read it vs the range, trend, and catalyst."),
    ("5y history / sparkline", "The price path over the trailing 60 months (a mini chart).",
     "Shows trend, seasonality, and regime. Buy dips in uptrends; respect seasonal cycles; don't chase vertical spikes."),
    ("5y range (lo-hi)", "Min and max price over the last 5 years.",
     "Context for cheap vs expensive. Near the high = strength/overextended; near the low = value or a falling knife."),
    ("rVol (realized volatility)", "Annualized stdev of the last 12 monthly returns (x sqrt12).",
     "How much it ACTUALLY moves. High rVol -> trade smaller, wider stops, pricier options. It's the baseline IV is judged against."),
    ("IV Rank (0-100)", "Percentile of today's 3-month vol within its own 5-year vol history.",
     "THE buy/sell switch. CHEAP (<=40) -> buy premium; RICH (>=66) -> sell premium (move likely priced in); FAIR in between."),
    ("Implied vol", "The vol used to price options = fair dispersion x an IV-Rank markup (0.85-1.15x).",
     "Above realized = options rich (sell); below = cheap (buy). The gap is the variance risk premium."),
    ("Trend (up/flat/down)", "Sign of the 6-month price change, with a +/-4% dead-band.",
     "Now carries SHORT-TERM momentum: recent moves tend to persist a month or two (verified by validate_model.py, lag-1 autocorr > 0), so trend is a useful secondary confirmation. But moves are bounded and seasonals mean-revert over longer horizons — don't chase a stretched move. The primary edges remain the catalyst + IV Rank; trend confirms direction."),
    ("Catalyst / market news", "An event shifting supply or demand (harvest, mine outage, housing, policy).",
     "The 'why now'. Match direction (bull->call, bear->put); no catalyst = no edge = sit out."),
    ("Forward price (F)", "Monte-Carlo mean of the next month's price with no catalyst.",
     "Options are priced off F, so direction alone is ~zero-EV. Your edge is only what F doesn't already include."),
    ("Strike & moneyness", "Strike vs spot: ITM (call<spot / put>spot), ATM (~spot), OTM (other side).",
     "Sets odds & risk/reward. OTM = cheap, low-odds lottery; ATM = balanced; ITM = stock-like, high-odds, costs more."),
    ("Intrinsic vs time value", "Intrinsic = exercise-now worth; time value = premium - intrinsic.",
     "Time value decays to zero by expiry (theta). Buyers fight it; sellers harvest it."),
    ("Premium", "Black-model price on the forward (1-month option).",
     "Buyer's MAX LOSS / seller's MAX PROFIT. Overpay and a correct direction can still lose."),
    ("Breakeven", "Call: strike+premium. Put: strike-premium. Spread: short strike +/- credit.",
     "The price the move must clear to profit. Compare to a realistic move (rVol x catalyst) before entering."),
]

ACTIONS = [
    ("Buy a call (long call)", "Pay a premium for the RIGHT to buy at the strike.",
     "Bullish, leveraged. Max loss = the premium; upside large. Long vega, hurt by time decay. Best when IV is CHEAP + a bullish catalyst."),
    ("Buy a put (long put)", "Pay a premium for the RIGHT to sell at the strike.",
     "Bearish, or a hedge on something you own. Max loss = the premium; profit grows as price falls. Best when IV is CHEAP + a bearish catalyst."),
    ("Sell a cash-secured put", "Sell a put and reserve cash to buy the stock if assigned.",
     "Bullish/neutral INCOME. Max profit = premium collected; large loss if price collapses toward zero. Ties up collateral. Short vega, gains from time decay. Best when IV is RICH."),
    ("Sell a covered call", "Own the underlying AND sell a call against it.",
     "Neutral / mild-bull INCOME. Premium + gains up to the strike are yours; upside ABOVE the strike is capped; still fully exposed to the stock's downside. Best when IV is RICH."),
    ("Bull put credit spread", "Sell a put and buy a lower-strike put (net credit).",
     "Bullish/neutral, DEFINED risk. Max profit = the credit; max loss = strike width - credit. Wins if price stays ABOVE the short strike. Best when IV is RICH."),
    ("Bear call credit spread", "Sell a call and buy a higher-strike call (net credit).",
     "Bearish/neutral, DEFINED risk. Max profit = the credit; max loss = strike width - credit. Wins if price stays BELOW the short strike. Best when IV is RICH."),
    ("Sit out (do nothing)", "Take no position this turn.",
     "Valid and often correct. No catalyst + fair IV = no edge; preserving capital beats a coin-flip trade. Discipline is a skill."),
]

def show_information():
    print("\n" + "#" * 72)
    print("#  INFORMATION — every metric: definition & impact")
    print("#" * 72)
    for name, defn, impact in INFO:
        print(f"\n{name}")
        wrap(f"Definition: {defn}", "   ")
        wrap(f"Impact: {impact}", "   ")
    print("\n" + "=" * 72)
    print("  ACTIONS — what you can do, and when")
    print("=" * 72)
    for name, defn, impact in ACTIONS:
        print(f"\n{name}")
        wrap(f"What it is: {defn}", "   ")
        wrap(f"When / impact: {impact}", "   ")
    print("\n" + "#" * 72)
    wrap("Decision chain: rVol sets the baseline -> IV Rank says buy-vs-sell -> trend+catalyst "
         "set direction -> forward/implied mean direction alone is ~free -> premium & breakeven "
         "are the final cost/feasibility check.")
    print("Rule of thumb: BUY (call/put) when IV is CHEAP + a catalyst; SELL (CSP / covered "
          "call / credit spread) when IV is RICH; sit out when neither.")
    wrap("Model is validated by validate_model.py: catalysts move price (bull>none>bear), "
         "volatility clusters (IV Rank is informative), prices show short-term momentum + "
         "co-move via a shared macro factor, the priced forward is unbiased (honest pricing), "
         "and signal-aligned trades beat no-edge beat wrong-way. Edges: catalyst + IV Rank, "
         "with trend as secondary confirmation.")
    print("#" * 72)

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

    def _emit(self, rng, catalyst_drift=0.0, market=None):
        pp = self.p
        # stochastic volatility (clustering)
        self.h = 0.9 * self.h + 0.30 * rng.gauss(0, 1)
        sigma = pp["vol_m"] * math.exp(0.5 * self.h)
        # shock: blend a shared MACRO factor (co-movement) with idiosyncratic noise.
        idio = rng.gauss(0, 1)
        if market is None:
            shock = idio                                   # unit variance either way
        else:
            b = pp["beta"]
            shock = b * market + math.sqrt(max(0.0, 1 - b * b)) * idio
        # asymmetric jumps (supply shocks skew up)
        jump = 0.0
        if rng.random() < pp["jump_p"]:
            jump = pp["jump"] * (1 if rng.random() < pp["jbias"] else -1)
        # short-term momentum from the recent 3-month return (clipped; decays via rho)
        mom = 0.0
        if len(self.prices) >= 4:
            rr = self.prices[-1] / self.prices[-4] - 1
            rr = max(-0.4, min(0.4, rr))               # clip to avoid runaway feedback
            mom = pp["mom_k"] * rr
        self.c = pp["rho"] * self.c + sigma * shock + jump + catalyst_drift + mom
        self.c = max(-1.5, min(1.5, self.c))           # bound deviation (price in ~0.2x..4.5x trend)
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

    def advance(self, catalyst_drift, market=None):
        return self._emit(random, catalyst_drift, market)   # forward uses global RNG

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

def prob_profit(kind, F, breakeven, sigma, T):
    """Estimated chance the option finishes PAST breakeven by expiry, from the
    forward F and vol (lognormal). A plain 'odds this trade makes money'."""
    sigma = max(sigma, 0.05)
    if T <= 0 or breakeven <= 0:
        return 0.0
    m = math.log(F) - 0.5 * sigma**2 * T          # mean of ln(S_T)
    s = sigma * math.sqrt(T)
    z = (math.log(breakeven) - m) / s
    return (Ncdf(-z) if kind == "call" else Ncdf(z))   # call: P(S>BE); put: P(S<BE)

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
    print("   (ITM = already exercisable, costs more, higher odds; OTM = cheap, lower odds.")
    print(f"    'profit odds' = est. chance the price clears your breakeven. "
          f"{GREEN}green = most likely{RESET}, {RED}red = least likely{RESET}.)")
    rows = []
    for i, K in enumerate(ks, 1):
        prem = black_premium(kind, F, K, sigma, T)
        be = K + prem if kind == "call" else K - prem
        odds = prob_profit(kind, F, be, sigma, T) * 100
        intr = intrinsic(kind, K, S)
        mny = "ITM" if intr > 0 else ("ATM" if K == round(S) else "OTM")
        note = f"{intr:.2f} intrinsic" if intr > 0 else f"needs {'rise' if kind=='call' else 'fall'} past {K}"
        chain[str(i)] = (K, prem)
        rows.append((i, K, prem, mny, note, odds))
    hi = max(rows, key=lambda r: r[5])[0]
    lo = min(rows, key=lambda r: r[5])[0]
    for i, K, prem, mny, note, odds in rows:
        if i == hi:
            col, tag = GREEN, "  <- MOST likely"
        elif i == lo:
            col, tag = RED, "  <- LEAST likely"
        else:
            col, tag = YELLOW, ""
        print(f"   {i}) strike {K:<4} premium {prem:>5.2f}/u  contract {prem*UNITS:>6.2f}  "
              f"[{mny}: {note}]  {col}~{odds:.0f}% profit odds{RESET}{tag}")
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
        if rank >= 66:
            stance = "RICH -> options pricey; favor SELLING premium (move may be priced in)"
        elif rank <= 40:
            hint = "calls" if cdir == "bull" else "puts"
            stance = f"CHEAP -> options cheap; favor BUYING {hint} on this {cdir}ish news"
        else:
            stance = "FAIR -> only a small edge; spreads or sitting out are reasonable"
        print(f"  -> {ccom} ({cdir}ish).  Its IV Rank = {rank} -> {stance}.")
    else:
        print("  -> no clear catalyst; no directional edge. Selling premium when IV is RICH is still valid.")
    print("  SUGGESTED MOVE: " + recommend(catalyst, coms) + "   (follow it while learning, or ignore)")

    while True:
        action = ask("\nAction: (b)uy, (s)ell, (g)uide, (i)nfo, (c)harts, (h)istory, or (n)othing? ",
                     {"b": "buy", "s": "sell", "g": "guide", "c": "charts", "i": "info",
                      "h": "history", "n": "none"})
        if action == "guide":
            show_guide(); input("\n(press Enter to return...)"); continue
        if action == "charts":
            show_charts(coms); input("\n(press Enter to return...)"); continue
        if action == "info":
            show_information(); input("\n(press Enter to return...)"); continue
        if action == "history":
            show_history(coms); input("\n(press Enter to return...)"); continue
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
    scored["trades"].append(dict(label=pos["label"], name=pos["name"], pl=pl,
                                 good=bool(pos.get("good")), vol=pos["vol"]))
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
def recommend(catalyst, coms):
    """The textbook play given the news + IV Rank (the decision recipe).
    Used as the post-game baseline to compare your choices against."""
    head, ccom, cdir, _ = catalyst
    if cdir == "none":
        rich_name = max(coms, key=lambda n: coms[n].iv_rank())
        r = coms[rich_name].iv_rank()
        if r >= 66:
            return f"Sell premium on {rich_name} (IV RICH {r}) — credit spread / cash-secured put"
        return "Sit out — no catalyst and no richly-priced IV (no edge)"
    r = coms[ccom].iv_rank()
    if cdir == "bull":
        if r <= 40:
            return f"BUY a CALL on {ccom} (bullish + cheap IV {r})"
        if r >= 66:
            return f"SELL puts on {ccom} (bullish + rich IV {r}) — cash-secured put / bull put spread"
        return f"Bull put spread on {ccom} (bullish, fair IV {r}) — or sit out"
    else:
        if r <= 40:
            return f"BUY a PUT on {ccom} (bearish + cheap IV {r})"
        if r >= 66:
            return f"SELL calls on {ccom} (bearish + rich IV {r}) — bear call spread"
        return f"Bear call spread on {ccom} (bearish, fair IV {r}) — or sit out"

def grade(net, edge_pl, noise_pl, win_rate, discipline):
    pts = 0
    pts += 2 if net > 0 else 0
    pts += 2 if edge_pl > 0 else 0
    pts += 1 if edge_pl > noise_pl else 0
    pts += 1 if win_rate >= 0.5 else 0
    pts += 1 if discipline >= 0.5 else 0          # most trades were edge plays / sat out junk
    return ["F", "F", "D", "C", "C", "B", "B", "A"][max(0, min(7, pts))]

def scorecard(start, final, scored):
    trades = scored["trades"]
    net = final - start
    roi = net / start * 100
    n = len(trades)
    wins = sum(1 for t in trades if t["pl"] > 0.01)
    losses = sum(1 for t in trades if t["pl"] < -0.01)
    win_rate = wins / n if n else 0.0
    edge = [t for t in trades if t["good"]]
    noise = [t for t in trades if not t["good"]]
    edge_pl = sum(t["pl"] for t in edge)
    noise_pl = sum(t["pl"] for t in noise)
    buys = [t for t in trades if t["vol"] == "long"]
    sells = [t for t in trades if t["vol"] == "short"]
    discipline = (len(edge) / n) if n else 1.0
    g = grade(net, edge_pl, noise_pl, win_rate, discipline)

    bar = "═" * 60
    print("\n" + bar)
    print("                     S C O R E C A R D")
    print(bar)
    print(f"  Starting gold ........ {start:8.2f}")
    print(f"  Final gold ........... {final:8.2f}")
    print(f"  Net P/L .............. {net:+8.2f}   ({roi:+.1f}% ROI)")
    print("  " + "-" * 56)
    print(f"  Trades taken ......... {n}   (wins {wins} / losses {losses}, "
          f"win rate {win_rate*100:.0f}%)")
    print(f"  Buys / Sells ......... {len(buys)} / {len(sells)}")
    if trades:
        best = max(trades, key=lambda t: t["pl"])
        worst = min(trades, key=lambda t: t["pl"])
        print(f"  Best trade ........... {best['pl']:+8.2f}  ({best['label']} on {best['name']})")
        print(f"  Worst trade .......... {worst['pl']:+8.2f}  ({worst['label']} on {worst['name']})")
    print("  " + "-" * 56)
    print(f"  EDGE trades .......... {len(edge):2d}   net {edge_pl:+8.2f}   <- your skill")
    print(f"  Coin-flip trades ..... {len(noise):2d}   net {noise_pl:+8.2f}   <- noise")
    print("  " + "-" * 56)
    print(f"  GRADE ................   {g}")
    print(bar)
    # tailored feedback
    if not trades:
        wrap("You never traded. Capital preserved — but you also learned nothing this run. "
             "Next time act when a catalyst lines up with cheap/rich IV.")
    else:
        if edge_pl > 0 and edge_pl >= noise_pl:
            wrap("Your edge trades carried the result — that's exactly the goal: profit comes "
                 "from reading the catalyst + IV, not from coin flips.")
        if noise_pl < 0 and len(noise) > len(edge):
            wrap("Too many no-edge trades dragged you down. Discipline — sitting out when there's "
                 "no signal — is itself a skill.")
        if edge_pl <= 0 and edge:
            wrap("Even edge trades lost this run — variance happens over a short season. The setups "
                 "were right; small samples are noisy. Process over outcome.")
    print(bar)

    # Coach's baseline: the textbook play each month vs what you did.
    plan = scored.get("plan", [])
    if plan:
        print("\n  COACH'S BASELINE — best play each month vs your choice")
        print("  (compare your decisions to the news + IV-Rank recipe)")
        print("  " + "-" * 56)
        for m in plan:
            print(f"  {m['month']}  news: {m['news']}")
            print(f"     reco: {m['reco']}")
            print(f"     you:  {m['you']}")
        print(bar)

def tutorial_intro():
    print("\n" + "=" * 72)
    print("        TUTORIAL — a short, guided 5-month walkthrough")
    print("=" * 72)
    wrap("I'll walk you through each turn: read the news, check the IV, and I'll tell you the "
         "best move and WHY. You make the move yourself (so you learn the keys). After it "
         "settles, I'll explain what happened. Start with the SUGGESTED MOVE every time — "
         "you can branch out later in a normal game.")
    wrap("The whole game is just this loop: NEWS (which way) + IV RANK (cheap or pricey) "
         "-> an action. That's it.")
    print("-" * 72)

def coach_setup(catalyst, coms, reco):
    head, ccom, cdir, _ = catalyst
    print("\n  +--- TUTORIAL ---------------------------------------------------+")
    if cdir == "none":
        wrap("Q1 (direction): the news has no clear catalyst -> no view on direction.", "  | ")
        wrap("Q2 (cheap/pricey): with no catalyst there's usually no edge to act on.", "  | ")
        wrap(f"Q3 (action): {reco}.", "  | ")
        wrap("Lesson: NOT trading is a real choice. No edge -> keep your gold.", "  | ")
    else:
        dir_word = "UP (bullish)" if cdir == "bull" else "DOWN (bearish)"
        rank = coms[ccom].iv_rank()
        tag = "CHEAP (good to BUY)" if rank <= 40 else ("RICH (good to SELL)" if rank >= 66 else "FAIR (small edge)")
        wrap(f"Q1 (direction): news on {ccom} suggests price goes {dir_word}.", "  | ")
        wrap(f"Q2 (cheap/pricey): {ccom} IV Rank = {rank} -> {tag}.", "  | ")
        wrap(f"Q3 (action) -> SUGGESTED MOVE: {reco}.", "  | ")
        if rank <= 40:
            atm = round(coms[ccom].price())
            wrap("Why: news says it moves + options are cheap -> BUYING is the cheap, "
                 "defined-risk way to bet that direction.", "  | ")
            wrap(f"Suggested strike: {atm} (at-the-money — the middle choice). ATM balances "
                 f"cost vs odds; pick the OTM strike only for a cheaper, longer-shot bet.", "  | ")
        elif rank >= 66:
            wrap("Why: options are pricey -> better to SELL premium (collect the fat fee) "
                 "than overpay to buy it.", "  | ")
            wrap("Strikes: the game auto-picks them for sell structures (an out-of-the-money "
                 "put/call, or the two legs of a spread) — no strike to choose here.", "  | ")
        else:
            wrap("Why: only a small edge at fair IV -> a spread or sitting out is reasonable.", "  | ")
    print("  +----------------------------------------------------------------+")

def coach_debrief(pos, scored):
    print("\n  +--- TUTORIAL: what happened -----------------------------------+")
    if not pos:
        wrap("You sat out — no gold at risk. Correct when there's no edge.", "  | ")
    else:
        t = scored["trades"][-1]
        if t["pl"] > 0:
            wrap(f"Profit {t['pl']:+.2f}. The move went your way and beat your breakeven "
                 f"(after the fee). That's the goal.", "  | ")
        elif t["pl"] < 0:
            if t["vol"] == "long":
                wrap(f"Loss {t['pl']:+.2f}. Either the move didn't come or it didn't clear "
                     f"your breakeven. Note: the premium was the MOST you could lose.", "  | ")
            else:
                wrap(f"Loss {t['pl']:+.2f}. You sold premium and the move went against you — "
                     f"selling can lose more than the fee collected (that's the risk).", "  | ")
        else:
            wrap("Broke even.", "  | ")
        if t["good"]:
            wrap("This was an EDGE play (signal + right IV). Over many turns these win.", "  | ")
    print("  +----------------------------------------------------------------+")

def play(months=PLAY_MONTHS, tutorial=False, seed=None):
    random.seed(seed) if seed is not None else random.seed()
    coms = {n: Commodity(n, dict(p)) for n, p in COMMODITIES.items()}
    tutorial_intro() if tutorial else intro(coms)
    gold = START_GOLD
    scored = {"n": 0, "pl": 0.0, "trades": [], "plan": []}
    month_idx = HIST_MONTHS            # first played month = right after history

    for turn in range(1, months + 1):
        show_board(coms, month_idx, turn, gold)
        catalyst = random.choice(CATALYSTS)
        _, ccom, cdir, strength = catalyst
        priced_in = (cdir != "none" and (coms[ccom].iv_rank() >= 66 or random.random() < 0.25))
        reco = recommend(catalyst, coms)              # baseline (uses IV the player can see)
        if tutorial:
            coach_setup(catalyst, coms, reco)
        pos, gold = trade_phase(coms, gold, catalyst, priced_in)
        scored["plan"].append(dict(month=month_label(month_idx),
                                   news=catalyst[0],
                                   reco=reco,
                                   you=(pos["label"] if pos else "sat out")))

        input("\n(press Enter to advance one month...)")
        # forward step: one shared MACRO shock drives co-movement; catalyst adds monthly
        # log-drift to its commodity (damped if priced in).
        market = random.gauss(0, 1)
        for n, c in coms.items():
            d = 0.0
            if n == ccom and cdir != "none":
                sign = 1 if cdir == "bull" else -1
                d = sign * (strength / 12) * (0.3 if priced_in else 1.0)
            c.advance(d, market=market)
        month_idx += 1
        if pos:
            gold = settle(pos, coms[pos["name"]].price(), gold, scored)
        if tutorial:
            coach_debrief(pos, scored)
        print(f"\n   Gold now: {gold:.2f}")
        if gold < 1:
            print("\n   Out of gold. Game over."); break

    scorecard(START_GOLD, gold, scored)
    if tutorial:
        wrap("Tutorial done. You've seen the full loop. Now try menu option 1 (Play) for a "
             "real 12-month run — follow the SUGGESTED MOVE while you build confidence.")
    else:
        print("Real parallel: history/sparkline = price chart; rVol & IV Rank = volatility "
              "signals; catalyst = news/earnings/supply data. The Trader Screener surfaces all "
              "of these so your decisions carry an edge.")

def main():
    """Start menu — toggle into History or Information pages before playing."""
    menu_coms = {n: Commodity(n, dict(p)) for n, p in COMMODITIES.items()}
    while True:
        print("\n" + "=" * 60)
        print("            CATAN OPTIONS — MAIN MENU")
        print("=" * 60)
        print("   1) Play          — full 12-month trading run")
        print("   2) Guide         — START HERE: plain-English how-to + example")
        print("   3) Tutorial      — guided 5-month walkthrough (coached each turn)")
        print("   4) History       — price charts + who exports/imports each")
        print("   5) Information    — every metric & action: definition & impact")
        print("   6) Quit")
        choice = ask("Choose (1-6): ",
                     {"1": "play", "2": "guide", "3": "tutorial", "4": "history",
                      "5": "info", "6": "quit"})
        if choice == "play":
            play()
        elif choice == "guide":
            show_guide(); input("\n(press Enter to return to menu...)")
        elif choice == "tutorial":
            play(months=5, tutorial=True, seed=7)
        elif choice == "history":
            show_history(menu_coms); input("\n(press Enter to return to menu...)")
        elif choice == "info":
            show_information(); input("\n(press Enter to return to menu...)")
        else:
            print("Farewell, trader."); return

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nLeft the trading post. Farewell.")
