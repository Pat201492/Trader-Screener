"""
Zero-token anchors that locate candidate guidance sentences (issue #187, EPIC #95).

Company guidance -- the forward-looking numbers an earnings release states about
future quarters/years -- is written in formulaic language, and not by accident:
the PSLRA safe harbour rewards issuers for framing a projection as a
"forward-looking statement" in stock phrases ("the Company expects", "we now
anticipate", "in the range of"). Because the language is formulaic, LOCATING it
is a rules job, not a model job. This module runs before any model and cuts what
a model later sees from a whole press release (thousands of tokens) to the
handful of sentences that actually carry a forward-looking number.

`find_candidates(text)` operates on ALREADY-NORMALIZED document text (the output
of `normalize.py` for an EX-99.x release located by `earnings_releases.py`, #185)
and returns, for each candidate sentence, its text, its `(start, end)` character
offsets into the text it was given, and the anchor that flagged it. It also
returns the candidate count and a per-anchor hit table, so anchor quality is
MEASURED -- a `SENTENCE_ANCHOR` that never fires across real filings is dead
weight, and the only way to know is to count.

A sentence is a candidate when it BOTH

  * states a forward-looking NUMBER -- a `RANGE_PATTERNS` hit (a dollar or
    percent range, a "between X and Y", an "in the range of ..."); and
  * is forward-looking -- a `SENTENCE_ANCHORS` verb hit ("the Company expects"),
    OR it sits inside a section a `SECTION_ANCHORS` heading opened ("Outlook").

Sentences inside a PSLRA safe-harbour boilerplate paragraph are EXCLUDED, even
when they contain a verb anchor and a range: that paragraph is a disclaimer
ABOUT forward-looking statements, not a forward-looking statement, and it prints
exactly the phrases the anchors match, so failing to exclude it would flag the
one paragraph in the release guaranteed to be noise.

stdlib only (`re`). Run the self-check:
    python tools/edgar_scrubber/guidance_anchors.py
"""
import re
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# Anchor constants -- each entry is a NAMED regex so per-anchor hits can be
# counted (see `CandidateSet.anchor_hits`). Names, not indexes, so the hit
# table stays readable when anchors are added or reordered.
# --------------------------------------------------------------------------- #

# Section headings that OPEN a forward-looking section. A short heading line
# matching one of these puts every following sentence "in section" until the
# next heading, so a bare guidance sentence with no verb anchor is still caught.
SECTION_ANCHORS = {
    # "Outlook", "Financial Outlook", "Fiscal 2026 Outlook", "Q4 Outlook".
    "outlook": re.compile(r"\boutlook\b", re.I),
    # "Guidance", "Full-Year Guidance", "Updated Guidance".
    "guidance_heading": re.compile(r"\bguidance\b", re.I),
    # "Business Outlook" variants that lead with "Looking Ahead"/"Looking Forward".
    "looking_ahead": re.compile(r"\blooking\s+(?:ahead|forward)\b", re.I),
    # "Financial Expectations", "Our Expectations".
    "expectations_heading": re.compile(r"\bexpectations\b", re.I),
}

# In-sentence phrasing that makes a sentence forward-looking. These are the
# PSLRA stock verbs; the safe-harbour paragraph prints them too, which is why it
# is excluded separately rather than by pattern.
SENTENCE_ANCHORS = {
    # "we expect", "we now anticipate", "we currently project/forecast/estimate".
    "we_expect": re.compile(
        r"\bwe\s+(?:now\s+|currently\s+|continue\s+to\s+)?"
        r"(?:expect|anticipate|project|forecast|estimate)\w*\b", re.I),
    # "the Company expects", "the Company now anticipates", etc.
    "company_expects": re.compile(
        r"\bthe\s+company\s+(?:now\s+|currently\s+|continues?\s+to\s+)?"
        r"(?:expects?|anticipates?|projects?|forecasts?|estimates?)\b", re.I),
    # Passive voice -- "Revenue is expected to be ...", "gross margins are
    # expected to be ...". NVIDIA and others state the outlook this way rather
    # than in the first person, so a section-less passive line is still caught.
    "expected_to_be": re.compile(
        r"\b(?:is|are)\s+expected\s+to\s+be\b", re.I),
    # "reaffirms/reiterates/raises/lowers/maintains/updates ... guidance".
    "reaffirm_guidance": re.compile(
        r"\b(?:reaffirms?|reiterates?|raises?|lowers?|maintains?|updates?)\b"
        r"[^.]*\bguidance\b", re.I),
    # "for the full year", "for fiscal 2026", "for the fourth quarter" -- the
    # period a projection is scoped to, printed right in the guidance sentence.
    "for_period": re.compile(
        r"\bfor\s+(?:the\s+)?(?:full[-\s]year|fiscal(?:\s+\d{4})?|"
        r"(?:the\s+)?(?:first|second|third|fourth)\s+quarter)\b", re.I),
}

# Numeric shapes a forward-looking NUMBER is printed in. A candidate must match
# at least one -- a forward-looking sentence with no number is not what this
# rung is for (it hands the model numbers to map, not prose to read).
RANGE_PATTERNS = {
    # "$1.20 billion to $1.30 billion", "$4.10 to $4.30", "$300 - $350 million".
    "dollar_range": re.compile(
        r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:billion|million|thousand|bn|mm))?"
        r"\s*(?:to|through|–|—|-)\s*"
        r"\$?\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:billion|million|thousand|bn|mm))?",
        re.I),
    # "8% to 10%", "18% - 20%" -- a percent range (margins, growth rates).
    "percent_range": re.compile(
        r"\d+(?:\.\d+)?%\s*(?:to|through|–|—|-)\s*\d+(?:\.\d+)?%",
        re.I),
    # "between $4.10 and $4.30", "between $300 million and $350 million".
    "between_and": re.compile(
        r"\bbetween\b\s+\$?\d[\d,]*(?:\.\d+)?"
        r"(?:\s*(?:billion|million|thousand|bn|mm))?\s+and\s+\$?\d", re.I),
    # "in the range of ..." -- the lead-in; a dollar_range/percent_range usually
    # follows and is counted too, but the phrase itself is a strong signal.
    "range_of": re.compile(r"\bin\s+the\s+range\s+of\b", re.I),
    # A point estimate with a tolerance -- "$108.0 billion, plus or minus 2%",
    # "74.0%, plus or minus 50 basis points". A tolerance is convertible to a
    # low-high range (see `tolerance_range`), so downstream {low, high} stays one
    # shape; the pattern is the gate, `tolerance_range` does the conversion.
    "tolerance": re.compile(
        r"\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|bn|mm)?%?"
        r"\s*,?\s*plus\s+or\s+minus\s+"
        r"\d+(?:\.\d+)?\s*(?:%|percentage\s+points?|percent|basis\s+points?|bps|bp)"
        r"(?![A-Za-z])",
        re.I),
}

# Scale words a dollar figure is printed with, mapped to their multiplier.
_SCALE_WORDS = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mm": 1e6,
                "thousand": 1e3}

# Capturing form of RANGE_PATTERNS["tolerance"], used by `tolerance_range` to
# pull the base figure and the tolerance apart for conversion.
_TOLERANCE_CAPTURE = re.compile(
    r"(?P<base>\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|bn|mm)?%?)"
    r"\s*,?\s*plus\s+or\s+minus\s+"
    r"(?P<tol>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|percentage\s+points?|percent|basis\s+points?|bps|bp)"
    r"(?![A-Za-z])",
    re.I)


def tolerance_range(text):
    """Convert a point-estimate-with-tolerance to ``{"low", "high"}``, or return
    ``None`` if `text` carries no tolerance phrasing.

    The tolerance unit decides how the spread is applied to the base figure:

      * ``%``/``percent`` -- RELATIVE to the base. "$108.0 billion, plus or minus
        2%" -> base 108.0e9, spread 108.0e9 * 0.02 -> 105.84e9 to 110.16e9.
      * ``basis points``/``bps`` -- ABSOLUTE, in percentage points (1 bp =
        0.01 pp). "74.0%, plus or minus 50 basis points" -> 73.5 to 74.5, which
        is NOT the same as "plus or minus 50 percent" (37.0 to 111.0).
      * ``percentage point(s)`` -- ABSOLUTE, in percentage points.

    The base's own scale word (billion/million/...) and any leading ``$`` are
    honoured; a trailing ``%`` on the base does not change how the tolerance is
    applied (it is just the unit the figure is quoted in).
    """
    m = _TOLERANCE_CAPTURE.search(text)
    if not m:
        return None
    base_raw = m.group("base")
    base = float(re.sub(r"[^\d.]", "", base_raw))
    scale = re.search(r"billion|million|thousand|bn|mm", base_raw, re.I)
    if scale:
        base *= _SCALE_WORDS[scale.group(0).lower()]
    tol = float(m.group("tol"))
    unit = m.group("unit").lower()
    if "basis" in unit or unit in ("bp", "bps"):
        delta = tol / 100.0            # 50 bp -> 0.50 percentage points, absolute
    elif "percentage" in unit:
        delta = tol                    # percentage points, absolute
    else:                              # "%" / "percent" -> relative to the base
        delta = base * tol / 100.0
    return {"low": base - delta, "high": base + delta}

# --------------------------------------------------------------------------- #
# Safe-harbour boilerplate detection
# --------------------------------------------------------------------------- #

# A paragraph is PSLRA boilerplate when it names forward-looking statements AND
# carries a safe-harbour marker -- the disclaimer, not a projection.
_FL_PHRASE = re.compile(r"forward[-\s]looking\s+statements?", re.I)
_SAFE_HARBOUR_MARKERS = re.compile(
    r"private\s+securities\s+litigation\s+reform\s+act"
    r"|safe\s+harbou?r"
    r"|risks?\s+and\s+uncertaint"
    r"|undue\s+reliance"
    r"|except\s+as\s+required\s+by\s+law"
    r"|undertakes?\s+no\s+obligation",
    re.I)


def _is_boilerplate(paragraph):
    """True when `paragraph` is a PSLRA safe-harbour disclaimer -- names
    forward-looking statements and carries a safe-harbour marker."""
    return bool(_FL_PHRASE.search(paragraph)
                and _SAFE_HARBOUR_MARKERS.search(paragraph))


# --------------------------------------------------------------------------- #
# Paragraph + sentence segmentation, offsets preserved throughout
# --------------------------------------------------------------------------- #

# A blank line (optionally carrying whitespace) separates paragraphs.
_PARA_SPLIT = re.compile(r"\n[ \t]*\n")
# Sentence boundary: end punctuation, whitespace, then a capital/opening paren.
# The decimals in "$1.20 to $1.30" are safe -- no space follows their dot.
_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def _paragraphs(text):
    """`(start, end)` offset pairs for each blank-line-separated paragraph."""
    out = []
    idx = 0
    for m in _PARA_SPLIT.finditer(text):
        if text[idx:m.start()].strip():
            out.append((idx, m.start()))
        idx = m.end()
    if text[idx:].strip():
        out.append((idx, len(text)))
    return out


def _is_heading(para_text):
    """A heading is a single short line with no terminal period -- "Financial
    Outlook", "Forward-Looking Statements", a title. Used to open/close the
    section a `SECTION_ANCHORS` match applies to."""
    if "\n" in para_text.strip():
        return False
    stripped = para_text.strip()
    return bool(stripped) and not stripped.endswith((".", "!", "?")) \
        and len(stripped.split()) <= 8


def _section_of_heading(para_text):
    """The `SECTION_ANCHORS` name a heading opens, or ``None`` -- a heading that
    matches no section anchor (a title, "Forward-Looking Statements") CLOSES the
    current section by resolving to ``None``."""
    for name, pat in SECTION_ANCHORS.items():
        if pat.search(para_text):
            return name
    return None


def _sentences(text, start, end):
    """`(start, end)` offset pairs for each sentence in ``text[start:end]``,
    leading/trailing whitespace trimmed so ``text[s:e]`` is the sentence."""
    seg = text[start:end]
    spans = []
    idx = 0
    for m in _SENT_BOUNDARY.finditer(seg):
        spans.append((start + idx, start + m.start()))
        idx = m.end()
    spans.append((start + idx, end))
    out = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            out.append((s, e))
    return out


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

@dataclass
class CandidateSet:
    """What `find_candidates` returns.

      * ``candidates`` -- list of ``{sentence, span, anchor}`` records, in the
        order they appear in the text; ``span`` is a ``(start, end)`` offset
        pair and ``text[start:end] == sentence``;
      * ``count`` -- ``len(candidates)``, the headline number;
      * ``anchor_hits`` -- every defined anchor name (section, sentence and
        range) mapped to how many candidate sentences it matched. An anchor
        sitting at 0 across a corpus is dead weight; that is measurable here
        rather than assumed.
    """
    candidates: list
    count: int
    anchor_hits: dict


def find_candidates(text):
    """Locate candidate guidance sentences in already-normalized `text`.

    See the module docstring for the candidate rule. Returns a `CandidateSet`.
    A document with no anchor hits returns an empty candidate list (and an
    all-zero `anchor_hits`); it never raises.
    """
    anchor_hits = {name: 0 for group in
                   (SECTION_ANCHORS, SENTENCE_ANCHORS, RANGE_PATTERNS)
                   for name in group}
    candidates = []

    current_section = None
    for p_start, p_end in _paragraphs(text):
        para = text[p_start:p_end]

        if _is_heading(para):
            current_section = _section_of_heading(para)
            continue

        # A disclaimer paragraph prints the anchor phrases but carries no real
        # guidance -- skip it whole. It also does not change section scope.
        if _is_boilerplate(para):
            continue

        for s_start, s_end in _sentences(text, p_start, p_end):
            sentence = text[s_start:s_end]

            ranges = [n for n, p in RANGE_PATTERNS.items() if p.search(sentence)]
            if not ranges:
                continue  # forward-looking prose with no number is not a candidate

            verbs = [n for n, p in SENTENCE_ANCHORS.items() if p.search(sentence)]
            if not verbs and current_section is None:
                continue  # a number, but nothing marks it forward-looking

            primary = verbs[0] if verbs else current_section
            candidates.append({
                "sentence": sentence,
                "span": (s_start, s_end),
                "anchor": primary,
            })
            for n in ranges:
                anchor_hits[n] += 1
            for n in verbs:
                anchor_hits[n] += 1
            if current_section is not None:
                anchor_hits[current_section] += 1

    return CandidateSet(candidates=candidates, count=len(candidates),
                        anchor_hits=anchor_hits)


if __name__ == "__main__":
    sample = (
        "ACME CORP REPORTS THIRD QUARTER RESULTS\n\n"
        "REDACTED -- ACME Corp today reported results for the quarter.\n\n"
        "Financial Outlook\n\n"
        "For the fourth quarter, the Company expects net revenue in the range "
        "of $1.20 billion to $1.30 billion.\n\n"
        "We now anticipate full-year earnings per share of between $4.10 and "
        "$4.30.\n\n"
        "Forward-Looking Statements\n\n"
        "This release contains forward-looking statements within the meaning of "
        "the Private Securities Litigation Reform Act of 1995. The Company "
        "expects results of $9.00 billion to $9.50 billion only under the "
        "assumptions in its filings, and undertakes no obligation to update "
        "them except as required by law."
    )
    result = find_candidates(sample)
    assert result.count == 2, result.candidates
    for c in result.candidates:
        assert sample[c["span"][0]:c["span"][1]] == c["sentence"]
        assert "9.00 billion" not in c["sentence"], "boilerplate leaked"
        print(f"  [{c['anchor']:<16}] {c['sentence'][:60]}...")
    assert find_candidates("Net income rose to $5 million.").count == 0
    assert find_candidates("").count == 0
    # A point estimate with a tolerance converts to {low, high} (issue #225).
    tr = tolerance_range("Revenue is expected to be $108.0 billion, plus or minus 2%.")
    assert tr and abs(tr["low"] - 105.84e9) < 1 and abs(tr["high"] - 110.16e9) < 1, tr
    assert tolerance_range("74.0%, plus or minus 50 basis points") == {"low": 73.5, "high": 74.5}
    print("\nanchor hits:", {k: v for k, v in result.anchor_hits.items() if v})
    print("guidance_anchors self-check: PASS")
