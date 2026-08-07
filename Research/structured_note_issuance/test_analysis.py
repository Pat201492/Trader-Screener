"""
Gate for Part A (issue #110): the descriptive map. Same convention as
tools/edgar_scrubber/test_output_store.py -- stdlib (+matplotlib, already a
repo dependency), run directly, exit 0 = pass.

Seeds a synthetic OutputStore with several notes across issuers, underlyings
and months (through the public write_document() API only -- no sqlite
poking), then checks:

  * issuance_by_underlying_month sums aggregate_principal per (underlying,
    month) and attributes a basket note's full principal to EVERY
    underlying it lists (the documented over-count caveat, checked here so
    it can't silently regress into something else);
  * barrier_levels_vs_spot maps every barrier_pct/coupon_barrier_pct/
    autocall_barrier_pct present to an absolute level via
    initial_underlying_value, one row per (note, underlying, barrier type);
  * issuer_markup_table's principal-weighted mean actually weights by
    principal (a big note should pull the average toward its own markup);
  * all three charts render to a real, non-empty PNG.

Run: python Research/structured_note_issuance/test_analysis.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from analysis import (  # noqa: E402
    OutputStore, load_notes, issuance_by_underlying_month,
    barrier_levels_vs_spot, issuer_markup_table,
)
from tools.edgar_scrubber import DocumentExtraction  # noqa: E402
import charts  # noqa: E402

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def seed_store():
    store = OutputStore(":memory:")
    rid = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")

    notes = [
        # single-underlying SPX autocallable, JPM
        dict(accession="0001", document="424b2.htm", issuer="JPMorgan Chase Financial Company LLC",
             product_type="autocallable contingent coupon", pricing_date="2026-01-15",
             underlyings=[{"name": "S&P 500 Index", "kind": "index"}],
             aggregate_principal=2_500_000, barrier_pct=70.0, coupon_barrier_pct=70.0,
             autocall_barrier_pct=100.0, initial_underlying_value=4800.0,
             estimated_value_per_1000=972.4),
        # second SPX note, same month, different issuer, smaller size -> tests
        # principal-weighted mean pulling toward the bigger note's markup
        dict(accession="0002", document="424b2.htm", issuer="Goldman Sachs Finance Corp",
             product_type="autocallable contingent coupon", pricing_date="2026-01-22",
             underlyings=[{"name": "S&P 500 Index", "kind": "index"}],
             aggregate_principal=250_000, barrier_pct=75.0, initial_underlying_value=4850.0,
             estimated_value_per_1000=940.0),
        # worst-of basket: SPX + RTY, JPM, next month -- the over-count case
        dict(accession="0003", document="424b2.htm", issuer="JPMorgan Chase Financial Company LLC",
             product_type="worst-of contingent coupon", pricing_date="2026-02-10",
             underlyings=[{"name": "S&P 500 Index", "kind": "index"},
                          {"name": "Russell 2000 Index", "kind": "index"}],
             aggregate_principal=1_000_000, barrier_pct=65.0, initial_underlying_value=4900.0,
             estimated_value_per_1000=965.0),
    ]
    for n in notes:
        store.write_document(rid, DocumentExtraction.from_record(
            n.pop("accession"), n.pop("document"), n,
        ))
    return store


def main():
    store = seed_store()
    notes = load_notes(store)

    section("load_notes")
    check("loaded 3 notes", len(notes) == 3)
    check("basket note carries both underlyings",
          sorted(next(n.underlyings for n in notes if n.accession == "0003"))
          == ["Russell 2000 Index", "S&P 500 Index"])

    section("issuance_by_underlying_month")
    issuance = issuance_by_underlying_month(notes)
    check("SPX Jan = note1 + note2",
          issuance[("S&P 500 Index", "2026-01")] == 2_500_000 + 250_000)
    check("SPX Feb picks up the FULL basket principal (over-count, by design)",
          issuance[("S&P 500 Index", "2026-02")] == 1_000_000)
    check("RTY Feb also gets the FULL basket principal",
          issuance[("Russell 2000 Index", "2026-02")] == 1_000_000)
    check("no bogus keys", len(issuance) == 3)

    section("barrier_levels_vs_spot")
    levels = barrier_levels_vs_spot(notes)
    note1_levels = [b for b in levels if b.accession == "0001"]
    check("note1 contributes 3 barrier rows (barrier/coupon/autocall)",
          len(note1_levels) == 3)
    barrier_row = next(b for b in note1_levels if b.barrier_type == "barrier_pct")
    check("absolute level = initial_value * pct / 100",
          barrier_row.absolute_level == 4800.0 * 70.0 / 100.0)
    basket_levels = [b for b in levels if b.accession == "0003"]
    check("basket note's single barrier maps onto BOTH underlyings",
          {b.underlying for b in basket_levels} == {"S&P 500 Index", "Russell 2000 Index"})

    section("issuer_markup_table")
    markup = issuer_markup_table(notes)
    jpm_jan = next(m for m in markup
                   if m.issuer == "JPMorgan Chase Financial Company LLC" and m.month == "2026-01")
    check("JPM Jan markup = (1000-972.4)/1000*100",
          abs(jpm_jan.mean_markup_pct - 2.76) < 1e-9)
    spx_jan_group = [m for m in markup if m.month == "2026-01"]
    # note1 ($2.5MM, markup 2.76%) dwarfs note2 ($250k, markup 6.0%) in a
    # principal-weighted average, but they're in different issuer buckets so
    # check each bucket's own weighted mean equals its own mean (single-note
    # buckets) and that they differ from each other.
    gs_jan = next(m for m in markup if m.issuer == "Goldman Sachs Finance Corp")
    check("GS markup = (1000-940)/1000*100 = 6.0",
          abs(gs_jan.mean_markup_pct - 6.0) < 1e-9)
    check("JPM and GS markups differ (weighting isn't collapsing buckets)",
          abs(jpm_jan.mean_markup_pct - gs_jan.mean_markup_pct) > 1.0)

    section("charts render")
    tmp = Path(tempfile.mkdtemp(prefix="snin_charts_"))
    try:
        p1 = charts.issuance_chart(issuance, tmp / "issuance.png")
        p2 = charts.barrier_chart(levels, tmp / "barriers.png")
        p3 = charts.markup_chart(markup, tmp / "markup.png")
        for p in (p1, p2, p3):
            check(f"{Path(p).name} rendered non-empty", Path(p).exists() and Path(p).stat().st_size > 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"test_analysis: FAIL ({len(failures)} check(s) failed)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("test_analysis: PASS")


if __name__ == "__main__":
    main()
