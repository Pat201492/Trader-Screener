"""Tests for the structured-XML form readers (issue #283). No network."""

from pathlib import Path

import pytest

from xml_forms import READERS, XmlFormError, read_filing

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
FORM4 = (FIXTURES / "form4_cook.xml").read_text(encoding="utf-8")
F13F_INFO = (FIXTURES / "form13fhr_infotable.xml").read_text(encoding="utf-8")
F13F_PRIMARY = (FIXTURES / "form13fhr_primary.xml").read_text(encoding="utf-8")
SCHED_13D = (FIXTURES / "schedule13d.xml").read_text(encoding="utf-8")
SCHED_13GA = (FIXTURES / "schedule13ga.xml").read_text(encoding="utf-8")
FORM_D = (FIXTURES / "formd_private_fund.xml").read_text(encoding="utf-8")
FORM_DA = (FIXTURES / "formd_amendment.xml").read_text(encoding="utf-8")

_SCHED13_ROW_KEYS = {
    "issuer_name", "issuer_cik", "issuer_cusip", "class_title",
    "reporting_person", "citizenship_or_organization", "sole_voting_power",
    "shared_voting_power", "sole_dispositive_power", "shared_dispositive_power",
    "aggregate_amount_owned", "percent_of_class", "type_of_reporting_person",
    "event_date", "is_amendment",
}

_FORM_D_ROW_KEYS = {
    "issuer_name", "issuer_cik", "jurisdiction", "entity_type",
    "industry_group", "federal_exemptions", "date_of_first_sale",
    "total_offering_amount", "total_amount_sold", "total_remaining",
    "investor_count", "is_amendment", "related_persons",
}

_F13F_ROW_KEYS = {
    "name_of_issuer", "title_of_class", "cusip", "value", "shares_or_principal",
    "sh_prn_type", "put_call", "investment_discretion", "voting_sole",
    "voting_shared", "voting_none", "report_period", "manager_name",
}

_ROW_KEYS = {
    "issuer_cik", "issuer_ticker", "owner_name", "owner_cik", "is_director",
    "is_officer", "officer_title", "is_ten_pct_owner", "table", "row_kind",
    "security_title", "transaction_date", "transaction_code", "shares",
    "price", "acquired_disposed", "shares_owned_after", "direct_indirect",
}


def test_form4_rows_carry_every_key_with_numeric_amounts():
    rows = read_filing("4", FORM4)
    # One row per line across both tables: 1 transaction + 1 holding + 1
    # derivative transaction.
    assert len(rows) == 3
    for row in rows:
        assert set(row) == _ROW_KEYS
    # The holding line says it is a holding, not a trade with null fields.
    assert [r["row_kind"] for r in rows] == ["transaction", "holding", "transaction"]

    tx = rows[0]
    assert tx["table"] == "non_derivative"
    assert tx["issuer_cik"] == "0000320193"
    assert tx["issuer_ticker"] == "AAPL"
    assert tx["owner_name"] == "COOK TIMOTHY D"
    assert tx["owner_cik"] == "0001214128"
    assert tx["is_director"] is True
    assert tx["is_officer"] is True
    assert tx["is_ten_pct_owner"] is False
    assert tx["officer_title"] == "Chief Executive Officer"
    assert tx["security_title"] == "Common Stock"
    assert tx["transaction_date"] == "2026-03-01"
    assert tx["transaction_code"] == "S"
    assert tx["acquired_disposed"] == "D"
    assert tx["direct_indirect"] == "D"
    # shares / price are numbers, not strings.
    assert tx["shares"] == 511000 and isinstance(tx["shares"], (int, float))
    assert tx["price"] == 170.0 and isinstance(tx["price"], (int, float))
    assert tx["shares_owned_after"] == 3280000


def test_holding_line_has_null_transaction_fields():
    holding = read_filing("4", FORM4)[1]
    assert holding["table"] == "non_derivative"
    assert holding["security_title"] == "Common Stock"
    assert holding["direct_indirect"] == "I"
    assert holding["shares_owned_after"] == 125000
    # A holding is not a transaction: date / code / shares / price are null.
    assert holding["transaction_date"] is None
    assert holding["transaction_code"] is None
    assert holding["shares"] is None
    assert holding["price"] is None
    assert holding["acquired_disposed"] is None


def test_derivative_table_and_missing_price_is_null():
    deriv = read_filing("4", FORM4)[2]
    assert deriv["table"] == "derivative"
    assert deriv["security_title"] == "Restricted Stock Unit"
    assert deriv["shares"] == 90000
    assert deriv["acquired_disposed"] == "A"
    # The derivative transaction omits transactionPricePerShare entirely.
    assert deriv["price"] is None


def test_forms_3_and_5_share_the_ownership_reader():
    # All three insider forms use the same schema, so each reads the fixture.
    for form in ("3", "4", "5"):
        assert read_filing(form, FORM4)[0]["issuer_ticker"] == "AAPL"
    assert READERS["3"] is READERS["4"] is READERS["5"]


def test_not_xml_raises():
    with pytest.raises(XmlFormError):
        read_filing("4", "<not-xml>")


def test_wrong_schema_raises():
    # The 13F-HR reader rejects a Form 4: an ownershipDocument is not an
    # information table, so it raises rather than returning garbage rows.
    with pytest.raises(XmlFormError):
        read_filing("13F-HR", FORM4)


def test_13f_rows_one_per_holding_with_every_key_and_numbers():
    rows = read_filing("13F-HR", F13F_INFO, primary_text=F13F_PRIMARY)
    assert len(rows) == 3
    for row in rows:
        assert set(row) == _F13F_ROW_KEYS

    apple = rows[0]
    assert apple["name_of_issuer"] == "APPLE INC"
    assert apple["title_of_class"] == "COM"
    assert apple["cusip"] == "037833100"
    assert apple["sh_prn_type"] == "SH"
    assert apple["investment_discretion"] == "DFND"
    # Cover fields from primary_doc.xml are stamped on every row.
    assert apple["report_period"] == "2026-06-30"
    assert apple["manager_name"] == "BERKSHIRE HATHAWAY INC"
    # Numeric fields are numbers, not strings.
    assert apple["value"] == 91000000 and isinstance(apple["value"], int)
    assert apple["shares_or_principal"] == 915560382
    assert isinstance(apple["shares_or_principal"], int)
    assert apple["voting_sole"] == 915560382
    assert apple["voting_shared"] == 0
    assert apple["voting_none"] == 0


def test_13f_put_call_is_null_when_absent_and_read_when_present():
    rows = read_filing("13F-HR", F13F_INFO, primary_text=F13F_PRIMARY)
    # The Apple holding has no putCall element.
    assert rows[0]["put_call"] is None
    # The Coca-Cola holding carries one.
    assert rows[1]["put_call"] == "Call"


def test_13f_principal_holding_carries_prn_type():
    prn = read_filing("13F-HR", F13F_INFO, primary_text=F13F_PRIMARY)[2]
    assert prn["sh_prn_type"] == "PRN"
    assert prn["shares_or_principal"] == 1500000
    assert prn["voting_none"] == 1500000


def test_13f_cover_fields_null_without_primary_document():
    # The reader still produces holdings when no primary_doc is supplied; only
    # the cover fields fall out as None.
    rows = read_filing("13F-HR", F13F_INFO)
    assert rows[0]["report_period"] is None
    assert rows[0]["manager_name"] is None
    assert rows[0]["name_of_issuer"] == "APPLE INC"


def test_13f_rejects_primary_document_as_data():
    # primary_doc.xml is the cover page, not an information table -- feeding it
    # as the data document raises, which is how xml_run skips past it.
    with pytest.raises(XmlFormError):
        read_filing("13F-HR", F13F_PRIMARY)


def test_wrong_root_element_raises():
    not_ownership = "<edgarSubmission><documentType>13F-HR</documentType></edgarSubmission>"
    with pytest.raises(XmlFormError):
        read_filing("4", not_ownership)


def test_form_code_matched_case_insensitively_and_trimmed():
    assert read_filing(" 4 ", FORM4)[0]["issuer_cik"] == "0000320193"


# ── Schedule 13D / 13G ─────────────────────────────────────────────────────────

def test_schedule_13d_one_row_per_person_with_every_key_and_numbers():
    rows = read_filing("SCHEDULE 13D", SCHED_13D)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _SCHED13_ROW_KEYS
    assert row["issuer_name"] == "Star Equity Holdings, Inc."
    assert row["issuer_cik"] == "0001210708"
    assert row["issuer_cusip"] == "443787205"
    assert row["class_title"] == "Common Stock, par value $0.001 per share"
    assert row["reporting_person"] == "JEFFREY E. EBERWEIN"
    assert row["citizenship_or_organization"] == "X1"
    assert row["type_of_reporting_person"] == "IN"
    assert row["event_date"] == "2026-06-25"
    assert row["is_amendment"] is False
    # Numeric boxes are numbers, not strings; percent_of_class is a number.
    assert row["sole_voting_power"] == 1149289
    assert row["sole_dispositive_power"] == 1149289
    assert row["aggregate_amount_owned"] == 1149289
    assert row["percent_of_class"] == 30.7
    assert isinstance(row["percent_of_class"], float)
    # A box the 13D filer left blank (shared power) reads back None, not zero.
    assert row["shared_voting_power"] is None
    assert row["shared_dispositive_power"] is None


def test_schedule_13g_amendment_one_row_per_person_with_every_key():
    rows = read_filing("SCHEDULE 13G/A", SCHED_13GA)
    assert len(rows) == 2
    for row in rows:
        assert set(row) == _SCHED13_ROW_KEYS
        # The /A amendment flag is set on every row.
        assert row["is_amendment"] is True
        # Cover fields are stamped on every row, read through the 13G spellings.
        assert row["issuer_name"] == "First Majestic Silver Corp."
        assert row["issuer_cik"] == "0001308648"
        assert row["issuer_cusip"] == "32076V103"
        assert row["class_title"] == "Common Shares, no par value"
        assert row["event_date"] == "2025-01-16"

    first = rows[0]
    assert first["reporting_person"] == "Electrum Silver US LLC"
    assert first["citizenship_or_organization"] == "DE"
    assert first["type_of_reporting_person"] == "OO"
    # The power boxes are nested two levels down in 13G; read as numbers.
    assert first["sole_voting_power"] == 0
    assert first["shared_voting_power"] == 45631412
    assert first["sole_dispositive_power"] == 0
    assert first["shared_dispositive_power"] == 45631412
    assert first["aggregate_amount_owned"] == 45631412
    assert first["percent_of_class"] == 9.5
    assert isinstance(first["percent_of_class"], float)

    second = rows[1]
    assert second["reporting_person"] == "Electrum Strategic Management LLC"
    assert second["aggregate_amount_owned"] == 56111157
    assert second["percent_of_class"] == 11.7


def test_schedule_13dg_shares_one_reader_across_all_four_form_types():
    for form in ("SCHEDULE 13D", "SCHEDULE 13D/A",
                 "SCHEDULE 13G", "SCHEDULE 13G/A"):
        assert READERS[form] is READERS["SCHEDULE 13D"]
    # Case and surrounding space do not matter at the front door.
    assert read_filing(" schedule 13g/a ", SCHED_13GA)[0]["percent_of_class"] == 9.5


def test_schedule_13dg_rejects_a_form_4_and_a_13f_cover():
    # An ownershipDocument is not a 13D/13G submission; a 13F cover page is an
    # edgarSubmission but in the wrong namespace. Both raise rather than return
    # garbage rows.
    with pytest.raises(XmlFormError):
        read_filing("SCHEDULE 13D", FORM4)
    with pytest.raises(XmlFormError):
        read_filing("SCHEDULE 13G", F13F_PRIMARY)
    # And the ownership reader rejects a 13D submission in turn.
    with pytest.raises(XmlFormError):
        read_filing("4", SCHED_13D)


# ── Form D — exempt offering ─────────────────────────────────────────────────────

def test_form_d_one_row_with_every_key_and_numeric_amounts():
    rows = read_filing("D", FORM_D)
    # Form D is one offering, so it reads into exactly one row.
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _FORM_D_ROW_KEYS

    assert row["issuer_name"] == "Evergreen Capital Partners Fund III, L.P."
    assert row["issuer_cik"] == "0001854776"
    assert row["jurisdiction"] == "DELAWARE"
    assert row["entity_type"] == "Limited Partnership"
    assert row["industry_group"] == "Pooled Investment Fund"
    assert row["date_of_first_sale"] == "2026-02-17"
    assert row["is_amendment"] is False

    # federal_exemptions and related_persons are lists.
    assert row["federal_exemptions"] == ["06b", "3C", "3C.1"]
    assert row["related_persons"] == [
        "Margaret A Chen", "Evergreen Capital GP III, LLC"]

    # Amounts come back as numbers, not strings.
    assert row["total_offering_amount"] == 750000000
    assert isinstance(row["total_offering_amount"], int)
    assert row["total_amount_sold"] == 312500000
    assert row["total_remaining"] == 437500000
    assert row["investor_count"] == 42


def test_form_d_indefinite_offering_amount_is_null():
    # A filer who has not capped the raise writes "Indefinite" where a number
    # would go; it reads back as None, and the row is still returned.
    rows = read_filing("D/A", FORM_DA)
    assert len(rows) == 1
    row = rows[0]
    assert row["total_offering_amount"] is None
    assert row["total_remaining"] is None
    # The sold figure is still a real number.
    assert row["total_amount_sold"] == 18750000
    assert row["issuer_name"] == "Northwind Ventures Opportunity Fund, LLC"


def test_form_d_amendment_flag_is_set():
    rows = read_filing("D/A", FORM_DA)
    assert rows[0]["is_amendment"] is True
    assert rows[0]["federal_exemptions"] == ["06c", "3C.7"]
    # A single-name block still joins to one display name.
    assert rows[0]["related_persons"] == ["Daniel Okafor"]


def test_form_d_shares_one_reader_across_both_form_types():
    assert READERS["D"] is READERS["D/A"]
    # Case and surrounding space do not matter at the front door.
    assert read_filing(" d ", FORM_D)[0]["issuer_cik"] == "0001854776"


def test_form_d_rejects_other_edgar_submissions():
    # A 13F cover page and a Schedule 13D both root at edgarSubmission but carry
    # no offeringData, so the Form D reader rejects them rather than returning a
    # blank row.
    with pytest.raises(XmlFormError):
        read_filing("D", F13F_PRIMARY)
    with pytest.raises(XmlFormError):
        read_filing("D", SCHED_13D)
    with pytest.raises(XmlFormError):
        read_filing("D", FORM4)
    with pytest.raises(XmlFormError):
        read_filing("D", "<not-xml>")


def test_form_d_entity_placeholder_is_not_part_of_a_related_name():
    # Live 0002160607-26-000001: firstName "-" before an entity's name.
    import xml.etree.ElementTree as ET
    from xml_forms import _related_person_name
    p = ET.fromstring("<relatedPersonName><firstName>-</firstName>"
                      "<lastName>iCapital PE GP, LLC</lastName></relatedPersonName>")
    assert _related_person_name(p) == "iCapital PE GP, LLC"
    q = ET.fromstring("<relatedPersonName><firstName>Jane</firstName>"
                      "<lastName>Doe</lastName></relatedPersonName>")
    assert _related_person_name(q) == "Jane Doe"
