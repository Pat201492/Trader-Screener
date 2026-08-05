"""
Gate for the date-chunked resumable crawl (issue #100), on top of #99's
`edgar_client`. Same convention as `test_edgar_client.py`: stdlib only, no
network, run directly, exit 0 = pass -- proven by injecting a fake transport
that scripts responses and records exactly which URLs were hit.

Every #100 acceptance criterion is checked here:

  * a query whose total saturates (>= 10000) gets bisected, and the resulting
    accession set equals the union of manually-bisected halves -- nothing
    silently dropped;
  * total == 10000 always triggers a split, never gets treated as a count;
  * kill mid-crawl (simulate via a hand-built partial state file) -> restart
    -> resume at the last completed chunk, no duplicate request for
    already-completed work, no gap in the final accession set;
  * the run summary reports accessions, chunks issued, requests made, cache
    hit rate;
  * SavedQuery round-trips through JSON, and re-running the same query id with
    an extended date window is an incremental crawl (diffs against the prior
    run) while changing search criteria under the same id starts clean.

Run:  python tools/edgar_scrubber/test_crawl.py
"""
import json
import os
import shutil
import tempfile
from urllib.parse import urlencode

import crawl as cr
import edgar_client as ec

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


UA = "Test Runner ci@example.com"


def new_cache_dir():
    return tempfile.mkdtemp(prefix="edgar-cache-")


def new_state_path():
    d = tempfile.mkdtemp(prefix="edgar-state-")
    return os.path.join(d, "state.json")


# --------------------------------------------------------------------------- #
# Fake transport -- same shape/behaviour as the one in test_edgar_client.py
# --------------------------------------------------------------------------- #
class FakeTransport:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        resp = self.routes.get(url)
        if resp is None:
            raise AssertionError(f"FakeTransport got an unrouted URL: {url}")
        return resp


def hits_page(ids, total):
    """A page carrying both `total` (what a count-check reads) and up to
    `len(ids)` hits (what a leaf fetch reads) -- the same response serves
    both roles, mirroring the real efts response shape."""
    body = json.dumps({"hits": {"hits": [{"_id": i} for i in ids],
                                "total": {"value": total}}}).encode()
    return (200, {"content-type": "application/json"}, body)


def efts_url(**params):
    return ec.EdgarClient.EFTS_URL + "?" + urlencode(params)


# --------------------------------------------------------------------------- #
# 1. Bisection: saturated total -> split; leaf set == union of manual halves
# --------------------------------------------------------------------------- #
def test_bisection_matches_manual_union():
    section("Saturated total forces a split; result == union of manually-bisected halves (#100)")
    q = cr.SavedQuery(id="bisect-q", forms=["424B2"], startdt="2025-01-01", enddt="2025-01-04")

    top_url = efts_url(forms="424B2", startdt="2025-01-01", enddt="2025-01-04")
    left_url = efts_url(forms="424B2", startdt="2025-01-01", enddt="2025-01-02")
    right_url = efts_url(forms="424B2", startdt="2025-01-03", enddt="2025-01-04")

    routes = {
        top_url: hits_page([], 10000),                                  # saturated: must split
        left_url: hits_page(["acc-l1:doc", "acc-l2:doc"], 2),
        right_url: hits_page(["acc-r1:doc", "acc-r2:doc", "acc-r3:doc"], 3),
    }

    client = ec.EdgarClient(UA, new_cache_dir(), transport=FakeTransport(routes))
    crawler = cr.Crawler(client, q, state_path=new_state_path())
    result = crawler.run()

    check("top-level (saturated) range is NOT recorded as a completed leaf",
          ["2025-01-01", "2025-01-04"] not in crawler.state.completed)
    check("only the two bisected halves were recorded as completed leaves",
          sorted(crawler.state.completed) ==
          [["2025-01-01", "2025-01-02"], ["2025-01-03", "2025-01-04"]])

    got = set(result["new_accessions"])
    expected = {"acc-l1", "acc-l2", "acc-r1", "acc-r2", "acc-r3"}
    check("crawl's accession set", got == expected)

    # Manually bisect the SAME range with plain iter_hits calls and union them
    # -- this is the literal #100 acceptance scenario.
    manual_client = ec.EdgarClient(UA, new_cache_dir(), transport=FakeTransport(routes))
    manual = set()
    for hit in manual_client.iter_hits(forms="424B2", startdt="2025-01-01", enddt="2025-01-02"):
        manual.add(ec.parse_hit(hit)["accession"])
    for hit in manual_client.iter_hits(forms="424B2", startdt="2025-01-03", enddt="2025-01-04"):
        manual.add(ec.parse_hit(hit)["accession"])
    check("crawl accession set == union of manually-bisected halves", got == manual)

    summary = result["summary"]
    check("2 leaf chunks issued (top-level split doesn't count as a chunk)",
          summary["chunks_issued"] == 2)
    check("3 network requests: top count-check + left leaf + right leaf",
          summary["requests_made"] == 3)
    check("cache hit rate > 0 (iter_hits' first page reuses the count-check's cache entry)",
          summary["cache_hit_rate"] > 0)
    check("done", summary["done"] is True)
    check("accessions reported in summary", summary["accessions"] == 5)


# --------------------------------------------------------------------------- #
# 2. total == 10000 exactly always triggers a split, never trusted as a count
# --------------------------------------------------------------------------- #
def test_exactly_10000_always_splits():
    section("hits.total.value == 10000 -> always a split signal, never a count (#100)")
    q = cr.SavedQuery(id="cap-q", forms=["424B2"], startdt="2025-03-01", enddt="2025-03-02")
    top_url = efts_url(forms="424B2", startdt="2025-03-01", enddt="2025-03-02")
    left_url = efts_url(forms="424B2", startdt="2025-03-01", enddt="2025-03-01")
    right_url = efts_url(forms="424B2", startdt="2025-03-02", enddt="2025-03-02")
    routes = {
        top_url: hits_page([], 10000),   # exactly the cap -- must split, not be counted as "10000 hits"
        left_url: hits_page(["a:1"], 1),
        right_url: hits_page(["b:1"], 1),
    }
    client = ec.EdgarClient(UA, new_cache_dir(), transport=FakeTransport(routes))
    crawler = cr.Crawler(client, q, state_path=new_state_path())
    crawler.run()

    check("the exactly-10000 range was split, not recorded as a completed leaf",
          ["2025-03-01", "2025-03-02"] not in crawler.state.completed)
    check("both bisected single-day halves were completed instead",
          ["2025-03-01", "2025-03-01"] in crawler.state.completed and
          ["2025-03-02", "2025-03-02"] in crawler.state.completed)
    check("no warning emitted (both halves resolved under the cap)",
          crawler.state.warnings == [])


# --------------------------------------------------------------------------- #
# 3. Kill mid-crawl -> restart -> resume with no duplicate work, no gap
# --------------------------------------------------------------------------- #
def test_resume_after_kill():
    section("Kill mid-crawl -> restart -> resume at the last completed chunk (#100)")
    q = cr.SavedQuery(id="resume-q", forms=["424B2"], startdt="2025-01-01", enddt="2025-01-04")
    state_path = new_state_path()

    # Hand-build the state a real crash would have left on disk: the top-level
    # split already happened and the left leaf finished; the right leaf never
    # started (this is exactly what `Crawler._process_chunk` persists right
    # after completing "left" and before touching "right").
    state = cr.CrawlState.empty(q)
    state.pending = [["2025-01-03", "2025-01-04"]]
    state.completed = [["2025-01-01", "2025-01-02"]]
    state.accessions = {"acc-l1": {}, "acc-l2": {}}
    state.chunks_issued = 1
    state.save(state_path)

    # Fresh transport routes ONLY the right-chunk URL. If the resumed crawler
    # re-requests the top-level split check or the already-completed left
    # chunk, FakeTransport raises -- proving no duplicate work.
    right_url = efts_url(forms="424B2", startdt="2025-01-03", enddt="2025-01-04")
    ft = FakeTransport({right_url: hits_page(["acc-r1:doc", "acc-r2:doc", "acc-r3:doc"], 3)})
    client = ec.EdgarClient(UA, new_cache_dir(), transport=ft)
    crawler = cr.Crawler(client, q, state_path=state_path)
    result = crawler.run()

    check("resume issues exactly 1 network request (the right chunk only)",
          len(ft.calls) == 1 and ft.calls[0] == right_url)
    check("resume's diff (new_accessions) is only the newly-fetched right chunk",
          set(result["new_accessions"]) == {"acc-r1", "acc-r2", "acc-r3"})
    check("final accession set = old union new, no gap",
          set(crawler.state.accessions) == {"acc-l1", "acc-l2", "acc-r1", "acc-r2", "acc-r3"})
    check("chunks_issued accumulated across the resumed run (1 old + 1 new)",
          crawler.state.chunks_issued == 2)
    check("frontier fully drained", crawler.state.pending == [] and crawler.state.done)

    # And loading the persisted state fresh (a second restart) changes nothing.
    reloaded = cr.CrawlState.load(state_path)
    check("re-loading the saved state after completion is stable (no pending work left)",
          reloaded.pending == [] and reloaded.done)


# --------------------------------------------------------------------------- #
# 4. Summary reports accessions, chunks issued, requests made, cache hit rate
# --------------------------------------------------------------------------- #
def test_summary_fields():
    section("Summary reports accessions / chunks issued / requests made / cache hit rate (#100)")
    state = cr.CrawlState(
        query_id="x", query={"id": "x"},
        accessions={"a": {}, "b": {}, "c": {}},
        chunks_issued=4, requests_made=7,
        cache_hits=3, cache_misses=1, done=True,
    )
    s = state.summary()
    check("accessions counted", s["accessions"] == 3)
    check("chunks_issued carried through", s["chunks_issued"] == 4)
    check("requests_made carried through", s["requests_made"] == 7)
    check("cache_hit_rate == hits / (hits + misses)", s["cache_hit_rate"] == 0.75)
    check("done flag carried through", s["done"] is True)

    empty = cr.CrawlState(query_id="y", query={"id": "y"})
    check("cache_hit_rate is 0.0, not a ZeroDivisionError, with no lookups yet",
          empty.summary()["cache_hit_rate"] == 0.0)


# --------------------------------------------------------------------------- #
# 5. SavedQuery: required fields, JSON round-trip, signature stability
# --------------------------------------------------------------------------- #
def test_saved_query_shape():
    section("SavedQuery: required startdt/enddt, JSON round-trip matches #100's example shape")
    try:
        cr.SavedQuery(id="bad")
        check("startdt/enddt required", False)
    except ValueError:
        check("startdt/enddt required", True)

    q = cr.SavedQuery(id="424b2-structured-notes", q='"contingent coupon"', forms=["424B2"],
                       startdt="2024-01-01", enddt="2026-08-04")
    d = q.to_dict()
    check("to_dict has exactly the #100 example's keys",
          set(d) == {"id", "q", "forms", "startdt", "enddt", "ciks", "excludeCiks"})
    check("forms serializes as a list (JSON array), not a tuple", d["forms"] == ["424B2"])

    tmp_dir = tempfile.mkdtemp(prefix="edgar-query-")
    path = os.path.join(tmp_dir, "q.json")
    try:
        cr.save_query(q, path)
        loaded = cr.load_query(path)
        check("round-trips through JSON unchanged", loaded == q)
        check("signature stable across an identical reload", loaded.signature() == q.signature())

        different_forms = cr.SavedQuery(id=q.id, q=q.q, forms=["424B5"],
                                         startdt=q.startdt, enddt=q.enddt)
        check("changing forms changes the signature", different_forms.signature() != q.signature())

        same_criteria_diff_dates = cr.SavedQuery(id=q.id, q=q.q, forms=list(q.forms),
                                                  startdt="2025-01-01", enddt="2025-12-31")
        check("changing only the date window does NOT change the signature",
              same_criteria_diff_dates.signature() == q.signature())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 6. Re-running the same query id: incremental extension diffs against prior run
# --------------------------------------------------------------------------- #
def test_incremental_rerun_diffs_against_prior():
    section("Same query id, extended enddt -> only the new span is fetched (#100 diff)")
    state_path = new_state_path()

    q1 = cr.SavedQuery(id="incr-q", forms=["424B2"], startdt="2025-02-01", enddt="2025-02-02")
    url1 = efts_url(forms="424B2", startdt="2025-02-01", enddt="2025-02-02")
    ft1 = FakeTransport({url1: hits_page(["acc-1:doc", "acc-2:doc"], 2)})
    client1 = ec.EdgarClient(UA, new_cache_dir(), transport=ft1)
    r1 = cr.crawl(client1, q1, state_path=state_path)
    check("first run finds its 2 accessions", set(r1["new_accessions"]) == {"acc-1", "acc-2"})

    # Same id, same q/forms/ciks, enddt extended two days -> incremental.
    q2 = cr.SavedQuery(id="incr-q", forms=["424B2"], startdt="2025-02-01", enddt="2025-02-04")
    url2 = efts_url(forms="424B2", startdt="2025-02-03", enddt="2025-02-04")
    ft2 = FakeTransport({url2: hits_page(["acc-3:doc"], 1)})  # url1 NOT routed: must not be re-fetched
    client2 = ec.EdgarClient(UA, new_cache_dir(), transport=ft2)
    r2 = cr.crawl(client2, q2, state_path=state_path)

    check("second run issues exactly 1 request (only the new span)", len(ft2.calls) == 1)
    check("second run's diff is only the newly-discovered accession",
          r2["new_accessions"] == ["acc-3"])
    check("full accumulated accession set is old union new",
          r2["summary"]["accessions"] == 3)


def test_signature_change_starts_clean():
    section("Changing search criteria under the same query id starts a clean crawl")
    state_path = new_state_path()
    q1 = cr.SavedQuery(id="chg-q", q=None, forms=["424B2"],
                        startdt="2025-04-01", enddt="2025-04-02")
    url1 = efts_url(forms="424B2", startdt="2025-04-01", enddt="2025-04-02")
    client1 = ec.EdgarClient(UA, new_cache_dir(),
                             transport=FakeTransport({url1: hits_page(["old:doc"], 1)}))
    cr.crawl(client1, q1, state_path=state_path)

    q2 = cr.SavedQuery(id="chg-q", q="a different term", forms=["424B2"],
                        startdt="2025-04-01", enddt="2025-04-02")
    client2 = ec.EdgarClient(UA, new_cache_dir(), transport=FakeTransport({}))
    crawler2 = cr.Crawler(client2, q2, state_path=state_path)
    check("state reset: no leftover completed chunks from the old criteria",
          crawler2.state.completed == [])
    check("state reset: no leftover accessions from the old criteria",
          crawler2.state.accessions == {})
    check("state reset: frontier reinitialised to the new query's full window",
          crawler2.state.pending == [["2025-04-01", "2025-04-02"]])


# --------------------------------------------------------------------------- #
# 7. bisect_range helper: exact coverage, no overlap, no gap
# --------------------------------------------------------------------------- #
def test_bisect_range_helper():
    section("bisect_range: halves cover the input exactly, no overlap, no gap")
    from datetime import date, timedelta
    start, end = date(2025, 1, 1), date(2025, 1, 10)
    (ls, le), (rs, re_) = cr.bisect_range(start, end)
    check("left starts at the range start", ls == start)
    check("right ends at the range end", re_ == end)
    check("no gap between halves", rs == le + timedelta(days=1))
    check("no overlap", le < rs)

    (ls2, le2), (rs2, re2) = cr.bisect_range(date(2025, 1, 1), date(2025, 1, 2))
    check("single-day-span input still splits into two single days",
          ls2 == le2 and rs2 == re2 and le2 + timedelta(days=1) == rs2)

    try:
        cr.bisect_range(date(2025, 1, 1), date(2025, 1, 1))
        check("bisecting a single day raises (nothing left to split)", False)
    except ValueError:
        check("bisecting a single day raises (nothing left to split)", True)


def main():
    tests = [
        test_bisection_matches_manual_union,
        test_exactly_10000_always_splits,
        test_resume_after_kill,
        test_summary_fields,
        test_saved_query_shape,
        test_incremental_rerun_diffs_against_prior,
        test_signature_change_starts_clean,
        test_bisect_range_helper,
    ]
    for t in tests:
        t()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nEDGAR crawl (#100): PASS")


if __name__ == "__main__":
    main()
