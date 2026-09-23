"""
Structured guidance extraction through the existing ladder (issue #188, EPIC #95).

`guidance_anchors` (#187) LOCATES the handful of forward-looking sentences in an
earnings release with zero tokens. This module turns those sentences into
structured `GuidanceRecord`s (#183) -- {metric, period_label, low, high, basis}
plus source offsets -- and it does so through `extraction_ladder`'s existing
machinery rather than a parallel path of its own:

  * the model is called through `ollama_client.OllamaClient.chat_completion`, the
    same client the ladder uses, and escalation to Claude is the same config swap
    the ladder makes -- `OllamaConfig.for_claude()` -- not a second client class
    and not a second escalation path (#104);
  * the wire schema is built by `extraction_ladder.build_wire_schema`, so the
    guidance fields inherit the OFFSET convention (a `[start, end]` pair, ~5
    tokens, not the ~50 a quoted span costs) the ladder already established;
  * prompt assembly preserves the ladder's ORDER-SENSITIVE discipline (#106):
    everything static (system prompt, the guidance field spec, per-issuer
    exemplars) comes first and is byte-identical within an issuer, and the only
    thing that varies call to call -- the candidate sentences -- comes LAST, so
    llama.cpp/Ollama reuse the KV cache for the prefix;
  * the confidence gate (`extraction_ladder.evaluate_gate`) decides escalation on
    checkable signals, and a gated field with no Claude configured returns a
    flagged local value and the run continues, exactly as the ladder's
    `gated_no_claude` behaviour does (#104).

Crucially the model NEVER sees the whole document. It sees only the candidate
sentences `guidance_anchors` flagged, capped at `MAX_CANDIDATES`; the assembled
prompt asserts it carries no more than that.

stdlib + the sibling modules only. Run the self-check:
    python tools/edgar_scrubber/guidance_extract.py
"""

try:  # package import: tools.edgar_scrubber.guidance_extract
    from .extraction_ladder import (
        build_wire_schema, chat_json, parse_ladder_response, evaluate_gate,
        estimate_cost, coerce_confidence, INVALID_CONFIDENCE,
        WIRE_VALUE_KEY, WIRE_SPAN_KEY, WIRE_CONF_KEY,
    )
    from .field_spec import FieldDefinition
    from .facts_store import GuidanceRecord
    from .guidance_anchors import find_candidates
except ImportError:  # standalone: python tools/edgar_scrubber/guidance_extract.py
    from extraction_ladder import (
        build_wire_schema, chat_json, parse_ladder_response, evaluate_gate,
        estimate_cost, coerce_confidence, INVALID_CONFIDENCE,
        WIRE_VALUE_KEY, WIRE_SPAN_KEY, WIRE_CONF_KEY,
    )
    from field_spec import FieldDefinition
    from facts_store import GuidanceRecord
    from guidance_anchors import find_candidates


#: A candidate list is already the cheap end of the prompt (#187), but a release
#: that trips a dozen anchors is a release whose "guidance" is mostly the model's
#: to disentangle, and past that the cap protects the token budget the whole
#: anchor rung exists to defend. Candidates past this are dropped before the
#: prompt is assembled, and `build_messages` ASSERTS the cap as an invariant.
MAX_CANDIDATES = 12


#: The five slots of a guidance record, as `field_spec.FieldDefinition`s so
#: `build_wire_schema` produces exactly `{metric, period_label, low, high, basis}`
#: each carrying the ladder's `{value, span, confidence}` -- offsets, not quoted
#: text. `description` is sent to the model; `low`/`high` are numbers so the wire
#: schema constrains them numeric, and `span` is the offset pair for the sentence
#: the value was read from.
GUIDANCE_FIELDS = (
    FieldDefinition(
        name="metric", type="string", extraction_path="prose-guidance", sections=(),
        description="What is being guided, e.g. revenue, EPS, operating margin. "
                    "Use the release's own words for the line item."),
    FieldDefinition(
        name="period_label", type="string", extraction_path="prose-guidance", sections=(),
        description="The filer's own words for the period the guidance covers, "
                    "e.g. 'FY2026', 'Q4 2025', 'full year'. A label, not dates."),
    FieldDefinition(
        name="low", type="number", extraction_path="prose-guidance", sections=(),
        description="The low end of the guided range, as the number the sentence "
                    "prints (a point estimate sets low and high equal)."),
    FieldDefinition(
        name="high", type="number", extraction_path="prose-guidance", sections=(),
        description="The high end of the guided range, as the number the sentence "
                    "prints (a point estimate sets low and high equal)."),
    FieldDefinition(
        name="basis", type="string", extraction_path="prose-guidance", sections=(),
        description="The accounting basis stated, e.g. GAAP, non-GAAP, constant "
                    "currency. Use null when the sentence states none."),
)

#: Which of the five carries the number the gate keys on. `low` is always a
#: guided figure with a span in the source sentence, so it is the field whose
#: bound/span signals decide escalation for the record as a whole.
_GATE_FIELD = next(f for f in GUIDANCE_FIELDS if f.name == "low")


# No "respond only in JSON" -- constrained decode makes it structural (#111), and
# a module-level constant means `static_prefix` builds the EXACT text a real call
# sends. Every slot is described and the offset convention spelled out, mirroring
# `extraction_ladder.SYSTEM_PROMPT` but for the five guidance fields at once.
SYSTEM_PROMPT = (
    "You are an SEC EDGAR guidance extraction engine. Below is a numbered list "
    "of candidate sentences taken from an earnings release, each prefixed with "
    "its [start, end] character offsets into the source document. From these "
    "sentences ONLY, extract the forward-looking guidance: the metric, the "
    "period it covers, the low and high of the guided range, and the accounting "
    "basis. "
    f"For each field, `{WIRE_VALUE_KEY}` is the extracted value in the "
    "document's own units -- a percentage as the percent figure written, a "
    "dollar figure as written -- or null when the sentences do not state it. "
    f"`{WIRE_SPAN_KEY}` is the [start, end] character offset pair into the "
    "source document for the sentence that supports the value, taken from the "
    "offsets printed with each candidate, or null if you cannot locate one -- "
    "never guess a span. "
    f"`{WIRE_CONF_KEY}` is how sure you are, 0 to 1. It describes the value; it "
    f"is never itself the answer. Never copy `{WIRE_CONF_KEY}` into "
    f"`{WIRE_VALUE_KEY}`."
)


def _field_spec_block():
    """The static field-spec portion of the prompt: one line per guidance field,
    identical on every call regardless of which release is being read."""
    lines = ["GUIDANCE FIELDS:"]
    for f in GUIDANCE_FIELDS:
        lines.append(f"- {f.name} ({f.type}): {f.description}")
    return "\n".join(lines)


def _exemplar_lines(exemplars, issuer):
    """Per-issuer exemplar lines (#106), or None. `exemplars` may be a callable
    `issuer -> lines` or a dict `{issuer: lines}`; anything else yields None."""
    if not exemplars or issuer is None:
        return None
    if callable(exemplars) and not isinstance(exemplars, dict):
        return exemplars(issuer) or None
    return exemplars.get(issuer) or None


def _exemplar_block(lines):
    if not lines:
        return None
    return "EXEMPLARS for this issuer (#106):\n" + "\n".join(f"- {e}" for e in lines)


def _static_user_parts(exemplar_lines):
    parts = [_field_spec_block()]
    block = _exemplar_block(exemplar_lines)
    if block:
        parts.append(block)
    return parts


def static_prefix(exemplar_lines=None):
    """The byte-identical-within-an-issuer prefix (#106): system prompt + the
    guidance field spec + per-issuer exemplars, and nothing document-specific.
    A pure function of the exemplar set, so the same issuer renders the same
    prefix on every call and the KV cache is reused -- see `build_messages`,
    which appends the varying candidate block AFTER this."""
    return SYSTEM_PROMPT + "\n\n" + "\n\n".join(_static_user_parts(exemplar_lines))


def build_candidate_block(candidates):
    """One line per candidate: `[i] (start-end) sentence`. The offsets are the
    candidate's own document span (#187), printed so the model can return them in
    its `span` slot -- the offset convention, never a quoted span."""
    lines = ["CANDIDATE SENTENCES (extract only from these):"]
    for i, c in enumerate(candidates):
        start, end = c["span"]
        lines.append(f"[{i}] ({start}-{end}) {c['sentence']}")
    return "\n".join(lines)


def build_messages(candidates, *, exemplar_lines=None, max_candidates=MAX_CANDIDATES):
    """Static prefix first (system + field spec + exemplars, identical across
    releases for one issuer), the VARYING candidate sentences LAST (#106). The
    model receives ONLY these sentences, never the document, and the cap is an
    invariant: more than `max_candidates` is a caller bug, not something to
    silently truncate here."""
    if len(candidates) > max_candidates:
        raise ValueError(
            f"prompt would carry {len(candidates)} candidates, over the "
            f"maximum of {max_candidates}; the caller must cap before assembling")
    parts = list(_static_user_parts(exemplar_lines))
    parts.append(build_candidate_block(candidates))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _first_span(parsed):
    """The first non-null offset span across the five fields -- the sentence the
    record was read from. All five typically point at the same guidance sentence;
    the first present one is the record's source span."""
    for f in GUIDANCE_FIELDS:
        entry = parsed.get(f.name)
        if entry and entry[1] is not None:
            return entry[1]
    return None


def _provenance_string(rung, model, tokens_in, tokens_out, cost_usd, prompt_version):
    """Compact provenance for `GuidanceRecord.provenance` carrying rung, model
    and token cost (#188 AC5). Same `rung:model@k=v` shape the ladder's
    `Provenance.as_string` uses, extended with the token counts a guidance run
    is measured on."""
    bits = [f"{rung}:{model}", f"prompt={prompt_version}",
            f"tokens_in={tokens_in}", f"tokens_out={tokens_out}"]
    if cost_usd:
        bits.append(f"cost_usd={cost_usd:.6f}")
    return "@".join(bits)


class GuidanceExtractor:
    """Turn located candidate sentences into `GuidanceRecord`s through the
    existing ladder. One local client, one optional Claude client that is the
    SAME class pointed at Anthropic via `OllamaConfig.for_claude()` -- see the
    module docstring. Not a second escalation path: the gate and the client are
    the ladder's."""

    def __init__(self, *, local_client, local_model=None,
                 claude_client=None, claude_model=None, claude_enabled=True,
                 store=None, exemplars=None, confidence_floor=0.35,
                 max_candidates=MAX_CANDIDATES, max_tokens=512, prompt_version="v1"):
        self.local_client = local_client
        self.local_model = local_model or getattr(
            getattr(local_client, "config", None), "model", None)
        self.claude_client = claude_client
        self.claude_model = claude_model or getattr(
            getattr(claude_client, "config", None), "model", None)
        self.claude_enabled = claude_enabled
        self.store = store
        self.exemplars = exemplars
        self.confidence_floor = confidence_floor
        self.max_candidates = max_candidates
        self.max_tokens = max_tokens
        self.prompt_version = prompt_version
        self.malformed_retries = 0
        # (issuer) -> last static prefix seen, the #106 KV-cache guard.
        self._static_prefix_seen = {}

    @property
    def claude_available(self):
        return bool(self.claude_enabled and self.claude_client is not None)

    def _call(self, client, model, messages, schema):
        payload, usage, retries = chat_json(
            client, model, messages, schema, temperature=0.0,
            max_tokens=self.max_tokens)
        self.malformed_retries += retries
        parsed = parse_ladder_response(GUIDANCE_FIELDS, payload)
        return parsed, (usage.get("prompt_tokens") or 0), (usage.get("completion_tokens") or 0)

    def _check_static_prefix(self, issuer, exemplar_lines):
        """The #106 invariant: same issuer, same static prefix, every call.
        A drift means something started interpolating candidate text ahead of
        the exemplar block and would triple prefill cost -- fail loudly."""
        if issuer is None:
            return
        prefix = static_prefix(exemplar_lines)
        prior = self._static_prefix_seen.get(issuer)
        if prior is not None and prior != prefix:
            raise AssertionError(
                f"static guidance prompt prefix changed for issuer={issuer!r} "
                f"-- this breaks KV-cache prefix reuse (#106/#111). A given "
                f"issuer must always render the same static prefix.")
        self._static_prefix_seen[issuer] = prefix

    def extract(self, text, *, cik, issuer=None, accession=None, document=None):
        """Extract one `GuidanceRecord` from `text` (an already-normalized
        release), or None when the located sentences carry no guidance.

        Runs `guidance_anchors.find_candidates` to LOCATE, caps to
        `max_candidates`, calls the local rung, gates, and escalates to Claude
        through the config-swap client when the gate asks and one is configured.
        With no Claude configured a gated result is flagged `gated_no_claude`,
        written anyway, and the run continues.
        """
        issuer = issuer if issuer is not None else cik
        found = find_candidates(text)
        candidates = found.candidates[:self.max_candidates]
        if not candidates:
            return None

        exemplar_lines = _exemplar_lines(self.exemplars, issuer)
        self._check_static_prefix(issuer, exemplar_lines)
        schema = build_wire_schema(GUIDANCE_FIELDS)
        messages = build_messages(candidates, exemplar_lines=exemplar_lines,
                                  max_candidates=self.max_candidates)

        parsed, tin, tout = self._call(self.local_client, self.local_model, messages, schema)
        rung, model, span_source = "local", self.local_model, text
        cost_usd = 0.0
        gated = False

        low_value, low_span, low_conf = parsed.get(_GATE_FIELD.name, (None, None, None))
        gate = evaluate_gate(_GATE_FIELD, low_value, low_span, spec=None,
                             model_confidence=low_conf,
                             confidence_floor=self.confidence_floor, source_text=text)

        if gate.escalate:
            if self.claude_available:
                parsed, ctin, ctout = self._call(
                    self.claude_client, self.claude_model, messages, schema)
                rung, model = "claude", self.claude_model
                tin, tout = ctin, ctout
                cost_usd = estimate_cost(self.claude_model, ctin, ctout)
            else:
                gated = True  # gated_no_claude: keep the local value, flag it

        record = self._to_record(parsed, cik=cik, accession=accession,
                                  document=document, rung=rung, model=model,
                                  tokens_in=tin, tokens_out=tout, cost_usd=cost_usd,
                                  gated=gated)
        if record is not None and self.store is not None:
            self.store.put_guidance([record])
        return record

    def _to_record(self, parsed, *, cik, accession, document, rung, model,
                   tokens_in, tokens_out, cost_usd, gated):
        def val(name):
            entry = parsed.get(name)
            return entry[0] if entry else None

        metric = val("metric")
        low, high = val("low"), val("high")
        # Nothing forward-looking actually came back: no metric and no numbers.
        if metric is None and low is None and high is None:
            return None

        conf = None
        low_entry = parsed.get("low")
        if low_entry and low_entry[2] not in (None, INVALID_CONFIDENCE):
            conf = low_entry[2]

        provenance = _provenance_string(rung, model, tokens_in, tokens_out,
                                        cost_usd, self.prompt_version)
        if gated:
            provenance += "@gated_no_claude"

        return GuidanceRecord(
            cik=cik, metric=metric, period_label=val("period_label"),
            low=low, high=high, basis=val("basis"),
            accession=accession, document=document,
            span=_first_span(parsed), provenance=provenance, confidence=conf,
        )


if __name__ == "__main__":
    # No self-check demo lives here on purpose: exercising the extractor needs a
    # scripted `chat_completion`, and a fake client class in this file would
    # muddy the one thing #188 guarantees -- that guidance introduces no second
    # model client. The offline gate, fake and all, is `test_guidance_extract.py`.
    print("guidance_extract: run `python tools/edgar_scrubber/test_guidance_extract.py`")
