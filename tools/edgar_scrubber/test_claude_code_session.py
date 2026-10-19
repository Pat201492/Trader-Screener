"""
The research assistant's background Claude Code session, against a FAKE CLI
process: no claude binary, no login, no network.

  * one process serves several turns, and only NEW user history is sent;
  * an idle close keeps the session id, and the next turn resumes with it;
  * a failed resume restarts fresh once, resending the whole user history;
  * reset (the "New session" button) forgets the conversation;
  * the child never inherits ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN;
  * the argv carries no tools, no settings, no MCP, the prompt and the schema;
  * a timeout or a dead process becomes a provider error, never a hang.
"""
import io
import json
import os
import queue
import threading

import claude_code_session as ccs
import research_assistant as ra


class FakeProc:
    """Speaks the CLI's stream-json: each user line gets init + result."""
    instances = []

    def __init__(self, args, **kw):
        self.args, self.kw = args, kw
        self.sent = []
        self._out = queue.Queue()
        self.returncode = None
        resume = args[args.index("--resume") + 1] if "--resume" in args else None
        self.sid = resume or f"sid-{len(FakeProc.instances) + 1}"
        self.fail_resume = FakeProc.fail_next_resume and resume is not None
        FakeProc.fail_next_resume = False
        self.silent = FakeProc.silent
        self.stdin = self
        self.stdout = self
        self.stderr = io.StringIO("")
        FakeProc.instances.append(self)
        if self.fail_resume:
            self._out.put(None)
            self.returncode = 1

    # stdin
    def write(self, s):
        msg = json.loads(s)["message"]["content"]
        self.sent.append(msg)
        if self.silent:
            return
        self._out.put(json.dumps({"type": "system", "subtype": "init", "session_id": self.sid,
                                  "apiKeySource": "none"}) + "\n")
        plan = {"reply": f"turn {len(self.sent)}", "searches": [
            {"q": "", "forms": ["4"], "ciks": ["NVDA"], "startdt": "", "enddt": "", "rationale": "x"}]}
        self._out.put(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                  "session_id": self.sid, "result": json.dumps(plan),
                                  "structured_output": plan}) + "\n")

    def flush(self):
        pass

    def close(self):
        self._out.put(None)
        self.returncode = 0

    # stdout iteration
    def __iter__(self):
        while True:
            item = self._out.get()
            if item is None:
                return
            yield item

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._out.put(None)


def _reset_fakes():
    FakeProc.instances = []
    FakeProc.fail_next_resume = False
    FakeProc.silent = False


def _registry():
    return ccs.SessionRegistry(factory=lambda sp, sc: ccs.ClaudeCodeSession(
        sp, sc, popen=FakeProc, binary="claude-fake"))


def test_one_process_serves_turns_and_only_new_history_is_sent():
    _reset_fakes()
    reg = _registry()
    p = ra.ClaudeCodeProvider("chat-1", registry=reg)
    h = [{"role": "user", "content": "Q1"}]
    out = json.loads(p.complete("SYS", h))
    assert out["reply"] == "turn 1"
    # The server appends the assistant reply and a results summary, then a new question.
    h2 = h + [{"role": "assistant", "content": "turn 1"},
              {"role": "user", "content": "EDGAR results (metadata only): no hits."},
              {"role": "user", "content": "Q2"}]
    out2 = json.loads(p.complete("SYS", h2))
    assert out2["reply"] == "turn 2"
    assert len(FakeProc.instances) == 1, "the same background process is reused"
    sent = FakeProc.instances[0].sent
    assert sent[0] == "Q1"
    assert "Q1" not in sent[1] and "no hits" in sent[1] and "Q2" in sent[1]
    reg.reset("chat-1")


def test_idle_close_resumes_the_same_conversation():
    _reset_fakes()
    reg = _registry()
    p = ra.ClaudeCodeProvider("chat-2", registry=reg)
    p.complete("SYS", [{"role": "user", "content": "Q1"}])
    s = reg._sessions["chat-2"]
    s.last_used -= ccs.IDLE_SECONDS + 1
    assert reg.reap() == 1 and not s.alive() and s.session_id == "sid-1"
    p.complete("SYS", [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "a"},
                       {"role": "user", "content": "Q2"}])
    second = FakeProc.instances[1]
    assert second.args[second.args.index("--resume") + 1] == "sid-1"
    assert second.sent == ["Q2"], "a resumed conversation is not replayed"
    reg.reset("chat-2")


def test_failed_resume_restarts_fresh_with_the_whole_history():
    _reset_fakes()
    reg = _registry()
    p = ra.ClaudeCodeProvider("chat-3", registry=reg)
    p.complete("SYS", [{"role": "user", "content": "Q1"}])
    reg._sessions["chat-3"].close()
    FakeProc.fail_next_resume = True
    out = json.loads(p.complete("SYS", [{"role": "user", "content": "Q1"},
                                        {"role": "assistant", "content": "a"},
                                        {"role": "user", "content": "Q2"}]))
    assert out["reply"] == "turn 1"
    fresh = FakeProc.instances[-1]
    assert "--resume" not in fresh.args
    assert "Q1" in fresh.sent[0] and "Q2" in fresh.sent[0]
    reg.reset("chat-3")


def test_reset_forgets_the_conversation():
    _reset_fakes()
    reg = _registry()
    p = ra.ClaudeCodeProvider("chat-4", registry=reg)
    p.complete("SYS", [{"role": "user", "content": "Q1"}])
    assert reg.reset("chat-4") is True
    assert reg.reset("chat-4") is False
    p.complete("SYS", [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "a"},
                       {"role": "user", "content": "Q2"}])
    assert "--resume" not in FakeProc.instances[-1].args
    reg.reset("chat-4")


def test_child_env_and_argv():
    _reset_fakes()
    saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "tok"
    try:
        reg = _registry()
        ra.ClaudeCodeProvider("chat-5", registry=reg).complete("SYS-PROMPT", [{"role": "user", "content": "Q"}])
        proc = FakeProc.instances[0]
        assert "ANTHROPIC_API_KEY" not in proc.kw["env"] and "ANTHROPIC_AUTH_TOKEN" not in proc.kw["env"]
        a = proc.args
        assert a[a.index("--tools") + 1] == ""
        assert a[a.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in a
        assert a[a.index("--system-prompt") + 1] == "SYS-PROMPT"
        assert json.loads(a[a.index("--json-schema") + 1]) == ra.PLAN_SCHEMA
        assert a[a.index("--input-format") + 1] == "stream-json"
        reg.reset("chat-5")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_a_silent_process_times_out_into_a_plan_error():
    _reset_fakes()
    FakeProc.silent = True
    s = ccs.ClaudeCodeSession("SYS", ra.PLAN_SCHEMA, popen=FakeProc, binary="claude-fake")
    try:
        s.turn("Q", timeout=0.3)
        raise AssertionError("expected a timeout")
    except ccs.ClaudeCodeError as exc:
        assert "did not answer" in str(exc)
    reg = ccs.SessionRegistry(factory=lambda sp, sc: ccs.ClaudeCodeSession(
        sp, sc, popen=FakeProc, binary="x", turn_timeout=0.3))
    plan = ra.plan(ra.ClaudeCodeProvider("chat-6", registry=reg), "Q")
    assert plan.searches == [] and any("provider error" in d for d in plan.dropped)
    reg.reset("chat-6")


def test_estimate_is_free_on_the_plan():
    reg = _registry()
    e = ra.estimate_cost(ra.ClaudeCodeProvider("chat-7", registry=reg), "Q")
    assert e["dollars"] == 0.0
