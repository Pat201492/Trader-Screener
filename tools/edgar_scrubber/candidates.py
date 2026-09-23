"""
Deterministic value-candidate enumeration (issue #177).

The candidate-SELECT extraction rung hands the model a short, labelled LIST of
the values a field's context text actually contains and asks it to pick one --
it returns an INDEX, never a number of its own. That closes a whole class of
failure the free-generation rungs cannot: on run #0018, `estimated_value_per_1000`
came back as 950 -- the midpoint of that field's `bounds` -- in 21 of 25
Citigroup filings, because the model, unable to find the figure, answered in the
middle of the range the prompt handed it. 950 is not printed anywhere in those
filings, so it is not a candidate, so a model that can only return an index into
this list cannot produce it. It is also far cheaper: a labelled candidate list is
a couple of hundred tokens against the thousands `build_field_context`
(`validation.py`) hands over as whole section slices.

`candidates_for(field_def, text)` finds every plausible value for one field in
its context text using CODE ALONE -- no model, no network -- and returns each
with the label it sits under and its exact offsets into `text`:

  * `number` / `percent` fields  -- every numeric literal in the forms filings
    print: ``70``, ``70.00%``, ``1,000``, ``$979.00``.
  * `date` fields  -- every date-shaped run, parsed through
    `normalize.parse_date_prose`, returned as its ISO value. A run that does not
    parse to a real date (``September 31, 2028``) is dropped, not guessed.
  * `string`, `enum`, `array` fields  -- an empty list. These already score well
    and are out of scope (#177).

The LABEL is what tells a maturity date from a pricing date when four dates sit
within a few lines of each other: it is the text of the nearest preceding
``Label:``-style run on the same or the previous line. The offsets are into the
text as given, so ``text[start:end]`` is always exactly the candidate's printed
form -- provenance the SELECT rung carries straight back through the reduction
`OffsetMap` to the original document bytes.

stdlib only. Run the self-check:  python tools/edgar_scrubber/candidates.py
"""
import re
from bisect import bisect_left, bisect_right

try:  # package import: tools.edgar_scrubber.candidates
    from .normalize import parse_date_prose
except ImportError:  # standalone: python tools/edgar_scrubber/candidates.py
    from normalize import parse_date_prose


# A numeric literal as a 424B2 prints it: an optional leading ``$``, digits with
# optional thousands separators, an optional fractional part, and an optional
# trailing ``%``. The lookbehind stops a match starting in the middle of a word
# or a longer number (``Note123`` -> no ``123``; ``1,000`` matches whole, never
# a bare ``000``). ``\d+(?:,\d{3})*`` matches both ``70`` and ``1,000`` and does
# not split ``12345`` at a comma boundary it does not have.
_NUMBER_RE = re.compile(r"(?<![\w,.])\$?\d+(?:,\d{3})*(?:\.\d+)?%?")

# A ``Label:``-style run -- everything up to a colon, not crossing a newline.
_LABEL_RE = re.compile(r"([^\n:]+):")

# A date-shaped run in the forms these filings use. This only LOCATES a
# candidate span; whether it is a real date, and its ISO value, is decided by
# `normalize.parse_date_prose` -- the single source of truth for date parsing,
# so ``September 31, 2028`` is found here and then dropped there.
_DATE_RE = re.compile(
    r"\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*\d{4}\b"      # August 31, 2028
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?\s*,?\s*\d{4}\b"     # 31 August 2028
    r"|\b\d{4}-\d{1,2}-\d{1,2}\b"                                        # 2028-08-31
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"                                        # 8/31/2028
)


def _line_starts(text):
    """Start offset of every line, so a candidate offset -> its line index is a
    binary search rather than a per-candidate `count`."""
    starts = [0]
    i = text.find("\n")
    while i != -1:
        starts.append(i + 1)
        i = text.find("\n", i + 1)
    return starts


def _label_index(text):
    """Every ``Label:`` run as `(end_offset, line_index, label_text)`, ordered
    by `end_offset` -- `end_offset` is just past the colon, which is where the
    value that follows begins, so "nearest preceding label" is a bisect on it."""
    starts = _line_starts(text)
    out = []
    for m in _LABEL_RE.finditer(text):
        label = m.group(1).strip()
        if not label:
            continue
        line = bisect_right(starts, m.start()) - 1
        out.append((m.end(), line, label))
    return out


def _label_for(start, label_index, label_ends, cand_line):
    """The label a candidate at `start` (on `cand_line`) sits under: the nearest
    preceding ``Label:`` run whose colon is before `start` and whose line is the
    candidate's own or the one immediately above. `label_ends` is the ordered
    list of the label index's `end_offset`s, for the bisect. Empty string when
    nothing qualifies -- an unlabelled value is still a candidate."""
    i = bisect_right(label_ends, start) - 1
    while i >= 0:
        end, line, label = label_index[i]
        if end > start:
            i -= 1
            continue
        if line in (cand_line, cand_line - 1):
            return label
        # A label further up than the previous line does not describe this
        # value; keep walking back only helps if an even-nearer one exists,
        # which it cannot (list is ordered), so stop.
        return ""
    return ""


def _parse_number(raw):
    """A printed numeric literal -> its value: `int` when it prints no
    fractional part (``1,000`` -> 1000), else `float` (``70.00%`` -> 70.0).
    The ``$`` and ``%`` are notation, not magnitude, and are stripped."""
    digits = raw.replace("$", "").replace("%", "").replace(",", "")
    if "." in digits:
        return float(digits)
    return int(digits)


def candidates_for(field_def, text):
    """Every plausible value for `field_def` printed in `text`, in order.

    Returns a list of dicts, each with:
      * ``value`` -- the parsed value (number for number/percent, ISO string for
        date);
      * ``label`` -- the nearest preceding ``Label:`` run (see `_label_for`);
      * ``span``  -- the ``(start, end)`` offsets into `text` of the printed form;
      * ``raw``   -- the literal string as printed.

    ``text[start:end]`` is always exactly ``raw``. `string`, `enum` and `array`
    fields return ``[]`` -- out of scope for the candidate-SELECT rung (#177).
    """
    ftype = getattr(field_def, "type", None)
    if ftype in ("number", "percent"):
        finder, parse = _NUMBER_RE, _parse_number
    elif ftype == "date":
        finder, parse = _DATE_RE, parse_date_prose
    else:
        return []

    starts = _line_starts(text)
    label_index = _label_index(text)
    label_ends = [e for e, _, _ in label_index]

    out = []
    for m in finder.finditer(text):
        raw = m.group(0)
        value = parse(raw)
        if value is None:  # date-shaped run that is not a real date
            continue
        start = m.start()
        cand_line = bisect_right(starts, start) - 1
        out.append({
            "value": value,
            "label": _label_for(start, label_index, label_ends, cand_line),
            "span": (start, m.end()),
            "raw": raw,
        })
    return out


if __name__ == "__main__":
    class _F:
        def __init__(self, t):
            self.type = t

    note = ("Contingent Coupon Rate: 9.15% per annum\n"
            "Coupon Barrier: 70.00%\n"
            "Denomination: $1,000\n"
            "Estimated Value: $979.00 per $1,000")
    for c in candidates_for(_F("percent"), note):
        assert note[c["span"][0]:c["span"][1]] == c["raw"]
        print(f"  {c['label']!r:<26} {c['raw']!r:<12} -> {c['value']}")

    dates = ("Strike Date: August 27, 2026\n"
             "Pricing Date: August 28, 2026\n"
             "Issue Date: September 2, 2026\n"
             "Maturity Date: August 31, 2028")
    cands = candidates_for(_F("date"), dates)
    assert len(cands) == 4, cands
    assert len({c["label"] for c in cands}) == 4
    print()
    for c in cands:
        assert dates[c["span"][0]:c["span"][1]] == c["raw"]
        print(f"  {c['label']!r:<16} {c['raw']!r:<22} -> {c['value']}")

    assert candidates_for(_F("string"), note) == []
    assert candidates_for(_F("enum"), note) == []
    assert candidates_for(_F("array"), note) == []

    print("\ncandidates self-check: PASS")
