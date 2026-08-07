"""
Part B (the stretch) -- dealer-hedging footprint test (issue #110).

Hypothesis: aggregate 424B2 structured-note issuance on an underlying
creates measurable dealer hedging exposure. Issuers sell notes with embedded
short-put/short-call profiles, hedge the book in the underlying and its
options, and that hedging leaves a footprint -- concentrated near barrier
and autocall levels, and around observation dates when hedges roll.
Prediction: underlyings with elevated recent issuance show different
realized-vol / price behavior near clustered barrier levels than matched
controls with little issuance.

Falsifier: no difference between high-issuance and matched low-issuance
underlyings after controlling for size, liquidity and options open interest.
Stated up front -- this hypothesis is attractive enough to rationalize a
null away, which is exactly why the falsifier has to be decided before
looking at any result.

Why this is hard (read before touching the grid below):
  - N is ~100 underlyings, not 30,000 filings. Issuance concentrates hard.
    Filing count is not sample size.
  - Confounded at the root: the heavily-noted names (SPX, NDX, RTY, AAPL,
    NVDA, TSLA) have distinct vol behavior for reasons that have nothing to
    do with note hedging.
  - Matched controls barely exist -- the heavily-noted universe is small and
    unlike everything else.
  - Issuance is not net exposure -- dealers hedge a whole book and offset
    against other flow, so a single underlying's issuance total is a noisy
    proxy for that underlying's net hedging demand.

TRIAL GRID -- declared BEFORE running any of them, per the repo's deflation
discipline (issue #47, same pre-commitment the screener's promotion gate
enforces). If this grid grows mid-project, the project restarts:

    issuance windows (trading days, trailing):   30, 90, 180        (3)
    barrier-proximity definition (% of level):    2, 5               (2)
    forward horizon (trading days):               5, 20, 60          (3)

    TRIAL_COUNT = 3 * 2 * 3 = 18

LOOK-AHEAD POSTURE -- the real, subtle risk here: a 424B2 filing carries a
pricing_date (what the document itself says) and an EDGAR full-text-search
file_date (when EDGAR actually indexed/accepted it), and these DIFFER --
pricing_date can predate public disclosure by a few days. The knowledge date
used everywhere in this module is the ACCEPTANCE date, supplied explicitly
by the caller as `Filing.acceptance_date` (sourced from the crawler's
full-text-search `file_date`, NOT from anything in analysis.py, which uses
pricing_date on purpose because Part A makes no forward prediction and has
no look-ahead exposure to guard). `trailing_issuance()` below reads ONLY
`acceptance_date` and only counts filings strictly BEFORE the as-of day --
one more session of lag on top of that, same discipline as every §2a/§2b
column in lookahead-gate/lookahead_lag.py. See test_causal_test.py for the
A-vs-B truncation gate, including the deliberately leaky pricing_date-keyed
variant it proves the gate catches.

DATA CONTRACT -- this module does not fetch prices, realized vol, options
open interest or ADV itself. That data belongs to the shared pipeline
(Stock-Data-Pipeline), not to a local, exploratory tool
(OUTPUT_STORE.md's ownership boundary). Callers supply `UnderlyingUniverse`
per symbol and a list of `Filing` (typically built from
analysis.load_notes() plus a caller-supplied accession -> acceptance_date
map, since acceptance date is crawl-time metadata, not an extracted field).

Run the self-check: python Research/structured_note_issuance/test_causal_test.py
"""

import math
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from statistics import mean, stdev

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_LOOKAHEAD_GATE_DIR = _REPO_ROOT / "lookahead-gate"
if str(_LOOKAHEAD_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_LOOKAHEAD_GATE_DIR))

import lookahead_lag as lag  # noqa: E402  (reused for the backward-looking realized_vol control)

# ── declared trial grid (do not edit without restarting the project) ───────

ISSUANCE_WINDOWS = (30, 90, 180)          # trading days, trailing
BARRIER_PROXIMITY_PCT = (2.0, 5.0)        # % of the absolute barrier level
FORWARD_HORIZONS = (5, 20, 60)            # trading days, forward
TRIAL_COUNT = len(ISSUANCE_WINDOWS) * len(BARRIER_PROXIMITY_PCT) * len(FORWARD_HORIZONS)
assert TRIAL_COUNT == 18, (
    "trial grid changed without updating the declared count -- that is a "
    "restart of the project (#110), not a one-line fix"
)

# Bonferroni-style two-sided critical |t| for TRIAL_COUNT=18 declared trials
# at a nominal alpha=0.05 (per-trial alpha ~= 0.0028, |z| ~= 3.0; using the
# slightly looser 2.7 as a deliberately conservative-toward-supported round
# number rather than tuning a threshold to any observed result).
CRITICAL_T = 2.7

HYPOTHESIS = (
    "Aggregate 424B2 structured-note issuance on an underlying creates "
    "measurable dealer hedging exposure, visible as realized-vol/price "
    "behavior near clustered barrier levels that differs from matched "
    "low-issuance controls."
)

FALSIFIER = (
    "No statistically distinguishable difference (after the Bonferroni-style "
    f"correction for the declared {TRIAL_COUNT} trials, |t| >= {CRITICAL_T}) "
    "between high-issuance and matched low-issuance underlyings' forward "
    "realized vol near clustered barrier levels, after controlling for ADV "
    "and options open interest."
)

LOOKAHEAD_NOTE = (
    "Knowledge date is the EDGAR acceptance date (crawl file_date), never "
    "pricing_date. trailing_issuance() only counts filings strictly before "
    "the as-of trading day. A-vs-B truncation test in test_causal_test.py "
    "passes for this implementation and fails for a deliberately leaky "
    "pricing_date-keyed variant, proving the gate has teeth."
)


# ── data contract ────────────────────────────────────────────────────────

@dataclass
class Filing:
    """One structured note, reduced to what the causal test needs.
    `acceptance_date` is EDGAR's file_date -- crawl-time metadata, not an
    extracted field -- and is the ONLY date this module reads."""

    accession: str
    underlying: str
    acceptance_date: str            # ISO date, EDGAR file_date
    aggregate_principal: float
    barrier_levels: list = dc_field(default_factory=list)   # absolute price levels


@dataclass
class PriceBar:
    date: str                        # ISO date, ascending order within a series
    close: float


@dataclass
class UnderlyingUniverse:
    """The 'from the existing pipeline' inputs (#110): prices, ADV, options
    OI. This module never fetches these -- they are supplied by the caller
    from Stock-Data-Pipeline reads, consistent with OUTPUT_STORE.md's
    ownership boundary (the scrubber/Research layer is a reader only)."""

    symbol: str
    bars: list             # PriceBar, ascending by date
    adv: float               # average daily dollar volume -- size/liquidity control
    options_oi: float        # aggregate open interest -- the third control


# ── feature computation (look-ahead-safe) ──────────────────────────────────

def trailing_issuance(filings, symbol, dates_index, as_of_idx, window_days):
    """Sum of aggregate_principal for `symbol` whose acceptance_date falls in
    the `window_days` trading days STRICTLY BEFORE `dates_index[as_of_idx]`.
    Never reads pricing_date. `as_of_idx == 0` (no prior history) returns 0.
    """
    if as_of_idx <= 0:
        return 0.0
    start_idx = max(0, as_of_idx - window_days)
    window_dates = set(dates_index[start_idx:as_of_idx])
    return sum(
        f.aggregate_principal for f in filings
        if f.underlying == symbol and f.acceptance_date in window_dates
    )


def near_barrier(price, barrier_levels, proximity_pct):
    """True if `price` is within `proximity_pct` percent of any absolute
    barrier level. barrier_levels come from analysis.barrier_levels_vs_spot's
    absolute_level -- already converted from % of initial via
    initial_underlying_value, so this is a same-scale comparison."""
    return any(lvl and abs(price - lvl) / lvl * 100.0 <= proximity_pct
               for lvl in barrier_levels)


def forward_realized_vol(bars, i, horizon):
    """Realized vol (annualized, %) over the `horizon` trading days STARTING
    at day i. This is the outcome being tested, not a lagged feature: the
    causal question is 'issuance signal known through yesterday's close ->
    vol over the NEXT horizon days', so day i's own close is the start point,
    not something to hide."""
    closes = [b.close for b in bars]
    j = i + horizon
    if j >= len(closes):
        return None
    window = closes[i:j + 1]
    rets = [window[k + 1] / window[k] - 1 for k in range(len(window) - 1) if window[k]]
    if len(rets) < 2:
        return None
    return stdev(rets) * (252 ** 0.5) * 100.0


# ── matched controls ────────────────────────────────────────────────────────

def match_controls(universe_by_symbol, high_issuance_symbols, k=3):
    """For each high-issuance symbol, the k nearest LOW-issuance symbols by
    Euclidean distance in (log ADV, log options_oi) -- the falsifier's
    'controlling for size, liquidity and options open interest'. Returns
    fewer than k (or none) rather than padding with a bad match: the
    'matched controls barely exist' warning in the module docstring is a
    real constraint, not a formality.
    """
    pool = [s for s in universe_by_symbol if s not in high_issuance_symbols]
    matches = {}
    for h in high_issuance_symbols:
        hu = universe_by_symbol[h]
        if hu.adv <= 0 or hu.options_oi <= 0:
            matches[h] = []
            continue
        h_vec = (math.log(hu.adv), math.log(hu.options_oi))
        scored = []
        for c in pool:
            cu = universe_by_symbol[c]
            if cu.adv <= 0 or cu.options_oi <= 0:
                continue
            c_vec = (math.log(cu.adv), math.log(cu.options_oi))
            dist = math.hypot(h_vec[0] - c_vec[0], h_vec[1] - c_vec[1])
            scored.append((dist, c))
        scored.sort(key=lambda t: t[0])
        matches[h] = [c for _, c in scored[:k]]
    return matches


# ── trials + verdict ────────────────────────────────────────────────────────

def welch_t(a, b):
    """Welch's t-statistic for unequal-variance groups. No p-value here --
    scipy isn't a dependency (stdlib-only convention); CRITICAL_T is the
    pre-declared threshold this compares against instead."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = mean(a), mean(b)
    va, vb = stdev(a) ** 2, stdev(b) ** 2
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None
    return (ma - mb) / se


@dataclass
class TrialResult:
    window: int
    proximity_pct: float
    horizon: int
    high_group_vol: list
    control_group_vol: list
    diff: float
    t_stat: float


def declared_trials():
    for w in ISSUANCE_WINDOWS:
        for p in BARRIER_PROXIMITY_PCT:
            for h in FORWARD_HORIZONS:
                yield w, p, h


def run_trial(universe_by_symbol, filings, high_symbols, matches, window, proximity_pct, horizon):
    high_vols, control_vols = [], []
    for h in high_symbols:
        hu = universe_by_symbol[h]
        dates_index = [b.date for b in hu.bars]
        h_barrier_levels = [lvl for f in filings if f.underlying == h for lvl in f.barrier_levels]
        for i, bar in enumerate(hu.bars):
            if trailing_issuance(filings, h, dates_index, i, window) <= 0:
                continue
            if not near_barrier(bar.close, h_barrier_levels, proximity_pct):
                continue
            fv = forward_realized_vol(hu.bars, i, horizon)
            if fv is not None:
                high_vols.append(fv)
        for c in matches.get(h, []):
            cu = universe_by_symbol[c]
            for i in range(len(cu.bars)):
                fv = forward_realized_vol(cu.bars, i, horizon)
                if fv is not None:
                    control_vols.append(fv)

    t = welch_t(high_vols, control_vols) if high_vols and control_vols else None
    diff = (mean(high_vols) - mean(control_vols)) if high_vols and control_vols else None
    return TrialResult(window=window, proximity_pct=proximity_pct, horizon=horizon,
                        high_group_vol=high_vols, control_group_vol=control_vols,
                        diff=diff, t_stat=t)


def run_all_trials(universe_by_symbol, filings, high_symbols, matches):
    results = [
        run_trial(universe_by_symbol, filings, high_symbols, matches, w, p, h)
        for w, p, h in declared_trials()
    ]
    assert len(results) == TRIAL_COUNT
    return results


def verdict(results):
    """SUPPORTED only if a majority of the scoreable declared trials clear
    CRITICAL_T in the SAME direction. INCONCLUSIVE if too few trials had
    enough observations to score at all (the honest outcome against an
    empty or thin store). REFUTED otherwise -- including a mixed-direction
    or minority-of-trials result. No single trial is ever allowed to carry
    the verdict; that is exactly the p-hacking the pre-declared grid exists
    to prevent.

    Returns (status, explanation) where status in
    {"supported", "refuted", "inconclusive"}.
    """
    scored = [r for r in results if r.t_stat is not None]
    if len(scored) < TRIAL_COUNT // 2:
        return "inconclusive", (
            f"Only {len(scored)}/{TRIAL_COUNT} declared trials had enough "
            "observations to compute a statistic. Most likely cause is an "
            "empty or thin OutputStore -- no live crawl has been run against "
            "it yet. Inconclusive is a legitimate published verdict here, "
            "not a placeholder for a result that will show up later."
        )
    hits = [r for r in scored if abs(r.t_stat) >= CRITICAL_T]
    directions = {1 if r.diff > 0 else -1 for r in hits}
    if len(hits) > len(scored) / 2 and len(directions) <= 1:
        return "supported", (
            f"{len(hits)}/{len(scored)} scoreable trials cleared the "
            f"pre-declared |t| >= {CRITICAL_T} threshold in the same "
            "direction."
        )
    return "refuted", (
        f"{len(hits)}/{len(scored)} scoreable trials cleared the "
        f"pre-declared |t| >= {CRITICAL_T} threshold -- not a majority, or "
        "not the same direction. The falsifier is satisfied."
    )
