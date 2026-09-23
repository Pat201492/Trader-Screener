"""
Gate for `tools/compare_runs.py` (issue #180). Same convention as
`tools/test_server.py` and the scrubber's tests: stdlib only, no network, no
model, run directly, exit 0 = pass.

The fixture builds a REAL store with `OutputStore` (so the schema under test is
the schema in production, not a hand-copied stand-in), writes two runs, and
asserts the compared numbers -- including the dominance flag that is the whole
point of the command, and that the store is opened read-only.

Run:  python tools/test_compare_runs.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "edgar_scrubber"))

import compare_runs as cr
from output_store import OutputStore, FieldValue

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def _fv(value, *, span=None, flags=None):
    return FieldValue(field="estimated_value_per_1000", value=value,
                      span=span, flags=flags or [])


# --------------------------------------------------------------------------- #
# Build a real store: run A dominated by one value, run B varied with a gap.
# --------------------------------------------------------------------------- #
_tmp = tempfile.TemporaryDirectory()
store_path = Path(_tmp.name) / "extractions.sqlite"

with OutputStore(store_path) as store:
    a = store.start_run("424b2.structured_note", "1.0.0", "2026-08-01T00:00:00Z",
                        run_id="run-a")
    b = store.start_run("424b2.structured_note", "1.1.0", "2026-08-02T00:00:00Z",
                        run_id="run-b")

    # Run A: 950 in 4 of 5 rows (fabricated-constant shape), one real 972.
    a_vals = [950, 950, 950, 950, 972]
    for i, v in enumerate(a_vals):
        span = (10, 18) if i in (0, 4) else None        # 2 spanned
        flags = [{"code": "out_of_bounds", "severity": "warn", "field": "x",
                  "message": "m"}] if i == 1 else []      # 1 flag total
        store.record(a, f"acc-{i}", "424b2.htm", _fv(v, span=span, flags=flags))
        # a second field, never dominated, present+spanned on every row.
        store.record(a, f"acc-{i}", "424b2.htm",
                     FieldValue(field="cusip", value=f"CUSIP{i:04d}",
                                span=(1, 9)))

    # Run B: five distinct values, all spanned, plus one row with no value.
    for i, v in enumerate([900, 910, 920, 930, 940]):
        store.record(b, f"acc-{i}", "424b2.htm", _fv(v, span=(10, 18)))
    store.record(b, "acc-5", "424b2.htm", _fv(None))     # present must not count

# --------------------------------------------------------------------------- #
section("read-only open")
# --------------------------------------------------------------------------- #

conn = cr.open_readonly(store_path)
try:
    conn.execute("INSERT INTO runs (run_id, spec_id, spec_version, started_at) "
                 "VALUES ('x','y','z','t')")
    wrote = True
except sqlite3.OperationalError:
    wrote = False
check("a write against the read-only connection is refused", wrote is False)
conn.close()

# --------------------------------------------------------------------------- #
section("per-field stats")
# --------------------------------------------------------------------------- #

conn = cr.open_readonly(store_path)
sa = cr.field_stats(conn, "run-a")
sb = cr.field_stats(conn, "run-b")
conn.close()

ev_a = sa["estimated_value_per_1000"]
check("run A n counts every row", ev_a["n"] == 5)
check("run A present counts rows with a value", ev_a["present"] == 5)
check("run A spanned counts rows carrying a span", ev_a["spanned"] == 2)
check("run A distinct counts distinct values", ev_a["distinct"] == 2)
check("run A flags sums flags across rows", ev_a["flags"] == 1)

ev_b = sb["estimated_value_per_1000"]
check("run B n counts the null row too", ev_b["n"] == 6)
check("run B present excludes the null row", ev_b["present"] == 5)
check("run B distinct is five", ev_b["distinct"] == 5)
check("run B spanned is five", ev_b["spanned"] == 5)

# --------------------------------------------------------------------------- #
section("dominance flag (the fabricated-constant tell)")
# --------------------------------------------------------------------------- #

check("run A field dominated by one value is flagged", cr.is_dominated(ev_a) is True)
check("run B varied field is not flagged", cr.is_dominated(ev_b) is False)
check("cusip (all distinct) is not flagged", cr.is_dominated(sa["cusip"]) is False)
dom = cr.dominant_value(ev_a)
check("dominant value is 950 at 4/5 of rows",
      cr._display_value(dom[0]) == "950" and dom[1] == 4 and abs(dom[2] - 0.8) < 1e-9)

# --------------------------------------------------------------------------- #
section("comparison table")
# --------------------------------------------------------------------------- #

table = cr.format_comparison(sa, sb, "run-a", "run-b")
check("table names both runs", "run-a" in table and "run-b" in table)
check("table has a row per field",
      "estimated_value_per_1000" in table and "cusip" in table)
check("dominated field is marked in the table",
      "const?" in table and "estimated_value_per_1000" in
      table.split("const? -- ")[-1])

# --------------------------------------------------------------------------- #
section("--field value listing")
# --------------------------------------------------------------------------- #

out = cr.format_field("estimated_value_per_1000", sa, sb, "run-a", "run-b")
check("lists the dominant value with its run-A count", "950" in out)
check("lists a run-B-only value", "900" in out)
check("names the fabricated-constant shape for the dominated run",
      "fabricated constant" in out)

# --------------------------------------------------------------------------- #
section("missing run id is reported, not a traceback")
# --------------------------------------------------------------------------- #

rc = cr.main(["run-a", "no-such-run", "--store", str(store_path)])
check("main returns a nonzero code for an unknown run id", rc == 2)

raised = None
try:
    conn = cr.open_readonly(store_path)
    try:
        cr.require_run(conn, "ghost")
    finally:
        conn.close()
except cr.CompareError:
    raised = True
check("require_run raises a clean CompareError, not a bare sqlite error",
      raised is True)

missing = cr.main(["a", "b", "--store", str(Path(_tmp.name) / "nope.sqlite")])
check("a missing store file is reported, not a traceback", missing == 2)

# --------------------------------------------------------------------------- #
_tmp.cleanup()
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ncompare_runs gate: PASS")
