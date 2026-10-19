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


def test_floor_rate_is_a_minimum_and_a_range_is_never_the_value():
    # Preliminary supplements: a floor and a range are not the final value.
    t = ("Contingent Coupon Rate:: The contingent coupon rate will be determined on the pricing "
         "date and will be at least 13.50% per annum. "
         "the estimated value of the securities as determined by the Bank would be between "
         "$934.09 (93.409%) and $964.09 (96.409%) per security.")
    got = extract_key_terms(t)
    assert got["contingent_coupon_rate"].bound == "min"
    # No cover legend in this snippet, so the range is not read as preliminary.
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
    assert extract_key_terms("Company (Issuer):: GS Finance Corp.")["issuer"].value == "GS Finance Corp."
    # RBC: the value ran on into the next line.
    t = "Issuer:: Royal Bank of Canada\nThe common stock of Amazon.com, Inc."
    assert extract_key_terms(t)["issuer"].value == "Royal Bank of Canada"


def test_glued_label_words_and_footnote_star():
    # Goldman Sachs 0001193125-26-415340: "Stated MaturityDate*::".
    t = "Stated MaturityDate*:: October 12, 2029, subject to postponement. Agent:: GS&Co."
    assert extract_key_terms(t)["maturity_date"].value == "2029-10-12"


def test_preliminary_rate_floor_is_reported_as_a_minimum():
    t = ("PRELIMINARY PRICING SUPPLEMENT Subject to Completion\n"
         "Contingent Coupon Rate:: The “contingent coupon rate” will be determined on the "
         "pricing date and will be at least 13.50% per annum. Agent:: X.")
    got = extract_key_terms(t)["contingent_coupon_rate"]
    assert (got.value, got.bound) == (13.5, "min")
    # Committed figures carry no bound.
    final = extract_key_terms("Contingent Coupon Rate:: 19.10% per annum. Agent:: X.")
    assert final["contingent_coupon_rate"].bound is None


def test_rate_stated_only_inside_the_coupon_term():
    # Goldman: no rate line; the coupon term states it in parentheses.
    t = ("Contingent Coupon Payment:: you will receive a contingent coupon payment equal to at "
         "least $19 (equivalent to a contingent coupon rate of at least 22.8% per annum) "
         "(set on the trade date). Agent:: X.")
    got = extract_key_terms(t)["contingent_coupon_rate"]
    assert (got.value, got.bound) == (22.8, "min")


def test_preliminary_estimated_value_range_and_stage():
    t = ("Preliminary Pricing Supplement Subject To Completion. The initial estimated value of "
         "the Securities as of the Pricing Date is expected to be between $920.00 and $970.00 "
         "per Security.")
    got = extract_key_terms(t)
    assert got["filing_stage"].value == "preliminary"
    assert got["estimated_value_low"].value == 920.0
    assert got["estimated_value_high"].value == 970.0
    # No point value exists yet: an explicit null, evidenced by the range.
    ev = got["estimated_value_per_1000"]
    assert ev.value is None and ev.raw == "920.00 and $970.00"


def test_preliminary_figure_as_of_today_is_not_the_estimated_value():
    # BMO 0001214659-26-012265: a figure today, a floor at pricing.
    t = ("Subject To Completion. On the date of this preliminary pricing supplement, the "
         "estimated initial value of the securities is $973.00 per security. The estimated "
         "initial value of the securities at pricing may differ from this value but will not "
         "be less than $925.00 per security.")
    got = extract_key_terms(t)
    assert got["estimated_value_per_1000"].value is None
    assert got["estimated_value_low"].value == 925.0
    assert "estimated_value_high" not in got


def test_final_stage_needs_a_committed_estimate():
    t = "The estimated value of the Securities as of the Pricing Date is $962.80 per Security."
    assert extract_key_terms(t)["filing_stage"].value == "final"
    assert "filing_stage" not in extract_key_terms("CUSIP:: 09712GX58 Agent:: X.")


# --- Scale test on the 114 stored filings (2026-10-18) ----------------------

def test_jpmorgan_run_on_single_colon_block():
    # 0001918704-26-0096xx: "Label: value" with no line breaks, a bullet before
    # CUSIP, a slash with no spaces, and a stray nested "::" line (the real
    # filing has 5 of those against 52 single-colon labels).
    t = ("●CUSIP: 46660RS47\n\nKey Terms Issuer: JPMorgan Chase Financial Company LLC, a direct, "
         "wholly owned finance subsidiary of JPMorgan Chase & Co. Guarantor: JPMorgan Chase & Co. "
         "Contingent Interest Rate: 15.00% per annum, payable at a rate of 1.25% per month "
         "Interest Barrier/Trigger Value: 70.00% of the Initial Value, which is 2,456.251 "
         "Pricing Date: April 7, 2026 Original Issue Date (Settlement Date): On or about April 10, "
         "2026 Review Date*: April 8, 2027 Maturity Date*: April 12, 2029 * Subject to postponement\n"
         "●first Review Date:: 12.20% × $1,000\n")
    got = extract_key_terms(t)
    assert got["cusip"].value == "46660RS47"
    assert got["issuer"].value == "JPMorgan Chase Financial Company LLC"
    assert got["contingent_coupon_rate"].value == 15.0
    assert got["coupon_barrier_pct"].value == 70.0 and got["barrier_pct"].value == 70.0
    assert got["maturity_date"].value == "2029-04-12"


def test_footnote_dagger_and_numbered_footnotes():
    assert extract_key_terms("Maturity Date†:: April 9, 2026 † Subject to postponement. "
                             "Agent:: X")["maturity_date"].value == "2026-04-09"
    assert extract_key_terms("Stated Maturity Date1, 2:: January 27, 2028 Principal Amount:: "
                             "$1,000")["maturity_date"].value == "2028-01-27"


def test_jpmorgan_represents_and_separate_interest_barrier():
    t = "Interest Barrier / Trigger Level:: An amount that represents 70.00% of the Initial Stock Price"
    assert extract_key_terms(t)["barrier_pct"].value == 70.0
    t = ("Interest Barrier: 70.00% of the Initial Value\nTrigger Value: 60.00% of the Initial Value\n"
         "Pricing Date: April 15, 2026\nIssuer: X\nAgent: Y\nCUSIP: 46660RS47\n")
    got = extract_key_terms(t)
    assert got["coupon_barrier_pct"].value == 70.0 and got["barrier_pct"].value == 60.0


def test_citi_comma_lead_in_and_rate_in_parentheses():
    t = ("Maturity date:: Unless earlier automatically redeemed, August 31, 2028 "
         "Contingent coupon:: the securities will pay a contingent coupon equal to 4.075% of the "
         "stated principal amount of the securities (16.30% per annum) if and only if the closing "
         "price of the underlying shares is greater than or equal to the downside threshold price. "
         "Downside threshold price:: $45.012, 60.00% of the initial share price Listing:: none")
    got = extract_key_terms(t)
    assert got["contingent_coupon_rate"].value == 16.3      # not the quarterly 4.075
    assert got["barrier_pct"].value == 60.0
    assert got["coupon_barrier_pct"].value == 60.0          # the coupon uses the same threshold


def test_citi_rounded_annual_rate_is_committed():
    t = ("Contingent coupon:: the securities will pay a contingent coupon equal to 0.8542% of the "
         "stated principal amount (equivalent to a contingent coupon rate of approximately 10.25% "
         "per annum) if and only if ... Listing:: none")
    got = extract_key_terms(t)["contingent_coupon_rate"]
    assert (got.value, got.bound) == (10.25, None)


def test_citi_star_footnote_table():
    t = ("Underlyings:: Underlying Initial underlying value* Coupon barrier value** Final barrier "
         "value*** Dow Jones Industrial Average Nasdaq-100 Index *For each underlying, its closing "
         "value on the pricing date **For each underlying, 70.00% of its initial underlying value "
         "***For each underlying, 55.00% of its initial underlying value\nStated principal amount:: $1,000")
    got = extract_key_terms(t)
    assert got["coupon_barrier_pct"].value == 70.0
    assert got["barrier_pct"].value == 55.0


def test_prose_with_a_colon_is_not_a_label():
    # Toronto-Dominion: "...is less than its downside threshold price: $1,000" mid-sentence.
    t = ("Issuer: TD\nCUSIP: 89115NL77\nAgent: TD\nMaturity Date: October 18, 2029\n"
         "Payment: if the ending price is greater than or equal to its downside threshold price: "
         "$1,000; or if less\nDownside Threshold Price: $ , which is equal to 60% of its starting price.\n")
    assert extract_key_terms(t)["barrier_pct"].value == 60.0


# --- More fields off the term lines (2026-10-18) ---------------------------

def test_guarantor_calculation_agent_isin_denomination():
    t = ("Issuer:: JPMorgan Chase Financial Company LLC, a direct, wholly owned finance subsidiary "
         "of JPMorgan Chase & Co.\nGuarantor:: JPMorgan Chase & Co.\n"
         "Calculation Agent:: J.P. Morgan Securities LLC (“JPMS”)\n"
         "CUSIP / ISIN:: 17334CV68 / US17334CV683\nDenominations:: $1,000 and any integral "
         "multiple of $1,000.\nAgent:: X\n")
    got = extract_key_terms(t)
    assert got["guarantor"].value == "JPMorgan Chase & Co."
    assert got["calculation_agent"].value == "J.P. Morgan Securities LLC"   # "J.P." is not a sentence end
    assert got["isin"].value == "US17334CV683"
    assert got["denomination"].value == 1000.0


def test_isin_check_digit():
    from key_terms import valid_isin
    assert valid_isin("US0378331005") and not valid_isin("US0378331006")


def test_citi_guarantee_sentence():
    t = ("Guarantee:: All payments due on the securities are fully and unconditionally guaranteed "
         "by Citigroup Inc.\nStated principal amount:: $1,000 per security\n")
    got = extract_key_terms(t)
    assert got["guarantor"].value == "Citigroup Inc."
    assert got["denomination"].value == 1000.0


def test_defined_abbreviation_resolves_to_the_entity():
    # BofA: "Guarantor:: BAC." after "Bank of America Corporation (“BAC” or the “Guarantor”)".
    t = ("issued by BofA Finance LLC (“BofA Finance”), and Bank of America Corporation (“BAC” or "
         "the “Guarantor”).\nIssuer:: BofA Finance LLC.\nGuarantor:: BAC.\nAgent:: X\n")
    got = extract_key_terms(t)
    assert got["guarantor"].value == "Bank of America Corporation"
    assert t[got["guarantor"].span[0]:got["guarantor"].span[1]] == "Bank of America Corporation"
    # An abbreviation nothing defines is not reported.
    assert "guarantor" not in extract_key_terms("Guarantor:: XYZ.\nAgent:: X\n")


def test_coupon_frequency_from_dates_line_or_rate():
    t = "Contingent Coupon Payment Dates:: Quarterly, on the third business day following each calculation day"
    assert extract_key_terms(t)["coupon_frequency"].value == "quarterly"
    t = ("Contingent Interest Rate: 11.55% per annum, payable at a rate of 0.9625% per month\n"
         "Pricing Date: April 7, 2026\nIssuer: X\n")
    assert extract_key_terms(t)["coupon_frequency"].value == "monthly"


def test_autocall_level_buffer_and_leverage():
    t = ("Call Value: With respect to each Index, 90.00% of its Initial Value\n"
         "Buffer Amount: 20.00%\nPricing Date: April 7, 2026\nIssuer: X\n")
    got = extract_key_terms(t)
    assert got["autocall_barrier_pct"].value == 90.0
    assert got["buffer_pct"].value == 20.0
    assert "barrier_pct" not in got                 # a call level is never the downside barrier
    t = "Downside Leverage Factor:: 1.42857 Automatic Call:: If ..."
    assert extract_key_terms(t)["downside_leverage"].value == 1.42857


# --- Underlyings (2026-10-18) ------------------------------------------------

def _und(t):
    return extract_key_terms(t)["underlyings"].value


def test_underlyings_with_tickers():
    assert _und("Underlying Stock:: The common stock of Micron Technology, Inc. (Bloomberg ticker: MU) "
                "(the “Underlying Stock”). We refer to the issuer...") == ["Micron Technology, Inc."]
    assert _und("Indices:: S&P 500® Index (Bloomberg ticker: SPX), Russell 2000® Index (Bloomberg "
                "ticker: RTY) and Dow Jones Industrial Average® (Bloomberg ticker: INDU) (each referred "
                "to as an “Index”)") == ["S&P 500® Index", "Russell 2000® Index", "Dow Jones Industrial Average®"]
    # Parentheticals between members; an ETF after an index.
    assert _und("Underlyings: The Russell 2000® Index (Bloomberg ticker: RTY) (the “Index”) and the "
                "iShares® MSCI EAFE ETF (Bloomberg ticker: EFA) (the “Fund”) (each ...)\nIssuer: X\n"
                "Agent: Y\n") == ["Russell 2000® Index", "iShares® MSCI EAFE ETF"]
    assert _und("Reference Stock:: The common stock of Oracle Corporation, par value $0.01 per share "
                "(Bloomberg ticker: ORCL). We refer to Oracle") == ["Oracle Corporation"]
    assert _und("Underlying Stock:: The Class A common stock of Space Exploration Technologies Corp. "
                "(Nasdaq Global Select Market symbol: “SPCX”) (the “Underlying Stock”).") == \
        ["Space Exploration Technologies Corp."]


def test_underlyings_without_tickers():
    assert _und("Market Measure:: Common stock of GE Vernova Inc. (the “Underlying Stock”).") == ["GE Vernova Inc."]
    assert _und("Underlyings:: The State Street® Energy Select Sector SPDR® ETF, the State Street® "
                "Financial Select Sector SPDR® ETF and the State Street® Technology Select Sector SPDR® "
                "ETF (each referred to as an “underlying”)") == [
        "State Street® Energy Select Sector SPDR® ETF", "State Street® Financial Select Sector SPDR® ETF",
        "State Street® Technology Select Sector SPDR® ETF"]
    assert _und("Market Measure:: Common stock of Eli Lilly and Company (the “Underlying Stock”).") == \
        ["Eli Lilly and Company"]                     # a bare "and" is not a member boundary


def test_underlyings_from_tables():
    # Citi index table: names run together with their levels.
    t = ("Underlyings:: Underlying Initial underlying value* Coupon barrier value** Final barrier value*** "
         "Nasdaq-100 Index® 29,433.43 20,603.401 17,660.058 Russell 2000® Index 2,972.372 2,080.660 "
         "1,783.423 S&P 500® Index 7,711.76 5,398.232 4,627.056 *For each underlying, its closing value")
    assert _und(t) == ["Nasdaq-100 Index®", "Russell 2000® Index", "S&P 500® Index"]
    # BMO: one "Market Measures::" row per stock.
    t = ("Market Measure Bloomberg Ticker Symbol Starting Value(1) Coupon Threshold Value(2) Downside "
         "Threshold Value(3)\nMarket Measures:: The Common Stock of D.R. Horton, Inc. DHI $127.15 $89.005 $89.005\n"
         "Market Measures:: The Common Shares of PulteGroup, Inc. PHM $104.44 $73.108 $73.108\n\n(1) With respect")
    assert _und(t) == ["D.R. Horton, Inc.", "PulteGroup, Inc."]


def test_least_performing_definition_is_not_an_underlying():
    t = "Least Performing Index:: The Index with the Least Performing Index Return Agent:: X"
    assert "underlyings" not in extract_key_terms(t)


def test_unreadable_table_is_left_to_the_model():
    # RBC prints tickers only.
    t = ("Market Measures:: Market Measure Bloomberg Ticker Symbol Starting Value(a) Coupon Threshold "
         "Value(b) Downside Threshold Value(c) Market Measures:: AMZN Stock AMZN UW $ $ $")
    assert "underlyings" not in extract_key_terms(t)


def test_nothing_readable_means_nothing_returned():
    assert extract_key_terms("This document has no term lines at all.") == {}


if __name__ == "__main__":
    import inspect, sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
        print(f"[ok] {f.__name__}")
    print(f"key_terms gate: PASS ({len(fns)} tests)")
