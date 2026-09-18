"""Tests for turning a filled chart slot into plottable points.

Populated, partially skipped, and empty. Nothing here reaches the network.
"""

from tools.free_research.chartdata import as_number, points_for, summarize

SLOT = {"mark": "bar", "x": "month", "y": "count"}


def rows(*pairs):
    return [{"month": m, "count": c} for m, c in pairs]


# -------------------------------------------------------------- populated

def test_points_come_from_the_named_columns_in_row_order():
    got = points_for(SLOT, rows(("2025-07", 4), ("2025-08", 9), ("2025-09", 2)))
    assert got["points"] == [["2025-07", 4.0], ["2025-08", 9.0], ["2025-09", 2.0]]
    assert got["skipped"] == 0
    assert got["reason"] is None
    assert summarize(got) is None


def test_a_column_the_row_does_not_have_plots_nothing_rather_than_guessing():
    got = points_for({"x": "month", "y": "absent"}, rows(("2025-07", 4)))
    assert got["points"] == []
    assert "absent" in got["reason"]


def test_numeric_strings_are_plottable():
    got = points_for(SLOT, rows(("a", "12"), ("b", "1,234"), ("c", " 3.5 ")))
    assert [p[1] for p in got["points"]] == [12.0, 1234.0, 3.5]


def test_the_mark_and_column_names_travel_with_the_points():
    got = points_for(SLOT, rows(("a", 1), ("b", 2)))
    assert (got["mark"], got["x"], got["y"]) == ("bar", "month", "count")


# ------------------------------------------------------- partially skipped

def test_rows_missing_x_or_y_are_skipped_and_counted():
    got = points_for(SLOT, [
        {"month": "2025-07", "count": 4},
        {"month": None, "count": 5},
        {"month": "2025-09", "count": None},
        {"count": 6},
        {"month": "2025-11", "count": 7},
    ])
    assert [p[0] for p in got["points"]] == ["2025-07", "2025-11"]
    assert got["skipped"] == 3
    assert got["reason"] is None


def test_the_skipped_count_is_stated_for_display():
    got = points_for(SLOT, [
        {"month": "a", "count": 1},
        {"month": "b", "count": 2},
        {"month": "c", "count": "not a number"},
    ])
    line = summarize(got)
    assert line.startswith("1 row skipped")
    assert "month" in line and "count" in line


def test_a_non_numeric_y_is_skipped_not_coerced():
    got = points_for(SLOT, rows(("a", "n/a"), ("b", 2), ("c", 3)))
    assert [p[0] for p in got["points"]] == ["b", "c"]
    assert got["skipped"] == 1


def test_a_boolean_is_not_a_number():
    assert as_number(True) is None
    assert as_number(False) is None
    got = points_for(SLOT, rows(("a", True), ("b", 2), ("c", 3)))
    assert got["skipped"] == 1


def test_zero_is_a_real_value_not_a_missing_one():
    got = points_for(SLOT, rows(("a", 0), ("b", 5)))
    assert got["points"] == [["a", 0.0], ["b", 5.0]]
    assert got["skipped"] == 0


# ------------------------------------------------------------------ empty

def test_no_usable_row_gives_a_reason_naming_both_columns():
    got = points_for(SLOT, [{"month": None, "count": None}])
    assert got["points"] == []
    assert "month" in got["reason"] and "count" in got["reason"]
    assert summarize(got) == got["reason"]


def test_no_rows_at_all_gives_the_same_stated_reason():
    got = points_for(SLOT, [])
    assert got["points"] == []
    assert "month" in got["reason"] and "count" in got["reason"]


def test_a_single_plottable_row_is_reported_because_a_line_needs_two():
    got = points_for(SLOT, rows(("only", 3)))
    assert len(got["points"]) == 1
    assert "two points" in got["reason"]


def test_a_slot_naming_no_columns_says_so():
    assert "no x and y" in points_for({}, rows(("a", 1)))["reason"]
    assert "no x and y" in points_for(None, rows(("a", 1)))["reason"]
