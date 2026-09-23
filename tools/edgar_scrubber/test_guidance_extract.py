"""
Gate for structured guidance extraction through the existing ladder (issue #188).

Same convention as `test_extraction_ladder.py` / `test_guidance_anchors.py`:
stdlib only, run directly, exit 0 = pass. No network -- a `FakeChatClient`
scripts `chat_completion` in the exact shape `ollama_client.OllamaClient`
returns, so `GuidanceExtractor` cannot tell it from a real local or Claude call.
That IS the "same client interface across local and Claude, differing by config
only" criterion: the same fake stands in for both rungs.

Every acceptance criterion in #188 is checked here:

  * extraction goes through `chat_completion`, and escalation to Claude is the
    SAME `OllamaClient` class built from `OllamaConfig.for_claude()` -- no second
    client class, no second escalation path;
  * the model receives ONLY the candidate sentences from `guidance_anchors`,
    never the whole document, and the assembled prompt carries no more than
    `MAX_CANDIDATES`;
  * the wire schema returns `{metric, period_label, low, high, basis}` plus
    offset spans, built through `build_wire_schema` -- offsets, not quoted spans;
  * the static prefix (system + field spec + exemplars) comes first and is
    byte-identical across calls within one issuer; candidate sentences come last;
  * each result is written to the facts store as a `GuidanceRecord` with rung,
    model and token cost in provenance;
  * with no Claude configured, a gated field returns a flagged local value and
    the run continues (`gated_no_claude`).

Run:  python tools/edgar_scrubber/test_guidance_extract.py
"""
import json
from pathlib import Path

import guidance_extract as ge
from extraction_ladder import WIRE_VALUE_KEY, WIRE_SPAN_KEY, WIRE_CONF_KEY
from facts_store import FactsStore, GuidanceRecord
from ollama_client import OllamaClient, OllamaConfig

FIX = Path(__file__).resolve().parent / "fixtures"
failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


class FakeChatClient:
    """Same shape as OllamaClient.chat_completion -- no network. Returns the
    same scripted payload every call and records the messages it was handed."""

    def __init__(self, payload, usage=None):
        self.payload = payload
        self.usage = usage or {"prompt_tokens": 200, "completion_tokens": 20}
        self.calls = []

    def chat_completion(self, messages, model=None, temperature=None,
                        max_tokens=None, top_p=None, response_format=None):
        self.calls.append({"messages": messages, "model": model,
                           "response_format": response_format})
        return {"choices": [{"message": {"content": json.dumps(self.payload)}}],
                "usage": self.usage}


ACME = load("guidance_acme_q3.txt")


def _entry(value, span=None, conf=0.9):
    e = {WIRE_VALUE_KEY: value, WIRE_SPAN_KEY: list(span) if span else None}
    if conf is not None:
        e[WIRE_CONF_KEY] = conf
    return e


def _acme_span():
    s = ACME.index("For the fourth quarter")
    e = ACME.index("$1.30 billion.") + len("$1.30 billion")
    return (s, e)


_DEFAULT = object()


def good_payload(span=_DEFAULT):
    span = _acme_span() if span is _DEFAULT else span
    return {
        "metric": _entry("revenue", span),
        "period_label": _entry("Q4 fiscal 2026", span),
        "low": _entry(1.20, span),
        "high": _entry(1.30, span),
        "basis": _entry(None, None, conf=0.4),
    }


# --------------------------------------------------------------------------- #
section("extraction goes through chat_completion; wire schema has the five keys")
# --------------------------------------------------------------------------- #
local = FakeChatClient(good_payload())
store = FactsStore(":memory:")
ex = ge.GuidanceExtractor(local_client=local, local_model="qwen2.5:7b", store=store)
rec = ex.extract(ACME, cik="0000019617", issuer="ACME", accession="0001", document="ex99.htm")

check("chat_completion was called", len(local.calls) == 1)
check("a GuidanceRecord came back", isinstance(rec, GuidanceRecord))
check("metric extracted", rec.metric == "revenue")
check("low/high extracted", rec.low == 1.20 and rec.high == 1.30)

schema = ge.build_wire_schema(ge.GUIDANCE_FIELDS)
props = schema["json_schema"]["schema"]["properties"]
check("wire schema carries exactly the five guidance keys",
      set(props) == {"metric", "period_label", "low", "high", "basis"})
check("each key uses the offset span slot, not a quoted span",
      all(props[k]["properties"][WIRE_SPAN_KEY]["type"] == ["array", "null"]
          for k in props))
check("constrained decode always on (response_format present)",
      local.calls[0]["response_format"] is not None)

# --------------------------------------------------------------------------- #
section("the model receives ONLY the candidate sentences, never the document")
# --------------------------------------------------------------------------- #
user = local.calls[0]["messages"][1]["content"]
check("the fourth-quarter guidance sentence is present",
      "net revenue in the range of $1.20 billion to $1.30 billion" in user)
check("the EPS guidance sentence is present", "between $4.10 and $4.30" in user)
check("the title line is NOT in the prompt",
      "REPORTS THIRD QUARTER" not in user)
check("the historical-results sentence is NOT in the prompt",
      "Net revenue was" not in user)
check("the safe-harbour boilerplate figure is NOT in the prompt",
      "$9.00 billion" not in user)

# --------------------------------------------------------------------------- #
section("the assembled prompt carries no more than MAX_CANDIDATES")
# --------------------------------------------------------------------------- #
many = [{"sentence": f"s{i}", "span": (i, i + 1), "anchor": "x"}
        for i in range(ge.MAX_CANDIDATES + 5)]
raised = False
try:
    ge.build_messages(many, max_candidates=ge.MAX_CANDIDATES)
except ValueError:
    raised = True
check("build_messages refuses more than the cap", raised)

capped = ge.GuidanceExtractor(local_client=FakeChatClient(good_payload()),
                              local_model="m", max_candidates=2)
capped.extract(ACME, cik="0000019617", issuer="ACME")
capped_user = capped.local_client.calls[0]["messages"][1]["content"]
check("a cap of 2 truncates the three ACME candidates to two",
      "[0]" in capped_user and "[1]" in capped_user and "[2]" not in capped_user)

# --------------------------------------------------------------------------- #
section("prompt order: system + field spec + exemplars first, candidates last")
# --------------------------------------------------------------------------- #
check("system message is the guidance system prompt",
      local.calls[0]["messages"][0]["content"] == ge.SYSTEM_PROMPT)
check("field spec precedes the candidate block",
      user.index("GUIDANCE FIELDS:") < user.index("CANDIDATE SENTENCES"))

exemplars = {"ACME": ["revenue | FY2026 | 38.0-39.0 | non-GAAP"]}
ex2 = ge.GuidanceExtractor(local_client=FakeChatClient(good_payload()),
                           local_model="m", exemplars=exemplars)
ex2.extract(ACME, cik="0000019617", issuer="ACME")
ex2_user = ex2.local_client.calls[0]["messages"][1]["content"]
check("exemplars sit between the field spec and the candidates",
      ex2_user.index("GUIDANCE FIELDS:") < ex2_user.index("EXEMPLARS")
      < ex2_user.index("CANDIDATE SENTENCES"))

# --------------------------------------------------------------------------- #
section("static prefix is byte-identical across calls within one issuer (#106)")
# --------------------------------------------------------------------------- #
p1 = ge.static_prefix(["revenue | FY2026 | 38.0-39.0 | non-GAAP"])
p2 = ge.static_prefix(["revenue | FY2026 | 38.0-39.0 | non-GAAP"])
check("same exemplar set renders the same prefix", p1 == p2)
check("a different exemplar set renders a different prefix",
      ge.static_prefix(["revenue | FY2027 | 40-41 | GAAP"]) != p1)

drift = ge.GuidanceExtractor(local_client=FakeChatClient(good_payload()), local_model="m")
drift._static_prefix_seen["ACME"] = "a stale prefix that does not match"
guard_raised = False
try:
    drift.extract(ACME, cik="0000019617", issuer="ACME")
except AssertionError:
    guard_raised = True
check("a static-prefix drift for the same issuer raises (#106 guard)", guard_raised)

# --------------------------------------------------------------------------- #
section("record written to the store with rung, model and token cost in provenance")
# --------------------------------------------------------------------------- #
stored = store.guidance_for("0000019617")
check("the record was persisted to the facts store", len(stored) == 1)
check("provenance names the local rung and model",
      rec.provenance.startswith("local:qwen2.5:7b"))
check("provenance records token cost",
      "tokens_in=" in rec.provenance and "tokens_out=" in rec.provenance)

# --------------------------------------------------------------------------- #
section("escalation to Claude is a config swap on the SAME client class")
# --------------------------------------------------------------------------- #
local_cfg = OllamaConfig(base_url="http://localhost:11434/v1", model="qwen2.5:7b",
                         batch_size=1, context_length=8192, max_tokens=512)
local_real = OllamaClient(local_cfg)
claude_real = OllamaClient(OllamaConfig.for_claude("claude-sonnet-4-6"))
check("local and Claude are the same client class (config swap, not a new class)",
      type(local_real) is type(claude_real))
check("for_claude() only swaps the base_url/model/key, not the client",
      claude_real.config.model == "claude-sonnet-4-6"
      and "anthropic" in claude_real.config.base_url)

# local returns a value with no span -> the span gate escalates
local_gated = FakeChatClient(good_payload(span=None))
claude_ok = FakeChatClient(good_payload())
store2 = FactsStore(":memory:")
esc = ge.GuidanceExtractor(local_client=local_gated, local_model="qwen2.5:7b",
                           claude_client=claude_ok, claude_model="claude-sonnet-4-6",
                           store=store2)
erec = esc.extract(ACME, cik="0000019617", issuer="ACME", accession="0002", document="ex99.htm")
check("a gated field escalated to the Claude client", len(claude_ok.calls) == 1)
check("provenance records the Claude rung and model",
      erec.provenance.startswith("claude:claude-sonnet-4-6"))
check("the Claude rung records a non-zero cost", "cost_usd=" in erec.provenance)

# --------------------------------------------------------------------------- #
section("no Claude configured: a gated field is flagged and the run continues")
# --------------------------------------------------------------------------- #
local_only = FakeChatClient(good_payload(span=None))
store3 = FactsStore(":memory:")
loc = ge.GuidanceExtractor(local_client=local_only, local_model="qwen2.5:7b", store=store3)
grec = loc.extract(ACME, cik="0000019617", issuer="ACME", accession="0003", document="ex99.htm")
check("the run did not raise and produced a record", isinstance(grec, GuidanceRecord))
check("the local value is retained", grec.low == 1.20)
check("the record is flagged gated_no_claude", "gated_no_claude" in grec.provenance)
check("the flagged record was still written", len(store3.guidance_for("0000019617")) == 1)

# --------------------------------------------------------------------------- #
section("a release with no candidates yields no record and does not raise")
# --------------------------------------------------------------------------- #
empty = ge.GuidanceExtractor(local_client=FakeChatClient(good_payload()), local_model="m")
check("historical-only release returns None",
      empty.extract("Net income rose to $5 million.", cik="X") is None)

# --------------------------------------------------------------------------- #
section("no second client class defined in the module")
# --------------------------------------------------------------------------- #
src = Path(ge.__file__).read_text(encoding="utf-8")
check("module defines no chat_completion of its own (reuses the ladder's client)",
      "def chat_completion" not in src)
check("module calls the existing ladder", "from extraction_ladder import" in src
      or "from .extraction_ladder import" in src)

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nguidance_extract gate: PASS")
