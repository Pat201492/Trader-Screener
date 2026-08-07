"""
Extraction eval harness (issue #108, part of #95) -- replace every quality
estimate with a measurement, per field.

Two things live here:

  1. A HELD-OUT SET selector (`select_held_out` / `reserve_held_out_set`)
     that reserves a stratified slice of #105-validated documents up front,
     before their verdicts are recorded -- #105 writes an exemplar the
     INSTANT a verdict lands (`validation.ValidationSession._write_exemplar`),
     so a document can only be honestly held out if it is marked BEFORE its
     first field is verdicted. `validation.py` owns the actual exclusion
     (`ValidationStore.mark_held_out` / `is_held_out`, and the
     `anchor_verdicts` query that #107's rule-seed stability reads); this
     module owns the SELECTION policy -- stratified by (issuer, product_type)
     so the read is not dominated by whichever issuer got validated most.

  2. A PER-FIELD SCORER (`run_eval`) that re-runs the current extractor
     (any object shaped like `validation.LadderExtractor`) against the
     held-out set's gold verdicts and reports, per field: precision, recall,
     span accuracy (right value from the wrong span is still a defect,
     because #107 induces regex anchors from spans), escalation rate, and
     absence-handling (false positives on fields genuinely not present).
     `evaluate_regression` compares two reports and blocks on any field that
     drops past a threshold -- the gate #102/#103/#106/#107 changes must
     pass before they ship.

`EvalReport` is stamped with spec/exemplar-set/rule-set/model versions so
two runs are honestly comparable; `EvalReportStore` persists them local-only
(the same ownership boundary as `output_store.py` / `validation.py`).

stdlib only. Run the self-check:  python tools/edgar_scrubber/eval_harness.py
"""

import json
import os
import random
import sqlite3
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

try:  # package import: tools.edgar_scrubber.eval_harness
    from .output_store import LOCAL_STORE_ROOT, _assert_local_destination
    from .validation import ACCEPT, CORRECT, REJECT
except ImportError:  # standalone: python tools/edgar_scrubber/eval_harness.py
    from output_store import LOCAL_STORE_ROOT, _assert_local_destination
    from validation import ACCEPT, CORRECT, REJECT

DEFAULT_EVAL_STORE_PATH = LOCAL_STORE_ROOT / "eval_reports.sqlite"


# --------------------------------------------------------------------------- #
# Held-out selection -- stratified, deterministic, and BEFORE validation
# --------------------------------------------------------------------------- #

def _stratum_key(candidate):
    return (candidate.get("issuer") or "unknown-issuer",
            candidate.get("product_type") or "unknown-product-type")


def select_held_out(candidates, *, target_n=None, fraction=0.15, min_per_stratum=1, seed=0):
    """Stratified slice of `candidates` (dicts with at least `accession`,
    `document`, `issuer`, optionally `product_type`) reserved for the eval
    set.

    Grouping is by `(issuer, product_type)` so the held-out read is not
    dominated by whichever issuer or product type has the most candidates --
    the whole reason a per-field score needs a held-out set at all (an
    aggregate score dominated by one issuer is exactly the "decorative
    number" #108 exists to replace). A candidate missing `product_type`
    (the common case before a document is fetched -- population detection
    needs the body text) falls into one `unknown-product-type` bucket per
    issuer, which still stratifies by issuer alone rather than refusing.

    Deterministic given `seed`: the same candidate pool always yields the
    same held-out slice, which the regression gate depends on for
    comparability across runs. Must be called on the FULL candidate pool
    BEFORE any of these documents are validated -- see the module docstring.

    Returns the selected candidates (each a shallow copy carrying its
    `stratum` key), in selection order.
    """
    candidates = list(candidates)
    if not candidates:
        return []

    strata = {}
    for c in candidates:
        strata.setdefault(_stratum_key(c), []).append(c)

    rng = random.Random(seed)
    for key, group in strata.items():
        group.sort(key=lambda c: (c.get("accession") or "", c.get("document") or ""))
        rng.shuffle(group)

    n = target_n if target_n is not None else max(1, round(len(candidates) * fraction))
    n = min(n, len(candidates))

    keys = sorted(strata.keys())
    selected = []

    # First pass: guarantee min_per_stratum per stratum where available, so a
    # small-N issuer is not crowded out entirely by a large-N one.
    for key in keys:
        for _ in range(min(min_per_stratum, len(strata[key]))):
            if len(selected) >= n:
                break
            selected.append(strata[key].pop(0))
        if len(selected) >= n:
            break

    # Round-robin the remainder across strata.
    idx = 0
    while len(selected) < n and any(strata.values()):
        key = keys[idx % len(keys)]
        if strata[key]:
            selected.append(strata[key].pop(0))
        idx += 1

    out = []
    for c in selected:
        picked = dict(c)
        picked["stratum"] = "::".join(_stratum_key(c))
        out.append(picked)
    return out


def reserve_held_out_set(session, candidates, *, target_n=None, fraction=0.15,
                         min_per_stratum=1, seed=0):
    """Select a stratified held-out slice and mark it in `session` in one
    step, via `ValidationSession.mark_held_out` -- so the FIRST verdict
    recorded against any of these documents already skips the exemplar
    write. This is the usual entry point; `select_held_out` is exposed
    separately for callers that want to select without a live session (e.g.
    to preview the slice, or to persist it before any store exists).

    Returns the selected candidates (each carrying its `stratum`).
    """
    selected = select_held_out(candidates, target_n=target_n, fraction=fraction,
                               min_per_stratum=min_per_stratum, seed=seed)
    for c in selected:
        session.mark_held_out(c["accession"], c["document"], issuer=c.get("issuer"),
                              product_type=c.get("product_type"), stratum=c.get("stratum"))
    return selected


# --------------------------------------------------------------------------- #
# Value / span comparison
# --------------------------------------------------------------------------- #

def _values_match(field_def, gold_value, pred_value, *, num_tol=1e-6):
    """Type-aware equality using the field's declared type (field_spec.py) --
    a percent/number compares within tolerance, a string compares
    case/whitespace-insensitively (extraction differences here are almost
    always formatting, not a different answer), everything else exact."""
    t = field_def.type if field_def is not None else None
    if t in ("number", "percent"):
        try:
            return abs(float(gold_value) - float(pred_value)) <= num_tol
        except (TypeError, ValueError):
            return gold_value == pred_value
    if t == "array":
        if isinstance(gold_value, list) and isinstance(pred_value, list):
            return sorted(str(v) for v in gold_value) == sorted(str(v) for v in pred_value)
        return gold_value == pred_value
    if isinstance(gold_value, str) and isinstance(pred_value, str):
        return gold_value.strip().lower() == pred_value.strip().lower()
    return gold_value == pred_value


def _spans_overlap(gold_span, pred_span):
    if gold_span is None or pred_span is None:
        return False
    g0, g1 = gold_span
    p0, p1 = pred_span
    return g0 < p1 and p0 < g1


# --------------------------------------------------------------------------- #
# Per-field metrics
# --------------------------------------------------------------------------- #

@dataclass
class FieldMetrics:
    """One field's confusion counts over the held-out set. Properties derive
    the reported rates; `as_dict()` carries both the rate and its support
    count (the denominator), because a rate computed from 2 examples and one
    from 200 should never be compared as if they were equally trustworthy --
    `evaluate_regression`'s `min_support` guard reads these counts."""

    field: str
    tp: int = 0             # value produced, gold present, value correct
    fp: int = 0             # value produced, either gold absent (hallucination)
                            # or gold present but value wrong
    fn: int = 0             # gold present, value missing or wrong
    tn: int = 0             # gold absent, correctly produced nothing
    span_correct: int = 0   # of the TPs with a checkable gold span, span overlapped
    span_checked: int = 0   # TPs where both gold and predicted spans exist
    escalations: int = 0    # predictions that reached the Claude rung
    total: int = 0          # every gold instance scored (present or absent)
    absent_total: int = 0   # gold instances where the field is genuinely absent
    absent_fp: int = 0      # of those, how many got a hallucinated value

    @property
    def precision(self):
        d = self.tp + self.fp
        return self.tp / d if d else None

    @property
    def recall(self):
        d = self.tp + self.fn
        return self.tp / d if d else None

    @property
    def span_accuracy(self):
        return self.span_correct / self.span_checked if self.span_checked else None

    @property
    def escalation_rate(self):
        return self.escalations / self.total if self.total else None

    @property
    def absence_fp_rate(self):
        return self.absent_fp / self.absent_total if self.absent_total else None

    def as_dict(self):
        return {
            "field": self.field,
            "precision": self.precision, "precision_n": self.tp + self.fp,
            "recall": self.recall, "recall_n": self.tp + self.fn,
            "span_accuracy": self.span_accuracy, "span_accuracy_n": self.span_checked,
            "escalation_rate": self.escalation_rate, "escalation_rate_n": self.total,
            "absence_fp_rate": self.absence_fp_rate, "absence_fp_rate_n": self.absent_total,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
        }


def _score_field(field_def, m, gold, pred):
    """Update `m` (a FieldMetrics) from one gold FieldVerdict and one
    predicted FieldProposal (or None if the extractor produced nothing for
    this field)."""
    m.total += 1
    gold_present = gold.verdict != REJECT
    pred_value = pred.value if pred is not None else None
    pred_present = pred_value is not None

    if pred is not None and pred.rung == "claude":
        m.escalations += 1

    if not gold_present:
        m.absent_total += 1
        if pred_present:
            m.fp += 1
            m.absent_fp += 1
        else:
            m.tn += 1
        return

    if not pred_present:
        m.fn += 1
        return

    if _values_match(field_def, gold.value, pred_value):
        m.tp += 1
        if gold.source_span is not None and pred.source_span is not None:
            m.span_checked += 1
            if _spans_overlap(gold.source_span, pred.source_span):
                m.span_correct += 1
    else:
        # Wrong value: a bad production (hurts precision) AND a miss of the
        # true value (hurts recall) -- the standard IE convention, and the
        # one the issue's own field definitions describe.
        m.fp += 1
        m.fn += 1


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

@dataclass
class HeldOutCase:
    """One held-out document ready to be scored: the gold verdicts live in
    `store` under `(session_id, accession, document)`; `render_doc` and
    `sections` are what `extractor.propose()` needs to produce fresh
    predictions (the same shape `validation.LadderExtractor.propose` takes)."""

    session_id: str
    accession: str
    document: str
    issuer: str = None
    render_doc: object = None
    sections: dict = None
    ex107: dict = None
    product_type: str = None


@dataclass
class EvalReport:
    """A scored run, stamped so two reports are honestly comparable. Every
    version field the issue asks for (spec / exemplar-set / rule-set /
    model) travels with the numbers -- a report with no idea what produced
    it cannot gate anything."""

    report_id: str
    spec_id: str
    spec_version: str
    generated_at: str
    exemplar_set_version: str = None
    rule_set_version: str = None
    local_model: str = None
    claude_model: str = None
    held_out_sessions: tuple = ()
    n_documents: int = 0
    fields: dict = _dc_field(default_factory=dict)   # field name -> FieldMetrics.as_dict()

    def as_dict(self):
        return {
            "report_id": self.report_id, "spec_id": self.spec_id,
            "spec_version": self.spec_version, "generated_at": self.generated_at,
            "exemplar_set_version": self.exemplar_set_version,
            "rule_set_version": self.rule_set_version,
            "local_model": self.local_model, "claude_model": self.claude_model,
            "held_out_sessions": list(self.held_out_sessions),
            "n_documents": self.n_documents, "fields": self.fields,
        }

    def to_json(self, indent=2):
        return json.dumps(self.as_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d):
        return cls(
            report_id=d["report_id"], spec_id=d["spec_id"], spec_version=d["spec_version"],
            generated_at=d["generated_at"], exemplar_set_version=d.get("exemplar_set_version"),
            rule_set_version=d.get("rule_set_version"), local_model=d.get("local_model"),
            claude_model=d.get("claude_model"),
            held_out_sessions=tuple(d.get("held_out_sessions") or ()),
            n_documents=d.get("n_documents", 0), fields=dict(d.get("fields") or {}),
        )


def run_eval(spec, extractor, cases, store, *, report_id, generated_at,
             exemplar_set_version=None, rule_set_version=None,
             local_model=None, claude_model=None, fields=None):
    """Score `extractor` against the held-out set's gold verdicts.

    `extractor` is anything shaped like `validation.LadderExtractor`:
    `.propose(render_doc, issuer=, ex107=, accession=, document=, fields=)
    -> [FieldProposal]`. Every `case` MUST already be marked held-out in
    `store` -- checked, not assumed, so a report can never be quietly built
    from a document that may also have trained the exemplars/rules it is
    grading (the entire reason #108 exists). Raises `ValueError` on the
    first case that is not.
    """
    field_names = list(fields) if fields else list(spec.field_names())
    metrics = {name: FieldMetrics(field=name) for name in field_names}
    n_docs = 0

    for case in cases:
        if not store.is_held_out(case.session_id, case.accession, case.document):
            raise ValueError(
                f"{case.accession}/{case.document} is not marked held-out in "
                f"session {case.session_id!r} -- refusing to score a document "
                f"that may have trained the exemplars or rules it would be grading"
            )
        gold_by_field = {v.field: v for v in
                         store.verdicts_for(case.session_id, case.accession, case.document)}
        if not gold_by_field:
            continue
        n_docs += 1

        proposals = extractor.propose(case.render_doc, issuer=case.issuer, ex107=case.ex107,
                                      accession=case.accession, document=case.document,
                                      fields=field_names)
        by_field = {p.field: p for p in proposals}

        for name in field_names:
            gold = gold_by_field.get(name)
            if gold is None:
                continue
            _score_field(spec.field(name), metrics[name], gold, by_field.get(name))

    return EvalReport(
        report_id=report_id, spec_id=spec.spec_id, spec_version=getattr(spec, "version", None),
        generated_at=generated_at, exemplar_set_version=exemplar_set_version,
        rule_set_version=rule_set_version, local_model=local_model, claude_model=claude_model,
        held_out_sessions=tuple(sorted({c.session_id for c in cases})),
        n_documents=n_docs, fields={name: m.as_dict() for name, m in metrics.items()},
    )


# --------------------------------------------------------------------------- #
# Regression gate
# --------------------------------------------------------------------------- #

# Max tolerated absolute change per metric before a field blocks the gate.
# precision/recall/span_accuracy: max allowed DROP. absence_fp_rate/
# escalation_rate: max allowed INCREASE. escalation_rate's threshold is
# looser -- an escalation-rate rise alone is a cost signal, not necessarily a
# quality regression, but a large jump still deserves a look before shipping.
DEFAULT_THRESHOLDS = {
    "precision": 0.05,
    "recall": 0.05,
    "span_accuracy": 0.05,
    "absence_fp_rate": 0.05,
    "escalation_rate": 0.15,
}

_DROP_METRICS = ("precision", "recall", "span_accuracy")
_RISE_METRICS = ("absence_fp_rate", "escalation_rate")


@dataclass(frozen=True)
class FieldRegression:
    field: str
    metric: str
    baseline: float
    candidate: float
    delta: float
    threshold: float

    def as_dict(self):
        return {"field": self.field, "metric": self.metric, "baseline": self.baseline,
                "candidate": self.candidate, "delta": self.delta, "threshold": self.threshold}


@dataclass(frozen=True)
class GateReport:
    blocked: bool
    regressions: tuple
    missing_fields: tuple    # in baseline, absent from candidate -- a hard block
    checked_fields: tuple
    baseline_report_id: str
    candidate_report_id: str

    def as_dict(self):
        return {
            "blocked": self.blocked,
            "regressions": [r.as_dict() for r in self.regressions],
            "missing_fields": list(self.missing_fields),
            "checked_fields": list(self.checked_fields),
            "baseline_report_id": self.baseline_report_id,
            "candidate_report_id": self.candidate_report_id,
        }

    def explain(self):
        if not self.blocked:
            return f"PASS -- {len(self.checked_fields)} field(s) checked, no regression past threshold."
        lines = [f"BLOCKED -- {self.candidate_report_id} vs {self.baseline_report_id}"]
        for f in self.missing_fields:
            lines.append(f"  {f}: present in baseline, MISSING from candidate")
        for r in self.regressions:
            lines.append(f"  {r.field}.{r.metric}: {r.baseline:.4f} -> {r.candidate:.4f} "
                        f"(delta {r.delta:+.4f}, threshold {r.threshold:.4f})")
        return "\n".join(lines)


def evaluate_regression(baseline, candidate, *, thresholds=None, min_support=3):
    """Compare `candidate` against `baseline` (both `EvalReport`s) per field.
    Blocks if any field present in the baseline is missing from the
    candidate (the field was not evaluated at all -- the worst possible
    regression, and not something a threshold can catch), or if any metric
    moves past its threshold in the bad direction.

    `min_support` skips a metric comparison when either report's denominator
    for that metric (the `_n` count in `FieldMetrics.as_dict()`) is below
    it -- a rate computed from one or two held-out examples swings too much
    on noise alone to gate a ship decision.
    """
    merged = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    regressions, missing, checked = [], [], []

    for name, base_m in baseline.fields.items():
        cand_m = candidate.fields.get(name)
        if cand_m is None:
            missing.append(name)
            continue
        checked.append(name)

        for metric in _DROP_METRICS:
            b, c = base_m.get(metric), cand_m.get(metric)
            if b is None or c is None:
                continue
            n = min(base_m.get(f"{metric}_n", 0), cand_m.get(f"{metric}_n", 0))
            if n < min_support:
                continue
            delta = c - b
            if delta < -merged[metric]:
                regressions.append(FieldRegression(name, metric, b, c, delta, merged[metric]))

        for metric in _RISE_METRICS:
            b, c = base_m.get(metric), cand_m.get(metric)
            if b is None or c is None:
                continue
            n = min(base_m.get(f"{metric}_n", 0), cand_m.get(f"{metric}_n", 0))
            if n < min_support:
                continue
            delta = c - b
            if delta > merged[metric]:
                regressions.append(FieldRegression(name, metric, b, c, delta, merged[metric]))

    blocked = bool(regressions) or bool(missing)
    return GateReport(blocked=blocked, regressions=tuple(regressions), missing_fields=tuple(missing),
                      checked_fields=tuple(checked), baseline_report_id=baseline.report_id,
                      candidate_report_id=candidate.report_id)


# --------------------------------------------------------------------------- #
# Report store -- local-only, comparable across runs
# --------------------------------------------------------------------------- #

_EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_reports (
    report_id             TEXT PRIMARY KEY,
    spec_id               TEXT NOT NULL,
    spec_version          TEXT NOT NULL,
    exemplar_set_version  TEXT,
    rule_set_version      TEXT,
    local_model           TEXT,
    generated_at          TEXT NOT NULL,
    accepted_baseline     INTEGER NOT NULL DEFAULT 0,
    report_json           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_eval_spec ON eval_reports(spec_id, generated_at);
CREATE INDEX IF NOT EXISTS ix_eval_baseline ON eval_reports(spec_id, accepted_baseline);
"""


class EvalReportStore:
    """Local-only persistence for `EvalReport`s, same ownership boundary as
    `output_store.OutputStore` / `validation.ValidationStore` (checked at
    open time via `_assert_local_destination`). Reports are what makes the
    regression gate possible run-over-run: `latest_baseline()` is what a new
    run's candidate gets compared against."""

    def __init__(self, path=None, *, readonly=False):
        path = str(path) if path is not None else str(DEFAULT_EVAL_STORE_PATH)
        _assert_local_destination(path)
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        if not readonly:
            self._conn.executescript(_EVAL_SCHEMA)
            self._conn.commit()
        self.readonly = readonly

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def save(self, report, *, accepted_baseline=False):
        # ON CONFLICT rather than INSERT OR REPLACE. REPLACE deletes the existing
        # row and inserts a fresh one, so re-saving a report with the default
        # accepted_baseline=False silently cleared a flag that accept_as_baseline()
        # had set. The regression gate would then find no baseline to compare
        # against and pass everything -- a silent loss of the only thing standing
        # between a quality regression and a merge.
        #
        # MAX() keeps an accepted baseline accepted across re-saves; demoting one
        # stays an explicit act rather than a side effect of writing the report again.
        self._conn.execute(
            "INSERT INTO eval_reports "
            "(report_id, spec_id, spec_version, exemplar_set_version, rule_set_version, "
            " local_model, generated_at, accepted_baseline, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(report_id) DO UPDATE SET "
            "  spec_id=excluded.spec_id, spec_version=excluded.spec_version, "
            "  exemplar_set_version=excluded.exemplar_set_version, "
            "  rule_set_version=excluded.rule_set_version, "
            "  local_model=excluded.local_model, generated_at=excluded.generated_at, "
            "  accepted_baseline=MAX(eval_reports.accepted_baseline, excluded.accepted_baseline), "
            "  report_json=excluded.report_json",
            (report.report_id, report.spec_id, report.spec_version, report.exemplar_set_version,
             report.rule_set_version, report.local_model, report.generated_at,
             1 if accepted_baseline else 0, report.to_json()),
        )
        self._conn.commit()

    def accept_as_baseline(self, report_id):
        self._conn.execute(
            "UPDATE eval_reports SET accepted_baseline = 1 WHERE report_id = ?", (report_id,)
        )
        self._conn.commit()

    def get(self, report_id):
        row = self._conn.execute(
            "SELECT report_json FROM eval_reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        return EvalReport.from_dict(json.loads(row["report_json"])) if row else None

    def latest_baseline(self, spec_id):
        row = self._conn.execute(
            "SELECT report_json FROM eval_reports WHERE spec_id = ? AND accepted_baseline = 1 "
            "ORDER BY generated_at DESC LIMIT 1",
            (spec_id,),
        ).fetchone()
        return EvalReport.from_dict(json.loads(row["report_json"])) if row else None

    def history(self, spec_id):
        rows = self._conn.execute(
            "SELECT report_json FROM eval_reports WHERE spec_id = ? ORDER BY generated_at DESC",
            (spec_id,),
        ).fetchall()
        return [EvalReport.from_dict(json.loads(r["report_json"])) for r in rows]


# --------------------------------------------------------------------------- #
# Self-check -- no network, no model, in-memory stores
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    try:
        from field_spec import load_specs
        from validation import (FieldVerdict, LadderExtractor, RenderDocument, ValidationSession,
                                 ValidationStore, locate_span)
        from extraction_ladder import ExtractionLadder
        from normalize import normalize_html
    except ImportError:
        from .field_spec import load_specs
        from .validation import (FieldVerdict, LadderExtractor, RenderDocument, ValidationSession,
                                  ValidationStore, locate_span)
        from .extraction_ladder import ExtractionLadder
        from .normalize import normalize_html

    spec = load_specs()["structured_note"]

    # -- held-out selection is stratified and deterministic -----------------
    candidates = (
        [{"accession": f"jpm-{i}", "document": "x", "issuer": "JPM", "product_type": "autocall"}
         for i in range(16)]
        + [{"accession": f"gs-{i}", "document": "x", "issuer": "GS", "product_type": "buffer"}
           for i in range(4)]
    )
    selected = select_held_out(candidates, fraction=0.25, seed=1)
    print(f"held-out selection: {len(selected)}/{len(candidates)} candidates "
          f"({sum(1 for c in selected if c['issuer'] == 'JPM')} JPM, "
          f"{sum(1 for c in selected if c['issuer'] == 'GS')} GS)")
    assert any(c["issuer"] == "GS" for c in selected), \
        "the minority issuer must not be crowded out of the held-out slice"
    assert select_held_out(candidates, fraction=0.25, seed=1) == selected, \
        "selection must be deterministic for a fixed seed"

    # One real document, reused below, so a gold span is a real span rather
    # than an arbitrary tuple -- span-accuracy scoring needs that to mean
    # anything.
    nd = normalize_html(
        "<html><body><h2>Downside</h2><p>Barrier: 70.00% of Initial Value.</p></body></html>")
    render_doc = RenderDocument.from_normalized(nd)
    gold_span = locate_span(render_doc, "70.00%").source_span

    # -- a held-out document never teaches the exemplar store or a rule seed
    store = ValidationStore(":memory:")
    session = ValidationSession(store, spec, session_id="s1", target_n=5,
                                rule_seed_threshold=2)
    session.mark_held_out("held-1", "x", issuer="JPM")
    fv = FieldVerdict.correct("barrier_pct", 70.0, source_span=gold_span)
    session.record_verdict("held-1", "x", fv, issuer="JPM", render_doc=render_doc)
    session.complete_document("held-1", "x", issuer="JPM")
    assert store(  # ValidationStore.__call__ is the exemplars= callable
        "JPM", "barrier_pct") is None, "held-out verdict must not write an exemplar"
    assert session.rule_seed_for("JPM", "barrier_pct") is None or \
        session.rule_seed_for("JPM", "barrier_pct").support == 0, \
        "held-out verdict must not count toward rule-seed anchor stability"
    print("held-out document excluded from exemplars and rule-seed anchors: PASS")

    # a NON-held-out doc for the same issuer still teaches normally --
    # proves the guard is per-document, not a blanket kill switch.
    session2 = ValidationSession(store, spec, session_id="s2", target_n=5)
    fv2 = FieldVerdict.correct("barrier_pct", 65.0, source_span=(0, 5))
    session2.record_verdict("live-1", "x", fv2, issuer="JPM")
    assert store("JPM", "barrier_pct") is not None, \
        "a normally-validated document must still write an exemplar"

    # -- run_eval refuses a non-held-out case -------------------------------
    class _StubClient:
        """Same trick as test_validation.py's SpanFindingClient: locate the
        target inside the SOURCE TEXT the ladder actually sent, so the span
        the extractor composes back to source is a real answer, not a
        constant that happens to work for one document layout."""

        def chat_completion(self, messages, model=None, response_format=None, **kw):
            props = response_format["json_schema"]["schema"]["properties"]
            key = next(iter(props))
            content = messages[-1]["content"]
            src = content.split("SOURCE TEXT:\n", 1)[-1]
            i = src.find("70.00%")
            s = [i, i + 6] if i >= 0 else None
            body = json.dumps({key: {"v": 70.0, "s": s, "c": 0.9}})
            return {"choices": [{"message": {"content": body}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

    ladder = ExtractionLadder(spec, local_client=_StubClient(), local_model="qwen2.5:7b")
    extractor = LadderExtractor(spec, ladder)

    bad_case = HeldOutCase(session_id="s2", accession="live-1", document="x",
                           issuer="JPM", render_doc=render_doc)
    raised = False
    try:
        run_eval(spec, extractor, [bad_case], store, report_id="r0", generated_at="2026-01-01",
                fields=["barrier_pct"])
    except ValueError:
        raised = True
    assert raised, "run_eval must refuse a case that is not marked held-out"
    print("run_eval refuses a non-held-out case: PASS")

    # -- run_eval scores a correct held-out prediction -----------------------
    good_case = HeldOutCase(session_id="s1", accession="held-1", document="x",
                            issuer="JPM", render_doc=render_doc)
    report = run_eval(spec, extractor, [good_case], store, report_id="r1",
                      generated_at="2026-01-02", local_model="qwen2.5:7b",
                      fields=["barrier_pct"])
    bm = report.fields["barrier_pct"]
    assert bm["precision"] == 1.0 and bm["recall"] == 1.0, bm
    assert bm["span_accuracy"] == 1.0, bm
    print(f"run_eval on a correct held-out prediction: precision={bm['precision']} "
          f"recall={bm['recall']} span_accuracy={bm['span_accuracy']}")

    # -- regression gate blocks a field that drops past threshold ------------
    baseline = EvalReport(report_id="base", spec_id="s", spec_version="1.0.0",
                          generated_at="2026-01-01",
                          fields={
                              "barrier_pct": {"precision": 0.95, "precision_n": 20,
                                             "recall": 0.90, "recall_n": 20,
                                             "span_accuracy": 0.90, "span_accuracy_n": 18,
                                             "escalation_rate": 0.10, "escalation_rate_n": 20,
                                             "absence_fp_rate": 0.02, "absence_fp_rate_n": 10},
                              "issuer": {"precision": 1.0, "precision_n": 20,
                                        "recall": 1.0, "recall_n": 20,
                                        "span_accuracy": 1.0, "span_accuracy_n": 20,
                                        "escalation_rate": 0.0, "escalation_rate_n": 20,
                                        "absence_fp_rate": 0.0, "absence_fp_rate_n": 0},
                          })
    candidate_ok = EvalReport(report_id="cand-ok", spec_id="s", spec_version="1.0.1",
                              generated_at="2026-01-05",
                              fields={
                                  "barrier_pct": {"precision": 0.93, "precision_n": 20,
                                                 "recall": 0.88, "recall_n": 20,
                                                 "span_accuracy": 0.91, "span_accuracy_n": 18,
                                                 "escalation_rate": 0.11, "escalation_rate_n": 20,
                                                 "absence_fp_rate": 0.02, "absence_fp_rate_n": 10},
                                  "issuer": {"precision": 1.0, "precision_n": 20,
                                            "recall": 1.0, "recall_n": 20,
                                            "span_accuracy": 1.0, "span_accuracy_n": 20,
                                            "escalation_rate": 0.0, "escalation_rate_n": 20,
                                            "absence_fp_rate": 0.0, "absence_fp_rate_n": 0},
                              })
    gate_ok = evaluate_regression(baseline, candidate_ok)
    assert not gate_ok.blocked, gate_ok.explain()
    print("regression gate: small change within threshold -> PASS\n" + gate_ok.explain())

    candidate_bad = EvalReport(report_id="cand-bad", spec_id="s", spec_version="1.0.2",
                               generated_at="2026-01-06",
                               fields={
                                   "barrier_pct": {"precision": 0.70, "precision_n": 20,
                                                  "recall": 0.60, "recall_n": 20,
                                                  "span_accuracy": 0.91, "span_accuracy_n": 18,
                                                  "escalation_rate": 0.11, "escalation_rate_n": 20,
                                                  "absence_fp_rate": 0.02, "absence_fp_rate_n": 10},
                               })
    gate_bad = evaluate_regression(baseline, candidate_bad)
    assert gate_bad.blocked
    assert any(r.field == "barrier_pct" and r.metric == "precision" for r in gate_bad.regressions)
    assert "issuer" in gate_bad.missing_fields
    print("\nregression gate: dropped precision + missing field -> BLOCKED\n" + gate_bad.explain())

    # -- report store round-trips and finds the latest accepted baseline -----
    rstore = EvalReportStore(":memory:")
    rstore.save(baseline, accepted_baseline=True)
    rstore.save(candidate_ok)
    assert rstore.latest_baseline("s").report_id == "base"
    assert len(rstore.history("s")) == 2
    print("\nEvalReportStore round-trip + latest_baseline: PASS")

    print("\neval_harness self-check: PASS")
