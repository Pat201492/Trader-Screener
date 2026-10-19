"""
Row store for structured-XML filings (issue #283).

``xml_forms.read_filing`` lifts a structured filing (Form 3/4/5 today, 13F-HR /
SC 13D-G / Form D later) into a list of row dicts. This is where those rows live:
one local SQLite store, keyed by the filing they came from.

Two tables, mirroring the grain of the reader output:

  * ``documents(run_id, accession, form, cik, filed)`` — one row per filing
    ingested, so a reader's output can be found by what produced it and queried by
    form / filer / filed date.
  * ``rows(run_id, accession, row_index, data_json)`` — one row per reader row,
    the dict serialized to JSON, ordered by ``row_index`` within its filing.

Re-ingesting a filing replaces it wholesale (its ``documents`` row and all its
``rows``), so a corrected re-read never leaves stale rows behind.

Local-only by construction: the path is checked against OutputStore's ownership
boundary (ARCHITECTURE.md — the pipeline is the only writer of shared data; the
scrubber keeps its own local datastore). stdlib ``sqlite3`` only, no network.

Run the self-check:  python tools/edgar_scrubber/forms_store.py
"""

import json
import sqlite3
from pathlib import Path

# Both flat (script / pytest via conftest) and package (relative) import styles
# must resolve, exactly as facts_store does it.
try:  # package import (from .forms_store import ...)
    from .output_store import (
        OutputStoreError, _assert_local_destination, LOCAL_STORE_ROOT,
    )
    from .edgar_client import cik10
except ImportError:  # flat import (import forms_store)
    from output_store import (
        OutputStoreError, _assert_local_destination, LOCAL_STORE_ROOT,
    )
    from edgar_client import cik10

DEFAULT_STORE_PATH = LOCAL_STORE_ROOT / "forms.sqlite"


class FormsStoreError(OutputStoreError):
    """Base error for the structured-XML forms store."""


def _norm_cik(cik):
    """Canonicalise a CIK to the 10-digit zero-padded form so one filer cannot
    land under two keys (bare ``320193`` vs ``0000320193``). A value with no
    digits is left untouched rather than raising, so a malformed key never crashes
    a write or a read."""
    if cik is None:
        return None
    try:
        return cik10(cik)
    except (ValueError, TypeError):
        return str(cik)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    run_id     TEXT NOT NULL,
    accession  TEXT NOT NULL,
    form       TEXT NOT NULL,
    cik        TEXT,
    filed      TEXT,
    PRIMARY KEY (run_id, accession)
);

CREATE TABLE IF NOT EXISTS rows (
    run_id     TEXT NOT NULL,
    accession  TEXT NOT NULL,
    row_index  INTEGER NOT NULL,
    data_json  TEXT NOT NULL,
    PRIMARY KEY (run_id, accession, row_index)
);

CREATE INDEX IF NOT EXISTS ix_documents_form  ON documents(form);
CREATE INDEX IF NOT EXISTS ix_documents_cik   ON documents(cik);
CREATE INDEX IF NOT EXISTS ix_documents_filed ON documents(filed);
"""


class FormsStore:
    """Local store for structured-XML filing rows.

    Local-only by construction: the path is checked against OutputStore's
    ownership boundary at open time.
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
            raise FormsStoreError("store opened read-only")

    # -- writes ------------------------------------------------------------

    def put_filing(self, run_id, accession, form, cik, filed, rows):
        """Store one filing's reader output: a ``documents`` row plus one ``rows``
        row per dict in ``rows``. Idempotent on ``(run_id, accession)`` — re-ingest
        replaces the document and ALL of its rows (old rows are deleted first), so
        a corrected re-read never leaves stale rows behind."""
        self._guard_writable()
        self._conn.execute(
            "INSERT OR REPLACE INTO documents "
            "(run_id, accession, form, cik, filed) VALUES (?, ?, ?, ?, ?)",
            (run_id, accession, str(form), _norm_cik(cik), filed),
        )
        self._conn.execute(
            "DELETE FROM rows WHERE run_id = ? AND accession = ?",
            (run_id, accession),
        )
        for i, row in enumerate(rows):
            self._conn.execute(
                "INSERT INTO rows (run_id, accession, row_index, data_json) "
                "VALUES (?, ?, ?, ?)",
                (run_id, accession, i, json.dumps(row)),
            )
        self._conn.commit()

    # -- reads -------------------------------------------------------------

    def rows_for(self, run_id, accession):
        """Every stored row for one ``(run_id, accession)``, in ``row_index``
        order, each decoded back to the dict the reader produced."""
        cur = self._conn.execute(
            "SELECT data_json FROM rows WHERE run_id = ? AND accession = ? "
            "ORDER BY row_index",
            (run_id, accession),
        )
        return [json.loads(r["data_json"]) for r in cur.fetchall()]

    def query(self, form, cik=None, date_from=None, date_to=None):
        """Filings of one ``form``, optionally narrowed by ``cik`` and by a
        ``filed`` date range (inclusive, ISO ``YYYY-MM-DD``). Returns a list of
        dicts, one per filing, each ``{run_id, accession, form, cik, filed, rows}``
        with its decoded reader rows attached, ordered by filed date then
        accession."""
        where, params = ["form = ?"], [str(form)]
        if cik is not None:
            where.append("cik = ?"); params.append(_norm_cik(cik))
        if date_from is not None:
            where.append("filed >= ?"); params.append(date_from)
        if date_to is not None:
            where.append("filed <= ?"); params.append(date_to)
        cur = self._conn.execute(
            "SELECT run_id, accession, form, cik, filed FROM documents "
            "WHERE " + " AND ".join(where) +
            " ORDER BY filed, accession",
            params,
        )
        out = []
        for d in cur.fetchall():
            out.append({
                "run_id": d["run_id"],
                "accession": d["accession"],
                "form": d["form"],
                "cik": d["cik"],
                "filed": d["filed"],
                "rows": self.rows_for(d["run_id"], d["accession"]),
            })
        return out


if __name__ == "__main__":
    # Self-check against an in-memory store — no files, no network.
    store = FormsStore(":memory:")
    rows = [
        {"table": "non_derivative", "security_title": "Common Stock",
         "shares": 511000, "price": 170.0, "acquired_disposed": "D"},
        {"table": "derivative", "security_title": "Restricted Stock Unit",
         "shares": 90000, "price": None, "acquired_disposed": "A"},
    ]
    store.put_filing("run-1", "0000320193-26-000031", "4", "320193",
                     "2026-03-03", rows)
    store.put_filing("run-1", "0000320193-26-000031", "4", "320193",
                     "2026-03-03", rows)  # idempotent re-ingest

    hits = store.query("4", cik="0000320193",
                       date_from="2026-01-01", date_to="2026-12-31")
    print(f"query('4', AAPL, 2026) -> {len(hits)} filing(s), "
          f"{len(hits[0]['rows'])} row(s)")
    print(f"first row price -> {hits[0]['rows'][0]['price']}")
    print(f"out-of-range query -> {store.query('4', date_from='2027-01-01')}")
    print(f"other-form query -> {store.query('13F-HR')}")
    print("\nforms_store self-check: PASS")
