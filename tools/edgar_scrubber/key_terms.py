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
    "isin": ("isin", "cusip / isin", "cusip/isin"),
    "guarantor": ("guarantor", "guarantee"),
    "calculation_agent": ("calculation agent",),
    # "$1,000 per security" / "$1,000 and any integral multiple of $1,000".
    "denomination": ("denominations", "denomination", "minimum denominations",
                     "stated principal amount", "principal amount", "face amount"),
    "coupon_frequency": ("contingent coupon payment dates", "contingent interest payment dates",
                         "coupon payment dates", "interest payment dates"),
    # The autocall level. Never the downside barrier: "call threshold price" is
    # kept out of barrier_pct by _QUALIFIERS, and lives here instead.
    "autocall_barrier_pct": ("call threshold price", "call threshold value", "call threshold level",
                             "call value", "call level", "call barrier", "autocall level",
                             "autocall barrier", "redemption threshold"),
    # What an autocall pays on top of principal, as a percent of it. JPMorgan
    # states it in dollars: "Call Premium Amount:: $147.50 per $1,000" = 14.75.
    "call_premium": ("call premium amount", "call premium", "call return",
                     "call premium percentage"),
    "buffer_pct": ("buffer amount", "buffer percentage", "buffer"),
    "downside_leverage": ("downside leverage factor",),
    "underlyings": ("underlyings", "underlying", "underlying stocks", "underlying stock",
                    "underlying shares", "underlying indices", "underlying index",
                    "underlying funds", "underlying fund", "reference stocks", "reference stock",
                    "reference assets", "reference asset", "market measures", "market measure",
                    "underliers", "underlier", "indices", "index", "fund"),
    "issuer": ("issuer", "company (issuer)", "issuer (company)"),
    "maturity_date": ("maturity date", "stated maturity date"),
    "pricing_date": ("pricing date", "trade date"),
    # JPMorgan: "Original Issue Date (Settlement Date):: On or about ...".
    "issue_date": ("issue date", "original issue date", "settlement date",
                   "original issue date (settlement date)"),
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
               "hypothetical", "upside", "cap", "knock-out",
               # "Least Performing Index:" is a definition, not the index.
               "performing", "least", "lesser", "worst", "lowest", "best"}

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


_ISIN_RE = re.compile(r"\b([A-Z]{2}[0-9A-Z]{9}\d)\b")
_FREQ = {"monthly": "monthly", "quarterly": "quarterly", "semiannually": "semiannual",
         "annually": "annual"}


def valid_isin(value):
    """12 characters, country prefix, Luhn check digit over the letters-as-numbers."""
    s = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{9}\d", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
        total += d // 10 + d % 10
    return (10 - total % 10) % 10 == int(s[-1])


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


# A name ends at a line break, a ";", a sentence end, ", a/an/which ...", or a
# parenthetical that DEFINES a term ("(“BofAS”)", "(the “Bank”)") -- not at any
# "(": "HSBC Securities (USA) Inc." keeps its "(USA)" (golden set, 2026-10-19).
# "Inc. / Corp. / Co. / Ltd." is not a sentence end ("Goldman Sachs & Co. LLC",
# "BofA Securities, Inc. (“BofAS”)") unless a sentence plainly starts after it.
_NOT_ABBR = r"(?<![A-Z])(?<!\bInc)(?<!\bCorp)(?<!\bCo)(?<!\bLtd)"
_SENTENCE = r"(?=(?:The|This|These|We|See|All|Each|Any|It|Our|Payments?|If|For)\s)"
_NAME_END = re.compile(r"\s*(?:\n|\((?=[“\"']|the\s|an?\s|collectively|each|together)|;"
                       rf"|{_NOT_ABBR}\.\s|{_NOT_ABBR}\.$|\.\s+{_SENTENCE}"
                       r"|,\s+a\s|,\s+an\s|,\s+which\s)")
# "Inc." / "Corp." / "Co." / "Ltd." / "N.A." keep their period: it is part of
# the legal name ("BofA Securities, Inc."), unlike a sentence-ending one.
_ABBREV_END = re.compile(r"(?:\b(?:Inc|Corp|Co|Ltd|Plc|PLC|L\.P|N\.A|S\.A|N\.V|AG|LLC|Corporation)\.?)$")


def _trim_name(name):
    name = name.strip().rstrip(",;")
    name = re.sub(r"[’']s$", "", name)          # possessive: "...Markets Inc.’s"
    if name.endswith(".") and not re.search(r"\b(?:Inc|Corp|Co|Ltd|L\.P|N\.A|S\.A|N\.V)\.$", name):
        name = name[:-1]
    return name.strip()


def _entity(term):
    """A legal-entity name at the start of a term value."""
    raw = _NAME_END.split(term.value, maxsplit=1)[0]
    name = _trim_name(raw)
    if 2 <= len(name) <= 120 and not re.search(r"\d{4}", name):
        return name, name, (term.value_start, term.value_start + len(name))
    return None


def _resolve_defined(text, kt):
    """BofA writes "Guarantor:: BAC." after defining "Bank of America Corporation
    (“BAC” or the “Guarantor”)" on the cover. A short all-caps name is a defined term: report the
    entity it was defined as, with the definition's span."""
    if not (kt and isinstance(kt.value, str)):
        return kt
    # An abbreviation ("BAC", "CGMI") or a one-word defined term ("The Bank" after
    # "The Toronto-Dominion Bank (the “Bank”)") stands for the entity defined.
    word = re.fullmatch(r"(?:[Tt]he\s+)?([A-Z][A-Za-z]{2,14})", kt.value)
    if re.fullmatch(r"[A-Z&]{2,6}", kt.value):
        term_name = kt.value
    elif word and word.group(1) in ("Bank", "Company", "Issuer", "Guarantor", "Parent", "Agent"):
        term_name = word.group(1)
    else:
        return kt
    # The defined name: a run of capitalised words ("Bank of America Corporation").
    m = re.search(r"([A-Z][\w.&'’-]*(?:\s+(?:[A-Z][\w.&'’-]*|of|&|and)){0,7})\s*\((?:the\s+)?[“\"]" + re.escape(term_name) + r"[”\"](?:\)|\s+or\s|,)", text)
    if not m:
        return None          # an unexplained abbreviation is not the legal name
    name = _trim_name(m.group(1))
    # The span points at the name as printed (a line break may sit inside it:
    # Citi's "Citigroup Global Markets\nInc."); the value is the name itself.
    return KeyTermValue(kt.field, " ".join(name.split()), name,
                        m.span(1)[0:1] + (m.start(1) + len(name),),
                        kt.label, kt.method + "+defined")


# A ticker after each name: "(Bloomberg ticker: SPX)", "(ticker symbol: “ZTS
# UN”)", "(current Bloomberg ticker: “GOOGL UW”)", "(Nasdaq Global Select
# Market symbol: “SPCX”)".
_TICKER_RE = re.compile(r"\s*\((?:current\s+)?(?:Bloomberg ticker|ticker symbol|"
                        r"[A-Z][A-Za-z ]{0,40}symbol)\s*:[^)]*\)")
_STOCK_LEAD = re.compile(r"^(?:the\s+|shares\s+of\s+)?(?:(?:class\s+[a-z]\s+)?(?:common|ordinary)\s+"
                         r"(?:stock|shares)|american depositary shares)\s+of\s+", re.I)
_FUNDISH = re.compile(r"\b(?:index|average|etf|fund|trust)(?:SM|TM)?\b", re.I)   # "IndexSM"
# A company name: Citi prints underlyings bare, with no "common stock of" lead-in
# ("Broadcom Inc., NVIDIA Corporation and Alphabet Inc.", #349).
_CORP_SUFFIX = re.compile(r"\b(?:Inc\.?|Incorporated|Corporation|Corp\.?|Company|Co\.|Ltd\.?|Limited|"
                          r"plc|N\.V\.?|S\.A\.?|SE|AG|Holdings|Group)$")
_LIST_SEP = re.compile(r",\s*(?:and\s+)?|\s+and\s+")


def _split_bare_list(text):
    """'A Inc., B Corporation and C Inc.' -> ['A Inc.', 'B Corporation', 'C Inc.'],
    splitting only where the text so far ends in a company suffix; anything
    else -- an index name with "and" in it -- stays one piece."""
    parts, start = [], 0
    for m in _LIST_SEP.finditer(text):
        if _CORP_SUFFIX.search(text[start:m.start()].strip()):
            parts.append(text[start:m.start()])
            start = m.end()
    parts.append(text[start:])
    if len(parts) > 1 and all(_CORP_SUFFIX.search(p.strip().rstrip(".") + ("." if p.strip().endswith(".") else ""))
                              for p in parts):
        return parts
    return [text]


def _clean_underlying(item):
    """One list item -> (name as printed, kind) or None."""
    # Leftovers of the previous member: '(the “Index”) and the iShares...',
    # '(each referred to as an “Index,” ...) and Technology Select...'.
    item = re.sub(r"^(?:\s*\([^()]*\))+", "", item.strip())
    item = re.sub(r"^(?:,\s*)?(?:and\s+)?", "", item.strip()).strip()
    # A table row (Citi: 'Underlying shares Initial share price* ... $266.43
    # $133.215 Shares of Class A Common Stock of Alphabet Inc.'): the member
    # starts at its "Shares of" / "common stock of".
    rows = list(re.finditer(r"(?:shares\s+of\s+)?(?:the\s+)?(?:class\s+[a-z]\s+)?common\s+stock\s+of\s+", item, re.I))
    if rows and rows[-1].start() > 0:
        item = item[rows[-1].start():]
    stock = bool(_STOCK_LEAD.match(item))
    name = _STOCK_LEAD.sub("", item)
    name = re.split(r",\s+(?:no\s+)?par value\b|\s*\(", name, maxsplit=1)[0].strip()
    if not stock:
        name = re.sub(r"^the\s+", "", name, flags=re.I)
    name = name.rstrip(" ,;")
    # A sentence-ending period is not part of the name; "Inc." / "Corp." is.
    if name.endswith(".") and not re.search(
            r"\b(?:Inc|Corp|Co|Ltd|plc|N\.V|S\.A|S\.E|A\.G|L\.P|S\.p\.A)\.$", name):
        name = name[:-1]
    if not (2 <= len(name) <= 100) or re.search(rf"[$%:*()]|\b(?:{_MONTHS})\b", name):
        return None
    if stock:
        kind = "single_stock"
    elif re.search(r"\b(?:etf|fund|trust)\b", name, re.I):
        kind = "etf"
    elif _FUNDISH.search(name):
        kind = "index"
    elif _CORP_SUFFIX.search(name):
        kind = "single_stock"     # a bare company name (#349)
    else:
        return None       # not recognisably a stock, fund or index: leave it
    return name, kind


def _underlyings(term):
    """The reference assets named on an underlying line, in order:
    ([names], first name, its span). A table header ("Underlying Initial
    underlying value* ...") is not a list of names and is left to the model."""
    v = term.value[:900]
    if re.match(r"(?:(?:underlying|market measure)\s+(?:initial|bloomberg|ticker|starting)|bloomberg\s+ticker)", v, re.I):
        return None
    pieces, pos = [], 0
    for m in _TICKER_RE.finditer(v):
        pieces.append((pos, v[pos:m.start()]))
        pos = m.end()
    if not pieces:
        # No tickers: "Common stock of NVIDIA Corporation (the “Underlying Stock”)."
        # Drop defined-term parentheticals first -- HSBC lists "The common stock
        # of Snowflake Inc. (“SNOW”) and the common stock of ServiceNow, Inc.
        # (“NOW”) (each referred to as ...)", and cutting at the first "(" lost
        # ServiceNow (golden set, 2026-10-19).
        head = re.sub(r"\s*\((?:[“\"]|the\s|each\s|collectively\s|together\s|an?\s)[^()]*\)", "", v)
        head = re.split(r"\s*\(|\n|\.\s+(?=(?:We|The|Each|Its|This|In)\b)", head, maxsplit=1)[0]
        parts = re.split(r",\s*(?:and\s+)?(?=the\s)|\s+and\s+(?=the\s)", head, flags=re.I)
        if len(parts) == 1:
            parts = _split_bare_list(head)
        pieces = [(0, part) for part in parts]
    names, kinds, first_span = [], [], None
    for off, piece in pieces:
        # A piece ends at its ticker; anything after a sentence end belongs to
        # the previous item's description, not to this name.
        piece = re.split(r"\)\.\s|\.\s(?=[A-Z])", piece)[-1]
        got = _clean_underlying(piece)
        if got is None:
            return None              # one unreadable member: the whole list is the model's
        name, kind = got
        idx = term.value.find(name, off)
        if idx < 0:
            return None
        if first_span is None:
            first_span = (term.value_start + idx, term.value_start + idx + len(name))
        names.append(name)
        kinds.append(kind)
    if not names or len(names) > 12:
        return None
    return names, names[0], first_span


_NUM_TOKEN = r"\$?\d[\d,.]*(?=\s|$)"
_FUND_NAME = re.compile(rf"\s*(?<!\S)((?![\d,.$])(?:(?!{_NUM_TOKEN})\S+\s+){{0,9}}?(?!{_NUM_TOKEN})\S*?"
                        r"(?:Index|Average|ETF|Fund|Trust)(?:®|TM|SM)?)(?=\s|$)")
_LEVEL = re.compile(r"\$?\d{1,3}(?:,\d{3})*(?:\.\d+)?")


def _table_underlyings(text):
    """Underlyings printed as a table, not a sentence. ([names], first, span).

    Citi:  'Underlying Initial underlying value* Coupon barrier value** Final
            barrier value*** Nasdaq-100 Index® 29,433.43 20,603.401 ...
            Russell 2000® Index 2,972.372 ... *For each underlying, ...'
    BMO / Wells Fargo:  'Market Measure Bloomberg Ticker Symbol Starting
            Value(1) ... The Common Stock of D.R. Horton, Inc. DHI $127.15 ...'
    """
    m = re.search(r"Underlying\s+Initial underlying value\*.*?\s\*\s?For each", text[:60000], re.S)
    if m:
        # The names start after the LAST column header ("... value***").
        heads = list(re.finditer(r"value\*+\s+", m.group(0)))
        base = m.start() + heads[-1].end()
        tail = re.search(r"\s\*\s?For each$", m.group(0))
        region = text[base:m.end() - len(tail.group(0))]
        names, pos = [], 0
        for n in _FUND_NAME.finditer(region):
            gap = region[pos:n.start()]
            if _LEVEL.sub("", gap).strip():
                return None          # something in the table this does not understand
            name = n.group(1).strip()
            if re.match(r"\$?\d", name):
                return None
            names.append((name, base + n.start(1)))
            pos = n.end()
        if not names or _LEVEL.sub("", region[pos:]).strip():
            return None
        return [x for x, _ in names], names[0][0], (names[0][1], names[0][1] + len(names[0][0]))
    m = re.search(r"Market Measure\s+Bloomberg Ticker Symbol\s+Starting Value\(1\)[^\n]*?Value\(3\)", text[:60000])
    if m:
        end = text.find("(1)", m.end())
        region = text[m.end():end if end > 0 else m.end() + 3000]
        out = []
        for r in re.finditer(r"(?:Market Measures?::\s*)?((?:The\s+)?(?:Class\s+[A-Z]\s+)?Common\s+(?:Stock|Shares)\s+of\s+.+?)"
                             r"\s+[A-Z]{1,5}\s+\$", region):
            got = _clean_underlying(r.group(1))
            if got is None:
                return None
            idx = text.find(got[0], m.end() + r.start(1))
            out.append((got[0], idx))
        if out:
            return [x for x, _ in out], out[0][0], (out[0][1], out[0][1] + len(out[0][0]))
    # JPMorgan multi-stock notes: "Reference Stocks: As specified under 'Key
    # Terms Relating to the Reference Stocks'", then a table whose rows read
    # "(Class A) common stock of NAME, par value $X per share [:] TICKER", often
    # wrapped across lines (5 of 7 JPMorgan NVIDIA notes had no underlyings
    # before this, found by the research digest 2026-10-19).
    m = re.search(r"Reference Stocks?:{0,2}\s+Bloomberg\s+Ticker", text[:80000])
    if m:
        stop = re.search(r"According to publicly available|\n\s*\(\d\)|Key Terms Relating to the Review", text[m.end():m.end() + 6000])
        region = text[m.end():m.end() + (stop.start() if stop else 4000)]
        flat = re.sub(r"\s+", " ", region)
        names = []
        for r in re.finditer(r"(?:(?:Class\s+[A-Z]\s+)?(?:common|ordinary)\s+(?:stock|shares)|"
                             r"American depositary shares)\s+of\s+(.+?),?\s+(?:no\s+)?par\s+value",
                             flat, re.I):
            name = _trim_name(r.group(1))
            if not (2 <= len(name) <= 100) or re.search(r"[$%:*()]", name):
                return None
            if name not in names:
                names.append(name)
        if names:
            # Span of the first name, matched across the table's line breaks.
            pat = r"\s+".join(re.escape(w) for w in names[0].split())
            s = re.search(pat, text[m.end():])
            span = (m.end() + s.start(), m.end() + s.end()) if s else (m.start(), m.end())
            return names, text[span[0]:span[1]], span
    return None


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
        return _entity(term)
    if field in ("guarantor", "calculation_agent"):
        # Citi: "Guarantee:: All payments due on the securities are fully and
        # unconditionally guaranteed by Citigroup Inc."
        g = re.search(rf"guaranteed by\s+(.{{2,80}}?)(?:{_NOT_ABBR}\.(?:\s|$)|\.\s+{_SENTENCE}|\n|,\s|$)", v[:300])
        if field == "guarantor" and g:
            name = g.group(1).strip()
            s0 = term.value_start + g.start(1)
            return name, name, (s0, s0 + len(name))
        if v.lower().startswith(("all ", "the notes", "the securities")):
            return None
        return _entity(term)
    if field == "underlyings":
        return _underlyings(term)
    if field == "isin":
        for m in _ISIN_RE.finditer(v[:120]):
            if valid_isin(m.group(1)):
                return m.group(1), m.group(1), _span_of(term, m)
        return None
    if field == "denomination":
        m = re.match(r"\$\s?(\d{1,3}(?:,\d{3})*)(?:\.00)?\s+(?:per\b|and\b|\(|$)", v)
        if not m:
            return None
        amt = float(m.group(1).replace(",", ""))
        return (amt, m.group(1), _span_of(term, m)) if 1 <= amt <= 1_000_000 else None
    if field == "coupon_frequency":
        m = re.match(r"(monthly|quarterly|semi-?annually|annually)\b", v, re.I)
        if not m:
            return None
        return _FREQ[m.group(1).lower().replace("-", "")], m.group(1), _span_of(term, m)
    if field == "call_premium":
        # One figure for the note. A schedule ("with respect to each Review
        # Date is set forth below: first Review Date: 18.5250% ...") is per
        # date, not one number -- leave it.
        if re.search(r"each review date|set forth below|\bfirst\b", v[:200], re.I):
            return None
        lead = r"(?:at least|not less than|no less than|a minimum of)?\s*"
        m = re.match(lead + r"\$\s?(\d{1,3}(?:\.\d+)?)\s+per\s+\$1,000\b", v, re.I)
        if m:
            pct = round(float(m.group(1)) / 10, 6)
            return (pct, m.group(1), _span_of(term, m)) if 0 < pct <= 100 else None
        m = re.match(lead + r"(\d{1,3}(?:\.\d+)?)\s?%", v, re.I)
        if m:
            pct = float(m.group(1))
            return (pct, m.group(1), _span_of(term, m)) if 0 < pct <= 100 else None
        return None
    if field == "buffer_pct":
        m = re.match(r"(\d{1,2}(?:\.\d+)?)\s?%", v)
        return (float(m.group(1)), m.group(1), _span_of(term, m)) if m else None
    if field == "downside_leverage":
        m = re.match(r"(\d{1,2}(?:\.\d+)?)\b(?!\s?%)", v)
        return (float(m.group(1)), m.group(1), _span_of(term, m)) if m else None
    if field in ("maturity_date", "pricing_date", "issue_date"):
        m = _DATE_RE.search(v[:120])
        if not m:
            return None
        iso = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date().isoformat()
        return iso, m.group(0), _span_of(term, m, 0)
    if field == "contingent_coupon_rate":
        m = _RATE_RE.search(v[:200])
        return (float(m.group(1)), m.group(1), _span_of(term, m, 1)) if m else None
    if field in ("coupon_barrier_pct", "barrier_pct", "autocall_barrier_pct"):
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
        top = 200 if field == "autocall_barrier_pct" else 100
        return (pct, m.group(1), _span_of(term, m, 1)) if 0 < pct <= top else None
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


# One bullet of a per-review-date call premium schedule. Anything between the
# end of one bullet and the next (the "set forth below:" lead-in, bullets,
# line breaks) is skipped lazily, up to 120 characters.
_SCHEDULE_ITEM_RE = re.compile(
    r"[\s\S]{0,120}?\b[a-z][a-z\-]*\s+review date:\s*(at least\s*)?"
    r"(\d{1,3}(?:\.\d+)?)\s?%\s*[×x]\s*\$1,000", re.I)
_DENOM_PROSE_RE = re.compile(
    r"minimum denominations of \$\s?(\d{1,3}(?:,\d{3})*)(?:\.00)?\s+and integral multiples", re.I)


def extract_key_terms(text, fields=None):
    """{field: KeyTermValue} for every field this module can read with
    certainty. Fields it cannot read are absent (left for the model)."""
    wanted = set(fields) if fields else set(FIELD_LABELS) | {
        "estimated_value_per_1000", "estimated_value_low", "estimated_value_high", "filing_stage",
        "call_premium_schedule"}
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
                if field in ("contingent_coupon_rate", "call_premium") \
                        and _floor(text, got[2][0], got[2][1]):
                    bound = "min"
                else:
                    got = None     # a range / to-be-set figure: the model's call
            if got:
                out[field] = KeyTermValue(field, got[0], got[1], got[2], t.raw_label, "term", bound)
                break

    for f in ("issuer", "guarantor", "calculation_agent"):
        if f in out:
            r = _resolve_defined(text, out[f])
            if r is None:
                del out[f]
            else:
                out[f] = r

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

    if "underlyings" in wanted and "underlyings" not in out:
        got = _table_underlyings(text)
        if got:
            out["underlyings"] = KeyTermValue("underlyings", got[0], got[1], got[2],
                                              "underlying table", "term-table")

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

    if "coupon_frequency" in wanted and "coupon_frequency" not in out:
        for t in terms:
            if not t.label.startswith(("contingent coupon", "contingent interest")):
                continue
            m = (re.search(r"payable at (?:a rate of )?(?:at least )?[\d.]+%\s+per\s+(month|quarter|year)", t.value[:300], re.I)
                 or re.search(r"\b(monthly|quarterly|semi-?annual|annual)\s+contingent (?:coupon|interest) payment date",
                              t.value[:300], re.I))
            if m:
                word = m.group(1).lower().replace("-", "")
                val = {"month": "monthly", "quarter": "quarterly", "year": "annual",
                       "semiannual": "semiannual", "annual": "annual"}.get(word, word)
                s0, e0 = _span_of(t, m)
                out["coupon_frequency"] = KeyTermValue("coupon_frequency", val, m.group(1),
                                                       (s0, e0), t.raw_label, "term-prose")
                break

    # A call premium stated per review date (JPMorgan Review Notes, #334):
    # "first Review Date: at least 18.5250% x $1,000 * second Review Date: ...".
    # The schedule runs past a term's length cap, so it is read from the text
    # after the term's start, one bullet after another, in date order.
    if "call_premium_schedule" in wanted and "call_premium" not in out:
        for t in terms:
            if t.label not in ("call premium amount", "call premium"):
                continue
            pos, vals, hedged, first, last = t.value_start, [], False, None, None
            while True:
                m = _SCHEDULE_ITEM_RE.match(text, pos)
                if not m:
                    break
                vals.append(float(m.group(2)))
                hedged = hedged or bool(m.group(1))
                first = m.start(2) if first is None else first
                last = m.end(2)
                pos = m.end()
            if len(vals) >= 2:
                out["call_premium_schedule"] = KeyTermValue(
                    "call_premium_schedule", vals, text[first:last], (first, last),
                    t.raw_label, "term-schedule", "min" if hedged else None)
            break

    # JPMorgan states the denomination as a cover bullet, not a term (#336):
    # "Minimum denominations of $1,000 and integral multiples thereof".
    if "denomination" in wanted and "denomination" not in out:
        m = _DENOM_PROSE_RE.search(text)
        if m:
            amt = float(m.group(1).replace(",", ""))
            if 1 <= amt <= 1_000_000:
                out["denomination"] = KeyTermValue("denomination", amt, m.group(1),
                                                   (m.start(1), m.end(1)), "denominations",
                                                   "prose")

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
