"""Tests for the slot filler.

Driven by a fake model client, in the style of `_FakeClient` in
`tools/edgar_scrubber/test_extraction_ladder.py`. Nothing here reaches the
network, and no Ollama process need be running.
"""

import json

import pytest

from tools.free_research import fill as fill_mod
from tools.free_research.fill import FillError, NotLocalError, assert_local, fill
from tools.free_research.store import Store

ROWS = [
    {"form": "8-K", "filing_date": "2025-10-30", "company": "APPLE INC",
     "accession": "0000320193-25-000101"},
    {"form": "10-Q", "filing_date": "2025-08-01", "company": "APPLE INC",
     "accession": "0000320193-25-000102"},
]


class FakeClient:
    """Answers per slot kind, and a log of every prompt it was handed."""

    class _Cfg:
        model = "qwen2.5:7b-instruct-q4_K_M"

    def __init__(self, answers):
        self.answers = answers
        self.prompts = []
        self.formats = []
        self.config = self._Cfg()

    def chat_completion(self, messages, model=None, response_format=None, **kw):
        self.prompts.append(messages[-1]["content"])
        self.formats.append(response_format)
        kind = response_format["json_schema"]["name"]
        answer = self.answers[kind]
        if callable(answer):
            answer = answer(len([f for f in self.formats
                                 if f["json_schema"]["name"] == kind]) - 1)
        return {"choices": [{"message": {"content": json.dumps(answer)}}]}


def template(*slots):
    return {"id": "t", "slots": list(slots)}


PROSE = {"name": "summary", "kind": "prose",
         "columns": ["form", "filing_date"], "max_words": 30}
TABLE = {"name": "filings", "kind": "table",
         "columns": ["form", "filing_date", "accession"]}
CHART = {"name": "by_form", "kind": "chart", "columns": ["form", "filing_date"],
         "mark": "bar", "x": "form", "y": "filing_date"}


@pytest.fixture
def store(tmp_path):
    return Store(home=str(tmp_path / "fr"))


@pytest.fixture
def pull(store):
    rec = store.create_pull("edgar", {})
    store.write_rows(rec["id"], ROWS)
    return rec


# ------------------------------------------------------------ local only

def test_a_local_base_url_is_accepted():
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1"):
        assert assert_local(url)


def test_a_remote_base_url_is_refused():
    with pytest.raises(NotLocalError):
        assert_local("https://api.anthropic.com/v1")


def test_no_code_path_reaches_a_billed_anthropic_endpoint():
    src = open(fill_mod.__file__, encoding="utf-8").read()
    for banned in ("anthropic", "for_claude", "ANTHROPIC"):
        assert banned not in src, "fill.py mentions %r" % banned


# ------------------------------------------------------- one slot at a time

def test_each_call_sees_only_the_columns_its_slot_declares(store, pull):
    client = FakeClient({"prose": {"text": "Two filings."},
                         "table": {"columns": ["form"]},
                         "chart": {"x": "form", "y": "filing_date"}})
    fill(store, client, pull["id"], template(PROSE, TABLE, CHART))
    prose_prompt = client.prompts[0]
    assert "company" not in prose_prompt
    assert "accession" not in prose_prompt
    assert "form" in prose_prompt


def test_every_call_is_schema_constrained(store, pull):
    client = FakeClient({"prose": {"text": "Two filings."},
                         "table": {"columns": ["form"]},
                         "chart": {"x": "form", "y": "filing_date"}})
    fill(store, client, pull["id"], template(PROSE, TABLE, CHART))
    assert [f["json_schema"]["name"] for f in client.formats] == \
        ["prose", "table", "chart"]


# ----------------------------------------------------------------- prose

def test_a_prose_value_within_its_bound_is_kept(store, pull):
    client = FakeClient({"prose": {"text": "An 8-K and a 10-Q."}})
    brief = fill(store, client, pull["id"], template(PROSE))
    assert brief["slots"]["summary"] == "An 8-K and a 10-Q."
    assert brief["dropped"] == []


def test_a_prose_value_over_its_word_count_is_dropped_not_truncated(store, pull):
    client = FakeClient({"prose": {"text": "word " * 40}})
    brief = fill(store, client, pull["id"], template(PROSE))
    assert "summary" not in brief["slots"]
    assert brief["dropped"][0]["slot"] == "summary"
    assert "maximum" in brief["dropped"][0]["reason"]


def test_a_number_the_rows_do_not_contain_drops_the_slot(store, pull):
    client = FakeClient({"prose": {"text": "Filings totalled $4,500,000."}})
    brief = fill(store, client, pull["id"], template(PROSE))
    assert "summary" not in brief["slots"]
    assert "4,500,000" in brief["dropped"][0]["reason"]


def test_a_number_that_is_in_the_rows_is_grounded(store, pull):
    client = FakeClient({"prose": {"text": "The 10-Q landed 2025-08-01."}})
    brief = fill(store, client, pull["id"], template(PROSE))
    assert brief["slots"]["summary"].endswith("2025-08-01.")


def test_an_empty_prose_answer_is_dropped(store, pull):
    client = FakeClient({"prose": {"text": "   "}})
    brief = fill(store, client, pull["id"], template(PROSE))
    assert brief["dropped"][0]["reason"] == "model returned no text"


# ----------------------------------------------------------------- table

def test_a_table_naming_present_columns_is_kept(store, pull):
    client = FakeClient({"table": {"columns": ["form", "filing_date"]}})
    brief = fill(store, client, pull["id"], template(TABLE))
    assert brief["slots"]["filings"] == {"columns": ["form", "filing_date"]}


def test_a_table_naming_an_absent_column_is_dropped_and_reported(store, pull):
    client = FakeClient({"table": {"columns": ["form", "not_a_column"]}})
    brief = fill(store, client, pull["id"], template(TABLE))
    assert "filings" not in brief["slots"]
    assert "not_a_column" in brief["dropped"][0]["reason"]


# ----------------------------------------------------------------- chart

def test_a_chart_naming_present_columns_is_kept(store, pull):
    client = FakeClient({"chart": {"x": "form", "y": "filing_date"}})
    brief = fill(store, client, pull["id"], template(CHART))
    assert brief["slots"]["by_form"] == {"mark": "bar", "x": "form",
                                         "y": "filing_date"}


def test_a_chart_naming_an_absent_column_is_dropped_and_reported(store, pull):
    client = FakeClient({"chart": {"x": "form", "y": "invented"}})
    brief = fill(store, client, pull["id"], template(CHART))
    assert "by_form" not in brief["slots"]
    assert "invented" in brief["dropped"][0]["reason"]


# ----------------------------------------------------------------- brief

def test_the_brief_records_the_model_that_filled_it(store, pull):
    client = FakeClient({"prose": {"text": "An 8-K and a 10-Q."}})
    brief = fill(store, client, pull["id"], template(PROSE))
    assert brief["model"] == "qwen2.5:7b-instruct-q4_K_M"
    assert store.read_brief(brief["id"])["model"] == "qwen2.5:7b-instruct-q4_K_M"


def test_a_partly_dropped_brief_still_stores_what_survived(store, pull):
    client = FakeClient({"prose": {"text": "Totalling $9,999,999."},
                         "table": {"columns": ["form"]}})
    brief = fill(store, client, pull["id"], template(PROSE, TABLE))
    assert list(brief["slots"]) == ["filings"]
    assert [d["slot"] for d in brief["dropped"]] == ["summary"]


def test_filling_against_an_unknown_pull_is_refused(store):
    client = FakeClient({"prose": {"text": "x"}})
    with pytest.raises(FillError):
        fill(store, client, "pull_nope", template(PROSE))


def test_a_non_json_model_answer_is_an_error_not_a_silent_empty_slot(store, pull):
    class Broken(FakeClient):
        def chat_completion(self, messages, model=None, response_format=None, **kw):
            return {"choices": [{"message": {"content": "not json"}}]}

    with pytest.raises(FillError):
        fill(store, Broken({}), pull["id"], template(PROSE))


def test_an_invalid_template_is_refused_before_any_model_call(store, pull):
    client = FakeClient({"prose": {"text": "x"}})
    bad = template({"name": "s", "kind": "prose", "columns": ["form"]})
    with pytest.raises(Exception):
        fill(store, client, pull["id"], bad)
    assert client.prompts == []
