"""
Gate for cross-filer frames ingest (issue #238). Same convention as
`test_facts_store.py`: stdlib only, run directly, exit 0 = pass. Offline
throughout -- frames come from small synthetic fixtures under `fixtures/frames/`
shaped like SEC's frames response; no network, no model.

Every acceptance criterion in #238 is checked here:

  * ingest_frames makes exactly one frames() call per concept in the field's
    CANONICAL list, and writes one fact per fixture data row with the fields
    mapped as Scope lists (cik/concept/unit/period/accession/source/form/filed);
  * an instant period (...I) gives period_start == period_end; a duration keeps
    the row's own start;
  * an invalid period raises ValueError and the fake client records zero calls;
  * entityName lands in the `filers` table and server.filer_name() returns it
    with no companyfacts cache present;
  * running ingest twice leaves the fact count unchanged (idempotent).

Run:  python tools/edgar_scrubber/test_frames_ingest.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import frames_ingest
from facts_store import FactsStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "frames"

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


class FakeFramesClient:
    """Resolves frames(taxonomy, tag, unit, period) from a fixture file named
    `{taxonomy}_{tag}_{unit}_{period}.json`. A concept with no fixture returns
    None (a real frames call with no data 404s / is empty) -- still one call.
    Records every call so the "one request per concept" and "zero calls on a bad
    period" criteria can be asserted."""

    def __init__(self):
        self.calls = []

    def frames(self, taxonomy, tag, unit, period):
        self.calls.append((taxonomy, tag, unit, period))
        path = FIXTURES / f"{taxonomy}_{tag}_{unit}_{period}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def run_checks():
    # ── AC: one call per concept; one fact per row; field mapping ──
    section("AC: one frames() call per concept, one fact per data row, "
            "fields mapped per Scope")
    store = FactsStore(":memory:")
    client = FakeFramesClient()
    summary = frames_ingest.ingest_frames(client, "operating_income", "CY2024", store)

    check("exactly one call per concept in the field's CANONICAL list",
          len(client.calls) == 1)
    check("the call carries taxonomy, bare tag, unit, period",
          client.calls[0] == ("us-gaap", "OperatingIncomeLoss", "USD", "CY2024"))
    check("summary reports the request count", summary["requests"] == 1)

    apple = store.facts_for("320193", concept="us-gaap:OperatingIncomeLoss")
    check("a fact was written for each data row (Apple present)", len(apple) == 1)
    f = apple[0]
    check("concept is the taxonomy-qualified name", f["concept"] == "us-gaap:OperatingIncomeLoss")
    check("unit comes from the frames payload uom", f["unit"] == "USD")
    check("value maps from the row's val", f["value"] == 123216000000)
    check("source is xbrl", f["source"] == "xbrl")
    check("form is empty (frames carries none)", f["form"] == "")
    check("filed is None (frames carries no filed date)", f["filed"] is None)
    check("accession maps from the row's accn", f["accession"] == "0000320193-24-000123")
    check("a duration keeps the row's own start",
          f["period_start"] == "2024-01-01" and f["period_end"] == "2024-12-31")
    check("both filers in the frame were written",
          store.facts_for("789019", concept="us-gaap:OperatingIncomeLoss"))
    check("summary counts facts written and distinct filers",
          summary["written"] == 2 and summary["filers"] == 2)

    # ── AC: one call PER CONCEPT even when a concept returns no data ──
    section("AC: a multi-concept field calls once per concept, incl. empty ones")
    store2 = FactsStore(":memory:")
    client2 = FakeFramesClient()
    rev = frames_ingest.ingest_frames(client2, "revenue", "CY2024", store2)
    from canonical_concepts import CANONICAL
    check("one call for each of revenue's concepts (SalesRevenueNet has no data)",
          len(client2.calls) == len(CANONICAL["revenue"]) == 3)
    check("requests summary equals the concept count", rev["requests"] == 3)
    # 2 rows (RFCC) + 1 row (Revenues) + 0 rows (SalesRevenueNet fixture absent).
    check("facts written = sum of rows across the concepts that had data",
          rev["written"] == 3)
    check("a concept whose frame is empty contributes no fact but still one call",
          ("us-gaap", "SalesRevenueNet", "USD", "CY2024") in client2.calls)

    # ── AC: instant period -> period_start == period_end ──
    section("AC: an instant period (...I) stores period_start == period_end")
    store3 = FactsStore(":memory:")
    client3 = FakeFramesClient()
    inst = frames_ingest.ingest_frames(client3, "total_assets", "CY2024Q4I", store3)
    check("the instant frame was requested with the I period",
          client3.calls == [("us-gaap", "Assets", "USD", "CY2024Q4I")])
    assets = store3.facts_for("320193", concept="us-gaap:Assets")
    check("an instant fact was written", len(assets) == 1)
    a = assets[0]
    check("instant fact has period_start == period_end (the row's end, twice)",
          a["period_start"] == a["period_end"] == "2024-12-28")
    check("instant summary wrote both filers", inst["written"] == 2)

    # ── AC: invalid period raises ValueError, zero calls ──
    section("AC: an invalid period raises ValueError before any request")
    for bad in ("2024", "FY2024", "CY24", "CY2024Q5", "CY2024I", "CY2024Q3X", ""):
        client4 = FakeFramesClient()
        store4 = FactsStore(":memory:")
        raised = False
        try:
            frames_ingest.ingest_frames(client4, "operating_income", bad, store4)
        except ValueError:
            raised = True
        check(f"{bad!r} raises ValueError", raised)
        check(f"{bad!r} makes zero frames() calls", client4.calls == [])
    # And the valid grammar is accepted.
    for good in ("CY2024", "CY2024Q3", "CY2024Q4I"):
        check(f"{good!r} is a valid period",
              frames_ingest._validate_period(good) == good.endswith("I"))

    # ── AC: idempotent -- second run leaves the fact count unchanged ──
    section("AC: running ingest twice leaves the fact count unchanged")
    store5 = FactsStore(":memory:")
    client5 = FakeFramesClient()
    frames_ingest.ingest_frames(client5, "operating_income", "CY2024", store5)
    n1 = len(store5.facts_for("320193", concept="us-gaap:OperatingIncomeLoss")) + \
        len(store5.facts_for("789019", concept="us-gaap:OperatingIncomeLoss"))
    frames_ingest.ingest_frames(client5, "operating_income", "CY2024", store5)
    n2 = len(store5.facts_for("320193", concept="us-gaap:OperatingIncomeLoss")) + \
        len(store5.facts_for("789019", concept="us-gaap:OperatingIncomeLoss"))
    check("fact count is unchanged after a second identical ingest", n1 == n2 == 2)

    # ── AC: entityName lands in `filers`; server.filer_name reads it ──
    section("AC: entityName lands in the filers table; server.filer_name reads it "
            "with no companyfacts cache")
    check("filer_names() returns the frame's entity names",
          store5.filer_names().get("320193") == "Apple Inc.")

    # A real on-disk store under a temp EDGAR_SCRUBBER_HOME, so server.facts_path
    # finds it. No cache/ dir is created, so the companyfacts path cannot fire.
    home = Path(tempfile.mkdtemp(prefix="frames-filer-"))
    disk = FactsStore(path=str(home / "store" / "facts.sqlite"))
    dclient = FakeFramesClient()
    try:
        frames_ingest.ingest_frames(dclient, "operating_income", "CY2024", disk)
        check("names are on disk in the filers table",
              disk.filer_names().get("789019") == "Microsoft Corporation")
    finally:
        disk.close()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tools/
    import server
    prev_home = os.environ.get("EDGAR_SCRUBBER_HOME")
    os.environ["EDGAR_SCRUBBER_HOME"] = str(home)
    server._FILER_NAMES.clear()
    try:
        check("server.filer_name resolves a frames-only filer from the store",
              server.filer_name("320193") == "Apple Inc.")
        check("...for the other filer too", server.filer_name("789019") == "Microsoft Corporation")
        check("an unknown CIK returns None, not a raise",
              server.filer_name("999999999") is None)
    finally:
        server._FILER_NAMES.clear()
        if prev_home is None:
            os.environ.pop("EDGAR_SCRUBBER_HOME", None)
        else:
            os.environ["EDGAR_SCRUBBER_HOME"] = prev_home


def main():
    print("EDGAR scrubber frames-ingest gate (#238)")
    run_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nframes-ingest gate: PASS")


if __name__ == "__main__":
    main()
