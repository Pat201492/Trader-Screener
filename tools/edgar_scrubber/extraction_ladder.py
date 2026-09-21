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

Prompt assembly is ORDER-SENSITIVE (#106): llama.cpp/Ollama reuse KV cache for
identical prompt PREFIXES, so `build_messages` puts everything static first
(system prompt, field spec, exemplars) and the VARYING document chunk last:

    [ static: system prompt ]                    <- identical across all calls
    [ static: field spec ]                        <- identical for one field
    [ static: exemplars for (issuer, field) ]     <- identical within an issuer
    [ VARYING: parsed table + document chunk ]    <- must be last

With this ordering, exemplar tokens prefill once per issuer instead of once
per document. `static_prefix()` is the literal text of that prefix, and
`ExtractionLadder` asserts it does not change mid-run for the same (issuer,
field) -- see `_static_prefix_seen` -- so a future refactor that interpolates
the document earlier fails loudly instead of silently tripling prefill cost.

Local-only mode (no `claude_client`, or `claude_enabled=False`) never raises
for a gated field: it returns the local value flagged `gated_no_claude` and
keeps going, per #104's acceptance.

stdlib only. Run the self-check:  python tools/edgar_scrubber/extraction_ladder.py
"""

import json
import re
from dataclasses import asdict, dataclass, field as _dc_field

try:  # package import: tools.edgar_scrubber.extraction_ladder
    from .field_spec import Flag
    from .normalize import parse_date_prose as _parse_date_prose
    from .output_store import FieldValue
except ImportError:  # standalone: python tools/edgar_scrubber/extraction_ladder.py
    from field_spec import Flag
    from normalize import parse_date_prose as _parse_date_prose
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
        # Only RuleManager instances should be callable; they take 3-tuple.
        return rules(form, issuer, field_name)
    # Dict-based: check 3-tuple first (new API)
    result = rules.get((form, issuer, field_name))
    if result is None:
        # Fall back to 2-tuple for backward compat with old dict-based rules
        result = rules.get((issuer, field_name))
    return result


def _lookup_exemplars(exemplars, form, issuer, field_name):
    """Returns `(lines, exemplar_set_version)`.

    `exemplars` may be a full `exemplars.ExemplarProvider` (#106) -- detected
    by its `.resolve(issuer, field) -> ExemplarSet` method -- in which case the
    version comes from the SPECIFIC set actually selected for this (issuer,
    field), the version #104's provenance needs to distinguish "exemplars
    changed" from "model changed" from "documents changed". A plain callable
    or dict (e.g. a bare `ValidationStore`, or a test double) yields lines only
    and the version falls back to whatever the ladder was constructed with.
    """
    if not exemplars:
        return None, None
    resolver = getattr(exemplars, "resolve", None)
    if callable(resolver):
        ex_set = resolver(issuer, field_name)
        return (list(ex_set.lines()) if ex_set.rows else None), ex_set.version
    if callable(exemplars) and not isinstance(exemplars, dict):
        # Backward compat: ValidationStore.__call__ only takes (issuer, field).
        # Try 3-tuple first (new API), fall back to 2-tuple (old API).
        try:
            result = exemplars(form, issuer, field_name)
        except TypeError:
            result = exemplars(issuer, field_name)
        # Wrap result to match (lines, version) tuple return
        if isinstance(result, tuple) and len(result) == 2:
            return result
        return result, None
    # Dict-based: check 3-tuple first (new API), then 2-tuple for backward compat
    result = exemplars.get((form, issuer, field_name))
    if result is None:
        result = exemplars.get((issuer, field_name))
    return result, None


# ── Constrained decode: wire schema + prompt ────────────────────────────────

_WIRE_TYPE_MAP = {
    "string": "string", "date": "string", "enum": "string",
    "number": "number", "percent": "number",
    "boolean": "boolean",
}


#: The three slots of one field's wire entry. Self-describing on purpose.
#: They were `v`/`s`/`c` to save tokens (#104), and measured against real
#: filings that trade was a bad one: a 7B filled the unlabelled value slot with
#: a CONFIDENCE-shaped number. Across 25 Citigroup 424B2s, `estimated_value_per_1000`
#: came back 0.0 twenty-one times out of twenty-five, and `contingent_coupon_rate`
#: read 0.95/0.9/0.8/1.0 -- the vocabulary of a probability, not of a coupon
#: (11.40) or a barrier (60.00). String fields like `issuer` and `cusip` were
#: unaffected, because a string slot cannot be confused with a probability.
#: The saving was ~4 tokens per field per call; the cost was the field itself.
#: Referenced by `SYSTEM_PROMPT` and `build_wire_schema` alike so the prompt
#: and the schema can never describe different key names.
WIRE_VALUE_KEY = "value"
WIRE_SPAN_KEY = "span"
WIRE_CONF_KEY = "confidence"

#: Superseded short keys, still accepted on the way IN so a cached response or
#: a replayed transcript from before the rename still parses.
_LEGACY_WIRE_KEYS = {WIRE_VALUE_KEY: "v", WIRE_SPAN_KEY: "s", WIRE_CONF_KEY: "c"}


def _wire_key(f):
    return f.wire_key or f.name


def _entry_get(entry, key):
    """Read one slot, preferring the current name and falling back to the
    short one it replaced."""
    if key in entry:
        return entry[key]
    return entry.get(_LEGACY_WIRE_KEYS[key])


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
                WIRE_VALUE_KEY: v_schema,
                WIRE_SPAN_KEY: {"type": ["array", "null"], "items": {"type": "integer"},
                                "minItems": 2, "maxItems": 2},
                # A probability, and constrained decode is what makes it one.
                # Unbounded, this slot came back holding the VALUE the model had
                # just emitted (a barrier of 0.95 self-certifying at "0.95"), or
                # plain garbage -- 1.6e+30 and 1000000.0 were both observed in
                # the store. Both make `evaluate_gate`'s floor meaningless, and
                # a copied value inverts it: a wrong big number passes, a right
                # small one escalates.
                WIRE_CONF_KEY: {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": [WIRE_VALUE_KEY, WIRE_SPAN_KEY, WIRE_CONF_KEY],
        }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "edgar_extraction",
            "schema": {"type": "object", "properties": props, "required": list(props)},
            "strict": True,
        },
    }


# No "respond only in JSON" -- constrained decode makes it structural, so that
# instruction is deleted from the prompt entirely (#111). A module-level
# constant, not inlined in `build_messages`, so `static_prefix` builds the
# EXACT same text a real call sends -- one source of truth, not two copies
# that can drift apart (#106).
# Every slot is described, and the value slot FIRST. The previous wording
# explained the span and the confidence and never once mentioned the value
# key -- so the only numeric instruction in the whole prompt was "0 to 1",
# and that is exactly the range the value slot came back in. Saying what the
# value is, and saying explicitly that it is not the confidence, is the other
# half of the `v` -> `value` rename above.
SYSTEM_PROMPT = (
    "You are an SEC EDGAR extraction engine. Extract exactly the requested "
    "field from SOURCE TEXT. "
    f"`{WIRE_VALUE_KEY}` is the extracted field itself: the number or text "
    "the document states, in the document's own units. A percentage is the "
    "percent figure as written, not a fraction of one. A per-note dollar "
    "amount is the dollar figure as written. Use null when the document "
    "does not state the field. "
    f"`{WIRE_SPAN_KEY}` is the [start, end] character offset pair into "
    "SOURCE TEXT for the text that supports the value, or null if you "
    "cannot locate one -- never guess a span. "
    f"`{WIRE_CONF_KEY}` is how sure you are, 0 to 1. It describes the "
    f"value; it is never itself the answer. Never copy `{WIRE_CONF_KEY}` "
    f"into `{WIRE_VALUE_KEY}`."
)


def _static_user_parts(field_def, exemplars):
    """The field-spec + exemplar portion of the prompt: identical on every
    call for a given (field, exemplar set), regardless of which document is
    being read. This is the part of the user message that belongs in the
    static, KV-cache-reusable prefix (#106) -- everything document-specific
    (parsed table, source text) is added AFTER this, never interleaved with it."""
    parts = [f"FIELD: {field_def.name} ({field_def.type})"]
    if field_def.description:
        parts.append(f"DESCRIPTION: {field_def.description}")
    if field_def.anchors:
        parts.append(f"ANCHORS: {', '.join(field_def.anchors)}")
    if field_def.enum:
        parts.append(f"ALLOWED VALUES: {', '.join(field_def.enum)}")
    if field_def.bounds:
        parts.append(f"BOUNDS: {field_def.bounds}")
    if exemplars:
        parts.append("EXEMPLARS for this issuer/field (#106):\n" +
                      "\n".join(f"- {e}" for e in exemplars))
    return parts


def static_prefix(field_def, exemplars=None):
    """The byte-identical-within-an-issuer prefix (#106): system prompt +
    field spec + exemplars. llama.cpp/Ollama reuse KV for identical prompt
    PREFIXES, so this text must be assembled once per (issuer, field) and
    never change while a document chunk is interpolated after it -- see the
    ordering `build_messages` enforces below, and the runtime check
    `ExtractionLadder` makes against this exact string (#111)."""
    return SYSTEM_PROMPT + "\n\n" + "\n\n".join(_static_user_parts(field_def, exemplars))


def build_messages(field_def, text, *, table_context=None, exemplars=None):
    """Static prefix first (system + field spec + exemplars, identical across
    documents for one issuer), VARYING document content last (parsed table,
    then source text) -- see module docstring's ordering diagram. Putting the
    document chunk anywhere earlier breaks KV-cache prefix reuse and roughly
    triples prefill per call (#106/#111)."""
    parts = list(_static_user_parts(field_def, exemplars))
    if table_context:
        parts.append(f"PARSED TABLE, label: value pairs (#101):\n{table_context}")
    parts.append(f"SOURCE TEXT:\n{text}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


#: A `c` that was present but is not a probability. Distinct from None ("the
#: model did not report one", which `evaluate_gate` passes vacuously): garbage
#: here is positive evidence the output is unreliable, so it must FAIL the
#: gate rather than be waved through. `-inf` is out of band and fails closed
#: against any floor even if some future path forgets to check it explicitly.
INVALID_CONFIDENCE = float("-inf")


def coerce_confidence(raw):
    """Wire `c` -> a probability in [0,1], None if absent, INVALID_CONFIDENCE
    if present but not one. Constrained decode should already guarantee the
    range (`build_wire_schema`); this is the backstop for an unconstrained
    client, a schema-ignoring model, or a replayed old payload."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return INVALID_CONFIDENCE
    value = float(raw)
    if value != value or value < 0.0 or value > 1.0:   # NaN or out of range
        return INVALID_CONFIDENCE
    return value


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
            value = _entry_get(entry, WIRE_VALUE_KEY)
            s = _entry_get(entry, WIRE_SPAN_KEY)
            conf = _entry_get(entry, WIRE_CONF_KEY)
        else:
            value, s, conf = entry, None, None
        span = tuple(s) if isinstance(s, (list, tuple)) and len(s) == 2 else None
        out[f.name] = (value, span, coerce_confidence(conf))
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

# ── Span support ─────────────────────────────────────────────────────────────
#
# A returned span proves the model pointed SOMEWHERE; it does not prove the
# span says what the model claims it says. That gap is not hypothetical: on a
# preliminary 424B2 that discloses `estimated_value_per_1000` as a RANGE
# ("expected to be between $962.60 and $992.60", with the point value deferred
# to the final pricing supplement), a 7B returned 989.5 -- a number that is in
# bounds (900-1000), the right type, non-null on a required field, and absent
# from the document. Bounds cannot catch a fabrication that lands inside them,
# and `span is not None` cannot catch one carrying a plausible span. Reading
# the span's own text is what separates extracted from invented (#144).

_NUMBER_IN_TEXT = re.compile(r"[-+]?\$?\s?\d[\d,]*(?:\.\d+)?\s?%?")

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

_LONG_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\w*\.?\s+(\d{1,2})\b(?:\s*,)?\s*(\d{4})\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _numbers_in(text):
    """Every number the span states, issuer formatting stripped: `$1,000.00`,
    `9.75%`, `(2,500)` all reduce to a float."""
    out = []
    for m in _NUMBER_IN_TEXT.finditer(text):
        raw = m.group().replace("$", "").replace(",", "").replace("%", "")
        raw = raw.replace(" ", "").strip()
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _dates_in(text):
    """ISO dates the span states, in any of the three formats filings use.
    `June 7, 2024`, `2024-06-07` and `06/07/2024` all reduce to `2024-06-07`,
    so a correct value written in prose is not mistaken for a fabrication."""
    out = set()
    for mo, d, y in _LONG_DATE.findall(text):
        out.add(f"{int(y):04d}-{_MONTHS[mo.lower()]:02d}-{int(d):02d}")
    for y, mo, d in _ISO_DATE.findall(text):
        out.add(f"{int(y):04d}-{int(mo):02d}-{int(d):02d}")
    for mo, d, y in _US_DATE.findall(text):
        out.add(f"{int(y):04d}-{int(mo):02d}-{int(d):02d}")
    return out


def _squash(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def span_supports_value(field_def, value, span_text):
    """Does the span's own text actually state `value`?

    Returns True (supported), False (contradicted -- the span does not contain
    the value), or None (not checkable for this type, so the caller must not
    treat it as a failure). None is deliberate and distinct from False: an
    `array` of underlying objects has no textual form to compare against, and
    silently scoring that as a failure would escalate every list-valued field
    to Claude for no reason.
    """
    if value is None or not span_text:
        return None

    t = field_def.type

    if t in ("number", "percent"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None          # type_mismatch is the bounds signal's job
        nums = _numbers_in(span_text)
        if not nums:
            return False
        # Exact-ish: the span must literally state the number. A percent may be
        # written `9.75%` (9.75) or `0.0975`, so accept the x100 form too.
        tol = max(abs(value) * 1e-6, 1e-9)
        cands = {value}
        if t == "percent":
            cands |= {value * 100.0, value / 100.0}
        return any(abs(n - c) <= max(tol, abs(c) * 1e-6) for n in nums for c in cands)

    if t == "date":
        if not isinstance(value, str):
            return None
        return value in _dates_in(span_text)

    if t in ("string", "enum"):
        v = _squash(str(value))
        return bool(v) and v in _squash(span_text)

    return None                  # array / object: no scalar textual form


@dataclass(frozen=True)
class GateSignal:
    name: str          # bounds | span | span_support | cross_check |
                       # self_consistency | model_confidence
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


def _span_support_signal(field_def, value, span, source_text):
    """The `span_support` GateSignal. Passes vacuously whenever the check
    cannot be made -- no source text, no span, an out-of-range span, or a type
    with no scalar textual form. Only an affirmative contradiction fails."""
    if source_text is None or span is None:
        return GateSignal("span_support", True, "not checked (no source text or span)")
    try:
        start, end = span
        snippet = source_text[start:end]
    except (TypeError, ValueError):
        return GateSignal("span_support", True, "not checked (malformed span)")
    if not snippet:
        return GateSignal("span_support", False,
                          f"span {list(span)} is empty or outside the source text")
    supported = span_supports_value(field_def, value, snippet)
    if supported is None:
        return GateSignal("span_support", True,
                          f"not checkable for type {field_def.type!r}")
    if supported:
        return GateSignal("span_support", True)
    return GateSignal("span_support", False,
                      f"{field_def.name}={value!r} does not appear in its own "
                      f"source span {list(span)}: {snippet[:120]!r}")


def evaluate_gate(field_def, value, span, *, ex107=None, spec=None, samples=None,
                   model_confidence=None, confidence_floor=0.35, source_text=None):
    """Gate on checkable signals, in the priority #104 specifies: field-spec
    bound violations need no judgment call and come first; then span
    resolution, then whether that span actually SAYS the value (#144); then
    cross-check against EX-107; then self-consistency across sampled passes;
    self-reported model confidence is weighted LAST -- it decides `reason`
    only when nothing else already did, though a confidence far below floor
    can still be the sole trigger for `escalate` when everything else checks
    out.

    `source_text` is the same text the value was extracted from, and `span`
    indexes into it. Omit it and the `span_support` signal passes vacuously --
    every pre-#144 caller keeps its old behaviour.
    """
    signals = []

    flags = spec.check_value(field_def.name, value, ex107=ex107) if spec else []
    bounds_bad = [f for f in flags if f.code != "cross_check_failed"]
    cross_bad = [f for f in flags if f.code == "cross_check_failed"]
    signals.append(GateSignal("bounds", not bounds_bad, "; ".join(f.message for f in bounds_bad)))

    span_ok = span is not None
    signals.append(GateSignal("span", span_ok, "" if span_ok else "no locatable source span"))

    signals.append(_span_support_signal(field_def, value, span, source_text))

    signals.append(GateSignal("cross_check", not cross_bad, "; ".join(f.message for f in cross_bad)))

    if samples and len(samples) > 1:
        consistent = all(s == samples[0] for s in samples[1:])
        signals.append(GateSignal("self_consistency", consistent,
                                   "" if consistent else f"sampled values disagree: {samples}"))
    else:
        signals.append(GateSignal("self_consistency", True, "single sample, not checked"))

    if model_confidence is INVALID_CONFIDENCE or model_confidence == INVALID_CONFIDENCE:
        # Not "low confidence" -- a model that reports 1.6e+30 has told you
        # nothing about this value except that its output cannot be read.
        signals.append(GateSignal("model_confidence", False,
                                   "self-reported confidence is not a probability in [0,1]"))
    elif model_confidence is not None:
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

def _normalize_dateish(f, value):
    """A date-typed value as ISO, plus any flag the conversion earned (#166).

    Returns `(value, flags)`. A prose date becomes ISO. A string that carries no
    date at all is left exactly as it came back, so `type_mismatch` still fires
    on it -- converting it would be inventing one, and the distinction between
    "stated in prose" and "not a date" is the thing worth keeping.
    """
    if getattr(f, "type", None) != "date" or value is None:
        return value, []
    iso = _parse_date_prose(value)
    if iso is None:
        return value, []
    if isinstance(value, str) and value.strip() == iso:
        return iso, []
    return iso, [Flag(f.name, "date_normalized", "info",
                      "%s normalized to ISO from %r" % (f.name, value))]


class ExtractionLadder:
    """Four rungs, cheapest first. See module docstring."""

    def __init__(self, spec, *, rules=None, exemplars=None,
                 local_client=None, local_model=None,
                 claude_client=None, claude_model=None, claude_enabled=True,
                 confidence_floor=0.35, self_consistency_samples=1,
                 prompt_version="v1", exemplar_set_version=None,
                 max_tokens=512, log=None, shadow_external=False):
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
        # Ask the model for fields the EX-107 exhibit already answered, purely to
        # record whether it agrees (#164). Off by default: it spends tokens on a
        # field that already resolves exact, and changes no stored value.
        self.shadow_external = shadow_external
        self.prompt_version = prompt_version
        self.exemplar_set_version = exemplar_set_version
        self.max_tokens = max_tokens
        self.log = log if log is not None else RunLog()
        # (issuer, field) -> last static prefix seen this run (#106 guard).
        self._static_prefix_seen = {}

    @property
    def claude_available(self):
        return bool(self.claude_enabled and self.claude_client is not None)

    def _check_static_prefix(self, field_def, exemplars, exemplar_version, issuer, field_name):
        """Runtime guard for the #106 KV-cache invariant: the static prompt
        prefix (system + field spec + exemplars) must be a PURE FUNCTION of
        the exemplar-set version -- same version, same prefix, every time.

        The version is allowed to change mid-run (a validation session
        compounds: document 11 legitimately gets a bigger exemplar set than
        document 1, per #105), and the prefix changes with it -- that is by
        design, not a bug. What must never happen is the SAME version
        producing a DIFFERENT prefix: that means something started
        interpolating per-document content ahead of the exemplar block (or an
        exemplar provider stopped being deterministic for a version it already
        reported), and it would silently triple prefill cost (#111) if not
        caught here."""
        if issuer is None:
            return
        prefix = static_prefix(field_def, exemplars)
        key = (issuer, field_name)
        prior_version, prior_prefix = self._static_prefix_seen.get(key, (None, None))
        if prior_version is not None and prior_version == exemplar_version and prior_prefix != prefix:
            raise AssertionError(
                f"static prompt prefix changed for issuer={issuer!r} "
                f"field={field_name!r} while its exemplar_set version "
                f"({exemplar_version!r}) stayed the same -- this breaks "
                f"llama.cpp/Ollama KV-cache prefix reuse (#106/#111). A given "
                f"exemplar-set version must always render the same static "
                f"prefix; only a version bump may change it."
            )
        self._static_prefix_seen[key] = (exemplar_version, prefix)

    def _external_shadow_fields(self):
        """Fields the spec asks to shadow against an external source (#164)."""
        out = {}
        for c in getattr(self.spec, "cross_checks", ()) or ():
            if c.get("rule") == "external_shadow":
                out[c["field"]] = c
        return out

    def _shadow_external(self, f, text, ex107, *, issuer=None, form=None,
                          table_context=None):
        """Ask the model for a field the exhibit already answered, and compare.

        Why this exists (#164): rung 2 returns the XBRL value before the model is
        ever asked, so the spec's old `external_equals` cross-check compared that
        value against itself and passed on all 25 filings in the sample. The
        short-circuit is the right behaviour -- exact, at zero token cost -- but
        it left the one field with external ground truth measuring nothing.

        `aggregate_principal` is the only field of 33 where truth is known
        independently of the model. Shadowing it is the only way this system can
        measure how well the model reads a field where the answer is checkable,
        which is a prior worth having for the 32 fields where it is not (#165).

        Off by default: it spends tokens on a field that is already exact. The
        stored value never changes -- this only records whether the model agreed.
        """
        if not self.shadow_external or self.local_client is None:
            return None
        spec_check = self._external_shadow_fields().get(f.name)
        if spec_check is None:
            return None
        external = ex107.get(spec_check.get("source_field", f.name))
        if external is None:
            return None
        try:
            exemplars, _ = _lookup_exemplars(self.exemplars, form, issuer, f.name)
            value, span, _conf, _samples, _tin, _tout, retries = _extract_via_model(
                self.local_client, self.local_model, f, text,
                table_context=table_context, exemplars=exemplars,
                n_samples=self.self_consistency_samples, max_tokens=self.max_tokens)
            self.log.malformed_retries += retries
        except Exception as exc:
            # A shadow measurement must never fail the extraction it shadows:
            # the stored value is the exhibit's and does not depend on this.
            return ShadowComparison(rule_value=external, model_value=None,
                                     rule_span=None, model_span=None,
                                     agreement=False,
                                     note="shadow model call failed: %s" % exc)
        tol = float(spec_check.get("tolerance_pct", 0)) / 100.0
        agree = False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            agree = abs(value - external) <= abs(external) * tol
        return ShadowComparison(
            rule_value=external, model_value=value,
            rule_span=None, model_span=span, agreement=agree,
            note=None if agree else "exhibit=%r vs model=%r" % (external, value))

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
                        exemplars, ex_version = _lookup_exemplars(self.exemplars, form, issuer, field_name)
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
            shadow = self._shadow_external(f, text, ex107, issuer=issuer,
                                            form=form, table_context=table_context)
            return self._finish(f, ex107[field_name], None, "xbrl", prov, 1.0,
                                 None, ex107, accession=accession, document=document,
                                 shadow=shadow)

        if self.local_client is None:
            raise RuntimeError(
                f"no local client configured; {field_name!r} cannot reach the "
                f"local rung (#103 must be wired up before #104 can escalate)."
            )

        # Rung 3: local 7B, with (form, issuer, field) exemplars if available (#106).
        exemplars, ex_version = _lookup_exemplars(self.exemplars, form, issuer, field_name)
        exemplar_set = ex_version if ex_version is not None else self.exemplar_set_version
        self._check_static_prefix(f, exemplars, ex_version, issuer, field_name)
        value, span, conf, samples, tin, tout, retries = _extract_via_model(
            self.local_client, self.local_model, f, text, table_context=table_context,
            exemplars=exemplars, n_samples=self.self_consistency_samples,
            max_tokens=self.max_tokens)
        self.log.malformed_retries += retries

        gate = evaluate_gate(f, value, span, ex107=ex107, spec=self.spec, samples=samples,
                              model_confidence=conf, confidence_floor=self.confidence_floor,
                              source_text=text)

        if not gate.escalate:
            prov = Provenance(rung="local", document=document, span=span, model=self.local_model,
                               prompt_version=self.prompt_version,
                               exemplar_set=exemplar_set,
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
                               exemplar_set=exemplar_set,
                               tokens_in=ctin, tokens_out=ctout, cost_usd=cost)
            return self._finish(f, c_value, c_span, "claude", prov, c_conf, gate, ex107,
                                 accession=accession, document=document, escalated=True)

        # Local-only mode: flag the gate failure, keep going -- never raise.
        prov = Provenance(rung="local", document=document, span=span, model=self.local_model,
                           prompt_version=self.prompt_version,
                           exemplar_set=exemplar_set, tokens_in=tin, tokens_out=tout)
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
        # The gate has already had its say on an unreadable confidence; what
        # lands in the store must be a probability or nothing, so `Results`
        # and `Coverage` can never average a 1.6e+30 into a score.
        if confidence == INVALID_CONFIDENCE:
            confidence = None
            flags_extra = [Flag(f.name, "confidence_uninterpretable", "warn",
                                "the model reported a confidence outside [0,1]; it is "
                                "discarded and this value is treated as unverified")]
        else:
            flags_extra = []
        # A date-typed field is declared ISO but stated in prose -- "August 31,
        # 2028" -- so normalize before anything checks or stores it (#166).
        # Without this every date tripped type_mismatch and was unusable: not
        # comparable, not sortable, and invisible to the date_after cross-check
        # against pricing_date, which therefore never ran either.
        value, date_flags = _normalize_dateish(f, value)
        flags_extra = list(flags_extra) + date_flags
        flags = self.spec.check_value(f.name, value, ex107=ex107) if value is not None else []
        flags = list(flags) + flags_extra
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
            body = json.dumps({"ev1000": {WIRE_VALUE_KEY: self.value,
                                           WIRE_SPAN_KEY: list(self.span) if self.span else None,
                                           WIRE_CONF_KEY: self.conf}})
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
