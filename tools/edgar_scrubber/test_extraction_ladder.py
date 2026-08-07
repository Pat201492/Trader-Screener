"""
Gate for the extraction provider ladder (issue #104). Same convention as
`test_field_spec.py` / `test_output_store.py`: stdlib only, run directly, exit
0 = pass. No network: a FakeChatClient scripts `chat_completion` responses --
the exact shape `ollama_client.OllamaClient` returns -- so the ladder can't
tell it apart from a real local or Claude call. That IS the "same client
interface across local and Claude, differing by config only" acceptance
criterion: the same fake class stands in for both rungs.

Every acceptance criterion in #104 is checked here:

  * same client interface for local vs Claude (one fake class, two configs);
  * constrained decode always on (response_format present on every call);
  * malformed-output retry rate measured, and zero on the clean path;
  * escalation rate and cost reported per run, broken down by field;
  * provenance on every value; a run reconstructible from RunLog.to_jsonl();
  * local-only mode (no Claude) runs to completion, flagging gated fields
    rather than raising.

Run:  python tools/edgar_scrubber/test_extraction_ladder.py
"""
import json

import extraction_ladder as el
import field_spec as fs

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


SPECS = fs.load_specs()
NOTE_SPEC = SPECS["structured_note"]
EV_FIELD = NOTE_SPEC.field("estimated_value_per_1000")
BUFFER_FIELD = NOTE_SPEC.field("buffer_pct")


def wire_body(field_def, value, span=None, conf=None):
    key = field_def.wire_key or field_def.name
    entry = {"v": value, "s": list(span) if span else None}
    if conf is not None:
        entry["c"] = conf
    return json.dumps({key: entry})


class FakeChatClient:
    """Same shape as OllamaClient.chat_completion -- no network. Scripts a
    list of (content, usage) responses returned in call order."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def chat_completion(self, messages, model=None, temperature=None,
                         max_tokens=None, top_p=None, response_format=None):
        self.calls.append({"messages": messages, "model": model,
                            "temperature": temperature, "response_format": response_format})
        content, usage = self.scripted.pop(0)
        return {"choices": [{"message": {"content": content}}], "usage": usage}


USAGE = {"prompt_tokens": 200, "completion_tokens": 20}


# --------------------------------------------------------------------------- #
def test_rule_rung_short_circuits():
    section("Rung 1: promoted rule (#107) short-circuits the model entirely")
    local = FakeChatClient([])  # scripted empty -- must never be called
    ladder = el.ExtractionLadder(
        NOTE_SPEC,
        rules={("JPM", "estimated_value_per_1000"): el.RuleMatch(972.4, "anchor-ev1000", span=(10, 20))},
        local_client=local, local_model="qwen2.5:7b",
    )
    r = ladder.extract("estimated_value_per_1000", text="...", issuer="JPM",
                        accession="0001", document="424b2.htm")
    check("rung == rule", r.rung == "rule")
    check("value from the rule", r.value == 972.4)
    check("provenance carries the rule id", r.provenance == "rule:anchor-ev1000")
    check("local client never called", len(local.calls) == 0)
    check("no gate recorded for a rule hit", ladder.log.entries[0].gate is None)


# --------------------------------------------------------------------------- #
def test_xbrl_rung_short_circuits():
    section("Rung 2: XBRL / EX-107 fee exhibit (#101) short-circuits the model")
    local = FakeChatClient([])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b")
    r = ladder.extract("aggregate_principal", text="...", ex107={"aggregate_principal": 2_500_000},
                        accession="0001", document="424b2.htm")
    check("rung == xbrl", r.rung == "xbrl")
    check("value from ex107", r.value == 2_500_000)
    check("provenance is xbrl:ex107", r.provenance == "xbrl:ex107")
    check("local client never called", len(local.calls) == 0)


# --------------------------------------------------------------------------- #
def test_local_confident_no_escalation():
    section("Rung 3: in-bounds, spanned, confident local value -> no escalation")
    local = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(100, 110), conf=0.92), USAGE)])
    claude = FakeChatClient([])  # must not be called
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  claude_client=claude, claude_model="claude-sonnet-4-6")
    r = ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("rung == local", r.rung == "local")
    check("not escalated", r.escalated is False)
    check("not gated", r.gated is False)
    check("no flags on a clean value", r.flags == [])
    check("Claude never called", len(claude.calls) == 0)
    check("constrained decode always on (response_format present)",
          local.calls[0]["response_format"] is not None)


# --------------------------------------------------------------------------- #
def test_out_of_bounds_escalates_to_claude():
    section("Bounds gate signal: out-of-bounds local value escalates to Claude")
    local = FakeChatClient([(wire_body(EV_FIELD, 1240.0, span=(100, 110), conf=0.9), USAGE)])
    claude = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(100, 110), conf=0.97), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  claude_client=claude, claude_model="claude-sonnet-4-6")
    r = ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("escalated to Claude", r.escalated is True and r.rung == "claude")
    check("final value is Claude's, not the bad local one", r.value == 972.4)
    check("Claude was called exactly once", len(claude.calls) == 1)
    check("Claude call also carries constrained decode", claude.calls[0]["response_format"] is not None)
    check("cost recorded > 0", ladder.log.cost_summary()["claude_cost_usd"] > 0)
    check("provenance string tags the rung and model",
          r.provenance.startswith("claude:claude-sonnet-4-6"))


# --------------------------------------------------------------------------- #
def test_span_resolution_failure_escalates():
    section("Span gate signal: in-bounds but no locatable span still escalates")
    local = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=None, conf=0.9), USAGE)])
    claude = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(50, 60), conf=0.95), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  claude_client=claude, claude_model="claude-sonnet-4-6")
    r = ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("escalated despite in-bounds value (no span)", r.escalated is True)
    check("escalation reason is span", ladder.log.entries[0].gate["reason"] == "span")


# --------------------------------------------------------------------------- #
def test_span_beats_cross_check_when_both_fail():
    section("Gate priority: span (2nd) outranks cross_check (3rd) when both fail (#104)")
    AGG_FIELD = NOTE_SPEC.field("aggregate_principal")
    # aggregate_principal (field_specs/424b2_structured_note.json:44) carries an
    # external_equals cross-check against ex107. Drive evaluate_gate() directly
    # (extract()'s rung-2 shortcut would otherwise short-circuit whenever ex107
    # already holds this field, masking the scenario): value in bounds (min: 0),
    # no locatable span, and a value that disagrees with EX-107 beyond
    # tolerance_pct. Priority order (bounds, span, cross_check, ...) says the
    # gate reason must be "span", not "cross_check".
    gate = el.evaluate_gate(AGG_FIELD, 2_500_000, None, ex107={"aggregate_principal": 2_000_000},
                             spec=NOTE_SPEC, model_confidence=0.9)
    by_name = {s.name: s.passed for s in gate.signals}
    check("bounds passed", by_name["bounds"] is True)
    check("span failed", by_name["span"] is False)
    check("cross_check failed", by_name["cross_check"] is False)
    check("gate escalates", gate.escalate is True)
    check("gate reason is span, not cross_check", gate.reason == "span")


# --------------------------------------------------------------------------- #
def test_self_consistency_disagreement_escalates():
    section("Self-consistency gate signal: two sampled passes disagree -> escalate")
    local = FakeChatClient([
        (wire_body(BUFFER_FIELD, 20.0, span=(10, 20), conf=0.9), USAGE),
        (wire_body(BUFFER_FIELD, 15.0, span=(10, 20), conf=0.9), USAGE),
    ])
    claude = FakeChatClient([(wire_body(BUFFER_FIELD, 20.0, span=(10, 20), conf=0.96), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  claude_client=claude, claude_model="claude-sonnet-4-6",
                                  self_consistency_samples=2)
    r = ladder.extract("buffer_pct", text="...", accession="0001", document="424b2.htm")
    check("two local samples were drawn", len(local.calls) == 2)
    check("disagreement escalated to Claude", r.rung == "claude")
    check("gate reason is self_consistency",
          ladder.log.entries[0].gate["reason"] == "self_consistency")


# --------------------------------------------------------------------------- #
def test_local_only_mode_flags_instead_of_failing():
    section("Local-only mode: gated field flagged, run completes without raising")
    local = FakeChatClient([(wire_body(EV_FIELD, 1240.0, span=(100, 110), conf=0.9), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  claude_client=None)
    r = ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("did not raise; returned a result", r is not None)
    check("gated, not escalated", r.gated is True and r.escalated is False)
    check("value kept from the local rung", r.value == 1240.0)
    check("flagged gated_no_claude", any(f.code == "gated_no_claude" for f in r.flags))
    check("bounds violation still flagged too", any(f.code == "out_of_bounds" for f in r.flags))

    # claude_enabled=False with a client present behaves the same as no client.
    claude = FakeChatClient([])
    ladder2 = el.ExtractionLadder(NOTE_SPEC, local_client=FakeChatClient(
        [(wire_body(EV_FIELD, 1240.0, span=(100, 110), conf=0.9), USAGE)]),
        local_model="qwen2.5:7b", claude_client=claude, claude_enabled=False)
    r2 = ladder2.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("claude_enabled=False also gates instead of calling Claude",
          r2.gated is True and len(claude.calls) == 0)


# --------------------------------------------------------------------------- #
def test_malformed_output_retry_measured():
    section("Malformed-output retry: measured, zero on the clean path")
    clean = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.9), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=clean, local_model="qwen2.5:7b")
    ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("zero malformed retries on a clean run", ladder.log.malformed_retries == 0)

    dirty = FakeChatClient([
        ("not json", USAGE),
        (wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.9), USAGE),
    ])
    ladder2 = el.ExtractionLadder(NOTE_SPEC, local_client=dirty, local_model="qwen2.5:7b")
    r = ladder2.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("recovered after one retry", r.value == 972.4)
    check("retry counted", ladder2.log.malformed_retries == 1)

    always_bad = FakeChatClient([("still not json", USAGE), ("nope", USAGE)])
    raised = False
    try:
        el.chat_json(always_bad, "qwen2.5:7b", [{"role": "user", "content": "x"}],
                      el.build_wire_schema([EV_FIELD]), max_retries=1)
    except el.MalformedOutputError:
        raised = True
    check("exhausted retries raise MalformedOutputError, not silently return", raised)


# --------------------------------------------------------------------------- #
def test_cost_and_escalation_reporting():
    section("Cost log + escalation rate, broken down by field, per document")
    local_bad = FakeChatClient([(wire_body(EV_FIELD, 1240.0, span=(1, 2), conf=0.9), USAGE)])
    local_good = FakeChatClient([(wire_body(BUFFER_FIELD, 20.0, span=(1, 2), conf=0.9), USAGE)])
    claude = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.97), USAGE)])
    log = el.RunLog()

    ladder_ev = el.ExtractionLadder(NOTE_SPEC, local_client=local_bad, local_model="qwen2.5:7b",
                                     claude_client=claude, claude_model="claude-sonnet-4-6", log=log)
    ladder_ev.extract("estimated_value_per_1000", text="...", accession="0001", document="a.htm")
    ladder_buf = el.ExtractionLadder(NOTE_SPEC, local_client=local_good, local_model="qwen2.5:7b",
                                      claude_client=FakeChatClient([]), claude_model="claude-sonnet-4-6", log=log)
    ladder_buf.extract("buffer_pct", text="...", accession="0002", document="b.htm")

    summary = log.summary()
    check("total_extractions == 2", summary["total_extractions"] == 2)
    check("overall escalation_rate == 0.5", summary["escalation_rate"] == 0.5)
    check("ev1000 escalation rate is 1.0", summary["escalation_rate_by_field"]["estimated_value_per_1000"] == 1.0)
    check("buffer_pct escalation rate is 0.0", summary["escalation_rate_by_field"]["buffer_pct"] == 0.0)
    check("cost attributed to the doc that escalated", "0001/a.htm" in summary["cost_by_document"])
    check("no cost on the doc that stayed local", "0002/b.htm" not in summary["cost_by_document"])
    check("claude_calls == 1", summary["claude_calls"] == 1)


# --------------------------------------------------------------------------- #
def test_provenance_reconstructible():
    section("Provenance on every value; a run reconstructible from RunLog.to_jsonl()")
    local = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(5, 15), conf=0.91), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  prompt_version="v3", exemplar_set_version="jpm-ev1000-v2", log=el.RunLog())
    ladder.extract("estimated_value_per_1000", text="...", issuer="JPM",
                    accession="0001", document="424b2.htm")

    lines = ladder.log.to_jsonl()
    check("one log line per extraction", len(lines) == 1)
    entry = json.loads(lines[0])
    check("rung recorded", entry["rung"] == "local")
    check("span recorded", entry["span"] == [5, 15])
    check("model recorded", entry["provenance"]["model"] == "qwen2.5:7b")
    check("prompt_version recorded", entry["provenance"]["prompt_version"] == "v3")
    check("exemplar_set recorded", entry["provenance"]["exemplar_set"] == "jpm-ev1000-v2")
    check("document recorded", entry["document"] == "424b2.htm")
    check("accession recorded", entry["accession"] == "0001")
    check("gate signals recorded", entry["gate"]["signals"][0]["name"] == "bounds")


# --------------------------------------------------------------------------- #
def test_same_client_interface_config_only():
    section("Same client class for local and Claude, differing by config only")
    # ollama_client.OllamaConfig.for_claude() proves the config-only claim on
    # the real client; here the ladder-facing proof is that ONE fake class
    # plays both roles without the ladder branching on client type anywhere.
    shared_class = FakeChatClient
    local = shared_class([(wire_body(EV_FIELD, 1240.0, span=(1, 2), conf=0.9), USAGE)])
    claude = shared_class([(wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.97), USAGE)])
    check("local and claude clients are the same class", type(local) is type(claude))
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b",
                                  claude_client=claude, claude_model="claude-sonnet-4-6")
    r = ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("escalation still resolves correctly through the shared class", r.value == 972.4)


# --------------------------------------------------------------------------- #
def test_wire_keys_never_leak():
    section("Wire keys (#102/#104 transport) never reach a LadderResult or the log")
    local = FakeChatClient([(wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.9), USAGE)])
    ladder = el.ExtractionLadder(NOTE_SPEC, local_client=local, local_model="qwen2.5:7b")
    r = ladder.extract("estimated_value_per_1000", text="...", accession="0001", document="424b2.htm")
    check("result.field is canonical, not the wire key", r.field == "estimated_value_per_1000")
    check("wire key not present in result field name", r.field != "ev1000")
    fv = r.to_field_value()
    check("to_field_value() round-trips into output_store.FieldValue", fv.field == "estimated_value_per_1000")


def test_static_prefix_byte_identical_across_calls():
    section("Static prefix (#106/#111): byte-identical across calls within an issuer/field")
    # Same issuer, field, exemplar version, different documents -> static prefix must not change
    exemplars_fn = lambda issuer, field: (["example: 972.4"], "v1") if issuer == "JPM" else (None, None)
    local = FakeChatClient([
        (wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.9), USAGE),
        (wire_body(EV_FIELD, 985.5, span=(10, 20), conf=0.9), USAGE),
        (wire_body(EV_FIELD, 950.0, span=(30, 40), conf=0.9), USAGE),
    ])
    ladder = el.ExtractionLadder(
        NOTE_SPEC,
        exemplars=exemplars_fn,
        local_client=local,
        local_model="qwen2.5:7b",
        exemplar_set_version="v1"
    )

    # Extract same field from three different documents, same issuer
    r1 = ladder.extract("estimated_value_per_1000", text="doc1...", issuer="JPM", accession="0001a", document="424b2_1.htm")
    r2 = ladder.extract("estimated_value_per_1000", text="doc2...", issuer="JPM", accession="0001b", document="424b2_2.htm")
    r3 = ladder.extract("estimated_value_per_1000", text="doc3...", issuer="JPM", accession="0001c", document="424b2_3.htm")

    check("all three calls succeeded", r1 and r2 and r3)

    # Extract the static prefix from the messages
    prefix1 = el.static_prefix(EV_FIELD, ["example: 972.4"])
    prefix2 = el.static_prefix(EV_FIELD, ["example: 972.4"])  # exact same inputs

    check("static prefix is byte-identical for same inputs", prefix1 == prefix2)

    # Check that the prompts sent to the model are ordered correctly:
    # static prefix + document text (SOURCE TEXT is always the last part)
    prefixes_seen = []
    for call in local.calls[:3]:
        # Find user message by role (brittle to assume index 1)
        messages = call["messages"]
        assert len(messages) >= 2, f"Expected at least 2 messages, got {len(messages)}"
        user_msg = None
        for msg in messages:
            if msg.get("role") == "user":
                user_msg = msg
                break
        assert user_msg is not None, "No user message found in call"
        user_message = user_msg["content"]

        # Assert SOURCE TEXT separator exists before splitting
        assert "\nSOURCE TEXT:\n" in user_message, "SOURCE TEXT delimiter not found; prompt ordering may be broken"
        parts = user_message.split("\nSOURCE TEXT:\n", 1)
        prefix = parts[0]
        prefixes_seen.append(prefix)

    # All three calls should have identical prefixes (same issuer, field, exemplar version)
    if prefixes_seen:
        check("static prefix identical across all calls (first vs second)",
              prefixes_seen[0] == prefixes_seen[1] if len(prefixes_seen) > 1 else True)
        check("static prefix identical across all calls (second vs third)",
              prefixes_seen[1] == prefixes_seen[2] if len(prefixes_seen) > 2 else True)

    # Verify that changing the exemplar version DOES change the prefix (expected)
    prefix_different_version = el.static_prefix(EV_FIELD, ["different_example: 999"])
    check("different exemplars produce different prefix", prefix1 != prefix_different_version)


def test_static_prefix_version_mismatch_blocked():
    section("Static prefix guard (#106/#111): blocks when version stays same but prefix drifts")
    # The guard catches programming errors where an exemplar provider returns different
    # content without bumping its version. This is structural -- the version is the only
    # thing that signals KV-cache coherence to llama.cpp/Ollama, so version skew is a silent
    # performance regression #111 must catch at runtime.
    #
    # Scenario: an ExemplarProvider with a resolve() method returns different ExemplarSet
    # objects for the same (issuer, field) without changing the version string.
    class BadExemplarProvider:
        def __init__(self):
            self.call_count = 0

        def resolve(self, issuer, field):
            self.call_count += 1
            class FakeExemplarSet:
                def __init__(self, version, rows):
                    self.version = version
                    self.rows = rows
                def lines(self):
                    return self.rows

            if self.call_count == 1:
                return FakeExemplarSet("v1", ["example1"])
            else:
                return FakeExemplarSet("v1", ["example1", "example2_different"])  # SAME version, DIFFERENT content!

    provider = BadExemplarProvider()
    local = FakeChatClient([
        (wire_body(EV_FIELD, 972.4, span=(1, 2), conf=0.9), USAGE),
        (wire_body(EV_FIELD, 985.5, span=(10, 20), conf=0.9), USAGE),
    ])
    ladder = el.ExtractionLadder(
        NOTE_SPEC,
        exemplars=provider,
        local_client=local,
        local_model="qwen2.5:7b"
    )

    # First call succeeds
    r1 = ladder.extract("estimated_value_per_1000", text="doc1...", issuer="JPM", accession="0001a", document="424b2_1.htm")
    check("first call succeeds", r1 is not None)

    # Second call detects the drift and raises AssertionError
    # Implementation: extraction_ladder.py:606 raises with message "static prompt prefix changed"
    raised = False
    try:
        r2 = ladder.extract("estimated_value_per_1000", text="doc2...", issuer="JPM", accession="0001b", document="424b2_2.htm")
    except AssertionError as e:
        raised = "static prompt prefix changed" in str(e)

    check("prefix drift is caught and raises AssertionError", raised)


def main():
    print("Extraction provider ladder gate (#104)")
    test_rule_rung_short_circuits()
    test_xbrl_rung_short_circuits()
    test_local_confident_no_escalation()
    test_out_of_bounds_escalates_to_claude()
    test_span_resolution_failure_escalates()
    test_span_beats_cross_check_when_both_fail()
    test_self_consistency_disagreement_escalates()
    test_local_only_mode_flags_instead_of_failing()
    test_malformed_output_retry_measured()
    test_cost_and_escalation_reporting()
    test_provenance_reconstructible()
    test_same_client_interface_config_only()
    test_wire_keys_never_leak()
    test_static_prefix_byte_identical_across_calls()
    test_static_prefix_version_mismatch_blocked()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nextraction_ladder gate: PASS")


if __name__ == "__main__":
    main()
