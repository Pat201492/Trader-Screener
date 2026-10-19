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
        # ClaudeProvider calls client.beta.messages.create (refusal fallbacks
        # are a beta parameter); both paths record into `seen`.
        self.beta = type("Beta", (), {"messages": self})()

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
        check("claude: default model is claude-opus-5-5",
              cp.model == "claude-opus-5-5")
        os.environ["ANTHROPIC_RESEARCH_MODEL"] = "claude-opus-5"
        cp2 = ra.ClaudeProvider(client=FakeAnthropicClient())
        check("claude: model read from ANTHROPIC_RESEARCH_MODEL",
              cp2.model == "claude-opus-5")
        cp2.complete(ra.SYSTEM_PROMPT, [{"role": "user", "content": "q"}])
        check("claude: schema passed via output_config",
              cp2.client.seen["output_config"]["format"]["schema"] is ra.PLAN_SCHEMA)
        check("claude: model passed through to the SDK call",
              cp2.client.seen["model"] == "claude-opus-5")
        s = cp2.client.seen
        check("claude: max_tokens leaves room for thinking (>= 16000)", s["max_tokens"] >= 16000)
        check("claude: effort set explicitly", s["output_config"].get("effort") == "medium")
        check("claude: server-side refusal fallbacks on (default routing)",
              s.get("fallbacks") == "default"
              and "server-side-fallback-2026-07-01" in (s.get("betas") or []))
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_RESEARCH_MODEL", None)
        else:
            os.environ["ANTHROPIC_RESEARCH_MODEL"] = saved

    # -- estimate_cost: tokens for any provider, growing with history --------
    # A fake price table so the test does not depend on the shipped JSON's
    # editable numbers; the Claude path multiplies tokens by these.
    prices = {"claude-sonnet-5-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0}}

    claude = ra.ClaudeProvider(client=FakeAnthropicClient(), model="claude-sonnet-5-5")
    e0 = ra.estimate_cost(claude, "find autocallable notes", prices=prices)
    check("estimate: input and output token estimates present",
          e0["input_tokens"] > 0 and e0["output_tokens"] == ra.OUTPUT_TOKEN_ALLOWANCE)
    history = [{"role": "user", "content": "x" * 400},
               {"role": "assistant", "content": "y" * 400}]
    e1 = ra.estimate_cost(claude, "find autocallable notes", history=history,
                          prices=prices)
    check("estimate: input tokens grow with history length",
          e1["input_tokens"] > e0["input_tokens"])

    # -- estimate_cost: listed Claude model -> dollars = tokens x prices ------
    expected_dollars = (e0["input_tokens"] * 2.0
                        + e0["output_tokens"] * 10.0) / 1_000_000
    check("estimate: listed Claude model dollars = tokens x listed prices",
          e0["dollars"] is not None and abs(e0["dollars"] - expected_dollars) < 1e-12)

    # -- estimate_cost: unlisted model -> dollars is null --------------------
    other = ra.ClaudeProvider(client=FakeAnthropicClient(), model="claude-nonesuch-9")
    eu = ra.estimate_cost(other, "q", prices=prices)
    check("estimate: unlisted model has dollars = None",
          eu["dollars"] is None and eu["input_tokens"] > 0)

    # -- estimate_cost: Ollama provider -> dollars is 0 ----------------------
    eo = ra.estimate_cost(ra.OllamaProvider(FakeOllamaClient()), "q", prices=prices)
    check("estimate: Ollama provider dollars = 0", eo["dollars"] == 0)
    check("estimate: Ollama still reports tokens", eo["input_tokens"] > 0)

    # The shipped price table loads and prices the default model.
    shipped = ra.load_prices()
    check("estimate: shipped prices list the default Claude model",
          ra.DEFAULT_CLAUDE_MODEL in shipped)
    e_default = ra.estimate_cost(ra.ClaudeProvider(client=FakeAnthropicClient()), "q")
    check("estimate: default model priced from the shipped table",
          e_default["dollars"] is not None and e_default["dollars"] > 0)

    # -- extract follow-up: validated like a search, against the chat's results
    KNOWN = {"0001234567-25-000001", "0001234567-25-000002"}
    good_ex = {"form": "424b2",
               "accessions": ["0001234567-25-000001", "0001234567-25-000002"],
               "rationale": "worth reading in full"}
    e_ok, e_drop = ra.validate_extract(good_ex, known_accessions=KNOWN)
    check("extract: a clean proposal validates with the form upper-cased",
          e_ok is not None and e_ok.form == "424B2" and not e_drop)
    check("extract: its accessions round-trip in order",
          e_ok.accessions == ["0001234567-25-000001", "0001234567-25-000002"])

    unknown = {"form": "424B2",
               "accessions": ["0001234567-25-000001", "9999999999-99-999999"],
               "rationale": "r"}
    u_ok, u_drop = ra.validate_extract(unknown, known_accessions=KNOWN)
    check("extract: an accession not in the results drops the whole proposal",
          u_ok is None and any("did not appear" in d for d in u_drop))

    too_many = {"form": "424B2",
                "accessions": ["0001234567-25-000001"] * 51,
                "rationale": "r"}
    # Build 51 DISTINCT known accessions so the cap -- not the "unknown" check --
    # is what trips: the count ceiling must fire on its own.
    many = ["%010d-25-%06d" % (1, i) for i in range(51)]
    tm_ok, tm_drop = ra.validate_extract(
        {"form": "424B2", "accessions": many, "rationale": "r"},
        known_accessions=set(many))
    check("extract: more than 50 accessions drops the proposal with a reason",
          tm_ok is None and any("limit 50" in d for d in tm_drop))

    bad_form = {"form": "FORM!", "accessions": ["0001234567-25-000001"],
                "rationale": "r"}
    bf_ok, bf_drop = ra.validate_extract(bad_form, known_accessions=KNOWN)
    check("extract: an invalid form code drops the proposal",
          bf_ok is None and any("invalid form code" in d for d in bf_drop))

    check("extract: no proposal is (None, []) -- never a drop reason",
          ra.validate_extract(None, known_accessions=KNOWN) == (None, []))

    # plan() threads known_accessions through and carries the extract.
    plan_json = json.dumps({"reply": "extract these", "searches": [
        {"q": "q", "forms": [], "ciks": [], "startdt": "", "enddt": "",
         "rationale": "r"}], "extract": good_ex})
    pe = ra.plan(FakeProvider(plan_json), "pull them",
                 known_accessions=KNOWN)
    check("plan: a valid extract proposal is carried on the Plan",
          pe.extract is not None and pe.extract.form == "424B2")
    check("plan: to_dict surfaces the extract",
          pe.to_dict()["extract"]["accessions"] == good_ex["accessions"])
    pe2 = ra.plan(FakeProvider(plan_json), "pull them", known_accessions=set())
    check("plan: with no known accessions the extract is dropped, not carried",
          pe2.extract is None and any("did not appear" in d for d in pe2.dropped))

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

    # -- live failures, 2026-10-19: no date in the prompt; a recalled CIK ------
    one = json.dumps({"reply": "r", "searches": [
        {"q": "contingent coupon NVIDIA", "forms": ["424B2"], "ciks": ["320193", "JPM"],
         "startdt": "2026-07-21", "enddt": "2026-10-18", "rationale": "x"}]})
    fp = FakeProvider(one)
    pd = ra.plan(fp, "NVIDIA notes from JPMorgan, last 90 days", today=TODAY)
    check("plan: the system prompt states today's date", "2026-10-18" in fp.last[0])
    check("plan: the system prompt says ciks are the filer and to use tickers",
          "FILED" in fp.last[0] and "ticker" in fp.last[0])
    check("plan: a numeric CIK the user never typed is removed, with a reason",
          pd.searches[0].ciks == ["JPM"]
          and any("320193" in d and "not given by the user" in d for d in pd.dropped))
    pk = ra.plan(FakeProvider(one), "filings by CIK 0000320193", today=TODAY)
    check("plan: a numeric CIK the user typed (zero-padded) is kept",
          pk.searches[0].ciks == ["320193", "JPM"])

    # "insider sales NVIDIA" on Form 4 matched 0 of NVIDIA's 17 Form 4s live.
    f4 = json.dumps({"reply": "r", "searches": [
        {"q": "insider sales NVIDIA", "forms": ["4", "4/A"], "ciks": ["NVDA"],
         "startdt": "", "enddt": "", "rationale": "x"},
        {"q": "\"contingent coupon\" NVIDIA", "forms": ["424B2"], "ciks": ["JPM"],
         "startdt": "", "enddt": "", "rationale": "y"}]})
    pf = ra.plan(FakeProvider(f4), "NVDA insiders and JPM notes", today=TODAY)
    check("plan: q is cleared on a structured-data-only search, with a reason",
          pf.searches[0].q == "" and any("structured" in d for d in pf.dropped))
    check("plan: q is kept on a prose form", pf.searches[1].q == "\"contingent coupon\" NVIDIA")
    kept, why = ra.validate_searches([{"q": "", "forms": ["4"], "ciks": ["NVDA"]}], today=TODAY)
    check("validate: an empty q with forms or ciks is a valid filter-only search", len(kept) == 1)
    kept, why = ra.validate_searches([{"q": "", "forms": [], "ciks": []}], today=TODAY)
    check("validate: a search with no q, forms or ciks is dropped",
          not kept and any("match everything" in w for w in why))


def run_conversation_checks():
    # #315: a turn may be a reply or a clarifying question with no searches.
    q = json.dumps({"reply": "Which issuer did you mean?", "searches": [],
                    "question": "Do you mean JPMorgan's notes, or all banks?", "extract": None})
    pq = ra.plan(FakeProvider(q), "find the notes", today=TODAY)
    check("conversation: a reply-only turn validates with a question and no dropped reason",
          pq.searches == [] and pq.question == "Do you mean JPMorgan's notes, or all banks?"
          and pq.reply_only and not pq.dropped)
    check("conversation: to_dict carries the question", pq.to_dict()["question"] == pq.question)
    with_s = ra.plan(FakeProvider(_valid_plan_json()), "x", today=TODAY)
    check("conversation: a plan with searches is not reply-only and has no question",
          not with_s.reply_only and with_s.question is None)
    sp = ra.system_prompt(TODAY)
    check("conversation: the prompt says to ask a clarifying question when ambiguous",
          "clarifying" in sp and "question" in sp)
    check("conversation: the prompt says to explain results and adjust on pushback",
          "explain" in sp and "pushes back" in sp)
    check("conversation: the prompt still forbids claiming to have read a filing",
          "never claim to know what" in sp)
    check("conversation: the schema allows zero searches and requires `question`",
          ra.PLAN_SCHEMA["properties"]["searches"]["minItems"] == 0
          and "question" in ra.PLAN_SCHEMA["required"])


def test_research_assistant():
    """pytest entrypoint: run every check and fail on the first problem."""
    run_checks()
    run_conversation_checks()
    assert not failures, f"{len(failures)} check(s) failed: {failures}"


def main():
    print("EDGAR scrubber research-assistant gate (#280)")
    run_checks()
    run_conversation_checks()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nresearch-assistant gate: PASS")


if __name__ == "__main__":
    main()
