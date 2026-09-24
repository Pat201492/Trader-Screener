"""
Locate earnings press releases in EDGAR filing metadata (issue #185, EPIC #95).

Company guidance lives in the earnings press release, which the SEC files as an
8-K reporting **Item 2.02** ("Results of Operations and Financial Condition")
with the release attached as an **EX-99.x** exhibit. This module narrows a CIK's
whole filing history down to that handful of documents per year using filing
METADATA ALONE -- the submissions history and the accession index -- so the
haystack is shrunk to the pages that can carry guidance before any model, or even
any document fetch of the release body, runs. It is the cheapest rung: zero
tokens, and it reads only two already-cached JSON endpoints (`submissions` and
`filing_index`, both from the #182 `EdgarClient`).

Two metadata facts this leans on, both measured against the live endpoints:

  1. The 8-K's `items` field in the submissions history is the authoritative
     "what is this 8-K about" list -- a comma-joined string like ``"2.02,9.01"``.
     `2.02` is the earnings-results item. Filtering on it is exact and free.

  2. The accession `index.json` does NOT carry a reliable per-document exhibit
     TYPE -- `directory.item[].type` is the directory-icon filename on every live
     manifest (see `document_expand.primary_by_size`). The AUTHORITATIVE pairing
     lives instead in the accession INDEX PAGE (``{accession}-index.html``): its
     Type column declares each document's exhibit type (``EX-99.1``, ``EX-99.2``
     ...) next to its filename. So an EX-99 exhibit is resolved from that
     declared type, parsed row-by-row so a Type is never mis-paired with another
     row's filename. The FILENAME heuristic (``ex99-1.htm``, ``ex991.htm``,
     ``ex9901.htm``, ``d1234dex991.htm``) is DEMOTED to a fallback, used only for
     a document the index page did not declare a type for -- because the filename
     heuristic misses real releases whose name is nothing like ``ex99`` (measured
     2026-09-24: NVDA ``q2fy27pr.htm``, CSCO ``exhibit991pressrelease-q4f.htm``).

A filing whose `items` is missing or empty is NOT dropped -- it is returned
carrying an ``items_missing`` flag, so a metadata gap is visible rather than
silently swallowing a filing that might be an earnings release.

Only `filings.recent` is read here; the older paginated `filings.files` history
is out of scope for this rung.

stdlib only; the network is the injected `EdgarClient`. Run the self-check:
    python tools/edgar_scrubber/earnings_releases.py
"""
import re

# The earnings-results 8-K item. A filing reporting this item is, by SEC form
# convention, an earnings release.
RESULTS_ITEM = "2.02"

# EX-99 exhibit recognised from a DECLARED exhibit type -- the authoritative
# ``EX-99.1`` from the index page's Type column (or, when populated, the manifest
# type). This is the primary signal.
_EX99_TYPE_RE = re.compile(r"EX-?99(?:\.(\d+))?", re.I)
# FALLBACK ONLY (no declared type): recognise EX-99 from the document FILENAME --
# an `ex` + `99`, an optional separator, then the sub-number. Matches ``ex99-1``,
# ``ex991``, ``ex9901`` (-> .1) and the same embedded in a prefixed name
# (``d12dex99-1``). Misses names unlike ``ex99`` (``q2fy27pr.htm``), which is why
# the declared type is preferred.
_EX99_NAME_RE = re.compile(r"ex[._\-]?99(?:[._\-]?(\d+))?", re.I)
# A document that is some exhibit (not the primary, not a graphic) -- used to
# build the exhibit list attached to each returned filing.
_EXHIBIT_NAME_RE = re.compile(r"ex[._\-]?\d", re.I)

# Accession index-page (`{accession}-index.html`) parsing. Each document is one
# table row: Seq | Description | Document (an <a> to the filename) | Type | Size.
# Rows are matched whole so a Type is paired only with the filename in its OWN
# row -- a naive page-wide regex would match ``EX-99.1`` against several rows.
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_items(items_field):
    """Normalise a submissions `items` value to a list of item-code strings.

    Live shape is a comma-joined string (``"2.02,9.01"``); a list is accepted
    too. ``None`` / ``""`` / whitespace come back as ``[]`` -- an empty list is
    how a caller tells "no items declared" from "items present without 2.02".
    """
    if items_field is None:
        return []
    if isinstance(items_field, str):
        parts = items_field.split(",")
    elif isinstance(items_field, (list, tuple)):
        parts = items_field
    else:
        return []
    return [str(p).strip() for p in parts if str(p).strip()]


def ex99_number(name, doc_type=""):
    """The EX-99 sub-number of one exhibit, or ``None`` if it is not an EX-99.

    The DECLARED type (`doc_type` -- the authoritative ``EX-99.1`` from the index
    page's Type column) is read first; the FILENAME (`name`) is only consulted as
    a fallback when no type was declared. A bare ``EX-99`` (no sub-number) is
    number ``0`` so it still counts as an EX-99 and sorts ahead of ``.1`` in the
    lowest-number fallback."""
    m = _EX99_TYPE_RE.search(doc_type or "")
    if not m:
        m = _EX99_NAME_RE.search(name or "")
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 0


def _basename(href):
    """The bare filename from an index-page anchor href. Handles the inline-XBRL
    viewer prefix (``/ix?doc=/Archives/.../doc.htm``) and a plain path alike."""
    href = href.split("doc=", 1)[-1]      # drop the `/ix?doc=` viewer wrapper
    return href.rsplit("/", 1)[-1].strip()


def parse_index_page(html):
    """Map ``filename -> declared exhibit type`` from an accession index page.

    Reads the Document-Format-Files table row by row: within each ``<tr>`` the
    document filename comes from the anchor and the type is the cell immediately
    after that anchor's cell. Pairing per-row is what stops a Type being matched
    against a different row's filename when several EX-99 rows are present. Rows
    with no document anchor (the header, the complete-submission row) are skipped.
    """
    type_by_name = {}
    for row in _TR_RE.findall(html or ""):
        cells = _TD_RE.findall(row)
        doc_idx = None
        name = None
        for i, cell in enumerate(cells):
            m = _ANCHOR_RE.search(cell)
            if m:
                doc_idx = i
                name = _basename(m.group(1))
                break
        if name is None or doc_idx is None:
            continue
        doc_type = ""
        if doc_idx + 1 < len(cells):
            doc_type = _TAG_RE.sub("", cells[doc_idx + 1]).strip()
        type_by_name[name] = doc_type
    return type_by_name


def _is_exhibit(name, doc_type):
    return bool(_EXHIBIT_NAME_RE.search(name or "")) or \
        (doc_type or "").upper().strip().startswith("EX-")


def exhibits_from_index(index_json, type_map=None):
    """The exhibit list for one accession, read from its `index.json` manifest.

    `type_map` (from `parse_index_page`) supplies the AUTHORITATIVE per-document
    exhibit type keyed by filename; when a document appears there, that declared
    type -- not the manifest's directory-icon `type`, and not the filename -- is
    what decides its EX-99 number. A document absent from `type_map` falls back
    to the old filename heuristic.

    Each entry is ``{name, type, ex99_number}`` -- `type` is the declared type
    when known (else the manifest's), and `ex99_number` is the EX-99 sub-number
    (0 for a bare EX-99) or ``None`` for a non-EX-99 exhibit, so
    `press_release_document` can select without re-parsing filenames.
    """
    type_map = type_map or {}
    items = ((index_json.get("directory", {}) or {}).get("item", []) or [])
    exhibits = []
    for it in items:
        name = it.get("name", "") or ""
        # The declared type from the index page wins; the manifest type (a
        # directory-icon filename live) is only a last resort.
        declared = type_map.get(name)
        doc_type = declared if declared else (it.get("type", "") or "")
        num = ex99_number(name, doc_type)
        if num is None and not _is_exhibit(name, doc_type):
            continue
        exhibits.append({"name": name, "type": doc_type, "ex99_number": num})
    return exhibits


def press_release_document(filing):
    """Pick the earnings press release exhibit out of `filing["exhibits"]`.

    EX-99.1 is the release by convention; when it is absent, fall back to the
    lowest-numbered EX-99.x present. Returns the exhibit dict, or ``None`` when
    the filing carries no EX-99 exhibit at all.
    """
    ex99 = [e for e in filing.get("exhibits", []) if e.get("ex99_number") is not None]
    if not ex99:
        return None
    for e in ex99:
        if e["ex99_number"] == 1:
            return e
    return min(ex99, key=lambda e: e["ex99_number"])


def earnings_release_filings(client, cik):
    """Every earnings-release 8-K in `cik`'s filing history, via metadata only.

    Reads `client.submissions(cik)` and, for each candidate, both
    `client.filing_index` (the document manifest) and `client.filing_index_page`
    (the authoritative Type column) to attach its exhibit list. A filing is
    returned when it is an 8-K AND either
    reports Item 2.02, OR has a missing/empty `items` field (returned with an
    ``items_missing`` flag rather than dropped). Each result is a dict with
    ``accession``, ``filing_date``, ``form``, ``items``, ``exhibits`` and
    ``flags``.
    """
    sub = client.submissions(cik)
    recent = ((sub.get("filings", {}) or {}).get("recent", {}) or {})

    accessions = recent.get("accessionNumber", []) or []
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    items_col = recent.get("items", []) or []

    out = []
    for i, accession in enumerate(accessions):
        form = forms[i] if i < len(forms) else ""
        if not str(form).upper().startswith("8-K"):
            continue

        items = parse_items(items_col[i] if i < len(items_col) else None)
        flags = []
        if not items:
            flags.append("items_missing")
        elif RESULTS_ITEM not in items:
            continue  # an 8-K about something else -- not an earnings release

        index_json = client.filing_index(cik, accession)
        type_map = parse_index_page(client.filing_index_page(cik, accession))
        out.append({
            "accession": accession,
            "filing_date": dates[i] if i < len(dates) else None,
            "form": form,
            "items": items,
            "exhibits": exhibits_from_index(index_json, type_map),
            "flags": flags,
        })
    return out


if __name__ == "__main__":
    class _FakeClient:
        def submissions(self, cik):
            return {"filings": {"recent": {
                "accessionNumber": ["0000012345-26-000001", "0000012345-26-000002"],
                "form": ["8-K", "8-K"],
                "filingDate": ["2026-01-15", "2026-04-15"],
                "items": ["2.02,9.01", ""],
            }}}

        def filing_index(self, cik, accession):
            return {"directory": {"item": [
                {"name": "d1_8k.htm", "type": "text.gif", "size": "9000"},
                # A release whose FILENAME says nothing (like NVDA's q2fy27pr.htm);
                # only the index page's declared type resolves it.
                {"name": "pr.htm", "type": "text.gif", "size": "12000"},
                {"name": "cfo.htm", "type": "text.gif", "size": "3000"},
            ]}}

        def filing_index_page(self, cik, accession):
            return (
                '<table class="tableFile" summary="Document Format Files">'
                '<tr><td>1</td><td>8-K</td>'
                '<td><a href="/ix?doc=/x/d1_8k.htm">d1_8k.htm</a></td>'
                '<td>8-K</td><td>9000</td></tr>'
                '<tr><td>2</td><td>EX-99.1</td>'
                '<td><a href="/x/pr.htm">pr.htm</a></td>'
                '<td>EX-99.1</td><td>12000</td></tr>'
                '<tr><td>3</td><td>EX-99.2</td>'
                '<td><a href="/x/cfo.htm">cfo.htm</a></td>'
                '<td>EX-99.2</td><td>3000</td></tr>'
                '</table>')

    fils = earnings_release_filings(_FakeClient(), "12345")
    assert len(fils) == 2, fils
    assert fils[1]["flags"] == ["items_missing"], fils[1]
    # Resolved from the declared type, not the (uninformative) filename.
    pr = press_release_document(fils[0])
    assert pr["name"] == "pr.htm", pr
    assert pr["ex99_number"] == 1, pr
    # The index-page parser pairs each Type with its own row's filename.
    tmap = parse_index_page(_FakeClient().filing_index_page("12345", "x"))
    assert tmap == {"d1_8k.htm": "8-K", "pr.htm": "EX-99.1",
                    "cfo.htm": "EX-99.2"}, tmap
    # Declared type is authoritative; filename is only the fallback.
    assert ex99_number("q2fy27pr.htm", "EX-99.1") == 1
    assert ex99_number("q2fy27pr.htm") is None
    for name, n in [("ex991.htm", 1), ("ex9901.htm", 1), ("ex99.htm", 0),
                    ("ex10-1.htm", None), ("d12dex99-1.htm", 1)]:
        assert ex99_number(name) == n, (name, ex99_number(name))
    print("earnings_releases self-check: PASS")
