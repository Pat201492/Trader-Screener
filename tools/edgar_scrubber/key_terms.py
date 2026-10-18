"""
Deterministic key-terms extraction for 424B2 pricing supplements -- the first
step, before any model.

Every bank's pricing supplement carries a labelled terms block. After
normalization it reads as `Label:: value` runs:

    CUSIP:: 09712GX58
    Maturity Date:: October 3, 2029, subject to postponement
    Coupon Barrier:: $119.392, which is equal to 80.00% of the Starting Price.
    Threshold Price:: $244.678, which is equal to 70% of the starting price

The labels differ by issuer ("Threshold Price" / "Downside Threshold Value" /
"Trigger Level"); the structure does not. So: parse the block into label ->
value pairs, map labels to spec fields through a synonym table, and pull the
figure out of the value with a pattern. Zero tokens, milliseconds, no GPU, and
every value comes with the exact span it was read from.

Measured 2026-10-13 on four live notes (BofA, BNS, Wells Fargo, JPMorgan): the
local 7B scored 27/36 on the default fields, with barrier_pct 1/4,
estimated_value_per_1000 1/4 and cusip 2/4. Those values were sitting in term
lines the whole time.

Anything this cannot read with certainty is left out, never guessed: the field
falls through to the model rung exactly as before. A wrong deterministic value
would be worse than a missing one, because it skips the model entirely.

stdlib only. Self-check:  python tools/edgar_scrubber/key_terms.py
"""

import re
from dataclasses import dataclass
from datetime import datetime

# A term label: words immediately followed by "::"; the value runs to the next
# label. A label is built from WORDS only -- a capitalised word, a short
# all-caps word (CUSIP, ISIN), a lowercase word after the first, a connector or
# a parenthetical -- so a value token like "GX58" (letters+digits) can never
# start one: "CUSIP:: 09712GX58 Maturity Date::" must give the value 09712GX58,
# not split it. Footnote markers glued to the end ("Stated Maturity Date1",
# "Maturity Date*") are allowed and stripped when the label is normalized.
# A capitalised word may have a second one glued on with no space: Goldman
# Sachs prints "Stated MaturityDate*::" (blind set 2, 2026-10-16).
_W_CAP = r"[A-Z][a-z'’\-]+(?:[A-Z][a-z'’\-]+)?(?:/[A-Z][a-z'’\-]+)?"
_W_FIRST = rf"(?:{_W_CAP}|[A-Z]{{2,6}}(?![A-Za-z0-9]))"
_W_NEXT = rf"(?:{_W_CAP}|[A-Z]{{2,6}}(?![A-Za-z0-9])|[a-z][a-z'’\-]*|/|&|\([^()]{{1,60}}\))"
_FOOTNOTE = r"[\d*†‡§]*(?:,\s?\d+)*"
_LABEL_RE = re.compile(rf"({_W_FIRST}(?:[ \t]+{_W_NEXT}){{0,9}}){_FOOTNOTE}\s*::")
_MAX_VALUE = 700

# Spec field -> normalized labels that state it, in preference order. A label
# starting "hypothetical" belongs to an illustration table and never matches.
FIELD_LABELS = {
    "cusip": ("cusip", "cusip / isin", "cusip/isin", "cusip number", "cusip no."),
    "issuer": ("issuer", "company (issuer)", "issuer (company)"),
    "maturity_date": ("maturity date", "stated maturity date"),
    "pricing_date": ("pricing date", "trade date"),
    "issue_date": ("issue date", "original issue date", "settlement date"),
    "contingent_coupon_rate": ("contingent coupon rate", "contingent interest rate",
                               "coupon rate", "interest rate"),
    "coupon_barrier_pct": ("coupon barrier", "coupon barrier level", "coupon threshold price",
                           "coupon threshold value", "coupon threshold level",
                           "interest barrier", "interest threshold", "coupon trigger",
                           # JPMorgan: one level for both coupon and downside.
                           "interest barrier / trigger level", "interest barrier / trigger value"),
    # Specific before generic: a note with both a "Call Threshold Price" (the
    # autocall level) and a "Downside Threshold Price" must read the downside
    # one. Held-out BNS 0001839882-26-045355 returned the 95% call level here.
    "barrier_pct": ("downside threshold price", "downside threshold value",
                    "downside threshold level", "downside threshold", "threshold price",
                    "threshold value", "threshold level", "barrier", "barrier level",
                    "barrier price", "barrier amount", "trigger level", "trigger value",
                    "trigger price", "knock-in level",
                    "interest barrier / trigger level", "interest barrier / trigger value"),
}

# A label with one of these words in front of a generic phrase is a DIFFERENT
# term ("call threshold price" is the autocall level, not the downside barrier),
# so the run-on fallback in extract_key_terms must never strip them off.
_QUALIFIERS = {"call", "autocall", "coupon", "interest", "redemption", "early",
               "hypothetical", "upside", "cap", "knock-out"}

# A figure the filing does not commit to: a floor, a ceiling, a range, or a
# value set later. A preliminary supplement says "will be at least 23.00% per
# annum" and "expected to be between $920.00 and $970.00". Reporting either as
# the value is wrong, so the field is left to the model, which the spec tells
# to answer null for an uncommitted figure.
_HEDGE_BEFORE = re.compile(
    r"(?:at least|at most|not less than|no less than|not more than|no more than|"
    r"between|approximately|expected to be|will be determined|to be determined|"
    r"will be set)\W*(?:\$\s*)?$", re.I)
# The one hedge that still states a usable figure: a floor. A preliminary
# supplement's "will be at least 13.50% per annum" is the minimum the final
# rate can be -- reported as the value with bound="min", which the server
# flags, rather than handed to a model that returns the same 13.50 unflagged.
_FLOOR_BEFORE = re.compile(
    r"(?:at least|not less than|no less than|a minimum of)\W*(?:\$\s*)?$", re.I)
_RANGE_AFTER = re.compile(r"^\s*%?\s*(?:per annum\s*)?(?:and|to|-|–)\s*\$?\s*\d", re.I)


def _committed(text, start, end):
    """False when the figure at text[start:end] is hedged: a floor, ceiling or
    range, or a value to be set later."""
    before = text[max(0, start - 40):start]
    after = text[end:end + 25]
    return not (_HEDGE_BEFORE.search(before) or _RANGE_AFTER.search(after))


def _floor(text, start, end):
    """True when the figure at text[start:end] is stated as a minimum only."""
    return bool(_FLOOR_BEFORE.search(text[max(0, start - 40):start])
                and not _RANGE_AFTER.search(text[end:end + 25]))

_MONTHS = ("January|February|March|April|May|June|July|August|September|"
           "October|November|December")
_DATE_RE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b")
# "of the / its / their starting price": HSBC and Citi write "each of which is
# 60% of its starting price" (blind set 2, 2026-10-16).
_OF_START = r"of\s+(?:(?:the|its|their|each)\s+)?(?:starting|initial|strike)"
_PCT_OF_INITIAL_RE = re.compile(
    rf"(?:equal to|is|equals|of|represents|\(|,)\s*(\d{{1,3}}(?:\.\d+)?)\s?%\s*{_OF_START}", re.I)
_PCT_LEAD_RE = re.compile(rf"^\s*(\d{{1,3}}(?:\.\d+)?)\s?%\s*{_OF_START}", re.I)
_RATE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s?%\s*per\s+annum", re.I)
_CUSIP_RE = re.compile(r"\b([0-9A-Z]{9})\b")
# Goldman states the rate only inside the coupon term: "a contingent coupon
# payment equal to at least $19 (equivalent to a contingent coupon rate of at
# least 22.8% per annum)".
_RATE_PROSE_RE = re.compile(
    r"(?:contingent (?:coupon|interest) rate of\s+(?:at least\s+|approximately\s+)?|\()"
    r"(\d{1,2}(?:\.\d+)?)\s?%\s*per\s+annum",
    re.I)
# Preliminary: "expected to be between $920.00 and $970.00" (BNS adds the
# percent: "$934.09 (93.409%) and $964.09"), or only a floor: Citi "at least
# $901.00"; BMO "is $973.00 per security ... at pricing may differ from this
# value but will not be less than $925.00" -- a figure as of today, not THE
# estimated value, so a preliminary filing never gets a point value here.
_EST_RANGE_RE = re.compile(
    r"estimated (?:initial )?value of (?:the|your) (?:securities|notes)[^$]{0,200}?"
    r"(?:between\s+\$\s?(\d{3,4}(?:\.\d{1,2})?)(?:\s*\([\d.]+%\))?\s+and\s+\$\s?(\d{3,4}(?:\.\d{1,2})?)"
    r"|(?:at least|not be less than|no less than|not less than)\s+\$\s?(\d{3,4}(?:\.\d{1,2})?))",
    re.I | re.S)
# The cover legend of a preliminary supplement, in its first lines.
_PRELIM_RE = re.compile(r"subject to completion|preliminary pricing supplement", re.I)
_EST_VALUE_RE = re.compile(
    r"estimated (?:initial )?value of the (?:securities|notes)[^$]{0,160}?\$\s?(\d{3,4}(?:,\d{3})?\.\d{2})",
    re.I | re.S)


@dataclass(frozen=True)
class Term:
    label: str          # normalized: lowercase, footnote markers stripped
    raw_label: str
    value: str          # text after "::" up to the next label (trimmed)
    value_start: int    # offset of `value` in the document text
    value_end: int


@dataclass(frozen=True)
class KeyTermValue:
    field: str
    value: object       # parsed: str / float / ISO date
    raw: str            # the figure exactly as the filing prints it
    span: tuple         # (start, end) of `raw` in the document text
    label: str          # the term label (or "prose") it was read from
    method: str         # "term", "term-table" or "prose"
    bound: str = None   # "min": the filing states only a floor (preliminary)


def normalize_label(label):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", label.strip())   # "MaturityDate"
    s = s.lower().replace("’", "'")
    s = re.sub(r"\s*/\s*", " / ", s)                      # "barrier/trigger"
    s = re.sub(r"[\d*†‡§,\s]+$", "", s).strip()   # footnotes: "date1", "date*", "date1, 2"
    s = re.sub(r"\s+", " ", s)
    return s


# Some issuers print "Label: value" with ONE colon (Toronto-Dominion,
# 2026-10-16). Used only when a filing has almost no "::" labels, and a
# single-colon label must start a line or follow a sentence end -- so prose
# like "the following: ..." mid-sentence can never become a label.
_LABEL1_RE = re.compile(
    rf"(?:(?<=\n)|(?<=\. )|(?<=[●•▪■◼])|(?<=[●•▪■◼] )|^)({_W_FIRST}(?:[ \t]+{_W_NEXT}){{0,9}}){_FOOTNOTE}:(?!:)\s", re.M)


_KNOWN1_RE = None


def _known_single(text):
    global _KNOWN1_RE
    if _KNOWN1_RE is None:
        labs = sorted({l for ls in FIELD_LABELS.values() for l in ls}, key=len, reverse=True)
        alt = "|".join(re.escape(l).replace(r"\ /\ ", r"\s*/\s*").replace(r"\ ", r"\s+")
                       for l in labs)
        # Capitalised, as a term line prints it: prose says "less than its
        # downside threshold price: $1,000" (Toronto-Dominion) mid-sentence.
        _KNOWN1_RE = re.compile(rf"(?<![\w’'/])(?=[A-Z])((?i:{alt})){_FOOTNOTE}:(?!:)\s")
    return list(_KNOWN1_RE.finditer(text))


def parse_terms(text):
    """Every `Label:: value` run in the text, in document order (or every
    `Label: value` run in a filing that does not use "::")."""
    hits = list(_LABEL_RE.finditer(text))
    single = list(_LABEL1_RE.finditer(text))
    # A filing that mostly writes "Label: value" gets both styles, in document
    # order -- so a single-colon block's values still end at the next label.
    # JPMorgan 0001918704-26-009603: 5 "::" labels (a nested Review Date list)
    # and 52 single-colon ones; reading only the "::" lost its whole terms block.
    if len(single) > len(hits):
        seen = {m.end() for m in single} | {m.end() for m in hits}
        extra = [m for m in _known_single(text) if m.end() not in seen]
        hits = sorted(hits + single + extra, key=lambda m: m.start())
    terms = []
    for i, m in enumerate(hits):
        start = m.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        end = min(end, start + _MAX_VALUE)
        raw_val = text[start:end]
        lead = len(raw_val) - len(raw_val.lstrip())
        val = raw_val.strip()
        terms.append(Term(normalize_label(m.group(1)), m.group(1).strip(), val,
                          start + lead, start + lead + len(val)))
    return terms


def valid_cusip(value):
    """9 characters with a correct modulus-10 "double-add-double" check digit."""
    s = (value or "").strip().upper()
    if len(s) != 9 or not s[8].isdigit():
        return False
    total = 0
    for i, ch in enumerate(s[:8]):
        if ch.isdigit():
            v = int(ch)
        elif "A" <= ch <= "Z":
            v = ord(ch) - ord("A") + 10
        else:
            return False
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(s[8])


def _span_of(term, m, group=1):
    return (term.value_start + m.start(group), term.value_start + m.end(group))


def _parse_for(field, term):
    """(value, raw, span) read from one term's value, or None if not certain."""
    v = term.value
    if field == "cusip":
        for m in _CUSIP_RE.finditer(v[:80]):
            if valid_cusip(m.group(1)):
                return m.group(1), m.group(1), _span_of(term, m)
        return None
    if field == "issuer":
        # A line break ends the name too: RBC's value ran on into the next line
        # ("Royal Bank of Canada\nThe common stock of Amazon...").
        name = re.split(r"\s*(?:\n|\(|;|\.\s|\.$|,\s+a\s|,\s+an\s)", v, maxsplit=1)[0].strip()
        if 2 <= len(name) <= 120 and not re.search(r"\d{4}", name):
            return name, name, (term.value_start, term.value_start + len(name))
        return None
    if field in ("maturity_date", "pricing_date", "issue_date"):
        m = _DATE_RE.search(v[:120])
        if not m:
            return None
        iso = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
        return iso, m.group(0), _span_of(term, m, 0)
    if field == "contingent_coupon_rate":
        m = _RATE_RE.search(v[:200])
        return (float(m.group(1)), m.group(1), _span_of(term, m, 1)) if m else None
    if field in ("coupon_barrier_pct", "barrier_pct"):
        # A note on several underlyings states one threshold per underlying
        # ("With respect to X: ... 75% of its starting value. With respect to
        # Y: ..."). One number is only THE threshold when every one of them
        # agrees; if they differ the field is per-underlying -- leave it.
        found = list(_PCT_OF_INITIAL_RE.finditer(v[:700])) or \
            [m for m in [_PCT_LEAD_RE.search(v[:300])] if m]
        if not found:
            return None
        pcts = {float(m.group(1)) for m in found}
        if len(pcts) != 1:
            return None
        m = found[0]
        pct = float(m.group(1))
        return (pct, m.group(1), _span_of(term, m, 1)) if 0 < pct <= 100 else None
    return None


def _table_threshold(text, header):
    """Wells Fargo prints thresholds in a small table: a header like
    "Coupon Threshold Value(2)" and the percent in footnote "(2) 50% of the
    starting value". Read the percent from the footnote the header points at."""
    # Footnote markers are digits (Wells Fargo "(2)"), letters (RBC "(b)") or
    # stars (Citi "Downside threshold price**" ... "** For each of the
    # underlying shares, 50.00% of its initial share price").
    m = re.search(re.escape(header) + r"\s*(?:\(([a-z\d])\)|(?<![*])(\*{1,3})(?![*]))", text, re.I)
    if not m:
        return None
    marker = (r"\(" + re.escape(m.group(1)) + r"\)" if m.group(1)
              else r"(?<![*])" + re.escape(m.group(2)) + r"(?![*])")
    # RBC puts a lead-in before the figure: "(b) With respect to each
    # Underlying Stock, 60% of its starting value".
    fn = re.search(marker + r"\s*(?:[^%()\n]{0,80}?,\s*)?"
                   r"(\d{1,3}(?:\.\d+)?)\s?%\s*" + _OF_START,
                   text[m.end():m.end() + 3000], re.I)
    if not fn:
        return None
    pct = float(fn.group(1))
    s = m.end() + fn.start(1)
    return (pct, fn.group(1), (s, s + len(fn.group(1)))) if 0 < pct <= 100 else None


def extract_key_terms(text, fields=None):
    """{field: KeyTermValue} for every field this module can read with
    certainty. Fields it cannot read are absent (left for the model)."""
    wanted = set(fields) if fields else set(FIELD_LABELS) | {
        "estimated_value_per_1000", "estimated_value_low", "estimated_value_high", "filing_stage"}
    terms = [t for t in parse_terms(text) if not t.label.startswith("hypothetical")]
    known = sorted({lab for labs in FIELD_LABELS.values() for lab in labs}, key=len, reverse=True)
    by_label = {}
    for t in terms:
        by_label.setdefault(t.label, t)          # first occurrence: the terms block
        # Run-on text with no period ("...America Corporation CUSIP::") glues
        # earlier words onto the label; match on its trailing known phrase.
        if by_label[t.label] is t:
            for lab in known:
                if t.label != lab and t.label.endswith(" " + lab):
                    prefix = t.label[: -len(lab)].split()
                    if prefix and prefix[-1] not in _QUALIFIERS:
                        by_label.setdefault(lab, t)
                    break      # "call threshold price" is its own term, not "threshold price"
    out = {}

    for field, labels in FIELD_LABELS.items():
        if field not in wanted:
            continue
        for lab in labels:
            t = by_label.get(lab)
            if t is None:
                continue
            got = _parse_for(field, t)
            bound = None
            if got and not _committed(text, got[2][0], got[2][1]):
                if field == "contingent_coupon_rate" and _floor(text, got[2][0], got[2][1]):
                    bound = "min"
                else:
                    got = None     # a range / to-be-set figure: the model's call
            if got:
                out[field] = KeyTermValue(field, got[0], got[1], got[2], t.raw_label, "term", bound)
                break

    if "contingent_coupon_rate" in wanted and "contingent_coupon_rate" not in out:
        for t in terms:
            if t.label.startswith(("contingent coupon", "contingent interest")):
                m = _RATE_PROSE_RE.search(t.value)
                if m:
                    s, e = _span_of(t, m)
                    floor = _floor(text, s, e)
                    rounded = re.search(r"equivalent to a contingent (?:coupon|interest) rate of "
                                        r"approximately\s*$", text[max(0, s - 80):s], re.I)
                    if floor or rounded or _committed(text, s, e):
                        out["contingent_coupon_rate"] = KeyTermValue(
                            "contingent_coupon_rate", float(m.group(1)), m.group(1), (s, e),
                            t.raw_label, "term-prose", "min" if floor else None)
                    break

    # Thresholds in a table with footnoted percents (Wells Fargo).
    for field, header in (("coupon_barrier_pct", "Coupon Threshold Value"),
                          ("barrier_pct", "Downside Threshold Value"),
                          ("barrier_pct", "Downside Threshold Price"),
                          # Citi index notes: "Coupon barrier value** Final
                          # barrier value***" over a per-index table.
                          ("coupon_barrier_pct", "Coupon Barrier Value"),
                          ("barrier_pct", "Final Barrier Value")):
        if field in wanted and field not in out:
            got = _table_threshold(text, header)
            if got:
                out[field] = KeyTermValue(field, got[0], got[1], got[2], header, "term-table")

    # One threshold for both coupon and downside: when no coupon-specific label
    # exists and the coupon term itself is conditioned on the threshold the
    # downside uses ("...closing price is greater than or equal to the threshold
    # price"), the coupon barrier IS that threshold.
    if "coupon_barrier_pct" in wanted and "coupon_barrier_pct" not in out and "barrier_pct" in out:
        bar = out["barrier_pct"]
        bar_words = normalize_label(bar.label)
        for t in terms:
            if (t.label.startswith(("contingent coupon", "contingent interest"))
                    and not t.label.endswith(("rate", "dates", "date"))
                    and bar_words and bar_words in t.value.lower()):
                out["coupon_barrier_pct"] = KeyTermValue(
                    "coupon_barrier_pct", bar.value, bar.raw, bar.span, bar.label, "term-shared")
                break

    preliminary = bool(_PRELIM_RE.search(text[:2000]))

    # A preliminary supplement states a range (or a floor) for the estimated
    # value; no point value exists yet.
    if preliminary and wanted & {"estimated_value_per_1000", "estimated_value_low",
                                 "estimated_value_high"}:
        m = _EST_RANGE_RE.search(text)
        if m:
            lo_g = 1 if m.group(1) else 3
            lo = float(m.group(lo_g))
            hi = float(m.group(2)) if m.group(2) else None
            if 800 <= lo <= 1000 and (hi is None or lo <= hi <= 1000):
                if "estimated_value_low" in wanted:
                    out["estimated_value_low"] = KeyTermValue(
                        "estimated_value_low", lo, m.group(lo_g), m.span(lo_g), "prose", "prose")
                if hi is not None and "estimated_value_high" in wanted:
                    out["estimated_value_high"] = KeyTermValue(
                        "estimated_value_high", hi, m.group(2), m.span(2), "prose", "prose")
                # Say "no point value" with the range as evidence, instead of
                # asking a model that has returned 13.5, 1 and 10.2 here.
                if "estimated_value_per_1000" in wanted:
                    end = m.end(2) if hi is not None else m.end(lo_g)
                    out["estimated_value_per_1000"] = KeyTermValue(
                        "estimated_value_per_1000", None, text[m.start(lo_g):end],
                        (m.start(lo_g), end), "prose", "prose-range")

    # Estimated value is stated in prose, not as a term line.
    if "estimated_value_per_1000" in wanted and "estimated_value_per_1000" not in out:
        m = _EST_VALUE_RE.search(text)
        if m and not _committed(text, m.start(1), m.end(1)):
            m = None           # "expected to be between $920.00 and $970.00"
        if m:
            val = float(m.group(1).replace(",", ""))
            if 900 <= val <= 1000:
                out["estimated_value_per_1000"] = KeyTermValue(
                    "estimated_value_per_1000", val, m.group(1), m.span(1), "prose", "prose")

    # Stage: the cover legend says preliminary; "final" only when there is no
    # legend AND the filing commits to a point estimated value.
    if "filing_stage" in wanted:
        lg = _PRELIM_RE.search(text[:2000])
        ev = out.get("estimated_value_per_1000")
        if lg:
            out["filing_stage"] = KeyTermValue("filing_stage", "preliminary", lg.group(0),
                                               lg.span(), "cover legend", "legend")
        elif ev is not None and ev.value is not None:
            out["filing_stage"] = KeyTermValue("filing_stage", "final", ev.raw, ev.span,
                                               "prose", "committed-estimate")
    return out


if __name__ == "__main__":
    sample = ("Issuer:: BofA Finance LLC (“BofA Finance”). Guarantor:: Bank of America "
              "Corporation. CUSIP:: 09712GX58 Maturity Date:: October 3, 2029, subject to "
              "postponement. Contingent Coupon Rate:: The Contingent Coupon Rate is 19.10% per annum. "
              "Coupon Barrier:: $119.392, which is equal to 80.00% of the Starting Price. "
              "Threshold Price:: $119.392, which is equal to 80.00% of the Starting Price. "
              "Hypothetical Threshold Price:: $80.00 The estimated value of the Securities as "
              "of the Pricing Date is $962.80 per Security.")
    got = extract_key_terms(sample)
    assert got["cusip"].value == "09712GX58"
    assert got["maturity_date"].value == "2029-10-03"
    assert got["contingent_coupon_rate"].value == 19.10
    assert got["coupon_barrier_pct"].value == 80.0 and got["barrier_pct"].value == 80.0
    assert got["estimated_value_per_1000"].value == 962.80
    assert got["issuer"].value == "BofA Finance LLC"
    for k in got.values():
        assert sample[k.span[0]:k.span[1]] == k.raw, k
    print("key_terms self-check: PASS")
