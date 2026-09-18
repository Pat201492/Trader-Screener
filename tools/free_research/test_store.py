"""Tests for the Free Research local store.

Every test runs against a temporary root, so nothing here can touch a real
`~/.free-research`. Nothing here reaches the network.
"""

import json
import os

import pytest

from tools.free_research import store as store_mod
from tools.free_research.store import PullNotFound, Store, StoreError


@pytest.fixture
def s(tmp_path):
    return Store(home=str(tmp_path / "fr-home"))


# ------------------------------------------------------------------ roots

def test_default_root_is_under_a_dot_free_research_home():
    assert store_mod.DEFAULT_HOME == os.path.join("~", ".free-research")


def test_home_argument_overrides_the_default(tmp_path):
    s = Store(home=str(tmp_path / "elsewhere"))
    assert str(tmp_path) in s.root
    assert os.path.isdir(s.pulls_dir) and os.path.isdir(s.briefs_dir)


def test_rows_path_stays_inside_the_store_root(s):
    assert os.path.commonpath([s.root, s.rows_path("pull_x")]) == s.root


# ------------------------------------------------------------------ pulls

def test_pull_round_trips_every_field(s):
    query = {"forms": ["8-K"], "since": "2025-07-01", "ciks": ["0000320193"]}
    made = s.create_pull(source="edgar", query=query, as_of="2026-09-17T12:00:00+00:00")
    back = s.read_pull(made["id"])
    assert back["source"] == "edgar"
    assert back["query"] == query
    assert back["as_of"] == "2026-09-17T12:00:00+00:00"
    assert back["rows_path"] == s.rows_path(made["id"])


def test_pull_stamps_an_as_of_when_none_is_given(s):
    made = s.create_pull(source="edgar", query={})
    assert made["as_of"].endswith("+00:00")


def test_pull_needs_a_source_and_a_dict_query(s):
    with pytest.raises(StoreError):
        s.create_pull(source="", query={})
    with pytest.raises(StoreError):
        s.create_pull(source="edgar", query=["not", "a", "dict"])


def test_reading_an_absent_pull_raises(s):
    with pytest.raises(StoreError):
        s.read_pull("pull_nope")


def test_list_pulls_is_newest_as_of_first(s):
    s.create_pull("edgar", {}, as_of="2026-01-01T00:00:00+00:00", pull_id="pull_old")
    s.create_pull("edgar", {}, as_of="2026-09-01T00:00:00+00:00", pull_id="pull_new")
    assert [p["id"] for p in s.list_pulls()] == ["pull_new", "pull_old"]


# ------------------------------------------------------------------- rows

def test_rows_are_newline_delimited_json(s):
    p = s.create_pull("edgar", {})
    n = s.write_rows(p["id"], [{"a": 1}, {"a": 2}])
    assert n == 2
    raw = open(p["rows_path"], encoding="utf-8").read()
    assert raw.count("\n") == 2
    assert [json.loads(line) for line in raw.splitlines()] == [{"a": 1}, {"a": 2}]


def test_rows_append_across_calls(s):
    p = s.create_pull("edgar", {})
    s.write_rows(p["id"], [{"a": 1}])
    s.write_rows(p["id"], [{"a": 2}])
    assert s.read_rows(p["id"]) == [{"a": 1}, {"a": 2}]


def test_a_file_truncated_mid_line_reads_back_every_complete_line(s):
    p = s.create_pull("edgar", {})
    s.write_rows(p["id"], [{"a": 1}, {"a": 2}, {"a": 3}])
    raw = open(p["rows_path"], encoding="utf-8").read()
    cut = raw[: raw.rindex("\n") + 1] + '{"a": 4, "partia'
    with open(p["rows_path"], "w", encoding="utf-8", newline="") as fh:
        fh.write(cut)
    assert s.read_rows(p["id"]) == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_reading_rows_of_a_pull_that_wrote_none_is_empty(s):
    p = s.create_pull("edgar", {})
    assert s.read_rows(p["id"]) == []


# ----------------------------------------------------------------- briefs

def test_brief_round_trips_every_field(s):
    p = s.create_pull("edgar", {})
    slots = {"summary": "Two filings.", "by_form": {"columns": ["form"]}}
    made = s.create_brief(pull_id=p["id"], template_id="filing_brief",
                          slots=slots, model="qwen2.5:7b")
    back = s.read_brief(made["id"])
    assert back["pull_id"] == p["id"]
    assert back["template_id"] == "filing_brief"
    assert back["slots"] == slots
    assert back["model"] == "qwen2.5:7b"


def test_brief_against_an_unknown_pull_is_refused_at_write(s):
    with pytest.raises(PullNotFound):
        s.create_brief("pull_nope", "filing_brief", {}, "qwen2.5:7b")


def test_reading_a_brief_whose_pull_vanished_raises_rather_than_part_answering(s):
    p = s.create_pull("edgar", {})
    made = s.create_brief(p["id"], "filing_brief", {"summary": "x"}, "qwen2.5:7b")
    os.remove(os.path.join(s.pulls_dir, "%s.json" % p["id"]))
    with pytest.raises(PullNotFound):
        s.read_brief(made["id"])


def test_brief_needs_a_template_a_dict_of_slots_and_a_model(s):
    p = s.create_pull("edgar", {})
    with pytest.raises(StoreError):
        s.create_brief(p["id"], "", {}, "qwen2.5:7b")
    with pytest.raises(StoreError):
        s.create_brief(p["id"], "filing_brief", ["nope"], "qwen2.5:7b")
    with pytest.raises(StoreError):
        s.create_brief(p["id"], "filing_brief", {}, "")


def test_list_briefs_is_newest_as_of_first(s):
    p = s.create_pull("edgar", {})
    s.create_brief(p["id"], "t", {}, "m", brief_id="brief_old",
                   as_of="2026-01-01T00:00:00+00:00")
    s.create_brief(p["id"], "t", {}, "m", brief_id="brief_new",
                   as_of="2026-09-01T00:00:00+00:00")
    assert [b["id"] for b in s.list_briefs()] == ["brief_new", "brief_old"]


# ---------------------------------------------------------- local-only rule

def test_store_writes_nothing_outside_its_own_root(s, tmp_path):
    p = s.create_pull("edgar", {"forms": ["8-K"]})
    s.write_rows(p["id"], [{"a": 1}])
    s.create_brief(p["id"], "filing_brief", {"summary": "x"}, "qwen2.5:7b")
    written = {
        os.path.join(dirpath, name)
        for dirpath, _, names in os.walk(str(tmp_path))
        for name in names
    }
    assert written, "the test wrote nothing, so it proves nothing"
    assert all(os.path.commonpath([s.root, w]) == s.root for w in written)


def test_store_module_imports_nothing_from_the_shared_pipeline():
    src = open(store_mod.__file__, encoding="utf-8").read()
    for banned in ("stock_data_pipeline", "from pipeline", "import pipeline"):
        assert banned not in src
