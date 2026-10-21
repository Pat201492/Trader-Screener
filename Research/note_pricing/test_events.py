"""Company event calendar (#366). No network: the submissions feed is a stub.

Run:  python -m pytest Research/note_pricing/test_events.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import events as ev  # noqa: E402


class StubClient:
    def __init__(self):
        self.json_calls, self.sub_calls = [], 0

    def ticker_to_cik(self, t):
        if t != "NVDA":
            raise LookupError(t)
        return "0001045810"

    def submissions(self, cik):
        self.sub_calls += 1
        return {"filings": {
            "recent": {"form": ["8-K", "10-Q", "4", "8-K", "DEF 14A", "8-K"],
                       "filingDate": ["2026-08-27", "2026-08-27", "2026-08-20", "2026-06-26", "2026-05-12",
                                      "2026-05-28"],
                       "accessionNumber": ["a1", "a2", "a3", "a4", "a5", "a6"],
                       "items": ["2.02,9.01", "", "", "5.07", "", "7.01,2.02"]},
            "files": [{"name": "CIK0001045810-submissions-001.json", "filingFrom": "2023-01-01",
                       "filingTo": "2025-12-31"},
                      {"name": "CIK0001045810-submissions-002.json", "filingFrom": "2019-01-01",
                       "filingTo": "2022-12-31"}]}}

    def get_json(self, url):
        self.json_calls.append(url)
        return {"form": ["10-K"], "filingDate": ["2025-02-26"], "accessionNumber": ["old"], "items": [""]}

    def archive_document(self, *a, **k):
        raise AssertionError("events are metadata only")


def test_classify():
    assert ev.classify("8-K", "2.02,9.01") == ["earnings"]
    assert ev.classify("8-K", "7.01,2.02") == ["presentation", "earnings"]
    assert ev.classify("8-K", "5.07") == ["shareholder_meeting"]
    assert ev.classify("10-q", "") == ["quarterly_report"] and ev.classify("DEF 14A", "") == ["proxy"]
    assert ev.classify("4", "") == [] and ev.classify("8-K", "8.01") == []


def test_events_for_a_ticker_metadata_only_with_older_pages(tmp_path):
    c = StubClient()
    got = ev.events_for_ticker(c, "nvda", "2025-01-01", "2026-12-31", home=tmp_path)
    assert [(e["date"], e["type"]) for e in got] == [
        ("2025-02-26", "annual_report"), ("2026-05-12", "proxy"), ("2026-05-28", "earnings"),
        ("2026-05-28", "presentation"), ("2026-06-26", "shareholder_meeting"),
        ("2026-08-27", "earnings"), ("2026-08-27", "quarterly_report")]
    # The page ending 2022 predates the window and is not read.
    assert c.json_calls == ["https://data.sec.gov/submissions/CIK0001045810-submissions-001.json"]


def test_events_are_cached(tmp_path):
    c = StubClient()
    ev.events_for_ticker(c, "NVDA", "2026-01-01", "2026-12-31", home=tmp_path)
    ev.events_for_ticker(c, "NVDA", "2026-06-01", "2026-06-30", home=tmp_path)
    assert c.sub_calls == 1


def test_indices_and_unknown_tickers_have_no_events(tmp_path):
    c = StubClient()
    assert ev.events_for_ticker(c, "SPY", "2026-01-01", "2026-12-31", home=tmp_path) == []
    assert ev.events_for_ticker(c, "ZZZZ", "2026-01-01", "2026-12-31", home=tmp_path) == []
    assert c.sub_calls == 0
