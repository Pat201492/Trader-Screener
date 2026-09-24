"""
Gate for earnings-press-release location (issue #185). Same convention as
`test_document_expand.py` / `test_edgar_client.py`: stdlib only, no network (a
`FakeTransport` scripts every response), run directly, exit 0 = pass.

The submissions history is a CHECKED-IN fixture JSON
(`fixtures/earnings_submissions.json`) served through the transport, so the whole
gate runs offline. Every #185 acceptance criterion is checked here:

  * `earnings_release_filings` returns the 8-Ks reporting Item 2.02, each
    carrying accession, filing_date, items, and the exhibit list from the index;
  * `press_release_document` picks EX-99.1, falls back to the lowest-numbered
    EX-99.x, and returns None when there is no EX-99;
  * an 8-K with a missing/empty `items` field comes back FLAGGED, not dropped;
  * a non-8-K, and an 8-K without Item 2.02, are excluded.

Plus the #224 fix -- the EX-99 exhibit is resolved from the accession index
page's authoritative Type column, not from the filename -- checked against
CHECKED-IN real index-page fixtures for NVDA, CSCO and AAPL
(`fixtures/{accession}-index.html`), whose releases (`q2fy27pr.htm`,
`exhibit991pressrelease-q4f.htm`) the filename heuristic misses.

Run:  python tools/edgar_scrubber/test_earnings_releases.py
"""
import json
import tempfile
from pathlib import Path

import earnings_releases as er
from edgar_client import EdgarClient

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


class FakeTransport:
    """Routes a URL to a scripted `(status, headers, body)` response; records
    every call so the test can assert what was and was not fetched."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"FakeTransport got an unrouted URL: {url}")
        return self.routes[url]


def ok_json_bytes(body):
    return (200, {"content-type": "application/json"}, body)


def ok_json(obj):
    return ok_json_bytes(json.dumps(obj).encode("utf-8"))


UA = "Test Runner ci@example.com"
CIK = "12345"

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "earnings_submissions.json"
SUB_URL = "https://data.sec.gov/submissions/CIK0000012345.json"


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def index_url(accession):
    return (f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
            f"{accession.replace('-', '')}/index.json")


def index_page_url(accession):
    return (f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
            f"{accession.replace('-', '')}/{accession}-index.html")


def manifest(*named):
    # `type` here is the directory-icon filename that live index.json carries --
    # deliberately uninformative, so the EX-99 number can ONLY come from the
    # index page's declared Type (see index_page). Each arg is (name, decl_type).
    return {"directory": {"item": [
        {"name": n, "type": "text.gif", "size": "10000"} for n, _ in named
    ]}}


def index_page(*named):
    # A minimal accession index page: one Document-Format-Files row per document,
    # Type declared in the Type column, exactly as EDGAR renders it. Each arg is
    # (name, decl_type).
    rows = "".join(
        f'<tr><td>{i}</td><td>{t}</td>'
        f'<td><a href="/Archives/edgar/data/{CIK}/x/{n}">{n}</a></td>'
        f'<td>{t}</td><td>10000</td></tr>'
        for i, (n, t) in enumerate(named, start=1)
    )
    html = f'<table class="tableFile" summary="Document Format Files">{rows}</table>'
    return (200, {"content-type": "text/html"}, html.encode("utf-8"))


# Each accession's documents as (filename, declared-type) pairs -- the declared
# type is what the index page carries and what now resolves the EX-99 exhibit.
A = [("d001_8k.htm", "8-K"), ("ex99-1.htm", "EX-99.1"), ("ex99-2.htm", "EX-99.2")]
B = [("d002_8k.htm", "8-K"), ("ex99-3.htm", "EX-99.3"), ("ex99-2.htm", "EX-99.2")]
C = [("d003_8k.htm", "8-K"), ("ex10-1.htm", "EX-10.1")]
D = [("d004_8k.htm", "8-K"), ("ex99-1.htm", "EX-99.1")]

ROUTES = {
    SUB_URL: ok_json_bytes(FIXTURE.read_bytes()),
    # A: has EX-99.1 and EX-99.2         -> release is EX-99.1
    index_url("0000012345-26-000001"): ok_json(manifest(*A)),
    index_page_url("0000012345-26-000001"): index_page(*A),
    # B: no EX-99.1, has .3 and .2       -> release is the lowest, EX-99.2
    index_url("0000012345-26-000002"): ok_json(manifest(*B)),
    index_page_url("0000012345-26-000002"): index_page(*B),
    # C: no EX-99 exhibit at all         -> no release document
    index_url("0000012345-26-000003"): ok_json(manifest(*C)),
    index_page_url("0000012345-26-000003"): index_page(*C),
    # D: items missing                   -> flagged, still carries its EX-99.1
    index_url("0000012345-26-000004"): ok_json(manifest(*D)),
    index_page_url("0000012345-26-000004"): index_page(*D),
}


def new_client():
    return EdgarClient(UA, tempfile.mkdtemp(prefix="edgar-earnings-test-"),
                       transport=FakeTransport(ROUTES))


# --------------------------------------------------------------------------- #
section("earnings_release_filings narrows to 8-K Item 2.02")
# --------------------------------------------------------------------------- #

client = new_client()
fils = er.earnings_release_filings(client, CIK)
by_acc = {f["accession"]: f for f in fils}

check("only the 8-K/2.02 filings and the missing-items 8-K come back (4)",
      set(by_acc) == {
          "0000012345-26-000001", "0000012345-26-000002",
          "0000012345-26-000003", "0000012345-26-000004"})
check("the 10-K is excluded (not an 8-K)",
      "0000012345-26-000005" not in by_acc)
check("the 8-K without Item 2.02 is excluded",
      "0000012345-26-000006" not in by_acc)

a = by_acc["0000012345-26-000001"]
check("result carries accession", a["accession"] == "0000012345-26-000001")
check("result carries filing_date", a["filing_date"] == "2026-01-21")
check("result carries parsed items", a["items"] == ["2.02", "9.01"])
check("result carries the exhibit list from the index",
      {e["name"] for e in a["exhibits"]} == {"ex99-1.htm", "ex99-2.htm"})

# --------------------------------------------------------------------------- #
section("a missing/empty items field is flagged, not dropped")
# --------------------------------------------------------------------------- #

d = by_acc["0000012345-26-000004"]
check("the missing-items 8-K is present, not dropped", d is not None)
check("its items list is empty", d["items"] == [])
check("it carries an items_missing flag", d["flags"] == ["items_missing"])
check("a normal filing carries no flags", a["flags"] == [])

# --------------------------------------------------------------------------- #
section("press_release_document selection")
# --------------------------------------------------------------------------- #

check("picks EX-99.1 when present",
      er.press_release_document(by_acc["0000012345-26-000001"])["name"] == "ex99-1.htm")
check("falls back to the lowest-numbered EX-99.x",
      er.press_release_document(by_acc["0000012345-26-000002"])["name"] == "ex99-2.htm")
check("returns None when the filing has no EX-99 exhibit",
      er.press_release_document(by_acc["0000012345-26-000003"]) is None)
check("still resolves the release on the flagged missing-items filing",
      er.press_release_document(by_acc["0000012345-26-000004"])["name"] == "ex99-1.htm")

# --------------------------------------------------------------------------- #
section("ex99_number recognises the filename forms filings print")
# --------------------------------------------------------------------------- #

check("ex99-1.htm -> 1", er.ex99_number("ex99-1.htm") == 1)
check("ex991.htm -> 1", er.ex99_number("ex991.htm") == 1)
check("ex9901.htm -> 1", er.ex99_number("ex9901.htm") == 1)
check("prefixed d12dex99-1.htm -> 1", er.ex99_number("d12dex99-1.htm") == 1)
check("bare ex99.htm -> 0", er.ex99_number("ex99.htm") == 0)
check("ex10-1.htm is not EX-99 (None)", er.ex99_number("ex10-1.htm") is None)
check("EX-99.1 in the manifest type is also recognised",
      er.ex99_number("something.htm", "EX-99.1") == 1)

# --------------------------------------------------------------------------- #
section("offline: only the fixture + the four indexes were fetched")
# --------------------------------------------------------------------------- #

check("no index fetched for the excluded filings",
      not any(("000005" in u or "000006" in u) for u in client._transport.calls))

_src = Path(er.__file__).read_text(encoding="utf-8")
check("module imports no network library of its own",
      not any(tok in _src for tok in
              ("import http", "import urllib", "import requests", "from urllib")))

# --------------------------------------------------------------------------- #
section("#224: EX-99 resolved from the index page's Type column, not the filename")
# --------------------------------------------------------------------------- #

def release_from_fixture(accession, cik):
    """Drive the real committed index page through the same code path a live
    run uses: parse its Type column, build the manifest from the same documents,
    then select the release exhibit."""
    html = (FIXTURES / f"{accession}-index.html").read_text(encoding="utf-8")
    type_map = er.parse_index_page(html)
    index_json = {"directory": {"item": [
        {"name": n, "type": "text.gif", "size": "10000"} for n in type_map
    ]}}
    exhibits = er.exhibits_from_index(index_json, type_map)
    return type_map, {"exhibits": exhibits}


# NVDA -- release filename is q2fy27pr.htm, which the ex99 FILENAME regex misses;
# only the declared type resolves it. It also carries BOTH EX-99.1 and EX-99.2.
nvda_types, nvda = release_from_fixture("0001045810-26-000073", "1045810")
check("NVDA: filename q2fy27pr.htm is NOT matched by the ex99 filename regex",
      er.ex99_number("q2fy27pr.htm") is None)
check("NVDA: index page declares q2fy27pr.htm as EX-99.1",
      nvda_types.get("q2fy27pr.htm") == "EX-99.1")
check("NVDA: index page declares q2fy27cfocommentary.htm as EX-99.2 (not .1)",
      nvda_types.get("q2fy27cfocommentary.htm") == "EX-99.2")
check("NVDA: both EX-99.1 and EX-99.2 are present as exhibits",
      {e["name"] for e in nvda["exhibits"] if e["ex99_number"] is not None}
      == {"q2fy27pr.htm", "q2fy27cfocommentary.htm"})
check("NVDA: EX-99.1 (the press release) is selected over EX-99.2",
      er.press_release_document(nvda)["name"] == "q2fy27pr.htm")

# CSCO -- release filename exhibit991pressrelease-q4f.htm; `ex` not followed by 99.
csco_types, csco = release_from_fixture("0000858877-26-000106", "858877")
check("CSCO: filename exhibit991pressrelease-q4f.htm is NOT matched by the regex",
      er.ex99_number("exhibit991pressrelease-q4f.htm") is None)
check("CSCO: the press release resolves to exhibit991pressrelease-q4f.htm",
      er.press_release_document(csco)["name"] == "exhibit991pressrelease-q4f.htm")

# AAPL -- previously-working case (filename contains ex991); must not regress.
aapl_types, aapl = release_from_fixture("0000320193-26-000018", "320193")
check("AAPL: the previously-working case still resolves (no regression)",
      er.press_release_document(aapl)["name"] == "a8-kex991q3202606272026.htm")

# The per-row pairing guard: a naive page-wide regex would match EX-99.1 against
# both NVDA rows. Confirm each Type is paired with its OWN row's filename only.
check("index-page parse pairs each Type with its own row (no mis-pairing)",
      [nvda_types["q2fy27pr.htm"], nvda_types["q2fy27cfocommentary.htm"]]
      == ["EX-99.1", "EX-99.2"])

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nearnings_releases gate: PASS")
