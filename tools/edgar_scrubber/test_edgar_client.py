"""
Gate for the EDGAR client core (issue #99). Same convention as
`lookahead-gate/ab_truncation_test.py` and `pipeline-mock/test_mock_server.py`:
stdlib only, run directly, exit 0 = pass.

Every acceptance criterion in #99 is checked here WITHOUT touching the network,
by injecting a fake transport that counts calls and can script status codes:

  * cache -> a second identical run issues ZERO network requests (offline proof);
  * one shared rate limiter, verified UNDER CONCURRENCY (threads), not serially;
  * retry/backoff on 429/5xx, and a 403 that fails loud instead of retrying;
  * issuer + CIK parsed out of `display_names` for downstream partitioning;
  * every endpoint builds the exact documented URL (CIK padding, nodash, efts qs).

The live integration test that #99 also names (search 424B2 over a one-week
window -> resolve an accession -> fetch its primary doc, all under the rate cap,
second run zero network) is gated behind EDGAR_LIVE=1 + EDGAR_USER_AGENT so this
suite stays green offline. Set both to run it against the real SEC.

Run:  python tools/edgar_scrubber/test_edgar_client.py

      bash:       EDGAR_LIVE=1 EDGAR_USER_AGENT="You you@example.com" python tools/edgar_scrubber/test_edgar_client.py
      PowerShell: $env:EDGAR_LIVE="1"; $env:EDGAR_USER_AGENT="You you@example.com"; python tools/edgar_scrubber/test_edgar_client.py

      See SETUP.md#edgar_user_agent-sec-live-tests for a persistent (non-inline) way to set the UA.
"""
import json
import os
import shutil
import tempfile
import threading
import time

import edgar_client as ec

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
# Fake transport — records calls, scripts responses. No network.
# --------------------------------------------------------------------------- #
class FakeTransport:
    """Callable with the same shape as HttpTransport: (url, headers) -> tuple.

    `routes` maps a URL to either a single (status, headers, body) response or a
    LIST of them (popped left-to-right, to script 429-then-200 retries). Records
    every call so tests can assert exact network counts and header compliance.
    """

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.calls = []
        self.headers_seen = []

    def __call__(self, url, headers):
        self.calls.append(url)
        self.headers_seen.append(headers)
        resp = self.routes.get(url, self.default)
        if isinstance(resp, list):
            resp = resp.pop(0) if len(resp) > 1 else resp[0]
        if resp is None:
            raise AssertionError(f"FakeTransport got an unrouted URL: {url}")
        return resp


def ok_json(obj):
    return (200, {"content-type": "application/json"}, json.dumps(obj).encode("utf-8"))


UA = "Test Runner ci@example.com"


def new_cache_dir():
    d = tempfile.mkdtemp(prefix="edgar-cache-")
    return d


# --------------------------------------------------------------------------- #
# 1. User-Agent validation — no anonymous crawls, no shipped default
# --------------------------------------------------------------------------- #
def test_user_agent():
    section("User-Agent validation (SEC blocks anonymous crawls; no default address)")
    check("valid '<name> <email>' accepted", ec.validate_user_agent(UA) == UA)
    for bad in ("", "   ", "NoEmailHere", "just a name"):
        try:
            ec.validate_user_agent(bad)
            check(f"rejects {bad!r}", False)
        except ec.EdgarConfigError:
            check(f"rejects {bad!r}", True)
    # constructing a client with no UA must fail loud, not silently go anonymous
    try:
        ec.EdgarClient("", new_cache_dir())
        check("EdgarClient('') raises EdgarConfigError", False)
    except ec.EdgarConfigError:
        check("EdgarClient('') raises EdgarConfigError", True)


# --------------------------------------------------------------------------- #
# 2. Compliant headers on every request
# --------------------------------------------------------------------------- #
def test_headers():
    section("Every request carries UA + Accept-Encoding: gzip + keep-alive")
    url = ec.EdgarClient.EFTS_URL + "?q=test"
    ft = FakeTransport({url: ok_json({"hits": {"hits": []}})})
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft)
    client.full_text_search(q="test")
    h = ft.headers_seen[0]
    check("User-Agent header == configured UA", h.get("User-Agent") == UA)
    check("Accept-Encoding: gzip present", h.get("Accept-Encoding") == "gzip")
    check("Connection: keep-alive (connection reuse)", h.get("Connection") == "keep-alive")


# --------------------------------------------------------------------------- #
# 3. Cache: second identical run = ZERO network (the load-bearing guarantee)
# --------------------------------------------------------------------------- #
def test_cache_zero_network():
    section("Cache: re-extraction never refetches; 2nd identical run = 0 requests")
    cache_dir = new_cache_dir()
    url = ec.EdgarClient.EFTS_URL + "?q=BUFFER&forms=424B2"
    payload = {"hits": {"hits": [{"_id": "x:y"}], "total": {"value": 1}}}

    ft1 = FakeTransport({url: ok_json(payload)})
    c1 = ec.EdgarClient(UA, cache_dir, transport=ft1)
    r1 = c1.full_text_search(q="BUFFER", forms="424B2")
    r2 = c1.full_text_search(q="BUFFER", forms="424B2")  # same call, warm cache
    check("same client, 2nd call served from cache (1 network hit total)", len(ft1.calls) == 1)
    check("client.network_requests counts only the miss", c1.network_requests == 1)
    check("cached result identical to fetched", r1 == r2 == payload)
    check("cache file physically on disk, gzipped", c1.cache.has(url))

    # a brand-new process/client over the SAME cache dir must hit network 0 times
    ft2 = FakeTransport({}, default=None)  # any network call -> AssertionError
    c2 = ec.EdgarClient(UA, cache_dir, transport=ft2)
    r3 = c2.full_text_search(q="BUFFER", forms="424B2")
    check("fresh client on same cache dir: ZERO network requests", len(ft2.calls) == 0)
    check("fresh client returns the cached payload", r3 == payload)


# --------------------------------------------------------------------------- #
# 4. Rate limiter — bounded throughput UNDER CONCURRENCY
# --------------------------------------------------------------------------- #
def test_rate_limiter_concurrency():
    section("Rate limiter holds the ceiling under concurrency (threads, not serial)")
    rate = 50.0                      # scaled up so the test runs in ~2s, mechanism identical
    n = 100
    limiter = ec.RateLimiter(rate)
    stamps = []
    stamps_lock = threading.Lock()
    start_barrier = threading.Barrier(n)

    def worker():
        start_barrier.wait()         # release all threads together -> real contention
        limiter.acquire()
        with stamps_lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(n)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0

    # throughput bounded: n slots spaced by 1/rate can't finish before (n-1)/rate
    lower = (n - 1) / rate * 0.9
    check(f"{n} concurrent acquires took >= {lower:.2f}s (got {elapsed:.2f}s) — actually throttled",
          elapsed >= lower)

    # and no 1-second rolling window exceeds the ceiling (allow small boundary slack)
    stamps.sort()
    worst = 0
    for i, ti in enumerate(stamps):
        j = i
        while j < len(stamps) and stamps[j] < ti + 1.0:
            j += 1
        worst = max(worst, j - i)
    check(f"no 1.0s window exceeds rate+2 (worst window held {worst}, rate={int(rate)})",
          worst <= rate + 2)


# --------------------------------------------------------------------------- #
# 5. Shared limiter across call sites (one ceiling for the whole process)
# --------------------------------------------------------------------------- #
def test_shared_limiter():
    section("ONE shared limiter across every endpoint method, not per-call-site")
    counter = {"n": 0}

    class CountingLimiter(ec.RateLimiter):
        def acquire(self):
            counter["n"] += 1
            super().acquire()

    limiter = CountingLimiter(rate=1000.0)
    sub_url = "https://data.sec.gov/submissions/CIK0000320193.json"
    idx_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/index.json"
    ft = FakeTransport({sub_url: ok_json({"cik": "320193"}), idx_url: ok_json({"directory": {}})})
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft, limiter=limiter)

    check("client uses the injected limiter object", client.limiter is limiter)
    client.submissions("320193")
    client.filing_index("320193", "0000320193-23-000106")
    check("two different endpoints both went through the one limiter", counter["n"] == 2)


# --------------------------------------------------------------------------- #
# 6. Retry/backoff on 429 and 5xx; Retry-After honoured
# --------------------------------------------------------------------------- #
def test_retry_backoff():
    section("Retry with backoff on 429/5xx (Retry-After honoured)")
    slept = []
    url = ec.EdgarClient.EFTS_URL + "?q=retry"
    ft = FakeTransport({url: [
        (429, {"retry-after": "2"}, b"slow down"),
        (503, {}, b"unavailable"),
        (200, {"content-type": "application/json"}, json.dumps({"ok": True}).encode()),
    ]})
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft,
                            sleep=slept.append, backoff_base=1.0)
    out = client.full_text_search(q="retry")
    check("eventually succeeds after 429 -> 503 -> 200", out == {"ok": True})
    check("made 3 network attempts", len(ft.calls) == 3)
    check("network_requests counts all attempts incl. retries", client.network_requests == 3)
    check("honoured Retry-After: 2 on first backoff", slept and slept[0] == 2.0)
    check("exponential backoff on the 503 (2^1 * base = 2.0)", len(slept) == 2 and slept[1] == 2.0)


def test_retry_exhausted():
    section("Retries exhaust -> EdgarHTTPError (doesn't loop forever)")
    url = ec.EdgarClient.EFTS_URL + "?q=down"
    ft = FakeTransport({url: (500, {}, b"boom")})
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft,
                            sleep=lambda s: None, max_retries=3)
    try:
        client.full_text_search(q="down")
        check("raises EdgarHTTPError after max_retries", False)
    except ec.EdgarHTTPError as e:
        check("raises EdgarHTTPError after max_retries", e.status == 500)
    check("attempted exactly max_retries+1 times", len(ft.calls) == 4)


# --------------------------------------------------------------------------- #
# 7. 403 fails loud, not retried (avoid escalating a UA ban)
# --------------------------------------------------------------------------- #
def test_403_fails_loud():
    section("Sustained 403 = config error, fail loud, DO NOT retry")
    url = ec.EdgarClient.EFTS_URL + "?q=blocked"
    ft = FakeTransport({url: (403, {}, b"Forbidden")})
    slept = []
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft, sleep=slept.append)
    try:
        client.full_text_search(q="blocked")
        check("403 raises EdgarConfigError", False)
    except ec.EdgarConfigError:
        check("403 raises EdgarConfigError", True)
    check("403 was NOT retried (single attempt)", len(ft.calls) == 1)
    check("403 did NOT sleep/backoff into a longer ban", slept == [])


# --------------------------------------------------------------------------- #
# 8. display_names / hit parsing — issuer + CIK for downstream partitioning
# --------------------------------------------------------------------------- #
def test_display_name_parsing():
    section("Issuer + CIK parsed out of display_names (partition key, #106/#107)")
    p = ec.parse_display_name("JPMORGAN CHASE & CO  (JPM, AMJB, JPM-P...)")
    check("issuer extracted", p["issuer"] == "JPMORGAN CHASE & CO")
    check("tickers extracted, trailing '...' stripped", p["tickers"] == ["JPM", "AMJB", "JPM-P"])

    p2 = ec.parse_display_name("BANK OF AMERICA CORP /DE/  (BAC, BML-G)  (CIK 0000070858)")
    check("CIK parsed from trailing (CIK ...) group", p2["cik"] == "0000070858")
    check("tickers parsed, CIK group not mistaken for a ticker", p2["tickers"] == ["BAC", "BML-G"])
    check("issuer stops before first paren", p2["issuer"] == "BANK OF AMERICA CORP /DE/")

    p3 = ec.parse_display_name("SOME PRIVATE FILER LLC")
    check("no-ticker filer degrades gracefully", p3["issuer"] == "SOME PRIVATE FILER LLC"
          and p3["tickers"] == [] and p3["cik"] is None)

    hit = {
        "_id": "0001740472-24-002631:d12345.htm",
        "_source": {
            "display_names": ["MORGAN STANLEY  (MS, MS-PA)  (CIK 0000895421)"],
            "ciks": ["0000895421"],
            "form": "424B2",
            "file_date": "2024-06-03",
            "adsh": "0001740472-24-002631",
        },
    }
    h = ec.parse_hit(hit)
    check("hit accession from _id", h["accession"] == "0001740472-24-002631")
    check("hit primary document from _id", h["document"] == "d12345.htm")
    check("hit issuer parsed", h["issuer"] == "MORGAN STANLEY")
    check("hit CIK available for partitioning", h["cik"] == "0000895421")
    check("hit form carried through", h["form"] == "424B2")

    acc, doc = ec.parse_hit_id("0001-1:doc.htm")
    check("parse_hit_id splits accession:document", acc == "0001-1" and doc == "doc.htm")


# --------------------------------------------------------------------------- #
# 9. Search total saturation (#100: caps at exactly 10000)
# --------------------------------------------------------------------------- #
def test_search_total_saturation():
    section("total.value saturates at 10000 -> flagged as a cap, not a count (#100)")
    v, sat = ec.search_total({"hits": {"total": {"value": 342}}})
    check("under the cap: not saturated", v == 342 and sat is False)
    v, sat = ec.search_total({"hits": {"total": {"value": 10000}}})
    check("at 10000: flagged saturated (means '>= 10000')", v == 10000 and sat is True)


# --------------------------------------------------------------------------- #
# 10. Endpoint URL construction (CIK padding, nodash accession, efts qs)
# --------------------------------------------------------------------------- #
def test_url_construction():
    section("Every endpoint builds the exact documented URL")
    captured = []

    def spy(url, headers):
        captured.append(url)
        return ok_json({"directory": {"item": []}})

    client = ec.EdgarClient(UA, new_cache_dir(), transport=spy)

    client.submissions("320193")
    check("submissions pads CIK to 10 digits",
          captured[-1] == "https://data.sec.gov/submissions/CIK0000320193.json")

    client.submissions("0000320193")  # already padded -> same URL, so cached (no new call)
    check("already-padded CIK normalises to the same URL (cache hit, no new request)",
          captured[-1] == "https://data.sec.gov/submissions/CIK0000320193.json"
          and len([u for u in captured if u.endswith("CIK0000320193.json")]) == 1)

    client.filing_index("0000320193", "0000320193-23-000106")
    check("filing_index strips CIK zeros + accession dashes",
          captured[-1] == "https://www.sec.gov/Archives/edgar/data/320193/"
                          "000032019323000106/index.json")

    client.archive_document("320193", "0000320193-23-000106", "aapl-20230701.htm")
    check("archive_document builds the doc path",
          captured[-1] == "https://www.sec.gov/Archives/edgar/data/320193/"
                          "000032019323000106/aapl-20230701.htm")

    client.company_facts("320193")
    check("company_facts XBRL URL",
          captured[-1] == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")

    client.company_concept("320193", "us-gaap", "Assets")
    check("company_concept XBRL URL",
          captured[-1] == "https://data.sec.gov/api/xbrl/companyconcept/"
                          "CIK0000320193/us-gaap/Assets.json")

    client.frames("us-gaap", "Assets", "USD", "CY2023Q1I")
    check("frames XBRL URL",
          captured[-1] == "https://data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY2023Q1I.json")

    client.form_index(2024, 2)
    check("form_index quarterly URL",
          captured[-1] == "https://www.sec.gov/Archives/edgar/full-index/2024/QTR2/form.idx")

    client.full_text_search(q="autocallable", forms=["424B2", "424B5"],
                            startdt="2024-01-01", enddt="2024-01-07", from_=100)
    u = captured[-1]
    check("efts URL base", u.startswith(ec.EdgarClient.EFTS_URL + "?"))
    for frag in ("q=autocallable", "forms=424B2%2C424B5",
                 "startdt=2024-01-01", "enddt=2024-01-07", "from=100"):
        check(f"efts querystring has {frag}", frag in u)


# --------------------------------------------------------------------------- #
# 10b. ticker -> CIK lookup + XBRL fact endpoints (#182)
# --------------------------------------------------------------------------- #
_TICKERS_FIXTURE = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 19617, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"},
    "2": {"cik_str": 70858, "ticker": "BAC", "title": "BANK OF AMERICA CORP"},
}


def test_ticker_to_cik():
    section("ticker -> CIK10, case-insensitive, unknown fails loud (#182)")
    ft = FakeTransport({ec.EdgarClient.TICKERS_URL: ok_json(_TICKERS_FIXTURE)})
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft)

    mapping = client.company_tickers()
    check("company_tickers maps ticker -> CIK10 from the SEC file",
          mapping["AAPL"] == "0000320193" and mapping["JPM"] == "0000019617")
    check("company_tickers requested exactly the SEC file URL",
          ft.calls[0] == ec.EdgarClient.TICKERS_URL)

    check("ticker_to_cik('aapl') == '0000320193'", client.ticker_to_cik("aapl") == "0000320193")
    check("ticker_to_cik('AAPL') == '0000320193'", client.ticker_to_cik("AAPL") == "0000320193")

    try:
        client.ticker_to_cik("NOSUCHTICKER")
        check("unknown ticker raises (not None)", False)
    except ec.EdgarError as e:
        check("unknown ticker raises an EdgarError subclass",
              isinstance(e, ec.EdgarError) and type(e) is not ec.EdgarError)

    # everything above resolved from ONE fetch of the tickers file (cache)
    check("all ticker lookups shared a single network fetch",
          len([u for u in ft.calls if u == ec.EdgarClient.TICKERS_URL]) == 1)


def test_xbrl_endpoints_zero_second_network():
    section("XBRL fact endpoints build exact URLs; 2nd call = ZERO network (#182)")
    concept_url = ("https://data.sec.gov/api/xbrl/companyconcept/"
                   "CIK0000320193/us-gaap/Revenues.json")
    facts_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    ft = FakeTransport({
        concept_url: ok_json({"tag": "Revenues"}),
        facts_url: ok_json({"cik": 320193}),
        ec.EdgarClient.TICKERS_URL: ok_json(_TICKERS_FIXTURE),
    })
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft)

    client.company_concept("320193", "us-gaap", "Revenues")
    check("company_concept requests the exact XBRL URL", ft.calls[-1] == concept_url)
    client.company_facts("320193")
    check("company_facts requests the exact XBRL URL", ft.calls[-1] == facts_url)

    # a ticker-driven pull resolves the CIK then reads facts; repeat = 0 network
    client.company_facts(client.ticker_to_cik("aapl"))
    n_after_warm = len(ft.calls)
    client.company_concept("320193", "us-gaap", "Revenues")
    client.company_facts("320193")
    client.company_tickers()
    client.company_facts(client.ticker_to_cik("AAPL"))
    check("every repeated new-method call served from cache (zero new network)",
          len(ft.calls) == n_after_warm)


# --------------------------------------------------------------------------- #
# 11. iter_hits pagination + stop conditions
# --------------------------------------------------------------------------- #
def test_iter_hits_pagination():
    section("iter_hits pages by 100 and stops at the declared total")
    base = ec.EdgarClient.EFTS_URL

    def page(ids, total):
        return ok_json({"hits": {"hits": [{"_id": i} for i in ids],
                                 "total": {"value": total}}})

    p0 = base + "?" + "q=x&forms=424B2"                 # from_=0 omitted
    p1 = base + "?" + "q=x&forms=424B2&from=2"
    routes = {p0: page(["a:1", "b:2"], 3), p1: page(["c:3"], 3)}
    client = ec.EdgarClient(UA, new_cache_dir(),
                            transport=FakeTransport(routes))
    got = list(client.iter_hits(q="x", forms="424B2"))
    check("walked both pages until total reached", [h["_id"] for h in got] == ["a:1", "b:2", "c:3"])

    # max_results cuts pagination short
    client2 = ec.EdgarClient(UA, new_cache_dir(),
                             transport=FakeTransport(routes))
    got2 = list(client2.iter_hits(q="x", forms="424B2", max_results=1))
    check("max_results caps the walk", len(got2) == 1)


# --------------------------------------------------------------------------- #
# 12. primary_document heuristic
# --------------------------------------------------------------------------- #
def test_primary_document():
    section("primary_document picks the rendered prospectus out of a manifest")
    index_json = {"directory": {"item": [
        {"name": "0000320193-23-000106-index.htm", "size": "1000"},
        {"name": "aapl-20230701.htm", "size": "300000"},
        {"name": "R1.htm", "size": "2000"},
        {"name": "logo.jpg", "size": "5000"},
        {"name": "filing.txt", "size": "50000"},
    ]}}
    check("largest real .htm chosen over index/exhibit pages",
          ec.EdgarClient.primary_document(index_json) == "aapl-20230701.htm")
    txt_only = {"directory": {"item": [{"name": "0001-full.txt", "size": "80000"}]}}
    check("falls back to .txt full submission",
          ec.EdgarClient.primary_document(txt_only) == "0001-full.txt")


# --------------------------------------------------------------------------- #
# 13. LIVE integration (gated) — the exact #99 acceptance scenario
# --------------------------------------------------------------------------- #
def test_live_integration():
    section("LIVE integration against the real SEC (gated by EDGAR_LIVE=1 + EDGAR_USER_AGENT)")
    if os.environ.get("EDGAR_LIVE") != "1":
        print("  [skip] set EDGAR_LIVE=1 and EDGAR_USER_AGENT='<name> <email>' to run this")
        return
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        check("EDGAR_USER_AGENT set when EDGAR_LIVE=1", False)
        return

    cache_dir = new_cache_dir()
    try:
        client = ec.EdgarClient(ua, cache_dir)  # real transport, real 10 req/s cap
        # search 424B2 over a one-week window
        hits = list(client.iter_hits(forms="424B2", startdt="2024-06-03",
                                      enddt="2024-06-07", max_results=5))
        check("search returned at least one 424B2 hit", len(hits) > 0)
        h = ec.parse_hit(hits[0])
        check("issuer + CIK parsed from the live hit", bool(h["cik"]))

        # resolve one accession to its document list, fetch the primary document
        idx = client.filing_index(h["cik"], h["accession"])
        primary = ec.EdgarClient.primary_document(idx)
        check("resolved a primary document from index.json", bool(primary))
        doc = client.archive_document(h["cik"], h["accession"], primary)
        check("fetched the primary document bytes", len(doc) > 0)

        n_first = client.network_requests

        # second identical run: ZERO network
        client2 = ec.EdgarClient(ua, cache_dir)
        list(client2.iter_hits(forms="424B2", startdt="2024-06-03",
                               enddt="2024-06-07", max_results=5))
        idx2 = client2.filing_index(h["cik"], h["accession"])
        client2.archive_document(h["cik"], h["accession"], ec.EdgarClient.primary_document(idx2))
        check(f"second identical run issued ZERO network requests (first run: {n_first})",
              client2.network_requests == 0)
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def main():
    tests = [
        test_user_agent, test_headers, test_cache_zero_network,
        test_rate_limiter_concurrency, test_shared_limiter,
        test_retry_backoff, test_retry_exhausted, test_403_fails_loud,
        test_display_name_parsing, test_search_total_saturation,
        test_url_construction, test_ticker_to_cik,
        test_xbrl_endpoints_zero_second_network,
        test_iter_hits_pagination, test_primary_document,
        test_live_integration,
    ]
    for t in tests:
        t()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nEDGAR client core: PASS")


if __name__ == "__main__":
    main()
