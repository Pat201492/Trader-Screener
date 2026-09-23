"""
Per-extraction result cache (issue #181) -- stop re-paying for model calls that
would return the same answer.

Every other `cache` in this repo is HTTP or search caching (`edgar_client`,
the crawl layer): they save a re-DOWNLOAD, not a re-EXTRACTION. The 25-filing
sample has been run at least four times (`#0015`-`#0018`), each one re-running
every model call from scratch even though nothing about the prompt, the
exemplars or the model had changed. A field-level cache measured 34.5% cost
reduction on a 10,000-filing benchmark (arXiv 2603.22651, Table V); this is
that cache, scoped to one tool.

What makes a cached answer THE SAME answer -- the key (all six ride on
`extraction_ladder.RunLog` per value already):

    (accession, document, field, prompt_version, exemplar_set, model)

Change the prompt, the exemplars or the model and the key changes, so a stale
answer cannot survive a prompt fix -- which matters because the prompt under
`estimated_value_per_1000` changed three times (0.0 -> 970.2 copied from an
example -> the bounds midpoint; see `extraction_ladder._bounds_line`). A cache
that keyed on `(accession, field)` alone would have served the first wrong
answer straight through every one of those fixes.

Two guard rails the acceptance criteria pin down:

  * OFF BY DEFAULT, enabled per run. A cached run is a replay, not a fresh
    measurement, so it must be opted into deliberately -- `ExtractCache()` with
    no `enabled=True` never reads, writes, or even creates its store.
  * A hit is RECORDED AS a hit (`CachedExtraction.cache_hit`), so a replayed
    value can never be counted as a new observation of the model's behaviour.

Ownership: the store lives under the scrubber home (`EDGAR_SCRUBBER_HOME`, same
root `output_store` uses) and nowhere else. `output_store._assert_local_destination`
is reused so the cache can never be pointed at a pipeline location either.

stdlib only (sqlite3). Run the self-check:  python tools/edgar_scrubber/extract_cache.py
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field as _dc_field
from pathlib import Path

try:  # package import: tools.edgar_scrubber.extract_cache
    from .output_store import _assert_local_destination
except ImportError:  # standalone: python tools/edgar_scrubber/extract_cache.py
    from output_store import _assert_local_destination


# The cache's default home -- a subtree of the scrubber home, never a shared or
# pipeline volume. Same `EDGAR_SCRUBBER_HOME` override `output_store` honours.
LOCAL_CACHE_ROOT = Path(
    os.getenv("EDGAR_SCRUBBER_HOME", Path.home() / ".edgar-scrubber")
) / "extract_cache"
DEFAULT_CACHE_PATH = LOCAL_CACHE_ROOT / "extractions.sqlite"


# ── Key ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CacheKey:
    """The six components that decide whether two extractions are the same
    answer. `document` is the specific filing document read; `exemplar_set` and
    `model` may be None (no exemplars, model not yet chosen) and None is a
    distinct key from any string -- an unversioned run does not collide with a
    versioned one."""

    accession: str
    document: str
    field: str
    prompt_version: str
    exemplar_set: str = None
    model: str = None

    def digest(self):
        """A stable primary-key string. NULLs make composite SQLite PKs
        non-unique (NULL != NULL), so the six components are hashed into one
        deterministic id instead; the raw components are still stored as
        columns for debugging."""
        raw = "\x1f".join(
            "\x00" if v is None else str(v)
            for v in (self.accession, self.document, self.field,
                      self.prompt_version, self.exemplar_set, self.model)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Value ────────────────────────────────────────────────────────────────────

@dataclass
class CachedExtraction:
    """What a hit returns and a miss stores: the model's answer, plus whether
    it came from the cache. `cache_hit` is the whole point of the record -- it
    is what stops a replayed value being mistaken for a fresh measurement, and
    it rides through `as_dict()` so it survives wherever the value is reported."""

    value: object
    span: tuple = None
    confidence: float = None
    flags: list = _dc_field(default_factory=list)
    cache_hit: bool = False

    def as_dict(self):
        d = {
            "value": self.value,
            "span": list(self.span) if isinstance(self.span, tuple) else self.span,
            "confidence": self.confidence,
            "flags": self.flags,
            "cache_hit": self.cache_hit,
        }
        return d


# ── Cache ────────────────────────────────────────────────────────────────────

class ExtractCache:
    """A local, per-(accession, document, field, prompt, exemplars, model)
    result cache. See module docstring.

    Off by default: an instance built without `enabled=True` reads nothing,
    writes nothing, and does not create its store file -- so importing or
    constructing the cache can never turn a fresh run into a replay by
    accident. Enable it explicitly, per run.
    """

    def __init__(self, path=None, *, enabled=False):
        self.enabled = enabled
        self.path = str(path) if path is not None else str(DEFAULT_CACHE_PATH)
        # Never a pipeline location, even when the caller passes an explicit path.
        if self.path != ":memory:":
            _assert_local_destination(self.path)
        self._conn = None

    # -- storage lifecycle ----------------------------------------------------

    def _connect(self):
        """Open (and on first write create) the store, lazily. A disabled cache
        never gets here, so a disabled cache never creates a file."""
        if self._conn is not None:
            return self._conn
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extractions (
                key            TEXT PRIMARY KEY,
                accession      TEXT NOT NULL,
                document       TEXT NOT NULL,
                field          TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                exemplar_set   TEXT,
                model          TEXT,
                value_json     TEXT NOT NULL,
                span_json      TEXT,
                confidence     REAL,
                flags_json     TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- read / write ---------------------------------------------------------

    def get(self, key):
        """Return the CachedExtraction for `key` with `cache_hit=True`, or None
        on a miss. A disabled cache always misses."""
        if not self.enabled:
            return None
        conn = self._connect()
        row = conn.execute(
            "SELECT value_json, span_json, confidence, flags_json "
            "FROM extractions WHERE key = ?",
            (key.digest(),),
        ).fetchone()
        if row is None:
            return None
        value_json, span_json, confidence, flags_json = row
        span = json.loads(span_json) if span_json is not None else None
        return CachedExtraction(
            value=json.loads(value_json),
            span=tuple(span) if isinstance(span, list) else span,
            confidence=confidence,
            flags=json.loads(flags_json),
            cache_hit=True,
        )

    def put(self, key, value, span=None, confidence=None, flags=None):
        """Store an extraction under `key`. A disabled cache is a no-op, so it
        writes no file. Returns the stored CachedExtraction (`cache_hit=False`:
        this is the fresh value that is now being remembered, not a replay)."""
        stored = CachedExtraction(value=value, span=span, confidence=confidence,
                                  flags=list(flags) if flags else [], cache_hit=False)
        if not self.enabled:
            return stored
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO extractions "
            "(key, accession, document, field, prompt_version, exemplar_set, "
            " model, value_json, span_json, confidence, flags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key.digest(), key.accession, key.document, key.field,
                key.prompt_version, key.exemplar_set, key.model,
                json.dumps(value),
                json.dumps(list(span)) if isinstance(span, tuple) else json.dumps(span),
                confidence,
                json.dumps(stored.flags),
            ),
        )
        conn.commit()
        return stored

    def extract(self, key, compute):
        """The whole point, in one call: on a hit return the cached value and
        DO NOT call `compute`; on a miss call `compute` (a zero-arg callable
        that does the actual model call), store its result, and return it.

        `compute` returns `(value, span, confidence, flags)`. The returned
        CachedExtraction carries `cache_hit` so the caller records a replay as a
        replay. A disabled cache always misses and always calls `compute`, so a
        run with the cache off behaves exactly as it did before this module."""
        hit = self.get(key)
        if hit is not None:
            return hit
        value, span, confidence, flags = compute()
        return self.put(key, value, span=span, confidence=confidence, flags=flags)


if __name__ == "__main__":
    import tempfile

    failures = []

    def check(label, cond):
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    root = Path(tempfile.mkdtemp(prefix="extract-cache-selfcheck-"))
    cache = ExtractCache(root / "extract_cache" / "extractions.sqlite", enabled=True)

    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return 972.4, (10, 20), 0.9, [{"code": "ok"}]

    k = CacheKey("0001-24-000001", "424b2.htm", "estimated_value_per_1000",
                 prompt_version="v3", exemplar_set="jpm@2", model="qwen2.5:7b")

    first = cache.extract(k, compute)
    check("first extraction is a miss and calls the model", not first.cache_hit and calls["n"] == 1)
    second = cache.extract(k, compute)
    check("identical key is a hit and makes no model call",
          second.cache_hit and calls["n"] == 1 and second.value == 972.4
          and second.span == (10, 20) and second.confidence == 0.9
          and second.flags == [{"code": "ok"}])

    for changed in (
        CacheKey(k.accession, k.document, k.field, "v4", k.exemplar_set, k.model),
        CacheKey(k.accession, k.document, k.field, k.prompt_version, "jpm@3", k.model),
        CacheKey(k.accession, k.document, k.field, k.prompt_version, k.exemplar_set, "claude-sonnet-4-6"),
    ):
        before = calls["n"]
        cache.extract(changed, compute)
        check(f"changed key misses (was {calls['n'] - 1} calls)", calls["n"] == before + 1)

    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        raise SystemExit(1)
    print("\nextract_cache self-check: PASS")
