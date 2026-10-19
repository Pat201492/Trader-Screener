"""
Gate for the research assistant planner (issue #280). Same convention as
`test_extraction_ladder.py`: stdlib only, run directly, exit 0 = pass, with a
`test_research_assistant` wrapper so `pytest tools/edgar_scrubber` runs it too.
No network and no API key -- every provider is a FAKE scripted in-process, which
is the whole point of the module: the planner must be exercisable (and the
validator provable) without ever touching Ollama, Anthropic, or EDGAR.

Every acceptance criterion in #280 is checked here:

  * a fake provider returning a valid plan -> Plan whose searches equal the
    fake's, with exactly one provider call made;
  * a bad form code, startdt > enddt, a 300-char q, and 7 searches are each
    dropped with a recorded reason, and at most 5 are kept;
  * a non-JSON reply -> Plan with no searches and a reason, nothing raised;
  * OllamaProvider sends response_format carrying the schema; ClaudeProvider
    reads its model from ANTHROPIC_RESEARCH_MODEL with the stated default;
  * claude_available() is false with a reason when ANTHROPIC_API_KEY is unset.

Run:  python tools/edgar_scrubber/test_research_assistant.py
"""
import datetime
import json
import os

import research_assistant as ra

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


# A fixed "today" so the 25-year window is deterministic regardless of run date.
TODAY = datetime.date(2026, 10, 18)


class FakeProvider(ra.Provider):
    """Returns a scripted raw string and counts how many times it was called.
    Stands in for both OllamaProvider and ClaudeProvider -- plan() only sees
    the Provider.complete contract."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0
        self.last = None

    def complete(self, system, messages):
        self.calls += 1
        self.last = (system, messages)
        return self.raw


class FakeOllamaClient:
    """Records the kwargs chat_completion is called with; returns a canned
    OpenAI-shaped response. The exact shape ollama_client.OllamaClient returns."""

    class _Cfg:
        model = "qwen2.5:7b-instruct-q4_K_M"

    config = _Cfg()

    def __init__(self, content='{"reply": "ok", "searches": []}'):
        self.content = content
        self.seen = None

    def chat_completion(self, **kwargs):
        self.seen = kwargs
        return {"choices": [{"message": {"content": self.content}}]}


class FakeAnthropicMessage:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [type("Block", (), {"type": "text", "text": text})()]


class FakeAnthropicClient:
    def __init__(self, text='{"reply": "ok", "searches": []}'):
        self.text = text
        self.seen = None
        self.messages = self

    def create(self, **kwargs):
        self.seen = kwargs
        return FakeAnthropicMessage(self.text)


def _valid_plan_json():
    searches = [
        {"q": "contingent coupon", "forms": ["424B2"], "ciks": ["MS"],
         "startdt": "2024-01-01", "enddt": "2024-12-31",
         "rationale": "structured note pricing supplements"},
        {"q": "buffered return", "forms": [], "ciks": [],
         "startdt": "", "enddt": "", "rationale": "broader sweep"},
    ]
    return json.dumps({"reply": "Here are two searches.", "searches": searches})


def run_checks():
    # -- valid plan: searches round-trip, exactly one call -------------------
    fp = FakeProvider(_valid_plan_json())
    p = ra.plan(fp, "find autocallable notes from Morgan Stanley")
    check("valid plan: exactly one provider call", fp.calls == 1)
    check("valid plan: reply carried through", p.reply == "Here are two searches.")
    check("valid plan: two searches kept, none dropped",
          len(p.searches) == 2 and not p.dropped)
    expected = ra.validate_searches(json.loads(_valid_plan_json())["searches"])[0]
    check("valid plan: searches equal the fake's", p.searches == expected)
    check("valid plan: user message appended to history",
          fp.last[1][-1] == {"role": "user",
                             "content": "find autocallable notes from Morgan Stanley"})

    # -- bad entries each dropped with a reason, at most 5 kept --------------
    raw = {
        "reply": "mixed bag",
        "searches": (
            [{"q": "ok " + str(i), "forms": [], "ciks": [], "startdt": "",
              "enddt": "", "rationale": "r"} for i in range(5)]
            + [
                {"q": "bad form", "forms": ["FORM!"], "ciks": [], "startdt": "",
                 "enddt": "", "rationale": "r"},
                {"q": "backwards dates", "forms": [], "ciks": [],
                 "startdt": "2024-12-31", "enddt": "2024-01-01", "rationale": "r"},
                {"q": "x" * 300, "forms": [], "ciks": [], "startdt": "",
                 "enddt": "", "rationale": "r"},
            ]
        ),
    }
    kept, dropped = ra.validate_searches(raw["searches"], today=TODAY)
    check("bad entries: at most 5 kept", len(kept) == 5)
    check("bad entries: three dropped", len(dropped) == 3)
    check("bad entries: bad form has a reason",
          any("invalid form code" in d for d in dropped))
    check("bad entries: startdt>enddt has a reason",
          any("after enddt" in d for d in dropped))
    check("bad entries: 300-char q has a reason",
          any("300 characters" in d for d in dropped))

    # 7 clean searches -> 5 kept, 2 dropped as past the cap.
    seven = [{"q": f"q{i}", "forms": [], "ciks": [], "startdt": "", "enddt": "",
              "rationale": "r"} for i in range(7)]
    kept7, dropped7 = ra.validate_searches(seven, today=TODAY)
    check("seven searches: five kept", len(kept7) == 5)
    check("seven searches: two dropped past the cap",
          len(dropped7) == 2 and all("more than 5" in d for d in dropped7))

    # -- ticker vs CIK -------------------------------------------------------
    cks = [{"q": "q", "forms": [], "ciks": ["AAPL", "320193"], "startdt": "",
            "enddt": "", "rationale": "r"}]
    k, d = ra.validate_searches(cks, today=TODAY)
    check("ciks: ticker and numeric CIK both accepted and upper-cased",
          k and k[0].ciks == ["AAPL", "320193"] and not d)
    badcik = [{"q": "q", "forms": [], "ciks": ["not a cik!"], "startdt": "",
               "enddt": "", "rationale": "r"}]
    _, dbad = ra.validate_searches(badcik, today=TODAY)
    check("ciks: a junk cik drops the entry", any("invalid ticker or CIK" in x for x in dbad))

    # -- date window ---------------------------------------------------------
    old = [{"q": "q", "forms": [], "ciks": [], "startdt": "1990-01-01",
            "enddt": "2024-01-01", "rationale": "r"}]
    _, dold = ra.validate_searches(old, today=TODAY)
    check("dates: >25 years ago is dropped", any("25 years" in x for x in dold))
    future = [{"q": "q", "forms": [], "ciks": [], "startdt": "",
               "enddt": "2030-01-01", "rationale": "r"}]
    _, dfut = ra.validate_searches(future, today=TODAY)
    check("dates: a future date is dropped", any("future" in x for x in dfut))

    # -- non-JSON reply degrades to an empty plan, no raise ------------------
    try:
        p2 = ra.plan(FakeProvider("I'm sorry, I cannot do that."), "hi")
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("non-JSON: nothing raised to the caller", not raised)
    check("non-JSON: no searches", not p2.searches)
    check("non-JSON: a reason recorded", len(p2.dropped) == 1 and "not valid JSON" in p2.dropped[0])

    # -- OllamaProvider sends response_format carrying the schema ------------
    foc = FakeOllamaClient()
    op = ra.OllamaProvider(foc)
    op.complete(ra.SYSTEM_PROMPT, [{"role": "user", "content": "q"}])
    rf = foc.seen.get("response_format")
    check("ollama: response_format present",
          isinstance(rf, dict) and rf.get("type") == "json_schema")
    check("ollama: response_format carries PLAN_SCHEMA",
          rf["json_schema"]["schema"] is ra.PLAN_SCHEMA)
    check("ollama: system prompt is the first message",
          foc.seen["messages"][0] == {"role": "system", "content": ra.SYSTEM_PROMPT})

    # -- ClaudeProvider model from env with the stated default ---------------
    saved = os.environ.get("ANTHROPIC_RESEARCH_MODEL")
    try:
        os.environ.pop("ANTHROPIC_RESEARCH_MODEL", None)
        cp = ra.ClaudeProvider(client=FakeAnthropicClient())
        check("claude: default model is claude-sonnet-5-5",
              cp.model == "claude-sonnet-5-5")
        os.environ["ANTHROPIC_RESEARCH_MODEL"] = "claude-opus-5"
        cp2 = ra.ClaudeProvider(client=FakeAnthropicClient())
        check("claude: model read from ANTHROPIC_RESEARCH_MODEL",
              cp2.model == "claude-opus-5")
        cp2.complete(ra.SYSTEM_PROMPT, [{"role": "user", "content": "q"}])
        check("claude: schema passed via output_config",
              cp2.client.seen["output_config"]["format"]["schema"] is ra.PLAN_SCHEMA)
        check("claude: model passed through to the SDK call",
              cp2.client.seen["model"] == "claude-opus-5")
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_RESEARCH_MODEL", None)
        else:
            os.environ["ANTHROPIC_RESEARCH_MODEL"] = saved

    # -- claude_available() false with a reason when the key is unset --------
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        ok, reason = ra.claude_available()
        check("claude_available: false without a key", ok is False)
        check("claude_available: reason names the key or SDK",
              bool(reason) and ("ANTHROPIC_API_KEY" in reason or "SDK" in reason))
    finally:
        if saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_key


def test_research_assistant():
    """pytest entrypoint: run every check and fail on the first problem."""
    run_checks()
    assert not failures, f"{len(failures)} check(s) failed: {failures}"


def main():
    print("EDGAR scrubber research-assistant gate (#280)")
    run_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nresearch-assistant gate: PASS")


if __name__ == "__main__":
    main()
