"""
EDGAR client core — the foundation the whole EDGAR scrubber sits on (issue #99,
part of EPIC #95 "Research + Tools"). Every other scrubber issue (#100 corpus
counts, #102 field spec, #106 exemplars, #107 rules) fetches through this.

Why this exists instead of driving the search UI with a model (issue #99):

    The SEC publishes JSON APIs. Search and fetch are *deterministic code*; the
    model only ever touches extraction. Driving the search page with a browser
    is a fragile re-implementation of something already solved and makes runs
    non-reproducible, so it is deliberately not done here.

What this module wraps (endpoints, all measured live 2026-08-04 — see #100):

    full-text search   https://efts.sec.gov/LATEST/search-index?q=&forms=&startdt=&enddt=&from=
    filing history     https://data.sec.gov/submissions/CIK##########.json
    accession manifest https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/index.json
    XBRL facts         https://data.sec.gov/api/xbrl/{companyfacts,companyconcept,frames}
    quarterly backfill https://www.sec.gov/Archives/edgar/full-index/{yyyy}/QTR{n}/form.idx

Three things this module treats as load-bearing, not optional (issue #99):

  1. SEC etiquette or you get blocked. A `User-Agent: <name> <email>` on every
     request (configurable via the `user_agent` arg or the `EDGAR_USER_AGENT`
     env var — see SETUP.md; there is deliberately NO default that would ship
     someone else's address); a hard 10 req/s ceiling enforced by ONE shared
     limiter across the whole process (a parallel fetcher that forgets this gets
     the IP blocked mid-crawl); `Accept-Encoding: gzip` + connection reuse;
     retry-with-backoff on 429/5xx; and a sustained 403 treated as a config
     error (bad UA) that fails loud rather than retrying into a longer ban.

  2. An on-disk cache, content-addressed by URL and gzipped on disk. This is
     load-bearing: the human-in-loop design (#102/#106/#107) re-processes the
     same documents dozens of times as the spec/exemplars/rules evolve. If every
     pass hit the network the 10 req/s cap would become the bottleneck on your
     own iteration speed. Re-extraction must NEVER refetch — a second identical
     run issues zero network requests.

  3. Issuer identification costs no extra request. `display_names[0]` from a
     search hit already carries issuer name + tickers + CIK in one string, and
     issuer is the partition key for #106/#107 — so it is parsed out here
     (`parse_display_name` / `parse_hit`) rather than re-fetched downstream.

Porting posture (same as `lookahead-gate/` and `pipeline-mock/`): stdlib-only,
runnable today, and the concrete spec for whoever folds EDGAR ingestion into the
Stock-Data-Pipeline repo (ARCHITECTURE.md rule: collect once, one writer). The
network layer is injectable (`transport=`) so `test_edgar_client.py` exercises
every guarantee — cache, limiter, retry, parsing — with zero network.
"""
import gzip
import hashlib
import http.client
import json
import os
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

DEFAULT_RATE = 10.0        # req/s — the SEC ceiling (issue #99 hard requirement)
EFTS_PAGE_SIZE = 100       # measured live: efts returns 100 hits/page (#100)
EFTS_MAX_WINDOW = 10000    # efts refuses from>=10000; total.value saturates here (#100)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class EdgarError(Exception):
    """Base for everything this module raises."""


class EdgarConfigError(EdgarError):
    """Caller misconfiguration — bad/absent User-Agent, or a sustained 403.

    A 403 is surfaced as this (not retried) on purpose: the SEC returns 403 for
    a missing/blocked UA, and hammering it turns a soft block into a long ban.
    Fail loud so the operator fixes the UA instead of the crawler digging in.
    """


class EdgarLookupError(EdgarError):
    """A requested identifier isn't in a resolved SEC index — e.g. an unknown
    ticker in `company_tickers.json`. Raised rather than returning None so a
    typo'd ticker fails loud at the lookup instead of as a confusing 404 on the
    CIK-shaped URL it would have built downstream.
    """


class EdgarHTTPError(EdgarError):
    """A non-200 the client will not retry (e.g. 404), or retries exhausted."""

    def __init__(self, status, url, body=None):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}")


# --------------------------------------------------------------------------- #
# User-Agent — configurable, no default that ships someone else's address
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def validate_user_agent(ua):
    """Return `ua` if it is a usable `<name> <email>` string, else raise.

    The SEC requires a contactable UA. We reject the empty string and anything
    without an email-looking token so a crawl can't silently go out anonymous
    (which draws a 403) — and there is intentionally no default value.
    """
    if not ua or not ua.strip():
        raise EdgarConfigError(
            "EDGAR requires a User-Agent of the form '<name> <email>'. "
            "There is no default — pass your own contact (e.g. 'Acme Research ops@acme.com')."
        )
    if not _EMAIL_RE.search(ua):
        raise EdgarConfigError(
            f"User-Agent {ua!r} has no contact email; SEC needs '<name> <email>' "
            "(e.g. 'Acme Research ops@acme.com')."
        )
    return ua.strip()


# --------------------------------------------------------------------------- #
# Shared rate limiter — ONE per process, safe under concurrency
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Monotonic-schedule limiter: hands out slots spaced by 1/rate seconds.

    The scheduling (advancing `_next`) happens under a lock and is O(1); the
    actual wait sleeps OUTSIDE the lock, so N concurrent callers get cleanly
    staggered start times instead of all sleeping the same interval and then
    firing together. Throughput is therefore bounded by `rate` no matter how
    many threads call `acquire()` — which is the whole point: a parallel
    document fetcher must not be able to exceed 10 req/s just by adding threads.

    A gap longer than one interval resets the schedule to "now" (via the max),
    so sparse callers aren't penalised and there's no unbounded credit buildup.
    """

    def __init__(self, rate=DEFAULT_RATE, monotonic=time.monotonic, sleep=time.sleep):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next = None
        self._monotonic = monotonic
        self._sleep = sleep

    def acquire(self):
        with self._lock:
            now = self._monotonic()
            start = now if self._next is None else max(now, self._next)
            self._next = start + self._interval
            wait = start - now
        if wait > 0:
            self._sleep(wait)


# --------------------------------------------------------------------------- #
# On-disk cache — content-addressed by URL, gzipped, atomic writes
# --------------------------------------------------------------------------- #
class HttpCache:
    """sha256(url) -> `<root>/<ab>/<hash>.gz`, holding the gzipped response body.

    Sharded one level so a year of the structured-note subset (~30k docs, #100)
    doesn't land 30k files in one directory. Writes go to a temp name then
    `os.replace` (atomic) so a crash or a concurrent writer can't leave a
    half-written entry that a later run would read as valid.
    """

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def path_for(self, url):
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return os.path.join(self.root, h[:2], h + ".gz")

    def get(self, url):
        p = self.path_for(url)
        if not os.path.exists(p):
            return None
        with gzip.open(p, "rb") as f:
            return f.read()

    def put(self, url, body):
        p = self.path_for(url)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # pid+tid keeps temp names unique across concurrent writers without
        # needing time/random (both banned in some run contexts).
        tmp = f"{p}.tmp-{os.getpid()}-{threading.get_ident()}"
        with gzip.open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, p)

    def has(self, url):
        return os.path.exists(self.path_for(url))


# --------------------------------------------------------------------------- #
# Real network transport — keep-alive, gzip, thread-safe by thread-locality
# --------------------------------------------------------------------------- #
class HttpTransport:
    """One-shot GET over a reused HTTPS keep-alive connection.

    Connections are thread-local: each thread keeps its own persistent
    connection per host, so we get connection reuse (issue #99) without a global
    lock serialising every fetch — the RateLimiter already bounds the rate. A
    dropped keep-alive connection is transparently re-opened once.

    Retry/backoff and status handling live in EdgarClient, not here; this just
    does exactly one request and returns `(status, headers, body_bytes)` with
    the body already gunzipped.
    """

    def __init__(self, timeout=30):
        self._timeout = timeout
        self._local = threading.local()

    def _conn(self, host):
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        c = conns.get(host)
        if c is None:
            c = conns[host] = http.client.HTTPSConnection(host, timeout=self._timeout)
        return c

    def _drop(self, host):
        conns = getattr(self._local, "conns", {})
        c = conns.pop(host, None)
        if c is not None:
            try:
                c.close()
            except OSError:
                pass

    def __call__(self, url, headers):
        parsed = urlsplit(url)
        host = parsed.netloc
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        last_exc = None
        for attempt in range(2):  # one retry for a stale keep-alive socket
            conn = self._conn(host)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                status = resp.status
                hdrs = {k.lower(): v for k, v in resp.getheaders()}
                if hdrs.get("content-encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                return status, hdrs, body
            except (http.client.HTTPException, OSError) as exc:
                last_exc = exc
                self._drop(host)
        raise last_exc

    def close(self):
        for c in getattr(self._local, "conns", {}).values():
            try:
                c.close()
            except OSError:
                pass
        self._local.conns = {}


# --------------------------------------------------------------------------- #
# CIK helpers
# --------------------------------------------------------------------------- #
def cik10(cik):
    """Zero-pad a CIK to the 10-digit form the submissions/XBRL endpoints want."""
    digits = re.sub(r"\D", "", str(cik))
    if not digits:
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def cik_bare(cik):
    """Strip leading zeros — the Archives path uses the un-padded CIK."""
    return str(int(re.sub(r"\D", "", str(cik))))


# --------------------------------------------------------------------------- #
# display_names / hit parsing — issuer + CIK for free (partition key, #106/#107)
# --------------------------------------------------------------------------- #
_CIK_IN_NAME = re.compile(r"\(\s*CIK\s+(\d+)\s*\)", re.I)


def parse_display_name(s):
    """Split a `display_names[0]` string into issuer / tickers / CIK.

    Live shape (2026-08-04): ``NAME  (TICK1, TICK2, ...)  (CIK 0000019617)``, e.g.
    ``JPMORGAN CHASE & CO  (JPM, AMJB, JPM-P...)``. The issuer is everything
    before the first ``(``; the first non-CIK parenthetical is the ticker list;
    a ``(CIK ...)`` group, when present, gives the CIK. Missing pieces come back
    as ``None`` / ``[]`` rather than raising — some filers have no tickers.
    """
    if not s:
        return {"issuer": None, "tickers": [], "cik": None}

    cik = None
    m = _CIK_IN_NAME.search(s)
    if m:
        cik = m.group(1)

    issuer = re.sub(r"\s+", " ", s.split("(", 1)[0]).strip() or None

    tickers = []
    for grp in re.findall(r"\(([^)]*)\)", s):
        if grp.strip().upper().startswith("CIK"):
            continue
        for t in grp.split(","):
            t = t.strip().rstrip(".")  # trailing '...' elision -> drop it
            if t:
                tickers.append(t)
        break

    return {"issuer": issuer, "tickers": tickers, "cik": cik}


def parse_hit_id(_id):
    """`_id` is ``<accession>:<document>`` — split it; document may be absent."""
    if _id and ":" in _id:
        acc, doc = _id.split(":", 1)
        return acc, doc
    return (_id or None), None


def parse_hit(hit):
    """Flatten one efts hit into the fields downstream partitioning needs.

    Pulls issuer/tickers/CIK from `display_names[0]`, falls back to the
    structured `_source.ciks` list for the CIK, and resolves `_id` into
    accession + primary document — all without a second request.
    """
    src = hit.get("_source", {}) or {}
    names = src.get("display_names") or []
    info = parse_display_name(names[0]) if names else {"issuer": None, "tickers": [], "cik": None}

    ciks = src.get("ciks") or []
    if not info["cik"] and ciks:
        info["cik"] = ciks[0]

    acc, doc = parse_hit_id(hit.get("_id", ""))
    info.update({
        "accession": acc or src.get("adsh"),
        "document": doc,
        "form": src.get("form"),
        "file_date": src.get("file_date"),
        "period_ending": src.get("period_ending"),
        "ciks": ciks,
        "display_name": names[0] if names else None,
    })
    return info


def search_total(page):
    """Return ``(value, saturated)`` for a search page.

    `hits.total.value` saturates at exactly 10000 (#100): the API stops counting
    there, so a value of 10000 means "at least 10000", not "exactly 10000".
    `saturated` flags that so callers narrow the date window instead of trusting
    the number.
    """
    total = (page.get("hits", {}) or {}).get("total", {}) or {}
    value = total.get("value", 0)
    return value, value >= EFTS_MAX_WINDOW


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
class EdgarClient:
    """SEC-compliant fetch + cache in front of the EDGAR JSON APIs.

    All network egress funnels through `get_bytes` -> the single shared
    `RateLimiter` -> `transport`, so the 10 req/s ceiling holds no matter which
    endpoint method is called or from how many threads. Anything already cached
    returns without touching the limiter or the network at all.
    """

    def __init__(self, user_agent=None, cache_dir=None, rate=DEFAULT_RATE,
                 transport=None, limiter=None, cache=None,
                 max_retries=5, backoff_base=1.0, backoff_cap=60.0,
                 sleep=time.sleep):
        # `user_agent=None` (i.e. the caller omitted it) falls back to the
        # EDGAR_USER_AGENT env var; an explicit "" still raises below. There is
        # still no shipped default UA — the env var itself has none.
        if user_agent is None:
            user_agent = os.environ.get("EDGAR_USER_AGENT", "")
        self.user_agent = validate_user_agent(user_agent)
        if cache is None and not cache_dir:
            raise EdgarConfigError("EdgarClient requires cache_dir (or an explicit cache=).")
        self.limiter = limiter or RateLimiter(rate)
        self.cache = cache or HttpCache(cache_dir)
        self._transport = transport or HttpTransport()
        self.max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self.network_requests = 0  # transport hits (retries included); cache hits don't count
        self.cache_hits = 0        # get_bytes calls served without touching the network
        self.cache_misses = 0      # get_bytes calls that had to fetch (1 per URL, retries excluded)

    # -- headers -----------------------------------------------------------
    def _headers(self):
        return {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
        }

    # -- core fetch --------------------------------------------------------
    def get_bytes(self, url):
        """Return the response body for `url`, from cache when present.

        Cache-first: a hit returns immediately (no limiter, no network). This is
        what makes a second identical run issue zero requests, and what keeps
        re-extraction off the network entirely (issue #99).
        """
        cached = self.cache.get(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.cache_misses += 1
        body = self._fetch(url)
        self.cache.put(url, body)
        return body

    def get_json(self, url):
        return json.loads(self.get_bytes(url).decode("utf-8"))

    def get_text(self, url):
        return self.get_bytes(url).decode("utf-8", "replace")

    def _fetch(self, url):
        headers = self._headers()
        attempt = 0
        while True:
            self.limiter.acquire()          # shared ceiling, before every network hit
            self.network_requests += 1
            status, hdrs, body = self._transport(url, headers)

            if status == 200:
                return body

            if status == 403:
                # Bad/blocked UA. Retrying deepens the ban — fail loud instead.
                raise EdgarConfigError(
                    f"403 for {url}. SEC rejected the request; this almost always means the "
                    f"User-Agent is missing/blocked (currently {self.user_agent!r}). "
                    "Not retrying, to avoid escalating into a longer block."
                )

            if status == 429 or 500 <= status < 600:
                if attempt >= self.max_retries:
                    raise EdgarHTTPError(status, url, body)
                self._sleep(self._backoff(attempt, hdrs))
                attempt += 1
                continue

            raise EdgarHTTPError(status, url, body)

    def _backoff(self, attempt, hdrs):
        """Honour a `Retry-After` header if the server sent one, else exp backoff."""
        ra = hdrs.get("retry-after")
        if ra:
            try:
                return min(float(ra), self._backoff_cap)
            except ValueError:
                pass
        return min(self._backoff_base * (2 ** attempt), self._backoff_cap)

    def close(self):
        if hasattr(self._transport, "close"):
            self._transport.close()

    # -- full-text search (efts) ------------------------------------------
    EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

    def full_text_search(self, q=None, forms=None, startdt=None, enddt=None, from_=0, **params):
        """One page (up to 100 hits) of efts full-text search, as a dict.

        `forms` accepts ``"424B2"`` or ``["424B2", "424B5"]``. Extra kwargs pass
        through as query params (e.g. `dateRange`, `category`).
        """
        query = {}
        if q is not None:
            query["q"] = q
        if forms is not None:
            query["forms"] = forms if isinstance(forms, str) else ",".join(forms)
        if startdt:
            query["startdt"] = startdt
        if enddt:
            query["enddt"] = enddt
        if from_:
            query["from"] = from_
        query.update(params)
        return self.get_json(self.EFTS_URL + "?" + urlencode(query))

    def iter_hits(self, q=None, forms=None, startdt=None, enddt=None,
                  max_results=EFTS_MAX_WINDOW, **params):
        """Yield hits across pages, stopping at the 10k window / declared total.

        Pages by 100 (`EFTS_PAGE_SIZE`) and never asks past `from=10000` because
        efts refuses it (#100). Callers wanting the *whole* corpus past 10k must
        slice the date window — `search_total(...)[1]` flags when that's needed.
        """
        got = 0
        frm = 0
        while frm < EFTS_MAX_WINDOW and got < max_results:
            page = self.full_text_search(q=q, forms=forms, startdt=startdt,
                                          enddt=enddt, from_=frm, **params)
            hits = (page.get("hits", {}) or {}).get("hits", []) or []
            if not hits:
                break
            for h in hits:
                yield h
                got += 1
                if got >= max_results:
                    return
            total, _ = search_total(page)
            frm += len(hits)
            if frm >= total:
                break

    # -- filing history (submissions) -------------------------------------
    def submissions(self, cik):
        """Full filing history for a CIK (data.sec.gov/submissions)."""
        return self.get_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json")

    # -- accession manifest (Archives index.json) -------------------------
    def filing_index(self, cik, accession):
        """The document manifest (index.json) for one accession."""
        acc = accession.replace("-", "")
        return self.get_json(
            f"https://www.sec.gov/Archives/edgar/data/{cik_bare(cik)}/{acc}/index.json"
        )

    def filing_index_page(self, cik, accession):
        """The HTML index page (``{accession}-index.html``) for one accession.

        Unlike ``index.json``, whose per-document ``type`` is the directory-icon
        filename, this page's Type column carries the AUTHORITATIVE exhibit type
        (``EX-99.1``, ``EX-99.2`` ...). It is the small fetch -- the alternative
        authoritative source, the full submission ``.txt``, is the entire filing
        concatenated. Used to resolve an EX-99 exhibit from its declared type
        rather than guessing from its filename.
        """
        acc = accession.replace("-", "")
        return self.get_text(
            f"https://www.sec.gov/Archives/edgar/data/{cik_bare(cik)}/{acc}/{accession}-index.html"
        )

    def archive_document(self, cik, accession, filename):
        """Fetch one document's raw bytes out of an accession's Archives folder."""
        acc = accession.replace("-", "")
        return self.get_bytes(
            f"https://www.sec.gov/Archives/edgar/data/{cik_bare(cik)}/{acc}/{filename}"
        )

    @staticmethod
    def primary_document(index_json):
        """Pick the primary document filename out of an index.json manifest.

        Heuristic (documents itself so #102 can override): skip the index pages
        and image/XML sidecars, prefer the largest ``.htm``/``.html`` item, and
        fall back to the largest ``.txt`` (the old full-submission format). The
        primary prospectus is reliably the biggest rendered doc in the folder.
        """
        items = ((index_json.get("directory", {}) or {}).get("item", []) or [])

        def size(it):
            try:
                return int(it.get("size") or 0)
            except (TypeError, ValueError):
                return 0

        def pick(pred):
            cands = [it for it in items if pred(it.get("name", "").lower())]
            return max(cands, key=size)["name"] if cands else None

        return (
            pick(lambda n: n.endswith((".htm", ".html")) and not n.startswith(("0001", "index")))
            or pick(lambda n: n.endswith((".htm", ".html")))
            or pick(lambda n: n.endswith(".txt"))
        )

    # -- ticker -> CIK -----------------------------------------------------
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

    def company_tickers(self):
        """Map every exchange ticker to its zero-padded CIK10.

        Parses `company_tickers.json`, whose live shape is a dict keyed by an
        arbitrary row index: ``{"0": {"cik_str": 320193, "ticker": "AAPL",
        "title": "Apple Inc."}, ...}``. Tickers are upper-cased so lookups are
        case-insensitive; CIKs are padded to the CIK10 the XBRL/submissions
        endpoints expect, so the value drops straight into `company_facts` etc.
        Cached like any other fetch — a second call issues zero network.
        """
        raw = self.get_json(self.TICKERS_URL)
        rows = raw.values() if isinstance(raw, dict) else raw
        out = {}
        for row in rows:
            tick = (row.get("ticker") or "").strip().upper()
            if tick:
                out[tick] = cik10(row["cik_str"])
        return out

    def ticker_to_cik(self, ticker):
        """Resolve a ticker to its CIK10, case-insensitively.

        Raises `EdgarLookupError` (not None) for an unknown ticker so a typo
        surfaces here rather than as a 404 on a malformed CIK URL downstream.
        """
        key = (ticker or "").strip().upper()
        cik = self.company_tickers().get(key)
        if cik is None:
            raise EdgarLookupError(f"ticker {ticker!r} not found in {self.TICKERS_URL}")
        return cik

    # -- XBRL --------------------------------------------------------------
    def company_facts(self, cik):
        return self.get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")

    def company_concept(self, cik, taxonomy, tag):
        return self.get_json(
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10(cik)}/{taxonomy}/{tag}.json"
        )

    def frames(self, taxonomy, tag, unit, period):
        return self.get_json(
            f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
        )

    # -- quarterly form index (backfill) ----------------------------------
    def form_index(self, year, qtr):
        """Raw ``form.idx`` text for a quarter (used for full-corpus backfill)."""
        return self.get_text(
            f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/form.idx"
        )
