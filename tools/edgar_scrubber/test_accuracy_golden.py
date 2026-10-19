"""
The accuracy suite's golden set as a test, so `pytest tools/edgar_scrubber`
fails when any extractor change breaks a hand-checked value: 17 424B2 notes
(19 fields each) and 10 XML filings (Forms 4/4A, 13F-HR, Schedule 13G/13G-A,
Form D), all checked against the filings themselves. Offline; no network.

Full suite (golden + cross-checks + sanity sweep + live canary):
    cd tools/edgar_scrubber && python -m accuracy all
"""
from accuracy import checks


def test_golden_values_are_all_correct():
    rep = checks.golden()
    wrong = [f"{where}: {msg}" for sev, where, msg in rep.findings if sev == "FAIL"]
    assert not wrong, "golden values changed:\n" + "\n".join(wrong[:40])
    assert rep.counts["ok"] >= 400


def test_13f_holdings_match_the_cover_page_totals():
    rep = checks.cross()
    bad = [f"{w}: {m}" for s, w, m in rep.findings if s == "FAIL" and w.startswith("13F")]
    assert not bad, "\n".join(bad)
