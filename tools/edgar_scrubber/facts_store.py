"""
Period-keyed facts store for the EDGAR scrubber (issue #183).

`OutputStore` (#109) keys on ``(run, accession, document, field)`` and every row
it holds is *document*-grained: it demands a span, a spec_id and a spec_version.
An XBRL fact does not fit that grain. It has no document and no spec — its natural
key is a reporting PERIOD: "revenue for FY2025 Q3", reported by a CIK. Forcing it
through OutputStore means nulling out span/spec on every row and pretending a
period is a document. So this is its own store, sharing OutputStore's ownership
boundary but nothing of its schema.

Two record types live here, because two things are period-grained:

  * ``FactRecord`` — a reported fact lifted from XBRL. Numeric, unit-carrying,
    keyed by ``(cik, concept, period_start, period_end, accession)``.
  * ``GuidanceRecord`` — a range extracted from prose ("we expect revenue of
    $9.0-$9.2 billion"). It DOES point back at a document + span, because unlike
    an XBRL fact it was read out of text and a human may need to check it.

Supersession, because a period is restated. A 10-Q reports Q3, then a later 10-K
restates the same Q3 under a new accession. Both are kept — ``history()`` returns
every version oldest-first — but ``latest()`` returns the superseding one, ordered
by accession (a later filing carries a later accession) with ingestion order as a
tiebreak.

Local-only by construction: the destination path is checked against the very same
ownership boundary OutputStore enforces (ARCHITECTURE.md — the pipeline is the only
writer of shared data; the scrubber keeps a local tool datastore).

stdlib only (sqlite3). Run the self-check:  python tools/edgar_scrubber/facts_store.py
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field as dc_field
from pathlib import Path

# The ownership boundary is defined once, in output_store. Reuse it verbatim so
# the facts store cannot drift into a second, laxer guard. Both flat (script /
# pytest via conftest) and package (relative) import styles must resolve.
try:  # package import (from .facts_store import ...)
    from .output_store import (
        OutputStoreError, OwnershipError, _assert_local_destination,
        LOCAL_STORE_ROOT,
    )
except ImportError:  # flat import (import facts_store)
    from output_store import (
        OutputStoreError, OwnershipError, _assert_local_destination,
        LOCAL_STORE_ROOT,
    )

DEFAULT_STORE_PATH = LOCAL_STORE_ROOT / "facts.sqlite"


class FactsStoreError(OutputStoreError):
    """Base error for the period-keyed facts store."""


# ── Records ──────────────────────────────────────────────────────────────────

@dataclass
class FactRecord:
    """One reported fact for one period, from XBRL or extracted.

    The key is a PERIOD, not a document: ``(cik, concept, period_start,
    period_end, accession)``. There is no span and no spec — an XBRL fact has
    neither. ``source`` records where the number came from so an extracted fact
    is never mistaken for a filer-reported one.

    * ``concept`` — the taxonomy concept, e.g. "us-gaap:Revenues".
    * ``period_start`` / ``period_end`` — ISO YYYY-MM-DD. For an instant fact
      (a balance-sheet point) the two are equal.
    * ``fiscal_year`` / ``fiscal_period`` — e.g. 2025 / "Q3" or "FY", as the
      filer framed it (dei:DocumentFiscalYearFocus / PeriodFocus).
    * ``form`` — the filing that carried it: "10-Q", "10-K", ...
    * ``value`` — numeric; stored as JSON so an int stays an int.
    * ``source`` — "xbrl" (structured) or "extracted" (pulled from prose).
    """

    cik: str
    concept: str
    unit: str
    period_start: str
    period_end: str
    fiscal_year: int
    fiscal_period: str
    form: str
    accession: str
    value: object
    source: str = "xbrl"

    def __post_init__(self):
        if self.source not in ("xbrl", "extracted"):
            raise FactsStoreError(
                f"FactRecord.source must be 'xbrl' or 'extracted', "
                f"got {self.source!r}"
            )


@dataclass
class GuidanceRecord:
    """A guidance range read out of prose. Period-grained like a fact, but it
    points back at the document + span it was read from, because a human may need
    to check the extraction against the text.

    * ``metric`` — what is being guided, e.g. "revenue".
    * ``period_label`` — the filer's own words for the period, e.g. "FY2026",
      "Q4 2025". Prose guidance rarely gives clean start/end dates, so this is a
      label, not a date pair.
    * ``low`` / ``high`` — the guided range. A point estimate sets both equal.
    * ``basis`` — "GAAP", "non-GAAP", "constant currency", ...
    * ``span`` — (start, end) char offsets into ``document`` (the guidance loop's
      negative filter, #163, keys on this).
    * ``provenance`` / ``confidence`` — how it was pulled and the model's report.
    """

    cik: str
    metric: str
    period_label: str
    low: object
    high: object
    basis: str
    accession: str
    document: str
    span: tuple = None
    provenance: str = None
    confidence: float = None


# ── Store ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    ingested_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    cik           TEXT NOT NULL,
    concept       TEXT NOT NULL,
    unit          TEXT,
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    fiscal_year   INTEGER,
    fiscal_period TEXT,
    form          TEXT,
    accession     TEXT NOT NULL,
    value_json    TEXT,
    value_num     REAL,
    source        TEXT NOT NULL,
    UNIQUE (cik, concept, period_start, period_end, accession)
);

CREATE TABLE IF NOT EXISTS guidance (
    ingested_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    cik           TEXT NOT NULL,
    metric        TEXT NOT NULL,
    period_label  TEXT NOT NULL,
    low_json      TEXT,
    high_json     TEXT,
    basis         TEXT,
    accession     TEXT NOT NULL,
    document      TEXT NOT NULL,
    span_start    INTEGER,
    span_end      INTEGER,
    provenance    TEXT,
    confidence    REAL,
    UNIQUE (cik, metric, period_label, accession, document, span_start, span_end)
);

CREATE INDEX IF NOT EXISTS ix_facts_period
    ON facts(cik, concept, period_start, period_end);
CREATE INDEX IF NOT EXISTS ix_facts_cik      ON facts(cik);
CREATE INDEX IF NOT EXISTS ix_guid_metric    ON guidance(cik, metric);
"""


class FactsStore:
    """Local, period-keyed store for reported facts and prose guidance.

    Local-only by construction: the path is checked against OutputStore's
    ownership boundary at open time, and nothing here imports or writes a pipeline
    artifact.
    """

    def __init__(self, path=None, *, readonly=False):
        path = str(path) if path is not None else str(DEFAULT_STORE_PATH)
        _assert_local_destination(path)
        self.path = path

        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        if not readonly:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.readonly = readonly

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _guard_writable(self):
        if self.readonly:
            raise FactsStoreError("store opened read-only")

    # -- reported facts ----------------------------------------------------

    def put_facts(self, records):
        """Write a batch of ``FactRecord``. Idempotent on
        ``(cik, concept, period_start, period_end, accession)`` — writing the same
        batch twice leaves one row per key (INSERT OR REPLACE on the UNIQUE key).

        A different accession for the same period is a NEW row, not a replacement:
        that is what supersession needs (see ``latest`` / ``history``).
        """
        self._guard_writable()
        for r in records:
            if not isinstance(r, FactRecord):
                raise FactsStoreError(f"expected FactRecord, got {type(r).__name__}")
            value_num = (r.value if isinstance(r.value, (int, float))
                         and not isinstance(r.value, bool) else None)
            self._conn.execute(
                "INSERT OR REPLACE INTO facts "
                "(cik, concept, unit, period_start, period_end, fiscal_year, "
                " fiscal_period, form, accession, value_json, value_num, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r.cik, r.concept, r.unit, r.period_start, r.period_end,
                 r.fiscal_year, r.fiscal_period, r.form, r.accession,
                 json.dumps(r.value), value_num, r.source),
            )
        self._conn.commit()

    def latest(self, cik, concept, period_start, period_end):
        """The current record for one ``(cik, concept, period)`` — the superseding
        one when the period has been restated under a later accession. Ordered by
        accession (a later filing carries a later accession), with ingestion order
        as a tiebreak. Returns a dict, or None."""
        row = self._conn.execute(
            "SELECT * FROM facts "
            "WHERE cik = ? AND concept = ? AND period_start = ? AND period_end = ? "
            "ORDER BY accession DESC, ingested_seq DESC LIMIT 1",
            (cik, concept, period_start, period_end),
        ).fetchone()
        return self._row_to_fact(row) if row else None

    def history(self, cik, concept, period_start, period_end):
        """Every version of one ``(cik, concept, period)``, oldest first — a
        restated period keeps all its prior filings, so a change is visible rather
        than silently overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM facts "
            "WHERE cik = ? AND concept = ? AND period_start = ? AND period_end = ? "
            "ORDER BY accession ASC, ingested_seq ASC",
            (cik, concept, period_start, period_end),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def facts_for(self, cik, *, concept=None):
        """All latest-per-period facts for a CIK, optionally one concept — the
        shape a period-series reader wants. Collapses supersession to the current
        version of each period."""
        where, params = ["cik = ?"], [cik]
        if concept:
            where.append("concept = ?"); params.append(concept)
        rows = self._conn.execute(
            "SELECT * FROM facts WHERE " + " AND ".join(where) +
            " ORDER BY concept, period_end, accession DESC, ingested_seq DESC",
            params,
        ).fetchall()
        seen, kept = set(), []
        for r in rows:
            key = (r["concept"], r["period_start"], r["period_end"])
            if key in seen:
                continue
            seen.add(key)
            kept.append(self._row_to_fact(r))
        return kept

    @staticmethod
    def _row_to_fact(r):
        return {
            "cik": r["cik"],
            "concept": r["concept"],
            "unit": r["unit"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "fiscal_year": r["fiscal_year"],
            "fiscal_period": r["fiscal_period"],
            "form": r["form"],
            "accession": r["accession"],
            "value": json.loads(r["value_json"]) if r["value_json"] is not None else None,
            "value_num": r["value_num"],
            "source": r["source"],
        }

    # -- prose guidance ----------------------------------------------------

    def put_guidance(self, records):
        """Write a batch of ``GuidanceRecord``. Idempotent on
        ``(cik, metric, period_label, accession, document, span)``."""
        self._guard_writable()
        for r in records:
            if not isinstance(r, GuidanceRecord):
                raise FactsStoreError(
                    f"expected GuidanceRecord, got {type(r).__name__}")
            span_start, span_end = (r.span if r.span else (None, None))
            self._conn.execute(
                "INSERT OR REPLACE INTO guidance "
                "(cik, metric, period_label, low_json, high_json, basis, "
                " accession, document, span_start, span_end, provenance, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r.cik, r.metric, r.period_label, json.dumps(r.low),
                 json.dumps(r.high), r.basis, r.accession, r.document,
                 span_start, span_end, r.provenance, r.confidence),
            )
        self._conn.commit()

    def guidance_for(self, cik, *, metric=None):
        """Guidance records for a CIK, optionally one metric."""
        where, params = ["cik = ?"], [cik]
        if metric:
            where.append("metric = ?"); params.append(metric)
        rows = self._conn.execute(
            "SELECT * FROM guidance WHERE " + " AND ".join(where) +
            " ORDER BY metric, period_label, accession",
            params,
        ).fetchall()
        return [self._row_to_guidance(r) for r in rows]

    @staticmethod
    def _row_to_guidance(r):
        span = None
        if r["span_start"] is not None or r["span_end"] is not None:
            span = (r["span_start"], r["span_end"])
        return {
            "cik": r["cik"],
            "metric": r["metric"],
            "period_label": r["period_label"],
            "low": json.loads(r["low_json"]) if r["low_json"] is not None else None,
            "high": json.loads(r["high_json"]) if r["high_json"] is not None else None,
            "basis": r["basis"],
            "accession": r["accession"],
            "document": r["document"],
            "span": span,
            "provenance": r["provenance"],
            "confidence": r["confidence"],
        }


if __name__ == "__main__":
    # Self-check against an in-memory store — no files, no clock, no network.
    store = FactsStore(":memory:")
    q3 = FactRecord(
        cik="0000019617", concept="us-gaap:Revenues", unit="USD",
        period_start="2025-07-01", period_end="2025-09-30",
        fiscal_year=2025, fiscal_period="Q3", form="10-Q",
        accession="0000019617-25-000123", value=9_100_000_000, source="xbrl",
    )
    store.put_facts([q3])
    store.put_facts([q3])  # idempotent
    hist = store.history("0000019617", "us-gaap:Revenues",
                         "2025-07-01", "2025-09-30")
    print(f"after double-write, history has {len(hist)} row(s)")

    restated = FactRecord(
        cik="0000019617", concept="us-gaap:Revenues", unit="USD",
        period_start="2025-07-01", period_end="2025-09-30",
        fiscal_year=2025, fiscal_period="Q3", form="10-K",
        accession="0000019617-26-000045", value=9_050_000_000, source="xbrl",
    )
    store.put_facts([restated])
    latest = store.latest("0000019617", "us-gaap:Revenues",
                          "2025-07-01", "2025-09-30")
    print(f"latest value -> {latest['value']} from {latest['form']}")
    print(f"history spans -> {[h['accession'] for h in store.history(*['0000019617', 'us-gaap:Revenues', '2025-07-01', '2025-09-30'])]}")

    store.put_guidance([GuidanceRecord(
        cik="0000019617", metric="revenue", period_label="FY2026",
        low=38_000_000_000, high=39_000_000_000, basis="non-GAAP",
        accession="0000019617-26-000045", document="ex99.htm",
        span=(1044, 1120), provenance="model:qwen2.5-7b", confidence=0.82,
    )])
    print(f"guidance -> {store.guidance_for('0000019617')[0]['period_label']}")
    print("\nfacts_store self-check: PASS")
