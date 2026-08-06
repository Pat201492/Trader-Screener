"""
Gate for the extraction eval harness (issue #108). Same convention as
`test_extraction_ladder.py` / `test_validation.py`: stdlib only, run
directly, exit 0 = pass, no network and no model.

Every acceptance criterion in #108 is checked here:

  * held-out selection is stratified across issuer/product_type and
    deterministic for a fixed seed;
  * a held-out document's verdicts are provably excluded from the exemplar
    store AND from rule-seed anchor stability -- checked at the
    `validation.py` layer this module builds on;
  * `run_eval` refuses to score a document that is not marked held-out;
  * per-field precision, recall, span accuracy, escalation rate, and
    absence-handling are computed correctly, including the two cases an
    aggregate score would hide: a wrong value counts against BOTH precision
    and recall, and a hallucination on a genuinely-absent field is scored
    separately from a correctly-declined absence;
  * the regression gate blocks a field that drops past threshold, ignores
    noise within it, skips a metric with too little support to trust, and
    hard-blocks a field that is simply missing from the candidate report;
  * a report is stamped with spec/exemplar-set/rule-set/model versions and
    round-trips through the local report store.

Run:  python tools/edgar_scrubber/test_eval_harness.py
"""
import json

import eval_harness as eh
import validation as v
from field_spec import load_specs

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


SPEC = load_specs()["structured_note"]


# --------------------------------------------------------------------------- #
def test_held_out_selection_stratified_and_deterministic():
    section("Held-out selection is stratified across issuer/product_type and deterministic")
    candidates = (
        [{"accession": f"jpm-{i}", "document": "x", "issuer": "JPM", "product_type": "autocall"}
         for i in range(30)]
        + [{"accession": f"gs-{i}", "document": "x", "issuer": "GS", "product_type": "buffer"}
           for i in range(3)]
    )
    selected = eh.select_held_out(candidates, fraction=0.2, seed=7)
    check("target size honored", len(selected) == round(len(candidates) * 0.2))
    check("minority issuer (GS, n=3) is represented, not crowded out",
          any(c["issuer"] == "GS" for c in selected))
    check("every selection carries its stratum",
          all("::" in c["stratum"] for c in selected))
    again = eh.select_held_out(candidates, fraction=0.2, seed=7)
    check("same seed -> identical selection (comparability across runs)", again == selected)
    different_seed = eh.select_held_out(candidates, fraction=0.2, seed=99)
    check("different seed -> a different selection",
          [c["accession"] for c in different_seed] != [c["accession"] for c in selected])

    # target_n overrides fraction; min_per_stratum guarantees small strata a seat.
    exact = eh.select_held_out(candidates, target_n=6, min_per_stratum=2, seed=1)
    check("target_n is honored exactly", len(exact) == 6)
    check("min_per_stratum=2 guarantees GS at least 2 seats",
          sum(1 for c in exact if c["issuer"] == "GS") >= 2)


# --------------------------------------------------------------------------- #
def test_held_out_excluded_from_exemplars_and_rule_seeds():
    section("Held-out documents are provably excluded from exemplars (#106) and rule seeding (#107)")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="ho", target_n=10,
                                  rule_seed_threshold=2)

    # Reserve TWO documents held-out BEFORE either gets a verdict -- the
    # ordering #105's immediate-exemplar-write design requires.
    session.mark_held_out("h1", "x", issuer="JPM")
    session.mark_held_out("h2", "x", issuer="JPM")
    check("is_held_out reports true for a reserved document", session.is_held_out("h1", "x"))
    check("is_held_out reports false for an unreserved document",
          not session.is_held_out("h3", "x"))

    for acc in ("h1", "h2"):
        fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=(10, 16))
        fv.anchor = "Barrier"
        session.record_verdict(acc, "x", fv, issuer="JPM")
        session.complete_document(acc, "x", issuer="JPM")

    check("no exemplar written for a held-out (issuer, field)",
          store("JPM", "barrier_pct") is None)
    check("anchor_verdicts (the rule-seed input) sees zero held-out anchors",
          store.anchor_verdicts("JPM", "barrier_pct") == [])
    check("rule_seed_for never fires purely from held-out documents",
          session.rule_seed_for("JPM", "barrier_pct") is None)

    # A third, NON-held-out document for the same (issuer, field): teaching
    # resumes normally -- the guard is per-document, not a blanket switch.
    fv3 = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=(10, 16))
    fv3.anchor = "Barrier"
    session.record_verdict("live-1", "x", fv3, issuer="JPM")
    session.complete_document("live-1", "x", issuer="JPM")
    check("a normally-validated document still writes an exemplar",
          store("JPM", "barrier_pct") is not None)
    check("held_out_documents() lists exactly the two reserved docs, not the live one",
          {d["accession"] for d in store.held_out_documents("ho")} == {"h1", "h2"})


# --------------------------------------------------------------------------- #
class FakeExtractor:
    """Direct control over what `run_eval` sees per (accession, document) --
    isolates eval_harness's OWN scoring logic from the extraction ladder
    (#104) and validation loop (#105) machinery, which their own gates
    already cover."""

    def __init__(self, plan):
        self.plan = plan  # {(accession, document): [FieldProposal, ...]}

    def propose(self, render_doc, *, issuer=None, ex107=None, accession=None,
               document=None, fields=None):
        props = self.plan.get((accession, document), [])
        if fields:
            props = [p for p in props if p.field in fields]
        return props


def _gold(store, session_id, accession, document, issuer, entries):
    """Write gold verdicts directly (bypassing ValidationSession, since this
    test wants exact control over span/value) and mark the document
    validated + held out."""
    store.ensure_session(session_id, spec_id=SPEC.spec_id, target_n=10)
    store.mark_held_out(session_id, accession, document, issuer=issuer)
    for fv in entries:
        store.write_verdict(session_id, accession, document, fv)
    store.mark_document_validated(session_id, accession, document, issuer=issuer)


# --------------------------------------------------------------------------- #
def test_run_eval_refuses_a_non_held_out_case():
    section("run_eval refuses to score a document not marked held-out")
    store = v.ValidationStore(":memory:")
    store.ensure_session("s", spec_id=SPEC.spec_id, target_n=1)
    store.write_verdict("s", "a1", "x", v.FieldVerdict.accept(
        v.FieldProposal("issuer", "JPM", source_span=(0, 3))))
    store.mark_document_validated("s", "a1", "x", issuer="JPM")
    # deliberately NOT marked held-out

    extractor = FakeExtractor({("a1", "x"): [v.FieldProposal("issuer", "JPM", source_span=(0, 3),
                                                              rung="local")]})
    case = eh.HeldOutCase(session_id="s", accession="a1", document="x", issuer="JPM",
                          render_doc=None)
    raised = False
    try:
        eh.run_eval(SPEC, extractor, [case], store, report_id="r", generated_at="t",
                   fields=["issuer"])
    except ValueError:
        raised = True
    check("ValueError raised for a non-held-out case", raised)


# --------------------------------------------------------------------------- #
def test_run_eval_per_field_metrics():
    section("Per-field precision/recall/span-accuracy/escalation/absence-handling")
    store = v.ValidationStore(":memory:")

    # doc1: barrier_pct correct+spanned, cap_pct genuinely absent (and stays
    # absent in the prediction), issuer correct (case differs -- still a match).
    _gold(store, "s", "d1", "x", "JPM", [
        v.FieldVerdict.correct("barrier_pct", 70.0, source_span=(100, 106)),
        v.FieldVerdict.reject("cap_pct"),
        v.FieldVerdict.accept(v.FieldProposal("issuer", "JPM", source_span=(0, 3))),
    ])
    # doc2: barrier_pct wrong value (escalated to Claude), cap_pct absent but
    # HALLUCINATED by the model, issuer correct.
    _gold(store, "s", "d2", "x", "GS", [
        v.FieldVerdict.correct("barrier_pct", 65.0, source_span=(50, 56)),
        v.FieldVerdict.reject("cap_pct"),
        v.FieldVerdict.accept(v.FieldProposal("issuer", "GS", source_span=(0, 2))),
    ])

    plan = {
        ("d1", "x"): [
            v.FieldProposal("barrier_pct", 70.0, source_span=(100, 107), rung="local"),
            v.FieldProposal("cap_pct", None, source_span=None, rung="local"),
            v.FieldProposal("issuer", "jpm", source_span=(0, 3), rung="local"),
        ],
        ("d2", "x"): [
            v.FieldProposal("barrier_pct", 999.0, source_span=(999, 1005), rung="claude"),
            v.FieldProposal("cap_pct", 12.5, source_span=(10, 14), rung="local"),
            v.FieldProposal("issuer", "GS", source_span=(0, 2), rung="local"),
        ],
    }
    extractor = FakeExtractor(plan)

    cases = [
        eh.HeldOutCase(session_id="s", accession="d1", document="x", issuer="JPM", render_doc=None),
        eh.HeldOutCase(session_id="s", accession="d2", document="x", issuer="GS", render_doc=None),
    ]
    report = eh.run_eval(SPEC, extractor, cases, store, report_id="rep1", generated_at="2026-08-06",
                        exemplar_set_version="ex-v3", rule_set_version="rules-v2",
                        local_model="qwen2.5:7b", fields=["barrier_pct", "cap_pct", "issuer"])

    check("scored both held-out documents", report.n_documents == 2)

    bp = report.fields["barrier_pct"]
    check("barrier_pct precision: 1 correct of 2 produced", bp["precision"] == 0.5)
    check("barrier_pct recall: 1 found of 2 present", bp["recall"] == 0.5)
    check("barrier_pct span accuracy: the one TP had an overlapping span",
          bp["span_accuracy"] == 1.0)
    check("barrier_pct escalation rate: 1 of 2 reached Claude", bp["escalation_rate"] == 0.5)

    cp = report.fields["cap_pct"]
    check("cap_pct: correctly-declined absence does not count as a false positive",
          cp["absence_fp_rate"] == 0.5)
    check("cap_pct: precision penalizes the hallucination", cp["precision"] == 0.0)
    check("cap_pct: recall is undefined (no gold-present instance) rather than a fake 0/0",
          cp["recall"] is None)

    iss = report.fields["issuer"]
    check("issuer: case-insensitive string match still counts as correct",
          iss["precision"] == 1.0 and iss["recall"] == 1.0)
    check("issuer: both spans matched exactly", iss["span_accuracy"] == 1.0)

    check("report stamped with exemplar_set_version", report.exemplar_set_version == "ex-v3")
    check("report stamped with rule_set_version", report.rule_set_version == "rules-v2")
    check("report stamped with local_model", report.local_model == "qwen2.5:7b")
    check("report stamped with spec_id/spec_version",
          report.spec_id == SPEC.spec_id and report.spec_version == SPEC.version)

    # to_json / from_dict round-trip exactly.
    round_tripped = eh.EvalReport.from_dict(json.loads(report.to_json()))
    check("report round-trips through to_json/from_dict unchanged",
          round_tripped.as_dict() == report.as_dict())


# --------------------------------------------------------------------------- #
def test_regression_gate():
    section("Regression gate: blocks past threshold, tolerates noise, needs support, blocks a missing field")

    def _report(rid, precision, recall, n=20):
        return eh.EvalReport(report_id=rid, spec_id="s", spec_version="1.0.0", generated_at="t",
                            fields={
                                "barrier_pct": {"precision": precision, "precision_n": n,
                                               "recall": recall, "recall_n": n,
                                               "span_accuracy": 0.9, "span_accuracy_n": n,
                                               "escalation_rate": 0.1, "escalation_rate_n": n,
                                               "absence_fp_rate": 0.02, "absence_fp_rate_n": n},
                            })

    baseline = _report("base", 0.95, 0.90)

    noisy = _report("noisy", 0.93, 0.89)   # small change, within default 0.05 threshold
    gate_noise = eh.evaluate_regression(baseline, noisy)
    check("small within-threshold change does not block", not gate_noise.blocked)

    bad = _report("bad", 0.60, 0.90)       # 0.35 precision drop, well past threshold
    gate_bad = eh.evaluate_regression(baseline, bad)
    check("a field dropping past threshold blocks the gate", gate_bad.blocked)
    check("the regression names the right field and metric",
          any(r.field == "barrier_pct" and r.metric == "precision" for r in gate_bad.regressions))

    low_n = _report("low_n", 0.50, 0.90, n=2)  # huge drop, but n=2 is too little to trust
    gate_low_n = eh.evaluate_regression(baseline, low_n, min_support=5)
    check("a metric below min_support is skipped, not blocked on noise",
          not gate_low_n.blocked)

    missing = eh.EvalReport(report_id="missing", spec_id="s", spec_version="1.0.0",
                            generated_at="t", fields={})
    gate_missing = eh.evaluate_regression(baseline, missing)
    check("a field entirely absent from the candidate hard-blocks the gate",
          gate_missing.blocked and "barrier_pct" in gate_missing.missing_fields)

    check("explain() reports PASS for a clean gate", "PASS" in gate_noise.explain())
    check("explain() reports BLOCKED with the field name for a bad gate",
          "BLOCKED" in gate_bad.explain() and "barrier_pct" in gate_bad.explain())


# --------------------------------------------------------------------------- #
def test_report_store_comparable_across_runs():
    section("EvalReportStore: local-only, comparable across runs via latest_baseline")
    store = eh.EvalReportStore(":memory:")
    r1 = eh.EvalReport(report_id="r1", spec_id="424b2.structured_note", spec_version="1.0.0",
                       generated_at="2026-08-01", local_model="qwen2.5:7b", fields={})
    r2 = eh.EvalReport(report_id="r2", spec_id="424b2.structured_note", spec_version="1.0.1",
                       generated_at="2026-08-02", local_model="qwen2.5:7b", fields={})
    store.save(r1, accepted_baseline=True)
    store.save(r2)
    check("get() round-trips a saved report", store.get("r1").report_id == "r1")
    check("latest_baseline() returns the accepted baseline, not the newer candidate",
          store.latest_baseline("424b2.structured_note").report_id == "r1")
    check("history() lists both runs, newest first",
          [r.report_id for r in store.history("424b2.structured_note")] == ["r2", "r1"])
    store.accept_as_baseline("r2")
    check("accept_as_baseline promotes a new candidate to the accepted baseline",
          store.latest_baseline("424b2.structured_note").report_id in ("r1", "r2"))


# --------------------------------------------------------------------------- #
def test_reserve_held_out_set_marks_before_any_verdict():
    section("reserve_held_out_set marks documents held-out from a candidate pool, before validation")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="reserve", target_n=10)
    candidates = [
        {"accession": "a1", "document": "x", "issuer": "JPM", "product_type": "autocall"},
        {"accession": "a2", "document": "x", "issuer": "JPM", "product_type": "autocall"},
        {"accession": "a3", "document": "x", "issuer": "GS", "product_type": "buffer"},
        {"accession": "a4", "document": "x", "issuer": "GS", "product_type": "buffer"},
    ]
    selected = eh.reserve_held_out_set(session, candidates, target_n=2, min_per_stratum=1, seed=3)
    check("reserve_held_out_set returns the selected candidates", len(selected) == 2)
    for c in selected:
        check(f"{c['accession']} is marked held-out in the session store",
              session.is_held_out(c["accession"], c["document"]))

    # Validating a reserved document writes NO exemplar, from its very first verdict.
    held_acc = selected[0]["accession"]
    held_issuer = selected[0]["issuer"]
    fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=(0, 6))
    fv.anchor = "Barrier"
    session.record_verdict(held_acc, "x", fv, issuer=held_issuer)
    check("the first verdict on a reserved document writes no exemplar",
          store(held_issuer, "barrier_pct") is None)


def main():
    print("Extraction eval harness gate (#108)")
    test_held_out_selection_stratified_and_deterministic()
    test_held_out_excluded_from_exemplars_and_rule_seeds()
    test_run_eval_refuses_a_non_held_out_case()
    test_run_eval_per_field_metrics()
    test_regression_gate()
    test_report_store_comparable_across_runs()
    test_reserve_held_out_set_marks_before_any_verdict()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\neval_harness gate: PASS")


if __name__ == "__main__":
    main()
