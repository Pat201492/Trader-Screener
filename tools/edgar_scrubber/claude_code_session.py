"""
A long-lived Claude Code CLI process per research chat, on the user's Claude
subscription login -- no API key, no Console credits.

    claude -p --input-format stream-json --output-format stream-json --verbose
           --tools "" --setting-sources "" --strict-mcp-config
           --system-prompt <planner prompt> --json-schema <plan schema>

Each chat gets ONE process that stays up between messages (a follow-up answered
in ~1 s instead of a cold start), and the process keeps the conversation, so
only what is NEW since the last turn is sent. Idle for IDLE_SECONDS, the process
is closed but its session id is kept: the next message resumes the same
conversation with `--resume <id>`. `reset(chat_id)` forgets it entirely -- the
"New session" button.

Why these flags (the same posture as study-hall's Claude chat adapter):
  * ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from the child's
    environment: the CLI prefers a key over its login when one is present,
    which would silently move this onto metered billing.
  * `--tools ""` -- the planner only proposes searches; it gets no tools at all.
  * `--setting-sources ""` and `--strict-mcp-config` -- no user, project or
    local settings, hooks or MCP servers leak into the planner.
  * cwd is an empty temp directory, so no repository CLAUDE.md is read.

stdlib only. The process is injected as `popen` so tests drive a fake.
"""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

IDLE_SECONDS = 15 * 60
TURN_TIMEOUT_SECONDS = 300
DEFAULT_MODEL = "opus"          # the CLI's alias for the current Opus
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def cli_path():
    return shutil.which("claude")


def subscription_env(base=None):
    """The child environment: the parent's minus the variables that would move
    the CLI off the subscription login onto metered API billing."""
    env = dict(os.environ if base is None else base)
    for k in _STRIP_ENV:
        env.pop(k, None)
    return env


def build_args(binary, system_prompt, schema, model, resume=None):
    args = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--model", model, "--tools", "", "--setting-sources", "",
            "--strict-mcp-config", "--system-prompt", system_prompt,
            "--json-schema", json.dumps(schema)]
    if resume:
        args += ["--resume", resume]
    return args


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeCodeSession:
    """One chat's CLI process. Not thread-safe on its own; the registry holds a
    per-session lock around `turn`."""

    def __init__(self, system_prompt, schema, model=None, popen=None, binary=None,
                 turn_timeout=TURN_TIMEOUT_SECONDS):
        self.turn_timeout = turn_timeout
        self.system_prompt = system_prompt
        self.schema = schema
        self.model = model or os.environ.get("CLAUDE_CODE_RESEARCH_MODEL") or DEFAULT_MODEL
        self._popen = popen or subprocess.Popen
        self._binary = binary
        self.session_id = None      # survives an idle close; cleared by reset
        self.seen = 0               # chat-history entries already in the conversation
        self.proc = None
        self.lines = None
        self.last_used = time.time()
        self.api_key_source = None
        self.stderr_tail = []
        self.lock = threading.Lock()

    # -- process lifecycle ----------------------------------------------------
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self, resume):
        binary = self._binary or cli_path()
        if not binary:
            raise ClaudeCodeError("the claude CLI is not installed (no `claude` on PATH)")
        cwd = tempfile.mkdtemp(prefix="research-claude-")
        self.proc = self._popen(
            build_args(binary, self.system_prompt, self.schema, self.model, resume),
            cwd=cwd, env=subscription_env(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1)
        self.lines = queue.Queue()
        proc = self.proc

        def pump(stream, sink):
            try:
                for line in stream:
                    sink(line)
            except Exception:
                pass
            finally:
                if stream is proc.stdout:
                    self.lines.put(None)       # EOF marker

        def keep_tail(line):
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

        threading.Thread(target=pump, args=(proc.stdout, self.lines.put), daemon=True).start()
        if proc.stderr is not None:
            threading.Thread(target=pump, args=(proc.stderr, keep_tail), daemon=True).start()

    def close(self):
        """Stop the process; keep session_id so the next turn can resume."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def reset(self):
        self.close()
        self.session_id = None
        self.seen = 0

    # -- one turn -------------------------------------------------------------
    def turn(self, text, timeout=None):
        """Send one user turn; return (result_text, structured_output_or_None).
        Raises ClaudeCodeError on a dead process, a timeout or an error result."""
        timeout = self.turn_timeout if timeout is None else timeout
        if not self.alive():
            self._spawn(resume=self.session_id)
        self.last_used = time.time()
        line = json.dumps({"type": "user", "parent_tool_use_id": None,
                           "message": {"role": "user", "content": text}})
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self.close()
            raise ClaudeCodeError(f"could not write to the claude process: {exc}")
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self.close()
                raise ClaudeCodeError(f"claude did not answer within {timeout}s")
            try:
                raw = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if raw is None:
                self.close()
                tail = " | ".join(self.stderr_tail[-3:])
                raise ClaudeCodeError("the claude process exited" + (f": {tail}" if tail else ""))
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                self.api_key_source = ev.get("apiKeySource")
            if ev.get("type") == "result":
                self.last_used = time.time()
                if ev.get("is_error"):
                    raise ClaudeCodeError(str(ev.get("result") or ev.get("subtype") or "error result"))
                return ev.get("result") or "", ev.get("structured_output")


class SessionRegistry:
    """chat_id -> ClaudeCodeSession, with an idle reaper."""

    def __init__(self, idle_seconds=IDLE_SECONDS, factory=None):
        self.idle_seconds = idle_seconds
        self.factory = factory or ClaudeCodeSession
        self._sessions = {}
        self._lock = threading.Lock()
        self._reaper = None

    def get(self, chat_id, system_prompt, schema):
        with self._lock:
            s = self._sessions.get(chat_id)
            if s is None:
                s = self.factory(system_prompt, schema)
                self._sessions[chat_id] = s
            self._start_reaper()
            return s

    def reset(self, chat_id):
        with self._lock:
            s = self._sessions.pop(chat_id, None)
        if s is None:
            return False
        with s.lock:
            s.reset()
        return True

    def reap(self, now=None):
        """Close sessions idle past idle_seconds (their ids are kept for resume)."""
        now = now or time.time()
        with self._lock:
            sessions = list(self._sessions.values())
        closed = 0
        for s in sessions:
            if s.alive() and now - s.last_used > self.idle_seconds and s.lock.acquire(blocking=False):
                try:
                    s.close()
                    closed += 1
                finally:
                    s.lock.release()
        return closed

    def status(self, chat_id):
        s = self._sessions.get(chat_id)
        if s is None:
            return {"exists": False, "running": False, "session_id": None}
        return {"exists": True, "running": s.alive(), "session_id": s.session_id,
                "api_key_source": s.api_key_source}

    def _start_reaper(self):
        if self._reaper is not None:
            return

        def loop():
            while True:
                time.sleep(60)
                try:
                    self.reap()
                except Exception:
                    pass

        self._reaper = threading.Thread(target=loop, daemon=True, name="research-claude-reaper")
        self._reaper.start()


SESSIONS = SessionRegistry()
