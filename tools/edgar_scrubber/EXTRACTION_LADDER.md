# EDGAR Scrubber — Extraction Provider Ladder (issue #104)

"Worse case Claude" as an explicit, measured ladder instead of a manual fallback
someone remembers to invoke.

> Part of #95. Blocked on #103 (local model runtime). Depends on #102 (field spec —
> bounds are the gate's primary signal).

## Four rungs, cheapest first

| # | Rung | Cost | Source |
|---|------|------|--------|
| 1 | **Rule** | zero tokens, exact | a promoted `(issuer, field)` rule (#107) |
| 2 | **XBRL / EX-107** | zero tokens | the fee exhibit already carries the value (#101) |
| 3 | **Local 7B** | local compute | Ollama + Qwen2.5-7B (#103), with exemplars (#106) |
| 4 | **Claude** | $ | only for the gated remainder |

No 14B tier: on a 12GB card 14B can't batch (#103), so it buys little over 7B while
costing ~5x throughput. The step up from 7B is Claude.

```python
from tools.edgar_scrubber import ExtractionLadder, OllamaClient, OllamaConfig

ladder = ExtractionLadder(
    spec,
    local_client=OllamaClient(local_config),          # #103
    local_model="qwen2.5:7b-instruct-q4_K_M",
    claude_client=OllamaClient(OllamaConfig.for_claude()),  # same client class
    claude_model="claude-sonnet-4-6",
)
result = ladder.extract("estimated_value_per_1000", text=doc_text, issuer="JPM",
                         accession=accession, document=doc_name)
store.record(run_id, accession, doc_name, result.to_field_value())  # -> #109
```

## Same client, different config

Rungs 3 and 4 are both `OllamaClient.chat_completion` calls. `OllamaConfig.for_claude()`
points `base_url` at Anthropic's OpenAI-compatible endpoint and carries an API key —
that is the *entire* difference. There is no separate Claude client class, so
escalation can never silently diverge in request/response shape from the local path.

## Constrained decoding, always on

Every rung-3/4 call sends `response_format` — an Ollama structured-output / GBNF
JSON schema built by `build_wire_schema()` from the field's spec. Three savings:

- No preamble tokens.
- No retries on malformed JSON in steady state — `chat_json()` still has a
  one-retry backstop, but the acceptance bar is that its retry counter measures
  **zero**, not that the retry loop is doing real work.
- `"respond only in JSON"` is deleted from every prompt (#111) — the schema makes
  it structural.

The wire payload is short keys, not canonical names (`ev1000`, not
`estimated_value_per_1000` — #102's `wire_key`), and **offsets rather than quoted
spans**: `"s": [4821, 4834]` is ~5 tokens against ~50 for the quoted text, and
offsets are already required by #105 and #107.

## The gate is checkable signals, not self-report

A 7B's self-reported confidence is worse than a larger model's, and either one
alone is close to worthless. `evaluate_gate()` checks, in priority order:

1. **Field-spec bound violations** (#102) — `estimated_value_per_1000 = 4200` is
   definitionally wrong; no judgment needed.
2. **Span resolution failure** — no locatable source span.
3. **Cross-check disagreement** — the prose value vs the EX-107 XBRL value.
4. **Self-consistency** — two sampled passes (`self_consistency_samples=2`)
   disagree.
5. **Model-reported confidence** — weighted **last**: it only decides `reason`
   when nothing else already did.

Any failing signal escalates rung 3 → rung 4. `GateResult.reason` names the
highest-priority signal that failed, so "why did this escalate" never needs
re-deriving from logs.

## Local-only mode never fails a document

If `claude_client` is `None` (or `claude_enabled=False`), a gated field does **not**
raise. It keeps the local value, and `_finish()` appends a `gated_no_claude` flag
(`field_spec.Flag`) alongside whatever bounds/cross-check flags already fired. The
run completes; `RunLog.gated_fields()` lists what needs a human look or a warmer
Claude budget.

## Provenance on every value

`Provenance` carries rung, document, span, prompt/exemplar-set version, model, and
token cost. `Provenance.as_string()` is the compact form
`output_store.FieldValue.provenance` already uses (`"rule:anchor-ev1000"`,
`"local:qwen2.5:7b@prompt=v3@exemplars=jpm-ev1000-v2"`); `Provenance.as_dict()` is
the full record kept in the run log.

## Cost log

`RunLog` collects one `LogEntry` per `ExtractionLadder.extract()` call:

- `RunLog.summary()` — total extractions, rung counts, overall escalation rate,
  **escalation rate by field** (the tool's health metric: a field escalating on
  most documents after exemplars are warm means its spec or routing is wrong —
  fix it in #102/#101, not with more Claude calls), Claude cost/tokens, cost
  **by document**, and the malformed-output retry count.
- `RunLog.to_jsonl()` — one JSON line per extraction. A run is fully
  reconstructible from this: rerun the same rungs against the same spans and
  compare "why did this number change between runs" directly.

## Files

| File | Purpose |
|---|---|
| `extraction_ladder.py` | the four rungs, the gate, constrained decode, provenance + cost log |
| `test_extraction_ladder.py` | stdlib gate — run directly, exit 0 = pass, no network (`FakeChatClient`) |
| `field_spec.py` | `check_value()` — the single-field bounds/cross-check probe the gate calls |
| `ollama_client.py` | `OllamaConfig.for_claude()` + `response_format` passthrough |
| `output_store.py` | `LadderResult.to_field_value()` bridges into the store's record shape (#109) |

Run the gate:

```bash
python tools/edgar_scrubber/test_extraction_ladder.py
```

## References

- Issue #95 — EDGAR scrubber (parent)
- Issue #103 — local model runtime (rung 3; blocks this issue)
- Issue #102 — field spec (bounds, wire keys, `check_value()`)
- Issue #101 — EX-107 fee exhibit parse (rung 2; table pre-parse for rung 3's prompts)
- Issue #105 / #107 — exemplar mining / rule promotion (rung 1's `rules=`, and the
  offset convention this module's wire format follows)
- Issue #106 — per-issuer exemplars (plugged in via `exemplars=`)
- Issue #111 — prompt minimization enabled by constrained decode
- Issue #109 — output store (`LadderResult.to_field_value()`)
- Anthropic OpenAI-SDK compatibility: https://docs.anthropic.com/en/api/openai-sdk
