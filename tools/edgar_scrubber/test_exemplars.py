"""
Gate for the exemplar store / few-shot prompt assembly (issue #106). Same
convention as `test_validation.py` / `test_extraction_ladder.py`: stdlib
only, run directly, exit 0 = pass. No network and no model -- fake clients
script `chat_completion`; an in-memory `ValidationStore` stands in for #105's
local store.

Every acceptance criterion in #106 is checked here:

  * #105 verdicts land in the store immediately and are visible to the
    provider without a separate build/index step;
  * the (form, issuer, field) key actually isolates -- a different form never
    leaks exemplars into another form's prompt, even for a same-named field;
  * selection policy: corrections rank ahead of accepts, near-duplicates
    collapse (diversity), a negative survives a tight cap, the cap is
    enforced;
  * fallback chain: exact issuer -> pooled (form, *, field) -> field
    description only, covering an unseen issuer AND an unseen field without
    ever raising;
  * static-prefix-first ordering is enforced in `build_messages`, with a test
    asserting byte-identical prefixes across two different document chunks
    for the same issuer/field, AND that the ladder's own runtime guard fires
    when that invariant is violated;
  * exemplar-set version is recorded in #104's provenance on every
    extraction -- present for the local/Claude rungs, absent (None) for
    rule/XBRL which never touch exemplars, and distinct across two different
    exemplar sets for the same field;
  * exemplars measurably change what the model is asked, end to end
    (provider -> ladder -> prompt), not just what the store returns in
    isolation.

Run:  python tools/edgar_scrubber/test_exemplars.py
"""
import json
import sys

import exemplars as ex
import extraction_ladder as el
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
FORM = SPEC.form_type
FIELD = "barrier_pct"
FIELD_DEF = SPEC.field(FIELD)
JPM = "JPMorgan Chase Financial Company LLC"
CITI = "Citigroup Global Markets Holdings Inc."
BOM = "Bank of Montreal"


def _wire_key(response_format):
    return next(iter(response_format["json_schema"]["schema"]["properties"]))


class ScriptedClient:
    """Same shape as OllamaClient.chat_completion -- no network. Always
    answers `value`/`span` regardless of prompt content."""

    def __init__(self, value, span, conf=0.9):
        self.value, self.span, self.conf = value, span, conf
        self.last_messages = None

    def chat_completion(self, messages, model=None, response_format=None, **kw):
        self.last_messages = messages
        key = _wire_key(response_format)
        body = json.dumps({key: {el.WIRE_VALUE_KEY: self.value,
                                 el.WIRE_SPAN_KEY: list(self.span),
                                 el.WIRE_CONF_KEY: self.conf}})
        return {"choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 5}}


class ExemplarReadingClient:
    """Reads the confirmed value straight out of the EXEMPLARS block, if one
    is present; otherwise returns a wrong, out-of-bounds default. This is the
    end-to-end proof that the exemplar store's output actually reaches the
    model's input, not just that the store's API returns something -- the
    prompt itself is what changes behavior."""

    BAD = 999.0  # out of barrier_pct's 0-100 bound -> gate would escalate it

    def chat_completion(self, messages, model=None, response_format=None, **kw):
        key = _wire_key(response_format)
        content = messages[-1]["content"]
        if "EXEMPLARS" in content:
            block = content.split("EXEMPLARS", 1)[1]
            import re
            m = re.search(r":\s*([\d.]+)", block)
            value = float(m.group(1)) if m else self.BAD
        else:
            value = self.BAD
        body = json.dumps({key: {el.WIRE_VALUE_KEY: value,
                                 el.WIRE_SPAN_KEY: [0, 4],
                                 el.WIRE_CONF_KEY: 0.9}})
        return {"choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 5}}


class FlakyProvider:
    """A deliberately BROKEN exemplars provider: reports the SAME version on
    every call for one (issuer, field) but returns DIFFERENT rendered content
    -- exactly the bug the guard exists to catch. A version bump is allowed
    to change the prefix (that's compounding exemplars across a session,
    working as designed); the SAME version silently rendering different text
    means the KV-cache contract ("this version == this exact prompt") has
    been violated, e.g. by a future refactor that starts interpolating
    per-document content ahead of the exemplar block."""

    def __init__(self):
        self._n = 0

    def resolve(self, issuer, field):
        self._n += 1
        key = ex.ExemplarKey(FORM, issuer, field)
        row = ex.ExemplarRow(issuer, ex.POSITIVE, f"{field}: {self._n}.0 [{self._n}%]",
                             float(self._n), "Barrier", f"{self._n}%", "acc", "doc")
        return ex.ExemplarSet(key=key, tier=ex.TIER_ISSUER, rows=(row,),
                              version="v-constant")  # version does NOT change -> bug


# --------------------------------------------------------------------------- #

def test_verdicts_land_immediately():
    section("#105 verdicts land in the exemplar store immediately (no build step)")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="s", target_n=10)
    provider = ex.ExemplarProvider(store, FORM)

    check("before any verdict, resolve() is empty (not an error)",
          provider.resolve(JPM, FIELD).rows == ())

    session.record_verdict("acc-1", "424b2.htm",
                           v.FieldVerdict.correct(FIELD, 70.0), issuer=JPM)
    session.complete_document("acc-1", "424b2.htm", issuer=JPM)

    check("immediately after ONE verdict, the provider sees it -- no separate "
          "index/build step between #105's write and #106's read",
          any(r.value == 70.0 for r in provider.resolve(JPM, FIELD).rows))


def test_form_isolation():
    section("(form, issuer, field) key: a different form never leaks in, "
            "even for a same-named field")
    store = v.ValidationStore(":memory:")
    store.write_exemplar(JPM, FIELD, "corrected", "barrier_pct: 70.0 (corrected)",
                         value=70.0, form="424B2")
    store.write_exemplar(JPM, FIELD, "corrected", "barrier_pct: -1.0 (corrected)",
                         value=-1.0, form="S-1")  # same issuer+field, OTHER form

    rows_424b2 = store.exemplar_rows("424B2", FIELD, issuer=JPM)
    rows_s1 = store.exemplar_rows("S-1", FIELD, issuer=JPM)
    check("424B2 query sees only its own form's row",
          {r["value"] for r in rows_424b2} == {70.0})
    check("S-1 query sees only its own form's row",
          {r["value"] for r in rows_s1} == {-1.0})

    provider_424b2 = ex.ExemplarProvider(store, "424B2")
    check("a provider bound to 424B2 never resolves the S-1 exemplar",
          all(r.value != -1.0 for r in provider_424b2.resolve(JPM, FIELD).rows))


def test_selection_policy():
    section("Selection policy: corrections > accepts, diversity, cap, "
            "negative survives a tight cap")
    rows = [
        ex.ExemplarRow(JPM, ex.POSITIVE, "barrier_pct: 60.0 [60%]", 60.0,
                       "Barrier", "60%", "a1", "d1"),
        ex.ExemplarRow(JPM, ex.POSITIVE, "barrier_pct: 61.0 [61%]", 61.0,
                       "Barrier", "61%", "a2", "d2"),  # same anchor+type -> collapses
        ex.ExemplarRow(JPM, ex.POSITIVE, "estimated_value: 972.30 [$972.30]",
                       "972.30", "Estimated Value", "$972.30", "a3", "d3"),  # different anchor+type
        ex.ExemplarRow(JPM, ex.CORRECTED, "barrier_pct: 70.0 [70.00%] (corrected)",
                       70.0, "Coupon Barrier", "70.00%", "a4", "d4"),
        ex.ExemplarRow(JPM, ex.NEGATIVE, "barrier_pct: absent in a prior filing",
                       None, None, None, "a5", "d5"),
    ]

    # cap=4: room for the correction, both distinct accepts, and the negative.
    picked = ex.select_exemplars(rows, cap=4)
    check("correction ranked ahead of every accept", picked[0].kind == ex.CORRECTED)
    check("negative survives", any(r.kind == ex.NEGATIVE for r in picked))
    check("a diverse accept (different anchor/value-shape) is represented, "
          "not crowded out by a same-shape near-duplicate",
          any(r.anchor == "Estimated Value" for r in picked))
    check("near-duplicate positives (same anchor+shape) collapsed to at most one",
          sum(1 for r in picked if r.kind == ex.POSITIVE and r.anchor == "Barrier") <= 1)

    # cap=3: too tight for everything -- corrections and the reserved negative
    # slot win over a same-shape duplicate accept.
    tight = ex.select_exemplars(rows, cap=3)
    check("tight cap is still enforced exactly", len(tight) <= 3)
    check("correction still wins the first slot under a tight cap",
          tight[0].kind == ex.CORRECTED)
    check("negative still survives a tight cap (reserved, not evicted by "
          "recency)", any(r.kind == ex.NEGATIVE for r in tight))

    check("cap=0 yields nothing, never errors", ex.select_exemplars(rows, cap=0) == [])
    check("no rows yields nothing, never errors", ex.select_exemplars([], cap=4) == [])


def test_fallback_chain():
    section("Fallback chain: exact issuer -> pooled (form, *, field) -> "
            "field description only -- never errors")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="s", target_n=10)
    for i, val in enumerate((70.0, 65.0, 68.0)):
        acc = f"jpm-{i}"
        session.record_verdict(acc, "424b2.htm", v.FieldVerdict.correct(FIELD, val), issuer=JPM)
        session.complete_document(acc, "424b2.htm", issuer=JPM)

    provider = ex.ExemplarProvider(store, FORM, cap=4, thin_below=2)

    jpm_set = provider.resolve(JPM, FIELD)
    check("well-supported issuer resolves from its own tier", jpm_set.tier == ex.TIER_ISSUER)

    citi_set = provider.resolve(CITI, FIELD)
    check("unseen issuer (no JPM validation at all) falls back to the pooled "
          "(form, *, field) tier without raising", citi_set.tier == ex.TIER_FORM)
    check("pooled tier is non-empty (borrows JPM's exemplars)", len(citi_set.rows) > 0)

    empty_set = provider.resolve(BOM, "some_field_never_seen")
    check("unseen issuer AND unseen field bottoms out at 'field description "
          "only' -- empty, not an error", empty_set.rows == () and empty_set.tier == ex.TIER_NONE)

    ladder = el.ExtractionLadder(SPEC, exemplars=provider,
                                 local_client=ScriptedClient(50.0, (0, 2)),
                                 local_model="qwen2.5:7b")
    r = ladder.extract(FIELD, text="Barrier: 50.00%.", issuer=BOM,
                       accession="bom-1", document="424b2.htm")
    check("extraction for an issuer with no exemplar support of its own "
          "still completes and returns a value (pooled/description-only "
          "prompt, never an error)", r is not None and r.value == 50.0)


def test_negative_retained_across_thin_merge():
    section("Thin issuer + pooled merge: a reserved negative slot survives "
            "the exact+pooled combine, not just standalone select_exemplars()")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="s", target_n=10)

    # JPM: thin support (below thin_below), but includes a correction so the
    # "exact" list is non-empty and occupies a slot ahead of the merge.
    session.record_verdict("jpm-1", "424b2.htm",
                           v.FieldVerdict.correct(FIELD, 70.0), issuer=JPM)
    session.complete_document("jpm-1", "424b2.htm", issuer=JPM)

    # CITI: enough rows to fill the pool, including a negative that must be
    # reserved a slot per the #106 selection policy.
    for i, val in enumerate((60.0, 61.0)):
        acc = f"citi-{i}"
        session.record_verdict(acc, "424b2.htm", v.FieldVerdict.correct(FIELD, val),
                               issuer=CITI)
        session.complete_document(acc, "424b2.htm", issuer=CITI)
    store.write_exemplar(CITI, FIELD, ex.NEGATIVE,
                         "barrier_pct: absent in a prior filing", value=None, form=FORM)

    provider = ex.ExemplarProvider(store, FORM, cap=4, thin_below=2)
    resolved = provider.resolve(JPM, FIELD)
    check("thin-issuer merge (exact + pooled) still reserves the negative slot",
          any(r.kind == ex.NEGATIVE for r in resolved.rows))


def test_static_prefix_ordering():
    section("Static-prefix-first ordering enforced")
    lines = ["barrier_pct: 70.0 [70.00%]  (corrected)"]

    prefix = el.static_prefix(FIELD_DEF, lines)
    static_user_part = prefix[len(el.SYSTEM_PROMPT) + 2:]

    msgs_1 = el.build_messages(FIELD_DEF, "First document's source text.", exemplars=lines)
    msgs_2 = el.build_messages(FIELD_DEF, "Second, totally different document.",
                               table_context="label: value", exemplars=lines)

    check("system message is the shared constant across both calls",
          msgs_1[0]["content"] == el.SYSTEM_PROMPT == msgs_2[0]["content"])
    check("both user messages start with the identical static prefix",
          msgs_1[1]["content"].startswith(static_user_part) and
          msgs_2[1]["content"].startswith(static_user_part))
    check("EXEMPLARS block precedes PARSED TABLE and SOURCE TEXT in the prompt",
          msgs_2[1]["content"].index("EXEMPLARS") < msgs_2[1]["content"].index("PARSED TABLE") <
          msgs_2[1]["content"].index("SOURCE TEXT"))
    check("SOURCE TEXT is always the last thing in the prompt",
          msgs_1[1]["content"].endswith("First document's source text.") and
          msgs_2[1]["content"].endswith("Second, totally different document."))

    section("Runtime guard: the ladder raises if the static prefix changes "
            "mid-run for one (issuer, field) (#106/#111)")
    flaky = FlakyProvider()
    ladder = el.ExtractionLadder(SPEC, exemplars=flaky,
                                 local_client=ScriptedClient(70.0, (0, 2)),
                                 local_model="qwen2.5:7b")
    ladder.extract(FIELD, text="doc one", issuer=JPM, accession="a1", document="d1")
    raised = False
    try:
        ladder.extract(FIELD, text="doc two", issuer=JPM, accession="a2", document="d2")
    except AssertionError:
        raised = True
    check("second call for the SAME issuer/field with a DIFFERENT exemplar "
          "set trips the guard", raised)

    # A different issuer is a different cache key -- no conflict expected.
    ladder2 = el.ExtractionLadder(SPEC, exemplars=flaky,
                                  local_client=ScriptedClient(70.0, (0, 2)),
                                  local_model="qwen2.5:7b")
    ladder2.extract(FIELD, text="doc", issuer=JPM, accession="a1", document="d1")
    no_raise = True
    try:
        ladder2.extract(FIELD, text="doc", issuer=CITI, accession="a2", document="d2")
    except AssertionError:
        no_raise = False
    check("a DIFFERENT issuer never trips the same-issuer guard", no_raise)


def test_version_in_provenance():
    section("Exemplar-set version recorded in provenance on every extraction")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="s", target_n=10)
    session.record_verdict("j1", "424b2.htm", v.FieldVerdict.correct(FIELD, 70.0), issuer=JPM)
    session.complete_document("j1", "424b2.htm", issuer=JPM)
    session.record_verdict("c1", "424b2.htm", v.FieldVerdict.correct(FIELD, 55.0), issuer=CITI)
    session.complete_document("c1", "424b2.htm", issuer=CITI)

    provider = ex.ExemplarProvider(store, FORM)
    log = el.RunLog()
    ladder = el.ExtractionLadder(
        SPEC, exemplars=provider, log=log,
        rules={(JPM, "issuer"): el.RuleMatch("JPMorgan", "rule-1", span=(0, 3))},
        local_client=ScriptedClient(70.0, (0, 2)),
        local_model="qwen2.5:7b",
    )

    ladder.extract("issuer", text="...", issuer=JPM, accession="e1", document="d1")
    rule_entry = log.entries[-1]
    check("rule rung never touches exemplars -> exemplar_set is None",
          rule_entry.provenance["exemplar_set"] is None)

    ladder.extract(FIELD, text="Barrier: 70.00%.", issuer=JPM, accession="e2", document="d2")
    jpm_entry = log.entries[-1]
    jpm_version = provider.resolve(JPM, FIELD).version
    check("local rung stamps the SPECIFIC exemplar set's version",
          jpm_entry.provenance["exemplar_set"] == jpm_version)

    ladder.extract(FIELD, text="Barrier: 55.00%.", issuer=CITI, accession="e3", document="d3")
    citi_entry = log.entries[-1]
    citi_version = provider.resolve(CITI, FIELD).version
    check("a different issuer's exemplar set stamps a different version "
          "(distinguishable in the provenance log)",
          citi_entry.provenance["exemplar_set"] == citi_version and citi_version != jpm_version)


def test_end_to_end_wiring():
    section("End-to-end: the store's exemplars actually reach the model's "
            "prompt and change its answer (not just the store's API)")
    store = v.ValidationStore(":memory:")
    session = v.ValidationSession(store, SPEC, session_id="s", target_n=10)
    provider = ex.ExemplarProvider(store, FORM)
    client = ExemplarReadingClient()
    ladder = el.ExtractionLadder(SPEC, exemplars=provider, local_client=client,
                                 local_model="qwen2.5:7b", claude_client=None)

    r0 = ladder.extract(FIELD, text="Barrier: irrelevant, client ignores it.",
                        issuer=JPM, accession="w1", document="d1")
    check("without any exemplars the client (and thus the ladder) has "
          "nothing to copy and returns the bad default",
          r0.value == ExemplarReadingClient.BAD)

    session.record_verdict("w1", "424b2.htm", v.FieldVerdict.correct(FIELD, 71.5), issuer=JPM)
    session.complete_document("w1", "424b2.htm", issuer=JPM)

    r1 = ladder.extract(FIELD, text="Barrier: irrelevant, client ignores it.",
                        issuer=JPM, accession="w2", document="d2")
    check("after ONE validated correction, the assembled prompt carries it "
          "and the SAME client now returns the confirmed value -- proof the "
          "assembly (#106), not just the storage (#105), is wired through",
          r1.value == 71.5)


if __name__ == "__main__":
    test_verdicts_land_immediately()
    test_form_isolation()
    test_selection_policy()
    test_fallback_chain()
    test_negative_retained_across_thin_merge()
    test_static_prefix_ordering()
    test_version_in_provenance()
    test_end_to_end_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\ntest_exemplars: PASS")
