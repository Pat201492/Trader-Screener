"""
Gate for the validation-session run report (issue #165). Same convention as
`test_validation.py` / `test_eval_harness.py`: stdlib only, run directly, exit
0 = pass. No network and no model -- verdicts are written straight into an
in-memory ValidationStore, and the report is read back out.

The three run-report facts #165 needs from a session's recorded verdicts are
checked here:

  * PER-FIELD ACCEPT RATE -- verdict-only, an upper bound on precision;
  * COMPOUNDING SPLIT -- first half vs second half accept rate, positive when
    later documents accept more (the VALIDATION_UI.md claim);
  * PER-DOCUMENT WALL-CLOCK -- derived from verdict timestamps, in validation
    order, None when the session ran without a clock.

Plus the markdown render and the held-out eval baseline table.

Run:  python tools/edgar_scrubber/test_session_report.py
"""
from datetime import datetime

try:  # package import: tools.edgar_scrubber.test_session_report
    from . import session_report as sr
    from .validation import ValidationStore, ValidationSession, FieldVerdict, ACCEPT, CORRECT, REJECT
    from .eval_harness import EvalReport
except ImportError:  # standalone
    import session_report as sr
    from validation import ValidationStore, ValidationSession, FieldVerdict, ACCEPT, CORRECT, REJECT
    from eval_harness import EvalReport

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


class _Spec:
    spec_id = "test-spec"
    form_type = "424B2"

    def field_names(self):
        return ["issuer", "aggregate_principal", "barrier_pct"]

    def field(self, name):
        return object()


def _clock():
    state = {"t": 0}

    def tick():
        state["t"] += 30
        return datetime.fromtimestamp(state["t"]).isoformat()

    return tick


def _seed_session(*, clock=None):
    """Four documents, two issuers. Early documents carry more corrects, later
    ones more accepts -- so the compounding delta must come out positive."""
    store = ValidationStore(":memory:")
    spec = _Spec()
    sess = ValidationSession(store, spec, session_id="s1", target_n=4, clock=clock)
    plan = [
        ("acc1", "JPMorgan", [ACCEPT, CORRECT, CORRECT]),
        ("acc2", "JPMorgan", [ACCEPT, CORRECT, REJECT]),
        ("acc3", "Citigroup", [ACCEPT, ACCEPT, CORRECT]),
        ("acc4", "Citigroup", [ACCEPT, ACCEPT, ACCEPT]),
    ]
    for acc, issuer, verdicts in plan:
        for fld, verdict in zip(spec.field_names(), verdicts):
            if verdict == ACCEPT:
                fv = FieldVerdict(field=fld, verdict=ACCEPT, value="v")
            elif verdict == CORRECT:
                fv = FieldVerdict.correct(fld, "corrected")
            else:
                fv = FieldVerdict.reject(fld)
            sess.record_verdict(acc, "424b2.htm", fv, issuer=issuer)
        sess.complete_document(acc, "424b2.htm", issuer=issuer)
    return store, spec


# --------------------------------------------------------------------------- #
def test_field_accept_rates():
    section("per-field accept rate is a verdict-only upper bound on precision")
    store, spec = _seed_session()
    rates = {fa.field: fa for fa in sr.field_accept_rates(store, "s1", spec)}
    check("issuer accepted on every document (rate 1.0)",
          rates["issuer"].accept == 4 and rates["issuer"].accept_rate == 1.0)
    check("barrier_pct counts its two corrects and one reject",
          rates["barrier_pct"].correct == 2 and rates["barrier_pct"].reject == 1)
    check("accept rate divides by PRODUCED (accept+correct), not by rejects too",
          abs(rates["barrier_pct"].accept_rate - 1 / 3) < 1e-9)
    check("every spec field appears even when never verdicted",
          set(rates) == set(spec.field_names()))
    store.close()


def test_field_never_verdicted_is_visible():
    section("a spec field with no verdicts shows as an empty tally, not missing")
    store = ValidationStore(":memory:")
    spec = _Spec()
    sess = ValidationSession(store, spec, session_id="s1", target_n=1)
    sess.record_verdict("acc1", "d.htm", FieldVerdict(field="issuer", verdict=ACCEPT, value="v"),
                        issuer="JPMorgan")
    sess.complete_document("acc1", "d.htm", issuer="JPMorgan")
    rates = {fa.field: fa for fa in sr.field_accept_rates(store, "s1", spec)}
    check("barrier_pct present with zero produced and a None rate",
          rates["barrier_pct"].produced == 0 and rates["barrier_pct"].accept_rate is None)
    store.close()


# --------------------------------------------------------------------------- #
def test_compounding_split():
    section("compounding: later documents accept at a higher rate than earlier")
    store, spec = _seed_session()
    comp = sr.compounding_split(store, "s1")
    check("four documents split two and two", comp.first.documents == 2 and comp.second.documents == 2)
    check("first-half accept rate below second-half",
          comp.first.accept_rate < comp.second.accept_rate)
    check("delta is positive (the compounding VALIDATION_UI.md claims)",
          comp.delta is not None and comp.delta > 0)
    store.close()


def test_compounding_odd_count_extra_in_second_half():
    section("an odd document count puts the extra document in the second half")
    store = ValidationStore(":memory:")
    spec = _Spec()
    sess = ValidationSession(store, spec, session_id="s1", target_n=3)
    for acc in ("a", "b", "c"):
        sess.record_verdict(acc, "d.htm", FieldVerdict(field="issuer", verdict=ACCEPT, value="v"),
                            issuer="JPMorgan")
        sess.complete_document(acc, "d.htm", issuer="JPMorgan")
    comp = sr.compounding_split(store, "s1")
    check("split is 1 + 2", comp.first.documents == 1 and comp.second.documents == 2)
    store.close()


# --------------------------------------------------------------------------- #
def test_per_document_timings():
    section("per-document wall-clock comes from verdict timestamps, in order")
    store, spec = _seed_session(clock=_clock())
    timings = sr.per_document_timings(store, "s1")
    check("a timing row per document", len(timings) == 4)
    check("returned in validation order",
          [t.accession for t in timings] == ["acc1", "acc2", "acc3", "acc4"])
    check("each timed document has a positive wall-clock",
          all(t.seconds is not None and t.seconds > 0 for t in timings))
    check("three verdicts per document counted", all(t.verdicts == 3 for t in timings))
    summ = sr.timing_summary(timings)
    check("summary reports all four as timed and a positive median",
          summ["timed_documents"] == 4 and summ["median_seconds"] > 0)
    store.close()


def test_timings_none_without_clock():
    section("without a clock, wall-clock is None rather than a fabricated zero")
    store, spec = _seed_session(clock=None)
    timings = sr.per_document_timings(store, "s1")
    check("no seconds when the session ran without a clock",
          all(t.seconds is None for t in timings))
    check("timing summary reports zero timed documents",
          sr.timing_summary(timings)["timed_documents"] == 0)
    store.close()


# --------------------------------------------------------------------------- #
def test_render_session_report_markdown():
    section("markdown report renders every section")
    store, spec = _seed_session(clock=_clock())
    md = sr.render_session_report(store, "s1", spec)
    for heading in ("Validation run report", "Compounding", "Per-field accept rate",
                    "Per-document wall-clock"):
        check(f"section present: {heading}", heading in md)
    check("issuer count reflects both issuers", "issuers: 2" in md)
    store.close()


def test_render_eval_baseline_table():
    section("held-out eval baseline renders as a per-field table (criterion 3)")
    report = EvalReport(
        report_id="run-1", spec_id="test-spec", spec_version="1",
        generated_at="2026-09-23T00:00:00Z", local_model="qwen2.5:7b",
        held_out_sessions=("s1",), n_documents=3,
        fields={"barrier_pct": {"precision": 0.5, "recall": 0.4, "span_accuracy": 1.0,
                                "escalation_rate": 0.0, "absence_fp_rate": 0.0}})
    md = sr.render_eval_markdown(report)
    check("eval baseline names the report id", "run-1" in md)
    check("precision rendered as a percentage", "50.0%" in md)
    store, spec = _seed_session()
    combined = sr.render_session_report(store, "s1", spec, eval_report=report)
    check("session report embeds the eval baseline when supplied",
          "Held-out eval baseline" in combined)
    store.close()


def main():
    print("Validation-session run report gate (#165)")
    test_field_accept_rates()
    test_field_never_verdicted_is_visible()
    test_compounding_split()
    test_compounding_odd_count_extra_in_second_half()
    test_per_document_timings()
    test_timings_none_without_clock()
    test_render_session_report_markdown()
    test_render_eval_baseline_table()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nsession report gate: PASS")


if __name__ == "__main__":
    main()
