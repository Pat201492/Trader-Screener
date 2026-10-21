"""
Part B — dealer-hedging causal test (issue #131; carved out of #110 by scope
triage). Registered in the Research registry (web-dashboard/research-projects.json,
id="dealer-hedging-barrier-causal-test") BEFORE this script ran a single trial —
this module is the single source of truth for the hypothesis/falsifier/trial
grid/look-ahead posture the registry entry quotes; if you change one of the
constants below, update the registry entry to match, don't let them drift.

HYPOTHESIS: underlyings with elevated recent issuance of barrier-linked
structured notes (an EDGAR-424B2-style filing burst in the trailing issuance
window) show different realized-vol / price behavior near their clustered
barrier levels than matched low-issuance controls, after controlling for
underlying size, liquidity (ADV), and options open interest.

FALSIFIER (stated up front, per #131): after propensity-style matching on
size/liquidity/OI, no statistically- and economically-meaningful difference
in forward realized vol or barrier-pinning rate between elevated-issuance and
matched low-issuance observations -> refuted. This is the null; the burden of
proof is on finding a difference, not on finding none.

TRIAL GRID (declared before the first run; growing this count restarts the
project under a new registry id, it does not append silently to this one):
    issuance windows (trailing days of issuance lookback)   x 3
    barrier-proximity definitions (distance to nearest live
        barrier, as a fraction of spot)                     x 3
    forward horizons (trading days the outcome is measured
        over)                                                x 3
    = 27 declared trials, see TRIAL_COUNT below.

LOOK-AHEAD POSTURE: "lag-prior-close". Every signal-side quantity (issuance
intensity, near-barrier flag, the matching covariates ADV/OI) is computed
from information strictly before trading day i -- day i's own bar is never
read (mirrors lookahead-gate/lookahead_lag.py's discipline; this module
brings its own truncation-gate self-check below rather than folding into
that module, which is explicitly scoped to OHLCV-derived screener columns).
Critically: the knowledge date for an issuance event is EDGAR's ACCEPTANCE
timestamp (`filing_idx` below -- the 424B2 hitting EDGAR, T+1/T+2 after terms
price in the real world), never `pricing_idx` (the earlier date the note's
terms were set). Using pricing_date as if it were the public-knowledge date
would smuggle in information nobody outside the desk had yet --
`run_knowledge_date_check()` asserts this directly, the way
`run_same_day_peek_check()` in ab_truncation_test.py asserts same-day-peek
directly: the plain A-vs-B truncation test alone cannot catch a
wrong-but-still-past date field, only an absolute future-index read.

SYNTHETIC FIXTURE (the mechanism check; real data is the REAL DATA section
below). Written when this repo had no live pipeline connection (Stock-Data-Pipeline is
a separate, not-checked-out repo; ARCHITECTURE.md's "single writer" rule means
this can't fork that ingest). Per the same posture as lookahead-gate/ and
pipeline-mock/, this module is a portable, stdlib-only, deterministically
seeded REFERENCE implementation: a synthetic universe standing in for (a)
scrubber-shaped issuance events and (b) pipeline-shaped prices/realized-vol/
options-OI/ADV. (b) has no local mock module to consume (pipeline-mock/ is a
network server, not an importable one) so it stays inline-synthetic; (a) does
have a local, stdlib+sqlite, zero-network mock -- tools/edgar_scrubber/
output_store.py's OutputStore(":memory:") -- so this module's own issuance
events are round-tripped through it for real: written with write_document(),
read back with query()/fields(), the tool interface only (#109), same pattern
tools/edgar_scrubber/research_maps.py uses for real scrubber output. See
_issuance_events_via_output_store() below. Not tuned to make the hypothesis
win or lose -- the canonical run uses a NEUTRAL fixture (effect_strength=0.0,
no mechanical link planted between issuance and forward behavior); a separate
power self-check with a DELIBERATELY planted effect proves the statistical
machinery would detect a real one if it existed, so a "refuted" verdict from
the neutral fixture means the test has no teeth to bite with, not that it has
no teeth at all.

OUTPUT_STORE GAP (pipeline TODO, not worked around here): OutputStore's
`documents.filing_date` is a single denormalized column, and
`DocumentExtraction.from_record()` defaults its source key to `pricing_date`
(output_store.py:174) -- the store has no native pricing-vs-acceptance-date
distinction. This module needs exactly that distinction (see LOOK-AHEAD
POSTURE above), so it passes `filing_date_field="filing_idx"` explicitly to
get acceptance-date semantics onto the document dimension, and keeps
filing_idx/pricing_idx as their own field rows (read back via fields(), not
the denormalized column) for the knowledge-date check to gate on. A real
scrubber extraction would want a proper `filing_date` (EDGAR acceptance,
ISO) alongside `pricing_date` as two first-class fields -- worth fixing in
output_store.py when a real 424B2 spec needs it, not a reason to bypass the
interface here.

REAL DATA (#341): `real_universe()` builds the same universe shape from the
dataset `dealer_hedging_data.py` builds (424B2 counts per EDGAR file date,
sampled barrier levels) and the pipeline's daily closes/volume. Issuance
intensity there is the daily FILING COUNT (aggregate principal is rarely
stated), barriers are the sampled notes', and the placebo is a circular time
shift of each ticker's own issuance series (`real_placebo_universe`) -- the
synthetic placebo universes are a different universe and the wrong null for a
real one. Matching uses options OI only where point-in-time OI exists;
otherwise size + ADV, and the result says so (`matched_on`).

Run:  python Research/dealer_hedging_causal_test.py            (mechanism check, synthetic)
      python Research/dealer_hedging_causal_test.py --real     (real data -> results JSON)
Exit code 0 = self-checks passed and the declared trial grid ran to a
published verdict (confirmed/refuted/inconclusive -- all three are a pass).
Non-zero = a self-check (look-ahead gate or power check) failed, which blocks
publishing a verdict at all, same convention as ab_truncation_test.py.
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lookahead-gate"))
import lookahead_lag as lag  # noqa: E402  (path insert must precede this import)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "edgar_scrubber"))
import output_store as scrubber_store  # noqa: E402  (path insert must precede this import)


# ── Declared up front (issue #131 posture) ──────────────────────────────────

HYPOTHESIS = (
    "Underlyings with elevated recent issuance of barrier-linked structured "
    "notes show different realized-vol/price behavior near their clustered "
    "barrier levels than matched low-issuance controls, after controlling for "
    "underlying size, liquidity (ADV), and options open interest."
)
FALSIFIER = (
    "No statistically- (p<0.05) and economically- (>=0.2 vol-pts realized-vol "
    "or >=5pp pinning-rate) meaningful, consistently-signed difference between "
    "elevated-issuance and matched low-issuance observations, in excess of what "
    "the declared 27-trial grid produces on independently-seeded NULL (no true "
    "effect) placebo universes -> refuted. The 27 trials share overlapping "
    "day-ranges/tickers across window/horizon/proximity cells, so they are NOT "
    "independent; a naive alpha*N chance floor understates the true false-"
    "positive rate under that correlation (confirmed empirically below -- the "
    "first canonical run without placebo calibration produced 7/18 confirming "
    "trials, all consistently signed, against a fixture with NO planted causal "
    "link at all). The bar is therefore calibrated against PLACEBO_SEEDS "
    "independently-seeded neutral runs, not assumed from independence."
)

ISSUANCE_WINDOWS = (10, 30, 60)                 # trailing trading days, issuance lookback
BARRIER_PROXIMITY_PCTS = (0.01, 0.025, 0.05)    # "near a barrier" distance-to-spot fractions
HORIZONS = (5, 10, 20)                          # forward trading days the outcome is measured over
TRIAL_COUNT = len(ISSUANCE_WINDOWS) * len(BARRIER_PROXIMITY_PCTS) * len(HORIZONS)
assert TRIAL_COUNT == 27, "trial grid changed -- register a NEW project id, do not silently grow this one"

ALPHA = 0.05
MIN_EFFECT_VOL = 0.002     # min meaningful |diff| in forward realized vol (daily-return stdev units)
MIN_EFFECT_PIN = 0.05      # min meaningful |diff| in forward barrier-pinning rate (fraction)
MIN_MATCHED_PAIRS = 20     # below this a trial is "underpowered", excluded from the confirm/refute tally
PLACEBO_SEEDS = tuple(f"placebo-{k}" for k in range(8))  # independent NULL universes, empirical calibration
LOOKAHEAD_POSTURE = "lag-prior-close"

N_UNDERLYINGS = 30
N_DAYS = 504  # ~2 trading years


# ── Synthetic universe (portable reference; see module docstring) ──────────

def make_universe(seed, effect_strength=0.0):
    """Build a synthetic multi-underlying universe of OHLCV bars, ADV, options
    total OI, and a book of barrier-linked note issuance events per underlying.

    effect_strength: 0.0 = neutral fixture, no mechanical link between issuance
    bursts and forward behavior near a barrier (used for the canonical
    registered run). >0.0 mechanically suppresses forward realized vol and
    raises the forward barrier-pinning rate after a treatment-eligible
    observation (used ONLY by run_power_check(), to prove the statistical
    procedure can detect a real effect when one exists).

    Returns {ticker: {"bars": [...], "adv": [...], "total_oi": [...],
    "mkt_cap": float, "notes": [{"filing_idx", "pricing_idx", "size_usd",
    "barrier": float}, ...], "treatment_days": set(int)}}. The "notes" book is
    round-tripped through an in-memory OutputStore before it's attached here
    -- see _issuance_events_via_output_store().
    """
    universe = {}
    notes_by_ticker = {}
    for t_i in range(N_UNDERLYINGS):
        tk = f"U{t_i:03d}"
        rnd = random.Random(f"{seed}:{tk}")
        tier = rnd.choice(["mega", "large", "small"])
        price0 = {"mega": rnd.uniform(150, 500), "large": rnd.uniform(50, 150),
                  "small": rnd.uniform(10, 50)}[tier]
        adv0 = {"mega": rnd.uniform(1e7, 4e7), "large": rnd.uniform(2e6, 1e7),
                "small": rnd.uniform(1e5, 2e6)}[tier]
        mkt_cap = price0 * adv0 * rnd.uniform(80, 200)  # crude size proxy, not literal shares out
        oi0 = adv0 * rnd.uniform(0.02, 0.08)

        bars, closes = [], []
        price = price0
        for d in range(N_DAYS):
            drift = rnd.gauss(0.0002, 0.016)
            price = max(0.5, price * (1 + drift))
            o = price * (1 + rnd.gauss(0, 0.002))
            c = price
            hi = max(o, c) * (1 + abs(rnd.gauss(0, 0.004)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
            bars.append({"date": f"D{d:04d}", "open": o, "high": hi, "low": lo,
                         "close": c, "volume": adv0 * (1 + rnd.gauss(0, 0.15))})
            closes.append(c)

        adv = [adv0 * (1 + rnd.gauss(0, 0.05)) for _ in range(N_DAYS)]
        total_oi = [max(0.0, oi0 * (1 + rnd.gauss(0, 0.10))) for _ in range(N_DAYS)]

        # Issuance book: a handful of notes over the 2-year window, each with
        # its own barrier fixed at issuance (standard autocallable/barrier-note
        # shape: barrier = pct of the note's OWN reference price, not spot at
        # observation time). pricing_idx < filing_idx by a realistic EDGAR
        # T+1/T+2 lag -- the fixture this module's knowledge-date check needs.
        notes = []
        n_notes = rnd.randint(3, 9)
        for _ in range(n_notes):
            pricing_idx = rnd.randint(5, N_DAYS - 30)
            filing_idx = pricing_idx + rnd.randint(1, 2)
            if filing_idx >= N_DAYS:
                continue
            ref_price = closes[pricing_idx]
            knockin_pct = rnd.uniform(0.60, 0.85)
            notes.append({
                "filing_idx": filing_idx,
                "pricing_idx": pricing_idx,
                "size_usd": rnd.uniform(1e6, 25e6),
                "barrier": ref_price * knockin_pct,
            })
        notes_by_ticker[tk] = notes

        universe[tk] = {
            "bars": bars, "closes": closes, "adv": adv, "total_oi": total_oi,
            "mkt_cap": mkt_cap, "treatment_days": set(),
        }

    # Consumed through the scrubber's tool interface only (#109), not this
    # module's own ad hoc shape -- see module docstring's DATA SOURCE note.
    notes_by_ticker = _issuance_events_via_output_store(notes_by_ticker)
    for tk, notes in notes_by_ticker.items():
        universe[tk]["notes"] = notes

    if effect_strength > 0:
        _plant_effect(universe, effect_strength)
    return universe


def _issuance_events_via_output_store(notes_by_ticker):
    """Round-trip the synthetic issuance book through a fresh in-memory
    OutputStore, the same way tools/edgar_scrubber/research_maps.py reads
    real scrubber output: write_document() to load, query()/fields() to
    read back -- never the sqlite file or an ad hoc dict shape directly.

    `notes_by_ticker`: {ticker: [{"filing_idx", "pricing_idx", "size_usd",
    "barrier"}, ...]} as built in make_universe(). Returns the same shape,
    sorted by filing_idx per ticker, but every value has passed through the
    store's public read surface.

    filing_date_field="filing_idx" makes the ACCEPTANCE date (not
    pricing_date) the document's queryable knowledge date -- see the
    OUTPUT_STORE GAP note in the module docstring. filing_idx/pricing_idx
    are also kept as their own field rows (read back via fields()) since
    this module's knowledge-date check needs the raw pair, not just the
    denormalized column.
    """
    store = scrubber_store.OutputStore(":memory:")
    run_id = store.start_run(
        "synthetic.dealer_hedging_fixture", "1.0.0", "2026-08-05T00:00:00Z",
        note="synthetic reference universe (issue #131), not a real crawl",
    )
    for tk, notes in notes_by_ticker.items():
        for idx, n in enumerate(notes):
            record = {
                "issuer": f"{tk}-issuer",
                "product_type": "barrier_note",
                "underlyings": [{"name": tk, "kind": "equity"}],
                "filing_idx": n["filing_idx"],
                "pricing_idx": n["pricing_idx"],
                "size_usd": n["size_usd"],
                "barrier": n["barrier"],
            }
            doc = scrubber_store.DocumentExtraction.from_record(
                f"{tk}-note-{idx:03d}", "synthetic.txt", record,
                filing_date_field="filing_idx",
            )
            store.write_document(run_id, doc)

    out = {}
    for tk in notes_by_ticker:
        read_notes = []
        for d in store.query(underlying=tk):
            values = {r["field"]: r["value"]
                      for r in store.fields(d["accession"], d["document"])}
            read_notes.append({
                "filing_idx": values["filing_idx"],
                "pricing_idx": values["pricing_idx"],
                "size_usd": values["size_usd"],
                "barrier": values["barrier"],
            })
        out[tk] = sorted(read_notes, key=lambda n: n["filing_idx"])
    store.close()
    return out


def _plant_effect(universe, effect_strength):
    """Mechanically dampen realized vol and raise barrier-pinning immediately
    after a treatment-eligible day, for the power self-check ONLY. Never used
    by the canonical registered run (effect_strength=0.0 there)."""
    for tk, u in universe.items():
        closes = u["closes"]
        for i in range(60, N_DAYS - 20):
            known_notes = [n for n in u["notes"] if n["filing_idx"] <= i - 1]
            if not known_notes:
                continue
            spot = closes[i - 1]
            nearest = min(abs(spot - n["barrier"]) / spot for n in known_notes)
            recent_size = sum(n["size_usd"] for n in known_notes if n["filing_idx"] >= i - 30)
            if nearest <= 0.05 and recent_size > 8e6:
                u["treatment_days"].add(i)
                # Pull the next few closes toward the nearest barrier and damp
                # their day-to-day moves -- a crude stand-in for a dealer
                # gamma-hedging flow that pins spot near a barrier it is short
                # gamma against.
                barrier = min(known_notes, key=lambda n: abs(spot - n["barrier"]) / spot)["barrier"]
                for j in range(i, min(i + 20, N_DAYS)):
                    pull = (barrier - closes[j]) * effect_strength * 0.05
                    closes[j] += pull
                    u["bars"][j]["close"] = closes[j]


# ── Signal-side functions (must never read day i or later) ─────────────────

def issuance_intensity_percentile(universe, tickers, i, window):
    """{tk: {"sum": trailing-window issued size, "percentile": rank 0-100}}
    as of the prior close. `percentile` ranks ONLY among tickers with a
    NONZERO trailing sum that day -- most (ticker, day) cells have zero
    recent issuance (each underlying carries only a handful of notes over 2
    years), and cross_sectional_rank's stable-sort tie-break would otherwise
    silently hand a high percentile to whichever zero-sum ticker happens to
    sort last among the ties -- a tie-break artifact, not a signal. A
    zero-sum ticker's percentile is None; callers should split on `sum`
    directly for zero vs nonzero, not on `percentile` alone. Recomputed FRESH
    each day across tickers (never against a fixed/pooled distribution), same
    discipline as lookahead_lag.cross_sectional_rank. Only counts notes whose
    `filing_idx` (EDGAR acceptance -- the knowledge date) is < i."""
    sums = {}
    for tk in tickers:
        u = universe[tk]
        if "issuance_cum" in u:
            # Real data: daily 424B2 counts by EDGAR file date; a day's count
            # is known after that day, so the window is days [i-window, i).
            cum = u["issuance_cum"]
            sums[tk] = cum[i] - cum[max(0, i - window)]
        else:
            sums[tk] = sum(n["size_usd"] for n in u["notes"] if i - window <= n["filing_idx"] < i)
    ranks = lag.cross_sectional_rank({tk: s for tk, s in sums.items() if s > 0})
    return {tk: {"sum": s, "percentile": ranks.get(tk)} for tk, s in sums.items()}


def near_barrier(universe, tk, i, proximity_pct):
    """True if the prior close is within `proximity_pct` of the nearest LIVE
    barrier (a note whose filing_idx < i) on this underlying. Reads spot at
    i-1 only; never i."""
    if i - 1 < 0:
        return False
    known = [n for n in universe[tk]["notes"] if n["filing_idx"] < i]
    if not known:
        return False
    spot = universe[tk]["closes"][i - 1]
    nearest = min(abs(spot - n["barrier"]) / spot for n in known)
    return nearest <= proximity_pct


def matching_covariates(universe, tk, i):
    """(log ADV, log total OI, log mkt_cap) as of the prior close -- the
    size/liquidity/OI controls #131 requires. i-1 only. A universe without
    point-in-time OI (`total_oi` None, the real data until the pipeline's OI
    history has accumulated) matches on (log ADV, log mkt_cap) only."""
    u = universe[tk]
    adv = u["adv"][i - 1]
    cap = u["mkt_cap"]
    if u.get("total_oi") is None:
        return (math.log(max(adv, 1.0)), math.log(max(cap, 1.0)))
    oi = u["total_oi"][i - 1]
    return (math.log(max(adv, 1.0)), math.log(max(oi, 1.0)), math.log(max(cap, 1.0)))


# ── Outcome-side functions (forward window -- allowed to read the future;
#    that is the entire point of a predictive/causal test) ─────────────────

def forward_realized_vol(closes, i, horizon):
    if i + horizon >= len(closes):
        return None
    rets = [closes[j] / closes[j - 1] - 1 for j in range(i, i + horizon)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    return var ** 0.5


def forward_pinned(universe, tk, i, horizon, proximity_pct):
    """1 if the close `horizon` days out is still within proximity_pct of the
    nearest barrier known as of i-1 (the barrier the note was hedged against),
    else 0. None if out of range."""
    closes = universe[tk]["closes"]
    if i + horizon >= len(closes):
        return None
    known = [n for n in universe[tk]["notes"] if n["filing_idx"] < i]
    if not known:
        return None
    barrier = min(known, key=lambda n: abs(closes[i - 1] - n["barrier"]) / closes[i - 1])["barrier"]
    end = closes[i + horizon]
    return 1.0 if abs(end - barrier) / end <= proximity_pct else 0.0


# ── Stats (stdlib only -- normal approximation via math.erf, same technique
#    pipeline-mock/sample_data.py uses for its Black-Scholes normal CDF) ────

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def paired_diff_test(diffs):
    """One-sample (paired) z-test on a list of treatment-minus-control
    differences. Returns (mean_diff, p_value) or (None, None) if too few."""
    n = len(diffs)
    if n < 2:
        return None, None
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se == 0:
        return mean, (0.0 if mean != 0 else 1.0)
    z = mean / se
    p = 2 * (1 - _norm_cdf(abs(z)))
    return mean, p


# ── Matching: nearest-neighbor on (log adv, log oi, log mkt_cap), with
#    replacement, caliper 0.25 standardized units -- lightweight propensity
#    matching, declared here (not tuned per trial) ──────────────────────────

CALIPER = 0.25


def match_treatment_to_controls(treated, controls):
    """treated/controls: [{"cov": (a,b,c), "vol": ..., "pin": ...}, ...].
    Standardizes covariates across the pooled sample, then for each treated
    obs finds the nearest control within CALIPER (Euclidean, standardized).
    Returns matched (treated_vol, control_vol, treated_pin, control_pin) triples."""
    pool = treated + controls
    if not pool:
        return []
    dims = len(pool[0]["cov"])
    means = [sum(o["cov"][d] for o in pool) / len(pool) for d in range(dims)]
    stds = [
        math.sqrt(sum((o["cov"][d] - means[d]) ** 2 for o in pool) / max(1, len(pool) - 1)) or 1.0
        for d in range(dims)
    ]

    def standardize(cov):
        return tuple((cov[d] - means[d]) / stds[d] for d in range(dims))

    # Controls sorted on the first standardized covariate: one further than
    # CALIPER on that axis is further than CALIPER overall, so only the window
    # [x0 - CALIPER, x0 + CALIPER] can hold a match (#357; the full scan was
    # O(treated x controls) and took hours on the 3-year universe). Ties break
    # on the control's original position -- the full scan's order -- so the
    # pairs are identical to it.
    import bisect
    ctrl_std = sorted(((standardize(c["cov"]), k, c) for k, c in enumerate(controls)),
                      key=lambda x: x[0][0])
    keys = [x[0][0] for x in ctrl_std]
    pairs = []
    for t in treated:
        t_std = standardize(t["cov"])
        lo = bisect.bisect_left(keys, t_std[0] - CALIPER)
        hi = bisect.bisect_right(keys, t_std[0] + CALIPER)
        best, best_key = None, None
        for c_std, k, c in ctrl_std[lo:hi]:
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(t_std, c_std)))
            if best_key is None or (dist, k) < best_key:
                best, best_key = c, (dist, k)
        if best is not None and best_key[0] <= CALIPER:
            pairs.append((t, best))
    return pairs


def _match_full_scan(treated, controls):
    """The original O(treated x controls) matcher, kept as the reference the
    pruned one is tested against (#357)."""
    pool = treated + controls
    if not pool:
        return []
    dims = len(pool[0]["cov"])
    means = [sum(o["cov"][d] for o in pool) / len(pool) for d in range(dims)]
    stds = [math.sqrt(sum((o["cov"][d] - means[d]) ** 2 for o in pool) / max(1, len(pool) - 1)) or 1.0
            for d in range(dims)]
    std = lambda cov: tuple((cov[d] - means[d]) / stds[d] for d in range(dims))
    ctrl_std = [(std(c["cov"]), c) for c in controls]
    pairs = []
    for t in treated:
        t_std = std(t["cov"])
        best, best_dist = None, None
        for c_std, c in ctrl_std:
            dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(t_std, c_std)))
            if best_dist is None or dist < best_dist:
                best, best_dist = c, dist
        if best is not None and best_dist <= CALIPER:
            pairs.append((t, best))
    return pairs


# ── One trial ────────────────────────────────────────────────────────────

# Who counts as a low-issuance control. "zero": no issuance in the window --
# the synthetic fixture's rule, where issuance is sparse. "low_tercile": zero,
# or the bottom third of that day's cross-sectional rank -- the real-data rule
# (#342): nearly every real underlying has 424B2 filings every week, so the
# first real run found 0-48 zero-issuance controls per trial and scored no
# trial at all. Declared from that feasibility count alone; no outcome had
# been computed (no trial reached a single test).
CONTROL_RULES = ("zero", "low_tercile")


def run_trial(universe, tickers, issuance_window, proximity_pct, horizon, control="zero", obs_days=None):
    """Build treatment/control observation pools, match, and test both
    outcomes for one (issuance_window, proximity_pct, horizon) cell."""
    assert control in CONTROL_RULES
    treated_obs, control_obs = [], []
    n_days = len(universe[tickers[0]]["closes"])
    for i in range(70, n_days - horizon):
        if obs_days is not None and i not in obs_days:
            continue
        percentiles = issuance_intensity_percentile(universe, tickers, i, issuance_window)
        for tk in tickers:
            if not near_barrier(universe, tk, i, proximity_pct):
                continue
            entry = percentiles[tk]
            vol = forward_realized_vol(universe[tk]["closes"], i, horizon)
            pin = forward_pinned(universe, tk, i, horizon, proximity_pct)
            if vol is None or pin is None:
                continue
            obs = {"cov": matching_covariates(universe, tk, i), "vol": vol, "pin": pin}
            if entry["sum"] > 0 and entry["percentile"] is not None and entry["percentile"] >= 66.7:
                treated_obs.append(obs)
            elif entry["sum"] == 0 or (control == "low_tercile" and entry["percentile"] is not None
                                       and entry["percentile"] <= 33.3):
                control_obs.append(obs)

    pairs = match_treatment_to_controls(treated_obs, control_obs)
    n_pairs = len(pairs)
    if n_pairs < MIN_MATCHED_PAIRS:
        return {"n_treated": len(treated_obs), "n_control": len(control_obs),
                "n_pairs": n_pairs, "underpowered": True}

    vol_diffs = [t["vol"] - c["vol"] for t, c in pairs]
    pin_diffs = [t["pin"] - c["pin"] for t, c in pairs]
    vol_mean, vol_p = paired_diff_test(vol_diffs)
    pin_mean, pin_p = paired_diff_test(pin_diffs)

    vol_hit = vol_p is not None and vol_p < ALPHA and abs(vol_mean) >= MIN_EFFECT_VOL
    pin_hit = pin_p is not None and pin_p < ALPHA and abs(pin_mean) >= MIN_EFFECT_PIN

    # Direction buckets by sign of whichever outcome hit (pin takes priority
    # when both hit -- either way, two trials only count as "consistent" if
    # they moved the SAME way, not merely if either outcome moved at all.
    if pin_hit:
        direction = "pin_elevated" if pin_mean > 0 else "pin_suppressed"
    elif vol_hit:
        direction = "vol_elevated" if vol_mean > 0 else "vol_suppressed"
    else:
        direction = "none"

    return {
        "n_treated": len(treated_obs), "n_control": len(control_obs), "n_pairs": n_pairs,
        "underpowered": False,
        "vol_mean_diff": vol_mean, "vol_p": vol_p, "vol_hit": vol_hit,
        "pin_mean_diff": pin_mean, "pin_p": pin_p, "pin_hit": pin_hit,
        "confirms_difference": bool(vol_hit or pin_hit),
        "direction": direction,
    }


# ── Full declared grid + verdict ─────────────────────────────────────────

def run_declared_grid(universe, control="zero", obs_days=None):
    tickers = list(universe.keys())
    results = []
    for window in ISSUANCE_WINDOWS:
        for prox in BARRIER_PROXIMITY_PCTS:
            for horizon in HORIZONS:
                r = run_trial(universe, tickers, window, prox, horizon, control=control,
                              obs_days=obs_days)
                r.update(issuance_window=window, proximity_pct=prox, horizon=horizon)
                results.append(r)
    assert len(results) == TRIAL_COUNT
    return results


def run_placebo_calibration(seeds=PLACEBO_SEEDS):
    """Run the declared 27-trial grid on `seeds` independently-seeded NEUTRAL
    (effect_strength=0.0, no planted causal link) universes. Empirically
    calibrates how many "confirming" trials the correlated grid throws off
    under a KNOWN null, instead of assuming trial independence (see FALSIFIER
    docstring -- the naive alpha*N chance floor was measurably wrong: it
    called a null fixture "confirmed"). Returns a list of per-seed
    (n_confirming, consistent_direction) tuples."""
    out = []
    for seed in seeds:
        universe = make_universe(seed=seed, effect_strength=0.0)
        results = run_declared_grid(universe)
        confirming = [r for r in results if not r["underpowered"] and r["confirms_difference"]]
        directions = {r["direction"] for r in confirming}
        out.append((len(confirming), len(directions) <= 1 and len(confirming) > 0))
    return out


def determine_verdict(results, placebo_counts):
    """Pre-declared rule (fixed here, before the run this function is called
    on): confirming-trial count in the canonical run is compared against its
    EMPIRICAL distribution under PLACEBO_SEEDS null universes (a permutation-
    style test), not against an independence-assuming alpha*N chance floor --
    see FALSIFIER. empirical_p = (#placebo runs >= canonical count + 1) /
    (n_placebo + 1) (standard add-one smoothing). CONFIRMED needs empirical_p
    < 0.1 AND a consistent direction across every confirming trial; REFUTED
    is empirical_p >= 0.3; everything between is INCONCLUSIVE."""
    scored = [r for r in results if not r["underpowered"]]
    underpowered = [r for r in results if r["underpowered"]]
    confirming = [r for r in scored if r["confirms_difference"]]

    directions = {r["direction"] for r in confirming}
    consistent = len(directions) <= 1 and len(confirming) > 0

    n_placebo = len(placebo_counts)
    ge = sum(1 for c, _ in placebo_counts if c >= len(confirming))
    empirical_p = (ge + 1) / (n_placebo + 1)

    if len(scored) * 2 < len(results):
        # Fewer than half the declared trials could be scored: there is not
        # enough evidence to refute (or confirm) anything. The first real run
        # scored 0/27 and this rule said "refuted" -- a placebo p of 1.0 on no
        # data is not a refutation.
        verdict = "inconclusive"
    elif empirical_p < 0.1 and consistent:
        verdict = "confirmed"
    elif empirical_p >= 0.3:
        verdict = "refuted"
    else:
        verdict = "inconclusive"

    return {
        "verdict": verdict,
        "n_trials": len(results),
        "n_scored": len(scored),
        "n_underpowered": len(underpowered),
        "n_confirming": len(confirming),
        "empirical_p": empirical_p,
        "consistent_direction": consistent,
    }


# ── Real data (#341) ─────────────────────────────────────────────────────

MIN_REAL_DAYS = 120     # a ticker needs this many aligned days to be included


def real_universe(dataset, prices, oi_by_ticker=None):
    """The test's universe from real inputs.

    `dataset`: what dealer_hedging_data.build_dataset returns. `prices`: a
    dealer_hedging_data.PriceSource. `oi_by_ticker`: {tk: {iso_date: total_oi}}
    from the pipeline's options_oi_history, or None.

    The calendar is the trading days every included ticker has a close on,
    within the dataset's range. Per ticker: closes; ADV = trailing 20-day mean
    dollar volume (read at i-1 by the signal); mkt_cap (an ETF proxy has none
    in the pipeline -- imputed from its ADV with the stocks' median cap/ADV
    ratio); `issuance` = the daily 424B2 count, each filing day mapped to the
    first calendar day on or after it; `notes` = sampled barriers with
    filing_idx (first calendar day on or after the EDGAR file date -- the
    knowledge date) and pricing_idx (last calendar day on or before pricing).
    Returns (universe, meta)."""
    start, end = dataset["start"], dataset["end"]
    series = {}
    for tk in dataset["tickers"]:
        rows = [(d, c, v) for d, c, v in prices.series(tk) if start <= d <= end and c]
        if len(rows) >= MIN_REAL_DAYS:
            series[tk] = {d: (c, v or 0.0) for d, c, v in rows}
    if not series:
        return {}, {"tickers": 0, "days": 0, "matched_on": None, "excluded": list(dataset["tickers"])}
    days = sorted(set.intersection(*(set(v) for v in series.values())))
    index = {d: k for k, d in enumerate(days)}

    def first_on_or_after(iso):
        for k, d in enumerate(days):
            if d >= iso:
                return k
        return None

    def last_on_or_before(iso):
        best = None
        for k, d in enumerate(days):
            if d > iso:
                break
            best = k
        return best

    universe, ratios = {}, []
    for tk, rows in series.items():
        closes = [rows[d][0] for d in days]
        dollar = [rows[d][0] * rows[d][1] for d in days]
        adv = []
        for k in range(len(days)):
            w = dollar[max(0, k - 19):k + 1]
            adv.append(sum(w) / len(w))
        info = dataset["tickers"][tk]
        issuance = [0] * len(days)
        for iso, n in (info.get("counts") or {}).items():
            k = first_on_or_after(iso)
            if k is not None:
                issuance[k] += n
        cum = [0]
        for n in issuance:
            cum.append(cum[-1] + n)
        notes = []
        for n in info.get("notes") or []:
            f = first_on_or_after(n["file_date"])
            pr = last_on_or_before(n["pricing_date"])
            if f is None or pr is None or pr > f:
                continue
            notes.append({"filing_idx": f, "pricing_idx": pr, "size_usd": 1.0,
                          "barrier": n["barrier"], "accession": n.get("accession")})
        oi = None
        if oi_by_ticker and oi_by_ticker.get(tk):
            hist = oi_by_ticker[tk]
            last, oi = None, []
            for d in days:
                last = hist.get(d, last)
                oi.append(last)
            if any(x is None for x in oi):
                oi = None          # not point-in-time for the whole calendar
        cap = info.get("mkt_cap")
        if cap and adv[-1]:
            ratios.append(cap / adv[-1])
        universe[tk] = {"closes": closes, "adv": adv, "total_oi": oi, "mkt_cap": cap,
                        "issuance": issuance, "issuance_cum": cum, "notes": notes,
                        "treatment_days": set()}
    ratio = sorted(ratios)[len(ratios) // 2] if ratios else 1.0
    for tk, u in universe.items():
        if not u["mkt_cap"]:
            u["mkt_cap"] = u["adv"][-1] * ratio
            u["mkt_cap_imputed"] = True
    with_oi = all(u["total_oi"] is not None for u in universe.values())
    if not with_oi:
        for u in universe.values():
            u["total_oi"] = None
    meta = {"tickers": len(universe), "days": len(days), "first_day": days[0], "last_day": days[-1],
            "matched_on": "size+ADV+OI" if with_oi else "size+ADV (no point-in-time OI yet)",
            "excluded": sorted(set(dataset["tickers"]) - set(universe)),
            "mkt_cap_imputed": sorted(tk for tk, u in universe.items() if u.get("mkt_cap_imputed")),
            "_days": days}
    return universe, meta


REAL_PLACEBO_SEEDS = tuple(f"shift-{k}" for k in range(20))
MIN_OI_DAYS = 60   # common point-in-time OI days before the OI-matched grid runs (#360)


def oi_window_universe(universe, days, oi_by_ticker):
    """(universe_with_oi, obs_days, n_common) for the OI-matched grid (#360).

    The whole calendar stays (issuance windows and prices need their
    history); OI is filled in from each ticker's own history -- carried
    forward over a missing day, never back before its first snapshot -- and
    an observation day i is used only when EVERY ticker has OI at i-1, the
    prior close the matching covariates read. n_common counts those days."""
    oi_by_ticker = oi_by_ticker or {}
    out, have = {}, None
    for tk, u in universe.items():
        hist = oi_by_ticker.get(tk) or {}
        last, series = None, []
        for d in days:
            last = hist.get(d, last)
            series.append(last)
        out[tk] = {**u, "total_oi": series}
        ok = {k for k, x in enumerate(series) if x is not None}
        have = ok if have is None else have & ok
    obs = {i for i in range(1, len(days)) if (i - 1) in (have or set())}
    return out, obs, len(have or ())


def real_placebo_universe(universe, seed):
    """The null for real data: each ticker's own issuance series circularly
    shifted by a seeded offset of at least 60 trading days. Prices, barriers
    and covariates are untouched, so any difference the grid still finds is
    what it finds with issuance timing unlinked from price behavior."""
    out = {}
    for tk, u in universe.items():
        series = u["issuance"]
        n = len(series)
        rnd = random.Random(f"{seed}:{tk}")
        k = rnd.randint(60, max(60, n - 60)) if n > 120 else n // 2
        shifted = series[-k:] + series[:-k] if k else list(series)
        cum = [0]
        for x in shifted:
            cum.append(cum[-1] + x)
        out[tk] = {**u, "issuance": shifted, "issuance_cum": cum}
    return out


# Left out of the real run, with the reason recorded in the results. Empty
# since #354: the first real run (#342) excluded SPY/QQQ/IWM/DIA because the
# plain index phrase matched nearly every 424B2; indices are now counted by
# note-title phrases (dealer_hedging_data.INDEX_TITLE_PHRASES).
REAL_EXCLUDED = {}
INDEX_COUNT_NOTE = ("an index counts only 424B2s whose title leads with it (\"Linked to the X\", "
                    "\"... Performing of the X\"); an index listed second in a basket is not counted")


def run_real(dataset, prices, oi_by_ticker=None, seeds=REAL_PLACEBO_SEEDS, log=print,
             exclude=None):
    """The declared grid on real data plus its shifted-issuance placebo.
    Returns the results document `dealer_hedging_results.json` stores."""
    exclude = REAL_EXCLUDED if exclude is None else exclude
    dataset = {**dataset, "tickers": {tk: v for tk, v in dataset["tickers"].items() if tk not in exclude}}
    universe, meta = real_universe(dataset, prices, oi_by_ticker)
    meta["excluded_by_design"] = dict(exclude)
    if len(universe) < 4:
        meta.pop("_days", None)
        return {"ok": False, "reason": f"only {len(universe)} priced underlyings", "universe": meta}
    log(f"real universe: {meta['tickers']} underlyings x {meta['days']} days, matched on {meta['matched_on']}")
    results = run_declared_grid(universe, control="low_tercile")
    placebo = []
    for seed in seeds:
        pr = run_declared_grid(real_placebo_universe(universe, seed), control="low_tercile")
        confirming = [r for r in pr if not r["underpowered"] and r["confirms_difference"]]
        placebo.append((len(confirming), len({r["direction"] for r in confirming}) <= 1 and bool(confirming)))
        log(f"  placebo {seed}: {placebo[-1][0]} confirming")
    summary = determine_verdict(results, placebo)
    days = meta.pop("_days")
    oi_universe, obs, n_oi = oi_window_universe(universe, days, oi_by_ticker)
    if n_oi < MIN_OI_DAYS:
        oi_block = {"skipped": True, "oi_days": n_oi, "min_oi_days": MIN_OI_DAYS}
    else:
        log(f"OI-matched grid: {n_oi} common OI days")
        oi_results = run_declared_grid(oi_universe, control="low_tercile", obs_days=obs)
        oi_placebo = []
        for seed in seeds:
            pr = run_declared_grid(real_placebo_universe(oi_universe, seed), control="low_tercile", obs_days=obs)
            c = [r for r in pr if not r["underpowered"] and r["confirms_difference"]]
            oi_placebo.append((len(c), len({r["direction"] for r in c}) <= 1 and bool(c)))
        first = days[min(obs) - 1] if obs else None
        oi_block = {"skipped": False, "oi_days": n_oi, "window": [first, days[-1]],
                    "matched_on": "size+ADV+OI", "trials": oi_results,
                    "placebo_confirming": [c for c, _ in oi_placebo],
                    "summary": determine_verdict(oi_results, oi_placebo)}
    meta["control_rule"] = "low_tercile: zero issuance, or bottom third of the day's rank"
    meta["index_counts"] = INDEX_COUNT_NOTE
    return {"ok": True, "universe": meta, "trials": results, "placebo_confirming": [c for c, _ in placebo],
            "oi_matched": oi_block,
            "summary": summary, "trial_count": TRIAL_COUNT, "lookahead_posture": LOOKAHEAD_POSTURE}


# ── Self-checks (must pass before a verdict is published) ──────────────────

def _tiny_fixture(seed=7):
    """Small deterministic fixture (fewer tickers/days) for the gate checks --
    fast, and isolated from the canonical N_UNDERLYINGS/N_DAYS run above."""
    global N_UNDERLYINGS, N_DAYS
    saved = (N_UNDERLYINGS, N_DAYS)
    N_UNDERLYINGS, N_DAYS = 6, 200
    try:
        u = make_universe(seed)
    finally:
        N_UNDERLYINGS, N_DAYS = saved
    return u


def _signal_table(universe, tickers, window=30, prox=0.025):
    table = {}
    max_i = min(len(universe[tk]["closes"]) for tk in tickers)
    for i in range(70, max_i):
        percentiles = issuance_intensity_percentile(universe, tickers, i, window)
        for tk in tickers:
            table[(tk, i)] = (percentiles.get(tk), near_barrier(universe, tk, i, prox))
    return table


def _leaky_percentile_pricing_date(universe, tickers, i, window):
    """BUG: uses pricing_idx (terms priced) instead of filing_idx (EDGAR
    acceptance) as the knowledge date -- treats a note as known before it was
    actually publicly filed. Same {"sum", "percentile"} shape as the correct
    implementation so the two are directly comparable."""
    sums = {}
    for tk in tickers:
        notes = universe[tk]["notes"]
        sums[tk] = sum(n["size_usd"] for n in notes if i - window <= n["pricing_idx"] < i)
    ranks = lag.cross_sectional_rank({tk: s for tk, s in sums.items() if s > 0})
    return {tk: {"sum": s, "percentile": ranks.get(tk)} for tk, s in sums.items()}


def run_truncation_gate():
    """A-vs-B truncation invariance for the signal-side functions: truncating
    trailing history must not change any overlapping (ticker, day) signal.
    Also proves the gate catches a same-day-close-style leak."""
    universe = _tiny_fixture()
    tickers = list(universe.keys())
    table_a = _signal_table(universe, tickers)

    ok = True
    for n_trunc in (10, 50):
        truncated = {
            tk: {**u, "closes": u["closes"][:-n_trunc], "bars": u["bars"][:-n_trunc],
                 "adv": u["adv"][:-n_trunc], "total_oi": u["total_oi"][:-n_trunc],
                 "notes": [n for n in u["notes"] if n["filing_idx"] < len(u["closes"]) - n_trunc]}
            for tk, u in universe.items()
        }
        table_b = _signal_table(truncated, tickers)
        mismatches = [k for k, v in table_b.items() if table_a.get(k) != v]
        status = "PASS" if not mismatches else "FAIL"
        print(f"[truncation]  N={n_trunc:>3}: {len(table_b)} overlapping rows -> {status}"
              + (f" ({len(mismatches)} mismatches)" if mismatches else ""))
        if mismatches:
            ok = False

    return ok


def run_same_day_peek_check():
    """Corrupting day i's own close must not change day i's own signal (the
    signal only ever reads i-1 and earlier)."""
    import copy
    universe = _tiny_fixture(seed=11)
    tickers = list(universe.keys())
    baseline = _signal_table(universe, tickers)

    corrupted = copy.deepcopy(universe)
    i_check = 150
    for tk in tickers:
        corrupted[tk]["closes"][i_check] = 1e9

    after = _signal_table(corrupted, tickers)
    ok = True
    for tk in tickers:
        if baseline[(tk, i_check)] != after[(tk, i_check)]:
            ok = False
            print(f"    SAME-DAY PEEK on {tk}@{i_check}: {baseline[(tk, i_check)]} -> {after[(tk, i_check)]}")
    print(f"[same-day peek] day {i_check}: {'PASS (unchanged)' if ok else 'FAIL (leaked)'}")
    return ok


def run_knowledge_date_check():
    """Directly asserts the knowledge date is filing_idx (EDGAR acceptance),
    not pricing_idx. Truncation alone can't catch this class of bug -- both
    fields are in the array's past relative to i, so index-based truncation
    sees nothing wrong; only a direct check on which field gates 'known'
    catches a wrong-but-still-past date. Same reason ab_truncation_test.py
    needs a same-day-peek check separate from its truncation test."""
    universe = _tiny_fixture(seed=23)
    tickers = list(universe.keys())

    # Find a note with a real pricing/filing gap to probe.
    probe_tk, probe_note = None, None
    for tk, u in universe.items():
        for n in u["notes"]:
            if n["filing_idx"] > n["pricing_idx"]:
                probe_tk, probe_note = tk, n
                break
        if probe_note:
            break
    if probe_note is None:
        print("[knowledge date] no probe note found in fixture -- FAIL")
        return False

    # Observe on the day AFTER pricing but BEFORE filing: the note priced but
    # has not yet hit EDGAR. The correct implementation must not count it yet;
    # the pricing_date-keyed variant incorrectly already does.
    i = probe_note["pricing_idx"] + 1
    window = probe_note["filing_idx"] - probe_note["pricing_idx"] + 5

    correct = issuance_intensity_percentile(universe, tickers, i, window)
    leaky = _leaky_percentile_pricing_date(universe, tickers, i, window)

    not_yet_known = correct[probe_tk]["sum"] == 0
    leak_caught = leaky[probe_tk]["sum"] >= probe_note["size_usd"]

    ok = not_yet_known and leak_caught
    print(f"[knowledge date] filing_idx={probe_note['filing_idx']} pricing_idx={probe_note['pricing_idx']} "
          f"observed_at={i}: correct impl {'correctly excludes' if not_yet_known else 'WRONGLY INCLUDES'} "
          f"the not-yet-filed note; pricing_date-keyed variant "
          f"{'correctly caught as leaky' if leak_caught else 'NOT CAUGHT -- check has no teeth'}")
    return ok


def run_power_check():
    """Proves the matching + paired-test machinery detects a REAL planted
    effect, so a 'refuted' verdict from the neutral canonical run means the
    fixture showed nothing, not that the test can't see anything."""
    universe = make_universe(seed="power-check", effect_strength=1.0)
    tickers = list(universe.keys())
    r = run_trial(universe, tickers, issuance_window=30, proximity_pct=0.025, horizon=10)
    detected = (not r["underpowered"]) and r["confirms_difference"]
    print(f"[power check] planted-effect universe, n_pairs={r.get('n_pairs')}: "
          f"{'PASS (detected the planted effect)' if detected else 'FAIL (missed a real effect -- no teeth)'}")
    if not r["underpowered"]:
        print(f"    vol_mean_diff={r['vol_mean_diff']:.4f} (p={r['vol_p']:.4f}), "
              f"pin_mean_diff={r['pin_mean_diff']:.4f} (p={r['pin_p']:.4f})")
    return detected


# ── Entry point ──────────────────────────────────────────────────────────

def main_real(out_path=None):
    """Self-checks, then the declared grid on real data; writes the results
    JSON (default Research/dealer_hedging_results.json)."""
    import json
    from datetime import datetime, timezone
    import dealer_hedging_data as dh
    print("-- self-checks (must pass before a verdict can be published) --")
    if not (run_truncation_gate() and run_same_day_peek_check() and run_knowledge_date_check()
            and run_power_check()):
        print("\nSelf-checks FAILED -- refusing to run the real grid.")
        return 1
    dataset = json.loads((dh.CACHE_DIR / "dataset.json").read_text(encoding="utf-8"))
    prices = dh.PriceSource()
    res = run_real(dataset, prices, oi_by_ticker=dh.oi_history(dh.PIPELINE_DB))
    # Coverage of what was TESTED: the excluded index proxies' counts are not
    # issuance and must not inflate the totals.
    res["coverage"] = dh.coverage({**dataset, "tickers": {
        tk: v for tk, v in dataset["tickers"].items() if tk not in REAL_EXCLUDED}})
    res["ran_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res["hypothesis"], res["falsifier"] = HYPOTHESIS, FALSIFIER
    out = out_path or os.path.join(os.path.dirname(__file__), "dealer_hedging_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, default=list)
    if res.get("ok"):
        s = res["summary"]
        print(f"\nScored {s['n_scored']}/{s['n_trials']} trials; {s['n_confirming']} confirming; "
              f"empirical p={s['empirical_p']:.3f} vs {len(res['placebo_confirming'])} shifted-issuance placebos")
        print(f"VERDICT (real data): {s['verdict'].upper()}  -> {out}")
    else:
        print(f"Not run: {res['reason']} -> {out}")
    return 0


def main():
    if "--real" in sys.argv:
        return main_real()
    print("Part B -- dealer-hedging causal test (issue #131)")
    print(f"Hypothesis: {HYPOTHESIS}")
    print(f"Falsifier:  {FALSIFIER}")
    print(f"Declared trial grid: {len(ISSUANCE_WINDOWS)} issuance windows x "
          f"{len(BARRIER_PROXIMITY_PCTS)} barrier-proximity defs x {len(HORIZONS)} horizons "
          f"= {TRIAL_COUNT} trials. Look-ahead posture: {LOOKAHEAD_POSTURE}.\n")

    print("-- self-checks (must pass before a verdict can be published) --")
    gate_ok = run_truncation_gate() and run_same_day_peek_check() and run_knowledge_date_check()
    power_ok = run_power_check()
    if not (gate_ok and power_ok):
        print("\nSelf-checks FAILED -- refusing to run/publish the declared trial grid.")
        return 1

    print("\n-- declared trial grid (canonical neutral fixture, seed=131, "
          "effect_strength=0.0) --")
    universe = make_universe(seed=131, effect_strength=0.0)
    results = run_declared_grid(universe)
    for r in results:
        tag = (f"n_pairs={r['n_pairs']:>3} UNDERPOWERED" if r["underpowered"] else
               f"n_pairs={r['n_pairs']:>3} vol_diff={r['vol_mean_diff']:+.4f}(p={r['vol_p']:.3f}) "
               f"pin_diff={r['pin_mean_diff']:+.4f}(p={r['pin_p']:.3f}) "
               f"{'CONFIRMS' if r['confirms_difference'] else 'no diff'}")
        print(f"  window={r['issuance_window']:>2} prox={r['proximity_pct']:.3f} "
              f"horizon={r['horizon']:>2}: {tag}")

    print(f"\n-- placebo calibration: same declared grid on {len(PLACEBO_SEEDS)} "
          f"independent NULL universes --")
    placebo_counts = run_placebo_calibration()
    for seed, (n, cons) in zip(PLACEBO_SEEDS, placebo_counts):
        print(f"  {seed}: {n} confirming trials (consistent direction: {cons})")

    summary = determine_verdict(results, placebo_counts)
    print(f"\nScored trials: {summary['n_scored']}/{summary['n_trials']} "
          f"({summary['n_underpowered']} underpowered, excluded)")
    print(f"Confirming trials: {summary['n_confirming']} "
          f"(empirical p={summary['empirical_p']:.3f} vs placebo null, consistent direction: "
          f"{summary['consistent_direction']})")
    print(f"\nMECHANISM CHECK on the synthetic NULL fixture: {summary['verdict'].upper()} "
          f"(expected -- no effect is planted there). This is not a finding about real "
          f"underlyings; run with --real for that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
