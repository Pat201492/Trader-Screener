# How to Read a Tear Sheet — What Every Line Means and *Why It Matters*

> Companion to the [Instrument Information Sheet / Tear Sheet](Information_Sheet_Tearsheet.md). That doc *shows* the sheets; this one teaches you to **read** one — line by line, with the reasoning behind each metric. The goal: look at any instrument's sheet and know, in under a minute, *what the position is, what it's risking, and whether the recommendation actually fits.*

> Educational only — not investment advice.

---

## The mental model: a tear sheet is an argument
A good tear sheet is not a data dump — it's a **logical argument** with a conclusion (the trade). It flows:

> **Snapshot** (the facts) → **Thesis** (the claim) → **Catalysts/Risks** (what could prove it right or wrong) → **Positioning/Technicals** (is anyone else seeing it?) → **Recommendation** (the trade that expresses the claim *given today's volatility*).

When you read one, your job is to **check the argument holds**: do the metrics support the thesis, and does the structure match both the direction *and* the volatility regime? If any link breaks, the trade is suspect. Below, each block — and *why it earns its place*.

---

## 1. The header — rating, target, horizon
**What it says:** name, asset class, a **rating** (Buy/Hold/Sell or Long/Neutral/Short), a **price target**, and a **time horizon**.

**Why it matters:** the rating is meaningless without the **target + horizon together**. "Buy" with a $240 target on a $200 stock = +20%; but over *what period*? +20% in 1 month and +20% in 2 years are completely different risk propositions. The horizon also dictates the **option expiry** in the recommendation — a 3-month thesis needs ~3-month options, not weeklies. **Red flag:** a rating with no target, or a target with no horizon — that's an opinion, not an argument.

---

## 2. The snapshot — and why each metric is there
This is the "screen columns" block. Each line answers a specific question.

### Price, market cap, 52-week range
**Why:** the **52-week range** instantly frames *where* you are. A price near the 52-wk high means you're buying strength (momentum) or chasing (risk); near the low means value (or a falling knife). Market cap tells you the **liquidity tier** and which risks apply (a $310B name doesn't get taken over on a whim; a $400M one can move 20% on one fund's order).

### Average daily *dollar* volume  ← the most underrated line
**Why it matters more than you think:** this is your **tradeability and exit-cost gauge**. Per Harris (microstructure), your real cost isn't the commission — it's the **spread + market impact** you pay getting in and out. A brilliant thesis on a name that trades $2M/day is nearly unexploitable: your own order moves the price against you, and in a panic there's **no liquidity to exit into**. Dollar volume (not share count) approximates **depth** — how much you can trade without moving the market. **A signal you can't act on at size is not a signal.**

### Beta, realized volatility, ATR
**Why:** these size the position and set the stops.
- **Beta** — how much the name amplifies the market. High beta = your P/L is really a leveraged bet on the *index*, not the company. In a market selloff, beta tells you how hard you get hit regardless of your thesis.
- **Realized volatility** — how much it *actually* moves. This is the baseline you compare *implied* vol against (see IV Rank). 
- **ATR (average true range)** — the typical daily dollar move. **Why it's critical:** you set stops and size by ATR so your *dollar risk per trade is constant* across names. A $2 stop on a stock with a $6 ATR will get hit by noise; ATR tells you how much room the position needs to breathe.

### Valuation (P/E, EV/EBITDA, upside-to-target)
**Why:** these justify the **direction** of the thesis for fundamentally-driven trades. A 31× P/E vs an 18× peer group (Example 3) is the *evidence* behind a SELL. For a pure volatility trade (Example 5), valuation barely matters — which itself tells you what *kind* of trade it is.

### Options block: IV, IV Rank, skew, term structure  ← decides the *structure*
**Why this is the pivot of the whole sheet:**
- **Implied Volatility (IV)** — the market's expected future move, priced into options. High IV = expensive options.
- **IV Rank / percentile** — *the* number. Raw IV is useless across names (a biotech's "normal" 60% dwarfs a utility's 18%). IV Rank asks **"is IV high *for this name*, right now?"** — placing today's IV in its own 1-year range. **This single number flips the recommendation:** high IV Rank → *sell* premium (it's overpriced) → spreads/condors/covered calls; low IV Rank → *buy* premium (it's cheap) → long calls/puts/straddles. Examples 1–4 are all high-IV-Rank → they all *sell or spread*; Examples 5–6 are low → they *buy*.
- **Skew** — relative cost of downside vs upside protection; a fear gauge and a hint at which strikes are richest to sell.
- **Term structure** — near vs far IV; backwardation warns of acute near-term event risk.

> **The core insight to internalize:** *the thesis tells you the direction; IV Rank tells you the structure.* Miss this and you'll buy expensive options on a high-IV name and get crushed even when right.

---

## 3. The thesis — does the evidence above support it?
**What it says:** 2–4 sentences: direction, horizon, *why now*.

**Why it matters / how to read it:** a thesis must be **falsifiable and time-bound**, and every claim should trace back to a snapshot metric. "Margins compressing as discounters take share, broke the 200-day, IV rich" (Example 3) is checkable: margins (fundamentals), the MA break (technicals), IV Rank 80 (options). **Red flag:** a thesis that asserts things the snapshot doesn't show, or that has no "why *now*" — timing is half the trade. A correct thesis with the wrong catalyst timing still loses money (you're early = you're wrong).

---

## 4. Catalysts & risks — what resolves the bet, and what kills it
**Why it matters:** **catalysts** are *why now* — the events that force the market to agree with you (earnings, data, OPEC, a readout). No catalyst = the thesis can stay unrealized indefinitely while theta bleeds your option. **Risks** are the disconfirming scenarios; reading them tells you if the author is honest. For a *short* (Example 3), the risks section must name **short squeeze and takeout risk** — if it doesn't, the author is talking their book. **The risks also size the trade:** a binary readout (Example 5) is exactly why you'd buy a defined-risk straddle rather than short stock — the risk *is* the structure choice.

---

## 5. Positioning / smart money — is anyone else seeing it?
**Why it matters:** a thesis is stronger when **informed participants** are already acting on it. Per Harris, **informed traders are who you want to be aligned with** — they're the ones who profit. So:
- **Insider / congressional buying** — the people closest to the company/policy are voting with money.
- **Fund flows / COT** — are institutions positioned the same way (confirmation) or crowded the opposite way (squeeze risk)?
- **Unusual options activity** — large, informed-looking option bets ahead of a catalyst.

**This is the project's moat.** It's also a double-edged read: **crowded** positioning (everyone already long) means little buying left and high reversal risk. Read it both as confirmation *and* as a crowding warning.

---

## 6. Technicals — timing and risk levels
**Why it matters:** even a correct fundamental thesis needs an **entry, a stop, and a target level**. Technicals provide them:
- **Trend (MAs)** — don't fight it; "cheap" in a downtrend stays cheap.
- **Support/resistance** — where to place stops (below support) and targets (into resistance). These define the **risk/reward** the option strikes are built around.
- **Momentum (RSI) / relative strength** — confirms the move has fuel and isn't already exhausted.

Technicals rarely *make* the thesis; they **time** it and set the **levels** the recommendation's strikes key off.

---

## 7. The derivative recommendation — reading it critically
This is the conclusion. Read it in this order and sanity-check each piece:

1. **Structure + strikes + expiry** — what's the actual position?
2. **Max profit / max loss / breakeven** — the three numbers. **Always find max loss first.** Is it *defined* (a spread, debit) or *undefined* (a naked short)? Defined-risk is the default for anyone not running a desk.
3. **Does the payoff match the thesis direction?** Bullish thesis → a payoff that profits when price rises. Sounds obvious; mismatches happen.
4. **Does the structure match IV Rank?** This is the expert check. High IV Rank but the rec is *buying* a lone option? That's overpaying for vega — wrong structure even if the direction is right. The recommendation should *explain* this link (Examples 1, 3, 4 all justify spread-vs-outright by IV Rank).
5. **What's the Greek bias?** +vega means you *need* IV to rise or hold (long options); −vega/+theta means time and calm are on your side (sellers). The Greek bias must agree with the thesis: a "big move" view that's accidentally −vega is self-contradictory.
6. **Re-read the risk.** For long premium: **IV crush** (Example 5). For short premium: the **undefined tail**. For hedges: the **cost** as a % drag.

**The one-line test:** *does this structure profit in the thesis's scenario, survive the named risk, and fit today's volatility?* If yes, the argument closes. If any answer is no, the sheet contradicts itself.

---

## Reading speed-run (how a pro scans one in 60 seconds)
1. **Rating + target + horizon** — what's claimed, how much, by when.
2. **IV Rank** — jump straight here; it pre-decides buy-vs-sell premium.
3. **Max loss** on the recommendation — defined or undefined?
4. **Does direction (delta) match the thesis, and structure match IV Rank?**
5. **Liquidity ($ volume)** — can I actually get in and out?
6. **Biggest named risk** — and does the structure survive it?

If those six agree, the trade is coherent. Everything else is supporting detail.

---

## How this trains the screener
Reading a sheet well = the **logic the Trader Screener should automate**:
- The snapshot block = the **columns** to collect (shortlist §2).
- The **IV-Rank → structure** rule = the recommendation **rules engine** (see the gallery's "match action to signal" table).
- "Can I act on it?" (liquidity) = a **hard filter**, not a nice-to-have.
- "Is smart money aligned?" = the **moat** (congress/insider/UOA) surfaced as a confirmation field.

A screener that outputs a *readable, self-consistent argument* per instrument — not just numbers — is the firm-grade product.

---

*Pair with: [Tear Sheet examples](Information_Sheet_Tearsheet.md) · [Derivatives Types & Signals](Derivatives_Types_and_Trading_Signals.md) · [Examples Gallery](Derivatives_Examples_Gallery.md) · [Trading and Exchanges summary](Trading_and_Exchanges_Harris_Summary.md).*
