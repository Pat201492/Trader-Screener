"""Deterministic key-terms step: every case here is a real wording from a live
424B2 (2026-09/10), including the two the blind held-out run caught.

Run:  python tools/edgar_scrubber/test_key_terms.py   (or pytest)
"""
from key_terms import extract_key_terms, parse_terms, valid_cusip


def _check_spans(text, got):
    for v in got.values():
        assert text[v.span[0]:v.span[1]] == v.raw, v


def test_bofa_final_supplement_reads_every_field():
    t = ("Terms of the Securities Issuer:: BofA Finance LLC (“BofA Finance”). Guarantor:: Bank of "
         "America Corporation. CUSIP:: 09712GX58 PS-4 Maturity Date:: October 3, 2029, subject to "
         "postponement. Contingent Coupon Rate:: The Contingent Coupon Rate is 19.10% per annum. "
         "Coupon Barrier:: $119.392, which is equal to 80.00% of the Starting Price. "
         "Threshold Price:: $119.392, which is equal to 80.00% of the Starting Price. "
         "The estimated value of the Securities as of the Pricing Date is $962.80 per Security.")
    got = extract_key_terms(t)
    assert got["issuer"].value == "BofA Finance LLC"
    assert got["cusip"].value == "09712GX58"
    assert got["maturity_date"].value == "2029-10-03"
    assert got["contingent_coupon_rate"].value == 19.10
    assert got["coupon_barrier_pct"].value == 80.0
    assert got["barrier_pct"].value == 80.0
    assert got["estimated_value_per_1000"].value == 962.80
    _check_spans(t, got)


def test_call_threshold_is_not_the_downside_barrier():
    # Held-out BNS 0001839882-26-045355: the first version returned the 95%
    # autocall level as barrier_pct.
    t = ("Call Threshold Price:: $140.771, which is equal to 95.00% of the starting price. "
         "Coupon Threshold Price:: $88.908, which is equal to 60.00% of the starting price. "
         "Downside Threshold Price:: $88.908, which is equal to 60.00% of the starting price.")
    got = extract_key_terms(t)
    assert got["barrier_pct"].value == 60.0
    assert got["coupon_barrier_pct"].value == 60.0


def test_a_lone_call_threshold_never_becomes_the_barrier():
    t = "Call Threshold Price:: $140.771, which is equal to 95.00% of the starting price."
    assert "barrier_pct" not in extract_key_terms(t)


def test_floor_rate_and_estimated_value_range_are_left_to_the_model():
    # Preliminary supplements: a floor and a range are not the value.
    t = ("Contingent Coupon Rate:: The contingent coupon rate will be determined on the pricing "
         "date and will be at least 13.50% per annum. "
         "the estimated value of the securities as determined by the Bank would be between "
         "$934.09 (93.409%) and $964.09 (96.409%) per security.")
    got = extract_key_terms(t)
    assert "contingent_coupon_rate" not in got
    assert "estimated_value_per_1000" not in got


def test_expected_between_range_is_left_to_the_model():
    t = ("The estimated value of the Securities as of the Pricing Date is expected to be between "
         "$920.00 and $970.00 per Security.")
    assert "estimated_value_per_1000" not in extract_key_terms(t)


def test_wells_fargo_table_thresholds_from_footnotes():
    t = ("Underlier Starting Value(1) Coupon Threshold Value(2) Downside Threshold Value(3) Common "
         "Stock of ServiceNow, Inc. NOW $131.45 $65.725 $65.725 (1) The closing value of the "
         "Underlier on the strike date. (2) 50% of the starting value. (3) 50% of the starting value.")
    got = extract_key_terms(t)
    assert got["coupon_barrier_pct"].value == 50.0
    assert got["barrier_pct"].value == 50.0
    assert got["barrier_pct"].method == "term-table"
    _check_spans(t, got)


def test_one_threshold_for_coupon_and_downside():
    # JPMorgan: a single "Threshold Price", and the coupon is conditioned on it.
    t = ("Contingent Coupon Payment:: You will receive a contingent coupon payment if the stock "
         "closing price on the calculation day is greater than or equal to the threshold price. "
         "Threshold Price:: $244.678, which is equal to 70% of the starting price. ")
    got = extract_key_terms(t)
    assert got["barrier_pct"].value == 70.0
    assert got["coupon_barrier_pct"].value == 70.0
    assert got["coupon_barrier_pct"].method == "term-shared"


def test_footnote_and_cusip_isin_labels_and_hypotheticals():
    t = ("Stated Maturity Date1:: September 21, 2028 Principal Amount:: $1,000 per security "
         "CUSIP / ISIN:: 06419TUP1 / US06419TUP10 "
         "Hypothetical Threshold Price:: $80.00 Hypothetical coupon threshold price:: $60.00")
    got = extract_key_terms(t)
    assert got["maturity_date"].value == "2028-09-21"
    assert got["cusip"].value == "06419TUP1"
    assert "barrier_pct" not in got and "coupon_barrier_pct" not in got


def test_value_token_never_starts_a_label():
    # "GX58 Maturity Date" must not swallow the CUSIP.
    labels = [x.label for x in parse_terms("CUSIP:: 09712GX58 Maturity Date:: October 3, 2029")]
    assert labels == ["cusip", "maturity date"]


def test_run_on_label_without_a_period_still_maps():
    t = "Guarantor:: Bank of America Corporation CUSIP:: 09712GX58"
    assert extract_key_terms(t)["cusip"].value == "09712GX58"


def test_cusip_needs_a_valid_check_digit():
    assert valid_cusip("46661PVZ7") and not valid_cusip("46661PVZ8")
    assert "cusip" not in extract_key_terms("CUSIP:: 46661PVZ8")


def test_its_starting_price_and_two_stocks_with_the_same_threshold():
    # HSBC 0001104659-26-113364: one figure for two stocks, "of its starting price".
    t = ("Coupon Threshold Price:: $203.736 with respect to SNOW and $80.406 with respect to NOW, "
         "each of which is 60% of its starting price. Downside Threshold Price:: $203.736 with "
         "respect to SNOW and $80.406 with respect to NOW, each of which is 60% of its starting price.")
    got = extract_key_terms(t)
    assert got["coupon_barrier_pct"].value == 60.0 and got["barrier_pct"].value == 60.0


def test_underlyings_with_different_thresholds_are_left_to_the_model():
    t = ("Downside Threshold Value:: With respect to ABC: $ , which is equal to 70% of its starting "
         "value. With respect to XYZ: $ , which is equal to 60% of its starting value.")
    assert "barrier_pct" not in extract_key_terms(t)


def test_letter_footnotes_with_a_lead_in_and_different_levels():
    # RBC 0000950103-26-015242: "(b)"/"(c)" footnotes and a lead-in phrase;
    # the coupon (60%) and downside (50%) thresholds differ.
    t = ("Ticker Symbol Starting Value(a) Coupon Threshold Value(b) Downside Threshold Value(c) "
         "AMZN UW $ $ $ (a) the closing value on the pricing date (b) With respect to each "
         "Underlying Stock, 60% of its starting value (c) With respect to each Underlying Stock, "
         "50% of its starting value")
    got = extract_key_terms(t)
    assert got["coupon_barrier_pct"].value == 60.0
    assert got["barrier_pct"].value == 50.0


def test_single_colon_terms_when_a_filing_has_no_double_colons():
    # Toronto-Dominion 0001140361-26-039351 prints "Label: value".
    t = ("Issuer: The Toronto-Dominion Bank (the “Bank”).\nCUSIP: 89115NL77\n"
         "Maturity Date*: October 18, 2029, subject to postponement.\n"
         "Downside Threshold Price: With respect to each stock, $ , which is equal to 60% of "
         "its starting price.\nAgent: TD Securities (USA) LLC.\n")
    got = extract_key_terms(t)
    assert got["issuer"].value == "The Toronto-Dominion Bank"
    assert got["cusip"].value == "89115NL77"
    assert got["maturity_date"].value == "2029-10-18"
    assert got["barrier_pct"].value == 60.0


def test_company_issuer_label_and_issuer_stops_at_a_line_break():
    assert extract_key_terms("Company (Issuer):: GS Finance Corp.")["issuer"].value == "GS Finance Corp"
    # RBC: the value ran on into the next line.
    t = "Issuer:: Royal Bank of Canada\nThe common stock of Amazon.com, Inc."
    assert extract_key_terms(t)["issuer"].value == "Royal Bank of Canada"


def test_nothing_readable_means_nothing_returned():
    assert extract_key_terms("This document has no term lines at all.") == {}


if __name__ == "__main__":
    import inspect, sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
        print(f"[ok] {f.__name__}")
    print(f"key_terms gate: PASS ({len(fns)} tests)")
