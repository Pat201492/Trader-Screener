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
section("LIVE-shaped manifest: index.json `type` is an icon, not a form type")
# --------------------------------------------------------------------------- #

# Verbatim shape of the real endpoint for accession 0001013762-25-000407
# (JPMorgan 424B2, fetched 2026-08-13). Every text item reports "text.gif" --
# the directory icon. Nothing here says "424B2" anywhere, which is why the
# fabricated INDEX_JSON above passed while every live filing resolved no
# primary document at all.
LIVE_INDEX_JSON = {
    "directory": {
        "item": [
            {"name": f"{ACCESSION}-index-headers.html", "type": "text.gif", "size": ""},
            {"name": f"{ACCESSION}-index.html", "type": "text.gif", "size": ""},
            {"name": f"{ACCESSION}.txt", "type": "text.gif", "size": ""},
            {"name": "ea0234769-01_424b2.htm", "type": "text.gif", "size": 155803},
            {"name": "image_001.jpg", "type": "image2.gif", "size": 5462},
        ]
    }
}

check("live-shaped item classifies as 'other' on type alone",
      de.classify_document("ea0234769-01_424b2.htm", "text.gif") == "other")
check("size heuristic picks the rendered prospectus over the index pages",
      de.primary_by_size(LIVE_INDEX_JSON["directory"]["item"]) == "ea0234769-01_424b2.htm")

LIVE_ROUTES = {
    f"{BASE}/index.json": ok_json(LIVE_INDEX_JSON),
    f"{BASE}/{ACCESSION}-index-headers.html": ok_html("<html><body>headers</body></html>"),
    f"{BASE}/{ACCESSION}-index.html": ok_html("<html><body>index</body></html>"),
    f"{BASE}/{ACCESSION}.txt": (200, {}, b"full submission text"),
    f"{BASE}/ea0234769-01_424b2.htm": ok_html(PRIMARY_HTML),
}

import tempfile as _tempfile
live_client = EdgarClient(UA, _tempfile.mkdtemp(prefix="edgar-live-shape-test-"),
                          transport=FakeTransport(LIVE_ROUTES))
live_bundle = de.expand_accession(live_client, CIK, ACCESSION)
live_primary = live_bundle.primary()

check("live-shaped manifest still resolves a primary document",
      live_primary is not None)
check("the primary is the prospectus, not an index page or the .txt submission",
      live_primary is not None and live_primary.name == "ea0234769-01_424b2.htm")
check("the primary carries normalized text",
      live_primary is not None and live_primary.normalized is not None
      and "Contingent Coupon Rate" in live_primary.normalized.text)
check("exactly one document is tagged primary",
      sum(1 for d in live_bundle.documents if d.category == "primary") == 1)

# The manifest's own type still wins where it exists -- the fallback must not
# start overriding a correctly-typed manifest.
typed_client = EdgarClient(UA, _tempfile.mkdtemp(prefix="edgar-typed-shape-test-"),
                           transport=FakeTransport(ROUTES))
typed_primary_doc = de.expand_accession(typed_client, CIK, ACCESSION).primary()
check("a typed manifest still resolves its declared primary",
      typed_primary_doc is not None and typed_primary_doc.name == "primary424b2.htm")

section("EX-107 uses the SEC's REAL element names, not a spelled-out invention")

# Every fixture below is copied from a live exhibit. The bug this section
# guards against passed a green self-check for months because that check
# invented element names (`ffd:AggregateOfferingAmount`) the SEC does not tag,
# so `aggregate_principal` was None on 100% of real filings and
# `field_spec._check_cross` skipped the cross-check as "exhibit absent".

# Citigroup 0000950103-26-013335: a Rule 457(n) guarantee row tags the max
# aggregate offering price as ZERO, with the real size in TtlOfferingAmt.
CITI_EX107 = """
<ix:nonFraction name="ffd:FeeRate" contextRef="c1" unitRef="pure" decimals="7">0.0001381</ix:nonFraction>
<ix:nonFraction name="ffd:MaxAggtOfferingPric" contextRef="c1" unitRef="usd" decimals="0">0</ix:nonFraction>
<ix:nonFraction name="ffd:FeeAmt" contextRef="c1" unitRef="usd" decimals="2">0</ix:nonFraction>
<ix:nonFraction name="ffd:TtlOfferingAmt" contextRef="c1" unitRef="usd" decimals="0">1,650,000</ix:nonFraction>
<ix:nonFraction name="ffd:TtlFeeAmt" contextRef="c1" unitRef="usd" decimals="2">227.87</ix:nonFraction>
"""
citi = de.parse_ex107_xbrl(CITI_EX107, "citi.htm")
check("abbreviated ffd: names resolve at all (TtlFeeAmt -> total_fee_amount)",
      citi.total_fee_amount == 227.87)
check("a 457(n) zero never wins over the row carrying the real offering size",
      citi.aggregate_principal == 1650000.0)

# Wells Fargo 0001839882-26-042967 tags ONLY the narrative price.
WFC_EX107 = """
<ix:nonFraction name="ffd:NrrtvMaxAggtOfferingPric" contextRef="c1" unitRef="usd" decimals="-3">1,000,000</ix:nonFraction>
"""
check("the narrative max aggregate price is enough on its own",
      de.parse_ex107_xbrl(WFC_EX107, "wfc.htm").aggregate_principal == 1000000.0)

# Goldman 0001193125-26-378405 nests the number INSIDE the narrative fact.
GS_EX107 = """
<ix:nonNumeric name="ffd:NrrtvDsclsr" contextRef="c1">The maximum aggregate offering price is
  $<ix:nonFraction name="ffd:NrrtvMaxAggtOfferingPric" contextRef="c1" unitRef="U_USD"
    scale="0" decimals="-3" format="ixt:num-dot-decimal">5,758,000</ix:nonFraction>.
</ix:nonNumeric>
"""
gs = de.parse_ex107_xbrl(GS_EX107, "gs.htm")
check("a fact nested inside a narrative fact is not swallowed as text",
      gs.aggregate_principal == 5758000.0)
check("the enclosing narrative fact is still captured too",
      "nrrtvdsclsr" in gs.raw)

# Semantics that must NOT be papered over to make a field populate.
NETFEE_EX107 = """
<ix:nonFraction name="ffd:NetFeeAmt" contextRef="c1" unitRef="usd" decimals="2">227.87</ix:nonFraction>
"""
check("a net FEE is never reported as a net OFFERING amount",
      de.parse_ex107_xbrl(NETFEE_EX107, "x.htm").net_offering_amount is None)

ZERO_ONLY = """
<ix:nonFraction name="ffd:AmtSctiesRegd" contextRef="c1" unitRef="usd" decimals="0">0</ix:nonFraction>
"""
check("a genuinely-tagged zero is reported as 0.0, not dropped to None",
      de.parse_ex107_xbrl(ZERO_ONLY, "x.htm").amount_registered == 0.0)

LEGACY = """
<ix:nonFraction name="ffd:AggregateOfferingAmount" contextRef="c1" unitRef="usd" decimals="0">2500000</ix:nonFraction>
"""
check("spelled-out legacy aliases still resolve",
      de.parse_ex107_xbrl(LEGACY, "x.htm").aggregate_principal == 2500000.0)


# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ndocument_expand self-check + gate: PASS")
