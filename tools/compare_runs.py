"""
Compare two extraction runs, field by field (issue #180).

Every accuracy or cost claim in this repo needs the same table: take two runs out
of the local store and put them side by side, per field -- how many rows, how many
carried a value, how many carried a span, how many distinct values, and how many
validation flags. Several open PRs are blocked on exactly this comparison, so it
lives here as a command instead of being rebuilt by hand each time.

    python tools/compare_runs.py <run_a> <run_b>
    python tools/compare_runs.py <run_a> <run_b> --field estimated_value_per_1000

The second form narrows to one field and lists its distinct values with counts for
both runs -- which is how you see WHAT changed, not just that something did.

The tell this command exists to surface: a field whose single most common value
accounts for 60% or more of its rows. That is the shape of a fabricated constant
-- on run #0018 `estimated_value_per_1000` came back as 950 (the midpoint of the
field's bounds) in 21 of 25 filings because the model, unable to find the figure,
answered in the middle of the range. A run where one value dominates a field is
flagged so the reader looks before trusting it.

Read-only by construction: the store is opened `mode=ro`, so this command cannot
write, migrate, or lock the database it inspects. stdlib only. Run the tests:
    python tools/test_compare_runs.py
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "edgar_scrubber"))

from output_store import DEFAULT_STORE_PATH  # noqa: E402

# A field where one value covers this share of its rows or more looks like a
# fabricated constant, not an extraction. See the module docstring (run #0018).
DOMINANCE_THRESHOLD = 0.60


class CompareError(RuntimeError):
    """A comparison could not be run -- a missing store or an unknown run id.
    Reported to the user, never raised as a traceback (see `main`)."""


def open_readonly(store_path):
    """Open the store `mode=ro`: this command reads, it never writes. A path
    that does not exist is reported as a CompareError rather than silently
    creating an empty database (which `sqlite3.connect` on a plain path would
    do)."""
    p = Path(store_path).expanduser()
    if not p.exists():
        raise CompareError(f"no store at {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def require_run(conn, run_id):
    """Raise CompareError if `run_id` is not in the store -- so an unknown id is
    a clean message, not a table full of zeros or a traceback."""
    row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        known = [r["run_id"] for r in
                 conn.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()]
        hint = f" known runs: {', '.join(known)}" if known else " store has no runs."
        raise CompareError(f"unknown run id {run_id!r}.{hint}")


def _value_key(value_json):
    """A stable key for one stored value: None when the row has no value (JSON
    null or a NULL column), else the canonical JSON string so equal values group
    and distinct ones do not."""
    if value_json is None:
        return None
    try:
        parsed = json.loads(value_json)
    except (TypeError, ValueError):
        return value_json
    if parsed is None:
        return None
    return json.dumps(parsed, sort_keys=True)


def field_stats(conn, run_id):
    """Per-field stats for one run: {field: {n, present, spanned, distinct,
    flags, values}}. `values` is a Counter of present values (by `_value_key`),
    which drives both the dominance flag and the `--field` value listing."""
    rows = conn.execute(
        "SELECT field, value_json, span_start, span_end, flags_json "
        "FROM extractions WHERE run_id = ?",
        (run_id,),
    ).fetchall()

    stats = {}
    for r in rows:
        s = stats.setdefault(r["field"], {
            "n": 0, "present": 0, "spanned": 0, "flags": 0,
            "values": Counter(),
        })
        s["n"] += 1
        key = _value_key(r["value_json"])
        if key is not None:
            s["present"] += 1
            s["values"][key] += 1
        if r["span_start"] is not None or r["span_end"] is not None:
            s["spanned"] += 1
        if r["flags_json"]:
            try:
                s["flags"] += len(json.loads(r["flags_json"]))
            except (TypeError, ValueError):
                pass

    for s in stats.values():
        s["distinct"] = len(s["values"])
    return stats


def dominant_value(stat):
    """The (value_key, count, fraction) of the field's most common value, or
    None when the field has no present value. `fraction` is over ALL rows (`n`),
    so a field that is null half the time and one constant the other half is not
    falsely flagged as dominated."""
    if not stat["values"] or stat["n"] == 0:
        return None
    key, count = stat["values"].most_common(1)[0]
    return key, count, count / stat["n"]


def is_dominated(stat):
    dom = dominant_value(stat)
    return dom is not None and dom[2] >= DOMINANCE_THRESHOLD


def _display_value(value_key):
    """A value key back to something readable for the table (strip the JSON
    quoting on a plain string, leave structured values as their JSON)."""
    if value_key is None:
        return "(none)"
    try:
        parsed = json.loads(value_key)
    except (TypeError, ValueError):
        return value_key
    if isinstance(parsed, str):
        return parsed
    return json.dumps(parsed, sort_keys=True)


def format_comparison(stats_a, stats_b, run_a, run_b):
    """The per-field before/after table as a string. One row per field present in
    either run; a field dominated by one value in either run is marked."""
    fields = sorted(set(stats_a) | set(stats_b))
    empty = {"n": 0, "present": 0, "spanned": 0, "distinct": 0, "flags": 0,
             "values": Counter()}

    lines = []
    lines.append(f"comparing {run_a}  vs  {run_b}")
    header = (f"{'field':<28} "
              f"{'n':>4} {'pres':>4} {'span':>4} {'dist':>4} {'flag':>4}  | "
              f"{'n':>4} {'pres':>4} {'span':>4} {'dist':>4} {'flag':>4}  const?")
    lines.append(header)
    lines.append("-" * len(header))

    for f in fields:
        a = stats_a.get(f, empty)
        b = stats_b.get(f, empty)
        marks = []
        if is_dominated(a):
            marks.append("A")
        if is_dominated(b):
            marks.append("B")
        mark = ("<= " + ",".join(marks)) if marks else ""
        lines.append(
            f"{f:<28} "
            f"{a['n']:>4} {a['present']:>4} {a['spanned']:>4} "
            f"{a.get('distinct', 0):>4} {a['flags']:>4}  | "
            f"{b['n']:>4} {b['present']:>4} {b['spanned']:>4} "
            f"{b.get('distinct', 0):>4} {b['flags']:>4}  {mark}"
        )

    dominated = [f for f in fields
                 if is_dominated(stats_a.get(f, empty))
                 or is_dominated(stats_b.get(f, empty))]
    if dominated:
        lines.append("")
        lines.append(f"const? -- one value covers >= {int(DOMINANCE_THRESHOLD * 100)}% "
                     f"of rows (shape of a fabricated constant): "
                     f"{', '.join(dominated)}")
    return "\n".join(lines)


def format_field(field, stats_a, stats_b, run_a, run_b):
    """Distinct values of one field with counts for both runs, side by side."""
    a = stats_a.get(field, {"values": Counter(), "n": 0})
    b = stats_b.get(field, {"values": Counter(), "n": 0})
    keys = sorted(set(a["values"]) | set(b["values"]),
                  key=lambda k: (-(a["values"][k] + b["values"][k]), _display_value(k)))

    lines = []
    lines.append(f"field {field!r}: {run_a}  vs  {run_b}")
    header = f"{'value':<40} {run_a[:16]:>16} {run_b[:16]:>16}"
    lines.append(header)
    lines.append("-" * len(header))
    if not keys:
        lines.append("(no values for this field in either run)")
    for k in keys:
        lines.append(f"{_display_value(k):<40} "
                     f"{a['values'].get(k, 0):>16} {b['values'].get(k, 0):>16}")

    for label, stat, rid in ((run_a, a, run_a), (run_b, b, run_b)):
        if is_dominated(stat):
            dom = dominant_value(stat)
            lines.append(f"  {rid}: {_display_value(dom[0])!r} covers "
                         f"{dom[1]}/{stat['n']} rows ({dom[2] * 100:.0f}%) -- "
                         f"shape of a fabricated constant")
    return "\n".join(lines)


def build_parser():
    p = argparse.ArgumentParser(
        prog="compare_runs",
        description="Compare two extraction runs field by field (read-only).")
    p.add_argument("run_a", help="first run id")
    p.add_argument("run_b", help="second run id")
    p.add_argument("--field", help="narrow to one field and list its distinct "
                                   "values with counts for both runs")
    p.add_argument("--store", default=str(DEFAULT_STORE_PATH),
                   help="path to the extractions sqlite store "
                        "(default: the local scrubber store)")
    return p


def run(args):
    """Do the comparison and return its text. Raises CompareError for a missing
    store or unknown run id; `main` turns that into a message and exit code."""
    conn = open_readonly(args.store)
    try:
        require_run(conn, args.run_a)
        require_run(conn, args.run_b)
        stats_a = field_stats(conn, args.run_a)
        stats_b = field_stats(conn, args.run_b)
    finally:
        conn.close()

    if args.field:
        return format_field(args.field, stats_a, stats_b, args.run_a, args.run_b)
    return format_comparison(stats_a, stats_b, args.run_a, args.run_b)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print(run(args))
    except CompareError as e:
        print(f"compare_runs: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
