"""
CLI for #130 (Part A of #110): generate the three structured-note issuance
maps -- chart (SVG) + short written finding each -- from the scrubber's
`OutputStore`.

    python tools/edgar_scrubber/generate_research_maps.py --demo
    python tools/edgar_scrubber/generate_research_maps.py \\
        --out-dir tools/edgar_scrubber/research_output

`--demo` builds a small SYNTHETIC in-memory store (a handful of made-up
notes across issuers/months) so the pipeline is runnable offline with no
crawl -- every number in its findings.md is clearly synthetic, not a real
measurement. Without `--demo`, it opens the real local store (default path,
or `--store`) and reads whatever a live crawl + validation loop (#105) has
actually populated; if that store is empty the findings say so rather than
fabricating numbers, same posture as this tool's other "not yet measured"
sections (see THROUGHPUT_BENCHMARK.md).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from research_maps import write_outputs


def _demo_store():
    from output_store import OutputStore, DocumentExtraction

    store = OutputStore(":memory:")
    rid = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z",
                          note="synthetic demo data, not a real crawl")

    demo_notes = [
        ("0001-26-000001", "JPMorgan Chase Financial Company LLC",
         "autocallable contingent coupon", "2026-01-15",
         [{"name": "S&P 500 Index", "kind": "index"}],
         2_500_000, 972.4, 70.0, 65.0, 5000.0),
        ("0001-26-000002", "JPMorgan Chase Financial Company LLC",
         "autocallable contingent coupon", "2026-02-10",
         [{"name": "S&P 500 Index", "kind": "index"}],
         3_000_000, 968.1, 70.0, 60.0, 5100.0),
        ("0001-26-000003", "BofA Finance LLC",
         "buffered return enhanced", "2026-02-20",
         [{"name": "Russell 2000 Index", "kind": "index"}],
         1_200_000, 981.0, None, None, 2100.0),
        ("0001-26-000004", "BofA Finance LLC",
         "autocallable contingent coupon", "2026-03-05",
         [{"name": "S&P 500 Index", "kind": "index"},
          {"name": "Nasdaq-100 Index", "kind": "index"}],
         4_000_000, 964.7, 65.0, 60.0, 5150.0),
        ("0001-26-000005", "Citigroup Global Markets Holdings Inc.",
         "trigger notes", "2026-03-18",
         [{"name": "Nasdaq-100 Index", "kind": "index"}],
         900_000, 975.3, 75.0, None, 18000.0),
        ("0001-26-000006", "JPMorgan Chase Financial Company LLC",
         "autocallable contingent coupon", "2026-03-30",
         [{"name": "S&P 500 Index", "kind": "index"}],
         2_800_000, 970.0, 70.0, 60.0, 5200.0),
    ]

    for i, (accession, issuer, ptype, pdate, underlyings, principal, ev,
            barrier, coupon_barrier, spot) in enumerate(demo_notes):
        record = {
            "issuer": issuer,
            "product_type": ptype,
            "pricing_date": pdate,
            "underlyings": underlyings,
            "aggregate_principal": principal,
            "estimated_value_per_1000": ev,
            "initial_underlying_value": spot,
        }
        if barrier is not None:
            record["barrier_pct"] = barrier
        if coupon_barrier is not None:
            record["coupon_barrier_pct"] = coupon_barrier
        store.write_document(rid, DocumentExtraction.from_record(accession, "424b2.htm", record))

    return store


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate structured-note issuance maps (#130): chart + finding, x3")
    ap.add_argument("--demo", action="store_true",
                    help="use a small synthetic in-memory store instead of the real one")
    ap.add_argument("--store", default=None,
                    help="path to the scrubber's OutputStore sqlite file (default: local home)")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "research_output"),
                    help="directory to write chart/data/finding files into")
    ap.add_argument("--issuer", default=None, help="filter to one issuer")
    ap.add_argument("--product-type", default=None, help="filter to one product_type")
    ap.add_argument("--date-from", default=None, help="filter: filing_date >= this ISO date")
    ap.add_argument("--date-to", default=None, help="filter: filing_date <= this ISO date")
    args = ap.parse_args(argv)

    if args.demo:
        store = _demo_store()
    else:
        from output_store import OutputStore
        store = OutputStore(args.store, readonly=True)

    query_kwargs = {
        k: v for k, v in {
            "issuer": args.issuer,
            "product_type": args.product_type,
            "date_from": args.date_from,
            "date_to": args.date_to,
        }.items() if v is not None
    }

    result = write_outputs(store, args.out_dir, query_kwargs=query_kwargs)
    store.close()

    print(f"read {result['n_documents']} document(s) from the store"
          f"{' (demo data)' if args.demo else ''}")
    for name, path in result["paths"].items():
        print(f"  wrote {name}: {path}")


if __name__ == "__main__":
    main()
