"""
Cross-filer ingest from the SEC frames API (issue #238).

The facts store holds one filer at a time, fetched per filing (`server.taxonomy_run`
pulls `companyfacts` per CIK). Ranking filers against each other needs the OTHER
axis: one field across EVERY filer for a single period. SEC's frames API returns
exactly that -- one concept, one unit, one period, every filer that tagged it --
in ONE request (`GET /api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json`).

`ingest_frames(client, field, period, store)` walks the canonical concept list
for `field` (`canonical_concepts.CANONICAL[field]`), calls `client.frames()` once
per concept, and writes one `FactRecord` per returned row into the period-keyed
facts store. Frames rows carry no `filed` date and no `form`, so those are left
empty/None -- a frames fact is point-in-time only through its period, never
"as known on" a date (`FactsStore.as_of` will not return it). Each row's
`entityName` is recorded in the store's `filers` directory so a read view can
name a filer the per-filing path never fetched.

Period strings follow SEC's frames grammar, validated BEFORE any request:
  * ``CY2024``     annual duration (a fiscal year)
  * ``CY2024Q3``   quarter duration
  * ``CY2024Q4I``  instant (a balance-sheet point at quarter end)
Anything else raises ``ValueError`` and no request is made.

Reads through the existing `EdgarClient.frames` (no new HTTP path). stdlib only.
Run the CLI / self-check:

    python tools/edgar_scrubber/frames_ingest.py --fields revenue,operating_income --period CY2024
"""

import re

try:  # package import (from .frames_ingest import ...)
    from .canonical_concepts import CANONICAL
    from .facts_store import FactRecord, FactsStore
except ImportError:  # flat import (import frames_ingest)
    from canonical_concepts import CANONICAL
    from facts_store import FactRecord, FactsStore


# ── Period grammar ───────────────────────────────────────────────────────────
#
# CY<year>[Q<1-4>[I]] -- an annual duration, a quarter duration, or (with the
# trailing I) an instant at that quarter's end. An annual instant (`CY2024I`)
# is NOT a frames period, so the quarter is required before the I.
_PERIOD_RE = re.compile(r"^CY\d{4}(?:Q[1-4]I?)?$")


def _validate_period(period):
    """Return whether `period` is an instant, raising ``ValueError`` on any
    string outside SEC's frames grammar. Called before the first request so a bad
    period costs zero HTTP calls."""
    if not isinstance(period, str) or not _PERIOD_RE.match(period):
        raise ValueError(
            f"invalid frames period {period!r}: expected CY<year>, "
            f"CY<year>Q<1-4>, or CY<year>Q<1-4>I (e.g. CY2024, CY2024Q3, "
            f"CY2024Q4I)"
        )
    return period.endswith("I")


# ── Request unit per field ───────────────────────────────────────────────────
#
# Frames is keyed by unit of measure. The fundamentals fields here are dollar
# amounts (uom "USD"); earnings-per-share is the one exception (uom
# "USD-per-shares"). A caller can override with `unit=`.
_UNIT_FOR_FIELD = {
    "eps_diluted": "USD-per-shares",
}


def _default_unit(field):
    return _UNIT_FOR_FIELD.get(field, "USD")


def _split_concept(concept):
    """``us-gaap:Revenues`` -> ``("us-gaap", "Revenues")``. Frames' URL takes the
    taxonomy and the bare tag separately."""
    taxonomy, _, tag = concept.partition(":")
    return taxonomy, tag


# ── Ingest ───────────────────────────────────────────────────────────────────

def ingest_frames(client, field, period, store, *, unit=None):
    """Ingest one `field` across every filer for one `period` into `store`.

    For each concept in ``CANONICAL[field]`` (KeyError on an unknown field, by
    design), make exactly one ``client.frames(taxonomy, tag, unit, period)`` call
    and write one ``FactRecord`` per returned data row. An instant period
    (``...I``) stores ``period_start == period_end`` (the row carries only an
    end); a duration keeps the row's own ``start``. ``entityName`` from each row
    is recorded in the store's `filers` directory.

    Returns a summary dict: ``{"field", "period", "unit", "requests", "written",
    "filers"}`` -- requests made, facts written, and the count of distinct filers
    seen. Idempotent: re-running with the same frames leaves the fact count
    unchanged (the store's UNIQUE key collapses the repeat).

    Raises ``ValueError`` for an invalid period BEFORE any request is made."""
    is_instant = _validate_period(period)  # raises before any HTTP call
    unit = unit or _default_unit(field)
    concepts = CANONICAL[field]

    requests = 0
    records = []
    filers = {}
    no_frame = []
    for concept in concepts:
        taxonomy, tag = _split_concept(concept)
        requests += 1
        try:
            payload = client.frames(taxonomy, tag, unit, period)
        except Exception as exc:
            # SEC answers 404 when no filer tagged this concept for this period
            # -- routine for an era's tag outside its era (`SalesRevenueNet`
            # ended with ASC 606, so it has no CY2024 frame). That is "no data
            # for this concept", not a failed ingest; any other error is.
            if getattr(exc, "status", None) != 404:
                raise
            no_frame.append(concept)
            continue
        if not payload:
            no_frame.append(concept)
            continue
        row_unit = payload.get("uom") or unit
        for row in payload.get("data") or []:
            end = row.get("end")
            if not end:
                continue  # a frames row with no period end cannot be period-keyed
            start = end if is_instant else row.get("start")
            cik = str(row.get("cik"))
            records.append(FactRecord(
                cik=cik, concept=concept, unit=row_unit,
                period_start=start, period_end=end,
                fiscal_year=None, fiscal_period="", form="",
                accession=row.get("accn"), value=row.get("val"),
                source="xbrl", filed=None,
            ))
            name = row.get("entityName")
            if name:
                filers[cik] = name

    if records:
        store.put_facts(records)
    if filers:
        store.put_filers(filers)

    return {
        "field": field,
        "period": period,
        "unit": unit,
        "requests": requests,
        "written": len(records),
        "filers": len(filers),
        "no_frame": no_frame,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_client():
    """The real EDGAR client, honouring EDGAR_USER_AGENT and the local cache."""
    import os
    from pathlib import Path
    try:
        from .edgar_client import EdgarClient
    except ImportError:
        from edgar_client import EdgarClient
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise SystemExit(
            "EDGAR_USER_AGENT is not set. The SEC fair-access policy requires a "
            "contactable '<name> <email>' on every request; see SETUP.md."
        )
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", Path.home() / ".edgar-scrubber"))
    return EdgarClient(user_agent=ua, cache_dir=str(home.expanduser() / "cache"))


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest SEC frames (one field across every filer for a "
                    "period) into the facts store.")
    parser.add_argument("--fields", required=True,
                        help="comma-separated canonical fields, e.g. "
                             "revenue,operating_income")
    parser.add_argument("--period", required=True,
                        help="SEC frames period: CY2024, CY2024Q3, CY2024Q4I")
    parser.add_argument("--store", default=None,
                        help="facts.sqlite path (default: the local store)")
    args = parser.parse_args(argv)

    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    unknown = [f for f in fields if f not in CANONICAL]
    if unknown:
        raise SystemExit(f"unknown field(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(sorted(CANONICAL))}")
    try:
        _validate_period(args.period)  # fail fast before opening client / store
    except ValueError as exc:
        raise SystemExit(str(exc))

    client = _build_client()
    store = FactsStore(path=args.store)
    try:
        for field in fields:
            summary = ingest_frames(client, field, args.period, store)
            print(f"{field}: {summary['requests']} request(s), "
                  f"{summary['written']} fact(s) written, "
                  f"{summary['filers']} distinct filer(s)"
                  + (f", no frame for {', '.join(summary['no_frame'])}"
                     if summary["no_frame"] else ""))
    finally:
        store.close()


if __name__ == "__main__":
    main()
