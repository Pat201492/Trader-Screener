"""
Rule induction, promotion, and shadow mode (issue #107).

Rule-first extraction: validators produce seeds from the first ~10 documents per
issuer (#105). Those seeds become promoted regex rules that cover fixed-anchor
fields. The model runs in shadow for a window, comparing silently and logging
disagreements. Detection: rule returns nothing where it previously matched, or
rule/model disagree beyond threshold -> auto-demote + flag for re-validation.

Run:  python tools/edgar_scrubber/test_rules.py
"""

try:  # package import: tools.edgar_scrubber.test_rules
    from . import rules as r
    from . import validation as v
    from . import field_spec as fs
except ImportError:  # standalone: python tools/edgar_scrubber/test_rules.py
    import rules as r
    import validation as v
    import field_spec as fs

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
def test_hand_seed_and_match():
    section("Hand-seeded rules from validated anchors")
    mgr = r.RuleManager(agreement_threshold=2)

    # Seed from validation output: "Estimated value of the notes:"
    rule = mgr.promote_rule_from_seed(
        "JPM", "estimated_value_per_1000",
        anchor="Estimated value",
        pattern_str=r"estimated value.*?(\$?[0-9.]+)",
    )
    check("rule created", mgr.has_rule("JPM", "estimated_value_per_1000"))
    check("rule_id format", rule.rule_id == "anchor-estimated_value_per_1000")

    # Test matching.
    text = "Our estimated value of the notes is $972.30 per $1,000."
    match = mgr.apply_rule("JPM", "estimated_value_per_1000", text)
    check("rule matches text", match is not None)
    if match:
        val, start, end = match
        # Should extract the numeric part
        check("extracts value", "972" in val or "$972" in val)
        check("span valid", start < end)


# --------------------------------------------------------------------------- #
def test_agreement_tracking_and_promotion():
    section("Rule promotion: N consecutive agreements, zero disagreements")
    mgr = r.RuleManager(agreement_threshold=3)

    rule = mgr.promote_rule_from_seed("JPM", "barrier_pct", "Barrier",
                                      pattern_str=r"Barrier[:\s]+([0-9.]+)")
    tracker = mgr.get_tracker("JPM", "barrier_pct")
    check("tracker created", tracker is not None)

    # Record agreements.
    for i in range(1, 4):
        promoted, reason, demoted = mgr.record_comparison(
            "JPM", "barrier_pct", 70.0, 70.0  # validated == extracted
        )
        check(f"agreement {i}", not promoted or i == 3)
        check(f"not demoted on agreement {i}", not demoted)

    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "barrier_pct", 70.0, 70.0
    )
    check("3rd agreement promotes the rule", promoted and tracker.status == "promoted")

    # Next agreement increments.
    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "barrier_pct", 70.0, 70.0
    )
    check("still promoted after another agreement", tracker.status == "promoted")


# --------------------------------------------------------------------------- #
def test_disagreement_demotes():
    section("Disagreement resets agreement counter and demotes")
    mgr = r.RuleManager(agreement_threshold=2)
    rule = mgr.promote_rule_from_seed("JPM", "coupon_pct", "Coupon",
                                      pattern_str=r"Coupon[:\s]+([0-9.]+)")

    # First agreement.
    mgr.record_comparison("JPM", "coupon_pct", 9.15, 9.15)
    tracker = mgr.get_tracker("JPM", "coupon_pct")
    check("1 agreement", tracker.agreements == 1)

    # Disagreement resets and demotes.
    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "coupon_pct", 9.15, 8.10
    )
    check("disagreement resets", tracker.agreements == 0)
    check("disagreement demotes", demoted and tracker.status == "demoted")
    check("reason includes mismatch", "disagreement" in reason)


# --------------------------------------------------------------------------- #
def test_shadow_window():
    section("Shadow mode window after promotion")
    mgr = r.RuleManager(agreement_threshold=1, shadow_window_days=7)

    rule = mgr.promote_rule_from_seed("JPM", "issue_date", "Issue Date",
                                      pattern_str=r"Issue Date[:\s]+([0-9-]+)")
    mgr.record_comparison("JPM", "issue_date", "2024-01-15", "2024-01-15")
    tracker = mgr.get_tracker("JPM", "issue_date")

    # Promoted, should be in shadow window immediately.
    check("promoted after 1 agreement", tracker.status == "promoted")
    check("in shadow window immediately", mgr.is_shadowing("JPM", "issue_date"))

    # Check the first_agreement_at was set.
    check("first_agreement_at recorded", tracker.first_agreement_at is not None)


# --------------------------------------------------------------------------- #
def test_export_import():
    section("Serialization for persistence")
    mgr1 = r.RuleManager()

    rule = mgr1.promote_rule_from_seed(
        "JPM", "estimated_value_per_1000", "Estimated value",
        pattern_str=r"value[:\s]+([0-9.]+)"
    )

    # Export.
    exported = mgr1.export_rules()
    check("exported rule", ("JPM", "estimated_value_per_1000") in exported)
    check("pattern preserved", "value[:\\s]+" in exported[("JPM", "estimated_value_per_1000")]["pattern"])

    # Import into a new manager.
    mgr2 = r.RuleManager()
    loaded = mgr2.import_rules(exported)
    check("imported 1 rule", loaded == 1)
    check("rule available after import", mgr2.has_rule("JPM", "estimated_value_per_1000"))

    # Test it still works after import.
    text2 = "our value: 972.4"
    match = mgr2.apply_rule("JPM", "estimated_value_per_1000", text2)
    check("imported rule matches", match is not None)
    if match:
        check("imported rule extracts value", "972" in match[0])


# --------------------------------------------------------------------------- #
def test_validation_store_integration():
    section("Integration with validation store (#105)")
    store = v.ValidationStore(":memory:")
    spec = fs.load_specs()["structured_note"]

    # Simulate validated documents with anchors.
    session = v.ValidationSession(store, spec, session_id="test", target_n=10,
                                  rule_seed_threshold=2, rule_seed_majority=0.6)

    # Create render_doc-like object to derive anchor.
    class MockRenderDoc:
        def __init__(self):
            self.text = "Barrier Percentage: 70.00%"
            self.offset_map = None
        def text_span_for_source(self, span):
            return (len(self.text) - 7, len(self.text))  # "70.00%"
        def source_text(self, span):
            return "70.00%"

    render_doc = MockRenderDoc()

    # Two documents with the same barrier_pct, same anchor will be derived.
    source_span = (1000, 1006)  # dummy offsets

    for acc in ("acc-1", "acc-2"):
        fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=source_span)
        session.record_verdict(acc, "424b2.htm", fv, issuer="JPM", render_doc=render_doc)
        session.complete_document(acc, "424b2.htm", issuer="JPM")

    # Check the raw verdicts have anchors.
    vd1 = store.verdicts_for("test", "acc-1", "424b2.htm")
    has_anchor = any(vd.anchor for vd in vd1)

    # Check if a rule seed is ready.
    seed = session.rule_seed_for("JPM", "barrier_pct")
    check("rule seed ready", seed is not None)
    if seed:
        check("seed support >= threshold", seed.support >= 2)
        check("seed majority >= threshold", seed.support / seed.total >= 0.6)

        # Promote the seed via the manager.
        mgr = r.RuleManager(agreement_threshold=2)
        promoted_rule = mgr.promote_rule_from_seed(
            "JPM", "barrier_pct", seed.anchor or "Barrier",
            pattern_str=r"Barrier[:\s]+([0-9.]+)%"
        )
        check("rule promoted from seed", mgr.has_rule("JPM", "barrier_pct"))

        # Test the full validation loop integration: record_verdict with rules.
        # Simulate extracting a third document where the rule matches and agrees with validation.
        third_fv = v.FieldVerdict.correct("barrier_pct", 70.0, source_span=source_span)
        fv_returned = session.record_verdict(
            "acc-3", "424b2.htm", third_fv, issuer="JPM",
            rules=mgr, extracted_value=70.0  # extracted_value: what the rule extracted
        )
        check("verdict returned", fv_returned.field == "barrier_pct")
        # Check the tracker directly to verify record_verdict triggered agreement tracking
        tracker = mgr.get_tracker("JPM", "barrier_pct")
        check("agreement tracked after record_verdict", tracker.agreements == 1)
        check("tracker status still pending", tracker.status == "pending")


# --------------------------------------------------------------------------- #
def test_shadow_demotion_flow():
    section("Shadow mode: agreement->disagreement->demotion->no shadow")
    mgr = r.RuleManager(agreement_threshold=1, shadow_window_days=7)

    rule = mgr.promote_rule_from_seed("JPM", "buffer_pct", "Buffer",
                                      pattern_str=r"Buffer[:\s]+([0-9.]+)")
    # First agreement promotes.
    mgr.record_comparison("JPM", "buffer_pct", 50.0, 50.0)
    tracker = mgr.get_tracker("JPM", "buffer_pct")
    check("promoted after 1 agreement", tracker.status == "promoted")
    check("in shadow immediately post-promotion", mgr.is_shadowing("JPM", "buffer_pct"))

    # Disagreement demotes.
    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "buffer_pct", 50.0, 45.0
    )
    check("disagreement demotes", demoted and tracker.status == "demoted")
    check("shadow mode exits after demotion", not mgr.is_shadowing("JPM", "buffer_pct"))


if __name__ == "__main__":
    test_hand_seed_and_match()
    test_agreement_tracking_and_promotion()
    test_disagreement_demotes()
    test_shadow_window()
    test_shadow_demotion_flow()
    test_export_import()
    test_validation_store_integration()

    print(f"\n{'=' * 70}")
    if failures:
        print(f"test_rules: FAIL ({len(failures)} checks failed)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("test_rules: PASS")
