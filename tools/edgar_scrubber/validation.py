"""
Human-in-loop validation loop (issue #105, part of #95).

The kicker in #95's original request: pop the first N documents, validate them
by hand, and the extractor LEARNS your field definitions before it runs
unattended. This is the highest-leverage issue in the chain because its output
feeds three consumers -- exemplars (#106), hand-seeded regex rules (#107), and
the held-out eval set (#108). Ten validated minutes here removes hours of GPU
time downstream.

This module is the CORE of that loop -- everything except the terminal I/O,
which lives in `validate_ui.py` so the parts that matter (span resolution,
verdict persistence, the compounding exemplar write, rule-seed detection,
resumability) are stdlib-only and testable without a keyboard.

The screen `validate_ui.py` draws is two panes: left, the rendered document
scrolled to the field's section with the CANDIDATE SPAN HIGHLIGHTED; right, the
proposed value with accept / correct / reject. Span highlighting is the whole
ergonomic argument: a proposed `barrier_pct` of 70% tells you nothing about
whether it came from Key Terms or a hypothetical-example table three pages
later -- the highlight tells you instantly. This module produces that
highlight (`render_highlight`) and turns a hand-marked correction back into
exact source offsets (`locate_span`).

Three verdicts, all informative:

  * ACCEPT  -- value and span both correct. A positive exemplar and a rule seed.
  * CORRECT -- wrong value, or the right value from the wrong span. You mark the
               true span. The most valuable labels in the system: they teach the
               boundary the model got wrong, which a plain accept cannot.
  * REJECT  -- field genuinely absent. Also a label: it teaches that absence is
               a valid answer and suppresses the value that would be hallucinated.

Every verdict writes to the exemplar store (#106) IMMEDIATELY, so document 11
already benefits from your work on 1-10 -- batching the learning until session
end throws away the compounding that makes this design work. The store doubles
as the callable `exemplars=` provider `extraction_ladder.ExtractionLadder`
already accepts (rung 3), so closing the loop is a wiring detail, not a rewrite.

Corrected spans persist with EXACT source offsets that resolve through all four
of #101's reduction stages: `LadderExtractor` composes the model's text-offset
answer through the reduction `OffsetMap` (`build_field_context`), and a
hand-marked correction resolves through the same map -- so a value marked in a
boilerplate-stripped, section-split, sub-blocked fragment still slices the
ORIGINAL document bytes exactly.

stdlib only (sqlite3). Run the self-check:  python tools/edgar_scrubber/validation.py
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

try:  # package import: tools.edgar_scrubber.validation
    from .normalize import OffsetMap, TextSpan, NormalizedDocument
    from .reduce import split_sections, sections_for_spec, ReducedDocument
    from .output_store import LOCAL_STORE_ROOT, _assert_local_destination
except ImportError:  # standalone: python tools/edgar_scrubber/validation.py
    from normalize import OffsetMap, TextSpan, NormalizedDocument
    from reduce import split_sections, sections_for_spec, ReducedDocument
    from output_store import LOCAL_STORE_ROOT, _assert_local_destination


# The validation loop keeps its own local SQLite file beside the output store,
# under the same local-only home and the same ownership boundary (never a
# pipeline volume -- see output_store._assert_local_destination).
DEFAULT_VALIDATION_PATH = LOCAL_STORE_ROOT / "validation.sqlite"

# Verdict vocabulary. Kept as bare strings (not an Enum) so they round-trip
# through JSON / SQLite unchanged and read the same in the store as in code.
ACCEPT = "accept"
CORRECT = "correct"
REJECT = "reject"
VERDICTS = (ACCEPT, CORRECT, REJECT)


# --------------------------------------------------------------------------- #
# Render document -- the one text + map + source a pane needs
# --------------------------------------------------------------------------- #

@dataclass
class RenderDocument:
    """What a pane renders and what a correction resolves against: the reduced
    (or normalized) text a reader actually sees, the `OffsetMap` from THAT text
    straight back to the original document bytes, and the original bytes to
    slice.

    A `NormalizedDocument` already carries all three (`from_normalized`). A
    `ReducedDocument` (the output of stages 2-4) carries text + a composed map
    but not the source, so `from_reduced` takes the original source alongside
    -- the map already composes back to it, per #101.
    """

    text: str
    offset_map: OffsetMap
    source: str

    @classmethod
    def from_normalized(cls, doc):
        return cls(text=doc.text, offset_map=doc.offset_map, source=doc.source)

    @classmethod
    def from_reduced(cls, reduced, source):
        return cls(text=reduced.text, offset_map=reduced.offset_map, source=source)

    def text_span_for_source(self, source_span):
        """Where a SOURCE-offset span lands in this document's text, so a pane
        can highlight it. Uses `OffsetMap.text_offset_for_source` (the #101
        stage-4 inverse). Returns None if the span does not resolve here."""
        if source_span is None:
            return None
        t0 = self.offset_map.text_offset_for_source(source_span[0])
        t1 = self.offset_map.text_offset_for_source(source_span[1])
        if t0 is None or t1 is None:
            return None
        return (min(t0, t1), max(t0, t1))

    def source_text(self, source_span):
        """The normalized text a source span covers -- the human-readable form
        used for exemplars and the highlighted snippet (raw source would carry
        HTML tags)."""
        ts = self.text_span_for_source(source_span)
        if ts is None:
            return ""
        return self.text[ts[0]:ts[1]]


# --------------------------------------------------------------------------- #
# Proposals + verdicts
# --------------------------------------------------------------------------- #

@dataclass
class FieldProposal:
    """One extracted field awaiting a verdict. `source_span` is in ORIGINAL
    document offsets (the shape `output_store.FieldValue.span` persists), so it
    resolves back through the reduction map into the rendered document; None
    means the model could not locate a span -- itself a strong correct/reject
    signal."""

    field: str
    value: object
    source_span: tuple = None
    provenance: str = None
    confidence: float = None
    rung: str = None
    unit: str = None
    flags: list = _dc_field(default_factory=list)   # list of dicts (Flag.as_dict())


@dataclass
class FieldVerdict:
    """A human's ruling on one proposal.

    * ACCEPT  -- `value`/`source_span` are the proposal's, unchanged.
    * CORRECT -- `value` is the true value and/or `source_span` the true span
                 (either may change; a right value from the wrong span is still
                 a correction).
    * REJECT  -- field absent: `value` is None and `source_span` is None.

    `anchor` is filled in by the session from the resolved span (the label the
    value sits under) -- it is what rule-seed stability is measured on.
    """

    field: str
    verdict: str
    value: object = None
    source_span: tuple = None
    anchor: str = None
    note: str = None

    @classmethod
    def accept(cls, proposal):
        return cls(field=proposal.field, verdict=ACCEPT,
                   value=proposal.value, source_span=proposal.source_span)

    @classmethod
    def correct(cls, field, value, source_span=None, note=None):
        return cls(field=field, verdict=CORRECT, value=value,
                   source_span=source_span, note=note)

    @classmethod
    def reject(cls, field, note=None):
        return cls(field=field, verdict=REJECT, value=None,
                   source_span=None, note=note)


# --------------------------------------------------------------------------- #
# Highlight rendering + span marking
# --------------------------------------------------------------------------- #

@dataclass
class Highlight:
    """A windowed view of the document with the candidate span split out, so a
    pane can print `before`, then the span in reverse-video, then `after`. All
    three are plain text; the caller owns the ANSI escapes."""

    before: str
    span_text: str
    after: str
    text_start: int          # span start in the document text (None if unresolved)
    text_end: int
    located: bool            # False when the span did not resolve into this doc


def render_highlight(render_doc, source_span, *, window=280):
    """The left pane's content for one field: +/- `window` characters of the
    rendered text around the candidate span, with the span itself carved out
    for highlighting. When `source_span` is None or does not resolve into this
    document, `located` is False and the window is the head of the document --
    a "no span located" state, itself the clearest possible correct/reject cue.
    """
    text = render_doc.text
    ts = render_doc.text_span_for_source(source_span)
    if ts is None:
        head = text[:window]
        return Highlight(before=head, span_text="", after="",
                         text_start=None, text_end=None, located=False)

    t0, t1 = ts
    lo = max(0, t0 - window)
    hi = min(len(text), t1 + window)
    return Highlight(
        before=text[lo:t0],
        span_text=text[t0:t1],
        after=text[t1:hi],
        text_start=t0, text_end=t1, located=True,
    )


def _iter_find(haystack, needle):
    if not needle:
        return
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return
        yield i
        start = i + 1


@dataclass
class SpanMark:
    """A hand-marked span: its offsets in the rendered text and -- the thing
    that has to survive -- the exact ORIGINAL source offsets it resolves to."""

    text_span: tuple
    source_span: tuple
    snippet: str


def locate_span(render_doc, snippet, *, near_text_offset=None):
    """Turn a correction typed as a text snippet into exact source offsets.

    This is the CORRECT path's whole job: the validator marks the true span by
    naming its text, and this resolves it -- through the reduction `OffsetMap`
    the render document carries -- to `(source_start, source_end)` in the
    original bytes. When the snippet occurs more than once, the occurrence
    nearest `near_text_offset` (usually where the wrong span was) is chosen, so
    a repeated value like "70.00%" resolves to the one the validator meant.
    Returns None if the snippet is not found.
    """
    matches = list(_iter_find(render_doc.text, snippet))
    if not matches:
        stripped = snippet.strip()
        if stripped and stripped != snippet:
            matches = list(_iter_find(render_doc.text, stripped))
            snippet = stripped
    if not matches:
        return None
    if near_text_offset is not None:
        idx = min(matches, key=lambda i: abs(i - near_text_offset))
    else:
        idx = matches[0]
    t0, t1 = idx, idx + len(snippet)
    src = render_doc.offset_map.resolve(t0, t1)
    if src is None:
        return None
    return SpanMark(text_span=(t0, t1), source_span=src, snippet=snippet)


# --------------------------------------------------------------------------- #
# Section-scoped field context -- the extractor's span-through-4-stages bridge
# --------------------------------------------------------------------------- #

def build_field_context(render_doc, sections, field):
    """The reduced text a field is extracted from, plus an `OffsetMap` from
    THAT text straight back to the original source.

    A field's `field_spec.sections` list routes it (stage 3) into only the
    sections it belongs to -- so a `barrier_pct` is never read out of
    Hypothetical Examples. Those section slices are concatenated into one
    context and a map is built from the concatenated text back through
    `render_doc.offset_map` (which itself already composes stages 1-2, and the
    section split is stage 3), so a model span into the context resolves to the
    original bytes through all four stages -- the #101 contract #105 depends on.

    Returns `(context_text, context_map)`. With no matching section (or no
    section index), falls back to the whole document so extraction still runs.
    """
    if not sections:
        return render_doc.text, render_doc.offset_map

    wanted = sections_for_spec(sections, field.sections)
    ordered = sorted(
        (s for group in wanted.values() for s in group),
        key=lambda s: s.text_start,
    )
    if not ordered:
        return render_doc.text, render_doc.offset_map

    parts, spans, pos = [], [], 0
    for i, sec in enumerate(ordered):
        seg = render_doc.text[sec.text_start:sec.text_end]
        parts.append(seg)
        spans.append(TextSpan(pos, pos + len(seg), sec.text_start, sec.text_end, literal=True))
        pos += len(seg)
        if i < len(ordered) - 1:
            parts.append("\n\n")
            spans.append(TextSpan(pos, pos + 2, sec.text_end, sec.text_end, literal=False))
            pos += 2
    context_text = "".join(parts)
    context_map = OffsetMap(spans).compose(render_doc.offset_map)
    return context_text, context_map


# --------------------------------------------------------------------------- #
# Anchors (the #107 rule-seed signal)
# --------------------------------------------------------------------------- #

_ANCHOR_WINDOW = 90


def derive_anchor(render_doc, source_span, field):
    """The label a validated value sits under -- e.g. "Coupon Barrier" for a
    value that follows "Coupon Barrier:". This is the raw material #107 induces
    a regex from, and the thing rule-seed stability is measured on. Prefers a
    canonical spec anchor when one appears just before the span (stable across
    issuers by construction); otherwise the same-line label text immediately
    preceding the value. Returns None when nothing resolves."""
    if source_span is None:
        return None
    ts = render_doc.text_span_for_source(source_span)
    if ts is None:
        return None
    t0 = ts[0]
    pre = render_doc.text[max(0, t0 - _ANCHOR_WINDOW):t0]
    low = pre.lower()
    for a in field.anchors:
        key = a.lower().rstrip(": ").strip()
        if key and key in low:
            return a.rstrip(": ").strip()
    line_pre = pre.rsplit("\n", 1)[-1]
    if ":" in line_pre:
        label = line_pre.split(":")[0].strip()
        if label:
            return label
    label = line_pre.strip()
    return label or None


@dataclass
class RuleSeed:
    """A "promote this to a rule?" candidate surfaced once a field's anchor is
    stable across validated documents for one issuer. #107 is regex-first and
    its seeds come from here; surfacing this in the UI turns validation into
    rule authoring at no extra cost."""

    issuer: str
    field: str
    anchor: str
    support: int             # how many validated docs agree on this anchor
    total: int               # accept/correct verdicts seen for (issuer, field)
    sample_spans: list       # a few (accession, source_span) it was seen at


# --------------------------------------------------------------------------- #
# Exemplar rendering
# --------------------------------------------------------------------------- #

def render_exemplar(kind, *, field, value=None, anchor=None, span_text=None):
    """One exemplar line for the ladder's rung-3 prompt (#106). It must carry
    the value string verbatim so the local model can copy the boundary the
    human confirmed. A negative (reject) exemplar carries no value -- it teaches
    that absence is a valid answer and suppresses the hallucination that would
    otherwise fill the field."""
    if kind == "negative":
        return f"{field}: absent in a prior filing -- null is a valid answer, do not invent one"
    label = anchor or field
    snippet = f" [{span_text.strip()}]" if span_text and span_text.strip() else ""
    suffix = "  (corrected)" if kind == "corrected" else ""
    return f"{label}: {value}{snippet}{suffix}"


# --------------------------------------------------------------------------- #
# Store -- verdicts, exemplars, rule seeds, resumable session state
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    spec_id     TEXT,
    target_n    INTEGER,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS validated_docs (
    session_id   TEXT NOT NULL,
    accession    TEXT NOT NULL,
    document     TEXT NOT NULL,
    issuer       TEXT,
    validated_at TEXT,
    PRIMARY KEY (session_id, accession, document)
);

CREATE TABLE IF NOT EXISTS verdicts (
    session_id  TEXT NOT NULL,
    accession   TEXT NOT NULL,
    document    TEXT NOT NULL,
    field       TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    value_json  TEXT,
    span_start  INTEGER,
    span_end    INTEGER,
    anchor      TEXT,
    note        TEXT,
    created_at  TEXT,
    PRIMARY KEY (session_id, accession, document, field)
);

CREATE TABLE IF NOT EXISTS exemplars (
    issuer       TEXT NOT NULL,
    field        TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- positive | corrected | negative
    rendered     TEXT NOT NULL,
    value_json   TEXT,
    anchor       TEXT,
    span_text    TEXT,
    accession    TEXT,
    document     TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS rule_seeds (
    issuer     TEXT NOT NULL,
    field      TEXT NOT NULL,
    anchor     TEXT NOT NULL,
    status     TEXT NOT NULL,        -- pending | seeded | dismissed
    updated_at TEXT,
    PRIMARY KEY (issuer, field)
);

CREATE TABLE IF NOT EXISTS held_out_docs (
    session_id   TEXT NOT NULL,
    accession    TEXT NOT NULL,
    document     TEXT NOT NULL,
    issuer       TEXT,
    product_type TEXT,
    stratum      TEXT,
    selected_at  TEXT,
    PRIMARY KEY (session_id, accession, document)
);

CREATE INDEX IF NOT EXISTS ix_ex_issuer_field ON exemplars(issuer, field);
CREATE INDEX IF NOT EXISTS ix_vd_issuer_field ON verdicts(field);
CREATE INDEX IF NOT EXISTS ix_held_out_issuer ON held_out_docs(issuer);
"""


class ValidationStore:
    """Local, resumable persistence for the validation loop, and -- because
    every verdict writes an exemplar immediately -- the callable `exemplars=`
    provider the ladder reads on rung 3.

    Resumable by construction: every verdict and every validated-doc mark is
    committed the moment it is made, so a killed session resumes from the last
    field ruled on, never re-doing completed work and never losing a partial
    document. Local by construction: the path is checked against the same
    ownership boundary as the output store (#109) at open time.
    """

    def __init__(self, path=None, *, readonly=False):
        path = str(path) if path is not None else str(DEFAULT_VALIDATION_PATH)
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

    # -- sessions ----------------------------------------------------------

    def ensure_session(self, session_id, *, spec_id=None, target_n=None, now=None):
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, spec_id, target_n, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, spec_id, target_n, now),
        )
        self._conn.commit()

    def validated_accessions(self, session_id):
        rows = self._conn.execute(
            "SELECT DISTINCT accession FROM validated_docs WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {r["accession"] for r in rows}

    def validated_count(self, session_id):
        return self._conn.execute(
            "SELECT COUNT(*) FROM validated_docs WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]

    def mark_document_validated(self, session_id, accession, document, *, issuer=None, now=None):
        self._conn.execute(
            "INSERT OR REPLACE INTO validated_docs "
            "(session_id, accession, document, issuer, validated_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, accession, document, issuer, now),
        )
        self._conn.commit()

    def is_document_validated(self, session_id, accession, document):
        return self._conn.execute(
            "SELECT 1 FROM validated_docs WHERE session_id = ? AND accession = ? AND document = ?",
            (session_id, accession, document),
        ).fetchone() is not None

    # -- verdicts ----------------------------------------------------------

    def write_verdict(self, session_id, accession, document, fv, *, now=None):
        span_start, span_end = (fv.source_span if fv.source_span else (None, None))
        self._conn.execute(
            "INSERT OR REPLACE INTO verdicts "
            "(session_id, accession, document, field, verdict, value_json, "
            " span_start, span_end, anchor, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, accession, document, fv.field, fv.verdict,
             json.dumps(fv.value), span_start, span_end, fv.anchor, fv.note, now),
        )
        self._conn.commit()

    def verdicts_for(self, session_id, accession, document):
        rows = self._conn.execute(
            "SELECT field, verdict, value_json, span_start, span_end, anchor, note "
            "FROM verdicts WHERE session_id = ? AND accession = ? AND document = ? "
            "ORDER BY field",
            (session_id, accession, document),
        ).fetchall()
        out = []
        for r in rows:
            span = None
            if r["span_start"] is not None or r["span_end"] is not None:
                span = (r["span_start"], r["span_end"])
            out.append(FieldVerdict(
                field=r["field"], verdict=r["verdict"],
                value=json.loads(r["value_json"]) if r["value_json"] is not None else None,
                source_span=span, anchor=r["anchor"], note=r["note"]))
        return out

    def anchor_verdicts(self, issuer, field):
        """Every (accession, anchor, span) an accept/correct verdict recorded
        for one (issuer, field), across every session -- the raw input to
        rule-seed stability.

        Held-out documents (#108) are excluded by a LEFT JOIN against
        `held_out_docs`: a document reserved for the eval set must never
        seed a rule, so its anchors cannot count toward stability here --
        not "count but get filtered downstream," excluded at the query that
        computes support in the first place.
        """
        rows = self._conn.execute(
            "SELECT v.accession, v.anchor, v.span_start, v.span_end "
            "FROM verdicts v JOIN validated_docs d "
            "  ON d.session_id = v.session_id AND d.accession = v.accession "
            "     AND d.document = v.document "
            "LEFT JOIN held_out_docs h "
            "  ON h.session_id = v.session_id AND h.accession = v.accession "
            "     AND h.document = v.document "
            "WHERE v.field = ? AND d.issuer = ? AND v.verdict IN (?, ?) "
            "  AND v.anchor IS NOT NULL AND h.session_id IS NULL",
            (field, issuer, ACCEPT, CORRECT),
        ).fetchall()
        return [(r["accession"], r["anchor"],
                 (r["span_start"], r["span_end"])
                 if r["span_start"] is not None else None) for r in rows]

    # -- held-out eval set (#108) -------------------------------------------

    def mark_held_out(self, session_id, accession, document, *, issuer=None,
                      product_type=None, stratum=None, now=None):
        """Reserve one document for the #108 held-out eval set.

        Call this BEFORE the document's first verdict is recorded: #105
        writes an exemplar the INSTANT a verdict is recorded
        (`ValidationSession._write_exemplar`), so marking a document
        held-out after even one of its fields has been verdicted would
        already have leaked that field into the exemplar store. The
        document's verdicts still get recorded normally -- they ARE the
        eval harness's ground truth -- they just never write an exemplar or
        count toward rule-seed anchor stability (`anchor_verdicts` above).
        Idempotent: marking the same document twice just overwrites the
        stratum/timestamp.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO held_out_docs "
            "(session_id, accession, document, issuer, product_type, stratum, selected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, accession, document, issuer, product_type, stratum, now),
        )
        self._conn.commit()

    def is_held_out(self, session_id, accession, document):
        return self._conn.execute(
            "SELECT 1 FROM held_out_docs WHERE session_id = ? AND accession = ? AND document = ?",
            (session_id, accession, document),
        ).fetchone() is not None

    def held_out_documents(self, session_id=None):
        """Every reserved (session_id, accession, document, issuer,
        product_type, stratum) -- across all sessions unless one is given.
        This is the eval harness's document list, and the set a report can
        point at as proof of what was, and was never, excluded."""
        if session_id is None:
            rows = self._conn.execute(
                "SELECT session_id, accession, document, issuer, product_type, stratum "
                "FROM held_out_docs ORDER BY session_id, accession, document"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT session_id, accession, document, issuer, product_type, stratum "
                "FROM held_out_docs WHERE session_id = ? ORDER BY accession, document",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- exemplars (#106) --------------------------------------------------

    def write_exemplar(self, issuer, field, kind, rendered, *, value=None,
                       anchor=None, span_text=None, accession=None,
                       document=None, now=None):
        if not issuer:
            return  # exemplars are keyed by issuer; an unknown issuer can't index
        self._conn.execute(
            "INSERT INTO exemplars "
            "(issuer, field, kind, rendered, value_json, anchor, span_text, "
            " accession, document, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issuer, field, kind, rendered, json.dumps(value), anchor, span_text,
             accession, document, now),
        )
        self._conn.commit()

    def exemplars_for(self, issuer, field, *, limit=6, include_negative=True):
        """Rendered exemplar lines for one (issuer, field), most recent first.
        Corrected exemplars rank ahead of plain positives (they teach a
        boundary the model already got wrong). This is what makes document 11
        better than document 1: by the time it runs, the ladder's rung-3 prompt
        carries the human-confirmed boundaries from 1-10."""
        if not issuer:
            return []
        kinds = ("positive", "corrected", "negative") if include_negative \
            else ("positive", "corrected")
        placeholders = ", ".join("?" for _ in kinds)
        rows = self._conn.execute(
            f"SELECT rendered, kind FROM exemplars "
            f"WHERE issuer = ? AND field = ? AND kind IN ({placeholders}) "
            f"ORDER BY CASE kind WHEN 'corrected' THEN 0 WHEN 'positive' THEN 1 "
            f"         ELSE 2 END, rowid DESC",
            (issuer, field, *kinds),
        ).fetchall()
        seen, out = set(), []
        for r in rows:
            if r["rendered"] in seen:
                continue
            seen.add(r["rendered"])
            out.append(r["rendered"])
            if len(out) >= limit:
                break
        return out

    def __call__(self, issuer, field):
        """The ladder plugs the store straight in as `exemplars=store`; it is
        called as `store(issuer, field)` on rung 3 (see
        `extraction_ladder._lookup_exemplars`). Returns None when there is
        nothing yet, so the ladder omits the exemplar block entirely."""
        ex = self.exemplars_for(issuer, field)
        return ex or None

    # -- rule seeds (#107) -------------------------------------------------

    def seed_status(self, issuer, field):
        row = self._conn.execute(
            "SELECT status, anchor FROM rule_seeds WHERE issuer = ? AND field = ?",
            (issuer, field),
        ).fetchone()
        return (row["status"], row["anchor"]) if row else (None, None)

    def set_seed_status(self, issuer, field, anchor, status, *, now=None):
        self._conn.execute(
            "INSERT OR REPLACE INTO rule_seeds (issuer, field, anchor, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (issuer, field, anchor, status, now),
        )
        self._conn.commit()


# --------------------------------------------------------------------------- #
# Session -- the loop over documents and fields
# --------------------------------------------------------------------------- #

class ValidationSession:
    """Drives one validator through the first N documents, one document at a
    time, ALL fields shown together (field-at-a-time across documents means
    re-reading the same filing once per field). Records each verdict, writes
    the exemplar that compounds into later documents, and surfaces a rule seed
    once a field's anchor is stable.

    The session owns loop state and persistence; the extractor (`LadderExtractor`
    or a stand-in) owns turning a document into proposals; `validate_ui.py`
    owns the keyboard. That split keeps this class stdlib-only and testable.
    """

    def __init__(self, store, spec, *, session_id, target_n=20, crawl_state=None,
                 exemplar_limit=6, rule_seed_threshold=3, rule_seed_majority=0.6,
                 clock=None):
        self.store = store
        self.spec = spec
        self.session_id = session_id
        self.target_n = target_n
        self.crawl_state = crawl_state
        self.exemplar_limit = exemplar_limit
        self.rule_seed_threshold = rule_seed_threshold
        self.rule_seed_majority = rule_seed_majority
        self._clock = clock  # callable -> ISO string; None => timestamps are None
        self.store.ensure_session(session_id, spec_id=getattr(spec, "spec_id", None),
                                  target_n=target_n, now=self._now())

    def _now(self):
        return self._clock() if self._clock else None

    # -- progress ----------------------------------------------------------

    def progress(self):
        done = self.store.validated_count(self.session_id)
        return {"validated": done, "target": self.target_n,
                "remaining": max(0, self.target_n - done),
                "complete": done >= self.target_n}

    def next_unvalidated(self):
        """The next accession from #100's crawl not yet validated in this
        session, in filing-date order. Returns `(accession, meta)` or None when
        the frontier is drained or the target is met. `meta` is the crawl
        state's per-accession record (cik, form, file_date, issuer, document)."""
        if self.crawl_state is None:
            return None
        done = self.store.validated_accessions(self.session_id)
        accessions = self.crawl_state.get("accessions") \
            if isinstance(self.crawl_state, dict) else self.crawl_state.accessions
        pending = [(acc, meta) for acc, meta in accessions.items() if acc not in done]
        pending.sort(key=lambda kv: (kv[1].get("file_date") or "", kv[0]))
        return pending[0] if pending else None

    # -- verdicts ----------------------------------------------------------

    def record_verdict(self, accession, document, fv, *, issuer=None, render_doc=None):
        """Persist one verdict and, from it, the exemplar that will help later
        documents. `render_doc` (when given) resolves the anchor and the span
        snippet the exemplar carries; without it the verdict is still stored,
        just without an anchor-derived seed signal. Returns the FieldVerdict as
        stored (anchor filled in)."""
        field_def = self.spec.field(fv.field)

        if fv.anchor is None and render_doc is not None and field_def is not None \
                and fv.source_span is not None:
            fv.anchor = derive_anchor(render_doc, fv.source_span, field_def)

        self.store.write_verdict(self.session_id, accession, document, fv, now=self._now())
        self._write_exemplar(issuer, accession, document, fv, render_doc)
        return fv

    def _write_exemplar(self, issuer, accession, document, fv, render_doc):
        if not issuer:
            return
        if self.store.is_held_out(self.session_id, accession, document):
            return  # #108: a held-out document never teaches the exemplar store
        field = fv.field
        if fv.verdict == REJECT:
            rendered = render_exemplar("negative", field=field)
            self.store.write_exemplar(issuer, field, "negative", rendered,
                                      accession=accession, document=document,
                                      now=self._now())
            return

        kind = "corrected" if fv.verdict == CORRECT else "positive"
        span_text = render_doc.source_text(fv.source_span) if render_doc else None
        rendered = render_exemplar(kind, field=field, value=fv.value,
                                   anchor=fv.anchor, span_text=span_text)
        self.store.write_exemplar(issuer, field, kind, rendered, value=fv.value,
                                  anchor=fv.anchor, span_text=span_text,
                                  accession=accession, document=document,
                                  now=self._now())

    def complete_document(self, accession, document, *, issuer=None):
        """Mark a document done -- called after every spec field has a verdict.
        This is the unit `next_unvalidated`/`progress` count against N."""
        self.store.mark_document_validated(self.session_id, accession, document,
                                           issuer=issuer, now=self._now())

    # -- held-out eval set (#108) -------------------------------------------

    def mark_held_out(self, accession, document, *, issuer=None, product_type=None,
                      stratum=None):
        """Reserve `accession/document` for the #108 held-out eval set.

        Call this BEFORE `record_verdict` runs for the document -- see
        `ValidationStore.mark_held_out`. `eval_harness.reserve_held_out_set`
        is the usual caller: it selects a stratified slice from the
        candidate pool up front and marks every one of them through this
        method before the validation loop ever reaches them."""
        self.store.mark_held_out(self.session_id, accession, document, issuer=issuer,
                                 product_type=product_type, stratum=stratum, now=self._now())

    def is_held_out(self, accession, document):
        return self.store.is_held_out(self.session_id, accession, document)

    def all_fields_verdicted(self, accession, document):
        """The #105 acceptance for one document: EVERY spec field got a verdict.
        Returns (ok, missing_field_names)."""
        have = {v.field for v in self.store.verdicts_for(self.session_id, accession, document)}
        missing = [f for f in self.spec.field_names() if f not in have]
        return (not missing, missing)

    # -- rule seeds --------------------------------------------------------

    def rule_seed_for(self, issuer, field):
        """A `RuleSeed` if this (issuer, field)'s anchor has stabilized across
        validated documents and hasn't already been seeded/dismissed, else
        None. Stable = the most common anchor is shared by >= threshold docs AND
        is a majority of the accept/correct verdicts (a field whose anchor keeps
        changing is not ready to become one regex)."""
        status, _ = self.store.seed_status(issuer, field)
        if status in ("seeded", "dismissed"):
            return None

        rows = self.store.anchor_verdicts(issuer, field)
        if len(rows) < self.rule_seed_threshold:
            return None

        counts, samples = {}, {}
        for accession, anchor, span in rows:
            counts[anchor] = counts.get(anchor, 0) + 1
            samples.setdefault(anchor, []).append((accession, span))
        top_anchor, support = max(counts.items(), key=lambda kv: kv[1])
        total = len(rows)
        if support < self.rule_seed_threshold:
            return None
        if support / total < self.rule_seed_majority:
            return None
        return RuleSeed(issuer=issuer, field=field, anchor=top_anchor,
                        support=support, total=total,
                        sample_spans=samples[top_anchor][:3])

    def pending_rule_seeds(self, issuer):
        """Every field of the spec whose anchor has just stabilized for this
        issuer -- what the UI offers to promote once per field."""
        seeds = []
        for field in self.spec.field_names():
            seed = self.rule_seed_for(issuer, field)
            if seed is not None:
                seeds.append(seed)
        return seeds

    def mark_seed(self, issuer, field, anchor, *, promoted):
        """Record the validator's answer to a rule-seed prompt so it appears
        ONCE: `promoted=True` -> seeded (handed to #107), else dismissed."""
        self.store.set_seed_status(issuer, field, anchor,
                                   "seeded" if promoted else "dismissed",
                                   now=self._now())


# --------------------------------------------------------------------------- #
# Extractor -- document -> proposals, with spans composed back to source
# --------------------------------------------------------------------------- #

class LadderExtractor:
    """Turns one document into `FieldProposal`s using the #104 extraction
    ladder, resolving each answer's span through the reduction map so the
    proposal carries an ORIGINAL-source span the UI can highlight and a
    correction can build on.

    The ladder returns a span in the coordinates of the text it was handed;
    `build_field_context` gives that text a map straight back to source, so
    `context_map.resolve(...)` lifts the model's offset pair to the original
    bytes -- through all four #101 stages.
    """

    def __init__(self, spec, ladder, *, sections=None):
        self.spec = spec
        self.ladder = ladder
        self._sections = sections

    def propose(self, render_doc, *, issuer=None, ex107=None, accession=None,
                document=None, fields=None):
        sections = self._sections
        if sections is None:
            sections = split_sections(render_doc)
        field_names = fields or self.spec.field_names()

        proposals = []
        for name in field_names:
            field_def = self.spec.field(name)
            ctx_text, ctx_map = build_field_context(render_doc, sections, field_def)
            result = self.ladder.extract(
                name, text=ctx_text, issuer=issuer, ex107=ex107,
                accession=accession, document=document)

            source_span = None
            if result.span is not None:
                if result.rung in ("rule", "xbrl"):
                    # rung 1/2 spans are already in source coordinates (or None).
                    source_span = tuple(result.span)
                else:
                    source_span = ctx_map.resolve(result.span[0], result.span[1])

            proposals.append(FieldProposal(
                field=name, value=result.value, source_span=source_span,
                provenance=result.provenance, confidence=result.confidence,
                rung=result.rung, unit=result.unit,
                flags=[f.as_dict() for f in result.flags]))
        return proposals


# --------------------------------------------------------------------------- #
# Self-check -- no network, no model, in-memory store
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    try:
        from normalize import normalize_html
        from reduce import build_boilerplate_model, strip_boilerplate
        from field_spec import load_specs
    except ImportError:
        from .normalize import normalize_html
        from .reduce import build_boilerplate_model, strip_boilerplate
        from .field_spec import load_specs

    spec = load_specs()["structured_note"]

    html = """
    <html><body>
    <p>Issuer: JPMorgan Chase Financial Company LLC. CUSIP: 48133YHT4.</p>
    <p>These securities are not deposits and are not FDIC insured.</p>
    <h2>Contingent Coupon</h2>
    <p>Contingent Coupon Rate: 9.15% per annum.</p>
    <h2>Downside</h2>
    <p>Barrier: 70.00% of the Initial Value.</p>
    <h2>Estimated Value of the Notes</h2>
    <p>Our estimated value of the notes is $972.30 per $1,000.</p>
    </body></html>
    """
    nd = normalize_html(html)
    # run stage 2 as well, so the render doc's map composes stages 1+2. The
    # second corpus filing shares only the FDIC boilerplate (every term line
    # differs), so the term lines survive the strip and the FDIC line does not.
    other = (html.replace("9.15", "8.10").replace("70.00", "65.00")
             .replace("972.30", "981.00").replace("48133YHT4", "12345ABC6"))
    model = build_boilerplate_model("JPM", [nd, normalize_html(other)])
    reduced = strip_boilerplate(nd, model)
    render_doc = RenderDocument.from_reduced(reduced, nd.source)

    # a corrected span marked on the REDUCED text still slices the ORIGINAL bytes.
    mark = locate_span(render_doc, "70.00%")
    assert mark is not None
    assert "70.00%" in nd.source[mark.source_span[0]:mark.source_span[1]], \
        "corrected span must resolve through reduction back to the source"
    print(f"span 'Barrier: 70.00%' resolves to source: "
          f"{nd.source[mark.source_span[0]:mark.source_span[1]]!r}")

    # highlight renders the window with the span carved out.
    hl = render_highlight(render_doc, mark.source_span, window=40)
    assert hl.located and hl.span_text == "70.00%"
    print(f"highlight: ...{hl.before[-20:]!r} [[{hl.span_text}]] {hl.after[:20]!r}...")

    # verdict loop against an in-memory store; exemplars compound; seed stabilizes.
    store = ValidationStore(":memory:")
    session = ValidationSession(store, spec, session_id="selfcheck", target_n=3,
                               rule_seed_threshold=2)
    issuer = "JPMorgan Chase Financial Company LLC"

    for i, acc in enumerate(("000-1", "000-2")):
        fv = FieldVerdict.correct("barrier_pct", 70.0, source_span=mark.source_span)
        session.record_verdict(acc, "424b2.htm", fv, issuer=issuer, render_doc=render_doc)
        session.complete_document(acc, "424b2.htm", issuer=issuer)

    ex = store.exemplars_for(issuer, "barrier_pct")
    assert any("70.0" in e for e in ex), ex
    print(f"exemplars for (JPM, barrier_pct): {ex}")

    seed = session.rule_seed_for(issuer, "barrier_pct")
    assert seed is not None and seed.support == 2, seed
    print(f"rule seed ready: anchor={seed.anchor!r} support={seed.support}/{seed.total}")
    session.mark_seed(issuer, "barrier_pct", seed.anchor, promoted=True)
    assert session.rule_seed_for(issuer, "barrier_pct") is None  # appears once

    assert store.validated_count("selfcheck") == 2
    print(f"progress: {session.progress()}")
    print("\nvalidation self-check: PASS")
