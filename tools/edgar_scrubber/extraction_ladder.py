"""
Extraction provider ladder (issue #104) -- "worse case Claude" as an explicit,
measured ladder instead of a manual fallback someone remembers to invoke.

Four rungs, cheapest first:

  1. RULE   -- a promoted rule for this (issuer, field) (#107). Zero tokens,
               exact. Plugged in via `rules`; #107 owns what fills it.
  2. XBRL   -- the EX-107 fee exhibit already carries the value (#101). Zero
               tokens. Plugged in via `ex107`.
  3. LOCAL  -- the local 7B (#103), with (issuer, field) exemplars (#106) if
               available. Handles the residual.
  4. CLAUDE -- only for the gated remainder.

Local and Claude are called through the exact same client
(`ollama_client.OllamaClient.chat_completion`); escalation is a config swap
(`OllamaConfig.for_claude()`), not a second code path -- see ollama_client.py.

Confidence is gated on CHECKABLE signals, not the model's self-report:
field-spec bound/cross-check violations (#102), an unresolved source span,
disagreement across two sampled passes, and self-reported confidence weighted
last (see `evaluate_gate`).

Constrained decoding (Ollama structured outputs / GBNF grammar) is always on
for rungs 3 and 4: `build_wire_schema` turns a field's short wire key (#102)
into a JSON schema requesting `{v, s, c}` -- value, source-text OFFSETS
`[start, end]` (not quoted spans -- an offset pair is ~5 tokens against ~50
for the quoted text, and #105/#107 already need offsets), and the model's own
confidence, weighted last in the gate above.

Every value's provenance (rung, document, span, prompt/exemplar-set version,
model, token cost) and the run's cost/escalation stats are collected in a
`RunLog` so a run is reconstructible from its log and escalation rate by field
-- the tool's health metric -- is reported, not just total spend.

Local-only mode (no `claude_client`, or `claude_enabled=False`) never raises
for a gated field: it returns the local value flagged `gated_no_claude` and
keeps going, per #104's acceptance.

stdlib only. Run the self-check:  python tools/edgar_scrubber/extraction_ladder.py
"""

import json
from dataclasses import asdict, dataclass, field as _dc_field

try:  # package import: tools.edgar_scrubber.extraction_ladder
    from .field_spec import Flag
    from .output_store import FieldValue
except ImportError:  # standalone: python tools/edgar_scrubber/extraction_ladder.py
    from field_spec import Flag
    from output_store import FieldValue


class MalformedOutputError(RuntimeError):
    """Constrained decode should make this impossible; it exists as the
    backstop the #104 acceptance criterion measures ("retry rate at zero"),
    not as an expected code path."""


RUNGS = ("rule", "xbrl", "local", "claude")


# ── Rungs 1 & 2: rule / XBRL plug points ────────────────────────────────────
#
# #107 (rule promotion) and #101 (EX-107 parsing) are separate issues; this
# module only needs a place to plug their output in, so both are accepted as
# either a plain dict or a callable -- whatever the caller has on hand today.

@dataclass(frozen=True)
class RuleMatch:
    """One promoted rule's answer for a (issuer, field) (#107)."""

    value: object
    rule_id: str
    span: tuple = None
    confidence: float = 1.0


def _lookup_rule(rules, form, issuer, field_name):
    if rules is None:
        return None
    if callable(rules) and not isinstance(rules, dict):
        return rules(form, issuer, field_name)
    return rules.get((form, issuer, field_name))


def _lookup_exemplars(exemplars, form, issuer, field_name):
    if not exemplars:
        return None
    if callable(exemplars) and not isinstance(exemplars, dict):
        return exemplars(form, issuer, field_name)
    return exemplars.get((form, issuer, field_name))


# ── Constrained decode: wire schema + prompt ────────────────────────────────

_WIRE_TYPE_MAP = {
    "string": "string", "date": "string", "enum": "string",
    "number": "number", "percent": "number",
    "boolean": "boolean",
}


def _wire_key(f):
    return f.wire_key or f.name


def build_wire_schema(fields):
    """JSON schema for structured-output constrained decoding: one property per
    field's wire key, each `{v: <value>, s: [start,end]|null, c: confidence}`.
    Offsets, not quoted spans -- `[4821,4834]` is ~5 tokens against ~50 for the
    text itself (#104), and #105/#107 need offsets anyway.
    """
    props = {}
    for f in fields:
        if f.type == "array":
            item_t = _WIRE_TYPE_MAP.get(f.item_type, "string")
            v_schema = {"type": "array", "items": {"type": item_t}}
        else:
            v_schema = {"type": _WIRE_TYPE_MAP.get(f.type, "string")}
            if f.type == "enum" and f.enum:
                v_schema["enum"] = list(f.enum)
        props[_wire_key(f)] = {
            "type": "object",
            "properties": {
                "v": v_schema,
                "s": {"type": ["array", "null"], "items": {"type": "integer"},
                      "minItems": 2, "maxItems": 2},
                "c": {"type": "number"},
            },
            "required": ["v", "s"],
        }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "edgar_extraction",
            "schema": {"type": "object", "properties": props, "required": list(props)},
            "strict": True,
        },
    }


def build_messages(field_def, text, *, table_context=None, exemplars=None):
    """No "respond only in JSON" -- constrained decode makes it structural, so
    that instruction is deleted from the prompt entirely (#111)."""
    system = (
        "You are an SEC EDGAR extraction engine. Extract exactly the requested "
        "field from SOURCE TEXT. `s` must be the [start, end] character offset "
        "pair into SOURCE TEXT for the span that supports the value, or null if "
        "you cannot locate one -- never guess a span. `c` is your confidence in "
        "the value, 0 to 1."
    )
    parts = [f"FIELD: {field_def.name} ({field_def.type})"]
    if field_def.description:
        parts.append(f"DESCRIPTION: {field_def.description}")
    if field_def.anchors:
        parts.append(f"ANCHORS: {', '.join(field_def.anchors)}")
    if field_def.enum:
        parts.append(f"ALLOWED VALUES: {', '.join(field_def.enum)}")
    if field_def.bounds:
        parts.append(f"BOUNDS: {field_def.bounds}")
    if table_context:
        parts.append(f"PARSED TABLE, label: value pairs (#101):\n{table_context}")
    if exemplars:
        parts.append("EXEMPLARS for this issuer/field (#106):\n" +
                      "\n".join(f"- {e}" for e in exemplars))
    parts.append(f"SOURCE TEXT:\n{text}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def parse_ladder_response(fields, payload):
    """wire-keyed `{v, s, c}` payload -> {canonical_name: (value, span, conf)}."""
    by_key = {_wire_key(f): f for f in fields}
    out = {}
    if not isinstance(payload, dict):
        return out
    for key, entry in payload.items():
        f = by_key.get(key)
        if f is None:
            continue
        if isinstance(entry, dict):
            value, s, conf = entry.get("v"), entry.get("s"), entry.get("c")
        else:
            value, s, conf = entry, None, None
        span = tuple(s) if isinstance(s, (list, tuple)) and len(s) == 2 else None
        out[f.name] = (value, span, conf)
    return out


def chat_json(client, model, messages, response_format, *, temperature=0.0,
              max_tokens=512, max_retries=1):
    """Call `client.chat_completion` with constrained decode always on,
    returning `(payload, usage, retries)`. Constrained decode should make
    malformed JSON impossible; `max_retries` is the backstop #104's acceptance
    criterion measures ("malformed-output retry rate at zero"), not the
    primary defense -- a nonzero rate here means the schema/model drifted, not
    that a retry loop is doing its job as designed.
    """
    retries = 0
    msgs = list(messages)
    last_err = None
    for _ in range(max_retries + 1):
        resp = client.chat_completion(messages=msgs, model=model, temperature=temperature,
                                       max_tokens=max_tokens, response_format=response_format)
        content = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage") or {}
        try:
            return json.loads(content), usage, retries
        except (json.JSONDecodeError, TypeError) as e:
            last_err = e
            retries += 1
            msgs = msgs + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "That was not valid JSON matching the schema. "
                                             "Return ONLY the structured output."},
            ]
    raise MalformedOutputError(
        f"model did not return schema-valid JSON after {max_retries + 1} attempt(s): {last_err}"
    )


def _extract_via_model(client, model, field_def, text, *, table_context=None,
                        exemplars=None, n_samples=1, max_tokens=512):
    """One field through one client. `n_samples > 1` drives the self-consistency
    gate signal: extra sampled passes at temperature > 0, compared downstream."""
    schema = build_wire_schema([field_def])
    messages = build_messages(field_def, text, table_context=table_context, exemplars=exemplars)

    samples, confidences = [], []
    span = None
    tokens_in = tokens_out = retries_total = 0
    for i in range(max(1, n_samples)):
        temperature = 0.0 if i == 0 else 0.7
        payload, usage, retries = chat_json(client, model, messages, schema,
                                             temperature=temperature, max_tokens=max_tokens)
        retries_total += retries
        tokens_in += usage.get("prompt_tokens") or 0
        tokens_out += usage.get("completion_tokens") or 0
        entry = parse_ladder_response([field_def], payload).get(field_def.name)
        if entry is None:
            continue
        value, entry_span, conf = entry
        samples.append(value)
        if i == 0:
            span = entry_span
        if conf is not None:
            confidences.append(conf)

    value = samples[0] if samples else None
    confidence = confidences[0] if confidences else None
    return value, span, confidence, samples, tokens_in, tokens_out, retries_total


# ── Cost ─────────────────────────────────────────────────────────────────────

# Approximate, as of writing -- pass `pricing=` to override if these drift.
DEFAULT_CLAUDE_PRICING_USD_PER_MTOK = {
    "claude-opus-4-8":            {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
}


def estimate_cost(model, tokens_in, tokens_out, pricing=None):
    rates = (pricing or DEFAULT_CLAUDE_PRICING_USD_PER_MTOK).get(model)
    if not rates:
        return 0.0
    return (tokens_in / 1_000_000) * rates["input"] + (tokens_out / 1_000_000) * rates["output"]


# ── Confidence gate ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateSignal:
    name: str          # bounds | cross_check | span | self_consistency | model_confidence
    passed: bool
    detail: str = ""

    def as_dict(self):
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class GateResult:
    escalate: bool
    signals: tuple      # of GateSignal, in priority order
    reason: str = None  # name of the highest-priority failing signal, or None

    def as_dict(self):
        return {"escalate": self.escalate, "reason": self.reason,
                "signals": [s.as_dict() for s in self.signals]}


def evaluate_gate(field_def, value, span, *, ex107=None, spec=None, samples=None,
                   model_confidence=None, confidence_floor=0.35):
    """Gate on checkable signals, in the priority #104 specifies: field-spec
    bound violations need no judgment call and come first; then span
    resolution; then cross-check against EX-107; then self-consistency
    across sampled passes; self-reported model confidence is weighted LAST
    -- it decides `reason` only when nothing else already did, though a
    confidence far below floor can still be the sole trigger for `escalate`
    when everything else checks out.
    """
    signals = []

    flags = spec.check_value(field_def.name, value, ex107=ex107) if spec else []
    bounds_bad = [f for f in flags if f.code != "cross_check_failed"]
    cross_bad = [f for f in flags if f.code == "cross_check_failed"]
    signals.append(GateSignal("bounds", not bounds_bad, "; ".join(f.message for f in bounds_bad)))

    span_ok = span is not None
    signals.append(GateSignal("span", span_ok, "" if span_ok else "no locatable source span"))

    signals.append(GateSignal("cross_check", not cross_bad, "; ".join(f.message for f in cross_bad)))

    if samples and len(samples) > 1:
        consistent = all(s == samples[0] for s in samples[1:])
        signals.append(GateSignal("self_consistency", consistent,
                                   "" if consistent else f"sampled values disagree: {samples}"))
    else:
        signals.append(GateSignal("self_consistency", True, "single sample, not checked"))

    if model_confidence is not None:
        conf_ok = model_confidence >= confidence_floor
        signals.append(GateSignal("model_confidence", conf_ok,
                                   "" if conf_ok else
                                   f"self-reported confidence {model_confidence} < floor {confidence_floor}"))
    else:
        signals.append(GateSignal("model_confidence", True, "not reported"))

    failing = [s for s in signals if not s.passed]
    return GateResult(bool(failing), tuple(signals), failing[0].name if failing else None)


# ── Provenance + run log ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Provenance:
    """Which rung produced a value, and everything needed to reconstruct why:
    document/span, prompt/exemplar-set version, model, token cost. `as_string`
    is the compact form `output_store.FieldValue.provenance` already uses
    (`"rule:..."` / `"model:..."`); `as_dict` is the full record for the log."""

    rung: str
    document: str = None
    span: tuple = None
    model: str = None
    rule_id: str = None
    prompt_version: str = None
    exemplar_set: str = None
    tokens_in: int = None
    tokens_out: int = None
    cost_usd: float = None

    def as_string(self):
        if self.rung == "rule":
            return f"rule:{self.rule_id}"
        if self.rung == "xbrl":
            return "xbrl:ex107"
        bits = [f"{self.rung}:{self.model}"]
        if self.prompt_version:
            bits.append(f"prompt={self.prompt_version}")
        if self.exemplar_set:
            bits.append(f"exemplars={self.exemplar_set}")
        return "@".join(bits)

    def as_dict(self):
        d = asdict(self)
        if isinstance(d.get("span"), tuple):
            d["span"] = list(d["span"])
        return d


@dataclass
class ShadowComparison:
    """Shadow mode: rule and model running in parallel (#107).
    Logged when a rule is in its promotion window."""

    rule_value: object
    model_value: object
    rule_span: tuple
    model_span: tuple
    agreement: bool
    note: str = None

    def as_dict(self):
        d = asdict(self)
        if isinstance(d.get("rule_span"), tuple):
            d["rule_span"] = list(d["rule_span"])
        if isinstance(d.get("model_span"), tuple):
            d["model_span"] = list(d["model_span"])
        return d


@dataclass
class LogEntry:
    accession: str
    document: str
    field: str
    rung: str
    value: object
    unit: str
    span: tuple
    confidence: float
    flags: list
    escalated: bool
    gated: bool
    gate: dict
    provenance: dict
    shadow: dict = None           # ShadowComparison when rule is in shadow window

    def as_dict(self):
        d = asdict(self)
        if isinstance(d.get("span"), tuple):
            d["span"] = list(d["span"])
        if d.get("shadow"):
            d["shadow"] = d["shadow"].as_dict() if hasattr(d["shadow"], "as_dict") else d["shadow"]
        return d


class RunLog:
    """Per-run provenance + cost/escalation stats. Every `ExtractionLadder`
    result is recorded here; `to_jsonl()` is the full reconstruction of the
    run, and `summary()` is the cost log #104 asks for -- per-document and
    per-run Claude spend, plus escalation rate by field."""

    def __init__(self):
        self.entries = []
        self.malformed_retries = 0

    def record(self, entry):
        self.entries.append(entry)

    def rung_counts(self):
        counts = {}
        for e in self.entries:
            counts[e.rung] = counts.get(e.rung, 0) + 1
        return counts

    def escalation_rate(self):
        if not self.entries:
            return 0.0
        return sum(1 for e in self.entries if e.escalated) / len(self.entries)

    def escalation_rate_by_field(self):
        totals, esc = {}, {}
        for e in self.entries:
            totals[e.field] = totals.get(e.field, 0) + 1
            if e.escalated:
                esc[e.field] = esc.get(e.field, 0) + 1
        return {f: round(esc.get(f, 0) / n, 4) for f, n in totals.items()}

    def gated_fields(self):
        return sorted({e.field for e in self.entries if e.gated})

    def cost_summary(self):
        claude = [e for e in self.entries if e.rung == "claude"]
        return {
            "claude_calls": len(claude),
            "claude_cost_usd": round(sum(e.provenance.get("cost_usd") or 0.0 for e in claude), 6),
            "claude_tokens_in": sum(e.provenance.get("tokens_in") or 0 for e in claude),
            "claude_tokens_out": sum(e.provenance.get("tokens_out") or 0 for e in claude),
            "malformed_output_retries": self.malformed_retries,
        }

    def cost_by_document(self):
        per_doc = {}
        for e in self.entries:
            if e.rung != "claude":
                continue
            key = f"{e.accession}/{e.document}"
            per_doc[key] = per_doc.get(key, 0.0) + (e.provenance.get("cost_usd") or 0.0)
        return {k: round(v, 6) for k, v in per_doc.items()}

    def extraction_breakdown(self):
        """Per-run breakdown: what % of extractions came from rule vs local vs Claude.
        The #107 throughput budget metric: rule coverage should dominate."""
        if not self.entries:
            return {"rule": 0.0, "xbrl": 0.0, "local": 0.0, "claude": 0.0}
        counts = self.rung_counts()
        total = len(self.entries)
        return {rung: round(counts.get(rung, 0) / total, 4) for rung in ("rule", "xbrl", "local", "claude")}

    def shadow_disagreements(self):
        """Fields where shadow mode detected rule/model disagreements (template change signal)."""
        disagreements = {}
        for e in self.entries:
            if e.shadow and not e.shadow.agreement:
                key = f"{e.field}"
                if key not in disagreements:
                    disagreements[key] = []
                disagreements[key].append({
                    "accession": e.accession, "document": e.document,
                    "note": e.shadow.note,
                })
        return disagreements

    def summary(self):
        return {
            "total_extractions": len(self.entries),
            "rung_counts": self.rung_counts(),
            "extraction_breakdown": self.extraction_breakdown(),
            "escalation_rate": round(self.escalation_rate(), 4),
            "escalation_rate_by_field": self.escalation_rate_by_field(),
            "gated_fields": self.gated_fields(),
            "shadow_disagreements": self.shadow_disagreements(),
            "cost_by_document": self.cost_by_document(),
            **self.cost_summary(),
        }

    def to_jsonl(self):
        """The run, fully reconstructible: one JSON line per extraction."""
        return [json.dumps(e.as_dict()) for e in self.entries]


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class LadderResult:
    field: str
    value: object
    unit: str
    span: tuple
    confidence: float
    rung: str
    provenance: str
    flags: list           # of field_spec.Flag
    escalated: bool = False
    gated: bool = False

    def to_field_value(self):
        """Bridge into output_store (#109) -- the same record the store wants."""
        return FieldValue(field=self.field, value=self.value, unit=self.unit,
                           span=self.span, provenance=self.provenance,
                           confidence=self.confidence,
                           flags=[f.as_dict() for f in self.flags])


# ── Ladder ───────────────────────────────────────────────────────────────────

class ExtractionLadder:
    """Four rungs, cheapest first. See module docstring."""

    def __init__(self, spec, *, rules=None, exemplars=None,
                 local_client=None, local_model=None,
                 claude_client=None, claude_model=None, claude_enabled=True,
                 confidence_floor=0.35, self_consistency_samples=1,
                 prompt_version="v1", exemplar_set_version=None,
                 max_tokens=512, log=None):
        self.spec = spec
        self.rules = rules
        self.exemplars = exemplars
        self.local_client = local_client
        self.local_model = local_model or getattr(
            getattr(local_client, "config", None), "model", None)
        self.claude_client = claude_client
        self.claude_model = claude_model or getattr(
            getattr(claude_client, "config", None), "model", None)
        self.claude_enabled = claude_enabled
        self.confidence_floor = confidence_floor
        self.self_consistency_samples = self_consistency_samples
        self.prompt_version = prompt_version
        self.exemplar_set_version = exemplar_set_version
        self.max_tokens = max_tokens
        self.log = log if log is not None else RunLog()

    @property
    def claude_available(self):
        return bool(self.claude_enabled and self.claude_client is not None)

    def extract(self, field_name, *, text, issuer=None, ex107=None,
                accession=None, document=None, table_context=None):
        f = self.spec.field(field_name)
        if f is None:
            raise KeyError(f"{field_name!r} is not defined in spec {self.spec.spec_id}")
        form = self.spec.spec_id

        # Rung 1: promoted rule (#107).
        if self.rules is not None and issuer is not None:
            rule_result = None
            if hasattr(self.rules, 'apply_rule') and callable(self.rules.apply_rule):
                # RuleManager instance: call apply_rule() with text
                rule_result = self.rules.apply_rule(form, issuer, field_name, text)
            else:
                # Dict or callable returning pre-computed RuleMatch
                rule = _lookup_rule(self.rules, form, issuer, field_name)
                if rule is not None:
                    rule_result = rule

            if rule_result is not None:
                if isinstance(rule_result, tuple):
                    # Result from RuleManager.apply_rule(): (value, start, end)
                    value, start, end = rule_result
                    rule_match = RuleMatch(value=value, rule_id=f"applied-rule-{issuer}-{field_name}",
                                          span=(start, end), confidence=1.0)
                else:
                    # Already a RuleMatch object
                    rule_match = rule_result

                # Check shadow mode and run model in parallel if needed
                shadow_comp = None
                if hasattr(self.rules, 'is_shadowing') and self.rules.is_shadowing(form, issuer, field_name):
                    # Rule is in shadow window; run model in parallel to compare
                    if self.local_client is not None:
                        exemplars = _lookup_exemplars(self.exemplars, form, issuer, field_name)
                        # Shadow mode requires exemplars (#106) to be meaningful; skip if missing
                        if exemplars:
                            model_value, model_span, model_conf, _samples, _tin, _tout, _retries = _extract_via_model(
                                self.local_client, self.local_model, f, text, table_context=table_context,
                                exemplars=exemplars, n_samples=1, max_tokens=self.max_tokens)
                            agreement = rule_match.value == model_value
                            shadow_comp = ShadowComparison(
                                rule_value=rule_match.value, model_value=model_value,
                                rule_span=rule_match.span, model_span=model_span,
                                agreement=agreement,
                                note=None if agreement else f"rule={rule_match.value!r} vs model={model_value!r}"
                            )

                prov = Provenance(rung="rule", rule_id=rule_match.rule_id, document=document, span=rule_match.span)
                result = self._finish(f, rule_match.value, rule_match.span, "rule", prov, rule_match.confidence,
                                     None, ex107, accession=accession, document=document, shadow=shadow_comp)
                return result

        # Rung 2: XBRL / EX-107 fee exhibit (#101).
        if ex107 and ex107.get(field_name) is not None:
            prov = Provenance(rung="xbrl", document=document)
            return self._finish(f, ex107[field_name], None, "xbrl", prov, 1.0,
                                 None, ex107, accession=accession, document=document)

        if self.local_client is None:
            raise RuntimeError(
                f"no local client configured; {field_name!r} cannot reach the "
                f"local rung (#103 must be wired up before #104 can escalate)."
            )

        # Rung 3: local 7B, with (form, issuer, field) exemplars if available (#106).
        exemplars = _lookup_exemplars(self.exemplars, form, issuer, field_name)
        value, span, conf, samples, tin, tout, retries = _extract_via_model(
            self.local_client, self.local_model, f, text, table_context=table_context,
            exemplars=exemplars, n_samples=self.self_consistency_samples,
            max_tokens=self.max_tokens)
        self.log.malformed_retries += retries

        gate = evaluate_gate(f, value, span, ex107=ex107, spec=self.spec, samples=samples,
                              model_confidence=conf, confidence_floor=self.confidence_floor)

        if not gate.escalate:
            prov = Provenance(rung="local", document=document, span=span, model=self.local_model,
                               prompt_version=self.prompt_version,
                               exemplar_set=self.exemplar_set_version,
                               tokens_in=tin, tokens_out=tout)
            return self._finish(f, value, span, "local", prov, conf, gate, ex107,
                                 accession=accession, document=document)

        # Rung 4: Claude, only for the gated remainder.
        if self.claude_available:
            c_value, c_span, c_conf, _samples, ctin, ctout, cretries = _extract_via_model(
                self.claude_client, self.claude_model, f, text, table_context=table_context,
                exemplars=exemplars, n_samples=1, max_tokens=self.max_tokens)
            self.log.malformed_retries += cretries
            cost = estimate_cost(self.claude_model, ctin, ctout)
            prov = Provenance(rung="claude", document=document, span=c_span, model=self.claude_model,
                               prompt_version=self.prompt_version,
                               exemplar_set=self.exemplar_set_version,
                               tokens_in=ctin, tokens_out=ctout, cost_usd=cost)
            return self._finish(f, c_value, c_span, "claude", prov, c_conf, gate, ex107,
                                 accession=accession, document=document, escalated=True)

        # Local-only mode: flag the gate failure, keep going -- never raise.
        prov = Provenance(rung="local", document=document, span=span, model=self.local_model,
                           prompt_version=self.prompt_version,
                           exemplar_set=self.exemplar_set_version, tokens_in=tin, tokens_out=tout)
        return self._finish(f, value, span, "local", prov, conf, gate, ex107,
                             accession=accession, document=document, gated=True)

    def extract_document(self, field_names, *, text, issuer=None, ex107=None,
                          accession=None, document=None, table_context=None):
        return [
            self.extract(name, text=text, issuer=issuer, ex107=ex107,
                          accession=accession, document=document, table_context=table_context)
            for name in field_names
        ]

    def _finish(self, f, value, span, rung, provenance, confidence, gate, ex107, *,
                accession, document, escalated=False, gated=False, shadow=None):
        """Finish extraction: apply field specs, record in log, return result.

        shadow: ShadowComparison when rule in shadow mode (comparing rule vs model).
        """
        flags = self.spec.check_value(f.name, value, ex107=ex107) if value is not None else []
        if gated:
            flags = list(flags) + [Flag(
                f.name, "gated_no_claude", "warn",
                "confidence gate wants escalation but Claude is disabled; value "
                "is from the local rung and unresolved",
            )]
        result = LadderResult(field=f.name, value=value, unit=f.unit, span=span,
                               confidence=confidence, rung=rung,
                               provenance=provenance.as_string(), flags=flags,
                               escalated=escalated, gated=gated)
        self.log.record(LogEntry(
            accession=accession, document=document, field=f.name, rung=rung,
            value=value, unit=f.unit, span=span, confidence=confidence,
            flags=[fl.as_dict() for fl in flags], escalated=escalated, gated=gated,
            gate=gate.as_dict() if gate else None, provenance=provenance.as_dict(),
            shadow=shadow,
        ))
        return result


if __name__ == "__main__":
    from field_spec import FieldDefinition

    field_def = FieldDefinition(
        name="estimated_value_per_1000", type="number", extraction_path="fixed-anchor",
        sections=("estimated_value",), wire_key="ev1000", bounds={"min": 900, "max": 1000},
    )

    class _Spec:
        spec_id = "demo"
        def field(self, name):
            return field_def if name == field_def.name else None
        def check_value(self, name, value, ex107=None):
            from field_spec import Flag as _F
            if value is not None and not (900 <= value <= 1000):
                return [_F(name, "out_of_bounds", "warn", f"{name}={value} outside 900-1000")]
            return []

    class _FakeClient:
        """Same shape as OllamaClient.chat_completion; no network."""
        def __init__(self, value, span, conf):
            self.value, self.span, self.conf = value, span, conf
        def chat_completion(self, messages, model=None, temperature=None,
                             max_tokens=None, top_p=None, response_format=None):
            body = json.dumps({"ev1000": {"v": self.value, "s": list(self.span) if self.span else None,
                                           "c": self.conf}})
            return {"choices": [{"message": {"content": body}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 12}}

    spec = _Spec()

    print("Rung 1: promoted rule short-circuits the model entirely")
    ladder = ExtractionLadder(spec, rules={("JPM", "estimated_value_per_1000"):
                               RuleMatch(972.4, "anchor-ev1000", span=(10, 20))},
                               local_client=_FakeClient(1240.0, (0, 5), 0.9))
    r = ladder.extract("estimated_value_per_1000", text="...", issuer="JPM")
    print(f"  rung={r.rung} value={r.value} provenance={r.provenance!r}")
    assert r.rung == "rule" and r.value == 972.4

    print("\nRung 3 -> 4: local out-of-bounds escalates to Claude")
    ladder = ExtractionLadder(
        spec, local_client=_FakeClient(1240.0, (100, 110), 0.9), local_model="qwen2.5:7b",
        claude_client=_FakeClient(972.4, (100, 110), 0.95), claude_model="claude-sonnet-4-6",
    )
    r = ladder.extract("estimated_value_per_1000", text="...", issuer="JPM",
                        accession="0001", document="424b2.htm")
    print(f"  rung={r.rung} value={r.value} escalated={r.escalated} "
          f"cost=${ladder.log.cost_summary()['claude_cost_usd']}")
    assert r.rung == "claude" and r.escalated and r.value == 972.4

    print("\nLocal-only mode: gated field flagged, run does not fail")
    ladder = ExtractionLadder(spec, local_client=_FakeClient(1240.0, (100, 110), 0.9),
                               local_model="qwen2.5:7b", claude_client=None)
    r = ladder.extract("estimated_value_per_1000", text="...", issuer="JPM",
                        accession="0001", document="424b2.htm")
    print(f"  rung={r.rung} gated={r.gated} flags={[f.code for f in r.flags]}")
    assert r.gated and not r.escalated and "gated_no_claude" in [fl.code for fl in r.flags]

    print("\nRun log summary:", ladder.log.summary())
    print("\nextraction_ladder self-check: PASS")
