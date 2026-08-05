"""
Saved-query crawl object + date-chunked pagination + resumable state (issue
#100, built on the fetch/cache/limiter foundation from #99's `edgar_client`).

Two problems this solves that `edgar_client.iter_hits` alone does not:

  1. THE 10,000-RESULT WALL. `efts` caps pagination at 10k hits and, worse,
     `hits.total.value` SATURATES at exactly 10000 (measured live 2026-08-04,
     see #100) -- the API returns HTTP 200 with a silently truncated result
     set, no error. A fixed monthly loop is one busy month from silent data
     loss (424B2 hit 10000/month in 2023, 2024, AND 2025). So chunking here is
     RECURSIVE on the measured total: issue the query, read `search_total`,
     and if it reads as "unknown, must split" (>= EFTS_MAX_WINDOW), bisect the
     date range and recurse. It bottoms out wherever the data requires --
     weekly for a busy 424B2 range, a single day for a narrow query -- rather
     than trusting a hardcoded granularity.

  2. RESUMABLE STATE. A full-year crawl is hours of wall clock under the
     10 req/s ceiling (#99). `CrawlState` persists the frontier (chunks not
     yet resolved), completed chunks, and every accession seen -- to disk,
     after every leaf chunk, never mid-chunk. Killed and restarted, a crawl
     picks up the frontier exactly where the last successful save left it: no
     gap (nothing gets skipped) and no more than one chunk's worth of
     redundant work (idempotent anyway, since accessions are deduped by id).

Search criteria are a `SavedQuery` -- a stored, id'd, versioned object, not a
text box whose contents vanish after the run. Re-running the same query id is
an INCREMENTAL crawl by default: extending `enddt` only fetches the new date
span and reports the diff (`new_accessions`); changing `q`/`forms`/`ciks`
under the same id starts a clean crawl instead of silently mixing two
incompatible result sets under one id.

Efficiency note: the total-count check for a chunk and its first page of hits
are the SAME `efts` request (`from=0`). `Crawler._count` issues it; if the
chunk turns out to be a leaf, `iter_hits`'s own `from=0` call is a cache hit
(#99) -- zero extra network requests. Splitting is therefore "free" beyond the
count checks a saturated range needed anyway.

Scope (per #100): this module is corpus-size-agnostic, but the intended first
run is one issuer, one year (see `queries/424b2-jpm-2025-pilot.json`) -- a few
thousand filings, not the ~91k/year full 424B2 population. Widen the query
once #107's rule coverage makes a broad crawl's downstream cost trivial.

stdlib only. Run the self-check:  python tools/edgar_scrubber/crawl.py
"""
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

try:  # package import: tools.edgar_scrubber.crawl
    from .edgar_client import EdgarClient, EdgarError, EFTS_MAX_WINDOW, parse_hit, search_total
except ImportError:  # standalone: python tools/edgar_scrubber/crawl.py
    from edgar_client import EdgarClient, EdgarError, EFTS_MAX_WINDOW, parse_hit, search_total

DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "crawl_state"


def default_state_path(query_id, state_dir=None):
    """`<state_dir>/<query_id>.json` -- one state file per saved-query id."""
    return str(Path(state_dir or DEFAULT_STATE_DIR) / f"{query_id}.json")


# --------------------------------------------------------------------------- #
# Saved query -- search criteria as a stored, id'd, versioned object
# --------------------------------------------------------------------------- #
@dataclass
class SavedQuery:
    """A persisted EDGAR full-text-search query: what a re-run resumes/diffs
    against. Shape matches the #100 issue example exactly.

    `ciks` is sent to `efts` as a server-side filter (`ciks=` param) -- cheap,
    and it shrinks the 10k-window problem for a single-issuer crawl (the pilot
    scope). `efts` has no server-side negation, so `excludeCiks` is applied as
    a client-side post-filter when hits are collected.

    `startdt`/`enddt` are required: they bound the window this object owns,
    and `Crawler` diffs against them across re-runs to decide what is new.
    """

    id: str
    q: str = None
    forms: tuple = ()
    startdt: str = None
    enddt: str = None
    ciks: tuple = ()
    excludeCiks: tuple = ()

    def __post_init__(self):
        self.forms = tuple(self.forms) if self.forms else ()
        self.ciks = tuple(self.ciks) if self.ciks else ()
        self.excludeCiks = tuple(self.excludeCiks) if self.excludeCiks else ()
        if not self.startdt or not self.enddt:
            raise ValueError(
                f"query {self.id!r}: startdt/enddt are required -- they bound the "
                "crawl window and are what re-runs diff against"
            )

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"], q=d.get("q"), forms=tuple(d.get("forms", ())),
            startdt=d["startdt"], enddt=d["enddt"],
            ciks=tuple(d.get("ciks", ())), excludeCiks=tuple(d.get("excludeCiks", ())),
        )

    def to_dict(self):
        return {
            "id": self.id, "q": self.q, "forms": list(self.forms),
            "startdt": self.startdt, "enddt": self.enddt,
            "ciks": list(self.ciks), "excludeCiks": list(self.excludeCiks),
        }

    def signature(self):
        """Everything EXCEPT the date window. Two queries with the same
        signature are the same search over a different span -- safe to
        extend incrementally. A different signature under the same id means
        the id was repurposed, so `Crawler` starts that id's state clean
        rather than mixing incompatible result sets."""
        return json.dumps(
            {"q": self.q, "forms": sorted(self.forms),
             "ciks": sorted(self.ciks), "excludeCiks": sorted(self.excludeCiks)},
            sort_keys=True,
        )


def load_query(path):
    with open(path, "r", encoding="utf-8") as f:
        return SavedQuery.from_dict(json.load(f))


def save_query(query, path):
    """Atomic write (temp + os.replace), same convention as `HttpCache.put`."""
    path = str(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(query.to_dict(), f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Date-chunk bisection
# --------------------------------------------------------------------------- #
def _pd(s):
    return date.fromisoformat(s)


def _sd(d):
    return d.isoformat()


def bisect_range(start, end):
    """Split `[start, end]` (inclusive, `date` objects) into two inclusive
    halves that together cover the same span with no overlap and no gap.
    Requires `start < end`; each half is strictly smaller than the input, so
    recursing on it always makes progress down to single-day chunks."""
    if start >= end:
        raise ValueError(f"cannot bisect a range that isn't start < end: {start}..{end}")
    span = (end - start).days
    mid = start + timedelta(days=span // 2)
    return (start, mid), (mid + timedelta(days=1), end)


# --------------------------------------------------------------------------- #
# Resumable crawl state
# --------------------------------------------------------------------------- #
@dataclass
class CrawlState:
    """Everything a crawl needs to resume exactly where it left off.

    `pending` is the frontier: date ranges (as `[start_iso, end_iso]` pairs)
    not yet resolved into either a split or a completed leaf. `Crawler` saves
    this to disk after every leaf chunk -- success or logged failure -- and
    NEVER mid-chunk, so the file on disk always reflects a consistent frontier
    a restart can resume from without a gap.
    """

    query_id: str
    query: dict
    pending: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    accessions: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    chunks_issued: int = 0
    requests_made: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    done: bool = False

    @classmethod
    def empty(cls, query):
        return cls(query_id=query.id, query=query.to_dict(),
                    pending=[[query.startdt, query.enddt]])

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            return cls(**json.load(f))

    def save(self, path):
        """Atomic write -- a crash mid-write can never leave a half-written,
        unresumable state file behind."""
        path = str(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)

    def summary(self):
        total_lookups = self.cache_hits + self.cache_misses
        hit_rate = (self.cache_hits / total_lookups) if total_lookups else 0.0
        return {
            "query_id": self.query_id,
            "done": self.done,
            "accessions": len(self.accessions),
            "chunks_issued": self.chunks_issued,
            "chunks_pending": len(self.pending),
            "requests_made": self.requests_made,
            "cache_hit_rate": hit_rate,
            "failures": len(self.failures),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Crawler -- orchestrates bisection + fetch + resumable persistence
# --------------------------------------------------------------------------- #
class Crawler:
    """Runs (or resumes) a `SavedQuery` to completion against `client`.

    The frontier (`state.pending`) is treated as a stack. Each chunk popped
    off it is checked with one `full_text_search` call: if the reported total
    is "unknown, must split" (`search_total`'s `saturated` flag -- true for
    ANY value >= 10000, per #100, never trusted as a count), the chunk is
    replaced by its two bisected halves and the state is saved; otherwise it
    is a leaf and every hit in it is collected via `iter_hits` (which reuses
    the count check's own `from=0` response from cache -- no extra request).

    State saves after every leaf resolution (success or logged failure), so a
    kill + restart resumes from the last chunk that finished, never re-doing
    completed work and never skipping a chunk that hadn't started.
    """

    def __init__(self, client, query, state_path=None, min_span_days=0):
        self.client = client
        self.query = query
        self.state_path = state_path or default_state_path(query.id)
        self.min_span_days = min_span_days
        self.state = self._load_or_init()
        # baselines so per-run request/cache deltas accumulate correctly
        # across multiple resumed invocations of the same client-less state.
        self._base_net = client.network_requests
        self._base_hits = getattr(client, "cache_hits", 0)
        self._base_miss = getattr(client, "cache_misses", 0)

    # -- state bootstrapping ------------------------------------------------
    def _load_or_init(self):
        if os.path.exists(self.state_path):
            return self._reconcile(CrawlState.load(self.state_path))
        return CrawlState.empty(self.query)

    def _reconcile(self, state):
        old = SavedQuery.from_dict(state.query)
        if old.id != self.query.id or old.signature() != self.query.signature():
            # Same file, incompatible criteria: the id was repurposed. Start
            # this id's state clean rather than mixing result sets that don't
            # mean the same thing.
            return CrawlState.empty(self.query)

        old_start, old_end = _pd(old.startdt), _pd(old.enddt)
        new_start, new_end = _pd(self.query.startdt), _pd(self.query.enddt)
        extra = []
        if new_start < old_start:
            extra.append([_sd(new_start), _sd(old_start - timedelta(days=1))])
        if new_end > old_end:
            extra.append([_sd(old_end + timedelta(days=1)), _sd(new_end)])

        state.pending = state.pending + extra
        state.query = self.query.to_dict()
        state.done = not state.pending
        return state

    # -- the crawl ------------------------------------------------------------
    def run(self):
        """Drain the frontier to completion. Returns a dict with the run
        summary plus `new_accessions` -- the diff against whatever was
        already recorded in state before this call (empty on a first full
        crawl; just the incremental slice on a re-run with an extended
        window)."""
        prev_accessions = set(self.state.accessions)
        while self.state.pending:
            start_s, end_s = self.state.pending.pop(0)
            self._process_chunk(_pd(start_s), _pd(end_s))
        self.state.done = True
        self._sync_counters()
        self.state.save(self.state_path)
        new_accessions = sorted(set(self.state.accessions) - prev_accessions)
        return {"summary": self.state.summary(), "new_accessions": new_accessions}

    def _process_chunk(self, start, end):
        total, saturated = self._count(start, end)
        span_days = (end - start).days

        if saturated and span_days > self.min_span_days:
            (ls, le), (rs, re_) = bisect_range(start, end)
            self.state.pending.insert(0, [_sd(rs), _sd(re_)])
            self.state.pending.insert(0, [_sd(ls), _sd(le)])
            self._sync_counters()
            self.state.save(self.state_path)
            return

        if saturated:
            self.state.warnings.append(
                f"{_sd(start)}..{_sd(end)}: total still >= {EFTS_MAX_WINDOW} and cannot "
                "be split further (single day) -- result set may be truncated"
            )

        try:
            for hit in self.client.iter_hits(
                q=self.query.q, forms=list(self.query.forms) or None,
                startdt=_sd(start), enddt=_sd(end), **self._extra_params(),
            ):
                info = parse_hit(hit)
                acc = info.get("accession")
                if not acc:
                    continue
                if self.query.excludeCiks and info.get("cik") in self.query.excludeCiks:
                    continue
                self.state.accessions[acc] = {
                    "cik": info.get("cik"), "form": info.get("form"),
                    "file_date": info.get("file_date"), "issuer": info.get("issuer"),
                    "document": info.get("document"),
                }
            self.state.completed.append([_sd(start), _sd(end)])
            self.state.chunks_issued += 1
        except EdgarError as exc:
            self.state.failures.append({"chunk": [_sd(start), _sd(end)], "error": str(exc)})

        self._sync_counters()
        self.state.save(self.state_path)

    def _count(self, start, end):
        page = self.client.full_text_search(
            q=self.query.q, forms=list(self.query.forms) or None,
            startdt=_sd(start), enddt=_sd(end), **self._extra_params(),
        )
        return search_total(page)

    def _extra_params(self):
        return {"ciks": ",".join(self.query.ciks)} if self.query.ciks else {}

    def _sync_counters(self):
        net, hits_, miss = (self.client.network_requests,
                            getattr(self.client, "cache_hits", 0),
                            getattr(self.client, "cache_misses", 0))
        self.state.requests_made += net - self._base_net
        self.state.cache_hits += hits_ - self._base_hits
        self.state.cache_misses += miss - self._base_miss
        self._base_net, self._base_hits, self._base_miss = net, hits_, miss


def crawl(client, query, state_path=None, min_span_days=0):
    """Convenience: run a `SavedQuery` to completion, return `Crawler.run()`."""
    return Crawler(client, query, state_path=state_path, min_span_days=min_span_days).run()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Resumable, date-chunked EDGAR full-text-search crawl (issue #100)"
    )
    ap.add_argument("query", help="path to a saved query JSON (see queries/*.json)")
    ap.add_argument("--user-agent", required=True,
                     help="'<name> <email>' -- SEC requires a contactable UA (no default)")
    ap.add_argument("--cache-dir", default=".edgar-cache")
    ap.add_argument("--state-dir", default=None)
    args = ap.parse_args()

    query = load_query(args.query)
    client = EdgarClient(args.user_agent, args.cache_dir)
    state_path = default_state_path(query.id, args.state_dir)
    result = crawl(client, query, state_path=state_path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
