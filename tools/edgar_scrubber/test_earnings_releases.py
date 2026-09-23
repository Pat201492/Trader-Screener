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


def index_url(accession):
    return (f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
            f"{accession.replace('-', '')}/index.json")


def manifest(*names):
    # `type` is the directory-icon filename on every live manifest, so EX-99
    # must be recognised from the NAME -- the fixtures reflect that.
    return {"directory": {"item": [
        {"name": n, "type": "text.gif", "size": "10000"} for n in names
    ]}}


ROUTES = {
    SUB_URL: ok_json_bytes(FIXTURE.read_bytes()),
    # A: has EX-99.1 and EX-99.2         -> release is EX-99.1
    index_url("0000012345-26-000001"): ok_json(manifest("d001_8k.htm", "ex99-1.htm", "ex99-2.htm")),
    # B: no EX-99.1, has .3 and .2       -> release is the lowest, EX-99.2
    index_url("0000012345-26-000002"): ok_json(manifest("d002_8k.htm", "ex99-3.htm", "ex99-2.htm")),
    # C: no EX-99 exhibit at all         -> no release document
    index_url("0000012345-26-000003"): ok_json(manifest("d003_8k.htm", "ex10-1.htm")),
    # D: items missing                   -> flagged, still carries its EX-99.1
    index_url("0000012345-26-000004"): ok_json(manifest("d004_8k.htm", "ex99-1.htm")),
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
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nearnings_releases gate: PASS")
