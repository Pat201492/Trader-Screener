"""Tests for the structured-XML forms store (issue #283). No network."""

from pathlib import Path

import pytest

from forms_store import FormsStore, FormsStoreError
from xml_forms import read_filing

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
FORM4 = (FIXTURES / "form4_cook.xml").read_text(encoding="utf-8")


def test_round_trips_documents_and_rows():
    rows = read_filing("4", FORM4)
    with FormsStore(":memory:") as store:
        store.put_filing("run-1", "0000320193-26-000031", "4", "320193",
                         "2026-03-03", rows)
        back = store.rows_for("run-1", "0000320193-26-000031")
    assert back == rows  # every row survives the round trip, in order


def test_query_filters_by_form_cik_and_filed_date():
    rows = read_filing("4", FORM4)
    with FormsStore(":memory:") as store:
        store.put_filing("r", "acc-aapl-4", "4", "320193", "2026-03-03", rows)
        store.put_filing("r", "acc-aapl-3", "3", "320193", "2026-02-01", rows)
        store.put_filing("r", "acc-msft-4", "4", "789019", "2026-05-10", rows)

        # by form
        got = store.query("4")
        assert {d["accession"] for d in got} == {"acc-aapl-4", "acc-msft-4"}
        # the attached rows round-trip too
        assert got[0]["rows"] == rows

        # by form + cik (CIK is normalised to the padded form)
        got = store.query("4", cik="320193")
        assert [d["accession"] for d in got] == ["acc-aapl-4"]
        assert store.query("4", cik="0000320193")[0]["accession"] == "acc-aapl-4"

        # by filed date range
        got = store.query("4", date_from="2026-04-01", date_to="2026-12-31")
        assert [d["accession"] for d in got] == ["acc-msft-4"]
        assert store.query("4", date_to="2026-01-01") == []


def test_reingest_replaces_rows_without_leaving_stragglers():
    with FormsStore(":memory:") as store:
        store.put_filing("r", "acc", "4", "320193", "2026-03-03",
                         [{"a": 1}, {"a": 2}, {"a": 3}])
        store.put_filing("r", "acc", "4", "320193", "2026-03-03", [{"a": 9}])
        assert store.rows_for("r", "acc") == [{"a": 9}]
        assert len(store.query("4")) == 1


def test_persists_to_disk(tmp_path):
    path = tmp_path / "forms.sqlite"
    rows = read_filing("4", FORM4)
    with FormsStore(path) as store:
        store.put_filing("r", "acc", "4", "320193", "2026-03-03", rows)
    with FormsStore(path, readonly=True) as reopened:
        assert reopened.rows_for("r", "acc") == rows
        assert reopened.query("4")[0]["accession"] == "acc"


def test_readonly_guard_refuses_writes():
    with FormsStore(":memory:", readonly=True) as store:
        with pytest.raises(FormsStoreError):
            store.put_filing("r", "acc", "4", "320193", "2026-03-03", [])
