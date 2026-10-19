"""Tests for the structured-XML form readers (issue #283). No network."""

from pathlib import Path

import pytest

from xml_forms import READERS, XmlFormError, read_filing

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
FORM4 = (FIXTURES / "form4_cook.xml").read_text(encoding="utf-8")
F13F_INFO = (FIXTURES / "form13fhr_infotable.xml").read_text(encoding="utf-8")
F13F_PRIMARY = (FIXTURES / "form13fhr_primary.xml").read_text(encoding="utf-8")

_F13F_ROW_KEYS = {
    "name_of_issuer", "title_of_class", "cusip", "value", "shares_or_principal",
    "sh_prn_type", "put_call", "investment_discretion", "voting_sole",
    "voting_shared", "voting_none", "report_period", "manager_name",
}

_ROW_KEYS = {
    "issuer_cik", "issuer_ticker", "owner_name", "owner_cik", "is_director",
    "is_officer", "officer_title", "is_ten_pct_owner", "table",
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
    assert apple["report_period"] == "06-30-2026"
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
