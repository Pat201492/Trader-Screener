"""
Accession document expansion + EX-107 inline XBRL parse (issue #101, part of
#95, built on #99's `edgar_client`).

"Open EVERY document." A full-text search hit is a FILING, and a filing is a
bundle: the primary 424B2 prospectus is one item in `index.json`'s
`directory.item[]`, alongside exhibits, graphics, and R-files. Extracting only
the primary document loses whatever terms live in the exhibits -- so
`expand_accession` fetches and normalizes every item in the manifest, not
just the one `EdgarClient.primary_document()` would guess at.

Special case: EX-FILING FEES (EX-107). Since 2022 the fee exhibit is
inline-XBRL tagged with the SEC's `ffd:` (filing-fee-data) namespace, handing
over the aggregate offering price and registration fee ALREADY STRUCTURED --
no model tokens spent, and exact ground truth for one field. It is also a
free cross-check: `field_spec`'s `external_equals` cross-check (see
`424b2_structured_note.json`'s `aggregate_principal` rule) compares the
model's prose read against this exhibit, and a mismatch is a high-signal flag
that extraction went wrong on that filing (#104).

stdlib only. Run the self-check:
    python tools/edgar_scrubber/document_expand.py
"""
import re
from dataclasses import dataclass, field

try:  # package import: tools.edgar_scrubber.document_expand
    from .normalize import normalize_html, NormalizedDocument
except ImportError:  # standalone: python tools/edgar_scrubber/document_expand.py
    from normalize import normalize_html, NormalizedDocument

# --------------------------------------------------------------------------- #
# Document classification
# --------------------------------------------------------------------------- #

# `type` values (from index.json) that mark the fee exhibit. Both the
# pre-2022 numbered exhibit and the post-2022 SEC-assigned type string are
# matched -- the rule changed the type label, not the accession structure.
_EX107_TYPE_RE = re.compile(r"ex-?\s*107\b|ex[-\s]?filing\s*fees", re.I)
_EX107_NAME_RE = re.compile(r"ex-?107|filingfee|ex107", re.I)

_GRAPHIC_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff")
_XML_EXTS = (".xml", ".xsd")
_HTML_EXTS = (".htm", ".html")

# Primary-document form types this expander recognizes without guessing from
# size (#99's `primary_document` heuristic is the fallback for anything not
# in the manifest's own `type`, e.g. an old full-submission .txt).
_PRIMARY_FORM_TYPES = {
    "424B2", "424B3", "424B4", "424B5", "424B7", "424B8",
}


def classify_document(name, doc_type, size=None):
    """Categorize one `index.json` item. Returns one of:
    "primary" | "ex107" | "exhibit" | "graphic" | "xml" | "xbrl_data" | "other"

    Classification never causes a document to be skipped -- it only tags
    HOW `expand_accession` processes it (normalize as HTML, parse as XBRL,
    or just record the manifest metadata). Every item in the manifest still
    yields a record either way (the #101 acceptance: "every document ...
    with type, size, normalized text").
    """
    name_low = (name or "").lower()
    type_up = (doc_type or "").upper().strip()

    if _EX107_TYPE_RE.search(type_up) or _EX107_NAME_RE.search(name_low):
        return "ex107"
    if type_up in _PRIMARY_FORM_TYPES:
        return "primary"
    if type_up == "GRAPHIC" or name_low.endswith(_GRAPHIC_EXTS):
        return "graphic"
    if "XBRL" in type_up and not name_low.endswith(_HTML_EXTS):
        return "xbrl_data"
    if name_low.endswith(_XML_EXTS):
        return "xml"
    if type_up.startswith("EX-") or re.match(r"^ex\d", name_low):
        return "exhibit"
    return "other"


def primary_by_size(items):
    """Fallback primary-document pick for a manifest whose `type` carries no
    form label.

    The live `index.json` does NOT report a form type per item -- measured
    against the real endpoint, `directory.item[].type` is the *directory-icon*
    filename, the same value for every text item in the folder:

        {"name": "ea0234769-01_424b2.htm", "type": "text.gif", "size": 155803}
        {"name": "image_001.jpg",          "type": "image2.gif", "size": 5462}

    So `classify_document` can never see "424B2" there and would tag every
    prose item "other", leaving `AccessionDocuments.primary()` None on every
    real filing. Only the fabricated manifests in the tests carry
    `type: "424B2"`.

    Same heuristic as `EdgarClient.primary_document` (#99) -- deliberately
    duplicated rather than imported, because this module is stdlib-only and
    does not depend on the network client.
    """
    def size(it):
        try:
            return int(it.get("size") or 0)
        except (TypeError, ValueError):
            return 0

    def pick(pred):
        cands = [it for it in items if pred((it.get("name") or "").lower())]
        return max(cands, key=size)["name"] if cands else None

    return (
        pick(lambda n: n.endswith(_HTML_EXTS) and not n.startswith(("0001", "index")))
        or pick(lambda n: n.endswith(_HTML_EXTS))
        or pick(lambda n: n.endswith(".txt"))
    )


def is_normalizable(name, category):
    """Whether this item is text/HTML worth running through `normalize_html`
    (as opposed to a graphic or a raw XBRL/XSD sidecar that has no prose)."""
    if category in ("graphic", "xml", "xbrl_data"):
        return False
    name_low = (name or "").lower()
    return name_low.endswith(_HTML_EXTS) or name_low.endswith(".txt")


# --------------------------------------------------------------------------- #
# Expanded document + accession bundle
# --------------------------------------------------------------------------- #

@dataclass
class ExpandedDocument:
    """One item out of an accession's manifest, fetched and (when it is
    prose) normalized. `normalized` is None for graphics/XBRL sidecars --
    the manifest metadata (name/type/size) is still recorded for them."""

    name: str
    doc_type: str
    size: int
    category: str                       # classify_document()'s result
    normalized: NormalizedDocument = None
    raw_bytes: bytes = field(default=None, repr=False)

    def as_manifest_dict(self):
        return {
            "name": self.name, "type": self.doc_type, "size": self.size,
            "category": self.category,
            "text_length": len(self.normalized.text) if self.normalized else None,
            "table_count": len(self.normalized.tables) if self.normalized else None,
        }


@dataclass
class AccessionDocuments:
    """Every document in one accession, expanded. `ex107` is the parsed fee
    exhibit facts (None if the accession predates the 2022 rule or has no
    fee exhibit -- absence is a fact worth carrying, not an error)."""

    cik: str
    accession: str
    documents: list = field(default_factory=list)   # [ExpandedDocument, ...]
    ex107: "Ex107Facts" = None

    def manifest(self):
        return [d.as_manifest_dict() for d in self.documents]

    def primary(self):
        for d in self.documents:
            if d.category == "primary":
                return d
        return None

    def by_name(self, name):
        for d in self.documents:
            if d.name == name:
                return d
        return None


def expand_accession(client, cik, accession, *, fetch_graphics=False):
    """Fetch `index.json` for `(cik, accession)` and expand EVERY item in its
    manifest -- not just the primary document. Prose/HTML items are run
    through `normalize_html`; the EX-107 fee exhibit (if present) is also
    parsed for its inline-XBRL `ffd:` facts. Graphics are skipped by default
    (`fetch_graphics=True` to include them) since they carry no extractable
    text and would otherwise burn the 10 req/s ceiling for nothing.
    """
    index_json = client.filing_index(cik, accession)
    items = ((index_json.get("directory", {}) or {}).get("item", []) or [])

    # A manifest whose `type` never says "424B2" (which is every LIVE one --
    # see `primary_by_size`) gets the size heuristic instead, so the bundle
    # always resolves a primary. The manifest's own type wins when it has one.
    typed_primary = any(
        classify_document(it.get("name", ""), it.get("type", "")) == "primary"
        for it in items
    )
    fallback_primary = None if typed_primary else primary_by_size(items)

    documents = []
    ex107 = None
    for item in items:
        name = item.get("name", "")
        doc_type = item.get("type", "")
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        category = classify_document(name, doc_type, size)
        if fallback_primary and name == fallback_primary and category == "other":
            category = "primary"

        if category == "graphic" and not fetch_graphics:
            documents.append(ExpandedDocument(name, doc_type, size, category))
            continue

        raw = client.archive_document(cik, accession, name)
        normalized = None
        if is_normalizable(name, category):
            normalized = normalize_html(raw)

        doc = ExpandedDocument(name, doc_type, size, category,
                                normalized=normalized, raw_bytes=raw)
        documents.append(doc)

        if category == "ex107" and normalized is not None:
            ex107 = parse_ex107_xbrl(normalized.source)

    return AccessionDocuments(cik=cik, accession=accession, documents=documents, ex107=ex107)


# --------------------------------------------------------------------------- #
# EX-107 inline XBRL (`ffd:` namespace) -> structured fields
# --------------------------------------------------------------------------- #

# ix:nonFraction / ix:nonNumeric tags, name="ffd:Something" (namespace prefix
# may not literally be "ffd" -- inline XBRL lets a filer bind any prefix to
# the filing-fee-data namespace URI, so we match on the ELEMENT localname,
# not the prefix). Attribute order in the wild is arbitrary, so attributes
# are parsed generically rather than assumed positional.
_IX_FACT_RE = re.compile(
    r"<ix:(nonfraction|nonnumeric)\b([^>]*)>(.*?)</ix:\1>",
    re.I | re.S,
)
_ATTR_RE = re.compile(r"([a-zA-Z_:][\w:.\-]*)\s*=\s*\"([^\"]*)\"")
_TAG_STRIP_RE = re.compile(r"<[^>]+>")

# Known `ffd:` local names -> canonical field name. Best-effort: the taxonomy
# has evolved release to release, so this is intentionally a loose match
# (case-insensitive, ignores the namespace prefix) rather than an exhaustive
# enum -- every fact is ALSO kept verbatim in `Ex107Facts.raw`, so a tag this
# map doesn't recognize is never silently dropped, just not promoted to a
# canonical field.
# The SEC's filing-fee taxonomy tags ABBREVIATED element names
# (`ffd:TtlOfferingAmt`, `ffd:AmtSctiesRegd`), not the spelled-out ones. Only
# `FeeRate` happens to be spelled in full, which is why it was the single
# canonical field that ever populated from a real exhibit -- everything else
# silently stayed None and `external_equals` skipped the cross-check as
# "exhibit absent" (`field_spec._check_cross`). Spelled-out names are kept as
# trailing aliases so a hand-written or pre-taxonomy exhibit still resolves.
#
# Ordered by preference, most specific first.
_FFD_ALIASES = {
    "aggregate_principal": (
        "ttlofferingamt", "nrrtvmaxaggtofferingpric", "maxaggtofferingpric",
        "aggregateofferingamount", "totalofferingamount",
        "proposedmaxaggregateofferingprice", "maximumaggregateofferingprice",
    ),
    "amount_registered": ("amtsctiesregd", "amountregistered"),
    "total_fee_amount": ("ttlfeeamt", "feeamt", "totalfeeamount", "feeamount"),
    # `ffd:NetFeeAmt` is a net FEE, not a net offering amount -- mapping it here
    # would put a ~$228 figure where a ~$1.65M one belongs, so it is left out
    # and this field stays None until an exhibit tags the amount itself.
    "net_offering_amount": ("ttlofferingamtnet", "totalofferingamountnet"),
    "fee_rate": ("feerate",),
}


def _resolve_canonical(facts):
    """Fill the canonical attributes from `raw`, preferring the first alias
    that carries a NONZERO number.

    Order matters and zero is not a value here. A 424B2 shelf takedown filed
    under Rule 457(r)/457(n) tags `MaxAggtOfferingPric` as 0.0 on the guarantee
    row -- no separate fee is payable -- while the offering's real size sits in
    `TtlOfferingAmt`. Taking the first alias present would therefore report an
    aggregate principal of $0 on exactly the filings this cross-check exists
    to verify. A zero is still used when it is genuinely all the exhibit
    tagged, so "tagged zero" stays distinguishable from "not tagged".
    """
    for canon, names in _FFD_ALIASES.items():
        if getattr(facts, canon, None) is not None:
            continue
        nums = [facts.raw[n] for n in names
                if isinstance(facts.raw.get(n), (int, float))
                and not isinstance(facts.raw.get(n), bool)]
        value = next((n for n in nums if n), None)
        if value is None:
            value = nums[0] if nums else None
        if value is not None:
            setattr(facts, canon, float(value))


@dataclass
class Ex107Facts:
    """Structured facts out of one EX-107 fee exhibit. `raw` is every parsed
    `ffd:` fact keyed by its element localname (lowercased) -> numeric or
    string value, exactly as tagged -- nothing here is dropped even when it
    doesn't map to a canonical field. The named attributes
    (`aggregate_principal` etc.) are the ones `field_spec`'s cross_checks
    reference by name (see `external_equals`, source="ex107")."""

    document: str = None
    raw: dict = field(default_factory=dict)
    aggregate_principal: float = None
    amount_registered: float = None
    total_fee_amount: float = None
    net_offering_amount: float = None
    fee_rate: float = None

    def as_dict(self):
        return {
            "document": self.document,
            "aggregate_principal": self.aggregate_principal,
            "amount_registered": self.amount_registered,
            "total_fee_amount": self.total_fee_amount,
            "net_offering_amount": self.net_offering_amount,
            "fee_rate": self.fee_rate,
            "raw": self.raw,
        }


def _parse_attrs(attr_text):
    return {m.group(1).lower(): m.group(2) for m in _ATTR_RE.finditer(attr_text)}


def _local_name(name_attr):
    """Strip an XBRL `prefix:LocalName` down to the localname, lowercased."""
    if not name_attr:
        return ""
    return name_attr.split(":", 1)[-1].strip().lower()


def _numeric_value(raw_text, attrs):
    """Apply inline-XBRL `scale`/`sign` transforms to a nonFraction's raw
    display text and return a float, or None if it isn't numeric."""
    text = _TAG_STRIP_RE.sub("", raw_text).strip()
    text = text.replace(",", "").replace("$", "").strip()
    if not text or text in ("-", "—"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    scale = attrs.get("scale")
    if scale is not None:
        try:
            value *= 10 ** int(scale)
        except ValueError:
            pass
    if attrs.get("sign") == "-":
        value = -value
    return value


def _iter_ix_facts(text):
    """Yield `(kind, attr_text, inner)` for every inline-XBRL fact, including
    facts NESTED inside another one.

    A narrative fact routinely wraps the tagged number it describes: Goldman
    Sachs files the offering size as an `ffd:NrrtvMaxAggtOfferingPric`
    nonFraction sitting inside the `ffd:NrrtvDsclsr` sentence. Treating an
    outer fact's content as opaque text therefore dropped the only number the
    exhibit carried -- the exhibit parsed "successfully" with ten facts and no
    amount at all.

    Same-kind nesting (a nonFraction inside a nonFraction) is not untangled
    here: the non-greedy close in `_IX_FACT_RE` binds the first `</ix:...>`.
    No exhibit observed does that, and it is preferable to a nesting-aware
    parser's cost for a stdlib-only module.
    """
    for m in _IX_FACT_RE.finditer(text):
        inner = m.group(3)
        yield m.group(1).lower(), m.group(2), inner
        if "<ix:" in inner.lower():
            for nested in _iter_ix_facts(inner):
                yield nested


def parse_ex107_xbrl(html_text, document_name=None):
    """Scan an EX-107 document's inline-XBRL facts for the `ffd:` namespace
    and return an `Ex107Facts`. Returns an `Ex107Facts` with an empty `raw`
    (all fields None) rather than `None` when no facts are found -- absence
    is itself informative (pre-2022 filing, or a fee exhibit that wasn't
    XBRL-tagged) and the caller should not have to distinguish "not parsed"
    from "parsed, nothing there."
    """
    facts = Ex107Facts(document=document_name)
    for kind, attr_text, inner in _iter_ix_facts(html_text):
        attrs = _parse_attrs(attr_text)
        local = _local_name(attrs.get("name"))
        if not local:
            continue

        if kind == "nonfraction":
            value = _numeric_value(inner, attrs)
        else:
            value = _TAG_STRIP_RE.sub("", inner).strip()
            value = value or None

        if value is None:
            continue
        facts.raw[local] = value

    _resolve_canonical(facts)
    return facts


if __name__ == "__main__":
    # Element names here are the ones the SEC ACTUALLY tags, copied from a live
    # exhibit (Citigroup 0000950103-26-013335). An invented spelled-out
    # vocabulary in this self-check is what let the name-map bug pass green
    # while every real filing resolved to None -- so this sample also keeps the
    # 457(n) shape that broke it: `MaxAggtOfferingPric` tagged 0.0 alongside
    # the real size in `TtlOfferingAmt`.
    sample_ex107 = """
    <html><body>
    <table>
    <tr><td>Security Type</td><td>Fee Rate</td><td>Amount Registered</td>
        <td>Maximum Aggregate Offering Price</td><td>Fee Amount</td></tr>
    <tr>
      <td>Other</td>
      <td><ix:nonFraction name="ffd:FeeRate" contextRef="c1" unitRef="pure" decimals="7">0.0001381</ix:nonFraction></td>
      <td><ix:nonFraction name="ffd:AmtSctiesRegd" contextRef="c1" unitRef="usd" decimals="0">0</ix:nonFraction></td>
      <td><ix:nonFraction name="ffd:MaxAggtOfferingPric" contextRef="c1" unitRef="usd" decimals="0" scale="0">0</ix:nonFraction></td>
      <td><ix:nonFraction name="ffd:FeeAmt" contextRef="c1" unitRef="usd" decimals="2">0</ix:nonFraction></td>
    </tr>
    <tr>
      <td><ix:nonFraction name="ffd:TtlOfferingAmt" contextRef="c1" unitRef="usd" decimals="0">1650000</ix:nonFraction></td>
      <td><ix:nonFraction name="ffd:TtlFeeAmt" contextRef="c1" unitRef="usd" decimals="2">227.87</ix:nonFraction></td>
      <td><ix:nonFraction name="ffd:NetFeeAmt" contextRef="c1" unitRef="usd" decimals="2">227.87</ix:nonFraction></td>
    </tr>
    </table>
    <ix:nonNumeric name="ffd:OfferingNote" contextRef="c1">Structured notes offering</ix:nonNumeric>
    </body></html>
    """
    facts = parse_ex107_xbrl(sample_ex107, document_name="ex107.htm")
    print("EX-107 facts:", facts.as_dict())
    # The offering size comes from TtlOfferingAmt, NOT the 0.0 on the 457(n) row.
    assert facts.aggregate_principal == 1650000.0, facts.aggregate_principal
    assert facts.total_fee_amount == 227.87, facts.total_fee_amount
    assert facts.fee_rate == 0.0001381, facts.fee_rate
    # Genuinely tagged zero stays zero -- distinguishable from "not tagged".
    assert facts.amount_registered == 0.0, facts.amount_registered
    # NetFeeAmt is a fee, so it must never land in net_offering_amount.
    assert facts.net_offering_amount is None, facts.net_offering_amount
    assert facts.raw["offeringnote"] == "Structured notes offering"

    # Spelled-out aliases still resolve, for a hand-built or pre-taxonomy exhibit.
    legacy = parse_ex107_xbrl(
        '<ix:nonFraction name="ffd:AggregateOfferingAmount" contextRef="c1" '
        'unitRef="usd" decimals="0">2500000</ix:nonFraction>')
    assert legacy.aggregate_principal == 2500000.0, legacy.aggregate_principal

    print("category(424b2.htm, '424B2') ->", classify_document("424b2.htm", "424B2"))
    print("category(ex107.htm, 'EX-FILING FEES') ->", classify_document("ex107.htm", "EX-FILING FEES"))
    print("category(image1.jpg, 'GRAPHIC') ->", classify_document("image1.jpg", "GRAPHIC"))
    assert classify_document("424b2.htm", "424B2") == "primary"
    assert classify_document("ex107.htm", "EX-FILING FEES") == "ex107"
    assert classify_document("image1.jpg", "GRAPHIC") == "graphic"
    assert classify_document("ex99-1.htm", "EX-99.1") == "exhibit"

    print("\ndocument_expand self-check: PASS")
