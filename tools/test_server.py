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

listed = reg.list()
check("list() omits the heavy per-run fields",
      len(listed) == 1 and "log" not in listed[0] and "documents" not in listed[0])

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
    provenance="local:qwen2.5:7b", confidence=0.9, flags=[]))
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
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ntool-runner server gate: PASS")
