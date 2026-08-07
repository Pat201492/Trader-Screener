"""
Part A -- descriptive map of 424B2 structured-note issuance (issue #110).

Pure extraction, no modeling, no controls, no p-values: three aggregations
over whatever the EDGAR scrubber's local OutputStore has extracted. Reads
ONLY through OutputStore's public query()/fields() interface (#109) -- this
module never touches the store's sqlite file directly, so the ownership
boundary output_store.py enforces stays the single enforcement point.

  1. issuance_by_underlying_month -- aggregate_principal summed by
     (underlying, pricing month). A map of where structured-note exposure is
     being created.
  2. barrier_levels_vs_spot       -- barrier_pct / coupon_barrier_pct /
     autocall_barrier_pct mapped to an absolute price via
     initial_underlying_value. Where protection levels cluster on each name
     -- a fact about the note book, no claim about what it causes.
  3. issuer_markup_table          -- (1000 - estimated_value_per_1000) / 1000
     as the issuer's own disclosed markup, aggregated by issuer x
     product_type x month. Costs nothing extra: the field is already
     extracted for Part B (causal_test.py).

Caveat carried in every function, not swept under the rug: a worst-of/basket
note lists more than one underlying, and there is no per-underlying issue
size in the field spec -- this module attributes the note's FULL
aggregate_principal (and its single initial_underlying_value) to EACH
underlying it lists. Summing across underlyings therefore over-counts total
book size; this is a map of where exposure and protection levels TOUCH each
name, not a partition of total issuance.

pricing_date (not an EDGAR acceptance timestamp) is used for the month
bucket here -- that is fine for a purely descriptive count with no forward
prediction. Part B (causal_test.py) is where the pricing-date-vs-acceptance-
date distinction becomes a look-ahead risk, and it deliberately does NOT
reuse this module's date handling.

Run the self-check: python Research/structured_note_issuance/test_analysis.py
"""

import sys
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.edgar_scrubber import OutputStore  # noqa: E402

BARRIER_FIELDS = ("barrier_pct", "coupon_barrier_pct", "autocall_barrier_pct")


@dataclass
class NoteRecord:
    """One (accession, document) flattened out of the store: document-level
    dimensions plus every extracted field, keyed by canonical field name."""

    accession: str
    document: str
    issuer: str
    product_type: str
    pricing_date: str
    underlyings: list          # [name, ...]
    fields: dict                # field name -> value


def load_notes(store, **query_kwargs):
    """Every document in `store`, flattened into a NoteRecord. Uses ONLY
    OutputStore.query() and OutputStore.fields() -- the tool interface #109
    exposes -- never the sqlite file underneath.

    `query_kwargs` pass straight through to OutputStore.query (underlying=,
    issuer=, product_type=, date_from=, date_to=), so a caller can scope the
    load instead of pulling the whole store.
    """
    notes = []
    for doc in store.query(**query_kwargs):
        rows = store.fields(doc["accession"], doc["document"])
        values = {r["field"]: r["value"] for r in rows}
        underlyings = [u["name"] for u in doc.get("underlyings", []) if u.get("name")]
        if not underlyings:
            underlyings = [u.get("name") for u in (values.get("underlyings") or [])
                           if isinstance(u, dict) and u.get("name")]
        notes.append(NoteRecord(
            accession=doc["accession"],
            document=doc["document"],
            issuer=doc.get("issuer"),
            product_type=doc.get("product_type"),
            pricing_date=values.get("pricing_date") or doc.get("filing_date"),
            underlyings=underlyings,
            fields=values,
        ))
    return notes


def _month(iso_date):
    return iso_date[:7] if iso_date else None


# ── 1. issuance by underlying, over time ────────────────────────────────────

def issuance_by_underlying_month(notes):
    """{(underlying, "YYYY-MM"): summed aggregate_principal}.

    Note the basket caveat in the module docstring: a worst-of note's FULL
    principal is attributed to every underlying it lists, so summing this
    dict across underlyings over-counts total issuance.
    """
    out = defaultdict(float)
    for n in notes:
        principal = n.fields.get("aggregate_principal")
        month = _month(n.pricing_date)
        if principal is None or month is None:
            continue
        for u in n.underlyings:
            out[(u, month)] += principal
    return dict(out)


# ── 2. barrier / autocall level clustering vs spot ─────────────────────────

@dataclass
class BarrierLevel:
    accession: str
    underlying: str
    barrier_type: str          # one of BARRIER_FIELDS
    barrier_pct: float          # as extracted, % of initial
    initial_underlying_value: float
    absolute_level: float       # initial_underlying_value * barrier_pct / 100


def barrier_levels_vs_spot(notes):
    """One BarrierLevel per (note, underlying, barrier field present).

    Caveat: initial_underlying_value is a single number per note -- the field
    spec has no per-underlying initial level for worst-of baskets -- so a
    multi-underlying note's barrier levels all map off the same reference
    value. Correct for the common single-underlying case; flagged here for
    baskets rather than silently assumed correct for them too.
    """
    out = []
    for n in notes:
        iv = n.fields.get("initial_underlying_value")
        if iv is None:
            continue
        for field_name in BARRIER_FIELDS:
            pct = n.fields.get(field_name)
            if pct is None:
                continue
            level = iv * pct / 100.0
            for u in (n.underlyings or [None]):
                out.append(BarrierLevel(
                    accession=n.accession, underlying=u, barrier_type=field_name,
                    barrier_pct=pct, initial_underlying_value=iv,
                    absolute_level=level,
                ))
    return out


# ── 3. issuer markup league table ──────────────────────────────────────────

@dataclass
class MarkupBucket:
    issuer: str
    product_type: str
    month: str
    n: int
    total_principal: float
    mean_markup_pct: float
    principal_weighted_markup_pct: float


def issuer_markup_table(notes):
    """Aggregated by issuer x product_type x pricing month. Reports both a
    plain mean across notes and a principal-weighted mean side by side -- a
    $500k note and a $50MM note are not the same exposure, but the plain mean
    is still worth keeping since it isn't sensitive to a handful of huge
    deals.
    """
    buckets = defaultdict(list)   # (issuer, product_type, month) -> [(markup_pct, principal)]
    for n in notes:
        ev = n.fields.get("estimated_value_per_1000")
        if ev is None:
            continue
        markup_pct = (1000.0 - ev) / 1000.0 * 100.0
        principal = n.fields.get("aggregate_principal") or 0.0
        key = (n.issuer, n.product_type, _month(n.pricing_date))
        buckets[key].append((markup_pct, principal))

    out = []
    for (issuer, product_type, month), rows in buckets.items():
        count = len(rows)
        total_principal = sum(p for _, p in rows)
        mean_markup = sum(m for m, _ in rows) / count
        weighted = (sum(m * p for m, p in rows) / total_principal
                    if total_principal > 0 else mean_markup)
        out.append(MarkupBucket(
            issuer=issuer, product_type=product_type, month=month, n=count,
            total_principal=total_principal, mean_markup_pct=mean_markup,
            principal_weighted_markup_pct=weighted,
        ))
    return out


if __name__ == "__main__":
    # Self-check against an in-memory store with one seeded note -- proves the
    # module runs end-to-end. The real gate with multiple notes/issuers is
    # test_analysis.py; run that for the full check.
    from tools.edgar_scrubber import DocumentExtraction

    store = OutputStore(":memory:")
    rid = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")
    store.write_document(rid, DocumentExtraction.from_record(
        "0001-24-000001", "424b2.htm",
        {
            "issuer": "JPMorgan Chase Financial Company LLC",
            "product_type": "autocallable contingent coupon",
            "pricing_date": "2026-01-15",
            "underlyings": [{"name": "S&P 500 Index", "kind": "index"}],
            "aggregate_principal": 2_500_000,
            "barrier_pct": 70.0,
            "initial_underlying_value": 4800.0,
            "estimated_value_per_1000": 972.4,
        },
    ))
    notes = load_notes(store)
    print(f"loaded {len(notes)} note(s)")
    print("issuance by underlying/month:", issuance_by_underlying_month(notes))
    print("barrier levels:", barrier_levels_vs_spot(notes))
    print("markup table:", issuer_markup_table(notes))
    print("\nanalysis self-check: PASS")
