"""
HTML -> text normalization with source span offsets, plus the stage-1 table
pre-parse (issue #101, part of #95, built on #99's `edgar_client`).

Two things downstream depends on that a plain `.get_text()`-style stripper
does not give you:

  1. SPAN OFFSETS. Every character of normalized output maps back to an exact
     offset range in the original document bytes. #105 highlights the source
     span during exemplar review; #107 induces regex anchors from where a
     span landed. Extraction that returns bare strings with no provenance
     kills both -- so `OffsetMap.resolve()` is not an add-on, it is the
     contract this module exists to provide.

  2. TABLE PRE-PARSE (stage 1 of #101's four-stage reduction). Key terms live
     in HTML `<table>` elements. Feeding raw table markup to a 7B model costs
     ~2000 tokens and lands exactly where a 7B is weakest: numeric values in
     nested/merged cells. Parsed deterministically into flat
     ``Label: value`` pairs it costs ~200 tokens and the model's job becomes
     "map a label onto the schema" instead of "read a table". This module
     resolves colspan/rowspan into a full grid (so a merged cell's value
     still lands under every column/row it spans) and recurses into nested
     tables, because SEC filings routinely nest a single-cell wrapper table
     around the real one for layout.

stdlib only (`html.parser`, `re`). Run the self-check:
    python tools/edgar_scrubber/normalize.py
"""
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from html.parser import HTMLParser
from html import unescape

# --------------------------------------------------------------------------- #
# Offset map -- text position -> source position, through arbitrary reduction
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TextSpan:
    """One contiguous run of the normalized text and where it came from.

    ``literal`` True means ``source[source_start:source_end]`` was copied
    character-for-character into ``text[text_start:text_end]`` (so any
    interior offset can be resolved by adding a constant delta). False means
    the run was produced some other way (an HTML entity decoded to a
    different length, or synthetic whitespace inserted for a stripped tag) --
    any offset inside it resolves to the whole ``(source_start, source_end)``
    range rather than an interpolated point, which is still an EXACT source
    range, just not a sub-range.
    """

    text_start: int
    text_end: int
    source_start: int
    source_end: int
    literal: bool = True


class OffsetMap:
    """Piecewise map from normalized-text offsets back to source offsets.

    Built from a list of `TextSpan`s that are contiguous and non-overlapping
    in text order (gaps are allowed and are resolved by clamping to the
    nearest span -- see `resolve`). Source offsets are monotonically
    non-decreasing across spans in text order: normalization never reorders
    content, only removes/collapses markup.
    """

    def __init__(self, spans):
        self.spans = list(spans)
        self._starts = [s.text_start for s in self.spans]
        by_source = sorted(self.spans, key=lambda s: s.source_start)
        self._by_source = by_source
        self._source_starts = [s.source_start for s in by_source]

    def __len__(self):
        return len(self.spans)

    def text_offset_for_source(self, source_pos):
        """Inverse of `resolve`: the normalized-text offset a SOURCE offset
        falls in. Used by the #101 stage-4 "warm path" -- once #107 has
        induced an anchor from where a span landed in the source, sub-block
        routing needs to find that anchor's position in the normalized text
        to center a window on it. Exact for `literal` spans (the vast
        majority of body prose); a source offset landing inside a
        non-literal span (an entity, a table, a synthetic break) resolves to
        that span's text start -- still an exact, defensible position, just
        not sub-span-interpolated.
        """
        if not self._by_source:
            return None
        i = bisect_right(self._source_starts, source_pos) - 1
        if i < 0:
            i = 0
        span = self._by_source[i]
        if span.literal and span.source_start <= source_pos <= span.source_end:
            return span.text_start + (source_pos - span.source_start)
        return span.text_start

    def _span_for(self, pos):
        """The span covering text offset `pos`, or the nearest one before it
        if `pos` lands in a gap (synthetic separator between real content)."""
        if not self.spans:
            return None
        i = bisect_right(self._starts, pos) - 1
        if i < 0:
            i = 0
        span = self.spans[i]
        # `pos` may be inside a later span's gap-adjacent territory; walk
        # forward if `pos` is past this span's end and the next span starts
        # later (a real gap) -- clamp to whichever span is closer.
        while i + 1 < len(self.spans) and pos >= self.spans[i].text_end and \
                pos >= self.spans[i + 1].text_start:
            i += 1
            span = self.spans[i]
        return span

    def _point(self, pos, *, end=False):
        """Map text offset `pos` to a source offset. When `end` is True,
        `pos` is the index of the LAST INCLUDED character (i.e. callers pass
        `text_end - 1`, not `text_end`) and the result is the source offset
        immediately AFTER that character -- so an exclusive text range's end
        lines up with the true exclusive source end, not the last
        character's own (inclusive) source start."""
        span = self._span_for(pos)
        if span is None:
            return None
        if span.literal and span.text_start <= pos <= span.text_end:
            offset = pos - span.text_start
            return span.source_start + offset + (1 if end else 0)
        return span.source_end if end else span.source_start

    def resolve(self, text_start, text_end):
        """Map a `[text_start, text_end)` normalized-text range back to an
        exact `(source_start, source_end)` range. Endpoints are resolved
        independently and then ordered, so a range spanning several spans
        (e.g. an extracted phrase that crosses a decoded entity) still comes
        back as a single sane `(min, max)` source range."""
        if not self.spans:
            return (0, 0)
        text_end = max(text_end, text_start)
        if text_end == text_start:
            p = self._point(text_start, end=False)
            return None if p is None else (p, p)
        a = self._point(text_start, end=False)
        b = self._point(text_end - 1, end=True)
        if a is None or b is None:
            return None
        return (min(a, b), max(a, b))

    def compose(self, upstream):
        """Return a new OffsetMap that maps THIS map's text offsets straight
        through to `upstream`'s source offsets -- i.e. treat `upstream` as
        mapping "its text" -> "original source", and this map's
        `source_start/source_end` as offsets INTO `upstream`'s text.

        This is how offsets survive stage 2/3/4: each reduction stage builds
        an OffsetMap from ITS OWN input text (the previous stage's output) to
        its own output text; composing against the previous stage's map
        (recursively, back to `normalize_html`'s map into the raw HTML)
        yields a map straight from the final stage's text to the original
        document bytes.

        A `literal` span in THIS map (e.g. one paragraph, unchanged
        character-for-character from `upstream`'s text) usually straddles
        several of `upstream`'s own spans -- an entity decode, a table
        substitution, a run boundary. Naively resolving the whole span as one
        blob collapses every offset inside it to the same whole-paragraph
        range. Instead, split it at each of `upstream`'s span boundaries it
        crosses, so each piece can inherit `upstream`'s own literal/affine
        mapping and sub-paragraph precision survives the compose.
        """
        composed = []
        for s in self.spans:
            if s.literal and s.source_end > s.source_start and upstream.spans:
                lo = bisect_right(upstream._starts, s.source_start)
                hi = bisect_left(upstream._starts, s.source_end)
                points = (s.source_start, *upstream._starts[lo:hi], s.source_end)
            else:
                points = (s.source_start, s.source_end)

            for i in range(len(points) - 1):
                o0, o1 = points[i], points[i + 1]
                if o1 == o0 and len(points) > 2:
                    continue
                t0 = s.text_start + (o0 - s.source_start)
                t1 = s.text_start + (o1 - s.source_start)
                span0 = upstream._span_for(o0) if upstream.spans else None
                if s.literal and span0 is not None and span0.literal and \
                        span0.text_start <= o0 and o1 <= span0.text_end:
                    delta = span0.source_start - span0.text_start
                    composed.append(TextSpan(t0, t1, o0 + delta, o1 + delta, literal=True))
                else:
                    lo_src, hi_src = upstream.resolve(o0, o1)
                    composed.append(TextSpan(t0, t1, lo_src, hi_src, literal=False))
        return OffsetMap(composed)


# --------------------------------------------------------------------------- #
# HTML -> text, tag-aware, span-tracked
# --------------------------------------------------------------------------- #

# Tags whose content must never reach the text output.
_SKIP_TAGS = {"script", "style", "head", "title"}

# Tags that force a paragraph break (a blank line) when they close, so
# adjacent block content doesn't glue together into one run-on sentence.
_BLOCK_BREAK_TAGS = {
    "p", "div", "tr", "table", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6",
}

# Tags that force a single space/tab-equivalent break (cell/line boundaries
# inside a row) without a full paragraph break.
_SOFT_BREAK_TAGS = {"td", "th", "br"}

_ENTITY_RE = re.compile(r"&(?:#\w+|\w+);")
_TAG_RE = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<[^>]*>", re.S)
_WS_RE = re.compile(r"[ \t\xa0]+")


class _RawTable:
    """One `<table>...</table>` region located in the raw source, kept as its
    exact source span so `parse_tables` can grid-parse it independently of
    the linear text walk (tables get pulled OUT of the linear text and
    replaced by their flattened pairs -- see `normalize_html`)."""

    __slots__ = ("start", "end")

    def __init__(self, start, end):
        self.start = start
        self.end = end


def _find_top_level_tables(html):
    """Locate every top-level `<table>` region (source start/end, end
    exclusive of the closing tag) without descending into nested tables --
    a nested `<table>` is handled by `parse_tables`'s own recursion, not
    here, so it isn't double-counted as a second top-level region."""
    tables = []
    depth = 0
    start = None
    for m in re.finditer(r"<(/?)table\b[^>]*>", html, re.I):
        closing = bool(m.group(1))
        if not closing:
            if depth == 0:
                start = m.start()
            depth += 1
        else:
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    tables.append(_RawTable(start, m.end()))
                    start = None
    return tables


def _decode_literal_run(source, lo, hi, text_pos, spans):
    """Append `source[lo:hi]` to the output, entity-decoded, splitting at
    each entity so every run in `spans` stays either a pure literal copy or a
    pure entity decode (never a mix) -- the invariant `OffsetMap.resolve`
    depends on. Returns (decoded_text, new_text_pos)."""
    out = []
    cursor = lo
    for m in _ENTITY_RE.finditer(source, lo, hi):
        if m.start() > cursor:
            chunk = source[cursor:m.start()]
            spans.append(TextSpan(text_pos, text_pos + len(chunk), cursor, m.start(), literal=True))
            out.append(chunk)
            text_pos += len(chunk)
        decoded = unescape(m.group(0))
        spans.append(TextSpan(text_pos, text_pos + len(decoded), m.start(), m.end(), literal=False))
        out.append(decoded)
        text_pos += len(decoded)
        cursor = m.end()
    if cursor < hi:
        chunk = source[cursor:hi]
        spans.append(TextSpan(text_pos, text_pos + len(chunk), cursor, hi, literal=True))
        out.append(chunk)
        text_pos += len(chunk)
    return "".join(out), text_pos


def _collapse_ws(text, spans):
    """Collapse runs of horizontal whitespace to a single space and runs of
    3+ newlines to 2, rebuilding `spans` to match the shrunk text. Pure
    whitespace collapse is never literal (length changes) but each collapsed
    run still resolves to its EXACT source range."""
    out_parts = []
    out_spans = []
    out_pos = 0

    def emit(piece, s_start, s_end, literal):
        nonlocal out_pos
        if not piece:
            return
        out_parts.append(piece)
        out_spans.append(TextSpan(out_pos, out_pos + len(piece), s_start, s_end, literal))
        out_pos += len(piece)

    i = 0
    n = len(text)
    # map text index -> source point via the pre-collapse spans, using the
    # same span-lookup logic as OffsetMap so this stays correct even though
    # `spans` here are pre-collapse (input) spans, not yet an OffsetMap.
    pre_map = OffsetMap(spans)

    while i < n:
        ch = text[i]
        if ch in " \t\xa0":
            j = i
            while j < n and text[j] in " \t\xa0":
                j += 1
            s0, s1 = pre_map.resolve(i, j)
            emit(" ", s0, s1, False)
            i = j
        elif ch == "\n":
            j = i
            # a run of newlines, plus any horizontal whitespace interleaved
            # or trailing (pure indentation) -- all of it collapses to at
            # most two newlines with no orphan space left behind.
            while j < n and text[j] in "\n \t\xa0":
                j += 1
            newlines = min(text.count("\n", i, j), 2)
            s0, s1 = pre_map.resolve(i, j)
            emit("\n" * newlines, s0, s1, False)
            i = j
        else:
            j = i
            start_span = pre_map._span_for(i)
            # stop at whitespace OR at the boundary of the underlying
            # (pre-collapse) span -- otherwise two words separated by a
            # stripped tag but no whitespace (e.g. "<b>World</b>.") would
            # merge into one imprecise chunk and a caller extracting just
            # "World" would resolve to a source range that also swallows the
            # intervening "</b>" markup.
            while j < n and text[j] not in " \t\n\xa0" and pre_map._span_for(j) is start_span:
                j += 1
            s0, s1 = pre_map.resolve(i, j)
            emit(text[i:j], s0, s1, s1 - s0 == j - i)
            i = j

    return "".join(out_parts), out_spans


@dataclass
class NormalizedDocument:
    """The output of `normalize_html`: plain text, the offset map back to the
    original document bytes, and the flattened tables pulled out along the
    way (stage 1)."""

    text: str
    offset_map: OffsetMap
    tables: list = field(default_factory=list)   # list of `FlatTable`
    source: str = ""


def normalize_html(html):
    """HTML (or full-submission text containing HTML) -> `NormalizedDocument`.

    Walks the source once: skips `<script>/<style>/<head>`, turns block tags
    into paragraph breaks, decodes entities per-run so span offsets stay
    exact, and pulls every top-level `<table>` out of the linear flow,
    replacing it with its stage-1 flattened `label: value` pairs (see
    `parse_tables`) so a downstream reader gets the ~200-token version
    instead of ~2000 tokens of markup, with each pair's own span into the
    original table cell.
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")

    tables = parse_tables(html)
    table_by_start = {t.source_start: t for t in tables}
    # tables[] entries carry (source_start, source_end) of the WHOLE
    # <table>..</table> region they replace in the linear walk.
    skip_ranges = sorted((t.source_start, t.source_end) for t in tables)

    raw_spans = []
    text_pos = 0
    out_parts = []
    cursor = 0
    skip_depth = 0  # nesting depth inside a _SKIP_TAGS element

    def append_break(paragraph):
        nonlocal text_pos
        piece = "\n\n" if paragraph else " "
        # synthetic content: source range collapses to the current cursor
        # point (zero-width), which is what "not literal" + zero-length
        # source range means for `resolve`.
        raw_spans.append(TextSpan(text_pos, text_pos + len(piece), cursor, cursor, literal=False))
        out_parts.append(piece)
        text_pos += len(piece)

    def append_table(tbl):
        nonlocal text_pos
        flat = tbl.render()
        raw_spans.append(TextSpan(text_pos, text_pos + len(flat), tbl.source_start, tbl.source_end, literal=False))
        out_parts.append(flat)
        text_pos += len(flat)
        append_break(True)

    i = 0
    n = len(html)
    skip_iter = iter(skip_ranges)
    next_skip = next(skip_iter, None)

    while i < n:
        if next_skip is not None and i == next_skip[0]:
            if cursor < i:
                chunk, text_pos = _decode_literal_run(html, cursor, i, text_pos, raw_spans)
                out_parts.append(chunk)
            append_table(table_by_start[next_skip[0]])
            i = next_skip[1]
            cursor = i
            next_skip = next(skip_iter, None)
            continue

        m = _TAG_RE.match(html, i)
        if m is None:
            i += 1
            continue

        tagtext = m.group(0)
        if cursor < m.start() and skip_depth == 0:
            chunk, text_pos = _decode_literal_run(html, cursor, m.start(), text_pos, raw_spans)
            out_parts.append(chunk)

        low = tagtext.lower()
        if low.startswith("<!--") or low.startswith("<![cdata["):
            pass
        else:
            tag_m = re.match(r"</?\s*([a-zA-Z0-9:]+)", tagtext)
            name = tag_m.group(1).lower() if tag_m else ""
            closing = tagtext.startswith("</")
            self_closing = tagtext.rstrip().endswith("/>") or name == "br"

            if name in _SKIP_TAGS:
                if closing:
                    skip_depth = max(0, skip_depth - 1)
                elif not self_closing:
                    skip_depth += 1
            elif skip_depth == 0:
                if name in _BLOCK_BREAK_TAGS and (closing or self_closing):
                    append_break(True)
                elif name in _SOFT_BREAK_TAGS:
                    append_break(False)

        i = m.end()
        cursor = i

    if cursor < n and skip_depth == 0:
        chunk, text_pos = _decode_literal_run(html, cursor, n, text_pos, raw_spans)
        out_parts.append(chunk)
    elif cursor < n:
        # trailing content still "inside" an unclosed skip tag: drop it, but
        # keep offsets consistent by not advancing text_pos.
        pass

    raw_text = "".join(out_parts)
    collapsed_text, collapsed_spans = _collapse_ws(raw_text, raw_spans)
    text, spans = _trim_edges(collapsed_text, collapsed_spans, "\n ")
    _reanchor_table_pairs(text, tables)

    return NormalizedDocument(text=text, offset_map=OffsetMap(spans), tables=tables, source=html)


def _reanchor_table_pairs(text, tables):
    """Locate each table's rendered block inside the final (collapsed,
    trimmed) document text and set every pair's `doc_text_start/end`.

    Collapsing whitespace can shift a table's absolute position (surrounding
    text may shrink), but never touches the CONTENTS of a rendered table
    block -- `FlatTable.render()` already emits single spaces/newlines, which
    collapse to themselves. So the block reappears in `text` byte-for-byte;
    finding it (in source order, advancing the search cursor past each match
    so a repeated block can't be mismatched to an earlier occurrence) is
    simpler and more robust than threading position math through the
    collapse pass.
    """
    cursor = 0
    for tbl in tables:
        flat = tbl.render()
        idx = text.find(flat, cursor) if flat else -1
        for pair in tbl.pairs:
            if idx == -1:
                pair.doc_text_start = pair.doc_text_end = None
            else:
                pair.doc_text_start = idx + pair.local_text_start
                pair.doc_text_end = idx + pair.local_text_end
        if idx != -1:
            cursor = idx + len(flat)


def _trim_edges(text, spans, strip_chars):
    """`text.strip(strip_chars)` while shifting `spans` to match -- drop
    spans fully inside the trimmed edges, truncate a span straddling one."""
    lo = 0
    while lo < len(text) and text[lo] in strip_chars:
        lo += 1
    hi = len(text)
    while hi > lo and text[hi - 1] in strip_chars:
        hi -= 1

    kept = []
    for s in spans:
        a, b = max(s.text_start, lo), min(s.text_end, hi)
        if a >= b:
            continue
        kept.append(TextSpan(a - lo, b - lo, s.source_start, s.source_end, s.literal))
    return text[lo:hi], kept


# --------------------------------------------------------------------------- #
# Stage 1 -- table pre-parse: HTML <table> -> flat label/value pairs
# --------------------------------------------------------------------------- #

@dataclass
class TablePair:
    """One flattened `label: value` line pulled out of a table row.

    `local_text_start/end` are offsets into the table's OWN rendered text
    (starting at 0); `normalize_html` re-anchors them to `doc_text_start/end`
    once it knows where the table's flattened text lands in the assembled
    document. `source_span` is the exact `(start, end)` in the original HTML
    the value cell came from.
    """

    label: str
    value: str
    source_span: tuple
    row: int
    col: int
    local_text_start: int = 0
    local_text_end: int = 0
    doc_text_start: int = None
    doc_text_end: int = None


@dataclass
class FlatTable:
    source_start: int
    source_end: int
    pairs: list = field(default_factory=list)
    rows: int = 0
    cols: int = 0

    def render(self):
        lines = []
        seen_rows = set()
        for p in self.pairs:
            if p.row in seen_rows and not p.label:
                continue
            line = f"{p.label}: {p.value}" if p.label else p.value
            lines.append(line)
            seen_rows.add(p.row)
        return "\n".join(lines)


class _CellCollector(HTMLParser):
    """Minimal table-structure walker: yields raw `(tag, is_close, attrs,
    start_offset, end_offset)` events with exact source offsets, tracking
    nested `<table>` so a wrapper/nested table can be recursed into rather
    than flattened as if it were plain cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.events = []

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, dict(attrs), self.getpos(), self._offset()))

    def handle_startendtag(self, tag, attrs):
        self.events.append(("startend", tag, dict(attrs), self.getpos(), self._offset()))

    def handle_endtag(self, tag):
        self.events.append(("end", tag, {}, self.getpos(), self._offset()))

    def handle_data(self, data):
        self.events.append(("data", data, {}, self.getpos(), self._offset()))

    def handle_entityref(self, name):
        self.events.append(("data", unescape(f"&{name};"), {}, self.getpos(), self._offset()))

    def handle_charref(self, name):
        self.events.append(("data", unescape(f"&#{name};"), {}, self.getpos(), self._offset()))

    def _offset(self):
        return self.getpos()


def _int_attr(attrs, key, default=1):
    try:
        v = int(str(attrs.get(key, default)).strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _line_offsets(text):
    """Cumulative character offset of the start of each 1-indexed line, so
    `html.parser`'s `(line, col)` positions can be converted to a flat
    character offset."""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _pos_to_offset(line_offsets, pos):
    line, col = pos
    return line_offsets[line - 1] + col


def parse_tables(html):
    """Find every top-level `<table>` in `html` and grid-parse it into a
    `FlatTable`. Handles colspan/rowspan by expanding into a full grid (a
    merged cell's value is placed under every row/col it spans, so
    "row/column relationships survive" per #101's acceptance criterion), and
    recurses into a nested `<table>` found inside a cell -- its pairs are
    flattened with the parent cell's own label as a prefix, since a nested
    table is routinely just a layout wrapper or a sub-schedule under one
    labeled row.
    """
    line_offsets = _line_offsets(html)
    out = []
    for region in _find_top_level_tables(html):
        out.append(_parse_one_table(html, region.start, region.end, line_offsets))
    return out


def _parse_one_table(html, start, end, line_offsets):
    fragment = html[start:end]
    parser = _CellCollector()
    parser.feed(fragment)
    events = parser.events

    def off(pos):
        line, col = pos
        return start + _pos_to_offset(line_offsets_frag, pos)

    line_offsets_frag = _line_offsets(fragment)

    # Walk events, tracking table/row/cell nesting. `table_depth` lets us
    # recognize the CLOSE of nested tables (skip re-descending into their
    # events at this level -- they get their own recursive parse) while
    # still capturing their pairs under the enclosing cell's row.
    grid = {}          # (row, col) -> (text, source_span)
    max_row = -1
    max_col = -1
    row_idx = -1
    col_cursor = 0
    occupied = {}       # (row, col) -> remaining rowspan carry from earlier rows
    table_depth = 0
    cell_stack = []      # stack of dicts: {text, start_off, colspan, rowspan, nested}

    def next_free_col(row):
        c = 0
        while occupied.get((row, c), 0) > 0:
            c += 1
        return c

    i = 0
    while i < len(events):
        kind, tag, attrs, pos, _ = events[i]

        if kind in ("start", "startend") and tag == "table":
            table_depth += 1
            if table_depth > 1 and cell_stack:
                cell_stack[-1]["nested_start"] = off(pos)
            i += 1
            continue
        if kind == "end" and tag == "table":
            if table_depth > 1 and cell_stack and "nested_start" in cell_stack[-1] \
                    and "nested_end" not in cell_stack[-1]:
                cell_stack[-1]["nested_end"] = off(pos) + len("</table>")
            table_depth -= 1
            i += 1
            continue

        if table_depth > 1:
            # inside a nested table: don't accumulate its raw text into the
            # parent cell; it will be re-parsed recursively below.
            i += 1
            continue

        if kind in ("start", "startend") and tag == "tr":
            row_idx += 1
            col_cursor = next_free_col(row_idx)
        elif kind in ("start",) and tag in ("td", "th"):
            col_cursor = next_free_col(row_idx if row_idx >= 0 else 0)
            cell_stack.append({
                "text": [], "start_off": off(pos), "end_off": off(pos),
                "colspan": _int_attr(attrs, "colspan", 1),
                "rowspan": _int_attr(attrs, "rowspan", 1),
                "row": row_idx if row_idx >= 0 else 0,
                "col": col_cursor,
            })
        elif kind == "end" and tag in ("td", "th") and cell_stack:
            cell = cell_stack.pop()
            cell["end_off"] = off(pos) + len(f"</{tag}>")
            span = (cell["start_off"], cell["end_off"])
            if "nested_start" in cell and "nested_end" in cell:
                # A nested <table> was found inside this cell -- recurse and
                # flatten its own pairs into this cell's text instead of
                # treating the wrapper markup as plain cell text. This is
                # what lets a single-cell layout wrapper OR a genuine
                # sub-schedule table still surface its values.
                inner = _parse_one_table(html, cell["nested_start"], cell["nested_end"], line_offsets)
                own_text = _clean_cell_text(cell["text"])
                nested_text = "; ".join(
                    f"{p.label}: {p.value}" if p.label else p.value for p in inner.pairs
                )
                text = "; ".join(t for t in (own_text, nested_text) if t)
            else:
                text = _clean_cell_text(cell["text"])
            r0, c0 = cell["row"], cell["col"]
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    grid[(r0 + dr, c0 + dc)] = (text, span)
                    occupied[(r0 + dr, c0 + dc)] = max(occupied.get((r0 + dr, c0 + dc), 0), 1)
            for dc in range(cell["colspan"]):
                occupied[(r0, c0 + dc)] = 1
            for dr in range(1, cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    occupied[(r0 + dr, c0 + dc)] = 1
            max_row = max(max_row, r0 + cell["rowspan"] - 1)
            max_col = max(max_col, c0 + cell["colspan"] - 1)
            col_cursor = c0 + cell["colspan"]
        elif kind == "data" and cell_stack:
            cell_stack[-1]["text"].append(tag)  # `tag` holds the data string here
        elif kind in ("start", "startend") and tag == "br" and cell_stack:
            cell_stack[-1]["text"].append(" ")

        i += 1

    pairs = []
    text_pos = 0
    for r in range(max_row + 1):
        cols_in_row = sorted({c for (rr, c) in grid if rr == r})
        if not cols_in_row:
            continue
        cells = [grid[(r, c)] for c in cols_in_row]
        # de-dup horizontally-merged repeats (same span repeated by the grid
        # expansion above) while preserving order.
        dedup = []
        seen_spans = set()
        for text, span in cells:
            if span in seen_spans:
                continue
            seen_spans.add(span)
            dedup.append((text, span))
        if not dedup:
            continue
        label, label_span = dedup[0]
        if len(dedup) > 1:
            value = " ".join(t for t, _ in dedup[1:] if t)
            value_span = _union_span([s for _, s in dedup[1:]])
        else:
            value, value_span = label, label_span
            label = ""
        if not label and not value:
            continue
        line = f"{label}: {value}" if label else value
        p = TablePair(
            label=label, value=value, source_span=value_span or label_span,
            row=r, col=0,
            local_text_start=text_pos, local_text_end=text_pos + len(line),
        )
        pairs.append(p)
        text_pos += len(line) + 1  # +1 for the joining "\n" in render()

    return FlatTable(source_start=start, source_end=end, pairs=pairs,
                      rows=max_row + 1, cols=max_col + 1)


def _clean_cell_text(parts):
    text = "".join(parts)
    text = _WS_RE.sub(" ", text.replace("\n", " ")).strip()
    return text


def _union_span(spans):
    spans = [s for s in spans if s]
    if not spans:
        return None
    return (min(s[0] for s in spans), max(s[1] for s in spans))


if __name__ == "__main__":
    sample = """
    <html><body>
    <p>Contingent Coupon Rate: <b>9.15%</b> per annum, Coupon Barrier 70.00%.</p>
    <table>
      <tr><td>Buffer Amount</td><td>10.00%</td></tr>
      <tr><td>Estimated&nbsp;Value</td><td>$972.30 per $1,000</td></tr>
    </table>
    <p>Risk factors follow &amp; are described below.</p>
    </body></html>
    """
    doc = normalize_html(sample)
    print("TEXT:\n" + doc.text)
    print(f"\ntables found: {len(doc.tables)}")
    for t in doc.tables:
        print(t.render())

    idx = doc.text.find("9.15%")
    span = doc.offset_map.resolve(idx, idx + len("9.15%"))
    recovered = doc.source[span[0]:span[1]]
    print(f"\n'9.15%' resolved to source{span} = {recovered!r}")
    assert "9.15" in recovered

    idx2 = doc.text.find("are described below")
    span2 = doc.offset_map.resolve(idx2, idx2 + len("are described below"))
    recovered2 = doc.source[span2[0]:span2[1]]
    print(f"post-entity text resolved to source{span2} = {recovered2!r}")
    assert "are described below" in recovered2

    print("\nnormalize self-check: PASS")
