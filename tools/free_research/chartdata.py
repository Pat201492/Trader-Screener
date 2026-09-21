#!/usr/bin/env python3
"""Turn a filled chart slot into points the dashboard's own `chart()` can draw.

`chart(points, opts)` in `web-dashboard/index.html` takes `[[label, value], ...]`
and draws an inline SVG line. Nothing here adds a charting library; this module
only decides WHICH rows are plottable and reads the two named columns out of
them.

The division of labour is the point. The model named two columns -- that is all
it did. Every number on the axis is read from the pull's rows here, so a chart
in a brief cannot show a value the data does not contain. A model that could
write a number into a chart could write one that was never in the filings, and
then the picture is fiction with a grid behind it.

Rows that cannot be plotted are counted rather than quietly dropped, and the
count travels with the points, because "14 filings" over a chart built from 9 of
them is a lie of omission.
"""

import numbers

MIN_POINTS = 2  # chart() divides by (points.length - 1): one point draws nothing


def as_number(value):
    """A y value as a float, or None if it is not a number.

    Accepts the numeric strings a JSON row often carries ("12", "1,234", "3.5").
    Booleans are not numbers: `True` is an int in Python and plotting it as 1
    would put a value on the axis that nobody wrote.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, numbers.Number):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def as_label(value):
    """An x value as a label, or None when there is nothing to label with."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def points_for(slot_value, rows):
    """Plottable points for a filled chart slot.

    Returns {points, skipped, x, y, mark, reason}. `reason` is None when the
    chart can be drawn, and otherwise says what stopped it -- naming both
    columns, so an empty chart says which fields it looked for rather than
    leaving a blank box.
    """
    x_col = (slot_value or {}).get("x")
    y_col = (slot_value or {}).get("y")
    mark = (slot_value or {}).get("mark", "bar")
    out = {"points": [], "skipped": 0, "x": x_col, "y": y_col,
           "mark": mark, "reason": None}

    if not x_col or not y_col:
        out["reason"] = "the chart slot names no x and y columns"
        return out

    for row in rows or []:
        label = as_label(row.get(x_col))
        value = as_number(row.get(y_col))
        if label is None or value is None:
            out["skipped"] += 1
            continue
        out["points"].append([label, value])

    if not out["points"]:
        out["reason"] = ("no row carries both %r and %r as plottable values"
                         % (x_col, y_col))
    elif len(out["points"]) < MIN_POINTS:
        out["reason"] = ("only one row carries both %r and %r; a line needs two "
                         "points" % (x_col, y_col))
    return out


def summarize(result):
    """One line for under the chart, or None when there is nothing to say."""
    if result["reason"]:
        return result["reason"]
    if result["skipped"]:
        return ("%d row%s skipped: no %s or no numeric %s"
                % (result["skipped"], "" if result["skipped"] == 1 else "s",
                   result["x"], result["y"]))
    return None
