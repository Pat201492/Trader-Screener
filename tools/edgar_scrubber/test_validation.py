"""
Gate for the human-in-loop validation loop (issue #105). Same convention as
`test_extraction_ladder.py` / `test_output_store.py`: stdlib only, run directly,
exit 0 = pass. No network and no model -- fake clients script the ladder, an
in-memory (or temp-file) ValidationStore stands in for the local store.

Every acceptance criterion in #105 is checked here:

  * validate a 424B2 end-to-end -- EVERY spec field gets a verdict;
  * corrected spans persist with EXACT offsets, resolving through all four of
    #101's reduction stages (table pre-parse, boilerplate strip, section split,
    sub-block) back to the original document bytes;
  * documents 11+ measurably improve over 1-10 on the same issuer -- the test
    that proves the loop closes (exemplars written by validation feed the
    ladder's rung 3, and the same document goes from gated/wrong to clean/right);
  * a rule-seed prompt appears once a field's anchor is stable across validated
    documents, and only once;
  * the session is resumable -- a partial document is never lost across a close.

Plus the pieces those rest on: the store is the ladder's callable exemplar
provider, the three verdicts render the three exemplar kinds, and the two-pane
screen highlights the candidate span.

Run:  python tools/edgar_scrubber/test_validation.py
"""
import json
import os
import tempfile

try:  # package import: tools.edgar_scrubber.test_validation
    from . import validation as v
    from . import validate_ui as ui
    from .field_spec import load_specs
    from .normalize import normalize_html
    from .reduce import build_boilerplate_model, strip_boilerplate, split_sections, sub_block
    from .extraction_ladder import ExtractionLadder
except ImportError:  # standalone: python tools/edgar_scrubber/test_validation.py
    import validation as v
    import validate_ui as ui
    from field_spec import load_specs
    from normalize import normalize_html
    from reduce import build_boilerplate_model, strip_boilerplate, split_sections, sub_block
    from extraction_ladder import ExtractionLadder

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


SPEC = load_specs()["structured_note"]
ISSUER = "JPMorgan Chase Financial Company LLC"


# --------------------------------------------------------------------------- #
# Fake clients (same shape as OllamaClient.chat_completion; no network)
# --------------------------------------------------------------------------- #

def _wire_key_of(response_format):
    return next(iter(response_format["json_schema"]["schema"]["properties"]))


class AllFieldsClient:
    """Answers whatever single field the ladder asks for, with a
    type-appropriate value and a locatable span -- so `LadderExtractor` yields a
    proposal for EVERY spec field."""

    def chat_completion(self, messages, model=None, response_format=None, **kw):
        props = response_format["json_schema"]["schema"]["properties"]
        key = next(iter(props))
        vtype = props[key]["properties"]["v"].get("type", "string")
        value = {"array": [], "number": 1.0, "boolean": True}.get(vtype, "x")
        body = json.dumps({key: {"v": value, "s": [0, 1], "c": 0.9}})
        return {"choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class SpanFindingClient:
    """Locates `target` inside the SOURCE TEXT the ladder actually sent and
    returns that offset pair -- so the span the extractor composes back to
    source is a REAL model answer, not a hand-fed constant."""

    def __init__(self, target, value):
        self.target, self.value = target, value

    def chat_completion(self, messages, model=None, response_format=None, **kw):
        key = _wire_key_of(response_format)
        content = messages[-1]["content"]
        src = content.split("SOURCE TEXT:\n", 1)[-1]
        i = src.find(self.target)
        s = [i, i + len(self.target)] if i >= 0 else None
        body = json.dumps({key: {"v": self.value, "s": s, "c": 0.95}})
        return {"choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class ImprovingClient:
    """The whole point of the loop, in one client: WITHOUT an exemplar for this
    (issuer, field) it returns a wrong, out-of-bounds value (which the local-only
    ladder gates); WITH one carrying the confirmed value it returns the right,
    in-bounds value. Nothing about the client changes between the two calls --
    only what the validation loop has written to the store since."""

    GOOD, BAD = 70.0, 130.0

    def chat_completion(self, messages, model=None, response_format=None, **kw):
        key = _wire_key_of(response_format)
        prompt = " ".join(m["content"] for m in messages)
        learned = "EXEMPLARS" in prompt and "70.0" in prompt
        value = self.GOOD if learned else self.BAD
        conf = 0.92 if learned else 0.5
        body = json.dumps({key: {"v": value, "s": [0, 6], "c": conf}})
        return {"choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def scripted_reader(answers):
    """A keyboard stand-in for `validate_document`: pops scripted answers, then
    returns '' (Enter / next document) forever."""
    queue = list(answers)

    def _read(_prompt=""):
        return queue.pop(0) if queue else ""
    return _read


# --------------------------------------------------------------------------- #
# Documents used across the four-stage tests
# --------------------------------------------------------------------------- #

DOC_A = """
<html><body>
<p>Issuer: JPMorgan Chase Financial Company LLC. CUSIP: 48133YHT4.</p>
<p>These securities are not deposits and are not FDIC insured. Principal at risk.</p>
<h2>Key Terms</h2>
<table>
  <tr><td>Contingent Coupon Rate</td><td>9.15% per annum</td></tr>
</table>
<h2>Downside Protection</h2>
<p>The Barrier is 70.00% of the Initial Value; principal is at risk below it.</p>
<h2>Estimated Value of the Notes</h2>
<p>Our estimated value of the notes is $972.30 per $1,000 stated principal amount.</p>
</body></html>
"""

# Shares ONLY the FDIC line with DOC_A (so stage-2 dedup strips that and nothing
# else); the headings are phrased differently but classify to the same sections,
# so each heading appears in only one filing and survives the strip.
DOC_B = """
<html><body>
<p>Issuer: GS Finance Corp. CUSIP: 40057KAA1.</p>
<p>These securities are not deposits and are not FDIC insured. Principal at risk.</p>
<h2>Summary of Terms</h2>
<table>
  <tr><td>Contingent Coupon Rate</td><td>8.10% per annum</td></tr>
</table>
<h2>Downside</h2>
<p>The Barrier is 65.00% of the Initial Value; principal is at risk below it.</p>
<h2>Estimated Value of the Securities</h2>
<p>Our estimated value of the notes is $981.00 per $1,000 stated principal amount.</p>
</body></html>
"""


def _reduced_render_doc():
    """DOC_A run through stages 1 (table pre-parse, in normalize) and 2
    (boilerplate strip) -- the render document a pane draws and a correction
    resolves against, its offset map composed back to the original bytes."""
    nd = normalize_html(DOC_A)
    model = build_boilerplate_model(ISSUER, [nd, normalize_html(DOC_B)])
    reduced = strip_boilerplate(nd, model)
    return nd, reduced, v.RenderDocument.from_reduced(reduced, nd.source)


# --------------------------------------------------------------------------- #
def test_every_field_gets_a_verdict():
    section("Validate a 424B2 end-to-end: EVERY spec field gets a verdict")
    nd = normalize_html(DOC_A)
    render_doc = v.RenderDocument.from_normalized(nd)
    ladder = ExtractionLadder(SPEC, local_client=AllFieldsClient(), local_model="qwen2.5:7b")
    extractor = v.LadderExtractor(SPEC, ladder)
    proposals = extractor.propose(render_doc, issuer=ISSUER, accession="0001",
                                  document="424b2.htm")
    check("one proposal per spec field",
          len(proposals) == len(SPEC.field_names()))

    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="e2e", target_n=1)
    # 'A' (accept-all) on the first field accepts it and every remaining field.
    ui.validate_document(session, proposals, render_doc, accession="0001",
                         document="424b2.htm", issuer=ISSUER, color=False,
                         read=scripted_reader(["A"]), write=lambda *a, **k: None)

    ok, missing = session.all_fields_verdicted("0001", "424b2.htm")
    check("every spec field has a verdict", ok)
    check("no field left undecided", missing == [])
    check("document counts toward the target once complete",
          session.progress()["validated"] == 1)


# --------------------------------------------------------------------------- #
def test_corrected_span_resolves_through_all_four_stages():
    section("Corrected spans persist with exact offsets through all four #101 stages")
    nd, reduced, render_doc = _reduced_render_doc()
    sections = split_sections(reduced)                 # stage 3
    check("stage 3 found the key_terms section", "key_terms" in sections)

    # LadderExtractor path: the model answers a span into the SECTION-scoped
    # context (stage 3), and the extractor composes it through the context map
    # -> reduced map (stages 1-2) -> original bytes.
    ladder = ExtractionLadder(SPEC, local_client=SpanFindingClient("70.00%", 70.0),
                              local_model="qwen2.5:7b")
    extractor = v.LadderExtractor(SPEC, ladder, sections=sections)
    props = extractor.propose(render_doc, issuer=ISSUER, accession="0001",
                              document="424b2.htm", fields=["barrier_pct"])
    prop = props[0]
    check("extractor produced a source span", prop.source_span is not None)
    check("extractor span slices '70.00%' out of the ORIGINAL html",
          "70.00%" in nd.source[prop.source_span[0]:prop.source_span[1]])

    # Correction path: a hand-marked span on the reduced document resolves to
    # the same exact original offsets.
    mark = v.locate_span(render_doc, "70.00%")
    check("correction resolves to the original bytes exactly",
          nd.source[mark.source_span[0]:mark.source_span[1]] == "70.00%")

    # Stage 4: the stored source offset round-trips back into a text window
    # (reduce.sub_block via source_offset), so the warm path #107 will use lands
    # on the same span.
    block = sub_block(reduced, source_offset=mark.source_span[0], window=60)
    check("stage-4 sub-block from the stored source offset re-finds the span",
          "70.00%" in block.text)

    # Persist the correction and read it back with byte-exact offsets.
    store = v.ValidationStore(":memory:")
    sess = v.ValidationSession(store, SPEC, session_id="s4")
    fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=mark.source_span)
    sess.record_verdict("0001", "424b2.htm", fv, issuer=ISSUER, render_doc=render_doc)
    stored = store.verdicts_for("s4", "0001", "424b2.htm")[0]
    check("stored corrected span is byte-identical after a round-trip",
          stored.source_span == mark.source_span)
    check("stored span still slices the original bytes",
          nd.source[stored.source_span[0]:stored.source_span[1]] == "70.00%")


# --------------------------------------------------------------------------- #
def test_documents_11_plus_improve():
    section("Documents 11+ measurably improve over 1-10 on the same issuer")
    store = v.ValidationStore(":memory:")

    def fresh_ladder():
        # local-only (#104): a gated field flags instead of escalating, so the
        # gate outcome is the measurable signal without a Claude call.
        return ExtractionLadder(SPEC, exemplars=store, local_client=ImprovingClient(),
                                local_model="qwen2.5:7b", claude_client=None)

    # Baseline: nothing learned yet -> wrong, out-of-bounds, GATED.
    baseline = fresh_ladder().extract("barrier_pct", text="Barrier level context.",
                                      issuer=ISSUER, accession="doc11", document="x")
    check("baseline value is wrong (no exemplars yet)", baseline.value == ImprovingClient.BAD)
    check("baseline field is gated (out of bounds)", baseline.gated is True)

    # The loop: validate documents 1-10, each recording the human-confirmed
    # boundary, which writes an exemplar keyed by (issuer, field).
    nd = normalize_html(DOC_A)
    render_doc = v.RenderDocument.from_normalized(nd)
    sess = v.ValidationSession(store, SPEC, session_id="loop", target_n=10)
    for i in range(10):
        fv = v.FieldVerdict.correct("barrier_pct", 70.0)
        sess.record_verdict(f"doc{i}", "x", fv, issuer=ISSUER, render_doc=render_doc)
        sess.complete_document(f"doc{i}", "x", issuer=ISSUER)
    check("exemplars accumulated for (issuer, barrier_pct)",
          len(store.exemplars_for(ISSUER, "barrier_pct")) > 0)

    # Document 11, same field, same client: now RIGHT, in-bounds, not gated --
    # purely because of the work on 1-10.
    after = fresh_ladder().extract("barrier_pct", text="Barrier level context.",
                                   issuer=ISSUER, accession="doc11", document="x")
    check("document 11 value is now correct", after.value == ImprovingClient.GOOD)
    check("document 11 is no longer gated", after.gated is False)
    check("measurable improvement: gated -> clean, wrong -> right",
          baseline.gated and not after.gated and baseline.value != after.value)


# --------------------------------------------------------------------------- #
def test_rule_seed_appears_once_when_anchor_stable():
    section("Rule-seed prompt appears once a field's anchor is stable, and only once")
    nd, reduced, render_doc = _reduced_render_doc()
    store = v.ValidationStore(":memory:")
    sess = v.ValidationSession(store, SPEC, session_id="seed", target_n=5,
                               rule_seed_threshold=3)
    mark = v.locate_span(render_doc, "70.00%")

    # Two validated docs is below threshold: not yet a seed.
    for acc in ("d1", "d2"):
        fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=mark.source_span)
        sess.record_verdict(acc, "x", fv, issuer=ISSUER, render_doc=render_doc)
        sess.complete_document(acc, "x", issuer=ISSUER)
    check("below threshold: no seed offered yet",
          sess.rule_seed_for(ISSUER, "barrier_pct") is None)

    # Third: the anchor ("Barrier") is now stable across the corpus.
    fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=mark.source_span)
    sess.record_verdict("d3", "x", fv, issuer=ISSUER, render_doc=render_doc)
    sess.complete_document("d3", "x", issuer=ISSUER)
    seed = sess.rule_seed_for(ISSUER, "barrier_pct")
    check("at threshold: a rule seed is offered", seed is not None)
    check("seed anchors on the stable label", seed and seed.anchor == "Barrier")
    check("seed reports its support", seed and seed.support == 3)

    # Answering it (promote or dismiss) makes it stop appearing.
    sess.mark_seed(ISSUER, "barrier_pct", seed.anchor, promoted=True)
    check("seeded: the prompt does not appear again",
          sess.rule_seed_for(ISSUER, "barrier_pct") is None)
    check("pending_rule_seeds no longer lists it",
          all(s.field != "barrier_pct" for s in sess.pending_rule_seeds(ISSUER)))


# --------------------------------------------------------------------------- #
def test_session_resumable_partial_never_lost():
    section("Session resumable; a partial document is never lost across a close")
    tmp = tempfile.mkdtemp(prefix="edgar-val-")
    path = os.path.join(tmp, "validation.sqlite")

    # First run: rule three fields of a document, then "quit" WITHOUT completing.
    store = v.ValidationStore(path)
    sess = v.ValidationSession(store, SPEC, session_id="resume", target_n=3)
    for fld, val in (("issuer", ISSUER), ("cusip", "48133YHT4"),
                     ("pricing_date", "2026-01-15")):
        sess.record_verdict("0001", "424b2.htm",
                            v.FieldVerdict.correct(fld, val), issuer=ISSUER)
    store.close()

    # Reopen: the partial verdicts are still there, and the document is NOT yet
    # counted (it was never completed) -- partial progress preserved, not lost.
    store2 = v.ValidationStore(path)
    sess2 = v.ValidationSession(store2, SPEC, session_id="resume", target_n=3)
    resumed = {fv.field for fv in store2.verdicts_for("resume", "0001", "424b2.htm")}
    check("all three partial verdicts survived the close",
          {"issuer", "cusip", "pricing_date"} <= resumed)
    check("incomplete document is not counted toward the target",
          sess2.progress()["validated"] == 0)

    # A re-verdict of the same field overwrites in place (no duplicate rows).
    sess2.record_verdict("0001", "424b2.htm",
                        v.FieldVerdict.correct("cusip", "48133YHT9"), issuer=ISSUER)
    rows = [fv for fv in store2.verdicts_for("resume", "0001", "424b2.htm")
            if fv.field == "cusip"]
    check("re-verdict overwrites rather than duplicating", len(rows) == 1)
    check("re-verdict kept the corrected value", rows[0].value == "48133YHT9")
    store2.close()


# --------------------------------------------------------------------------- #
def test_store_is_the_ladder_exemplar_provider():
    section("The store is the ladder's callable exemplar provider (#106 handoff)")
    store = v.ValidationStore(":memory:")
    check("empty store returns None (ladder omits the exemplar block)",
          store(ISSUER, "barrier_pct") is None)

    sess = v.ValidationSession(store, SPEC, session_id="ex", target_n=3)
    nd = normalize_html(DOC_A)
    render_doc = v.RenderDocument.from_normalized(nd)

    sess.record_verdict("d1", "x", v.FieldVerdict.correct("barrier_pct", 70.0),
                        issuer=ISSUER, render_doc=render_doc)
    provided = store(ISSUER, "barrier_pct")
    check("after a verdict the store yields exemplar lines", bool(provided))
    check("a corrected exemplar carries the confirmed value string",
          any("70.0" in e for e in provided))

    # The three verdicts render three exemplar kinds; a reject teaches absence.
    sess.record_verdict("d1", "x", v.FieldVerdict.reject("cap_pct"),
                        issuer=ISSUER, render_doc=render_doc)
    neg = store(ISSUER, "cap_pct")
    check("reject writes a negative exemplar (absence is a valid answer)",
          neg and any("absent" in e for e in neg))
    check("corrected exemplars outrank plain positives in the returned order",
          store.exemplars_for(ISSUER, "barrier_pct")[0].endswith("(corrected)"))


# --------------------------------------------------------------------------- #
def test_two_pane_screen_highlights_the_candidate_span():
    section("The two-pane screen highlights the candidate span")
    nd = normalize_html(DOC_A)
    render_doc = v.RenderDocument.from_normalized(nd)
    store = v.ValidationStore(":memory:")
    sess = v.ValidationSession(store, SPEC, session_id="screen", target_n=1)

    mark = v.locate_span(render_doc, "70.00%")
    proposal = v.FieldProposal("barrier_pct", 70.0, source_span=mark.source_span,
                               provenance="model:qwen2.5-7b", confidence=0.9,
                               rung="local", unit="percent_of_initial")
    screen = ui.render_screen(sess, proposal, render_doc, field_index=0,
                              total_fields=1, section="key_terms", color=False)
    check("field name shown in the right pane", "barrier_pct" in screen)
    check("proposed value shown", "70.0" in screen)
    check("candidate span highlighted (bracketed in no-color mode)",
          "[70.00%]" in screen)
    check("section label shown ('scrolled to the relevant section')",
          "key_terms" in screen)
    check("accept/correct/reject offered", "[a]ccept" in screen and "[r]eject" in screen)

    # A proposal with no locatable span reads as a prime correct/reject cue.
    nospan = v.FieldProposal("guarantor", "??", source_span=None, rung="local")
    screen2 = ui.render_screen(sess, nospan, render_doc, field_index=1,
                               total_fields=2, color=False)
    check("missing span is called out explicitly", "no span located" in screen2)


# --------------------------------------------------------------------------- #
def test_locate_span_disambiguates_repeats():
    section("locate_span picks the occurrence nearest the wrong span (repeat values)")
    html = ("<html><body><p>Coupon Barrier: 70.00% of Initial Value.</p>"
            "<p>Downside Barrier: 70.00% of Initial Value.</p></body></html>")
    nd = normalize_html(html)
    render_doc = v.RenderDocument.from_normalized(nd)
    second = nd.text.rfind("70.00%")
    mark = v.locate_span(render_doc, "70.00%", near_text_offset=second)
    check("nearest-occurrence match lands on the second '70.00%'",
          mark.text_span[0] == second)
    check("still resolves to the original bytes",
          nd.source[mark.source_span[0]:mark.source_span[1]] == "70.00%")


def main():
    print("Human-in-loop validation loop gate (#105)")
    test_every_field_gets_a_verdict()
    test_corrected_span_resolves_through_all_four_stages()
    test_documents_11_plus_improve()
    test_rule_seed_appears_once_when_anchor_stable()
    test_session_resumable_partial_never_lost()
    test_store_is_the_ladder_exemplar_provider()
    test_two_pane_screen_highlights_the_candidate_span()
    test_locate_span_disambiguates_repeats()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nvalidation loop gate: PASS")


if __name__ == "__main__":
    main()
