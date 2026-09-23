"""
EDGAR scrubber output store (issue #109) — the local, append-only tool datastore.

This module is the *only* place a scrubber extraction is written, and it writes to
exactly one thing: a local SQLite file that belongs to this tool. It exists to
DEFEND an ownership boundary, so read the boundary before the API:

  * ARCHITECTURE.md is explicit: the Stock-Data-Pipeline is the ONLY writer of
    shared data, and "don't fork the ingest scripts." Stock-Data-Pipeline already
    ingests EDGAR. Without a stated rule, this scrubber becomes a second permanent
    EDGAR ingest path with its own datastore — and calcifies.
  * So the rule this file enforces in code: **the scrubber is exploratory,
    human-supervised, and project-local. It is not a production feed.** Output is
    a local tool datastore, never a pipeline write. Trader-Screener reads scrubber
    output the same way it reads everything else — as a reader.
  * There is deliberately no import of, and no code path to, any pipeline artifact.
    `OwnershipError` fires if the store is even *pointed* at a pipeline location,
    so the boundary is a check, not a comment.

Graduation (the exit from local):
  A field is allowed to stay local ONLY while it is being explored. When it proves
  out inside a Research project it GRADUATES — its spec + induced rules port into
  the pipeline's EDGAR ingest, it joins the shared field contract, and the local
  extractor is RETIRED, not left running in parallel. `graduate_field()` records
  that, and afterwards `write_document()` REFUSES to re-extract that field locally:
  two extractors for one field is two answers for one field.

Output format (what a record carries):
  One row per ``(run, accession, document, field)`` with value, unit, span offsets,
  provenance, confidence, and flags. Queryable by underlying, issuer, product type,
  and date — the shape the first Research project (#110, issuance-by-underlying)
  needs. Append-only with run stamps: a re-extraction under a new spec version does
  NOT destroy the prior read, so you can compare extractions across spec versions
  and catch a spec change that silently altered history.

stdlib only (sqlite3). Run the self-check:  python tools/edgar_scrubber/output_store.py
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field as dc_field
from pathlib import Path

# ── Ownership boundary ───────────────────────────────────────────────────────

# The scrubber's default home. A local, per-tool directory — never a shared or
# pipeline volume. Overridable with EDGAR_SCRUBBER_HOME for a different local dir.
LOCAL_STORE_ROOT = Path(
    os.getenv("EDGAR_SCRUBBER_HOME", Path.home() / ".edgar-scrubber")
) / "store"
DEFAULT_STORE_PATH = LOCAL_STORE_ROOT / "extractions.sqlite"

# Markers of the shared pipeline. If a store path smells like any of these, we
# refuse to open it for writing — the scrubber must never become a second writer
# of pipeline data. This is the code embodiment of ARCHITECTURE.md's "only writer"
# rule; it is intentionally conservative (a false positive is a renamed store
# file, a false negative would be a forked ingest path).
_PIPELINE_DIR_MARKERS = ("stock-data-pipeline",)
_PIPELINE_ARTIFACTS = (
    "universe.json", "fundamentals.json", "model.json", "etf_universe.json",
    "options.json", "news.json",
)


class OutputStoreError(RuntimeError):
    """Base error for the local output store."""


class OwnershipError(OutputStoreError):
    """The store was pointed at something other than its local tool datastore.

    The pipeline is the only writer of shared data (ARCHITECTURE.md); the scrubber
    is a reader everywhere else. There is deliberately no code path from this store
    to a pipeline artifact, and this error guards the one place a caller could try
    to open one."""


class GraduationError(OwnershipError):
    """A graduated field was extracted locally again.

    Once a field graduates upstream its local extractor is retired. Re-extracting
    it here would give two answers for one field, which is exactly the drift the
    graduation rule exists to prevent."""


def _assert_local_destination(path):
    """Reject any store path that resolves to a shared / pipeline location.

    ``:memory:`` and ordinary local paths pass. A path under a Stock-Data-Pipeline
    tree, or one named like a pipeline artifact, or one under $STOCK_PIPELINE_DATA
    is refused. Raises OwnershipError."""
    if path == ":memory:":
        return
    p = Path(path).expanduser()
    parts_lower = {part.lower() for part in p.parts}

    for marker in _PIPELINE_DIR_MARKERS:
        if marker in parts_lower:
            raise OwnershipError(
                f"refusing to open a scrubber store at {p}: it lives under a "
                f"'{marker}' tree. The scrubber writes ONLY to its local tool "
                f"datastore; the pipeline is the only writer of shared data "
                f"(ARCHITECTURE.md)."
            )

    if p.name.lower() in _PIPELINE_ARTIFACTS:
        raise OwnershipError(
            f"refusing to open a scrubber store named {p.name!r}: that is a "
            f"pipeline artifact name. The scrubber never writes a pipeline file."
        )

    shared = os.getenv("STOCK_PIPELINE_DATA")
    if shared:
        try:
            p.resolve().relative_to(Path(shared).expanduser().resolve())
        except ValueError:
            pass
        else:
            raise OwnershipError(
                f"refusing to open a scrubber store under $STOCK_PIPELINE_DATA "
                f"({shared}). That volume belongs to the pipeline; the scrubber "
                f"keeps its own local store."
            )


# ── Records ──────────────────────────────────────────────────────────────────

@dataclass
class FieldValue:
    """One extracted field for one document. This is the grain the store keys on:
    ``(accession, document, field)`` within a run.

    * ``span`` — (start, end) character offsets into the source document, so a
      later reader can point back at the exact text the value came from.
    * ``provenance`` — how it was pulled: e.g. "rule:anchor 'Issuer:'" for a
      regex hit (#107) or "model:qwen2.5-7b" for a variable field (#104).
    * ``confidence`` — 0..1. The field-spec bounds flags are the *checkable*
      confidence signal (#102); this is the model's self-report, kept alongside.
    * ``flags`` — the field_spec validation flags (list of dicts). A record is
      never dropped for failing validation — it travels flagged.
    """

    field: str
    value: object
    unit: str = None
    span: tuple = None          # (start, end) char offsets, or None
    provenance: str = None
    confidence: float = None
    flags: list = dc_field(default_factory=list)
    # How many deterministic candidates the value was chosen from (#179). The
    # candidate LIST is scratch and is never stored; the COUNT survives, because
    # "chosen from 3" and "chosen from 47" are different facts about the same
    # answer, and a count of 0 is exactly the free-form fallback, visible after
    # the fact. Only the chosen value, its span and this count reach the store.
    candidate_count: int = 0


@dataclass
class DocumentExtraction:
    """Everything pulled from one document under one spec, ready to append.

    The document-level dimensions (issuer, product_type, filing_date, underlyings)
    are denormalized out of ``fields`` so the store is queryable by them — that is
    the shape the first Research project needs. They may be passed explicitly or
    left to ``from_record()`` to lift them out of a canonical extraction record.
    """

    accession: str
    document: str
    issuer: str = None
    product_type: str = None
    filing_date: str = None      # ISO YYYY-MM-DD — the queryable date
    underlyings: list = dc_field(default_factory=list)  # [{"name", "kind"}, ...]
    fields: list = dc_field(default_factory=list)       # [FieldValue, ...]

    @classmethod
    def from_record(cls, accession, document, record, *, units=None,
                    spans=None, provenance=None, confidence=None, flags=None,
                    candidate_counts=None, filing_date_field="pricing_date"):
        """Build from a canonical-keyed extraction record (the field_spec output).

        Lifts issuer / product_type / underlyings / the filing date out of the
        record into queryable dimensions, and turns every remaining key into a
        FieldValue. Per-field metadata dicts (units/spans/provenance/confidence/
        flags) are keyed by field name; all optional.
        """
        units = units or {}
        spans = spans or {}
        provenance = provenance or {}
        confidence = confidence or {}
        flags = flags or {}
        candidate_counts = candidate_counts or {}

        fields = []
        for name, value in record.items():
            fields.append(FieldValue(
                field=name,
                value=value,
                unit=units.get(name),
                span=spans.get(name),
                provenance=provenance.get(name),
                confidence=confidence.get(name),
                flags=flags.get(name, []),
                candidate_count=candidate_counts.get(name, 0),
            ))

        return cls(
            accession=accession,
            document=document,
            issuer=record.get("issuer"),
            product_type=record.get("product_type") or record.get("security_type"),
            filing_date=record.get(filing_date_field) or record.get("pricing_date"),
            underlyings=list(record.get("underlyings") or []),
            fields=fields,
        )


@dataclass
class Run:
    """A single extraction pass. Every write is stamped with one, so a
    re-extraction under a new spec version lands as a NEW run beside the old one
    rather than overwriting it."""

    run_id: str
    spec_id: str
    spec_version: str
    started_at: str              # ISO 8601, supplied by the caller
    note: str = None


# ── Store ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    spec_id      TEXT NOT NULL,
    spec_version TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    run_id       TEXT NOT NULL,
    accession    TEXT NOT NULL,
    document     TEXT NOT NULL,
    issuer       TEXT,
    product_type TEXT,
    filing_date  TEXT,
    PRIMARY KEY (run_id, accession, document),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS document_underlyings (
    run_id    TEXT NOT NULL,
    accession TEXT NOT NULL,
    document  TEXT NOT NULL,
    name      TEXT NOT NULL,
    kind      TEXT
);

CREATE TABLE IF NOT EXISTS extractions (
    run_id       TEXT NOT NULL,
    accession    TEXT NOT NULL,
    document     TEXT NOT NULL,
    field        TEXT NOT NULL,
    value_json   TEXT,
    value_num    REAL,
    unit         TEXT,
    span_start   INTEGER,
    span_end     INTEGER,
    provenance   TEXT,
    confidence   REAL,
    flags_json   TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS graduations (
    field         TEXT PRIMARY KEY,
    graduated_to  TEXT NOT NULL,
    graduated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_doc_issuer  ON documents(issuer);
CREATE INDEX IF NOT EXISTS ix_doc_ptype   ON documents(product_type);
CREATE INDEX IF NOT EXISTS ix_doc_date    ON documents(filing_date);
CREATE INDEX IF NOT EXISTS ix_und_name    ON document_underlyings(name);
CREATE INDEX IF NOT EXISTS ix_ext_field   ON extractions(field);
CREATE INDEX IF NOT EXISTS ix_ext_doc     ON extractions(accession, document);
"""


class OutputStore:
    """Local, append-only, queryable store for scrubber extractions.

    Append-only by construction: the public surface inserts and selects. There is
    no update or delete method, so a re-extraction is a new run, never a mutation
    of a prior read.

    Local-only by construction: the path is checked against the ownership boundary
    at open time, and nothing here imports or writes a pipeline artifact.
    """

    def __init__(self, path=None, *, readonly=False):
        path = str(path) if path is not None else str(DEFAULT_STORE_PATH)
        _assert_local_destination(path)
        self.path = path

        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if not readonly:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        self.readonly = readonly

    def _migrate(self):
        """Additive migrations for a store opened from an older run (#179). A new
        column is added only when the CREATE TABLE IF NOT EXISTS above found a
        pre-existing `extractions` table without it, so the append-only history is
        never rebuilt. Old rows read back their DEFAULT, which for candidate_count
        is 0 -- the free-form value, honest for rows written before the count."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(extractions)").fetchall()}
        if "candidate_count" not in cols:
            self._conn.execute(
                "ALTER TABLE extractions ADD COLUMN candidate_count "
                "INTEGER NOT NULL DEFAULT 0")

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _guard_writable(self):
        if self.readonly:
            raise OutputStoreError("store opened read-only")

    # -- runs --------------------------------------------------------------

    def start_run(self, spec_id, spec_version, started_at, *, run_id=None, note=None):
        """Open a run and return its id. ``started_at`` is an ISO-8601 string the
        caller supplies (the store does not read the clock — that keeps writes
        reproducible and testable). A run_id is derived if not given."""
        self._guard_writable()
        if run_id is None:
            seq = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] + 1
            run_id = f"{spec_id}@{spec_version}#{seq:04d}"
        self._conn.execute(
            "INSERT INTO runs (run_id, spec_id, spec_version, started_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, spec_id, spec_version, started_at, note),
        )
        self._conn.commit()
        return run_id

    def runs(self):
        """All runs, newest stamp first — the run history that append-only buys."""
        rows = self._conn.execute(
            "SELECT run_id, spec_id, spec_version, started_at, note "
            "FROM runs ORDER BY started_at DESC, run_id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def last_used(self):
        """ISO stamp of the most recent run, or None. This is what the tool
        manifest surfaces as 'age since last use' so drift is visible: a field
        local for a long time with no project consuming it is drift, not
        exploration."""
        row = self._conn.execute(
            "SELECT MAX(started_at) FROM runs"
        ).fetchone()
        return row[0] if row and row[0] else None

    # -- writes (append-only, local-only) ----------------------------------

    def write_document(self, run_id, doc):
        """Append one document's extraction under ``run_id``.

        Refuses any field that has already graduated: its local extractor is
        retired, and re-extracting it here would be a second answer for one field.
        """
        self._guard_writable()
        self._require_run(run_id)

        graduated = self.graduated_fields()
        offending = [fv.field for fv in doc.fields if fv.field in graduated]
        if offending:
            raise GraduationError(
                f"fields {sorted(offending)} have graduated to the pipeline; their "
                f"local extractor is retired. Read them from the shared field "
                f"contract, not from the scrubber (graduatedTo: "
                f"{ {f: graduated[f] for f in offending} })."
            )

        self._conn.execute(
            "INSERT OR REPLACE INTO documents "
            "(run_id, accession, document, issuer, product_type, filing_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, doc.accession, doc.document, doc.issuer,
             doc.product_type, doc.filing_date),
        )

        for u in doc.underlyings:
            if isinstance(u, dict):
                name, kind = u.get("name"), u.get("kind")
            else:
                name, kind = str(u), None
            if not name:
                continue
            self._conn.execute(
                "INSERT INTO document_underlyings "
                "(run_id, accession, document, name, kind) VALUES (?, ?, ?, ?, ?)",
                (run_id, doc.accession, doc.document, name, kind),
            )

        for fv in doc.fields:
            self._insert_field(run_id, doc.accession, doc.document, fv)

        self._conn.commit()

    def record(self, run_id, accession, document, fv):
        """Append a single ``(accession, document, field)`` row. Lower-level than
        write_document; the same graduation guard applies."""
        self._guard_writable()
        self._require_run(run_id)
        if fv.field in self.graduated_fields():
            raise GraduationError(
                f"field {fv.field!r} has graduated; its local extractor is retired."
            )
        self._insert_field(run_id, accession, document, fv)
        self._conn.commit()

    def _insert_field(self, run_id, accession, document, fv):
        span_start, span_end = (fv.span if fv.span else (None, None))
        value_num = fv.value if isinstance(fv.value, (int, float)) and not isinstance(fv.value, bool) else None
        self._conn.execute(
            "INSERT INTO extractions "
            "(run_id, accession, document, field, value_json, value_num, unit, "
            " span_start, span_end, provenance, confidence, flags_json, "
            " candidate_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, accession, document, fv.field,
             json.dumps(fv.value), value_num, fv.unit,
             span_start, span_end, fv.provenance, fv.confidence,
             json.dumps(fv.flags or []), int(fv.candidate_count or 0)),
        )

    def _require_run(self, run_id):
        exists = self._conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not exists:
            raise OutputStoreError(f"unknown run_id {run_id!r}; call start_run first")

    # -- graduation --------------------------------------------------------

    def graduate_field(self, field, graduated_to, graduated_at):
        """Mark a field as graduated upstream and retire its local extractor.

        ``graduated_to`` is where it now lives in the pipeline (the manifest's
        ``graduatedTo``). After this, write_document/record refuse the field."""
        self._guard_writable()
        self._conn.execute(
            "INSERT OR REPLACE INTO graduations (field, graduated_to, graduated_at) "
            "VALUES (?, ?, ?)",
            (field, graduated_to, graduated_at),
        )
        self._conn.commit()

    def graduated_fields(self):
        """{field: graduated_to} for every retired field."""
        rows = self._conn.execute(
            "SELECT field, graduated_to FROM graduations"
        ).fetchall()
        return {r["field"]: r["graduated_to"] for r in rows}

    # -- queries -----------------------------------------------------------

    def query(self, *, underlying=None, issuer=None, product_type=None,
              date_from=None, date_to=None, run_id=None, latest=True):
        """Documents matching the given dimensions.

        Queryable by underlying, issuer, product type, and date — the first
        Research project's shape. ``latest`` keeps only the most recent run per
        ``(accession, document)`` so a caller reads current values by default;
        pass ``latest=False`` (or a specific ``run_id``) to see history across
        spec versions.

        Returns a list of dicts: run/spec stamp, the document dimensions, and the
        document's underlyings.
        """
        where, params = [], []
        if issuer:
            where.append("d.issuer = ?"); params.append(issuer)
        if product_type:
            where.append("d.product_type = ?"); params.append(product_type)
        if date_from:
            where.append("d.filing_date >= ?"); params.append(date_from)
        if date_to:
            where.append("d.filing_date <= ?"); params.append(date_to)
        if run_id:
            where.append("d.run_id = ?"); params.append(run_id)
        if underlying:
            where.append(
                "EXISTS (SELECT 1 FROM document_underlyings u "
                "WHERE u.run_id = d.run_id AND u.accession = d.accession "
                "AND u.document = d.document AND u.name = ?)"
            )
            params.append(underlying)

        sql = (
            "SELECT d.run_id, d.accession, d.document, d.issuer, d.product_type, "
            "       d.filing_date, r.spec_id, r.spec_version, r.started_at "
            "FROM documents d JOIN runs r ON r.run_id = d.run_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.filing_date, d.accession, d.document, r.started_at DESC"

        rows = [dict(r) for r in self._conn.execute(sql, params).fetchall()]

        if latest and not run_id:
            seen, kept = set(), []
            for r in sorted(rows, key=lambda x: x["started_at"], reverse=True):
                key = (r["accession"], r["document"])
                if key in seen:
                    continue
                seen.add(key)
                kept.append(r)
            rows = sorted(kept, key=lambda x: (x["filing_date"] or "",
                                               x["accession"], x["document"]))

        for r in rows:
            r["underlyings"] = self._underlyings_for(
                r["run_id"], r["accession"], r["document"])
        return rows

    def _underlyings_for(self, run_id, accession, document):
        rows = self._conn.execute(
            "SELECT name, kind FROM document_underlyings "
            "WHERE run_id = ? AND accession = ? AND document = ?",
            (run_id, accession, document),
        ).fetchall()
        return [dict(r) for r in rows]

    def fields(self, accession, document, *, run_id=None):
        """Every field row for one document. Without ``run_id`` returns the most
        recent run's values; the raw rows carry value, unit, span, provenance,
        confidence, and flags."""
        if run_id is None:
            run_id = self._latest_run_for(accession, document)
            if run_id is None:
                return []
        rows = self._conn.execute(
            "SELECT field, value_json, value_num, unit, span_start, span_end, "
            "       provenance, confidence, flags_json, candidate_count "
            "FROM extractions WHERE run_id = ? AND accession = ? AND document = ? "
            "ORDER BY field",
            (run_id, accession, document),
        ).fetchall()
        return [self._row_to_field(r) for r in rows]

    def field_history(self, accession, document, field):
        """The value of one field across every run, oldest first — how you catch a
        spec change that silently altered history. Each entry carries the run's
        spec_version and stamp beside the value."""
        rows = self._conn.execute(
            "SELECT e.run_id, r.spec_version, r.started_at, e.field, "
            "       e.value_json, e.value_num, e.unit, e.span_start, e.span_end, "
            "       e.provenance, e.confidence, e.flags_json, e.candidate_count "
            "FROM extractions e JOIN runs r ON r.run_id = e.run_id "
            "WHERE e.accession = ? AND e.document = ? AND e.field = ? "
            "ORDER BY r.started_at, e.run_id",
            (accession, document, field),
        ).fetchall()
        out = []
        for r in rows:
            entry = self._row_to_field(r)
            entry.update(run_id=r["run_id"], spec_version=r["spec_version"],
                         started_at=r["started_at"])
            out.append(entry)
        return out

    def _latest_run_for(self, accession, document):
        row = self._conn.execute(
            "SELECT d.run_id FROM documents d JOIN runs r ON r.run_id = d.run_id "
            "WHERE d.accession = ? AND d.document = ? "
            "ORDER BY r.started_at DESC, d.run_id DESC LIMIT 1",
            (accession, document),
        ).fetchone()
        return row["run_id"] if row else None

    @staticmethod
    def _row_to_field(r):
        span = None
        if r["span_start"] is not None or r["span_end"] is not None:
            span = (r["span_start"], r["span_end"])
        return {
            "field": r["field"],
            "value": json.loads(r["value_json"]) if r["value_json"] is not None else None,
            "value_num": r["value_num"],
            "unit": r["unit"],
            "span": span,
            "provenance": r["provenance"],
            "confidence": r["confidence"],
            "flags": json.loads(r["flags_json"]) if r["flags_json"] else [],
            "candidate_count": r["candidate_count"],
        }

    # -- manifest support --------------------------------------------------

    def manifest_stats(self):
        """The facts the tool manifest surfaces for this store: whether any field
        has graduated, and the last-used stamp so age (and therefore drift) is
        visible rather than inferred."""
        graduated = self.graduated_fields()
        n_docs = self._conn.execute(
            "SELECT COUNT(DISTINCT accession || '/' || document) FROM documents"
        ).fetchone()[0]
        return {
            "last_used": self.last_used(),
            "run_count": self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "document_count": n_docs,
            "graduated_fields": graduated,
            "status": "graduated" if graduated else "local",
        }


if __name__ == "__main__":
    # Self-check against an in-memory store — no files, no clock, no network.
    store = OutputStore(":memory:")
    rid = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")
    doc = DocumentExtraction.from_record(
        "0001-24-000001", "424b2.htm",
        {
            "issuer": "JPMorgan Chase Financial Company LLC",
            "product_type": "autocallable contingent coupon",
            "pricing_date": "2026-01-15",
            "underlyings": [{"name": "S&P 500 Index", "kind": "index"}],
            "estimated_value_per_1000": 972.4,
        },
        units={"estimated_value_per_1000": "usd_per_1000"},
        spans={"estimated_value_per_1000": (10432, 10440)},
        provenance={"estimated_value_per_1000": "rule:anchor 'our estimated value'"},
        confidence={"estimated_value_per_1000": 0.98},
    )
    store.write_document(rid, doc)

    hits = store.query(underlying="S&P 500 Index")
    print(f"query by underlying -> {len(hits)} doc(s): "
          f"{hits[0]['issuer']} / {hits[0]['product_type']}")
    print(f"last used -> {store.last_used()}")
    print(f"manifest stats -> {store.manifest_stats()}")
    print("\noutput_store self-check: PASS")
