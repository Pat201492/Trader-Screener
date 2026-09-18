"""Tests for the brief template format and its validator.

One accepting and one rejecting case per rule. Nothing here reaches the network.
"""

import json
import os

import pytest

from tools.free_research import template as tpl_mod
from tools.free_research.template import TemplateError, load, load_by_id, validate


def prose(**over):
    slot = {"name": "summary", "kind": "prose", "columns": ["form"], "max_words": 50}
    slot.update(over)
    return slot


def table(**over):
    slot = {"name": "filings", "kind": "table", "columns": ["form", "filing_date"]}
    slot.update(over)
    return slot


def chart(**over):
    slot = {"name": "by_month", "kind": "chart", "columns": ["month", "n"],
            "mark": "bar", "x": "month", "y": "n"}
    slot.update(over)
    return slot


def tmpl(*slots, **over):
    t = {"id": "t", "slots": list(slots)}
    t.update(over)
    return t


# --------------------------------------------------------------- loading

def test_load_returns_slots_in_document_order(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps(tmpl(prose(), table(), chart())), encoding="utf-8")
    got = tpl_mod.slots(load(str(path)))
    assert [s["name"] for s in got] == ["summary", "filings", "by_month"]


def test_load_rejects_a_file_that_is_not_json(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TemplateError):
        load(str(path))


# ----------------------------------------------------------------- kinds

def test_the_three_kinds_are_accepted():
    assert validate(tmpl(prose(), table(), chart()))


def test_an_unknown_kind_is_rejected_by_name():
    with pytest.raises(TemplateError) as exc:
        validate(tmpl(prose(kind="timeline")))
    assert "timeline" in str(exc.value)


# --------------------------------------------------------------- columns

def test_a_slot_declaring_columns_is_accepted():
    assert validate(tmpl(table(columns=["form"])))


def test_a_slot_declaring_no_readable_columns_is_rejected():
    with pytest.raises(TemplateError) as exc:
        validate(tmpl(table(columns=[])))
    assert "columns" in str(exc.value)
    with pytest.raises(TemplateError):
        validate(tmpl(table(columns=None)))


# ------------------------------------------------------------ prose bound

def test_a_prose_slot_with_a_max_word_count_is_accepted():
    assert validate(tmpl(prose(max_words=10)))


def test_a_prose_slot_without_a_max_word_count_is_rejected():
    slot = prose()
    del slot["max_words"]
    with pytest.raises(TemplateError) as exc:
        validate(tmpl(slot))
    assert "max_words" in str(exc.value)


def test_a_prose_max_word_count_must_be_a_positive_integer():
    for bad in (0, -5, "many", 12.5, True):
        with pytest.raises(TemplateError):
            validate(tmpl(prose(max_words=bad)))


# ------------------------------------------------------ chart names columns

def test_a_chart_naming_declared_columns_is_accepted():
    assert validate(tmpl(chart(x="month", y="n")))


def test_a_chart_carrying_a_literal_number_is_rejected():
    for bad in (42, 3.5, 0):
        with pytest.raises(TemplateError) as exc:
            validate(tmpl(chart(y=bad)))
        assert "names a column" in str(exc.value)


def test_a_chart_plotting_a_column_it_did_not_declare_is_rejected():
    with pytest.raises(TemplateError) as exc:
        validate(tmpl(chart(y="undeclared")))
    assert "undeclared" in str(exc.value)


def test_a_chart_without_a_mark_or_an_axis_is_rejected():
    slot = chart()
    del slot["mark"]
    with pytest.raises(TemplateError):
        validate(tmpl(slot))
    slot = chart()
    del slot["y"]
    with pytest.raises(TemplateError):
        validate(tmpl(slot))


# ---------------------------------------------------------------- shape

def test_a_template_needs_an_id_and_slots():
    with pytest.raises(TemplateError):
        validate({"slots": [prose()]})
    with pytest.raises(TemplateError):
        validate({"id": "t", "slots": []})


def test_a_repeated_slot_name_is_rejected():
    with pytest.raises(TemplateError) as exc:
        validate(tmpl(prose(name="a"), table(name="a")))
    assert "twice" in str(exc.value)


def test_a_slot_without_a_name_is_rejected():
    slot = table()
    del slot["name"]
    with pytest.raises(TemplateError):
        validate(tmpl(slot))


# --------------------------------------------------------- shipped template

def test_the_shipped_filing_brief_validates_and_has_each_kind():
    shipped = load_by_id("filing_brief")
    kinds = {s["kind"] for s in tpl_mod.slots(shipped)}
    assert kinds == {"prose", "table", "chart"}


def test_shipped_templates_all_validate():
    listed = tpl_mod.list_templates()
    assert [t["id"] for t in listed] == ["filing_brief"]


def test_list_templates_skips_a_broken_file_rather_than_hiding_the_rest(tmp_path):
    (tmp_path / "good.json").write_text(
        json.dumps({"id": "good", "slots": [table()]}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert [t["id"] for t in tpl_mod.list_templates(str(tmp_path))] == ["good"]


def test_load_by_id_rejects_a_file_whose_declared_id_disagrees(tmp_path):
    (tmp_path / "named.json").write_text(
        json.dumps({"id": "other", "slots": [table()]}), encoding="utf-8")
    with pytest.raises(TemplateError):
        load_by_id("named", str(tmp_path))


def test_templates_dir_is_inside_the_package():
    assert os.path.isdir(tpl_mod.templates_dir())
    assert os.path.dirname(tpl_mod.__file__) in tpl_mod.templates_dir()
