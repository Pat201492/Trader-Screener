# EDGAR Scrubber — Rule Induction & Promotion (issue #107)

**Rule-first extraction**, not model-first: regex rules extracted from validated spans dominate throughput. The model is fallback and change-detector.

## Why Rules First?

On target hardware, local inference is the throughput bottleneck. For templated documents (424B2 filings), most target fields sit at literal fixed anchors:

- Issuer: JPMorgan Chase Financial Company LLC
- CUSIP: 48133YHT4
- Pricing/issue/maturity dates
- Denomination: $1,000
- Estimated value: $972.30
- Agent commission: 0.80%

Running a model to find text a regex finds exactly costs throughput. On 30,000 documents:

| rule coverage | throughput | keys to throughput |
|---|---|---|
| 0% | ~8 hrs | brute-force model |
| ~90% | **~1 hr** | **rules for templated fields** |

Per-run `extraction_breakdown()` reports this metric. If rule share doesn't grow with each issuer validated, the templating premise is wrong and worth knowing early.

## Order of Operations

Hand-validated exemplars (#105) produce rule seeds, which become promoted regex rules:

```
ExtractionLadder.extract():
  1. RULE     -- promoted regex for (issuer, field). Zero tokens, exact.
  2. XBRL     -- EX-107 fee exhibit already has the value (#101).
  3. LOCAL    -- local 7B with exemplars (#106), self-consistency gate.
  4. CLAUDE   -- gated remainder only.
```

## Hand-Seeded Rules from #105

The validation UI (#105) produces **rule seeds** by collecting accepted/corrected spans:

```python
from validation import ValidationSession, RuleSeed

store = ValidationSession(...)
store.record_verdict(accession, document, fv, issuer="JPM", render_doc=render_doc)

# After N documents with stable anchors:
seed = store.rule_seed_for("JPM", "barrier_pct")
if seed:
    print(f"anchor={seed.anchor!r}, support={seed.support}/{seed.total}")
    # seed.sample_spans holds a few examples
```

The validator promotes the seed → `RuleManager` creates a regex rule:

```python
from rules import RuleManager

mgr = RuleManager(agreement_threshold=3, shadow_window_days=7)
rule = mgr.promote_rule_from_seed(
    "JPM", "barrier_pct",
    anchor="Barrier Percentage",
    pattern_str=r"Barrier[:\s]+([0-9.]+)%",  # optional; derives from anchor if omitted
)
```

## Promotion Flow

A rule does not run live until it has proven stable against validated data.

### 1. Agreement Tracking

Track consecutive agreement with validated values. One disagreement resets.

```python
# In the validation loop after a value is hand-corrected:
promoted, reason, demoted = mgr.record_comparison(
    issuer="JPM", field="barrier_pct",
    validated_value=70.0,     # what the validator corrected to
    extracted_value=70.0,     # what the rule extracted
)

if promoted:
    print(f"Rule promoted: {reason}")  # "3 consecutive agreements; promoted"
if demoted:
    print(f"Rule demoted: {reason}")   # "disagreement: rule=65.0 vs validated=70.0"
```

### 2. Shadow Mode

A promoted rule keeps the model running in shadow for a window, comparing silently.

```python
# Check if the rule is still in shadow:
if mgr.is_shadowing("JPM", "barrier_pct"):
    # Run both rule and model on this document; log disagreements silently
    rule_result = mgr.apply_rule("JPM", "barrier_pct", text)
    model_result = ... # run model in parallel
    if rule_result != model_result:
        # Log as a template-change signal; flag for re-validation
        log.shadow_disagreements["barrier_pct"].append({
            "accession": ..., "document": ...,
            "rule": rule_result, "model": model_result,
        })
```

Shadow window is configurable (`shadow_window_days`); default is 7 days. After the window, the rule runs alone (no model comparison).

### 3. Template Change Detection

When a rule no longer matches or the rule/model disagree beyond threshold:

1. **Auto-demote** the rule back to shadow status
2. **Flag** the field for re-validation in #105
3. **Never silently ship wrong values**

Demotion happens immediately when:
- A previously-reliable rule returns nothing
- Rule and model disagree on `N` consecutive documents
- Model and rule diverge on value interpretation (cross-check flags, #102)

The flagged field queues back into #105's human-in-loop, where a fresh validated span can re-promote or modify the rule.

## API

### RuleManager

```python
from rules import RuleManager

mgr = RuleManager(
    agreement_threshold=3,       # N consecutive agrees before promote
    shadow_window_days=7,        # shadow duration after promotion
)

# Create and store a rule
rule = mgr.promote_rule_from_seed(
    issuer="JPM",
    field="estimated_value_per_1000",
    anchor="Estimated value of the notes",
    pattern_str=r"estimated value[:\s]+\$?([0-9.]+)",
    capture_group=1,  # which group to extract; None = whole match
)

# Check if a rule exists
if mgr.has_rule("JPM", "estimated_value_per_1000"):
    rule = mgr.get_rule("JPM", "estimated_value_per_1000")

# Apply a rule to text
match = mgr.apply_rule("JPM", "estimated_value_per_1000", document_text)
if match:
    value, start, end = match
    # span [start, end] is in the original document's coordinates

# Track comparisons during shadow mode
promoted, reason, demoted = mgr.record_comparison(
    issuer="JPM", field="barrier_pct",
    validated_value=70.0, extracted_value=70.0
)

# Check if still in shadow
in_shadow = mgr.is_shadowing("JPM", "barrier_pct")

# Persist / load
exported = mgr.export_rules()  # -> dict of {(issuer, field): {...pattern...}}
mgr2 = RuleManager()
mgr2.import_rules(exported)
```

### ExtractionLadder Integration

The ladder accepts a `rules=` parameter (dict or callable):

```python
from extraction_ladder import ExtractionLadder

ladder = ExtractionLadder(
    spec,
    rules=mgr,  # RuleManager instance acts as callable
    # or rules={(issuer, field): RuleMatch(value, rule_id, span), ...}
    local_client=...,
    claude_client=...,
)

result = ladder.extract("barrier_pct", text=doc_text, issuer="JPM",
                        accession=acc, document=doc)
# If a rule matched: result.rung == "rule", result.value is the rule's answer
# Otherwise: escalates through local → Claude as normal
```

### RunLog Extraction Breakdown

Every run reports what share of extractions came from each rung:

```python
summary = ladder.log.summary()
print(summary["extraction_breakdown"])
# {"rule": 0.87, "xbrl": 0.02, "local": 0.09, "claude": 0.02}
```

Target: `rule` should grow toward ~90% as more issuers are validated.

## Regex Pattern Authoring

A pattern should:

1. **Match the anchor** precisely (case-insensitive):
   ```python
   # Anchor: "Barrier Percentage:"
   r"Barrier Percentage[:\s]+([0-9.]+)"
   ```

2. **Capture the value** (group 1 by default):
   ```python
   # Captures "70.00" from "Barrier: 70.00% of Initial Value"
   r"Barrier[:\s]+([0-9.]+)"
   ```

3. **Be permissive on whitespace**:
   ```python
   r"Coupon Rate[:\s]*([0-9.]+)"  # matches "Coupon Rate: 9.15" or "Coupon Rate 9.15"
   ```

4. **Use value patterns, not absolute offsets**:
   ```python
   # GOOD: pattern-based (works across issuers)
   r"Estimated value[:\s]+\$?([0-9.]+)"
   
   # BAD: offset-based (breaks across filings)
   r".{450}([0-9.]+)"
   ```

Patterns are case-insensitive by default. Use `capture_group=1` (or None for whole match).

## Rules Persistence Bootstrap

Before extraction runs, rules must be loaded from persisted storage (e.g., JSON):

```python
from rules import RuleManager
import json

# Load persisted rules
with open("rules.json") as f:
    exported = json.load(f)

mgr = RuleManager(shadow_window_days=7)
loaded = mgr.import_rules(exported)
print(f"Loaded {loaded} rules for {len(set((i, f) for i, f in exported.keys()))} issuers")

# Pass to the ladder
ladder = ExtractionLadder(
    spec,
    rules=mgr,
    local_client=...,
    claude_client=...,
)

# Extract with rules active
result = ladder.extract("barrier_pct", text=doc, issuer="JPM", accession=acc, document=doc)
```

**Timing:** Rules are loaded once before batch processing begins, not per-document. A rule loaded 7 days ago but just accessed still reports `is_shadowing=True` (based on `first_agreement_at`), not current time.

## Validation Loop Integration

1. **Validator** reviews a document and marks correct/incorrect values
2. **record_verdict()** stores the verdict, derives the anchor, writes an exemplar
3. **rule_seed_for()** checks if the anchor has stabilized (N docs, majority agreement)
4. **Validator** is offered "promote this to a rule?"
5. **Promoted rule** is exported to JSON and loaded (see Bootstrap above)
6. **record_comparison()** is called during the next batch to track agreement/disagreement:
   ```python
   # After extraction and validation on document N+1:
   promoted, reason, demoted = mgr.record_comparison(
       issuer="JPM", field="barrier_pct",
       validated_value=70.0,     # what the validator confirmed
       extracted_value=70.0,     # what the rule extracted from the document
   )
   if demoted:
       # Field escalates back into validation queue for re-seeding
       flag_for_revalidation("JPM", "barrier_pct", reason)
   ```

On the next batch of documents:

- Rules run first (rung 1)
- If promoted N days ago, the model shadows in parallel
- If rule/model disagree, the field is flagged for re-validation
- If agreement holds, rule stays live after the shadow window

## Files

| File | Purpose |
|------|---------|
| **rules.py** | `RuleManager`, `RuleDefinition`, `AgreementTracker` |
| **test_rules.py** | Unit tests: hand-seeding, promotion, tracking, import/export |

## Tests

```bash
python tools/edgar_scrubber/test_rules.py
python tools/edgar_scrubber/test_extraction_ladder.py  # rung 1 integration
```

Self-checks verify:
- Rule creation and matching
- Agreement tracking (N consecutive, zero disagreements)
- Shadow window timing
- Serialization (export/import for persistence)
- Integration with validation store (#105)
- ExtractionLadder rung 1 short-circuiting the model

## References

- Issue #95 — EDGAR scrubber (parent)
- Issue #101 — document reduction + EX-107 parsing (rung 2)
- Issue #104 — extraction ladder (rungs 1-4)
- Issue #105 — validation loop that seeds rules
- Issue #106 — exemplar mining from validated spans
