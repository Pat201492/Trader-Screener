"""
Simplified rule induction tests for issue #107. Tests form dimension, tracker persistence.
"""

try:  # package import
    from . import rules as r
except ImportError:  # standalone
    import rules as r

def test_hand_seed():
    """Hand-seeded rule creation and matching."""
    mgr = r.RuleManager(agreement_threshold=2)
    rule = mgr.promote_rule_from_seed(
        "424b2", "JPM", "estimated_value_per_1000",
        anchor="Estimated value", pattern_str=r"estimated value.*?(\$?[0-9.]+)"
    )
    assert mgr.has_rule("424b2", "JPM", "estimated_value_per_1000")
    text = "Our estimated value of the notes is $972.30 per $1,000."
    match = mgr.apply_rule("424b2", "JPM", "estimated_value_per_1000", text)
    assert match is not None
    val, start, end = match
    assert "972" in val
    print("[OK] hand-seeded rule")

def test_agreement_promotes():
    """Rule promotes after N consecutive agreements."""
    mgr = r.RuleManager(agreement_threshold=2)
    mgr.promote_rule_from_seed("424b2", "JPM", "barrier_pct", "Barrier",
                                pattern_str=r"Barrier[:\s]+([0-9.]+)")

    # Two agreements = promote
    mgr.record_comparison("424b2", "JPM", "barrier_pct", 70.0, 70.0)
    promoted, _, demoted = mgr.record_comparison("424b2", "JPM", "barrier_pct", 70.0, 70.0)
    assert promoted and not demoted
    tracker = mgr.get_tracker("424b2", "JPM", "barrier_pct")
    assert tracker.status == "promoted"
    print("[OK] agreement tracking and promotion")

def test_disagreement_demotes():
    """Disagreement demotes the rule."""
    mgr = r.RuleManager(agreement_threshold=1)
    mgr.promote_rule_from_seed("424b2", "JPM", "coupon", "Coupon",
                                pattern_str=r"Coupon[:\s]+([0-9.]+)")
    mgr.record_comparison("424b2", "JPM", "coupon", 9.15, 9.15)
    tracker = mgr.get_tracker("424b2", "JPM", "coupon")
    assert tracker.status == "promoted"

    # Disagreement
    _, _, demoted = mgr.record_comparison("424b2", "JPM", "coupon", 9.15, 9.10)
    assert demoted
    assert tracker.status == "demoted"
    print("[OK] disagreement demotes")

def test_shadow_window():
    """Rule shadows after promotion for configured window."""
    mgr = r.RuleManager(agreement_threshold=1, shadow_window_days=7)
    mgr.promote_rule_from_seed("424b2", "JPM", "price", "Price",
                                pattern_str=r"Price[:\s]+([0-9.]+)")
    mgr.record_comparison("424b2", "JPM", "price", 100.0, 100.0)
    assert mgr.is_shadowing("424b2", "JPM", "price")
    print("[OK] shadow window")

def test_export_import_preserves_state():
    """Export/import preserves tracker state."""
    mgr1 = r.RuleManager(agreement_threshold=1)
    mgr1.promote_rule_from_seed("424b2", "JPM", "issue_date", "Issue Date",
                                 pattern_str=r"Issue[:\s]+([0-9-]+)")

    # Promote the rule
    mgr1.record_comparison("424b2", "JPM", "issue_date", "2024-01-15", "2024-01-15")
    tracker1 = mgr1.get_tracker("424b2", "JPM", "issue_date")
    assert tracker1.status == "promoted"

    # Export and import
    exported = mgr1.export_rules()
    assert ("424b2", "JPM", "issue_date") in exported
    data = exported[("424b2", "JPM", "issue_date")]
    assert data["status"] == "promoted"
    assert data["agreements"] == 1

    mgr2 = r.RuleManager()
    loaded = mgr2.import_rules(exported)
    assert loaded == 1
    assert mgr2.has_rule("424b2", "JPM", "issue_date")

    # Verify tracker state was restored
    tracker2 = mgr2.get_tracker("424b2", "JPM", "issue_date")
    assert tracker2.status == "promoted"
    assert tracker2.agreements == 1
    assert tracker2.first_agreement_at is not None
    print("[OK] export/import preserves tracker state")

def test_missing_match_demotes():
    """Template change (rule returns None) auto-demotes."""
    mgr = r.RuleManager(agreement_threshold=1)
    mgr.promote_rule_from_seed("424b2", "JPM", "term", "Term",
                                pattern_str=r"Term[:\s]+([0-9.]+)")
    mgr.record_comparison("424b2", "JPM", "term", 5.0, 5.0)
    tracker = mgr.get_tracker("424b2", "JPM", "term")
    assert tracker.status == "promoted"

    # Simulate template change: rule no longer matches
    _, reason, demoted = mgr.record_missing_match("424b2", "JPM", "term")
    assert demoted
    assert tracker.status == "demoted"
    assert "template change" in reason
    print("[OK] missing match demotes")

if __name__ == "__main__":
    test_hand_seed()
    test_agreement_promotes()
    test_disagreement_demotes()
    test_shadow_window()
    test_export_import_preserves_state()
    test_missing_match_demotes()
    print("\n[OK] All tests passed")
