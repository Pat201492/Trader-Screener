"""
Structured-XML form readers for the EDGAR scrubber (issue #283).

A handful of filings are not prose a model has to read — they are structured XML
with a published schema, so their data can be lifted exactly, with no model and
no network. Forms 3/4/5 (insider ownership), 13F-HR (institutional holdings),
Schedule 13D/13G and Form D all fall here.

This module is the reader *contract* and its readers: Forms 3/4/5 (insider
ownership), 13F-HR (institutional holdings), Schedule 13D/13G (5%+ beneficial
ownership) and Form D (exempt offering). Each registers itself in ``READERS``
and is driven through the same ``read_filing`` front door.

A reader is a function ``(xml_text) -> list[dict]``, one dict per row. It raises
``XmlFormError`` when handed XML that is not its own schema (a 13F read of a
Form 4, say), and ``read_filing`` raises the same error for a form with no reader
at all. A missing element becomes ``None`` — never a guess, never a zero.

A few forms keep their data and their cover fields in *different* documents of
one accession: a 13F-HR lists its holdings in a separate information-table XML,
while the period of report and filing manager live in ``primary_doc.xml``. Such a
reader also accepts an optional ``primary_text`` keyword; ``read_filing`` passes
it through when the reader declares it, and readers that don't need it ignore it.

Stdlib ``xml.etree.ElementTree`` only; no network, no third-party parser.
Run the self-check:  python tools/edgar_scrubber/xml_forms.py
"""

import inspect
import xml.etree.ElementTree as ET


class XmlFormError(ValueError):
    """XML that is not the requested form's schema, or a form with no reader."""


# ── Form 3 / 4 / 5 — insider ownership ─────────────────────────────────────────
#
# All three share one schema, the SEC `ownershipDocument`. The difference between
# them is regulatory (3 = initial statement, 4 = change, 5 = annual), not
# structural, so one reader serves all three. Every leaf value the filer gives is
# wrapped in a `<value>` child, e.g.
#     <transactionShares><value>511000</value></transactionShares>
# so a leaf read is always "the text of the <value> under this path", and a path
# that is absent (a holding has no transactionShares) reads back as None.


def _first(elem, path):
    """The first descendant matching ``path`` under ``elem``, or None."""
    return elem.find(path) if elem is not None else None


def _text(elem, path):
    """Stripped text at ``path`` under ``elem``, or None when the element is
    absent or empty. ``path`` of ``"."`` reads ``elem``'s own text."""
    found = elem.find(path) if elem is not None else None
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _value(elem, path):
    """The text of the ``<value>`` child at ``path`` (the ownership schema wraps
    every leaf in one), or None when the path or its ``<value>`` is absent."""
    return _text(elem, f"{path}/value")


def _num(s):
    """Parse a numeric string to a number — int when it has no fractional part,
    float otherwise — or None for None/blank/non-numeric. Shares come through as
    ints (511000) and prices as floats (170.00); a garbled value is None, never a
    silent zero."""
    if s is None:
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _bool(s):
    """Parse an ownership relationship flag ("1"/"0", "true"/"false") to a bool,
    or None when the element was absent."""
    if s is None:
        return None
    return s.strip().lower() in ("1", "true")


_DATE_FORMS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y")


def _iso_date(value):
    """Every date as ISO YYYY-MM-DD. EDGAR's XML forms disagree: Form 4 writes
    2026-10-06, Schedule 13D/G 10/15/2026, 13F-HR 06-30-2026 (golden set,
    2026-10-19), so a sort or a date filter across forms broke. A string none of
    the known shapes parses is returned unchanged rather than guessed."""
    if not value:
        return value
    import datetime as _dt
    v = str(value).strip()
    for fmt in _DATE_FORMS:
        try:
            return _dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return v


def read_ownership(xml_text):
    """Read a Form 3/4/5 ``ownershipDocument`` into one row per transaction or
    holding, across both the non-derivative and derivative tables.

    Raises ``XmlFormError`` if the text is not well-formed XML or its root is not
    ``ownershipDocument``. Every row carries the issuer, owner and relationship
    fields (constant across the filing) plus the per-line security and amount
    fields; any element the filer omitted is ``None``.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XmlFormError(f"not well-formed XML: {exc}") from exc
    if root.tag != "ownershipDocument":
        raise XmlFormError(
            f"not a Form 3/4/5 ownershipDocument (root is {root.tag!r})")

    issuer = _first(root, "issuer")
    issuer_cik = _value(issuer, "issuerCik") or _text(issuer, "issuerCik")
    issuer_ticker = (_value(issuer, "issuerTradingSymbol")
                     or _text(issuer, "issuerTradingSymbol"))

    owner = _first(root, "reportingOwner")
    owner_id = _first(owner, "reportingOwnerId")
    owner_name = _text(owner_id, "rptOwnerName")
    owner_cik = _text(owner_id, "rptOwnerCik")
    rel = _first(owner, "reportingOwnerRelationship")
    is_director = _bool(_text(rel, "isDirector"))
    is_officer = _bool(_text(rel, "isOfficer"))
    is_ten_pct_owner = _bool(_text(rel, "isTenPercentOwner"))
    officer_title = _text(rel, "officerTitle")

    common = {
        "issuer_cik": issuer_cik,
        "issuer_ticker": issuer_ticker,
        "owner_name": owner_name,
        "owner_cik": owner_cik,
        "is_director": is_director,
        "is_officer": is_officer,
        "officer_title": officer_title,
        "is_ten_pct_owner": is_ten_pct_owner,
    }

    rows = []
    for table, tag in (("non_derivative", "nonDerivativeTable"),
                       ("derivative", "derivativeTable")):
        table_el = _first(root, tag)
        if table_el is None:
            continue
        for line in list(table_el):
            # Transactions and holdings both appear here; a holding simply has no
            # transaction date/code/amount, which fall out as None below.
            amounts = _first(line, "transactionAmounts")
            post = _first(line, "postTransactionAmounts")
            nature = _first(line, "ownershipNature")
            row = dict(common)
            # A holding line (nonDerivativeHolding / derivativeHolding) reports a
            # position, not a trade; say so instead of leaving a row of nulls.
            tag = line.tag.rsplit("}", 1)[-1]
            row.update({
                "table": table,
                "row_kind": "holding" if tag.endswith("Holding") else "transaction",
                "security_title": _value(line, "securityTitle"),
                "transaction_date": _iso_date(_value(line, "transactionDate")),
                # transactionCode is plain text, not <value>-wrapped.
                "transaction_code":
                    _text(_first(line, "transactionCoding"), "transactionCode"),
                "shares": _num(_value(amounts, "transactionShares")),
                "price": _num(_value(amounts, "transactionPricePerShare")),
                "acquired_disposed":
                    _value(amounts, "transactionAcquiredDisposedCode"),
                "shares_owned_after":
                    _num(_value(post, "sharesOwnedFollowingTransaction")),
                "direct_indirect": _value(nature, "directOrIndirectOwnership"),
            })
            rows.append(row)
    return rows


# ── Form 13F-HR — institutional holdings ────────────────────────────────────────
#
# A 13F-HR reports an institutional manager's holdings as of a quarter end. Unlike
# the ownership forms, its data is NOT in `primary_doc.xml`: that document is the
# cover page (period of report, filing manager), and the holdings live in a
# separate information-table XML whose root is `informationTable`, one `infoTable`
# element per holding. Both documents carry a default XML namespace, so every read
# here matches on the *local* tag name, ignoring the namespace. Values are lifted
# exactly as filed (13F values are historically in whole dollars or thousands
# depending on the period; the reader does not rescale) and a leaf the filer
# omitted — a holding with no `putCall`, say — reads back as None.


def _local(tag):
    """The local part of an element tag, dropping any ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def _child(elem, *names):
    """Walk child elements by local name, ignoring namespaces, returning the
    element at the end of the path or None when any step is absent."""
    cur = elem
    for name in names:
        if cur is None:
            return None
        cur = next((c for c in cur if _local(c.tag) == name), None)
    return cur


def _ctext(elem, *names):
    """Stripped text of the child reached by local-name ``names``, or None."""
    found = _child(elem, *names)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _deep(root, name):
    """The first element anywhere under ``root`` whose local name is ``name``,
    or None. Used for cover fields whose exact path varies by filer agent."""
    if root is None:
        return None
    return next((e for e in root.iter() if _local(e.tag) == name), None)


def _read_13f_cover(primary_text):
    """Period of report and filing manager name from a 13F `primary_doc.xml`, or
    ``(None, None)`` when no primary document was supplied or it won't parse."""
    if not primary_text:
        return None, None
    try:
        root = ET.fromstring(primary_text)
    except ET.ParseError:
        return None, None
    period_el = _deep(root, "periodOfReport")
    report_period = _iso_date(period_el.text.strip() or None
                              if period_el is not None and period_el.text else None)
    manager = _deep(root, "filingManager")
    return report_period, _ctext(manager, "name")


def read_13f(xml_text, primary_text=None):
    """Read a 13F-HR information table into one row per holding.

    ``xml_text`` is the information-table document (root ``informationTable``);
    ``primary_text``, when given, is the accession's ``primary_doc.xml``, whose
    period of report and filing manager name are stamped onto every row. Raises
    ``XmlFormError`` if ``xml_text`` is not well-formed or is not an information
    table (a Form 4 read, say). Numeric fields come back as numbers; any element
    the filer omitted — notably ``putCall`` — is None.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XmlFormError(f"not well-formed XML: {exc}") from exc
    if _local(root.tag) != "informationTable":
        raise XmlFormError(
            f"not a 13F-HR information table (root is {root.tag!r})")

    report_period, manager_name = _read_13f_cover(primary_text)

    rows = []
    for it in root:
        if _local(it.tag) != "infoTable":
            continue
        amt = _child(it, "shrsOrPrnAmt")
        voting = _child(it, "votingAuthority")
        rows.append({
            "name_of_issuer": _ctext(it, "nameOfIssuer"),
            "title_of_class": _ctext(it, "titleOfClass"),
            "cusip": _ctext(it, "cusip"),
            "value": _num(_ctext(it, "value")),
            "shares_or_principal": _num(_ctext(amt, "sshPrnamt")),
            "sh_prn_type": _ctext(amt, "sshPrnamtType"),
            "put_call": _ctext(it, "putCall"),
            "investment_discretion": _ctext(it, "investmentDiscretion"),
            "voting_sole": _num(_ctext(voting, "Sole")),
            "voting_shared": _num(_ctext(voting, "Shared")),
            "voting_none": _num(_ctext(voting, "None")),
            "report_period": report_period,
            "manager_name": manager_name,
        })
    return rows


# ── Schedule 13D / 13G — 5%+ beneficial ownership ───────────────────────────────
#
# Since December 2024 Schedules 13D and 13G are filed as structured XML under the
# form types `SCHEDULE 13D`, `SCHEDULE 13D/A`, `SCHEDULE 13G` and `SCHEDULE 13G/A`
# (older `SC 13D` / `SC 13G` are HTML or text and stay search-only). Both carry
# the issuer's cover page and one block per reporting person — the 5%+ beneficial
# owner, its citizenship, its voting and dispositive power, the aggregate amount
# owned and the percent of class.
#
# The two forms do NOT share one schema. 13D is the `schedule13D` namespace and
# spells the event date `dateOfEvent`, the issuer CIK `issuerCIK`, the CUSIP
# `issuerCusips/issuerCusipNumber`, the aggregate `aggregateAmountOwned`, the
# percent `percentOfClass`, and lists each person as `reportingPersonInfo` with
# the power boxes as direct children. 13G is the `schedule13g` namespace and
# spells the same data `eventDateRequiresFilingThisStatement`, `issuerCik`,
# `issuerCusip`, `reportingPersonBeneficiallyOwnedAggregateNumberOfShares`,
# `classPercent`, and `coverPageHeaderReportingPersonDetails` with the power
# boxes nested under `reportingPersonBeneficiallyOwnedNumberOfShares`. One reader
# serves all four by matching on the LOCAL element name, case-insensitively, and
# accepting either spelling for the fields the two schemas name differently; a
# box the filer left blank reads back as None, never a zero.


# The per-person block, by local name, in each schema.
_SCHED13_PERSON_TAGS = ("reportingPersonInfo",
                        "coverPageHeaderReportingPersonDetails")


def _desc(elem, *names):
    """First element at or below ``elem`` whose local name is any of ``names``
    (case-insensitive, namespace ignored), or None. A descendant search because
    the 13D and 13G schemas nest the same datum at different depths — a power box
    is a direct child in 13D and two levels down in 13G."""
    if elem is None:
        return None
    wanted = {n.lower() for n in names}
    for e in elem.iter():
        if _local(e.tag).lower() in wanted:
            return e
    return None


def _dtext(elem, *names):
    """Stripped text of the first descendant matching ``names`` (see ``_desc``),
    or None when it is absent or empty."""
    found = _desc(elem, *names)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def read_schedule_13dg(xml_text):
    """Read a structured Schedule 13D/13G into one row per reporting person.

    Serves all four XML form types (`SCHEDULE 13D`, `SCHEDULE 13D/A`,
    `SCHEDULE 13G`, `SCHEDULE 13G/A`). Every row carries the issuer and cover
    fields (constant across the filing) plus that person's citizenship, voting
    and dispositive power, aggregate amount owned, percent of class and type;
    `event_date` and `is_amendment` are stamped on every row. `is_amendment` is
    read from the filing's own `submissionType` (a `/A` suffix), not guessed.

    Raises ``XmlFormError`` if the text is not well-formed XML or is not a
    Schedule 13D/13G submission (a Form 4 or a 13F cover page, say). Numeric
    boxes — the power boxes, the aggregate and the percent — come back as
    numbers; a box the filer omitted is None.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XmlFormError(f"not well-formed XML: {exc}") from exc
    # The root is `edgarSubmission` in a `schedule13D`/`schedule13g` namespace.
    # That namespace is what tells a 13D/13G apart from a 13F-HR cover page,
    # whose root is also `edgarSubmission` but in the thirteenF namespace.
    if _local(root.tag).lower() != "edgarsubmission" \
            or "schedule13" not in root.tag.lower():
        raise XmlFormError(
            f"not a Schedule 13D/13G submission (root is {root.tag!r})")

    submission_type = _dtext(root, "submissionType")
    is_amendment = bool(submission_type
                        and submission_type.strip().upper().endswith("/A"))

    cover = _desc(root, "coverPageHeader")
    issuer = _desc(cover, "issuerInfo")
    common = {
        "issuer_name": _dtext(issuer, "issuerName"),
        "issuer_cik": _dtext(issuer, "issuerCIK", "issuerCik"),
        "issuer_cusip": _dtext(issuer, "issuerCusipNumber", "issuerCusip"),
        "class_title": _dtext(cover, "securitiesClassTitle"),
        "event_date": _iso_date(_dtext(cover, "dateOfEvent",
                                       "eventDateRequiresFilingThisStatement")),
        "is_amendment": is_amendment,
    }

    rows = []
    person_tags = {t.lower() for t in _SCHED13_PERSON_TAGS}
    for person in root.iter():
        if _local(person.tag).lower() not in person_tags:
            continue
        row = dict(common)
        row.update({
            "reporting_person": _dtext(person, "reportingPersonName"),
            "citizenship_or_organization":
                _dtext(person, "citizenshipOrOrganization"),
            "sole_voting_power": _num(_dtext(person, "soleVotingPower")),
            "shared_voting_power": _num(_dtext(person, "sharedVotingPower")),
            "sole_dispositive_power":
                _num(_dtext(person, "soleDispositivePower")),
            "shared_dispositive_power":
                _num(_dtext(person, "sharedDispositivePower")),
            "aggregate_amount_owned": _num(_dtext(
                person, "aggregateAmountOwned",
                "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")),
            "percent_of_class":
                _num(_dtext(person, "percentOfClass", "classPercent")),
            "type_of_reporting_person":
                _dtext(person, "typeOfReportingPerson"),
        })
        rows.append(row)
    return rows


# ── Form D — notice of exempt (private) offering ─────────────────────────────────
#
# Form D (and its amendment `D/A`) is the notice of an exempt offering under
# Regulation D — a private placement that skips registration. It is filed as a
# single structured XML document whose root is a bare `edgarSubmission` (no
# default namespace, unlike the 13F cover and the 13D/13G submissions, which also
# root at `edgarSubmission` but inside a namespace). Its data is one offering, so
# it reads into exactly ONE row: the issuer's cover fields, the offering's
# industry and exemptions, the money raised and still open, and the list of
# related persons (the executives, directors and promoters named on the notice).
#
# The dollar fields are the one trap: a filer who has not capped the raise writes
# the literal word "Indefinite" where a number would go, so `totalOfferingAmount`
# and `totalRemaining` are sometimes prose, not digits. `_num` already maps a
# non-numeric string to None, so "Indefinite" reads back as None — never a zero,
# never a guess. `is_amendment` is read from the filing's own `isAmendment` box
# (and the `/A` submission type), not inferred.


def _all_dtext(elem, name):
    """Stripped text of every descendant of ``elem`` whose local name is ``name``
    (case-insensitive, namespace ignored), skipping empties, as a list."""
    out = []
    if elem is None:
        return out
    wanted = name.lower()
    for e in elem.iter():
        if _local(e.tag).lower() == wanted and e.text and e.text.strip():
            out.append(e.text.strip())
    return out


def _related_person_name(person):
    """Join a `relatedPersonName`'s first/middle/last into one display name, or
    None when the block carries no name parts."""
    parts = [_dtext(person, "firstName"),
             _dtext(person, "middleName"),
             _dtext(person, "lastName")]
    # An entity filer puts a placeholder in the person fields: "-" as the
    # first name of "iCapital PE GP, LLC" read as "- iCapital PE GP, LLC".
    name = " ".join(p for p in parts if p and p.strip() not in ("-", "--", "N/A", "n/a", "None"))
    return name or None


def read_form_d(xml_text):
    """Read a Form D / D/A notice of exempt offering into exactly one row.

    The row carries the issuer (name, CIK, jurisdiction, entity type), the
    offering's industry group and federal exemptions (a list), the date of first
    sale, the offering amounts (total offered, sold and remaining), the count of
    investors, the amendment flag and the related persons named on the notice (a
    list). A dollar field the filer gave as "Indefinite" reads back as None, as
    does any element the filer omitted.

    Raises ``XmlFormError`` if the text is not well-formed XML or is not a Form D
    submission (a 13F cover page or a Schedule 13D, say, which also root at
    ``edgarSubmission`` but carry no ``offeringData``).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XmlFormError(f"not well-formed XML: {exc}") from exc
    # A Form D roots at a bare `edgarSubmission` and is the only one of the three
    # `edgarSubmission` forms that carries an `offeringData` block; that block is
    # what tells it apart from a 13F cover page or a Schedule 13D/13G.
    if _local(root.tag).lower() != "edgarsubmission" \
            or _desc(root, "offeringData") is None:
        raise XmlFormError(
            f"not a Form D submission (root is {root.tag!r})")

    submission_type = _dtext(root, "submissionType")
    issuer = _desc(root, "primaryIssuer")
    offering = _desc(root, "offeringData")
    sales = _desc(offering, "offeringSalesAmounts")

    is_amendment = bool(
        (submission_type and submission_type.strip().upper().endswith("/A"))
        or _bool(_dtext(offering, "isAmendment")))

    related = [name for person in root.iter()
               if _local(person.tag).lower() == "relatedpersoninfo"
               for name in (_related_person_name(person),)
               if name]

    return [{
        "issuer_name": _dtext(issuer, "entityName"),
        "issuer_cik": _dtext(issuer, "cik"),
        "jurisdiction": _dtext(issuer, "jurisdictionOfInc"),
        "entity_type": _dtext(issuer, "entityType"),
        "industry_group": _dtext(offering, "industryGroupType"),
        "federal_exemptions": _all_dtext(
            _desc(offering, "federalExemptionsExclusions"), "item"),
        "date_of_first_sale": _iso_date(_dtext(
            _desc(offering, "dateOfFirstSale"), "value")),
        # "Indefinite" is prose, not a number; _num maps it to None.
        "total_offering_amount": _num(_dtext(sales, "totalOfferingAmount")),
        "total_amount_sold": _num(_dtext(sales, "totalAmountSold")),
        "total_remaining": _num(_dtext(sales, "totalRemaining")),
        "investor_count": _num(_dtext(offering, "totalNumberAlreadyInvested")),
        "is_amendment": is_amendment,
        "related_persons": related,
    }]


def read_form_144(xml_text):
    """Read a Form 144 / 144/A notice of proposed sale into one row per security
    to be sold (each ``securitiesInformation`` block).

    Every row carries the issuer, the person for whose account the securities
    are sold and their relationship to the issuer, the class, units to be sold,
    aggregate market value, units outstanding, approximate sale date, exchange
    and broker -- plus the acquisition (date and nature) from the
    ``securitiesToBeSold`` table entry of the same class (the first entry when
    no class matches), and the amendment flag. Missing elements read as None.

    Raises ``XmlFormError`` if the text is not well-formed XML or is not a Form
    144 (a Form D, 13F cover or Schedule 13D also roots at ``edgarSubmission``
    but carries no ``securitiesInformation``).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise XmlFormError(f"not well-formed XML: {exc}") from exc
    if _local(root.tag).lower() != "edgarsubmission" \
            or _desc(root, "securitiesInformation") is None:
        raise XmlFormError(
            f"not a Form 144 submission (root is {root.tag!r})")

    submission_type = _dtext(root, "submissionType")
    is_amendment = bool(submission_type
                        and submission_type.strip().upper().endswith("/A"))
    issuer = _desc(root, "issuerInfo")
    relationships = _all_dtext(_desc(issuer, "relationshipsToIssuer"),
                               "relationshipToIssuer")
    acquisitions = [e for e in root.iter()
                    if _local(e.tag).lower() == "securitiestobesold"]

    rows = []
    for info in root.iter():
        if _local(info.tag).lower() != "securitiesinformation":
            continue
        cls = _dtext(info, "securitiesClassTitle")
        acq = next((a for a in acquisitions
                    if cls and _dtext(a, "securitiesClassTitle") == cls),
                   acquisitions[0] if acquisitions else None)
        broker = _desc(info, "brokerOrMarketmakerDetails")
        rows.append({
            "issuer_name": _dtext(issuer, "issuerName"),
            "issuer_cik": _dtext(issuer, "issuerCik"),
            "seller_name": _dtext(
                issuer, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"),
            "relationship_to_issuer": "; ".join(relationships) or None,
            "class_title": cls,
            "units_to_be_sold": _num(_dtext(info, "noOfUnitsSold")),
            "aggregate_market_value": _num(_dtext(info, "aggregateMarketValue")),
            "units_outstanding": _num(_dtext(info, "noOfUnitsOutstanding")),
            "approx_sale_date": _iso_date(_dtext(info, "approxSaleDate")),
            "exchange": _dtext(info, "securitiesExchangeName"),
            "broker_name": _dtext(broker, "name"),
            "acquired_date": _iso_date(_dtext(acq, "acquiredDate")),
            "nature_of_acquisition": _dtext(acq, "natureOfAcquisitionTransaction"),
            "is_amendment": is_amendment,
        })
    return rows


# ── Registry + front door ──────────────────────────────────────────────────────

# Form code -> reader. Each reader owns its schema check and raises XmlFormError
# when handed another form's XML. Codes are matched case-insensitively (see
# `read_filing`).
READERS = {
    # Amendments (/A) carry the same schema; a live NVDA Form 4 search returned
    # 4 and 4/A together, and the /A was unreadable (2026-10-19).
    "3": read_ownership,
    "3/A": read_ownership,
    "4": read_ownership,
    "4/A": read_ownership,
    "5": read_ownership,
    "5/A": read_ownership,
    "13F-HR": read_13f,
    "13F-HR/A": read_13f,
    "SCHEDULE 13D": read_schedule_13dg,
    "SCHEDULE 13D/A": read_schedule_13dg,
    "SCHEDULE 13G": read_schedule_13dg,
    "SCHEDULE 13G/A": read_schedule_13dg,
    "D": read_form_d,
    "D/A": read_form_d,
    "144": read_form_144,
    "144/A": read_form_144,
}


def read_filing(form, xml_text, primary_text=None):
    """Read a structured-XML filing into a list of row dicts.

    ``form`` is the EDGAR form code ("4", "13F-HR", ...). ``primary_text`` is the
    accession's ``primary_doc.xml``, passed through to readers that declare it
    (13F-HR, whose cover fields live there) and ignored by those that don't.
    Raises ``XmlFormError`` for a form with no reader, or (via the reader) for XML
    that is not that form's schema. No network, no model."""
    reader = READERS.get(str(form).strip().upper())
    if reader is None:
        raise XmlFormError(
            f"no XML reader for form {form!r}; readers exist for "
            f"{sorted(READERS)}")
    if "primary_text" in inspect.signature(reader).parameters:
        return reader(xml_text, primary_text=primary_text)
    return reader(xml_text)


if __name__ == "__main__":
    from pathlib import Path

    fixture = (Path(__file__).resolve().parent
               / "tests" / "fixtures" / "form4_cook.xml").read_text(
                   encoding="utf-8")
    out = read_filing("4", fixture)
    print(f"read_filing('4', fixture) -> {len(out)} row(s)")
    for r in out:
        print(f"  {r['table']:<14} {r['security_title']!r:<24} "
              f"shares={r['shares']} price={r['price']} "
              f"A/D={r['acquired_disposed']}")
    try:
        read_filing("13F-HR", fixture)
    except XmlFormError as exc:
        print(f"read_filing('13F-HR', ...) correctly raised: {exc}")
    try:
        read_filing("4", "<not-xml>")
    except XmlFormError as exc:
        print(f"read_filing('4', '<not-xml>') correctly raised: {exc}")
    print("\nxml_forms self-check: PASS")
