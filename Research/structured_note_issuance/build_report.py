"""
Part A runner (issue #110): OutputStore -> analysis.py -> charts.py -> a
short findings write-up. This is the script a user runs after the EDGAR
scrubber crawl + extraction ladder have populated the local store (see
tools/edgar_scrubber/README_crawl.md and the saved query
tools/edgar_scrubber/queries/424b2-structured-notes.json).

Reads the store through OutputStore's public interface only (analysis.py
does the same) -- never touches the sqlite file directly.

Usage:
    python Research/structured_note_issuance/build_report.py
    python Research/structured_note_issuance/build_report.py --store /path/to/extractions.sqlite

Writes three PNGs + findings.md into Research/structured_note_issuance/output/.
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import charts  # noqa: E402
from analysis import (  # noqa: E402
    OutputStore, load_notes, issuance_by_underlying_month,
    barrier_levels_vs_spot, issuer_markup_table,
)

OUTPUT_DIR = _HERE / "output"


def build(store_path=None, run_date=None):
    store = OutputStore(store_path) if store_path else OutputStore()
    notes = load_notes(store)

    if not notes:
        print(
            "No documents in the scrubber's local store -- nothing to report.\n"
            "Run the crawl + extraction ladder first:\n"
            "  python tools/edgar_scrubber/crawl.py "
            "tools/edgar_scrubber/queries/424b2-structured-notes.json\n"
            "then run the extraction ladder over the crawled accessions "
            "(see tools/edgar_scrubber/EXTRACTION_LADDER.md), which is what "
            "populates this store via write_document()."
        )
        return None

    issuance = issuance_by_underlying_month(notes)
    barriers = barrier_levels_vs_spot(notes)
    markup = issuer_markup_table(notes)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p_issuance = charts.issuance_chart(issuance, OUTPUT_DIR / "issuance_by_underlying.png")
    p_barriers = charts.barrier_chart(barriers, OUTPUT_DIR / "barrier_clustering.png")
    p_markup = charts.markup_chart(markup, OUTPUT_DIR / "issuer_markup_league_table.png")

    stamp = run_date or "unknown-date"
    n_underlyings = len({u for (u, _m) in issuance})
    n_issuers = len({m.issuer for m in markup})
    total_principal = sum(issuance.values())
    findings = f"""# Structured-note issuance -- Part A findings ({stamp})

Generated from {len(notes)} document(s) in the local scrubber store, covering
{n_underlyings} distinct underlying(s) and {n_issuers} issuer(s). Pure
extraction -- no modeling, no controls, no p-values (see causal_test.py for
Part B).

## 1. Issuance by underlying, over time
![issuance]({p_issuance.name})

Aggregate principal summed across all notes: ${total_principal:,.0f}. See the
module docstring in analysis.py for the basket over-count caveat: a
worst-of note's full principal is attributed to every underlying it lists.

## 2. Barrier and autocall level clustering vs spot
![barriers]({p_barriers.name})

barrier_pct / coupon_barrier_pct / autocall_barrier_pct mapped to an
absolute price via initial_underlying_value, per underlying.

## 3. Issuer markup league table
![markup]({p_markup.name})

(1000 - estimated_value_per_1000) / 1000 as the issuer's own disclosed
markup vs the $1,000 issue price, principal-weighted by issuer.
"""
    (OUTPUT_DIR / "findings.md").write_text(findings, encoding="utf-8")
    print(f"wrote {p_issuance}\nwrote {p_barriers}\nwrote {p_markup}\n"
          f"wrote {OUTPUT_DIR / 'findings.md'}")
    return {
        "n_notes": len(notes), "n_underlyings": n_underlyings,
        "n_issuers": n_issuers, "total_principal": total_principal,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=None, help="path to the scrubber's sqlite store")
    parser.add_argument("--run-date", default=None,
                         help="ISO date stamp for the findings header (caller-supplied -- "
                              "this script does not read the clock, same convention as "
                              "OutputStore.start_run)")
    args = parser.parse_args()
    build(store_path=args.store, run_date=args.run_date)


if __name__ == "__main__":
    main()
