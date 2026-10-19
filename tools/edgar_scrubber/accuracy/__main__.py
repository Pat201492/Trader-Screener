import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))       # tools/edgar_scrubber
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # tools (server)

from accuracy import checks  # noqa: E402


def main(argv):
    what = (argv[1] if len(argv) > 1 else "all").lower()
    runs = {"golden": [checks.golden], "cross": [checks.cross], "sanity": [checks.sanity],
            "all": [checks.golden, checks.cross, checks.sanity]}
    if what == "canary":
        n = int(argv[2]) if len(argv) > 2 else 5
        reports = [checks.canary(per_form=n)]
    elif what in runs:
        reports = [fn() for fn in runs[what]]
    else:
        print(__doc__ or "usage: python -m accuracy [golden|cross|sanity|canary [N]|all]")
        return 2
    for r in reports:
        print(r.render())
        if r.name == "golden" and getattr(r, "per_field", None):
            fields = sorted({f for f, _ in r.per_field})
            print("   per field: " + ", ".join(
                f"{f} {r.per_field[(f, 'ok')]}/{r.per_field[(f, 'ok')] + r.per_field[(f, 'wrong')]}"
                for f in fields))
    return 1 if any(r.failed and r.name == "golden" for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
