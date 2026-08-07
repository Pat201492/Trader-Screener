"""
Gate for Part B (issue #110): the causal test. Same convention as
lookahead-gate/ab_truncation_test.py and tools/edgar_scrubber/test_*.py --
stdlib only, run directly, exit 0 = pass.

The section that matters most is "look-ahead A-vs-B truncation gate": it
adapts Chan's truncation test (lookahead-gate/ab_truncation_test.py) from a
price-history signal to this event-stream (filing) signal, and -- per that
file's own methodology -- proves the gate has teeth by showing it CATCHES a
deliberately leaky variant that reads pricing_date instead of the EDGAR
acceptance date. A gate that can't fail on a known bug isn't validating
anything.

Run: python Research/structured_note_issuance/test_causal_test.py
"""

import random
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import causal_test as ct  # noqa: E402

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


# ── synthetic universe (deterministic, no clock, no network) ──────────────

N_DAYS = 300
DATES = [f"D{d:04d}" for d in range(N_DAYS)]


def make_bars(seed):
    rnd = random.Random(seed)
    price = 100.0
    bars = []
    for d in DATES:
        drift = rnd.gauss(0.0003, 0.015)
        price = max(1.0, price * (1 + drift))
        bars.append(ct.PriceBar(date=d, close=price))
    return bars


def make_universe():
    universe = {}
    for i, sym in enumerate(["HIGH1", "HIGH2", "CTRL1", "CTRL2", "CTRL3", "CTRL4"]):
        bars = make_bars(seed=100 + i)
        high = sym.startswith("HIGH")
        universe[sym] = ct.UnderlyingUniverse(
            symbol=sym, bars=bars,
            adv=5.0e8 if high else (4.5e8 + i * 1e7),   # controls close in ADV
            options_oi=2.0e5 if high else (1.8e5 + i * 5e3),
        )
    return universe


def make_filings(universe):
    """A handful of filings on the HIGH symbols, priced through the barriers
    those symbols' own price paths cross, so near_barrier() actually fires."""
    filings = []
    for sym in ("HIGH1", "HIGH2"):
        bars = universe[sym].bars
        for k, accept_idx in enumerate((60, 140, 220)):
            spot_at_accept = bars[accept_idx].close
            filings.append(ct.Filing(
                accession=f"{sym}-{k}", underlying=sym,
                acceptance_date=DATES[accept_idx],
                aggregate_principal=1_000_000.0,
                barrier_levels=[spot_at_accept * 0.9, spot_at_accept * 1.05],
            ))
    return filings


def main():
    section("declared trial grid")
    check("TRIAL_COUNT == 18", ct.TRIAL_COUNT == 18)
    check("grid product matches TRIAL_COUNT",
          len(ct.ISSUANCE_WINDOWS) * len(ct.BARRIER_PROXIMITY_PCT) * len(ct.FORWARD_HORIZONS)
          == ct.TRIAL_COUNT)
    check("hypothesis and falsifier are non-empty strings",
          isinstance(ct.HYPOTHESIS, str) and len(ct.HYPOTHESIS) > 20
          and isinstance(ct.FALSIFIER, str) and len(ct.FALSIFIER) > 20)

    section("near_barrier / forward_realized_vol / welch_t")
    check("near_barrier true within tolerance", ct.near_barrier(101.0, [100.0], 2.0))
    check("near_barrier false outside tolerance", not ct.near_barrier(110.0, [100.0], 2.0))
    check("near_barrier handles empty levels", not ct.near_barrier(101.0, [], 2.0))

    flat = [ct.PriceBar(date=f"D{i:03d}", close=100.0) for i in range(30)]
    check("forward_realized_vol ~0 on a flat series", ct.forward_realized_vol(flat, 0, 10) < 1e-6)
    check("forward_realized_vol None past the end of history",
          ct.forward_realized_vol(flat, 25, 10) is None)

    hi = [10.0, 11.0, 12.0, 13.0, 14.0]
    lo = [1.0, 1.1, 0.9, 1.0, 1.05]
    t = ct.welch_t(hi, lo)
    check("welch_t large and positive when group means are far apart", t is not None and t > 5)
    check("welch_t None with <2 obs in a group", ct.welch_t([1.0], lo) is None)

    section("match_controls")
    universe = make_universe()
    matches = ct.match_controls(universe, ["HIGH1", "HIGH2"], k=2)
    check("every high symbol gets some matches", all(matches[h] for h in ("HIGH1", "HIGH2")))
    check("no high symbol matched to itself or another high symbol",
          all(m not in ("HIGH1", "HIGH2") for ms in matches.values() for m in ms))
    check("matches are drawn from the control pool",
          all(m.startswith("CTRL") for ms in matches.values() for m in ms))

    section("look-ahead A-vs-B truncation gate")
    filings = make_filings(universe)
    # pick a filing with a real pricing/acceptance gap: priced day 40, not
    # accepted (publicly known) until day 200 -- the "pricing_date leaks
    # info that was not yet public" scenario the issue calls out.
    gap_filing = ct.Filing(accession="GAP-1", underlying="HIGH1",
                            acceptance_date=DATES[200], aggregate_principal=5_000_000.0,
                            barrier_levels=[])
    gap_filing.pricing_date = DATES[40]   # test-only attribute; real Filing has no pricing_date
    all_filings = filings + [gap_filing]

    def leaky_trailing_issuance(filings, symbol, dates_index, as_of_idx, window_days):
        """Deliberately buggy sibling of ct.trailing_issuance: keys off
        pricing_date instead of acceptance_date. Test-local only -- never
        exported from causal_test.py."""
        if as_of_idx <= 0:
            return 0.0
        start_idx = max(0, as_of_idx - window_days)
        window_dates = set(dates_index[start_idx:as_of_idx])
        return sum(
            f.aggregate_principal for f in filings
            if f.underlying == symbol and getattr(f, "pricing_date", f.acceptance_date) in window_dates
        )

    def visible_as_of(filings, dates_index, cutoff_idx):
        """Point-in-time snapshot: only filings EDGAR had actually accepted
        before `cutoff_idx` could be in a real dataset at that point -- this
        is the honest analogue of Chan's price-history truncation."""
        cutoff_date = dates_index[cutoff_idx]
        return [f for f in filings if f.acceptance_date < cutoff_date]

    N_TRUNC = 100
    dates_full = DATES
    dates_trunc = DATES[:N_TRUNC]
    filings_full = all_filings
    filings_trunc = visible_as_of(all_filings, DATES, N_TRUNC)

    correct_mismatches = []
    leaky_mismatches = []
    for i in range(1, N_TRUNC):
        a_correct = ct.trailing_issuance(filings_full, "HIGH1", dates_full, i, window_days=180)
        b_correct = ct.trailing_issuance(filings_trunc, "HIGH1", dates_trunc, i, window_days=180)
        if a_correct != b_correct:
            correct_mismatches.append(i)

        a_leaky = leaky_trailing_issuance(filings_full, "HIGH1", dates_full, i, window_days=180)
        b_leaky = leaky_trailing_issuance(filings_trunc, "HIGH1", dates_trunc, i, window_days=180)
        if a_leaky != b_leaky:
            leaky_mismatches.append(i)

    check("shipped trailing_issuance (acceptance_date) has ZERO A-vs-B mismatches",
          len(correct_mismatches) == 0)
    check("leaky pricing_date-keyed variant IS caught (mismatches exist)",
          len(leaky_mismatches) > 0)
    if leaky_mismatches:
        print(f"    caught leak at day index(es): {leaky_mismatches[:5]}"
              f"{'...' if len(leaky_mismatches) > 5 else ''}")

    # Direct check too (belt and suspenders, same posture as
    # ab_truncation_test.py's same-day-peek check): at a day strictly between
    # pricing_date(40) and acceptance_date(200), the shipped implementation
    # must NOT count the filing; the leaky one incorrectly does.
    i_between = 90
    correct_val = ct.trailing_issuance([gap_filing], "HIGH1", DATES, i_between, window_days=180)
    leaky_val = leaky_trailing_issuance([gap_filing], "HIGH1", DATES, i_between, window_days=180)
    check("shipped implementation excludes a not-yet-accepted filing",
          correct_val == 0.0)
    check("leaky implementation wrongly includes it (proves the scenario is real)",
          leaky_val == 5_000_000.0)

    section("end-to-end run on SYNTHETIC data (mechanism check, not a real finding)")
    results = ct.run_all_trials(universe, all_filings, ["HIGH1", "HIGH2"], matches)
    check("returns exactly TRIAL_COUNT results", len(results) == ct.TRIAL_COUNT)
    status, explanation = ct.verdict(results)
    check("verdict status is one of the three declared outcomes",
          status in ("supported", "refuted", "inconclusive"))
    check("verdict comes with a non-empty explanation", bool(explanation))
    print(f"    synthetic verdict: {status} -- {explanation}")

    # make_bars() gives every symbol the SAME drift/vol random walk with NO
    # engineered issuance -> vol relationship -- a known-null case. Each
    # trial's false-positive rate is calibrated to ~0.3% (CRITICAL_T is a
    # Bonferroni bar for 18 trials), so a correctly-specified mechanism
    # should almost never call this "supported". A prior version of
    # run_trial() sampled the high group from a conditioned subsample
    # (near_barrier + trailing_issuance>0) against an UNCONDITIONED control
    # (the control's whole history) with overlapping forward-vol windows on
    # top -- that combination cleared |t| >= CRITICAL_T on this exact
    # synthetic data in the same direction on a majority of trials, i.e. a
    # false "supported" verdict on pure noise. This check exists so that
    # regression can't silently come back.
    check("synthetic null case (no engineered relationship) does NOT verdict 'supported'",
          status != "supported")

    print()
    if failures:
        print(f"test_causal_test: FAIL ({len(failures)} check(s) failed)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("test_causal_test: PASS")


if __name__ == "__main__":
    main()
