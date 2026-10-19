"""
Gate for the local tool-runner service (`tools/server.py`).

Same convention as the scrubber's tests: stdlib only, no network, no model, run
directly, exit 0 = pass. What is checked here is everything the browser depends
on that does NOT need a GPU: the run registry, the doc allowlist (a path guard
is not something to find out about in production), the markdown renderer, and
the store reader's shape.

Run:  python tools/test_server.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "edgar_scrubber"))

import server
import field_spec as _fs

# Loaded once, up here: the span-location checks below need the real spec's
# per-field anchors, not a hand-written stand-in for them.
_spec = _fs.load_spec(Path(__file__).resolve().parent / "edgar_scrubber" /
                      "field_specs" / "424b2_structured_note.json")

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
section("run registry")
# --------------------------------------------------------------------------- #

reg = server.RunRegistry()
rid = reg.create("edgar-scrubber", {"limit": 2, "fields": ["cusip", "barrier_pct"]})
prog = reg.get(rid)["progress"]
check("create returns a namespaced id", rid == "edgar-scrubber-001")
check("a new run is queued with zero progress",
      reg.get(rid)["status"] == "queued" and prog["done"] == 0 and prog["fields_done"] == 0)
check("total work counts fields, not just documents", prog["fields_total"] == 4)

reg.tick_field(rid, "0001-25-1", "cusip", 10.0)
prog = reg.get(rid)["progress"]
check("a finished field advances progress and names what is in flight",
      prog["fields_done"] == 1 and prog["current"]["field"] == "cusip")
check("time remaining is extrapolated from measured elapsed time",
      prog["seconds_remaining"] == 30)

check("a running run accepts a stop request", reg.request_stop(rid) is True)
check("the worker sees the stop flag", reg.stop_requested(rid) is True)
reg.update(rid, status="stopped")
check("a finished run cannot be stopped again", reg.request_stop(rid) is False)
check("stopping an unknown run is refused, not an exception",
      reg.request_stop("no-such-run") is False)

reg.log(rid, "expanding")
reg.add_document(rid, {"accession": "0001-25-1", "fields": [{"field": "cusip", "value": "X"}]})
snap = reg.get(rid)
check("log entries are timestamped", snap["log"][0]["message"] == "expanding" and "t" in snap["log"][0])
check("adding a document advances progress", snap["progress"]["done"] == 1)

snap["documents"].append("mutated")
check("get() hands back a copy, not the live run", len(reg.get(rid)["documents"]) == 1)

reg.update(rid, rows=[{"big": "x" * 1000}], template={"also": "big"})
listed = reg.list()
check("list() omits the heavy per-run fields",
      len(listed) == 1 and "log" not in listed[0] and "documents" not in listed[0])
# The header runs board polls this every couple of seconds from every view, so
# a run's RESULT must not ride along -- only its status and the newest log line.
check("list() omits results too, whatever kind of run produced them",
      "rows" not in listed[0] and "template" not in listed[0])
check("list() carries the latest log line, which is what a status board shows",
      listed[0]["last_log"] == "expanding")
check("list() still carries what the board renders",
      {"id", "kind", "status", "progress", "params"} <= set(listed[0]))

listed[0]["progress"]["done"] = 999
check("list() hands back a copy, not the live runs",
      reg.get(rid)["progress"]["done"] == 1)

# The activity feed. A finished document lists survivors only, so "produced a
# value and then dropped it" is a fact that exists nowhere but here.
reg.event(rid, "field_start", accession="0001-25-1", field="barrier_pct")
reg.event(rid, "field_done", accession="0001-25-1", field="barrier_pct",
          value="60.00%", kept=True, verdict="kept", span=[10, 16])
reg.event(rid, "field_done", accession="0001-25-1", field="coupon_barrier_pct",
          value="13", kept=False, verdict="dropped",
          reason="the filing never uses this field's wording")

feed = reg.events_since(rid)
check("events are returned in order, numbered from zero",
      [e["n"] for e in feed["events"]] == [0, 1, 2])
check("a dropped value is reported as an event, not silently discarded",
      feed["events"][2]["verdict"] == "dropped"
      and feed["events"][2]["value"] == "13"
      and feed["events"][2]["reason"])
check("the feed carries live status and progress beside the events",
      feed["status"] == "stopped" and "fields_done" in feed["progress"])
check("`after` returns only what the poller has not seen",
      [e["n"] for e in reg.events_since(rid, after=1)["events"]] == [2])
check("polling a caught-up feed returns no events, not the whole run",
      reg.events_since(rid, after=2)["events"] == [])
check("events for an unknown run are None, not an empty feed",
      reg.events_since("no-such-run") is None)

feed["events"].append("mutated")
check("events_since() hands back a copy, not the live run",
      len(reg.events_since(rid)["events"]) == 3)

# The buffer is bounded -- a 450-filing run emits thousands of events and this
# registry is the live view, not the record. A poller that fell behind has to be
# able to SEE that it has a hole.
_cap = reg.EVENT_CAP
for i in range(_cap + 25):
    reg.event(rid, "field_start", field=f"f{i}")
capped = reg.events_since(rid)
check("the event buffer is bounded", len(capped["events"]) == _cap)
check("the oldest surviving event number is reported, so a gap is visible",
      capped["dropped_before"] == capped["events"][0]["n"] > 0)

reg.update(rid, status="done")
check("update sets status", reg.get(rid)["status"] == "done")

# --------------------------------------------------------------------------- #
section("span location -- a highlight has to be earned")
# --------------------------------------------------------------------------- #

SAMPLE = ("The Interest Barrier is 70.00% of the Initial Stock Price. "
          "CUSIP:: 48136CYQ6. The estimated value would be approximately "
          "$979.00 per $1,000 principal amount note.")

span, how = server.locate_value(SAMPLE, 70.0)
check("a number is found in the form the filing prints it (70.0 -> '70.00')",
      SAMPLE[span[0]:span[1]] == "70.00" and how == "value-match")
span, how = server.locate_value(SAMPLE, "48136CYQ6")
check("a string value is found verbatim", SAMPLE[span[0]:span[1]] == "48136CYQ6")
span, how = server.locate_value(SAMPLE, 979)
check("an integer highlights the whole printed number, not the bare digits",
      SAMPLE[span[0]:span[1]] == "979.00")

cusip_at = SAMPLE.index("48136CYQ6")
check("a value that is nowhere in the text yields no span at all",
      server.locate_value(SAMPLE, 12345) == (None, None))
check("a model span that really states the value is kept as-is",
      server.locate_value(SAMPLE, "48136CYQ6", (cusip_at, cusip_at + 9))[1] == "model")
check("an empty model span is not believed",
      server.locate_value(SAMPLE, "48136CYQ6", (cusip_at, cusip_at))[1] == "value-match")
check("an absurdly wide model span is not believed",
      server.locate_value(SAMPLE, 70.0, (0, len(SAMPLE)))[1] == "value-match")
check("a list value gets no span rather than a wrong one",
      server.locate_value(SAMPLE, ["a", "b"]) == (None, None))

# Not every note carries every field. A filing that never says "buffer" cannot
# state a buffer, and the same digits appearing elsewhere are a coincidence,
# not evidence.
AUTOCALL = ("Interest Barrier: 70.00% of the Initial Stock Price. "
            "Downside Leverage Factor: 1.42857. Coupon paid quarterly.")
buffer_field = _spec.field("buffer_pct")
barrier_field = _spec.field("coupon_barrier_pct")

check("a field whose vocabulary is absent is reported absent",
      server.field_vocabulary_present(AUTOCALL.lower(), buffer_field) is False)
check("a field whose vocabulary is present is reported present",
      server.field_vocabulary_present(AUTOCALL.lower(), barrier_field) is True)
check("a field the spec gives no anchors for is untestable, not absent",
      server.field_vocabulary_present(AUTOCALL.lower(), _spec.field("underlyings")) is None)
check("anchor positions are found for a present field",
      len(server.anchor_positions(AUTOCALL.lower(), barrier_field)) == 1)

# 1.42857 IS in the text (as the leverage factor), so a bare value match would
# happily "locate" a fabricated buffer of 1.42857.
near = server.anchor_positions(AUTOCALL.lower(), barrier_field)
span, how = server.locate_value(AUTOCALL, 70.0, None, near=near)
check("a value near its own anchor is preferred over the same digits elsewhere",
      AUTOCALL[span[0]:span[1]] == "70.00" and how == "value-match")

# --------------------------------------------------------------------------- #
section("availability scan -- what these filings carry, before any model runs")
# --------------------------------------------------------------------------- #

PROSPECTUS = ("Interest Barrier: 70.00% of the Initial Stock Price. "
              "CUSIP:: 48136CYQ6. Maturity Date:: April 9, 2026.")
GROWTH_NOTE = ("Participation Rate: 150%. Buffer: 10.00%. "
               "CUSIP:: 06744AAA1. Maturity Date:: May 1, 2027.")
_docs = {"0001-26-1": PROSPECTUS, "0001-26-2": GROWTH_NOTE}
_asked = []

_real_load = server.load_document
server.load_document = lambda cik, accession, document=None: (
    _asked.append((accession, document)) or
    {"accession": accession, "document": "primary.htm", "chars": len(_docs[accession]),
     "text": _docs[accession], "document_url": "u"})
try:
    scan = server.scan_availability(
        [{"cik": "1", "accession": "0001-26-1", "issuer": "A",
          "document": "exfilingfees.htm"},
         {"cik": "1", "accession": "0001-26-2", "issuer": "B",
          "document": "exfilingfees.htm"}],
        ["coupon_barrier_pct", "buffer_pct", "cusip", "underlyings"])
finally:
    server.load_document = _real_load

check("the scan reads the PRIMARY document, never the hit's matched file",
      all(document is None for _, document in _asked))
by_acc = {r["accession"]: r for r in scan["rows"]}
check("a field the filing carries reads present",
      by_acc["0001-26-1"]["present"]["coupon_barrier_pct"] == "present")
check("a field that note type lacks reads absent, not missing",
      by_acc["0001-26-1"]["present"]["buffer_pct"] == "absent")
check("the other note type is the mirror image",
      by_acc["0001-26-2"]["present"]["buffer_pct"] == "present"
      and by_acc["0001-26-2"]["present"]["coupon_barrier_pct"] == "absent")
check("a field with no spec anchors reads untestable rather than absent",
      by_acc["0001-26-1"]["present"]["underlyings"] == "untestable")

summ = {s["field"]: s for s in scan["summary"]}
check("a field in every scanned filing is classed universal here",
      summ["cusip"]["class"] == "universal" and summ["cusip"]["rate"] == 1.0)
check("a field in half of them is classed product-specific",
      summ["buffer_pct"]["class"] == "product-specific"
      and summ["buffer_pct"]["present"] == 1)
check("untestable fields are excluded from the rate, not scored zero",
      summ["underlyings"]["rate"] is None and summ["underlyings"]["testable"] == 0)

# --------------------------------------------------------------------------- #
section("cues -- where to look before any model runs")
# --------------------------------------------------------------------------- #

CUE_TEXT = ("Key Terms\n"
            "Interest Barrier: 70.00% of the Initial Stock Price\n"
            "Contingent Interest Rate: 9.15% per annum, paid quarterly\n"
            "The estimated value is $979.00 per $1,000 principal amount note.\n")

cues = server.document_cues(CUE_TEXT, _spec)
anchors = [c for c in cues if c["kind"] == "anchor"]
units = [c for c in cues if c["kind"] == "unit"]

check("a spec anchor is found and knows which field it belongs to",
      any(c["cue"].startswith("Interest Barrier") and c["field"] == "coupon_barrier_pct"
          for c in anchors))
check("the number after an anchor is captured as the candidate value",
      any(c["value_guess"] == "70.00%" for c in anchors))
check("that candidate carries its own span, so it can be marked in one click",
      all(CUE_TEXT[c["value_span"][0]:c["value_span"][1]] == c["value_guess"]
          for c in anchors if c["value_span"]))
check("unit phrases are found where no anchor is labelled",
      any(c["cue"].lower() == "per annum" for c in units)
      and any(c["cue"].lower() == "per $1,000" for c in units))
check("unit cues are typed so the UI can group them",
      {c["cue_kind"] for c in units if c["cue"].lower() == "per annum"} == {"rate"})
check("a unit cue carries no field -- it says 'a number lives here', not which one",
      all(c["field"] is None for c in units))
check("cues come back in document order", [c["span"][0] for c in cues] ==
      sorted(c["span"][0] for c in cues))
check("restricting to one field drops the other fields' anchors",
      all(c["field"] in (None, "barrier_pct")
          for c in server.document_cues(CUE_TEXT, _spec, fields=["barrier_pct"])))

# The label a value sits under, shown next to it. `validation.derive_anchor`
# splits the preceding line on its FIRST colon, which is several labels too
# early when a whole key-terms table shares one line -- the shape these filings
# actually have.
ONE_LINE = ("Valuation Date::: April 6, 2026 Maturity Date::: April 9, 2026 "
            "CUSIP:: 48136CYQ6  and more")


def label_at(text, frag, field_name):
    at = text.index(frag)
    return server.label_before(text, (at, at + len(frag)), _spec.field(field_name))


check("the label ADJACENT to the value wins, not the first one on the line",
      label_at(ONE_LINE, "48136CYQ6", "cusip") == "CUSIP")
check("a second value on the same line gets its own label",
      label_at(ONE_LINE, "April 9, 2026", "maturity_date") == "Maturity Date")
check("a canonical spec anchor is preferred over raw preceding text",
      label_at("Key Terms Interest Barrier:: 70.00%", "70.00", "coupon_barrier_pct")
      == "Interest Barrier")
check("a term named AFTER the value is still found",
      label_at("equal to 70.00% of the Initial Stock Price, which we refer to as "
               "the Interest Barrier.", "70.00", "coupon_barrier_pct")
      == "Interest Barrier")
check("no label either side returns nothing rather than a sentence fragment",
      label_at("the notes will pay you 70.00 dollars at some point", "70.00",
               "barrier_pct") is None)

# --------------------------------------------------------------------------- #
section("query resolution -- what a run will actually extract")
# --------------------------------------------------------------------------- #


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


picked = [{"accession": "0001-25-1", "cik": "0000019617", "issuer": "JPM"},
          {"accession": "0001-25-2", "cik": "0000019617", "issuer": "JPM"}]
targets, prov = server.resolve_targets({"accessions": picked, "limit": 5})
check("an explicit selection is used verbatim -- never re-searched",
      targets == picked and prov["source"] == "selection")

targets, _ = server.resolve_targets({"accessions": picked, "limit": 1})
check("limit truncates the selection", len(targets) == 1)

check("a selection row without a CIK is refused (it cannot be fetched)",
      raises(lambda: server.resolve_targets({"accessions": [{"accession": "x"}]}), ValueError))
check("a bare accession string is refused with a message that says what to send",
      raises(lambda: server.resolve_targets({"accessions": ["0001-25-1"]}), ValueError))
check("an unknown saved query is refused",
      raises(lambda: server.resolve_targets({"query": "no-such-query"}), FileNotFoundError))

check("a search with no phrase, form, or CIK is refused before any request",
      raises(lambda: server.edgar_search({}), ValueError))

# #313: say when the search was cut off. EDGAR returns one page, newest first.
import edgar_client as _ec_t
_real_ec_t = _ec_t.EdgarClient


class _TruncClient:
    def __init__(self, total, n_hits, **kw):
        self.total, self.n = total, n_hits

    def ticker_to_cik(self, t):
        return "1"

    def full_text_search(self, **kw):
        hits = [{"_id": f"0000000000-26-{i:06d}:d{i}.htm",
                 "_source": {"adsh": f"0000000000-26-{i:06d}", "ciks": ["0000000001"],
                             "display_names": ["X Co (CIK 0000000001)"], "form": "4",
                             "root_forms": ["4"], "file_date": f"2026-10-{16 - (i % 5):02d}"}}
                for i in range(self.n)]
        return {"hits": {"total": {"value": self.total, "relation": "eq"}, "hits": hits}}


_saved_ua_t = os.environ.get("EDGAR_USER_AGENT")
os.environ["EDGAR_USER_AGENT"] = "test admin@example.com"
try:
    _ec_t.EdgarClient = lambda **kw: _TruncClient(9671, 100)
    _r_t = server.edgar_search({"forms": ["4"], "max_results": 100})
    check("a search matching more than it returned is truncated, with the oldest date shown",
          _r_t["truncated"] is True and _r_t["oldest_returned"] == "2026-10-12"
          and _r_t["total"] == 9671)
    check("...and `saturated` still means only EDGAR's 10,000 cap", _r_t["saturated"] is False)
    _ec_t.EdgarClient = lambda **kw: _TruncClient(9, 9)
    _r_f = server.edgar_search({"forms": ["4"], "max_results": 100})
    check("a fully returned search is not truncated", _r_f["truncated"] is False and _r_f["returned"] == 9)
    _ec_t.EdgarClient = lambda **kw: _TruncClient(30, 30)
    _r_m = server.edgar_search({"forms": ["4"], "max_results": 10})
    check("stopping at max_results with more hits waiting is truncated",
          _r_m["truncated"] is True and _r_m["returned"] == 10)
finally:
    _ec_t.EdgarClient = _real_ec_t
    if _saved_ua_t is None:
        os.environ.pop("EDGAR_USER_AGENT", None)
    else:
        os.environ["EDGAR_USER_AGENT"] = _saved_ua_t

# Issuer resolution reuses ticker_to_cik: a bare CIK passes through, a ticker is
# resolved before the query is built, and there is no second ticker->CIK map.
_fake_t2c = lambda t: {"AAPL": "0000320193", "CSCO": "0000858877"}[t]
check("a bare-CIK issuer token passes through untouched",
      server.resolve_issuer_ciks(["0000320193"], _fake_t2c) == ["0000320193"])
check("a ticker issuer token is resolved to a CIK via the injected ticker_to_cik",
      server.resolve_issuer_ciks(["AAPL"], _fake_t2c) == ["0000320193"])
check("mixed ticker + CIK tokens resolve in order, blanks dropped",
      server.resolve_issuer_ciks(["AAPL", "", "19617", "CSCO"], _fake_t2c)
      == ["0000320193", "19617", "0000858877"])
check("an unknown ticker raises (surfaced as a 4xx upstream)",
      raises(lambda: server.resolve_issuer_ciks(["NOPE"], _fake_t2c), KeyError))
check("limit is clamped to the documented ceiling",
      server.resolve_targets({"accessions": picked * 600, "limit": 99999})[1]["limit"] == 1000)

# --------------------------------------------------------------------------- #
section("daily sampling + EDGAR links")
# --------------------------------------------------------------------------- #

urls = server.edgar_urls("0000019617", "0001013762-25-000407", "ea0234769-01_424b2.htm")
check("document URL is the real Archives path (CIK unpadded, accession undashed)",
      urls["document_url"] ==
      "https://www.sec.gov/Archives/edgar/data/19617/000101376225000407/ea0234769-01_424b2.htm")
check("filing index URL points at the -index page",
      urls["filing_url"].endswith("/0001013762-25-000407-index.htm"))
check("a hit with no CIK yields no links rather than a broken one",
      server.edgar_urls(None, "0001-25-1") == {})

su = server.edgar_search_url('"contingent coupon"', ["424B2"], ["19617"],
                             "2025-01-01", "2025-12-31")
check("search URL targets EDGAR's own full-text UI",
      su.startswith("https://www.sec.gov/edgar/search/#/"))
check("search URL carries the phrase, forms, padded CIK and custom range",
      "q=%22contingent%20coupon%22" in su and "forms=424B2" in su
      and "ciks=0000019617" in su and "dateRange=custom" in su
      and "startdt=2025-01-01" in su)

calls = []


def fake_search(params):
    """Stand-in for the live efts call. Two filings on every weekday except
    Friday the 13th, which is empty -- so a 4-day walk back from Tuesday the
    17th has to step over both a weekend (14th-15th) and an empty day."""
    calls.append(params["startdt"])
    day = params["startdt"]
    if day == "2025-06-13":
        return {"hits": [], "total": 0, "saturated": False}
    return {"hits": [{"accession": f"acc-{day}-{i}", "cik": "0000019617",
                      "issuer": "JPM", "file_date": day} for i in range(2)],
            "total": 40, "saturated": False}


real_search = server.edgar_search
server.edgar_search = fake_search
try:
    daily = server.edgar_search_daily({"q": "x", "per_day": 2, "days": 4,
                                        "enddt": "2025-06-17"})
finally:
    server.edgar_search = real_search

check("covers the requested number of days that actually have filings",
      daily["days_covered"] == 4)
check("an empty day is requested but does not count toward the coverage",
      "2025-06-13" in calls and all(d["day"] != "2025-06-13" for d in daily["days"]))
check("weekends are never requested at all",
      "2025-06-14" not in calls and "2025-06-15" not in calls)
check("per_day caps each day's sample", all(d["sampled"] == 2 for d in daily["days"]))
check("series runs oldest -> newest", [d["day"] for d in daily["days"]] ==
      sorted(d["day"] for d in daily["days"]))
check("every hit is stamped with the day it was sampled from",
      all(h["sample_day"] for h in daily["hits"]))
check("empty days are reported, not hidden", daily["days_empty"] == 1)

# --------------------------------------------------------------------------- #
section("doc allowlist -- a path guard, not a path join")
# --------------------------------------------------------------------------- #

check("a repo doc under an allowed root renders",
      (server.serve_doc("tools/edgar_scrubber/README.md") or "").startswith("<!doctype html>"))
check("traversal above the repo is refused",
      server.serve_doc("../../../Windows/win.ini") is None)
check("an absolute path is refused",
      server.serve_doc("C:/Windows/win.ini") is None)
check("a repo file outside the allowlisted roots is refused",
      server.serve_doc("README.md") is None)
check("a path that does not exist is refused",
      server.serve_doc("tools/edgar_scrubber/NOPE.md") is None)

# --------------------------------------------------------------------------- #
section("markdown rendering")
# --------------------------------------------------------------------------- #

html = server.markdown_to_html(
    "# Title\n\nSome `code` and a [link](https://x.test).\n\n"
    "- one\n- two\n\n```\nraw <b>block</b>\n```\n\n"
    "| a | b |\n|---|---|\n| 1 | 2 |\n", "t.md")
check("heading renders", "<h1>Title</h1>" in html)
check("inline code renders", "<code>code</code>" in html)
check("link renders", '<a href="https://x.test">link</a>' in html)
check("list renders", "<li>one</li>" in html and "</ul>" in html)
check("fenced block is escaped, not injected", "raw &lt;b&gt;block&lt;/b&gt;" in html)
check("table header separator row is dropped", "<td>---</td>" not in html)
check("table renders", "<td>1</td>" in html)

evil = server.markdown_to_html("<script>alert(1)</script>", "x.md")
check("raw HTML in a doc is escaped", "<script>alert(1)</script>" not in evil
      and "&lt;script&gt;" in evil)

# --------------------------------------------------------------------------- #
section("store reader")
# --------------------------------------------------------------------------- #

tmp_home = Path(tempfile.mkdtemp(prefix="tool-server-test-"))
os.environ["EDGAR_SCRUBBER_HOME"] = str(tmp_home)

check("an absent store reports exists=False rather than raising",
      server.store_extractions()["exists"] is False and server.store_runs()["exists"] is False)

from output_store import OutputStore, FieldValue

store = OutputStore(tmp_home / "store" / "extractions.sqlite")
run_id = store.start_run("424b2.structured_note", "1", "2026-08-13T00:00:00Z", note="test")
store.record(run_id, "0001-25-1", "doc.htm", FieldValue(
    field="cusip", value="48136CYQ6", unit=None, span=(10, 19),
    provenance="local:qwen2.5:7b", confidence=1.0,
    flags=[{"code": "gated_no_claude", "severity": "warn"}]))
store.record(run_id, "0001-25-1", "doc.htm", FieldValue(
    field="barrier_pct", value=70.0, unit="percent_of_initial", span=(1, 5),
    provenance="local:qwen2.5:7b", confidence=0.9, flags=[], candidate_count=4))
store.close()

data = server.store_extractions()
check("the store is found once it exists", data["exists"] is True)
check("rows group into one document", len(data["documents"]) == 1)
doc = data["documents"][0]
check("document carries its run and accession",
      doc["run_id"] == run_id and doc["accession"] == "0001-25-1")
by_name = {f["field"]: f for f in doc["fields"]}
check("a string value round-trips out of value_json", by_name["cusip"]["value"] == "48136CYQ6")
check("a numeric value round-trips", by_name["barrier_pct"]["value"] == 70.0)
check("units survive", by_name["barrier_pct"]["unit"] == "percent_of_initial")
check("flags are flattened to their codes",
      by_name["cusip"]["flags"] == ["gated_no_claude"] and by_name["barrier_pct"]["flags"] == [])
check("filtering by run_id keeps the run",
      len(server.store_extractions(run_id)["documents"]) == 1)
check("filtering by an unknown run_id returns nothing",
      len(server.store_extractions("no-such-run")["documents"]) == 0)
check("store_runs lists the run", any(r["run_id"] == run_id for r in server.store_runs()["runs"]))

# --------------------------------------------------------------------------- #
section("candidate sets are scratch (#179) -- the count is surfaced, never the list")
# --------------------------------------------------------------------------- #
check("the API surfaces candidate_count for a value chosen from candidates",
      by_name["barrier_pct"]["candidate_count"] == 4)
check("candidate_count is 0 on a free-form field", by_name["cusip"]["candidate_count"] == 0)
check("no API field carries a candidate LIST",
      all(not any(k != "candidate_count" and "candidate" in k for k in f)
          and not isinstance(f.get("candidate_count"), (list, dict))
          for d in data["documents"] for f in d["fields"]))

# --------------------------------------------------------------------------- #
section("coverage -- what can we grab consistently, per issuer")
# --------------------------------------------------------------------------- #

from output_store import DocumentExtraction

cov = OutputStore(tmp_home / "store" / "extractions.sqlite")
cov_run = cov.start_run("424b2.structured_note", "1", "2026-08-14T00:00:00Z")


def doc(issuer, accession, barrier_span, cusip_value):
    """One filing where barrier_pct may or may not be locatable and cusip may
    or may not have been read at all."""
    return DocumentExtraction(
        accession=accession, document="d.htm", issuer=issuer,
        filing_date="2026-08-01",
        fields=[
            FieldValue(field="barrier_pct", value=70.0, unit="percent_of_initial",
                       span=barrier_span, provenance="local",
                       flags=([] if barrier_span else
                              [{"code": "span_unlocatable", "severity": "warn"}])),
            FieldValue(field="cusip", value=cusip_value, span=(5, 14) if cusip_value else None,
                       provenance="local",
                       flags=[{"code": "gated_no_claude", "severity": "warn"}]),
        ])


cov.write_document(cov_run, doc("ISSUER A", "0001-26-1", (1, 5), "48136CYQ6"))
cov.write_document(cov_run, doc("ISSUER A", "0001-26-2", (2, 6), "48136CYQ7"))
cov.write_document(cov_run, doc("ISSUER B", "0002-26-1", None, None))
cov.close()

cover = server.store_coverage()
by = {(r["issuer"], r["field"]): r for r in cover["rows"]}

check("issuers and fields are both enumerated",
      cover["issuers"] == ["ISSUER A", "ISSUER B"] and "barrier_pct" in cover["fields"])
check("a field located in every filing reads 100%",
      by[("ISSUER A", "barrier_pct")]["located_rate"] == 1.0)
check("the same field for another issuer reads 0% -- issuer scope is the point",
      by[("ISSUER B", "barrier_pct")]["located_rate"] == 0.0)
check("an unlocatable value is counted as such, not as a miss of the value",
      by[("ISSUER B", "barrier_pct")]["values_got"] == 1
      and by[("ISSUER B", "barrier_pct")]["unlocatable"] == 1)
check("a null value is not counted as a value",
      by[("ISSUER B", "cusip")]["values_got"] == 0)
check("gated fields are surfaced separately from located ones",
      by[("ISSUER A", "cusip")]["gated"] == 2)
check("documents are counted distinctly from extractions",
      by[("ISSUER A", "barrier_pct")]["documents"] == 2)
check("min_documents filters out issuers with too little evidence",
      all(r["documents"] >= 2 for r in server.store_coverage(2)["rows"]))
check("human-confirmed examples are counted per issuer+field",
      by[("ISSUER A", "cusip")]["taught"] == 0)

# A field the note type simply does not carry (no coupon barrier on a growth
# note) must not be scored as a field we failed to read -- otherwise the metric
# rewards inventing a value.
vs = server.validation_store()
vs.ensure_session("dashboard", spec_id="424b2.structured_note", now="2026-08-14T00:00:00Z")
vs.write_exemplar("ISSUER B", "barrier_pct", "negative",
                  "barrier_pct: absent in a prior filing", form="424b2.structured_note",
                  now="2026-08-14T00:00:00Z")
vs.close()

after = {(r["issuer"], r["field"]): r for r in server.store_coverage()["rows"]}
check("a filing ruled absent leaves the denominator",
      after[("ISSUER B", "barrier_pct")]["applicable"] == 0
      and after[("ISSUER B", "barrier_pct")]["absent_marked"] == 1)
check("a field absent in every filing reads n/a, not 0%",
      after[("ISSUER B", "barrier_pct")]["located_rate"] is None)
check("another issuer's score is untouched by that ruling",
      after[("ISSUER A", "barrier_pct")]["located_rate"] == 1.0)

absent_res = server.save_annotation({
    "accession": "0002-26-1", "document": "d.htm", "issuer": "ISSUER B",
    "field": "coupon_barrier_pct", "absent": True})
check("marking a field absent needs no span", absent_res["saved"] is True)
check("absence is filed as a reject verdict", absent_res["verdict"] == "reject")
check("absence teaches that null is valid rather than teaching a value",
      "null is a valid answer" in absent_res["rendered"])

# --------------------------------------------------------------------------- #
section("extractions grid -- whose filing each value came out of")
# --------------------------------------------------------------------------- #

# The grid is grouped by issuer in the browser, which is only possible if the
# row carries one. `extractions` does not store the issuer -- `documents` does
# -- so this is a join that can silently regress into "every card says the same
# thing" without any error.
grid = server.store_extractions()
by_acc = {d["accession"]: d for d in grid["documents"]}

check("a row written through the low-level `record` path reports no issuer "
      "rather than borrowing another filing's",
      by_acc["0001-25-1"]["issuer"] is None)
check("the issuer is the one that filed it, not the first run's",
      by_acc["0001-26-1"]["issuer"] == "ISSUER A"
      and by_acc["0002-26-1"]["issuer"] == "ISSUER B")
check("the filing's own date rides along, not just the run's timestamp",
      by_acc["0001-26-1"]["filing_date"] == "2026-08-01")
check("issuers are enumerated for the filter chips",
      grid["issuers"] == ["ISSUER A", "ISSUER B"])
check("the join does not duplicate a document's field rows",
      len(by_acc["0001-26-1"]["fields"]) == 2)
check("values still come through beside the issuer",
      {f["field"] for f in by_acc["0001-26-1"]["fields"]} == {"barrier_pct", "cusip"})

# --------------------------------------------------------------------------- #
section("claude template -- a value it cannot point at is not a finding")
# --------------------------------------------------------------------------- #

# No network and no SDK: what is checked here is the part that decides whether
# a returned value is believed. The API call itself is one function away
# (`request_template`), deliberately, so this gate covers the checking without
# needing a key.
import claude_template as ctpl

_FILING = (
    "Key Terms\n\n"
    "Barrier Amount: 60.00% of the Initial Value, which is 6,579.942\n"
    "CUSIP: 46660RRA4\n"
    "Initial Value: The closing level of the Index on the Pricing Date, "
    "which was 10,966.57\n"
    "Payment at Maturity: $1,000 + ($1,000 x Index Return)\n"
)

_raw = {
    "product_type": "review note",
    "fields": [
        {"name": "barrier_pct", "label": "Barrier Amount", "value": "60.00%",
         "unit": "percent_of_initial", "type": "percent", "section": "Key Terms",
         "quote": "Barrier Amount: 60.00% of the Initial Value, which is 6,579.942",
         "spec_field": "barrier_pct"},
        # Same quote text, but with the whitespace a table row loses. A copy is
        # still a copy; only a paraphrase is a miss.
        {"name": "cusip", "label": "CUSIP", "value": "46660RRA4",
         "unit": "cusip", "type": "string", "section": "Key Terms",
         "quote": "CUSIP:   46660RRA4", "spec_field": "cusip"},
        # Not in the filing at all -- the failure mode this whole check exists
        # for. A plausible number with a quote nobody wrote.
        {"name": "coupon_barrier_pct", "label": "Coupon Barrier", "value": "70.00%",
         "unit": "percent_of_initial", "type": "percent", "section": "Key Terms",
         "quote": "Coupon Barrier: 70.00% of the Initial Value", "spec_field": ""},
    ],
    "absent_spec_fields": ["contingent_coupon_rate"],
    "notes": "",
}

_located = ctpl.locate(_raw, _FILING)
_by = {f["name"]: f for f in _located["fields"]}

check("an invented value is dropped, not returned as a field",
      "coupon_barrier_pct" not in _by
      and _located["unlocatable"][0]["name"] == "coupon_barrier_pct")
check("a quote that IS in the filing resolves to its offsets",
      _FILING[_by["barrier_pct"]["span"][0]:_by["barrier_pct"]["span"][1]]
      == _raw["fields"][0]["quote"])
check("whitespace differences do not make a real quote a miss",
      "cusip" in _by and _FILING[_by["cusip"]["span"][0]:_by["cusip"]["span"][1]]
      == "CUSIP: 46660RRA4")
# "$1,000" appears twice on one line and "60.00%" once; the value span has to
# land inside the quoted occurrence or a highlight points at the wrong number.
check("the value's own span lands inside its quote",
      _FILING[_by["barrier_pct"]["value_span"][0]:
              _by["barrier_pct"]["value_span"][1]] == "60.00%"
      and _by["barrier_pct"]["span"][0] <= _by["barrier_pct"]["value_span"][0])
check("the answer key is not handed to the model",
      "barrier_pct" in ctpl.build_prompt(_FILING, ["barrier_pct"])
      and "fill in" not in ctpl.build_prompt(_FILING, ["barrier_pct"]).lower())
check("a filing longer than the cap is truncated, and says so",
      "[The document was truncated at 100 characters.]"
      in ctpl.build_prompt("x" * 500, [], max_chars=100))

_draft = ctpl.as_field_spec(_located)
check("the draft spec carries only located fields",
      [f["name"] for f in _draft["fields"]] == ["barrier_pct", "cusip"])
check("the draft spec anchors on the filing's own label",
      "Barrier Amount" in _draft["fields"][0]["anchors"])
check("the draft spec is labelled a draft, not the spec",
      _draft["spec_id"].endswith("claude_draft"))

# --------------------------------------------------------------------------- #
section("health")
# --------------------------------------------------------------------------- #

h = server.health()
check("health reports the fields the banner reads",
      {"ollama", "edgar_user_agent", "store", "queries"} <= set(h))
check("health never raises when ollama is unreachable -- it reports detail",
      isinstance(h["ollama"]["ok"], bool))
check("saved queries are discovered", "424b2-jpm-2025-pilot" in h["queries"])

# --------------------------------------------------------------------------- #
section("free research")
# --------------------------------------------------------------------------- #
import threading
import urllib.error
import urllib.request
from functools import partial as _partial
from http.server import ThreadingHTTPServer as _THS

_fr_home = tempfile.mkdtemp(prefix="fr-gate-")
os.environ["FREE_RESEARCH_HOME"] = _fr_home

# Templates come off disk and must all validate.
_tpls = server.fr_templates()
check("free research lists its templates",
      "filing_brief" in [t["id"] for t in _tpls])
check("a listed template carries its slots and their columns",
      all(s.get("columns") for t in _tpls for s in t["slots"]))

# A brief needs a pull that exists. This is refused BEFORE a run is made.
_runs_before = len(server.RUNS.list())
try:
    server.start_fr_brief({"pull_id": "pull_does_not_exist"})
    _refused = False
except LookupError:
    _refused = True
check("a brief naming an unknown pull is refused", _refused)
check("...and no run was created for it",
      len(server.RUNS.list()) == _runs_before)

try:
    server.start_fr_pull({})
    _no_target = False
except ValueError:
    _no_target = True
check("a pull naming neither CIK nor ticker is refused", _no_target)

# A brief view assembles its chart points from the STORED rows, not the model.
_store = server.fr_store()
_pull = _store.create_pull("edgar", {"forms": ["8-K"]})
_store.write_rows(_pull["id"], [
    {"month": "2025-07", "count": 2, "form": "8-K"},
    {"month": "2025-08", "count": 5, "form": "8-K"},
    {"month": "2025-09", "count": None, "form": "8-K"},
])
_brief = _store.create_brief(
    _pull["id"], "filing_brief",
    {"by_month": {"mark": "bar", "x": "month", "y": "count"}},
    "qwen2.5:7b", dropped=[{"slot": "summary", "reason": "ungrounded number"}])
_view = server.fr_brief_view(_brief["id"])
check("the brief view reads points out of the stored rows",
      _view["charts"]["by_month"]["points"] == [["2025-07", 2.0], ["2025-08", 5.0]])
check("an unplottable row is counted, not silently dropped",
      _view["charts"]["by_month"]["skipped"] == 1)
check("the view carries the pull the brief was built from",
      _view["pull"]["id"] == _pull["id"])
check("dropped slots survive into the view",
      _view["brief"]["dropped"][0]["slot"] == "summary")

# Routes, over a real loopback server -- stdlib only, no outside network.
_httpd = _THS(("127.0.0.1", 0), _partial(server.Handler, directory=str(server.REPO_ROOT)))
threading.Thread(target=_httpd.serve_forever, daemon=True).start()
_base = "http://127.0.0.1:%d" % _httpd.server_address[1]


def _get(path):
    try:
        with urllib.request.urlopen(_base + path, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _post(path, payload):
    req = urllib.request.Request(
        _base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


_st, _body = _get("/api/tools/free-research/templates")
check("GET templates serves the validated templates",
      _st == 200 and "filing_brief" in [t["id"] for t in _body])

_st, _body = _get("/api/tools/free-research/briefs")
check("GET briefs lists stored briefs",
      _st == 200 and _brief["id"] in [b["id"] for b in _body])

_st, _body = _get("/api/tools/free-research/brief?id=%s" % _brief["id"])
check("GET brief returns one by id", _st == 200 and _body["brief"]["id"] == _brief["id"])

_st, _body = _get("/api/tools/free-research/brief?id=nope")
check("GET brief for an unknown id is a 4xx, not an empty 200", _st == 404)

_st, _body = _get("/api/tools/free-research/brief")
check("GET brief with no id is a 400", _st == 400)

_st, _body = _post("/api/tools/free-research/brief", {"pull_id": "pull_nope"})
check("POST brief naming an unknown pull is a 404 naming it",
      _st == 404 and "pull_nope" in _body.get("error", ""))

_st, _body = _post("/api/tools/free-research/pull", {})
check("POST pull with no target is a 400", _st == 400)

_st, _body = _get("/api/tools")
check("the manifest marks free-research runnable when it is listed",
      _st == 200 and all(t.get("runnable") is not None for t in _body))

_httpd.shutdown()

# The routes write only the Free Research store, never shared pipeline data.
_written = {os.path.join(dp, n) for dp, _, ns in os.walk(_fr_home) for n in ns}
check("free research wrote only under its own store root",
      bool(_written) and all(_fr_home in w for w in _written))

# --------------------------------------------------------------------------- #
section("accuracy switches -- off by default, one command to turn on")
# --------------------------------------------------------------------------- #
# Both cost model time, so both default off. self-consistency has been inert on
# every run so far: the gate records "single sample, not checked" at N=1, which
# is the default the ladder ships.

for _var in ("SCRUBBER_SELF_CONSISTENCY", "SCRUBBER_SHADOW_EXTERNAL"):
    os.environ.pop(_var, None)

check("both switches are off with no params and no env",
      server.accuracy_switches({}) == (1, False, False))
check("self-consistency comes from params",
      server.accuracy_switches({"self_consistency": 2}) == (2, False, False))
check("shadow comes from params",
      server.accuracy_switches({"shadow_external": True}) == (1, True, False))

os.environ["SCRUBBER_SELF_CONSISTENCY"] = "3"
os.environ["SCRUBBER_SHADOW_EXTERNAL"] = "1"
check("the environment supplies defaults, so --self-consistency reaches a run",
      server.accuracy_switches({}) == (3, True, False))
check("an explicit param still overrides the environment",
      server.accuracy_switches({"self_consistency": 1}) == (1, True, False))
check("...in both directions",
      server.accuracy_switches({"shadow_external": False}) == (3, False, False))
os.environ["SCRUBBER_SELF_CONSISTENCY"] = "not a number"
check("a junk env value falls back to off rather than raising",
      server.accuracy_switches({}) == (1, True, False))
os.environ["SCRUBBER_SELF_CONSISTENCY"] = "99"
check("sampling is capped -- 99 passes per field is not a setting anyone wants",
      server.accuracy_switches({})[0] == 5)
os.environ["SCRUBBER_SELF_CONSISTENCY"] = "0"
check("and floored at 1, since zero samples is no extraction at all",
      server.accuracy_switches({})[0] == 1)
for _var in ("SCRUBBER_SELF_CONSISTENCY", "SCRUBBER_SHADOW_EXTERNAL"):
    os.environ.pop(_var, None)

check("candidate-select is off with no params and no env",
      server.accuracy_switches({})[2] is False)
check("candidate-select comes from params",
      server.accuracy_switches({"candidate_select": True})[2] is True)
os.environ["SCRUBBER_CANDIDATE_SELECT"] = "1"
check("candidate-select comes from the environment too",
      server.accuracy_switches({})[2] is True)
check("an explicit param still overrides it",
      server.accuracy_switches({"candidate_select": False})[2] is False)
os.environ.pop("SCRUBBER_CANDIDATE_SELECT", None)

# The switches have to actually reach the ladder, not just be parsed.
import inspect as _inspect
_sig = _inspect.signature(server.scrubber_run)
check("scrubber_run accepts both switches",
      {"self_consistency", "shadow_external"} <= set(_sig.parameters))
check("and defaults them off",
      _sig.parameters["self_consistency"].default == 1
      and _sig.parameters["shadow_external"].default is False)
_src = _inspect.getsource(server.scrubber_run)
check("it passes self-consistency to the ladder",
      "self_consistency_samples=samples" in _src)
check("it passes the shadow flag to the ladder",
      "shadow_external=bool(shadow_external)" in _src)
check("and records which setting a run used, so a comparison is attributable",
      "self-consistency:" in _src)
check("candidate-select reaches the ladder too",
      "candidate_select=bool(candidate_select)" in _src)
check("scrubber_run accepts it, defaulted off",
      "candidate_select" in _sig.parameters
      and _sig.parameters["candidate_select"].default is False)

# --------------------------------------------------------------------------- #
section("issuer fallback -- a run from a selection still knows whose filing it is")
# --------------------------------------------------------------------------- #
# Found auditing the local store for #165: 75 of 201 documents had a NULL
# issuer, and they were exactly runs #0016, #0017 and #0018 -- 25 each. Run
# #0015 covered the SAME 25 filings and recorded the issuer fine.
#
# The model was not at fault. It read "Citigroup Global Markets Holdings Inc."
# on 25 of 25 filings in #0018. `documents.issuer` is written from EDGAR search
# metadata, and `resolve_targets` passes an explicit {accession, cik} selection
# straight through, so a run started that way has no display name to write.
#
# It matters because exemplars, rules and coverage are all keyed by issuer: a
# NULL detaches everything the run produced from the issuer it belongs to, and
# nothing complains.

check("an explicit selection carries no issuer metadata, which is the bug",
      server.resolve_targets(
          {"accessions": [{"accession": "0000950103-26-013335", "cik": "831001"}]}
      )[0][0].get("issuer") is None)

check("a selection is still accepted -- the fix is a fallback, not a refusal",
      server.resolve_targets(
          {"accessions": [{"accession": "0000950103-26-013335", "cik": "831001"}]}
      )[1]["source"] == "selection")

# The fallback itself, as the run applies it.
_meta_with = {"issuer": "CITIGROUP INC"}
_meta_without = {}
check("EDGAR's display name wins when the run came from a search",
      (_meta_with.get("issuer") or "model reading") == "CITIGROUP INC")
check("the model's reading is used when metadata has none",
      (_meta_without.get("issuer") or "Citigroup Global Markets Holdings Inc.")
      == "Citigroup Global Markets Holdings Inc.")
check("and a document with neither stays None rather than inventing one",
      (_meta_without.get("issuer") or None) is None)

# --------------------------------------------------------------------------- #
section("span gate -- a figure nothing points at is not stored (#163)")
# --------------------------------------------------------------------------- #

_EV = _spec.field("estimated_value_per_1000")      # number
_BUF = _spec.field("buffer_pct")                   # percent
_MAT = _spec.field("maturity_date")                # date
_ISSUER = _spec.field("issuer")                    # string
_PROD = _spec.field("product_type")                # enum
_UND = _spec.field("underlyings")                  # array

_SPAN = (10, 15)

# With a span, nothing changes -- the gate only fires on absence.
check("a spanned figure is stored untouched",
      server.withhold_unsupported_value(_EV, 983.0, _SPAN, []) == (983.0, None))

# Without one, the figure is withheld across every value-typed field.
for _f, _v, _label in ((_EV, 950, "number"), (_BUF, 9.75, "percent"),
                       (_MAT, "2028-08-31", "date")):
    _val, _flag = server.withhold_unsupported_value(_f, _v, None, [])
    check("an unsupported %s is not stored as a value" % _label, _val is None)
    check("...and the %s withholding is recorded" % _label,
          _flag is not None and _flag["code"] == "value_withheld_no_span")

# The withheld value itself is named, so the run is still auditable.
_v, _flag = server.withhold_unsupported_value(_EV, 950, None, [])
check("the withheld figure is named in the reason", "950" in _flag["message"])

# String and enum fields are untouched: they score well and locate reliably.
check("a string field is unaffected",
      server.withhold_unsupported_value(_ISSUER, "Citigroup", None, [])
      == ("Citigroup", None))
check("an enum field is unaffected",
      server.withhold_unsupported_value(_PROD, "autocallable", None, [])
      == ("autocallable", None))
check("an array field is left to its own per-element check (#167)",
      server.withhold_unsupported_value(_UND, ["Zoetis Inc."], None, [])
      == (["Zoetis Inc."], None))

# The two failure modes stay distinct -- they are different facts.
_absent_val, _absent_flag = server.withhold_unsupported_value(
    _BUF, 12.0, None, ["field_absent_from_document"])
check("a field the filing does not carry is withheld too", _absent_val is None)
check("...but is NOT relabelled as a model failure", _absent_flag is None)
_model_val, _model_flag = server.withhold_unsupported_value(_BUF, 12.0, None, [])
check("whereas an unlocatable model value says so explicitly",
      _model_flag["code"] == "value_withheld_no_span")

# An empty value was never a claim, so there is nothing to withhold.
check("an absent value is left alone, not re-flagged",
      server.withhold_unsupported_value(_EV, None, None, []) == (None, None))
check("an empty string likewise",
      server.withhold_unsupported_value(_EV, "", None, []) == ("", None))

# Regression, run #0019: a value the spec's own bounds reject was stored anyway,
# because it had a span. estimated_value_per_1000 came back 0 on 20 of 25
# filings, and a zero digit locates somewhere in every filing, so the span gate
# passed it and only out_of_bounds objected.
_v, _f = server.withhold_unsupported_value(_EV, 0, _SPAN, ["out_of_bounds"])
check("a spanned figure the bounds reject is still withheld", _v is None)
check("...and says so as its own code",
      _f is not None and _f["code"] == "value_withheld_out_of_bounds")
check("the reason names the value", "0" in _f["message"])
check("an in-bounds spanned value is untouched by that rule",
      server.withhold_unsupported_value(_EV, 983.0, _SPAN, []) == (983.0, None))
check("out-of-bounds outranks having no span, and reports the bounds reason",
      server.withhold_unsupported_value(_EV, 0, None, ["out_of_bounds"])[1]["code"]
      == "value_withheld_out_of_bounds")
check("a string field is still exempt from the bounds rule",
      server.withhold_unsupported_value(_ISSUER, "Citigroup", _SPAN,
                                        ["out_of_bounds"]) == ("Citigroup", None))

# The types the gate covers, stated once so a spec change is visible here.
check("the gate covers number, percent and date",
      server.VALUE_TYPED == {"number", "percent", "date"})
check("22 of the 33 spec fields are value-typed",
      sum(1 for f in _spec.fields if f.type in server.VALUE_TYPED) == 22)

# --------------------------------------------------------------------------- #
section("array fields -- a span per member, and the kind is not a member (#167)")
# --------------------------------------------------------------------------- #

UND_DOC = ("The notes are linked to the common stock of GE Vernova Inc. "
           "Additional notes reference Zoetis Inc., the Nasdaq-100 Index and "
           "the Russell 2000 Index.")

# 1. A classification token is never an array member.
_names, _kinds = server.classify_array_members(["GE Vernova Inc.", "single_stock"])
check("a classification token is split out of the list",
      _names == ["GE Vernova Inc."] and _kinds == ["single_stock"])
check("'common stock' is a kind, not an underlying",
      server.classify_array_members(
          ["Advanced Micro Devices, Inc.", "common stock"])[0]
      == ["Advanced Micro Devices, Inc."])
check("a real index name survives, because matching is whole-element",
      server.classify_array_members(["Nasdaq-100 Index", "Russell 2000"])
      == (["Nasdaq-100 Index", "Russell 2000"], []))
check("case and a trailing point do not hide a kind",
      server.classify_array_members(["Zoetis Inc.", "Index."])[1] == ["Index."])

# 2. Spans resolve per element, not for the serialized array.
_kept, _dropped, _spans = server.locate_array_elements(
    UND_DOC, ["GE Vernova Inc.", "Zoetis Inc.", "Fabricated Corp."])
check("each stated member gets its own span",
      _kept == ["GE Vernova Inc.", "Zoetis Inc."] and len(_spans) == 2)
check("every returned span really contains its member",
      all(UND_DOC[s["span"][0]:s["span"][1]].strip().lower().startswith(n.lower()[:6])
          for n, s in _spans.items()))

# 3. An element the filing does not state is dropped individually, and the
#    surviving list is what would be stored.
check("an unstated member is dropped on its own", _dropped == ["Fabricated Corp."])
check("the survivors are the other two, not the whole list discarded",
      len(_kept) == 2)

# 4. A multi-underlying basket keeps every genuine member.
_basket, _bdrop, _ = server.locate_array_elements(
    UND_DOC, ["Nasdaq-100 Index", "Russell 2000 Index"])
check("a basket keeps every genuine member",
      _basket == ["Nasdaq-100 Index", "Russell 2000 Index"] and _bdrop == [])

# 5. The whole-array path was the bug: a serialized array is never a verbatim
#    substring of a filing, so it could never locate.
check("locate_value still refuses a list outright, which is why per-element exists",
      server.locate_value(UND_DOC, ["GE Vernova Inc."]) == (None, None))

# 6. The spec now carries the kind as its own field.
_und_spec = _spec.field("underlyings")
_kind_spec = _spec.field("underlying_type")
check("underlyings is declared as names", _und_spec.item_type == "string")
check("the kind has its own field", _kind_spec is not None)
check("...and it is the enum the array used to carry",
      set(_kind_spec.enum) == {"index", "etf", "single_stock", "basket", "worst_of"})

# --------------------------------------------------------------------------- #
section("taxonomy-sourced forms take the XBRL path, not the prose ladder")
# --------------------------------------------------------------------------- #
# The registry marks each form's field_source: 424B2 is `spec` (prose terms need
# a hand-authored field spec), 10-K/10-Q/8-K are `taxonomy` (every number carries
# an SEC tag, so the filing states its own fields).
#
# Nothing read field_source before this. Every extraction path loaded the 424B2
# spec from a hardcoded path, so choosing 10-Q would have fetched quarterly
# reports and then hunted for barrier_pct in them. That is why no run of any form
# but 424B2 exists in the store: the path was absent, not untested.

check("424B2 resolves to the spec path",
      server.scrubber_for_forms(["424B2"]).field_source == "spec")
for _f in ("10-K", "10-Q", "8-K"):
    _s = server.scrubber_for_forms([_f])
    check("%s resolves to the taxonomy path" % _f,
          _s is not None and _s.field_source == "taxonomy")
check("an unknown form falls back to the spec path rather than raising",
      server.scrubber_for_forms(["NOSUCH"]) is None)
check("no forms at all also falls back", server.scrubber_for_forms([]) is None)

# Drive the whole taxonomy path offline: the committed Apple companyfacts
# fixture through a fake client, into a temp FactsStore.
import taxonomy_fields as _txf
from facts_store import FactsStore as _FactsStore

_FIX = (server.REPO_ROOT / "tools" / "edgar_scrubber" / "fixtures"
        / "companyfacts_320193.json")
_facts = json.loads(_FIX.read_text(encoding="utf-8"))
_ACCN, _FORM = "0000320193-23-000106", "10-K"


class _FakeFactsClient:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def company_facts(self, cik):
        self.calls += 1
        return self.payload


_tax_home = Path(tempfile.mkdtemp(prefix="taxrun-gate-"))
_prev_home = os.environ.get("EDGAR_SCRUBBER_HOME")
_prev_ua = os.environ.get("EDGAR_USER_AGENT")
os.environ["EDGAR_SCRUBBER_HOME"] = str(_tax_home)
os.environ["EDGAR_USER_AGENT"] = "Test Tester test@example.com"

import edgar_client as _ec
_real_client = _ec.EdgarClient
_fake = _FakeFactsClient(_facts)
_ec.EdgarClient = lambda **kw: _fake
try:
    _rid = server.RUNS.create("edgar-scrubber", {"limit": 2, "fields": []})
    # The same filing twice, to prove the store's key is idempotent.
    server.taxonomy_run(_rid, [
        {"cik": "320193", "accession": _ACCN, "form": _FORM},
        {"cik": "320193", "accession": _ACCN, "form": _FORM},
    ], _tax_home)
finally:
    _ec.EdgarClient = _real_client

_tax_run = server.RUNS.get(_rid)
check("a taxonomy run completes without a model", _tax_run["status"] == "done")
check("companyfacts is fetched once per filer, not once per filing",
      _fake.calls == 1)
check("facts were written", (_tax_run.get("facts_written") or 0) > 0)

_fs_store = _FactsStore(path=str(_tax_home / "store" / "facts.sqlite"))
try:
    _rows = _fs_store.facts_for("320193")

    def _g(r, k):
        return r[k] if isinstance(r, dict) else getattr(r, k)

    check("rows read back from the period-keyed store", len(_rows) > 0)
    check("writing the same filing twice leaves one row per key",
          len(_rows) * 2 == (_tax_run.get("facts_written") or 0))
    check("every fact is marked source=xbrl, never 'extracted'",
          all(_g(r, "source") == "xbrl" for r in _rows))
    check("every fact carries a period at both ends",
          all(_g(r, "period_start") and _g(r, "period_end") for r in _rows))
    check("an instant fact has period_start == period_end, not a guessed start",
          any(_g(r, "period_start") == _g(r, "period_end") for r in _rows))
    check("a duration fact spans two different dates",
          any(_g(r, "period_start") != _g(r, "period_end") for r in _rows))
    check("concepts are taxonomy-qualified",
          all(":" in str(_g(r, "concept")) for r in _rows))
    # #236: a backtest must only see a number after it was public, so every
    # written fact carries the companyfacts entry's `filed` date.
    check("every written fact carries a non-empty ISO filed date",
          all(isinstance(_g(r, "filed"), str) and _g(r, "filed") for r in _rows))

    # The Results tab reads this. Before it existed a taxonomy run completed and
    # then showed nothing anywhere in the dashboard.
    _grid = server.store_facts()
    check("the facts view finds the filer the run wrote",
          [f["cik"] for f in _grid["filers"]] == ["320193"])
    check("annual columns are fiscal period ends, not cover-page dates",
          "2023-09-30" in _grid["periods"])
    _by = {(r["concept"], r["unit"]): r for r in _grid["rows"]}
    _assets = _by.get(("us-gaap:Assets", "USD"))
    check("an instant fact lands in its period-end column",
          _assets is not None and _assets["values"].get("2023-09-30") == 352583000000)
    _ni = _by.get(("us-gaap:NetIncomeLoss", "USD"))
    check("an annual duration lands in its period-end column",
          _ni is not None and _ni["values"].get("2023-09-30") == 96995000000)
    check("headline statement lines sort first",
          _grid["rows"][0]["concept"] in server.HEADLINE_CONCEPTS)
    check("every column is an end some duration defines",
          all(any(r["kind"] == "duration" and p in r["values"] for r in _grid["rows"])
              for p in _grid["periods"]))
    # The Results tab is per form: the 10-K view must not show another form's
    # filings, and a form with nothing stored is empty rather than a fallback.
    _k = server.store_facts(form=_FORM.lower())
    check("a form filter keeps that form's filings",
          _k["form"] == _FORM.upper() and len(_k["rows"]) == len(_grid["rows"]))
    _other = "8-K" if _FORM.upper() != "8-K" else "10-Q"
    _none = server.store_facts(form=_other)
    check("a form with no stored filings shows nothing, not another form's data",
          _none["filers"] == [] and _none["rows"] == [])
    try:
        server.store_facts(period="weekly")
        check("an unknown period is refused", False)
    except ValueError:
        check("an unknown period is refused", True)
finally:
    _fs_store.close()
    if _prev_home is None:
        os.environ.pop("EDGAR_SCRUBBER_HOME", None)
    else:
        os.environ["EDGAR_SCRUBBER_HOME"] = _prev_home
    if _prev_ua is None:
        os.environ.pop("EDGAR_USER_AGENT", None)
    else:
        os.environ["EDGAR_USER_AGENT"] = _prev_ua

# --------------------------------------------------------------------------- #
section("calculation linkbase: store roll-ups on ingest, flag non-adding values (#262)")
# --------------------------------------------------------------------------- #
# Drive the whole wiring offline against the #261 fixtures: a fake client that
# serves the Apple companyfacts (so facts are written) plus the hand-written
# Walmart-shaped cal fixtures (index.json + *_cal.xml).
from calc_linkbase import parse_cal as _parse_cal
from facts_store import FactRecord as _FactRecord

_CAL_FIX = (server.REPO_ROOT / "tools" / "edgar_scrubber" / "fixtures" / "cal")
_cal_index = json.loads((_CAL_FIX / "index.json").read_text(encoding="utf-8"))
_cal_xml = (_CAL_FIX / "wmt-20250131_cal.xml").read_text(encoding="utf-8")
_cal_arcs = _parse_cal(_cal_xml)


class _FakeCalClient(_FakeFactsClient):
    """Adds the two methods find_cal_url / the ingest path need on top of the
    companyfacts fake: a filing index and the cal XML text, no network."""

    def __init__(self, payload, index, cal_xml):
        super().__init__(payload)
        self._index, self._cal_xml = index, cal_xml

    def filing_index(self, cik, accession):
        return self._index

    def get_text(self, url):
        return self._cal_xml


# -- AC1: a run over a filing whose index lists a _cal.xml stores its arcs --
_calc_home = Path(tempfile.mkdtemp(prefix="calc-wire-"))
_cp_home = os.environ.get("EDGAR_SCRUBBER_HOME")
_cp_ua = os.environ.get("EDGAR_USER_AGENT")
os.environ["EDGAR_SCRUBBER_HOME"] = str(_calc_home)
os.environ["EDGAR_USER_AGENT"] = "Test Tester test@example.com"
import edgar_client as _ec3
_real_client3 = _ec3.EdgarClient
_ec3.EdgarClient = lambda **kw: _FakeCalClient(_facts, _cal_index, _cal_xml)
try:
    _wrid = server.RUNS.create("edgar-scrubber", {"limit": 1, "fields": []})
    server.taxonomy_run(_wrid, [{"cik": "320193", "accession": _ACCN,
                                 "form": _FORM}], _calc_home)
finally:
    _ec3.EdgarClient = _real_client3

_wrun = server.RUNS.get(_wrid)
check("a taxonomy run over a filing with a _cal.xml completes",
      _wrun["status"] == "done" and (_wrun.get("facts_written") or 0) > 0)
_wstore = _FactsStore(path=str(_calc_home / "store" / "facts.sqlite"))
try:
    _stored_arcs = _wstore.arcs_for("320193", _ACCN)
    check("the run stored the filing's calculation arcs",
          len(_stored_arcs) == len(_cal_arcs) and len(_stored_arcs) > 0)
    check("a stored arc carries parent/child/weight from the linkbase",
          any(a["parent"] == "us-gaap:Revenues"
              and a["child"] == "us-gaap:OtherIncome" and a["weight"] == 1.0
              for a in _stored_arcs))
    check("the negative-weight arc round-trips through the store",
          any(a["weight"] == -1.0 for a in _stored_arcs))
    check("a log line records the arcs stored",
          any("calculation arc" in l["message"] and _ACCN in l["message"]
              for l in _wrun["log"]))
finally:
    _wstore.close()

# -- AC2: a filing with no _cal.xml completes with facts written + one log line --
_nocal_home = Path(tempfile.mkdtemp(prefix="calc-nocal-"))
os.environ["EDGAR_SCRUBBER_HOME"] = str(_nocal_home)
_nocal_index = {"directory": {"name": "x", "item": [
    {"name": "aapl.htm", "type": "10-K", "size": "1"},
    {"name": "aapl_lab.xml", "type": "EX-101.LAB", "size": "1"},
]}}
_ec3.EdgarClient = lambda **kw: _FakeCalClient(_facts, _nocal_index, _cal_xml)
try:
    _nrid = server.RUNS.create("edgar-scrubber", {"limit": 1, "fields": []})
    server.taxonomy_run(_nrid, [{"cik": "320193", "accession": _ACCN,
                                 "form": _FORM}], _nocal_home)
finally:
    _ec3.EdgarClient = _real_client3

_nrun = server.RUNS.get(_nrid)
check("a filing with no _cal.xml still completes with facts written",
      _nrun["status"] == "done" and (_nrun.get("facts_written") or 0) > 0)
check("a filing with no _cal.xml logs one line naming the accession",
      any("no calculation linkbase" in l["message"] and _ACCN in l["message"]
          for l in _nrun["log"]))
_nstore = _FactsStore(path=str(_nocal_home / "store" / "facts.sqlite"))
try:
    check("no arcs are stored for a filing with no _cal.xml",
          _nstore.arcs_for("320193", _ACCN) == [])
finally:
    _nstore.close()

# -- AC3/AC4: /standardized checks on a seeded Walmart-shaped filer --
_WMT_ACCN = "0000104169-25-000012"
_WMT_PS, _WMT_PE = "2024-02-01", "2025-01-31"


def _seed_walmart(home, total):
    """A store under `home` holding one Walmart-shaped annual period plus its
    calculation arcs, with `total` as the reported Revenues value."""
    s = _FactsStore(path=str(home / "store" / "facts.sqlite"))
    try:
        s.put_facts([_FactRecord(
            cik="104169", concept=c, unit="USD", period_start=_WMT_PS,
            period_end=_WMT_PE, fiscal_year=2024, fiscal_period="FY",
            form="10-K", accession=_WMT_ACCN, value=v, source="xbrl",
            filed="2025-03-20") for c, v in (
                ("us-gaap:Revenues", total),
                ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                 674538000000),
                ("us-gaap:OtherIncome", 6447000000),
                ("us-gaap:CostOfGoodsAndServicesSold", 523002000000),
            )])
        s.put_arcs("104169", _WMT_ACCN, _cal_arcs)
        s.put_filers({"104169": "Walmart Inc."})
    finally:
        s.close()


_wmt_ok_home = Path(tempfile.mkdtemp(prefix="calc-wmt-ok-"))
_seed_walmart(_wmt_ok_home, 680985000000)
os.environ["EDGAR_SCRUBBER_HOME"] = str(_wmt_ok_home)
server._FILER_NAMES.clear()
_wstd = server.store_standardized(cik="104169", period="annual")
_wrev = next((r for r in _wstd["rows"] if r["field"] == "revenue"), None)
check("standardized has a revenue row for the Walmart-shaped filer",
      _wrev is not None and _wrev["values"].get(_WMT_PE) == 680985000000)
check("revenue carries a roll-up check that is ok when the total adds up",
      _wrev is not None and _wrev.get("checks", {}).get(_WMT_PE, {}).get("ok")
      is True)
check("the check names expected and reported",
      _wrev["checks"][_WMT_PE]["expected"] == 680985000000
      and _wrev["checks"][_WMT_PE]["reported"] == 680985000000)
# AC4: cost_of_revenue resolves to CostOfGoodsAndServicesSold, which is a CHILD
# in the arcs, never a parent -- so its row carries no checks entry.
_wcost = next((r for r in _wstd["rows"] if r["field"] == "cost_of_revenue"), None)
check("a field whose concept is not a parent in the arcs has no checks key",
      _wcost is not None and "checks" not in _wcost)

_wmt_bad_home = Path(tempfile.mkdtemp(prefix="calc-wmt-bad-"))
_seed_walmart(_wmt_bad_home, int(680985000000 * 1.01))  # total off by 1%
os.environ["EDGAR_SCRUBBER_HOME"] = str(_wmt_bad_home)
server._FILER_NAMES.clear()
_wstd_bad = server.store_standardized(cik="104169", period="annual")
_wrev_bad = next((r for r in _wstd_bad["rows"] if r["field"] == "revenue"), None)
check("revenue's check is not ok when the reported total is changed by 1%",
      _wrev_bad is not None
      and _wrev_bad.get("checks", {}).get(_WMT_PE, {}).get("ok") is False)
check("the failing check still reports the sum of children as expected",
      _wrev_bad["checks"][_WMT_PE]["expected"] == 680985000000)

server._FILER_NAMES.clear()
if _cp_home is None:
    os.environ.pop("EDGAR_SCRUBBER_HOME", None)
else:
    os.environ["EDGAR_SCRUBBER_HOME"] = _cp_home
if _cp_ua is None:
    os.environ.pop("EDGAR_USER_AGENT", None)
else:
    os.environ["EDGAR_USER_AGENT"] = _cp_ua

# --------------------------------------------------------------------------- #
section("canonical layer: /standardized (one filer) and /compare (cross-filer)")
# --------------------------------------------------------------------------- #
# One temp store seeded two ways: the Apple companyfacts fixture through
# taxonomy_run (per-filer facts that carry a `filed` date), and the frames
# fixtures through frames_ingest (cross-filer facts that carry NONE). The
# canonical endpoints must serve standard fields off both.
import frames_ingest as _fi
from facts_store import FactRecord as _FactRecord

_canon_home = Path(tempfile.mkdtemp(prefix="canon-gate-"))
_cprev_home = os.environ.get("EDGAR_SCRUBBER_HOME")
_cprev_ua = os.environ.get("EDGAR_USER_AGENT")
os.environ["EDGAR_SCRUBBER_HOME"] = str(_canon_home)
os.environ["EDGAR_USER_AGENT"] = "Test Tester test@example.com"
# filer_name memoises; earlier gates cached Apple as unknown against a different
# store, so clear it before this store fills the `filers` directory.
server._FILER_NAMES.clear()


class _FakeFramesClient:
    """Serves the committed frames fixtures by (tag, unit, period); an unseen
    concept returns None, exactly as a frames call with no data would."""

    def __init__(self, by_key):
        self.by_key = by_key

    def frames(self, taxonomy, tag, unit, period):
        return self.by_key.get((tag, unit, period))


_frames_dir = (server.REPO_ROOT / "tools" / "edgar_scrubber" / "fixtures" / "frames")
_frames_by_key = {}
for _fp in sorted(_frames_dir.glob("*.json")):
    _parts = _fp.stem.split("_")  # us-gaap, <tag>, <unit>, <period>
    _key = ("_".join(_parts[1:-2]), _parts[-2], _parts[-1])
    _frames_by_key[_key] = json.loads(_fp.read_text(encoding="utf-8"))

_frames_client = _FakeFramesClient(_frames_by_key)

# 1) Apple's per-filer facts (filed dates present).
import edgar_client as _ec2
_real_client2 = _ec2.EdgarClient
_ec2.EdgarClient = lambda **kw: _FakeFactsClient(_facts)
try:
    _crid = server.RUNS.create("edgar-scrubber", {"limit": 1, "fields": []})
    server.taxonomy_run(_crid, [{"cik": "320193", "accession": _ACCN,
                                 "form": _FORM}], _canon_home)
finally:
    _ec2.EdgarClient = _real_client2

_canon_store = _FactsStore(path=str(_canon_home / "store" / "facts.sqlite"))
try:
    # 2) Cross-filer frames facts (no filed date), for two fields.
    _fi.ingest_frames(_frames_client, "revenue", "CY2024", _canon_store)
    _fi.ingest_frames(_frames_client, "operating_income", "CY2024", _canon_store)
    # 3) A synthetic filer whose revenue at the frames period end was filed AFTER
    #    the as_of the test will use -- so it must be dropped, but never counted
    #    among the no-filed-date exclusions.
    _canon_store.put_facts([_FactRecord(
        cik="1750", concept="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        unit="USD", period_start="2024-01-01", period_end="2024-12-31",
        fiscal_year=2024, fiscal_period="FY", form="10-K",
        accession="0000001750-25-000001", value=5_000_000_000, source="xbrl",
        filed="2025-03-01")])
    _canon_store.put_filers({"1750": "Synthetic Co"})

    # #253: four filers whose FY2024 ends land in Jun, Sep, Dec and Jan -- the
    # spread that an exact-date compare drops. All carry a filed date so the
    # as_of test above still counts exactly the three frames (no-filed) rows.
    _REV = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    _fy_filers = [
        ("501", "Jun Filer", "2023-07-01", "2024-06-30", 600),
        ("502", "Sep Filer", "2023-09-30", "2024-09-28", 900),
        ("503", "Dec Filer", "2024-01-01", "2024-12-31", 1200),
        ("504", "Jan Filer", "2024-01-28", "2025-01-26", 300),
    ]
    _canon_store.put_facts([_FactRecord(
        cik=c, concept=_REV, unit="USD", period_start=ps, period_end=pe,
        fiscal_year=2024, fiscal_period="FY", form="10-K",
        accession="%010d-25-000001" % int(c), value=v, source="xbrl",
        filed="2025-03-01") for c, _n, ps, pe, v in _fy_filers])
    # The Dec filer also reports a Q4 (quarterly duration) at the SAME end date,
    # with a larger value -- it must never be ranked as the annual figure (AC3).
    _canon_store.put_facts([_FactRecord(
        cik="503", concept=_REV, unit="USD", period_start="2024-10-01",
        period_end="2024-12-31", fiscal_year=2024, fiscal_period="Q4",
        form="10-Q", accession="0000000503-25-000002", value=9_999,
        source="xbrl", filed="2025-03-01")])
    _canon_store.put_filers({c: n for c, n, _ps, _pe, _v in _fy_filers})
finally:
    _canon_store.close()

# Seed the cached company_tickers.json the ticker resolver reads (cache only).
from edgar_client import HttpCache as _HttpCache, EdgarClient as _EC
_HttpCache(str(_canon_home / "cache")).put(
    _EC.TICKERS_URL,
    json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                "1": {"cik_str": 12927, "ticker": "BA", "title": "Boeing Company"}})
    .encode("utf-8"))

try:
    # -- standardized (AC1) --
    _std = server.store_standardized(cik="320193", period="annual")
    _std_rev = next((r for r in _std["rows"] if r["field"] == "revenue"), None)
    check("standardized has a revenue row for the seeded filer",
          _std_rev is not None)
    check("standardized revenue's FY2023 value is the fixture's",
          _std_rev is not None and _std_rev["values"].get("2023-09-30") == 383285000000)
    check("standardized names the us-gaap concept behind the standard field",
          _std_rev is not None
          and _std_rev["concepts"].get("2023-09-30", "").startswith("us-gaap:"))
    check("standardized carries the filer's name and the no-filed-date count",
          _std.get("name") and _std.get("excluded_no_filed_date") == 0)

    # -- compare, no as_of (AC2) --
    _cmp = server.store_compare(field="revenue", period_end="2024-12-31")
    _cmp_ciks = [r["cik"] for r in _cmp["rows"]]
    check("compare returns the seeded frames filers for the period",
          {"320193", "12927", "19617"} <= set(_cmp_ciks))
    check("compare rows are sorted by value descending",
          all(_cmp["rows"][i]["value"] >= _cmp["rows"][i + 1]["value"]
              for i in range(len(_cmp["rows"]) - 1)))
    check("compare's top filer is Apple at the frames value",
          _cmp["rows"][0]["cik"] == "320193"
          and _cmp["rows"][0]["value"] == 391035000000)
    check("every compare row carries a name",
          all(r.get("name") for r in _cmp["rows"]))
    check("every compare row names the us-gaap concept it resolved through",
          all(r["concept"].startswith("us-gaap:") for r in _cmp["rows"]))

    # -- compare, tickers filter (AC3) --
    _cmp_t = server.store_compare(field="revenue", period_end="2024-12-31",
                                  tickers="AAPL,BA,ZZZZ")
    check("a tickers filter keeps only the matched filers",
          {r["cik"] for r in _cmp_t["rows"]} == {"320193", "12927"})
    check("an unknown ticker is reported, not silently dropped",
          "ZZZZ" in _cmp_t["unresolved"])

    # -- compare, as_of (AC4) --
    _cmp_a = server.store_compare(field="revenue", period_end="2024-12-31",
                                  as_of="2024-06-30")
    check("as_of excludes the frames facts (no filed date) and counts them",
          _cmp_a["excluded_no_filed_date"] == 3)
    check("a value filed after the as_of date is not returned",
          "1750" not in [r["cik"] for r in _cmp_a["rows"]]
          and _cmp_a["rows"] == [])

    # -- compare by fiscal year (#253) --
    _cmp_fy = server.store_compare(field="revenue", fy=2024)
    _fy_by_cik = {r["cik"]: r for r in _cmp_fy["rows"]}
    check("compare fy=2024 ranks filers whose FY ends span Jun/Sep/Dec/Jan",
          {"501", "502", "503", "504"} <= set(_fy_by_cik))
    check("each fy filer appears exactly once",
          [r["cik"] for r in _cmp_fy["rows"]].count("503") == 1)
    check("each fy row carries its own period_end",
          _fy_by_cik["501"]["period_end"] == "2024-06-30"
          and _fy_by_cik["502"]["period_end"] == "2024-09-28"
          and _fy_by_cik["503"]["period_end"] == "2024-12-31"
          and _fy_by_cik["504"]["period_end"] == "2025-01-26")
    check("a Jan-ending FY (2025-01-26) is aligned to FY2024",
          "504" in _fy_by_cik)
    _fy_seq = [r["cik"] for r in _cmp_fy["rows"] if r["cik"] in
               {"501", "502", "503", "504"}]
    check("fy rows are sorted by value descending",
          _fy_seq == ["503", "502", "501", "504"])
    check("a quarterly fact at a matching end is not ranked as the annual figure",
          _fy_by_cik["503"]["value"] == 1200)

    # Exactly one of fy / period_end (AC4).
    check("neither fy nor period_end is a ValueError",
          raises(lambda: server.store_compare(field="revenue"), ValueError))
    check("both fy and period_end is a ValueError",
          raises(lambda: server.store_compare(field="revenue", fy=2024,
                                              period_end="2024-12-31"), ValueError))

    # -- errors over the wire (AC5) -- on a dedicated loopback server, so this
    # store's env is what the handler reads.
    os.environ["EDGAR_SCRUBBER_HOME"] = str(_canon_home)
    _canon_httpd = _THS(("127.0.0.1", 0),
                        _partial(server.Handler, directory=str(server.REPO_ROOT)))
    threading.Thread(target=_canon_httpd.serve_forever, daemon=True).start()
    _canon_api = "http://127.0.0.1:%d" % _canon_httpd.server_address[1]

    def _cget(path):
        try:
            with urllib.request.urlopen(_canon_api + path, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    _st, _body = _cget("/api/store/compare?field=bogus&period_end=2024-12-31")
    check("an unknown compare field is a 400 with an error message",
          _st == 400 and _body.get("error"))
    _st, _body = _cget("/api/store/compare?field=revenue")
    check("compare with neither fy nor period_end is a 400 over the wire",
          _st == 400 and _body.get("error"))
    _st, _body = _cget(
        "/api/store/compare?field=revenue&fy=2024&period_end=2024-12-31")
    check("compare with both fy and period_end is a 400 over the wire",
          _st == 400 and _body.get("error"))
    _st, _body = _cget("/api/store/compare?field=revenue&fy=2024")
    check("compare fy over the wire returns ranked rows with their period ends",
          _st == 200 and all(r.get("period_end") for r in _body["rows"]))
    _st, _body = _cget("/api/store/standardized?cik=320193&as_of=notadate")
    check("a malformed as_of is a 400 with an error message",
          _st == 400 and _body.get("error"))
    _st, _body = _cget("/api/store/standardized?cik=320193")
    check("standardized is served over the wire",
          _st == 200 and any(r["field"] == "revenue" for r in _body["rows"]))
    _canon_httpd.shutdown()
finally:
    if _cprev_home is None:
        os.environ.pop("EDGAR_SCRUBBER_HOME", None)
    else:
        os.environ["EDGAR_SCRUBBER_HOME"] = _cprev_home
    if _cprev_ua is None:
        os.environ.pop("EDGAR_USER_AGENT", None)
    else:
        os.environ["EDGAR_USER_AGENT"] = _cprev_ua

# --------------------------------------------------------------------------- #
section("CUSIP is a literal code: withheld when invalid or not in the filing")
# --------------------------------------------------------------------------- #
from field_spec import load_spec as _load_spec
_spec424 = _load_spec(server.SCRUBBER / "field_specs" / "424b2_structured_note.json")
_fcusip = _spec424.field("cusip")
_v, _wf = server.withhold_unsupported_value(_fcusip, "424B21000", None, ["span_unlocatable"])
check("a CUSIP not found in the filing is withheld (the live '424B21000' case)",
      _v is None and _wf and _wf["code"] == "value_withheld_not_in_document")
_v, _wf = server.withhold_unsupported_value(_fcusip, "09712GX59", (10, 19), ["invalid_format"])
check("a CUSIP failing its check digit is withheld",
      _v is None and _wf and _wf["code"] == "value_withheld_invalid_format")
_v, _wf = server.withhold_unsupported_value(_fcusip, "09712GX58", (10, 19), [])
check("a valid CUSIP that appears in the filing is kept", _v == "09712GX58" and _wf is None)
_v, _wf = server.withhold_unsupported_value(_spec424.field("issuer"), "Some Issuer", None, ["span_unlocatable"])
check("a plain string field is not withheld just for being unlocatable", _v == "Some Issuer" and _wf is None)

# --------------------------------------------------------------------------- #
section("Start local model button: bring Ollama up, load the model")
# --------------------------------------------------------------------------- #
_ms_base, _ms_model = server._ollama_base_and_model()
_tags_with = {"models": [{"name": _ms_model}]}

# 1) Ollama down -> launch -> comes up on the 2nd poll -> model warmed.
_seq = [None, None, _tags_with]
_launched, _warmed = [], []
_r = server.start_local_model(up=lambda base: _seq.pop(0) if _seq else _tags_with,
                              find=lambda: "C:/fake/ollama.exe",
                              launch=_launched.append, warm=lambda: _warmed.append(1),
                              sleep=lambda s: None, wait_s=10)
check("Ollama down: it is launched once", _launched == ["C:/fake/ollama.exe"])
check("...then the model is loaded and the result is ready",
      _r["ok"] and _r["state"] == "ready" and _warmed == [1])

# 2) Already up -> no launch, just load.
_launched2 = []
_r2 = server.start_local_model(up=lambda base: _tags_with, find=lambda: "x",
                               launch=_launched2.append, warm=lambda: None,
                               sleep=lambda s: None)
check("Ollama already up: nothing is launched, the model is loaded",
      _r2["ok"] and _launched2 == [])

# 3) Model not downloaded -> never pulls; names the exact command.
_r3 = server.start_local_model(up=lambda base: {"models": [{"name": "other:1b"}]},
                               find=lambda: "x", launch=lambda e: None,
                               warm=lambda: (_ for _ in ()).throw(AssertionError("must not warm")),
                               sleep=lambda s: None)
check("model not downloaded: reported with the pull command, nothing loaded",
      not _r3["ok"] and _r3["state"] == "model-missing" and f"ollama pull {_ms_model}" in _r3["message"])

# 3b) Ollama lists "<name>:latest"; OLLAMA_MODEL is usually bare. Same model.
check("a bare model name matches its :latest tag (health and the start button)",
      server.model_pulled("edgar-qwen7b", ["qwen2.5-coder:7b", "edgar-qwen7b:latest"])
      and server.model_pulled("edgar-qwen7b:latest", ["edgar-qwen7b"])
      and not server.model_pulled("edgar-qwen7b", ["edgar-qwen7b:q8"])
      and not server.model_pulled("qwen2.5:7b", ["qwen2.5:7b-instruct-q4_K_M"]))

# 4) No ollama installed.
_r4 = server.start_local_model(up=lambda base: None, find=lambda: None,
                               launch=lambda e: None, warm=lambda: None, sleep=lambda s: None)
check("Ollama not installed: says so", not _r4["ok"] and _r4["state"] == "no-ollama")

# 5) Launched but never answers -> times out instead of hanging.
_r5 = server.start_local_model(up=lambda base: None, find=lambda: "x", launch=lambda e: None,
                               warm=lambda: None, sleep=lambda s: None, wait_s=3)
check("Ollama launched but silent: times out with a message",
      not _r5["ok"] and _r5["state"] == "ollama-timeout")

# 6) /api/health carries the start state for the page to poll.
check("/api/health reports model_start", "model_start" in server.health()
      and "state" in server.health()["model_start"])

# --------------------------------------------------------------------------- #
section("the scrubber tool names no population (#211 AC6)")
# --------------------------------------------------------------------------- #
# The scrubber is a REGISTRY of per-form scrubbers -- 10-K, 10-Q, 8-K and 424B2
# at the time of writing. A display name that picks one population out and puts
# it in the title tells a reader the tool only does that, which stopped being
# true when the registry landed. #226 removed "424B2" and left "(structured
# notes)" behind, which is the same claim in different words.

_MANIFEST = json.loads((server.REPO_ROOT / "web-dashboard" / "tools-manifest.json")
                       .read_text(encoding="utf-8"))
_SCRUBBER = [t for t in _MANIFEST if t["id"] == "edgar-scrubber"][0]

# Any of these in the DISPLAY NAME means a population has crept back in.
_POPULATION_WORDS = ("424B2", "424b2", "structured note", "structured-note",
                     "structured notes", "10-K", "10-Q", "8-K")
_leaked = [w for w in _POPULATION_WORDS if w.lower() in _SCRUBBER["name"].lower()]
check("the scrubber's display name names no filing population",
      not _leaked)
if _leaked:
    print("      name is %r and leaks: %s" % (_SCRUBBER["name"], ", ".join(_leaked)))
check("...and it is still a non-empty name",
      isinstance(_SCRUBBER["name"], str) and _SCRUBBER["name"].strip())

# The registry, not the title, is where form types belong -- and it has to be
# ASSERTED, not skipped. An `if available:` guard around a check is how a test
# reads green while verifying nothing.
import scrubbers as _scrubbers_mod

_reg = _scrubbers_mod.load_scrubbers()
_forms = sorted(s.form_type for s in _reg.values())
check("the registry carries the form types, and carries more than one",
      len(_forms) > 1)
check("...including the population the title used to claim",
      "424B2" in _forms)
print("      registry serves: %s" % ", ".join(_forms))

# --------------------------------------------------------------------------- #
section("search takes any well-formed form; a run for an unowned form refuses (#279)")
# --------------------------------------------------------------------------- #
# The registry owns four forms (10-K/10-Q/8-K/424B2). Search is a discovery step
# and must not be penned to those -- it takes any well-formed EDGAR form code --
# but a RUN for a form nothing extracts has to fail loudly, not be silently
# scrubbed as a 424B2 structured note.

check("FIELD_SOURCES carries xml for the later issues that add such a scrubber",
      "xml" in _scrubbers_mod.FIELD_SOURCES)
check("supported_forms unions every registered scrubber's forms",
      set(_scrubbers_mod.supported_forms()) >= {"10-K", "10-Q", "8-K", "424B2"})

# Form-code validation: well-formed codes are upper-cased and kept; an injected
# one is refused naming it.
check("a bare form passes, upper-cased", server.normalize_forms(["s-1"]) == ["S-1"])
check("a form with a space passes", server.normalize_forms(["DEF 14A"]) == ["DEF 14A"])
check("a slashed amendment passes", server.normalize_forms(["SC 13D/A"]) == ["SC 13D/A"])
check("a comma string is split and validated",
      server.normalize_forms("13F-HR, SCHEDULE 13G") == ["13F-HR", "SCHEDULE 13G"])
_bad_form = None
try:
    server.normalize_forms(["10-K;DROP"])
except ValueError as _e:
    _bad_form = str(_e)
check("an injected form code is refused, naming it",
      _bad_form is not None and "10-K;DROP" in _bad_form)

# A run for a form nothing extracts refuses BEFORE any SEC request.
_run_refused = None
try:
    server.start_scrubber_run({"forms": ["S-1"]})
except ValueError as _e:
    _run_refused = str(_e)
check("a run for an unowned form is refused with 'no scrubber extracts S-1'",
      _run_refused == "no scrubber extracts S-1")

# 424B2 and an empty form list both pass the form gate and reach target
# resolution exactly as before. Stub resolve_targets so neither touches SEC;
# an empty target list then raises the SAME "nothing to extract" both would.
_real_rt = server.resolve_targets
server.resolve_targets = lambda params: ([], {"source": "selection", "limit": 1})
try:
    for _desc, _req in (("424B2", {"forms": ["424B2"]}), ("no forms", {})):
        _reached = None
        try:
            server.start_scrubber_run(_req)
        except ValueError as _e:
            _reached = str(_e)
        check("a run with %s passes the form gate and resolves targets as before"
              % _desc,
              _reached == "nothing to extract -- the query returned no filings")
finally:
    server.resolve_targets = _real_rt

# Over the wire: the search route, the /forms route and the run route.
_279_httpd = _THS(("127.0.0.1", 0),
                  _partial(server.Handler, directory=str(server.REPO_ROOT)))
threading.Thread(target=_279_httpd.serve_forever, daemon=True).start()
_279_base = "http://127.0.0.1:%d" % _279_httpd.server_address[1]


def _post279(path, payload):
    _req = urllib.request.Request(
        _279_base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(_req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _get279(path):
    try:
        with urllib.request.urlopen(_279_base + path, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


# An injected form is a 400 naming it -- validation happens before the UA check,
# so this needs neither EDGAR_USER_AGENT nor the network.
_prev_ua279 = os.environ.pop("EDGAR_USER_AGENT", None)
try:
    _st, _b = _post279("/api/tools/edgar-scrubber/search", {"forms": ["10-K;DROP"]})
    check("POST search refuses an injected form with a 400 naming it",
          _st == 400 and "10-K;DROP" in _b.get("error", ""))

    # A well-formed form is ACCEPTED: it gets past validation to the UA check
    # (there is no allowlist to trip on), which is as far as an offline test can
    # drive it. A form-code 400 here would mean it was rejected.
    for _form in ("S-1", "DEF 14A"):
        _st, _b = _post279("/api/tools/edgar-scrubber/search", {"forms": [_form]})
        check("POST search accepts %r -- past validation to the UA check" % _form,
              _st == 400 and "EDGAR_USER_AGENT" in _b.get("error", "")
              and "form code" not in _b.get("error", ""))

    # The run route maps the ValueError to a 400 naming the form.
    _st, _b = _post279("/api/tools/edgar-scrubber/run", {"forms": ["S-1"]})
    check("POST run for an unowned form is a 400 saying 'no scrubber extracts S-1'",
          _st == 400 and "no scrubber extracts S-1" in _b.get("error", ""))
finally:
    if _prev_ua279 is not None:
        os.environ["EDGAR_USER_AGENT"] = _prev_ua279

_st, _b = _get279("/api/tools/edgar-scrubber/forms")
check("GET /forms lists the registry's owned forms",
      _st == 200 and "424B2" in _b.get("forms", []) and "10-K" in _b.get("forms", []))

_279_httpd.shutdown()

# --------------------------------------------------------------------------- #
section("charset -- declared, not sniffed")
# --------------------------------------------------------------------------- #

check("text/html gains the charset",
      server.with_charset("text/html") == "text/html; charset=utf-8")
check("application/json gains it too",
      server.with_charset("application/json") == "application/json; charset=utf-8")
check("css and js gain it",
      server.with_charset("text/css") == "text/css; charset=utf-8"
      and server.with_charset("application/javascript")
      == "application/javascript; charset=utf-8")
check("a binary type does not -- there is no UTF-8 PNG",
      server.with_charset("image/png") == "image/png"
      and server.with_charset("font/woff2") == "font/woff2")
check("an existing charset is left alone, not doubled",
      server.with_charset("text/html; charset=iso-8859-1")
      == "text/html; charset=iso-8859-1")
check("a type with parameters is not mangled",
      server.with_charset("text/html;charset=utf-8") == "text/html;charset=utf-8")

# Over the wire, on both an API response and a static file. The static path goes
# through guess_type, which is a different code path from _send.
_cs_httpd = _THS(("127.0.0.1", 0),
                 _partial(server.Handler, directory=str(server.REPO_ROOT)))
threading.Thread(target=_cs_httpd.serve_forever, daemon=True).start()
_cs_base = "http://127.0.0.1:%d" % _cs_httpd.server_address[1]


def _ctype(path):
    try:
        with urllib.request.urlopen(_cs_base + path, timeout=5) as r:
            return r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.headers.get("Content-Type", "")


check("a JSON response declares utf-8 on the wire",
      "charset=utf-8" in _ctype("/api/health"))
check("a served HTML file declares utf-8 on the wire",
      "charset=utf-8" in _ctype("/web-dashboard/index.html"))

# The header alone has to be sufficient: serve a UTF-8 file carrying no <meta
# charset> at all and confirm the bytes and the declared type still agree.
_nometa = Path(tempfile.mkdtemp(prefix="charset-gate-")) / "nometa.html"
_nometa.write_text("<!doctype html><html><body>— é \U0001f5c4</body></html>",
                   encoding="utf-8")
_nm_httpd = _THS(("127.0.0.1", 0),
                 _partial(server.Handler, directory=str(_nometa.parent)))
threading.Thread(target=_nm_httpd.serve_forever, daemon=True).start()
with urllib.request.urlopen("http://127.0.0.1:%d/nometa.html"
                            % _nm_httpd.server_address[1], timeout=5) as _r:
    _hdr = _r.headers.get("Content-Type", "")
    _body = _r.read()
check("a file with no <meta charset> is still served as utf-8",
      "charset=utf-8" in _hdr)
check("...and its bytes decode as utf-8 under that declaration",
      _body.decode("utf-8").endswith("</body></html>")
      and "\U0001f5c4" in _body.decode("utf-8"))
_nm_httpd.shutdown()
_cs_httpd.shutdown()

# --------------------------------------------------------------------------- #
section("derived fields reach /standardized and /compare (#260 follow-up)")
# --------------------------------------------------------------------------- #
# #260 keeps `other_revenue` out of CANONICAL (frames cannot fetch a computed
# value), and both endpoints iterated CANONICAL alone -- so on live Walmart data
# the derived $6.447B never appeared. Seed a Walmart-shaped filer and ask.
_dv_home = Path(tempfile.mkdtemp(prefix="derived-gate-"))
_dv_prev = os.environ.get("EDGAR_SCRUBBER_HOME")
os.environ["EDGAR_SCRUBBER_HOME"] = str(_dv_home)
server._FILER_NAMES.clear()
_dv_store = _FactsStore(path=str(_dv_home / "store" / "facts.sqlite"))
try:
    _dv_store.put_facts([
        _FactRecord(cik="104169", concept=c, unit="USD", period_start="2024-02-01",
                    period_end="2025-01-31", fiscal_year=2025, fiscal_period="FY",
                    form="10-K", accession="0000104169-25-000021", value=v,
                    source="xbrl", filed="2025-03-14")
        for c, v in (("us-gaap:Revenues", 680_985_000_000),
                     ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                      674_538_000_000))])
    _dv_store.put_filers({"104169": "Walmart-shaped Co"})
finally:
    _dv_store.close()
try:
    _dv = server.store_standardized(cik="104169")
    _dv_rows = {r["field"]: r for r in _dv["rows"]}
    check("/standardized returns the derived other_revenue row",
          "other_revenue" in _dv_rows)
    check("...valued total minus sales, 6447000000",
          (_dv_rows.get("other_revenue") or {}).get("values", {}).get("2025-01-31")
          == 6_447_000_000)
    check("...beside total revenue and sales revenue",
          _dv_rows["revenue"]["values"]["2025-01-31"] == 680_985_000_000
          and _dv_rows["sales_revenue"]["values"]["2025-01-31"] == 674_538_000_000)
    _dvc = server.store_compare(field="other_revenue", fy="2024")
    check("/compare accepts a derived field and ranks it",
          [(r["name"], r["value"]) for r in _dvc["rows"]]
          == [("Walmart-shaped Co", 6_447_000_000)])
finally:
    if _dv_prev is None:
        os.environ.pop("EDGAR_SCRUBBER_HOME", None)
    else:
        os.environ["EDGAR_SCRUBBER_HOME"] = _dv_prev
    server._FILER_NAMES.clear()

# --------------------------------------------------------------------------- #
section("structured-XML forms: xml_run routing + /api/store/forms (#286)")
# --------------------------------------------------------------------------- #
# Drive the whole XML path offline: the committed Form 4 fixture through a fake
# client (filing_index + archive_document, no network), into a temp FormsStore.
# A model call or an OutputStore write here would be a routing bug.
from forms_store import FormsStore as _FormsStore

_FORM4_FIX = (server.REPO_ROOT / "tools" / "edgar_scrubber" / "tests"
              / "fixtures" / "form4_cook.xml")
_form4_bytes = _FORM4_FIX.read_bytes()
_F13F_DIR = (server.REPO_ROOT / "tools" / "edgar_scrubber" / "tests" / "fixtures")
_f13f_primary_bytes = (_F13F_DIR / "form13fhr_primary.xml").read_bytes()
_f13f_info_bytes = (_F13F_DIR / "form13fhr_infotable.xml").read_bytes()
_ACCN_OK = "0000320193-26-000031"       # a readable ownership XML
_ACCN_BAD = "0000000000-26-000099"      # only a wrong-schema XML -> skipped
_ACCN_13F = "0001067983-26-000057"      # 13F-HR: primary_doc + info table


class _FakeXmlClient:
    """Serves one filing index + its documents per accession, no network. A
    `company_facts` call would mean the run took the taxonomy path by mistake,
    so it fails loudly rather than returning data."""

    def __init__(self, filings):
        self.filings = filings
        self.facts_calls = 0

    def filing_index(self, cik, accession):
        return {"directory": {"name": accession,
                              "item": self.filings[accession]["items"]}}

    def archive_document(self, cik, accession, name):
        return self.filings[accession]["docs"][name]

    def company_facts(self, cik):
        self.facts_calls += 1
        raise AssertionError("xml_run must never fetch companyfacts")


_xml_filings = {
    _ACCN_OK: {"items": [{"name": "form4.xml", "type": "4", "size": "1"}],
               "docs": {"form4.xml": _form4_bytes}},
    # A lone XBRL-shaped instance doc: read_ownership rejects it (root is not
    # ownershipDocument), so pick_xml_rows finds nothing readable.
    _ACCN_BAD: {"items": [{"name": "inst_htm.xml", "type": "XML", "size": "1"}],
                "docs": {"inst_htm.xml": b"<xbrl><context/></xbrl>"}},
    # A 13F-HR: the cover page (primary_doc.xml) lists first, then the holdings
    # in a separate information-table document. pick_xml_rows must skip past the
    # cover and read the info table, stamping the cover fields onto each row.
    _ACCN_13F: {"items": [{"name": "primary_doc.xml", "type": "13F-HR", "size": "1"},
                          {"name": "infotable.xml", "type": "INFORMATION TABLE", "size": "1"}],
                "docs": {"primary_doc.xml": _f13f_primary_bytes,
                         "infotable.xml": _f13f_info_bytes}},
}

_xml_home = Path(tempfile.mkdtemp(prefix="xmlrun-gate-"))
_xprev_home = os.environ.get("EDGAR_SCRUBBER_HOME")
_xprev_ua = os.environ.get("EDGAR_USER_AGENT")
os.environ["EDGAR_SCRUBBER_HOME"] = str(_xml_home)
os.environ["EDGAR_USER_AGENT"] = "Test Tester test@example.com"

# The registry must own forms 3/4/5 on the xml path.
check("form 4 resolves to the xml path",
      server.scrubber_for_forms(["4"]) is not None
      and server.scrubber_for_forms(["4"]).field_source == "xml")
check("forms 3 and 5 also resolve to the xml path",
      all((server.scrubber_for_forms([f]) or None)
          and server.scrubber_for_forms([f]).field_source == "xml"
          for f in ("3", "5")))

import edgar_client as _ecx
_real_ecx = _ecx.EdgarClient
_fake_xml = _FakeXmlClient(_xml_filings)
_ecx.EdgarClient = lambda **kw: _fake_xml
try:
    # The acceptance path: POST /run with forms ["4"] and an accession. Routed
    # through start_scrubber_run, which spawns the worker thread.
    import threading as _threading
    _xrid = server.start_scrubber_run({
        "forms": ["4"],
        "accessions": [{"accession": _ACCN_OK, "cik": "320193",
                        "file_date": "2026-03-03"},
                       {"accession": _ACCN_BAD, "cik": "320193",
                        "file_date": "2026-03-04"}],
        "limit": 2,
    })
    for _th in _threading.enumerate():
        if _th.name == "run-%s" % _xrid:
            _th.join(timeout=20)
finally:
    _ecx.EdgarClient = _real_ecx

_xrun = server.RUNS.get(_xrid)
check("a form-4 run is routed to the xml field_source",
      _xrun["params"].get("field_source") == "xml")
check("the xml run completes", _xrun["status"] == "done")
check("no companyfacts fetch -- the run did not take the taxonomy path",
      _fake_xml.facts_calls == 0)
check("no OutputStore write -- extractions.sqlite is never created",
      not (_xml_home / "store" / "extractions.sqlite").exists())
check("no facts store write either", not (_xml_home / "store" / "facts.sqlite").exists())
check("one filing read, one skipped",
      _xrun.get("rows_written") == 1 and _xrun.get("filings_skipped") == 1)
check("the unreadable filing is logged and skipped",
      any(_ACCN_BAD in l["message"] and "skipped" in l["message"]
          for l in _xrun["log"]))


# A selection carries only {accession, cik}: the form and filed date come from
# the filer's submissions feed (a SCHEDULE 13G was stored as "SCHEDULE 13D",
# with filed=null, before this).
class _SubsClient:
    calls = 0

    def submissions(self, cik):
        _SubsClient.calls += 1
        return {"filings": {"recent": {"accessionNumber": ["0002160411-26-000001", "x"],
                                       "form": ["SCHEDULE 13G", "4"],
                                       "filingDate": ["2026-10-02", "2026-10-03"]}}}


class _BrokenSubsClient:
    def submissions(self, cik):
        raise OSError("SEC down")


server._SUBMISSIONS_CACHE.clear()
_sc = _SubsClient()
check("filing_meta reads form and filed date from the submissions feed",
      server.filing_meta(_sc, "2160411", "0002160411-26-000001") == ("SCHEDULE 13G", "2026-10-02"))
check("filing_meta reads the feed once per filer",
      server.filing_meta(_sc, "0002160411", "x") == ("4", "2026-10-03") and _SubsClient.calls == 1)
check("an accession outside the feed gives (None, None)",
      server.filing_meta(_sc, "2160411", "nope") == (None, None))
server._SUBMISSIONS_CACHE.clear()
check("a failed feed read gives (None, None) and is not cached",
      server.filing_meta(_BrokenSubsClient(), "999", "a") == (None, None)
      and "999" not in server._SUBMISSIONS_CACHE)
check("the run reports rows_total beside rows_written", "rows_total" in _xrun)

# The dashboard's Run sends ticked hits and no `forms`: route by the hits' own form.
check("a selection of Form 4 hits routes to the xml extractor",
      server.run_forms({"accessions": [{"accession": "a", "cik": "1", "form": "4"}]}) == ["4"]
      and server.scrubber_for_forms(server.run_forms(
          {"accessions": [{"accession": "a", "cik": "1", "form": "4"}]})).field_source == "xml")
check("13G and 13D/A hits share one extractor and are allowed together",
      server.run_forms({"accessions": [{"accession": "a", "form": "SCHEDULE 13G"},
                                       {"accession": "b", "form": "SCHEDULE 13D/A"}]})
      == ["SCHEDULE 13D/A", "SCHEDULE 13G"])
try:
    server.run_forms({"accessions": [{"accession": "a", "form": "4"}, {"accession": "b", "form": "10-K"}]})
    _mixed = None
except ValueError as _e:
    _mixed = str(_e)
check("a selection mixing two extractors is refused", bool(_mixed) and "mixes" in _mixed)
check("explicit forms win, and no forms anywhere keeps the 424B2 default (empty list)",
      server.run_forms({"forms": ["10-K"], "accessions": [{"accession": "a", "form": "4"}]}) == ["10-K"]
      and server.run_forms({"accessions": [{"accession": "a", "cik": "1"}]}) == [])

# The Results tab reads this endpoint.
_grid = server.store_forms(form="4")
check("store_forms returns the run's rows", len(_grid["rows"]) == 3)
check("columns are the union of row keys",
      "shares" in _grid["columns"] and "security_title" in _grid["columns"]
      and "table" in _grid["columns"])
check("a transaction row carries its lifted values",
      any(r.get("shares") == 511000 and r.get("price") == 170.0
          for r in _grid["rows"]))
check("every flattened row is tagged with its filing",
      all(r.get("_accession") == _ACCN_OK for r in _grid["rows"]))
check("one document, from the readable filing only",
      [d["accession"] for d in _grid["documents"]] == [_ACCN_OK])

check("a cik filter keeps that filer",
      len(server.store_forms(form="4", cik="320193")["rows"]) == 3)
check("a cik filter excludes other filers",
      server.store_forms(form="4", cik="999")["rows"] == [])
check("a date range that spans the filing keeps it",
      len(server.store_forms(form="4", date_from="2026-01-01",
                             date_to="2026-12-31")["rows"]) == 3)
check("a date range after the filing excludes it",
      server.store_forms(form="4", date_from="2027-01-01")["rows"] == [])
check("a form with nothing stored is empty, not another form's rows",
      server.store_forms(form="3")["rows"] == []
      and server.store_forms(form="3")["documents"] == [])

# xml_run skips a selection with no CIK and still finishes.
_nrid = server.RUNS.create("edgar-scrubber", {"limit": 1, "fields": []})
_ecx.EdgarClient = lambda **kw: _FakeXmlClient(_xml_filings)
try:
    server.xml_run(_nrid, [{"accession": _ACCN_OK, "file_date": "2026-03-03"}],
                   _xml_home, form_type="4")
finally:
    _ecx.EdgarClient = _real_ecx
_nxrun = server.RUNS.get(_nrid)
check("a selection with no CIK is skipped and the run still finishes",
      _nxrun["status"] == "done" and _nxrun.get("filings_skipped") == 1
      and _nxrun.get("rows_written") == 0)

# A 13F-HR run reads the information-table document (not primary_doc.xml) and
# writes its holdings, each stamped with the cover fields from primary_doc.xml.
check("13F-HR resolves to the xml path",
      (server.scrubber_for_forms(["13F-HR"]) or None)
      and server.scrubber_for_forms(["13F-HR"]).field_source == "xml")

# Structured Schedule 13D/13G (#291) resolve to the xml path; the legacy
# HTML/text SC 13D / SC 13G are owned by no scrubber, so the search view shows
# them as "search only, no extractor".
_s13g = server.scrubber_for_forms(["SCHEDULE 13G"])
check("SCHEDULE 13G resolves to the xml 13D/13G scrubber",
      _s13g is not None and _s13g.id == "schedule-13d-13g"
      and _s13g.field_source == "xml")
check("legacy SC 13G is owned by no scrubber",
      server.scrubber_for_forms(["SC 13G"]) is None)

_f13frid = server.RUNS.create("edgar-scrubber", {"limit": 1, "fields": []})
_ecx.EdgarClient = lambda **kw: _FakeXmlClient(_xml_filings)
try:
    server.xml_run(_f13frid,
                   [{"accession": _ACCN_13F, "cik": "1067983",
                     "file_date": "2026-08-14"}],
                   _xml_home, form_type="13F-HR")
finally:
    _ecx.EdgarClient = _real_ecx
_f13frun = server.RUNS.get(_f13frid)
check("the 13F-HR run completes and writes the one filing",
      _f13frun["status"] == "done" and _f13frun.get("rows_written") == 1)
_13f_docs = _f13frun.get("documents") or []
check("xml_run read the information table, not primary_doc.xml",
      len(_13f_docs) == 1 and _13f_docs[0]["document"] == "infotable.xml")
_13f_grid = server.store_forms(form="13F-HR")
check("the 13F holdings land in FormsStore, one row per infoTable",
      len(_13f_grid["rows"]) == 3)
check("a holding carries its lifted numeric values",
      any(r.get("cusip") == "037833100" and r.get("value") == 91000000
          and r.get("shares_or_principal") == 915560382
          for r in _13f_grid["rows"]))
check("every holding carries the cover fields from primary_doc.xml",
      all(r.get("report_period") == "2026-06-30"
          and r.get("manager_name") == "BERKSHIRE HATHAWAY INC"
          for r in _13f_grid["rows"]))
check("a holding with no putCall reads back null",
      any(r.get("name_of_issuer") == "APPLE INC" and r.get("put_call") is None
          for r in _13f_grid["rows"]))

if _xprev_home is None:
    os.environ.pop("EDGAR_SCRUBBER_HOME", None)
else:
    os.environ["EDGAR_SCRUBBER_HOME"] = _xprev_home
if _xprev_ua is None:
    os.environ.pop("EDGAR_USER_AGENT", None)
else:
    os.environ["EDGAR_USER_AGENT"] = _xprev_ua

# --------------------------------------------------------------------------- #
section("research assistant -- proposal batches (plan, edit, approve, caps)")
# --------------------------------------------------------------------------- #
# Nothing the assistant proposes may run without the user approving THAT batch
# by id. Exercised with a fake provider (no model) and a stubbed search (no
# network): the plan step must store a proposal and touch nothing executable,
# and only an approve of a specific id may run the stored searches, full-text
# only and hard-capped at 20 hits apiece.

# A fake provider returns a canned plan and records that it was asked -- no
# model, no key, no network. build_research_provider is the seam we replace.
class _FakeResearchProvider:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def complete(self, system, messages):
        self.calls += 1
        return json.dumps(self.payload)


# A stub search records every call (and its max_results) and returns a hit.
class _StubSearch:
    def __init__(self):
        self.calls = []

    def __call__(self, params):
        self.calls.append(dict(params))
        return {"returned": 1, "total": 1, "hits": [{"accession": "a-%d" % len(self.calls),
                                                     "q": params.get("q")}]}


_two_plan = {
    "reply": "Proposing two searches.",
    "searches": [
        {"q": "contingent coupon", "forms": ["424B2"], "ciks": [],
         "startdt": "", "enddt": "", "rationale": "the coupon terms"},
        {"q": "buffer", "forms": [], "ciks": ["AAPL"], "startdt": "",
         "enddt": "", "rationale": "the buffer"},
    ],
}

_stub_search = _StubSearch()
_prev_search = server.edgar_search
_prev_build = server.build_research_provider
_prev_research = server.RESEARCH
_fake_provider = _FakeResearchProvider(_two_plan)
server.edgar_search = _stub_search
server.build_research_provider = lambda name, chat_id=None: _fake_provider
server.RESEARCH = server.ResearchBatchStore()      # this test's counts only

_rb_httpd = _THS(("127.0.0.1", 0), _partial(server.Handler, directory=str(server.REPO_ROOT)))
threading.Thread(target=_rb_httpd.serve_forever, daemon=True).start()
_rb_base = "http://127.0.0.1:%d" % _rb_httpd.server_address[1]
_RB = "/api/tools/research"


def _rb_req(method, path, payload, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        _rb_base + _RB + path, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


# -- plan: stores a proposal, runs nothing ----------------------------------
_st, _b = _rb_req("POST", "/plan", {"provider": "fake", "message": "coupons?",
                                    "chat_id": "c1"})
check("POST /plan stores a batch with status 'proposed'",
      _st == 200 and _b["status"] == "proposed")
check("...carrying the validated searches", len(_b["searches"]) == 2)
check("...after asking the provider exactly once", _fake_provider.calls == 1)
check("POST /plan does NOT call the search function", _stub_search.calls == [])
_bid = _b["batch_id"]

_st, _b = _rb_req("POST", "/plan", {"provider": "fake", "message": "", "chat_id": "c1"})
check("POST /plan with an empty message is a 400", _st == 400)

# -- providers: what the picker may offer, and why Claude is disabled --------
import research_assistant as _ra
_st, _b = _rb_req("GET", "/providers", None)
_provs = {p["name"]: p for p in _b.get("providers", [])}
check("GET /providers lists ollama, claude-code (the user's plan) and claude (API key)",
      _st == 200 and set(_provs) == {"ollama", "claude-code", "claude"})
check("ollama is always available", _provs["ollama"]["available"] is True
      and _provs["ollama"]["reason"] == "")
_claude_ok, _claude_reason = _ra.claude_available()
check("claude availability matches claude_available()",
      _provs["claude"]["available"] is bool(_claude_ok))
check("an unavailable claude carries the server's reason",
      bool(_claude_ok) or _provs["claude"]["reason"] == _claude_reason)

# -- edit: validated, proposed-only -----------------------------------------
_st, _b = _rb_req("PUT", "/batch/%s" % _bid,
                  {"searches": [{"q": "x", "forms": ["FORM!"], "ciks": [],
                                 "startdt": "", "enddt": ""}]})
check("PUT with an invalid form code returns 400", _st == 400)

_st, _b = _rb_req("PUT", "/batch/%s" % _bid,
                  {"searches": [{"q": "only one", "forms": ["10-K"], "ciks": [],
                                 "startdt": "", "enddt": "", "rationale": "r"}]})
check("a valid edit replaces the stored searches",
      _st == 200 and len(_b["searches"]) == 1 and _b["searches"][0]["q"] == "only one")

_st, _b = _rb_req("PUT", "/batch/%s" % _bid,
                  {"searches": [{"q": "q%d" % i, "forms": [], "ciks": [],
                                 "startdt": "", "enddt": ""} for i in range(6)]})
check("an edit to 6 searches is refused with 400", _st == 400)

_st, _b = _rb_req("PUT", "/batch/does-not-exist",
                  {"searches": [{"q": "x", "forms": [], "ciks": [],
                                 "startdt": "", "enddt": ""}]})
check("PUT to an unknown batch is a 404", _st == 404)

# A foreign Origin is refused the same as on POST.
_st, _b = _rb_req("PUT", "/batch/%s" % _bid,
                  {"searches": []}, origin="http://evil.example")
check("a cross-origin PUT is refused 403", _st == 403)

# -- approve: runs the stored searches, capped, once ------------------------
_st, _b = _rb_req("POST", "/batch/%s/approve" % _bid, {})
check("POST approve runs the batch and marks it 'ran'",
      _st == 200 and _b["status"] == "ran")
check("approve ran exactly the stored search(es)",
      len(_stub_search.calls) == 1 and _stub_search.calls[0]["q"] == "only one")
check("each search was capped at max_results <= 20",
      all(c["max_results"] <= 20 for c in _stub_search.calls))
check("the hits are stored on the batch",
      _b["hits"] and _b["hits"][0]["result"]["returned"] == 1)

_st, _b = _rb_req("POST", "/batch/%s/approve" % _bid, {})
check("a second approve returns 409", _st == 409)
check("...and did not re-run the searches", len(_stub_search.calls) == 1)

_st, _b = _rb_req("PUT", "/batch/%s" % _bid,
                  {"searches": [{"q": "late", "forms": [], "ciks": [],
                                 "startdt": "", "enddt": ""}]})
check("editing a batch that already ran returns 409", _st == 409)

_st, _b = _rb_req("POST", "/batch/no-such/approve", {})
check("approving an unknown batch is a 404", _st == 404)

# -- caps: at most 20 batches per chat --------------------------------------
_cap = server.ResearchBatchStore.MAX_BATCHES_PER_CHAT
for _i in range(_cap):
    _st, _ = _rb_req("POST", "/plan", {"provider": "f", "message": "m",
                                       "chat_id": "cap"})
    if _st != 200:
        break
check("a chat accepts up to the batch cap", _st == 200)
_st, _b = _rb_req("POST", "/plan", {"provider": "f", "message": "m",
                                    "chat_id": "cap"})
check("the %dst batch in one chat is refused with 400" % (_cap + 1), _st == 400)

# A model that proposes 6 searches has the 6th dropped by plan(), not stored.
_fake_provider.payload = {
    "reply": "too many",
    "searches": [{"q": "q%d" % i, "forms": [], "ciks": [], "startdt": "",
                  "enddt": "", "rationale": "r"} for i in range(6)],
}
_st, _b = _rb_req("POST", "/plan", {"provider": "f", "message": "m",
                                    "chat_id": "over"})
check("a plan proposing 6 searches stores only the 5-search cap",
      _st == 200 and len(_b["searches"]) == server.ResearchBatchStore.MAX_SEARCHES)
check("...and records the dropped 6th", bool(_b["dropped"]))

_rb_httpd.shutdown()
server.edgar_search = _prev_search
server.build_research_provider = _prev_build
server.RESEARCH = _prev_research

# Claude on the user's plan: a provider entry, per-chat sessions, a reset route.
_provs = {p["name"]: p for p in server.research_providers()["providers"]}
check("providers offer Claude on the user's plan beside the API-key option",
      "claude-code" in _provs and _provs["claude-code"]["label"] == "Claude (your plan)"
      and _provs["claude"]["label"] == "Claude (API key)")
import research_assistant as _ra_cc
_ccp = server.build_research_provider("claude-code", "chat-xyz")
check("claude-code builds a per-chat Claude Code provider",
      isinstance(_ccp, _ra_cc.ClaudeCodeProvider) and _ccp.chat_id == "chat-xyz")
check("claude-code is not a paid provider (no cost gate)", not server._is_paid_provider("claude-code"))
check("session reset for a chat with no session reports reset=false",
      server.research_session_reset({"chat_id": "never-started"}) == {"reset": False, "chat_id": "never-started"})
try:
    server.research_session_reset({})
    _rs_err = None
except ValueError as _e:
    _rs_err = str(_e)
check("session reset needs a chat_id", _rs_err == "chat_id is required")

# --------------------------------------------------------------------------- #
section("research assistant -- cost gate, metadata-only history, audit, persist")
# --------------------------------------------------------------------------- #
# The guardrails that make the assistant safe to leave running (#287): no paid
# Claude call before the user has seen a cost, no filing text ever fed back to
# the model, every decision on the audit log, and chats/batches that survive a
# restart. Exercised with a fake provider (no key, no network) and a stub search
# over a disk root under tempfile.

_gr_root = Path(tempfile.mkdtemp(prefix="research-guard-"))


# A fake provider that records the conversation it was handed, so the history
# fed to plan() can be inspected. `model` lets estimate_cost price it (or not).
class _GateProvider:
    def __init__(self, payload):
        self.payload, self.calls, self.last_messages = payload, 0, None
        self.model = "fake-model"

    def complete(self, system, messages):
        self.calls += 1
        self.last_messages = list(messages)
        return json.dumps(self.payload)


# A stub search whose hit carries BOTH metadata and a filing-text body, so the
# "metadata only" claim is testable: the body must never reach the history.
_SECRET = "SECRET FILING BODY: contingent coupon mechanics in full"


def _gr_search(params):
    return {"returned": 1, "total": 1, "hits": [{
        "accession": "0001234567-25-000042", "form": "424B2",
        "issuer": "Example Structured Products Inc", "cik": "0001234567",
        "file_date": "2025-06-10", "document": "d424b2.htm",
        "body": _SECRET, "text": _SECRET}]}


_gr_payload = {
    "reply": "One search proposed.",
    "searches": [{"q": "contingent coupon", "forms": ["424B2"], "ciks": [],
                  "startdt": "", "enddt": "", "rationale": "coupon terms"}],
}

_gr_prev_search = server.edgar_search
_gr_prev_build = server.build_research_provider
_gr_prev_research = server.RESEARCH
_gr_provider = _GateProvider(_gr_payload)
server.edgar_search = _gr_search
server.build_research_provider = lambda name, chat_id=None: _gr_provider
server.RESEARCH = server.ResearchBatchStore(_gr_root)

_gr_httpd = _THS(("127.0.0.1", 0), _partial(server.Handler, directory=str(server.REPO_ROOT)))
threading.Thread(target=_gr_httpd.serve_forever, daemon=True).start()
_gr_base = "http://127.0.0.1:%d" % _gr_httpd.server_address[1]


def _gr_req(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _gr_base + "/api/tools/research" + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _audit_lines():
    p = _gr_root / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# -- cost gate: a paid plan needs an estimate the chat has seen --------------
_st, _b = _gr_req("POST", "/plan", {"provider": "claude", "message": "coupons?",
                                    "chat_id": "g1"})
check("POST /plan claude with no estimate_id is a 409", _st == 409)
check("...and did NOT call the provider", _gr_provider.calls == 0)

_st, _b = _gr_req("POST", "/estimate", {"provider": "claude", "message": "coupons?",
                                        "chat_id": "g1"})
check("POST /estimate returns a cost and an estimate_id",
      _st == 200 and _b.get("estimate_id") and _b.get("input_tokens", 0) > 0)
check("...without calling the provider (text measured, not sent)",
      _gr_provider.calls == 0)
_eid = _b["estimate_id"]

_st, _b = _gr_req("POST", "/plan", {"provider": "claude", "message": "coupons?",
                                    "chat_id": "g1", "estimate_id": _eid})
check("POST /plan claude WITH a valid estimate_id proceeds (200)",
      _st == 200 and _b["status"] == "proposed")
check("...and now the provider was called exactly once", _gr_provider.calls == 1)
_g1_batch = _b["batch_id"]

# The estimate is spent: a second paid plan reusing it is gated again.
_st, _b = _gr_req("POST", "/plan", {"provider": "claude", "message": "again?",
                                    "chat_id": "g1", "estimate_id": _eid})
check("a spent estimate_id no longer unlocks a paid plan (409)", _st == 409)

# -- metadata-only history: a run's hits summarise to metadata, no body ------
_st, _approved = _gr_req("POST", "/batch/%s/approve" % _g1_batch, {})
check("approving the batch runs it and stores hits", _st == 200 and _approved["hits"])

_gr_provider.calls = 0
_st, _est = _gr_req("POST", "/estimate", {"provider": "claude", "message": "more?",
                                          "chat_id": "g1"})
_st, _b = _gr_req("POST", "/plan", {"provider": "claude", "message": "more?",
                                    "chat_id": "g1", "estimate_id": _est["estimate_id"]})
_hist = json.dumps(_gr_provider.last_messages or [])
check("the history fed to the next plan carries each hit's metadata",
      all(s in _hist for s in ("424B2", "Example Structured Products Inc",
                               "0001234567", "2025-06-10", "0001234567-25-000042")))
check("the history carries NO filing text", _SECRET not in _hist)

# -- audit: one line per estimate, plan, edit, approval ----------------------
_st, _ = _gr_req("POST", "/estimate", {"provider": "claude", "message": "audit?",
                                       "chat_id": "aud"})
_eid2 = _["estimate_id"]
_st, _b = _gr_req("POST", "/plan", {"provider": "claude", "message": "audit?",
                                    "chat_id": "aud", "estimate_id": _eid2})
_aud_batch = _b["batch_id"]
_st, _ = _gr_req("PUT", "/batch/%s" % _aud_batch,
                 {"searches": [{"q": "edited", "forms": ["10-K"], "ciks": [],
                                "startdt": "", "enddt": "", "rationale": "r"}]})
_st, _ = _gr_req("POST", "/batch/%s/approve" % _aud_batch, {})
_events = [r["event"] for r in _audit_lines() if r["chat_id"] == "aud"]
check("the audit log holds exactly one line per decision, in order",
      _events == ["estimate", "plan", "edit", "approve"])
_approve_rec = next(r for r in _audit_lines()
                    if r["chat_id"] == "aud" and r["event"] == "approve")
check("the approval line records batch_id, provider and the searches as approved",
      _approve_rec["batch_id"] == _aud_batch
      and _approve_rec["provider"] == "claude"
      and _approve_rec["searches"][0]["q"] == "edited")

# -- persistence: a batch survives a server restart --------------------------
_restarted = server.ResearchBatchStore(_gr_root)
_reloaded = _restarted.get(_aud_batch)
check("a batch created before a restart is returned after it, with its status",
      _reloaded is not None and _reloaded["status"] == "ran"
      and _reloaded["searches"][0]["q"] == "edited")

_gr_httpd.shutdown()
server.edgar_search = _gr_prev_search
server.build_research_provider = _gr_prev_build
server.RESEARCH = _gr_prev_research

# --------------------------------------------------------------------------- #
section("research assistant -- extraction follow-up proposals (#292)")
# --------------------------------------------------------------------------- #
# After a batch runs, the planner may propose extracting specific filings. That
# proposal is stored on the same batch but has its OWN approval -- nothing scrubs
# until /extract/{id}/approve -- because extraction spends GPU time on the spec
# path. Exercised with a fake provider (no model), a stub search (no network) and
# a stub start_scrubber_run that captures its params and mimics the run's own
# refusal of a form no scrubber owns, so no SEC request or GPU time is spent.

_ex_root = Path(tempfile.mkdtemp(prefix="research-extract-"))
_ACC = "0001234567-25-000042"


class _ExProvider:
    def __init__(self):
        self.payload = {"reply": "", "searches": [], "extract": None}
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        return json.dumps(self.payload)


def _ex_search(params):
    return {"returned": 1, "total": 1, "hits": [{
        "accession": _ACC, "form": "424B2",
        "issuer": "Example Structured Products Inc", "cik": "0001234567",
        "file_date": "2025-06-10", "document": "d424b2.htm"}]}


_captured_runs = []


def _ex_start_run(params):
    # Capture what a run would get, and refuse an unowned form exactly as the
    # real start_scrubber_run does -- without touching the network or a model.
    _captured_runs.append(json.loads(json.dumps(params)))
    forms = params.get("forms") or []
    if server.scrubber_for_forms(forms) is None and forms:
        raise ValueError("no scrubber extracts %s"
                         % ", ".join(str(f) for f in forms))
    return "edgar-scrubber-777"


_ex_prev_search = server.edgar_search
_ex_prev_build = server.build_research_provider
_ex_prev_research = server.RESEARCH
_ex_prev_start = server.start_scrubber_run
_ex_provider = _ExProvider()
server.edgar_search = _ex_search
server.build_research_provider = lambda name, chat_id=None: _ex_provider
server.RESEARCH = server.ResearchBatchStore(_ex_root)
server.start_scrubber_run = _ex_start_run

_ex_httpd = _THS(("127.0.0.1", 0), _partial(server.Handler, directory=str(server.REPO_ROOT)))
threading.Thread(target=_ex_httpd.serve_forever, daemon=True).start()
_ex_base = "http://127.0.0.1:%d" % _ex_httpd.server_address[1]


def _ex_req(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _ex_base + "/api/tools/research" + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _ex_audit():
    p = _ex_root / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# -- seed: run a search batch so the chat has a result an extract can name ----
_ex_provider.payload = {"reply": "search", "searches": [
    {"q": "contingent coupon", "forms": ["424B2"], "ciks": [], "startdt": "",
     "enddt": "", "rationale": "r"}], "extract": None}
_st, _b = _ex_req("POST", "/plan", {"provider": "ollama", "message": "find them",
                                    "chat_id": "x1"})
_seed_batch = _b["batch_id"]
_st, _ = _ex_req("POST", "/batch/%s/approve" % _seed_batch, {})
check("extract: the seeding search batch ran", _st == 200)

# -- a plan with an extract proposal is stored, and starts no run ------------
_ex_provider.payload = {"reply": "extract it", "searches": [
    {"q": "ignored", "forms": ["424B2"], "ciks": [], "startdt": "", "enddt": "",
     "rationale": "r"}], "extract": {"form": "424B2", "accessions": [_ACC],
                                     "rationale": "read in full"}}
_runs_before = len(_captured_runs)
_st, _b = _ex_req("POST", "/plan", {"provider": "ollama", "message": "pull it",
                                    "chat_id": "x1"})
_ex_batch = _b["batch_id"]
check("extract: a fake-provider plan with an extract proposal is stored",
      _st == 200 and _b.get("extract") and _b["extract"]["accessions"] == [_ACC])
check("extract: the proposal carries the extractor kind and the spec estimate",
      _b["extract"]["kind"] == "spec" and _b["extract"]["estimate_seconds"] > 0
      and _b["extract"]["fields"] > 0)
check("extract: storing the plan starts NO run", len(_captured_runs) == _runs_before)

# -- approve: starts exactly one run over exactly that accession -------------
_st, _b = _ex_req("POST", "/extract/%s/approve" % _ex_batch, {})
check("extract: approve starts a run and returns its run_id",
      _st == 200 and _b.get("run_id") == "edgar-scrubber-777")
check("extract: the run got exactly the proposed accession",
      _captured_runs and [t["accession"] for t in _captured_runs[-1]["accessions"]] == [_ACC])
check("extract: the run carries the CIK resolved from the chat's results",
      _captured_runs[-1]["accessions"][0]["cik"] == "0001234567")
check("extract: the run's form is the proposed form",
      _captured_runs[-1]["forms"] == ["424B2"])

_st, _ = _ex_req("POST", "/extract/%s/approve" % _ex_batch, {})
check("extract: a second approve of the same proposal is a 409", _st == 409)

# -- the approval is on the audit log ----------------------------------------
_ex_events = [r for r in _ex_audit() if r.get("event") == "extract-approve"]
check("extract: the approval is written to audit.jsonl",
      any(r["batch_id"] == _ex_batch and r["accessions"] == [_ACC]
          and r["run_id"] == "edgar-scrubber-777" for r in _ex_events))

# -- a form no scrubber owns is refused with the run's own 400 ---------------
_ex_provider.payload = {"reply": "extract", "searches": [
    {"q": "ignored", "forms": ["424B2"], "ciks": [], "startdt": "", "enddt": "",
     "rationale": "r"}], "extract": {"form": "S-1", "accessions": [_ACC],
                                     "rationale": "r"}}
_st, _b = _ex_req("POST", "/plan", {"provider": "ollama", "message": "pull s1",
                                    "chat_id": "x1"})
_s1_batch = _b["batch_id"]
check("extract: a proposal for an unowned form stores with kind null",
      _st == 200 and _b["extract"]["kind"] is None)
_st, _b = _ex_req("POST", "/extract/%s/approve" % _s1_batch, {})
check("extract: approving an unowned-form extraction is a 400 from the run",
      _st == 400 and "no scrubber extracts S-1" in _b.get("error", ""))
_st, _b = _ex_req("GET", "/batch/%s" % _s1_batch, None)
check("extract: a refused approval leaves the proposal approvable again",
      _st == 200 and (_b.get("extract") or {}).get("status") == "proposed")

# -- a plan naming an accession not in the results drops the extract ---------
_ex_provider.payload = {"reply": "extract", "searches": [
    {"q": "ignored", "forms": ["424B2"], "ciks": [], "startdt": "", "enddt": "",
     "rationale": "r"}], "extract": {"form": "424B2",
                                     "accessions": ["9999999999-99-999999"],
                                     "rationale": "r"}}
_st, _b = _ex_req("POST", "/plan", {"provider": "ollama", "message": "pull ghost",
                                    "chat_id": "x1"})
check("extract: an accession not in the chat's results is dropped with a reason",
      _st == 200 and not _b.get("extract")
      and any("did not appear" in d for d in _b.get("dropped", [])))

_ex_httpd.shutdown()
server.edgar_search = _ex_prev_search
server.build_research_provider = _ex_prev_build
server.RESEARCH = _ex_prev_research
server.start_scrubber_run = _ex_prev_start

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ntool-runner server gate: PASS")
