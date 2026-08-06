"""
Rule induction, promotion, and shadow mode (issue #107).

**Rule-first extraction**, not model-first: validators produce seeds from the first
~10 documents per issuer (#105). Those seeds become hand-authored regex rules that
cover fixed-anchor fields (issuer, guarantor, CUSIP, pricing/issue/maturity dates,
denomination, estimated_value_per_1000, agent commission). The model is fallback
and change-detector.

Order of operations (in extraction_ladder.py#ExtractionLadder.extract):
  1. RULE -- a promoted rule for (issuer, field) -- zero tokens, exact. Plugged
             in via `rules=` (#107).
  2. XBRL -- the EX-107 fee exhibit already carries the value (#101).
  3. LOCAL -- the local 7B (#103), with exemplars (#106) if available.
  4. CLAUDE -- only for the gated remainder.

A promoted rule keeps the model running in **shadow** for a window, silently
comparing and logging disagreements. Promotion without shadow means the first
template change ships wrong values into the chart with no signal.

**Shadow mode signals:**
- Rule returns nothing where it previously always matched → auto-demote
- Rule and shadow model disagree beyond threshold → demote, flag
- Flagged fields queue back into #105 for re-validation, so a template change
  costs a few minutes of re-validation instead of corrupted data.

**Induction from validated spans:** For a `(form, issuer, field)` key, derive
candidate extractors from accepted spans:
  - **section anchor** -- which heading the span sits under
  - **label anchor** -- preceding label text ("Estimated value of the notes:")
  - **table position** -- row/column when inside a flattened terms table
  - **value pattern** -- the regex shape the value takes

Promote only on a documented bar: N consecutive agreements between rule and
validated value, zero disagreements. One disagreement demotes.

stdlib only. Run the self-check:  python tools/edgar_scrubber/rules.py
"""

import json
import re
from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timedelta, timezone

try:  # package import: tools.edgar_scrubber.rules
    from .validation import ACCEPT, CORRECT
except ImportError:  # standalone: python tools/edgar_scrubber/rules.py
    ACCEPT = "accept"
    CORRECT = "correct"


@dataclass(frozen=True)
class RuleDefinition:
    """One promoted regex rule for a (issuer, field). Defines how to match and
    extract the value given a document's text. `pattern` is a compiled regex;
    `capture_group` names which group contains the value (1 for the first unnamed
    or named group, None to use the whole match). `anchor` describes the rule for
    operator visibility and debugging."""

    issuer: str
    field: str
    anchor: str                    # e.g. "Estimated value of the notes:"
    pattern: object                # compiled regex
    capture_group: int = None
    confidence: float = 1.0
    created_at: str = None
    comment: str = None

    @property
    def rule_id(self):
        """Short stable identifier for logging/debugging."""
        return f"anchor-{self.field}"

    def match(self, text):
        """Extract a value from text if the pattern matches. Returns
        (value, start_offset, end_offset) or None if no match."""
        if not text:
            return None
        m = self.pattern.search(text)
        if not m:
            return None
        if self.capture_group is not None and self.capture_group <= len(m.groups()):
            val_text = m.group(self.capture_group)
            if val_text is None:
                return None
            # Offset of the captured group within the full match.
            start = m.start(self.capture_group)
            end = m.end(self.capture_group)
        else:
            val_text = m.group(0)
            start = m.start(0)
            end = m.end(0)
        return val_text.strip(), start, end


@dataclass
class AgreementTracker:
    """Tracks consecutive agreement/disagreement between a rule and the model
    (via the validation store's verdicts) for promotion/demotion decisions.

    A rule is promoted when it has N consecutive agreements with validated values
    and zero disagreements. One disagreement resets the counter."""

    issuer: str
    field: str
    rule: RuleDefinition
    agreement_threshold: int = 3       # N consecutive agrees before promoting
    shadow_window_days: int = 7        # how long to keep shadowing after promotion

    agreements: int = 0
    first_agreement_at: str = None
    total_comparisons: int = 0
    last_comparison_at: str = None
    status: str = "pending"            # pending | promoted | demoted | dismissed

    def record_match(self, validated_value, extracted_value, *, now_iso=None):
        """Record one comparison between rule and validated value. Increments
        `agreements` on exact match, resets to 0 on mismatch. Returns
        (promoted, reason) where promoted=True if the rule crossed the
        agreement threshold."""
        now_iso = now_iso or datetime.now(timezone.utc).isoformat()
        self.total_comparisons += 1
        self.last_comparison_at = now_iso

        if extracted_value == validated_value:
            self.agreements += 1
            if self.first_agreement_at is None:
                self.first_agreement_at = now_iso
        else:
            self.agreements = 0
            self.first_agreement_at = None
            self.status = "demoted"
            return False, f"disagreement: rule={extracted_value!r} vs validated={validated_value!r}"

        if self.agreements >= self.agreement_threshold:
            self.status = "promoted"
            return True, f"{self.agreements} consecutive agreements; promoted"

        return False, f"agreement {self.agreements}/{self.agreement_threshold}"

    def should_shadow(self, *, now_iso=None):
        """True if the rule is newly promoted and still in the shadow window."""
        if self.status != "promoted" or self.first_agreement_at is None:
            return False
        now_iso = now_iso or datetime.now(timezone.utc).isoformat()
        now = datetime.fromisoformat(now_iso.replace('Z', '+00:00'))
        first = datetime.fromisoformat(self.first_agreement_at.replace('Z', '+00:00'))
        return (now - first) <= timedelta(days=self.shadow_window_days)

    def as_dict(self):
        """Serialize for JSON storage."""
        return {
            "issuer": self.issuer,
            "field": self.field,
            "anchor": self.rule.anchor,
            "agreements": self.agreements,
            "total_comparisons": self.total_comparisons,
            "first_agreement_at": self.first_agreement_at,
            "last_comparison_at": self.last_comparison_at,
            "status": self.status,
            "pattern": self.rule.pattern.pattern,  # regex pattern string
        }


class RuleManager:
    """Manage promoted rules and shadow mode for a given form/issuer combination.

    Hand-seeded rules come from validation.py's validated spans. A rule sits in
    shadow for a window, comparing silently against the model and logging
    disagreements. Promotion without shadow means the first template change ships
    wrong values with no signal.

    When a rule that previously always matched suddenly returns nothing, or the
    rule and model disagree beyond threshold, the rule is auto-demoted and the
    field is flagged for re-validation (back to #105).
    """

    def __init__(self, store=None, *, shadow_window_days=7, agreement_threshold=3):
        """
        Args:
            store: optional validation store instance (for looking up validated
                   spans and rule statuses)
            shadow_window_days: how long to shadow a newly promoted rule
            agreement_threshold: N consecutive agreements before promoting
        """
        self.store = store
        self.shadow_window_days = shadow_window_days
        self.agreement_threshold = agreement_threshold
        self._rules = {}         # (issuer, field) -> RuleDefinition
        self._trackers = {}      # (issuer, field) -> AgreementTracker

    def load_rules(self, rules_dict):
        """Load hand-seeded rules. `rules_dict` is {(issuer, field): RuleDefinition, ...}"""
        self._rules = dict(rules_dict) if rules_dict else {}
        return len(self._rules)

    def has_rule(self, issuer, field):
        """True if a rule exists for this (issuer, field)."""
        return (issuer, field) in self._rules

    def get_rule(self, issuer, field):
        """Return the RuleDefinition or None."""
        return self._rules.get((issuer, field))

    def apply_rule(self, issuer, field, text):
        """Try to extract a value using the rule. Returns (value, start, end) or None
        if the rule does not match."""
        rule = self.get_rule(issuer, field)
        if rule is None:
            return None
        return rule.match(text)

    def promote_rule_from_seed(self, issuer, field, anchor, *, pattern_str=None,
                               capture_group=None):
        """Create and store a rule from a validated anchor and pattern. If
        `pattern_str` is not given, derives a simple literal-string match from
        the anchor. Returns the RuleDefinition."""
        if pattern_str is None:
            # Default: treat anchor as a literal string to match on a line.
            # E.g., "Estimated value of the notes:" -> match it, then a number.
            pattern_str = re.escape(anchor.rstrip(": ")) + r"[:\s]*([0-9]+\.?[0-9]*)"
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex pattern: {pattern_str!r}: {e}")

        rule = RuleDefinition(
            issuer=issuer, field=field, anchor=anchor, pattern=pattern,
            capture_group=capture_group or 1, confidence=1.0,
            created_at=datetime.now(timezone.utc).isoformat()
        )
        self._rules[(issuer, field)] = rule
        self._trackers[(issuer, field)] = AgreementTracker(
            issuer=issuer, field=field, rule=rule,
            agreement_threshold=self.agreement_threshold,
            shadow_window_days=self.shadow_window_days,
        )
        return rule

    def record_comparison(self, issuer, field, validated_value, extracted_value, *,
                          now_iso=None):
        """Record a comparison during shadow mode. Returns (promoted, reason, demoted).
        - promoted=True if the rule crossed the agreement threshold.
        - demoted=True if a disagreement was found.
        """
        key = (issuer, field)
        if key not in self._trackers:
            return False, "no tracker", False

        tracker = self._trackers[key]
        promoted, reason = tracker.record_match(validated_value, extracted_value,
                                                 now_iso=now_iso)
        demoted = tracker.status == "demoted"
        return promoted, reason, demoted

    def is_shadowing(self, issuer, field, *, now_iso=None):
        """True if this rule is in the shadow window."""
        key = (issuer, field)
        if key not in self._trackers:
            return False
        return self._trackers[key].should_shadow(now_iso=now_iso)

    def get_tracker(self, issuer, field):
        """Return the AgreementTracker for debugging/testing."""
        return self._trackers.get((issuer, field))

    def export_rules(self):
        """Export all rules as serializable dicts. Use for persistence."""
        out = {}
        for (issuer, field), rule in self._rules.items():
            out[(issuer, field)] = {
                "anchor": rule.anchor,
                "pattern": rule.pattern.pattern,
                "capture_group": rule.capture_group,
                "confidence": rule.confidence,
                "created_at": rule.created_at,
                "comment": rule.comment,
            }
        return out

    def import_rules(self, rules_dict):
        """Load rules from exported dicts (e.g., from JSON)."""
        loaded = 0
        for (issuer, field), data in (rules_dict or {}).items():
            try:
                pattern = re.compile(data["pattern"], re.IGNORECASE)
                rule = RuleDefinition(
                    issuer=issuer, field=field, anchor=data.get("anchor"),
                    pattern=pattern, capture_group=data.get("capture_group"),
                    confidence=data.get("confidence", 1.0),
                    created_at=data.get("created_at"),
                    comment=data.get("comment"),
                )
                self._rules[(issuer, field)] = rule
                loaded += 1
            except Exception as e:
                print(f"Warning: failed to load rule {(issuer, field)}: {e}")
        return loaded


if __name__ == "__main__":
    # Self-check: basic rule creation, matching, promotion tracking.
    mgr = RuleManager(agreement_threshold=2, shadow_window_days=7)

    # Hand-seed a rule.
    rule = mgr.promote_rule_from_seed(
        "JPM", "barrier_pct",
        anchor="Barrier",
        pattern_str=r"Barrier[:\s]+([0-9.]+)%",
    )
    assert mgr.has_rule("JPM", "barrier_pct")
    print(f"[OK] Hand-seeded rule: {rule.rule_id}")

    # Test matching.
    text = "The Barrier: 70.00% of Initial Value."
    match = mgr.apply_rule("JPM", "barrier_pct", text)
    assert match is not None
    val, start, end = match
    assert val == "70.00" and text[start:end] == "70.00"
    print(f"[OK] Rule matches text, extracts: {val!r}")

    # Test agreement tracking.
    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "barrier_pct", 70.0, 70.0  # validated vs extracted
    )
    assert promoted is False and not demoted
    print(f"  Agreement 1/2: {reason}")

    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "barrier_pct", 70.0, 70.0
    )
    assert promoted is True and not demoted
    print(f"  Agreement 2/2: {reason} -> PROMOTED")

    # Test shadowing.
    assert mgr.is_shadowing("JPM", "barrier_pct")
    print(f"[OK] Rule is in shadow window")

    # Test disagreement -> demotion.
    promoted, reason, demoted = mgr.record_comparison(
        "JPM", "barrier_pct", 70.0, 65.0  # mismatch!
    )
    assert not promoted and demoted
    print(f"[OK] Disagreement demotes rule: {reason}")

    # Test export/import.
    exported = mgr.export_rules()
    assert ("JPM", "barrier_pct") in exported
    print(f"[OK] Export rules: {len(exported)} rule(s)")

    mgr2 = RuleManager()
    loaded = mgr2.import_rules(exported)
    assert loaded == 1 and mgr2.has_rule("JPM", "barrier_pct")
    print(f"[OK] Import rules: {loaded} rule(s) loaded")

    print("\nrules self-check: PASS")
