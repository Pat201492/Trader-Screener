"""
Gate for accession document expansion + EX-107 inline XBRL (issue #101). Same
convention as `test_edgar_client.py`: stdlib only, no network (a `FakeTransport`
scripts every response), run directly, exit 0 = pass.

Every #101 acceptance criterion this module owns is checked here:

  * one accession yields EVERY document in its manifest (primary + exhibits +
    graphics + XML sidecars), each with type, size, and normalized text where
    applicable -- not just the guessed "primary" document;
  * EX-107 inline XBRL parsed into structured fields where present;
  * document classification (primary/ex107/exhibit/graphic/xml/other).

Run:  python tools/edgar_scrubber/test_document_expand.py
"""
import json

import document_expand as de
from edgar_client import EdgarClient

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


class FakeTransport:
    """Same shape as `test_edgar_client.FakeTransport`: routes a URL to a
    scripted `(status, headers, body)` response, records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"FakeTransport got an unrouted URL: {url}")
        return self.routes[url]


def ok_json(obj):
    return (200, {"content-type": "application/json"}, json.dumps(obj).encode("utf-8"))


def ok_html(html):
    return (200, {"content-type": "text/html"}, html.encode("utf-8"))


UA = "Test Runner ci@example.com"
CIK = "1234567890"
ACCESSION = "0001234567-26-000001"
BASE = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{ACCESSION.replace('-', '')}"

INDEX_JSON = {
    "directory": {
        "item": [
            {"name": "primary424b2.htm", "type": "424B2", "size": "185000"},
            {"name": "ex107.htm", "type": "EX-FILING FEES", "size": "4200"},
            {"name": "ex99-1.htm", "type": "EX-99.1", "size": "9000"},
            {"name": "image1.jpg", "type": "GRAPHIC", "size": "50000"},
            {"name": "R1.htm", "type": "XML", "size": "1200"},
        ]
    }
}

PRIMARY_HTML = "<html><body><p>Contingent Coupon Rate: 9.15% per annum.</p></body></html>"
EX99_HTML = "<html><body><p>Guarantee of JPMorgan Chase & Co.</p></body></html>"
EX107_HTML = """
<html><body><table>
<tr><td>Aggregate Offering</td>
    <td><ix:nonFraction name="ffd:AggregateOfferingAmount" contextRef="c1" unitRef="usd" decimals="0">2500000</ix:nonFraction></td></tr>
<tr><td>Fee</td>
    <td><ix:nonFraction name="ffd:TotalFeeAmount" contextRef="c1" unitRef="usd" decimals="2">275.50</ix:nonFraction></td></tr>
</table></body></html>
"""

ROUTES = {
    f"{BASE}/index.json": ok_json(INDEX_JSON),
    f"{BASE}/primary424b2.htm": ok_html(PRIMARY_HTML),
    f"{BASE}/ex107.htm": ok_html(EX107_HTML),
    f"{BASE}/ex99-1.htm": ok_html(EX99_HTML),
    f"{BASE}/R1.htm": (200, {}, b"<xml>not prose</xml>"),
}


def new_client():
    import tempfile
    cache_dir = tempfile.mkdtemp(prefix="edgar-expand-test-")
    client = EdgarClient(UA, cache_dir, transport=FakeTransport(ROUTES))
    return client, cache_dir


# --------------------------------------------------------------------------- #
section("classify_document")
# --------------------------------------------------------------------------- #

check("primary form type -> primary", de.classify_document("424b2.htm", "424B2") == "primary")
check("EX-FILING FEES -> ex107", de.classify_document("ex107.htm", "EX-FILING FEES") == "ex107")
check("EX-107 (numbered) -> ex107", de.classify_document("ex-107.htm", "EX-107") == "ex107")
check("GRAPHIC -> graphic", de.classify_document("image1.jpg", "GRAPHIC") == "graphic")
check("generic exhibit -> exhibit", de.classify_document("ex99-1.htm", "EX-99.1") == "exhibit")
check("xml sidecar -> xml", de.classify_document("R1.xml", "XML") == "xml")
check("is_normalizable true for .htm", de.is_normalizable("x.htm", "exhibit"))
check("is_normalizable false for graphic", not de.is_normalizable("x.jpg", "graphic"))

# --------------------------------------------------------------------------- #
section("expand_accession -- every document in the manifest")
# --------------------------------------------------------------------------- #

client, cache_dir = new_client()
try:
    bundle = de.expand_accession(client, CIK, ACCESSION)

    check("every manifest item present in the bundle",
          len(bundle.documents) == len(INDEX_JSON["directory"]["item"]))
    names = {d.name for d in bundle.documents}
    check("primary doc present", "primary424b2.htm" in names)
    check("EX-107 present", "ex107.htm" in names)
    check("generic exhibit present", "ex99-1.htm" in names)
    check("graphic recorded in manifest even though not fetched",
          "image1.jpg" in names)

    primary = bundle.primary()
    check("primary() returns the 424B2 doc", primary is not None and primary.name == "primary424b2.htm")
    check("primary doc normalized (has text)",
          primary.normalized is not None and "9.15%" in primary.normalized.text)

    ex99 = bundle.by_name("ex99-1.htm")
    check("exhibit document also normalized",
          ex99.normalized is not None and "JPMorgan Chase" in ex99.normalized.text)

    graphic = bundle.by_name("image1.jpg")
    check("graphic not fetched by default (no raw_bytes)", graphic.raw_bytes is None)
    check("graphic has no normalized text", graphic.normalized is None)
    check("graphic's manifest size still recorded", graphic.size == 50000)

    manifest = bundle.manifest()
    check("manifest() carries type + size for every doc",
          all("type" in m and "size" in m for m in manifest))

    # graphic never touched the network at all (classified before fetch).
    graphic_fetch_urls = [u for u in client._transport.calls if u.endswith("image1.jpg")]
    check("graphic never fetched over the network", graphic_fetch_urls == [])
finally:
    client.close()

# --------------------------------------------------------------------------- #
section("expand_accession -- EX-107 XBRL parsed into structured fields")
# --------------------------------------------------------------------------- #

client, cache_dir = new_client()
try:
    bundle = de.expand_accession(client, CIK, ACCESSION)
    check("ex107 facts parsed", bundle.ex107 is not None)
    check("aggregate_principal extracted", bundle.ex107.aggregate_principal == 2500000.0)
    check("total_fee_amount extracted", bundle.ex107.total_fee_amount == 275.50)
finally:
    client.close()

# --------------------------------------------------------------------------- #
section("parse_ex107_xbrl -- absence is informative, not an error")
# --------------------------------------------------------------------------- #

empty_facts = de.parse_ex107_xbrl("<html><body><p>No XBRL facts here.</p></body></html>")
check("no facts -> Ex107Facts with all-None fields, not an exception",
      empty_facts.aggregate_principal is None and empty_facts.raw == {})

# --------------------------------------------------------------------------- #
section("EX-107 cross-check wires into field_spec (issue #101 <-> #102)")
# --------------------------------------------------------------------------- #

try:
    import field_spec as fs
    specs = fs.load_specs()
    note_spec = specs["structured_note"]
    record = {"aggregate_principal": 2500000.0}
    flags = note_spec.validate_record(record, ex107={"aggregate_principal": 2500000.0})
    check("aggregate_principal matches EX-107 -> no cross_check_failed flag",
          not any(f.code == "cross_check_failed" for f in flags))

    bad_flags = note_spec.validate_record(record, ex107={"aggregate_principal": 9999999.0})
    check("aggregate_principal mismatching EX-107 -> flagged",
          any(f.code == "cross_check_failed" for f in bad_flags))
except ImportError:
    print("  [skip] field_spec not importable standalone here")

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ndocument_expand self-check + gate: PASS")
