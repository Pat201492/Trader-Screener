"""Tests for the EDGAR source.

Driven by a recorded fixture through the client's injectable transport, so
nothing here touches the network.
"""

import json

import pytest

from tools.edgar_scrubber.edgar_client import EdgarClient
from tools.free_research.sources import edgar as edgar_src
from tools.free_research.sources.edgar import EdgarSourceError
from tools.free_research.store import Store

UA = "Test Tester test@example.com"

SUBMISSIONS = {
    "name": "APPLE INC",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-25-000101",
                                "0000320193-25-000102",
                                "0000320193-24-000099"],
            "form": ["8-K", "10-Q", "8-K"],
            "filingDate": ["2025-10-30", "2025-08-01", "2024-11-01"],
            "primaryDocument": ["aapl-8k.htm", "aapl-10q.htm", ""],
        }
    },
}

TSLA_SUBMISSIONS = {
    "name": "Tesla, Inc.",
    "filings": {
        "recent": {
            "accessionNumber": ["0001318605-25-000044"],
            "form": ["8-K"],
            "filingDate": ["2025-07-15"],
            "primaryDocument": ["tsla-8k.htm"],
        }
    },
}

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
}


class FakeTransport:
    """(status, headers, body) per URL, plus a log of what was asked for."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        for fragment, payload in self.bodies.items():
            if fragment in url:
                return 200, {}, json.dumps(payload).encode("utf-8")
        return 404, {}, b"not found"


@pytest.fixture
def client(tmp_path):
    transport = FakeTransport({
        "submissions/CIK0000320193": SUBMISSIONS,
        "submissions/CIK0001318605": TSLA_SUBMISSIONS,
        "company_tickers.json": TICKERS,
    })
    c = EdgarClient(user_agent=UA, cache_dir=str(tmp_path / "cache"),
                    transport=transport)
    c.fake = transport
    return c


@pytest.fixture
def store(tmp_path):
    return Store(home=str(tmp_path / "fr"))


# ------------------------------------------------------------------- rows

def test_every_row_carries_the_five_stated_fields(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"])
    rows = store.read_rows(rec["id"])
    assert rows
    for row in rows:
        for field in ("accession", "cik", "form", "filing_date", "doc_url"):
            assert field in row


def test_rows_are_as_fetched_with_no_extraction_or_scoring(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"])
    rows = store.read_rows(rec["id"])
    allowed = {"accession", "cik", "company", "form", "filing_date", "doc_url"}
    for row in rows:
        assert set(row) <= allowed, "the adapter invented a field"
    first = [r for r in rows if r["accession"] == "0000320193-25-000101"][0]
    assert first["form"] == "8-K"
    assert first["filing_date"] == "2025-10-30"
    assert first["cik"] == "0000320193"
    assert first["doc_url"].endswith("/000032019325000101/aapl-8k.htm")


def test_a_filing_with_no_primary_document_gets_an_empty_url_not_a_guess(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"])
    row = [r for r in store.read_rows(rec["id"])
           if r["accession"] == "0000320193-24-000099"][0]
    assert row["doc_url"] == ""


# ---------------------------------------------------------------- filters

def test_the_form_filter_is_applied(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"], forms=["8-K"])
    assert {r["form"] for r in store.read_rows(rec["id"])} == {"8-K"}


def test_the_date_range_is_applied_at_both_ends(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"],
                         since="2025-01-01", until="2025-09-01")
    dates = [r["filing_date"] for r in store.read_rows(rec["id"])]
    assert dates == ["2025-08-01"]


# ------------------------------------------------------------------ query

def test_the_pull_record_keeps_the_query_exactly_as_asked(store, client):
    rec = edgar_src.pull(store, client, forms=["8-K", "10-Q"], since="2025-07-01",
                         until="2026-01-01", ciks=["320193"], tickers=["TSLA"])
    q = store.read_pull(rec["id"])["query"]
    assert q["forms"] == ["8-K", "10-Q"]
    assert q["since"] == "2025-07-01"
    assert q["until"] == "2026-01-01"
    assert q["ciks"] == ["320193"]
    assert q["tickers"] == ["TSLA"]


def test_rows_land_at_the_pull_records_own_rows_path(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"])
    assert rec["rows_path"] == store.rows_path(rec["id"])
    assert store.read_rows(rec["id"])


def test_the_source_id_is_recorded(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193"])
    assert store.read_pull(rec["id"])["source"] == "edgar"


# ---------------------------------------------------------------- tickers

def test_a_ticker_resolves_to_a_cik_through_the_clients_own_fetch(store, client):
    found, missing = edgar_src.resolve_tickers(client, ["AAPL"])
    assert found == {"AAPL": "0000320193"}
    assert missing == []
    assert any("company_tickers.json" in u for u in client.fake.urls)


def test_an_unresolvable_ticker_is_reported_rather_than_dropped(store, client):
    rec = edgar_src.pull(store, client, tickers=["AAPL", "NOSUCH"])
    assert rec["unresolved_tickers"] == ["NOSUCH"]


def test_a_cik_named_twice_is_fetched_once(store, client):
    rec = edgar_src.pull(store, client, ciks=["320193", "0000320193"])
    subs = [u for u in client.fake.urls if "submissions/CIK" in u]
    assert len(subs) == 1
    assert rec["row_count"] == 3


def test_a_pull_naming_neither_cik_nor_ticker_is_refused(store, client):
    with pytest.raises(EdgarSourceError):
        edgar_src.pull(store, client)


# --------------------------------------------------------- one client only

def test_the_adapter_defines_no_http_of_its_own():
    src = open(edgar_src.__file__, encoding="utf-8").read()
    for banned in ("import requests", "urllib.request", "http.client",
                   "urlopen", "socket"):
        assert banned not in src, "the adapter reaches the network on its own"
    assert "from tools.edgar_scrubber.edgar_client import" in src
