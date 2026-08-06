"""
Stages 2-4 of the #101 four-stage reduction (part of #95; stage 1, the table
pre-parse, lives in `normalize.py` because it has to run in the same pass as
the HTML walk). A 424B2 runs 150k-400k+ characters; the extraction model is a
7B at 8k context (#103), so the document must shrink ~50x before it reaches a
prompt -- and every stage here is deterministic and TOKEN-FREE, the same
design constraint as stage 1.

  2. BOILERPLATE STRIP -- hash normalized paragraphs across one issuer's
     filings; any paragraph present in > ~80% of them is template text with
     no filing-specific content. On a templated shelf program that is
     plausibly 60-80% of the body.

  3. SECTION SPLIT -- heading-keyword classification into addressable
     sections (`Key Terms`, `Estimated Value of the Notes`, ...). This is an
     ACCURACY mechanism as much as a cost one: a barrier percentage pulled
     from `Hypothetical Examples` instead of `Key Terms` is a classic silent
     wrong answer, and section routing makes that structurally impossible --
     a field tagged `sections: ["coupon_terms"]` (see `field_spec.py`) can
     only ever be searched for inside text this stage bucketed as
     `coupon_terms`.

  4. SUB-BLOCK ROUTING (warm path) -- once #107 has induced an anchor for a
     field, send +/-500 characters around it instead of the whole section.
     Section routing is the cold path; sub-block is the warm path.

Every stage's output carries an `OffsetMap` chained (via `OffsetMap.compose`)
back through stage 1's map into the ORIGINAL document bytes, so a span
extracted from a boilerplate-stripped, section-split sub-block still resolves
to an exact source offset -- the #101 acceptance criterion that spans survive
"through all four stages."

stdlib only. Run the self-check:  python tools/edgar_scrubber/reduce.py
"""
import hashlib
import re
from dataclasses import dataclass, field

try:  # package import: tools.edgar_scrubber.reduce
    from .normalize import OffsetMap, TextSpan
except ImportError:  # standalone: python tools/edgar_scrubber/reduce.py
    from normalize import OffsetMap, TextSpan

_PARA_SPLIT_RE = re.compile(r"\n{2,}")
_WS_COLLAPSE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Shared: paragraph segmentation with text offsets
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Paragraph:
    text: str
    text_start: int
    text_end: int


def iter_paragraphs(text):
    """Split `text` on blank-line boundaries (the paragraph break
    `normalize_html` emits) into `Paragraph`s carrying their own text
    offsets. Every stage below is built on this so paragraph-granularity
    spans are consistent everywhere."""
    paras = []
    pos = 0
    for m in _PARA_SPLIT_RE.finditer(text):
        if m.start() > pos:
            chunk = text[pos:m.start()]
            if chunk.strip():
                paras.append(Paragraph(chunk, pos, m.start()))
        pos = m.end()
    if pos < len(text):
        chunk = text[pos:]
        if chunk.strip():
            paras.append(Paragraph(chunk, pos, len(text)))
    return paras


def _normalize_paragraph_key(text):
    """Canonical form used ONLY for hashing/matching (lowercased, whitespace
    collapsed) -- never used as the stored/displayed text, so casing and
    exact spacing in the original are preserved everywhere else."""
    return _WS_COLLAPSE_RE.sub(" ", text.strip().lower())


def _paragraph_hash(text):
    return hashlib.sha1(_normalize_paragraph_key(text).encode("utf-8")).hexdigest()


def _rebuild_offset_map(kept_paragraphs, upstream_map):
    """Build the OffsetMap for text reassembled by joining `kept_paragraphs`
    with `\\n\\n`, composed against `upstream_map` (the map from the INPUT
    text -- e.g. stage 1's normalized text -- back to the original source).
    """
    spans = []
    pos = 0
    for i, p in enumerate(kept_paragraphs):
        spans.append(TextSpan(pos, pos + len(p.text), p.text_start, p.text_end, literal=True))
        pos += len(p.text)
        if i < len(kept_paragraphs) - 1:
            # the synthetic "\n\n" joiner has no single source point; anchor
            # it (zero-width) at the end of the paragraph it follows.
            spans.append(TextSpan(pos, pos + 2, p.text_end, p.text_end, literal=False))
            pos += 2
    local_map = OffsetMap(spans)
    return local_map.compose(upstream_map)


# --------------------------------------------------------------------------- #
# Stage 2 -- boilerplate strip by cross-filing dedup
# --------------------------------------------------------------------------- #

@dataclass
class BoilerplateModel:
    """A per-issuer boilerplate model: which paragraph hashes are template
    text, learned from a corpus of that issuer's own filings. Computed once
    per issuer and refreshed when #107 detects a template change (this
    module does not decide staleness -- it just takes whatever corpus it is
    given)."""

    issuer: str
    doc_count: int
    threshold: float
    frequency: dict = field(default_factory=dict)    # hash -> # docs containing it
    boilerplate_hashes: set = field(default_factory=set)
    sample_text: dict = field(default_factory=dict)   # hash -> one example paragraph

    def is_boilerplate(self, paragraph_text):
        return _paragraph_hash(paragraph_text) in self.boilerplate_hashes


def build_boilerplate_model(issuer, documents, threshold=0.8):
    """Learn a `BoilerplateModel` from `documents` (an issuer's `NormalizedDocument`s).

    A paragraph hash counts AT MOST ONCE per document (a boilerplate line
    repeated three times within one filing must not look like it appeared in
    three filings) -- present in >= `threshold` fraction of the corpus makes
    it boilerplate. A single-document corpus never produces boilerplate:
    nothing is "cross-filing" yet.
    """
    frequency = {}
    sample_text = {}
    doc_count = len(documents)

    for doc in documents:
        seen_in_doc = set()
        for p in iter_paragraphs(doc.text):
            h = _paragraph_hash(p.text)
            if h in seen_in_doc:
                continue
            seen_in_doc.add(h)
            frequency[h] = frequency.get(h, 0) + 1
            sample_text.setdefault(h, p.text)

    boilerplate = set()
    if doc_count > 1:
        for h, count in frequency.items():
            if (count / doc_count) >= threshold:
                boilerplate.add(h)

    return BoilerplateModel(issuer=issuer, doc_count=doc_count, threshold=threshold,
                             frequency=frequency, boilerplate_hashes=boilerplate,
                             sample_text=sample_text)


@dataclass
class BoilerplateStripReport:
    original_chars: int
    kept_chars: int
    original_paragraphs: int
    kept_paragraphs: int
    removed_paragraph_hashes: list

    @property
    def reduction_pct(self):
        if self.original_chars == 0:
            return 0.0
        return 100.0 * (1 - self.kept_chars / self.original_chars)


@dataclass
class ReducedDocument:
    """The output of any stage past stage 1: reduced text plus an
    `offset_map` composed all the way back to the ORIGINAL source bytes."""

    text: str
    offset_map: OffsetMap
    report: object = None


def strip_boilerplate(doc, model):
    """Remove every paragraph of `doc` (a `NormalizedDocument`, or another
    stage's `ReducedDocument`) that `model` flags as boilerplate. Returns a
    `ReducedDocument` whose `offset_map` composes onto `doc.offset_map`, so a
    span found in the stripped text still resolves to the original source --
    and a `BoilerplateStripReport` with the MEASURED reduction, per #101's
    acceptance criterion ("boilerplate dedup reports measured reduction per
    issuer").
    """
    paragraphs = iter_paragraphs(doc.text)
    kept, removed_hashes = [], []
    for p in paragraphs:
        h = _paragraph_hash(p.text)
        if h in model.boilerplate_hashes:
            removed_hashes.append(h)
            continue
        kept.append(p)

    text = "\n\n".join(p.text for p in kept)
    offset_map = _rebuild_offset_map(kept, doc.offset_map)
    report = BoilerplateStripReport(
        original_chars=len(doc.text), kept_chars=len(text),
        original_paragraphs=len(paragraphs), kept_paragraphs=len(kept),
        removed_paragraph_hashes=removed_hashes,
    )
    return ReducedDocument(text=text, offset_map=offset_map, report=report)


# --------------------------------------------------------------------------- #
# Stage 3 -- section split
# --------------------------------------------------------------------------- #

# The canonical section vocabulary this splitter recognizes -- a superset of
# any one field_spec's `sections[]` (structured-note and shelf-takedown
# specs each use a subset). "cover" is never matched by a heading pattern;
# it is everything before the first recognized heading.
#
# Patterns are heading-phrase fragments, matched against a SHORT candidate
# line (see `_heading_candidate`), not the full body -- so a body sentence
# that happens to mention "the estimated value of the notes" mid-paragraph
# does not get misread as a new section boundary. Order matters: earlier
# entries win on a tie, so more specific phrasing is listed first.
SECTION_PATTERNS = (
    ("estimated_value", re.compile(r"estimated\s+value(\s+of\s+the\s+(notes|securities))?", re.I)),
    ("hypothetical_examples", re.compile(r"hypothetical\s+(examples?|payout|return)", re.I)),
    ("risk_considerations", re.compile(r"(selected\s+)?risk\s+(factors|considerations)", re.I)),
    ("coupon_terms", re.compile(r"contingent\s+(coupon|interest)|coupon\s+(terms|payment|rate)|interest\s+payment", re.I)),
    ("call_terms", re.compile(r"automatic(ally)?\s+call|autocall|call\s+(terms|feature|dates)|redemption\s+feature", re.I)),
    ("downside_terms", re.compile(r"principal\s+at\s+risk|downside|payment\s+at\s+maturity|buffer\s+amount|barrier\s+event", re.I)),
    ("dates", re.compile(r"key\s+dates|important\s+dates|schedule\s+of\s+dates", re.I)),
    ("fees", re.compile(r"fees\s+and\s+(expenses|conflicts)|selling\s+concession|commissions?\b", re.I)),
    ("calculation_agent", re.compile(r"calculation\s+agent", re.I)),
    ("plan_of_distribution", re.compile(r"(supplemental\s+)?plan\s+of\s+distribution|underwriting", re.I)),
    ("use_of_proceeds", re.compile(r"use\s+of\s+proceeds", re.I)),
    ("description_of_securities", re.compile(r"description\s+of\s+(the\s+)?(notes|securities|debt)", re.I)),
    ("offering_summary", re.compile(r"offering\s+summary|summary\s+of\s+the\s+offering", re.I)),
    ("key_terms", re.compile(r"key\s+terms|indicative\s+terms|general\s+terms|terms\s+of\s+the\s+notes|summary\s+(of\s+)?terms", re.I)),
    ("summary", re.compile(r"^summary\b", re.I)),
)

_MAX_HEADING_LEN = 100


def _heading_candidate(paragraph_text):
    """The line a heading-check runs against: the paragraph's first line,
    only if it plausibly reads as a heading (short, no sentence-ending
    punctuation trailing prose) rather than the start of a normal sentence.
    """
    first_line = paragraph_text.split("\n", 1)[0].strip(" :-")
    if not first_line or len(first_line) > _MAX_HEADING_LEN:
        return None
    if first_line.endswith((".", ";", ",")):
        return None
    return first_line


def classify_heading(line):
    """Return the canonical section name `line` matches, or None. Checked in
    `SECTION_PATTERNS` order; the first match wins."""
    for name, pattern in SECTION_PATTERNS:
        if pattern.search(line):
            return name
    return None


@dataclass
class SectionSpan:
    section: str
    heading_text: str            # None for the leading "cover" bucket
    text_start: int
    text_end: int
    source_span: tuple


def split_sections(doc):
    """Partition `doc` (a `NormalizedDocument` or `ReducedDocument`) into
    `{section_name: [SectionSpan, ...]}` by heading detection. A section name
    can recur (e.g. `key_terms` summarized up top and detailed again later);
    every occurrence is kept, not just the first, since a field the model
    misses in one occurrence may be findable in the other.

    Content before the first recognized heading is always "cover" -- SEC
    cover-page terms (issuer, CUSIP, pricing date) routinely precede any
    heading at all.
    """
    paragraphs = iter_paragraphs(doc.text)
    boundaries = []   # list of (para_index, section_name, heading_text)
    for i, p in enumerate(paragraphs):
        cand = _heading_candidate(p.text)
        if not cand:
            continue
        section = classify_heading(cand)
        if section:
            boundaries.append((i, section, cand))

    sections = {}

    def _add(name, heading_text, start_para, end_para):
        if start_para > end_para:
            return
        text_start = paragraphs[start_para].text_start
        text_end = paragraphs[end_para].text_end
        source_span = doc.offset_map.resolve(text_start, text_end)
        sections.setdefault(name, []).append(
            SectionSpan(name, heading_text, text_start, text_end, source_span)
        )

    if not paragraphs:
        return sections

    first_heading_para = boundaries[0][0] if boundaries else len(paragraphs)
    if first_heading_para > 0:
        _add("cover", None, 0, first_heading_para - 1)

    for idx, (para_i, name, heading_text) in enumerate(boundaries):
        end_para = boundaries[idx + 1][0] - 1 if idx + 1 < len(boundaries) else len(paragraphs) - 1
        _add(name, heading_text, para_i, end_para)

    return sections


def text_for_sections(doc, sections, names):
    """Concatenate every span (in document order) whose section is in
    `names` -- what a caller feeding a field's `field_spec.sections` list
    passes to the model as the "cold path" context."""
    names = set(names)
    spans = [s for group in sections.values() for s in group if s.section in names]
    spans.sort(key=lambda s: s.text_start)
    return "\n\n".join(doc.text[s.text_start:s.text_end] for s in spans)


def sections_for_spec(sections, spec_sections):
    """Subset of `sections` whose key is in a field_spec's own `sections[]`
    vocabulary -- the bridge between this module's broader heading
    vocabulary and one `FieldSpec`'s narrower routing list."""
    wanted = set(spec_sections)
    return {name: spans for name, spans in sections.items() if name in wanted}


# --------------------------------------------------------------------------- #
# Stage 4 -- sub-block routing (warm path)
# --------------------------------------------------------------------------- #

@dataclass
class SubBlock:
    text: str
    text_span: tuple
    source_span: tuple


def sub_block(doc, *, text_offset=None, source_offset=None, window=500):
    """+/-`window` characters of `doc.text` around an anchor, plus the exact
    source span it resolves to. Pass exactly one of `text_offset` (an offset
    already in `doc.text`) or `source_offset` (an offset into the ORIGINAL
    source bytes -- the shape a #107-induced anchor is stored in, per
    `output_store.FieldValue.span`); `source_offset` is converted via
    `OffsetMap.text_offset_for_source` first.
    """
    if (text_offset is None) == (source_offset is None):
        raise ValueError("sub_block: pass exactly one of text_offset or source_offset")

    if text_offset is None:
        text_offset = doc.offset_map.text_offset_for_source(source_offset)
        if text_offset is None:
            raise ValueError(f"source_offset {source_offset} does not resolve into this document")

    lo = max(0, text_offset - window)
    hi = min(len(doc.text), text_offset + window)
    span_text = doc.text[lo:hi]
    resolved = doc.offset_map.resolve(lo, hi)
    return SubBlock(text=span_text, text_span=(lo, hi), source_span=resolved)


if __name__ == "__main__":
    try:
        from normalize import normalize_html
    except ImportError:
        from .normalize import normalize_html

    # -- stage 2: boilerplate -------------------------------------------------
    boiler = "<p>These securities are not deposits and are not FDIC insured.</p>"
    filing_a = normalize_html(f"<html><body><h2>Key Terms</h2><p>Issuer: JPMorgan.</p>{boiler}</body></html>")
    filing_b = normalize_html(f"<html><body><h2>Key Terms</h2><p>Issuer: GS Finance.</p>{boiler}</body></html>")
    filing_c = normalize_html(f"<html><body><h2>Key Terms</h2><p>Issuer: Citi.</p>{boiler}</body></html>")

    model = build_boilerplate_model("ACME", [filing_a, filing_b, filing_c], threshold=0.8)
    assert any(model.is_boilerplate(p.text) for p in iter_paragraphs(filing_a.text))
    reduced = strip_boilerplate(filing_a, model)
    print(f"stage2: {reduced.report.original_chars} -> {reduced.report.kept_chars} chars "
          f"({reduced.report.reduction_pct:.1f}% reduction)")
    assert "FDIC" not in reduced.text
    assert "JPMorgan" in reduced.text
    idx = reduced.text.find("JPMorgan")
    src = reduced.offset_map.resolve(idx, idx + len("JPMorgan"))
    assert "JPMorgan" in filing_a.source[src[0]:src[1]]
    print(f"stage2 span survives to source: {filing_a.source[src[0]:src[1]]!r}")

    # -- stage 3: section split ------------------------------------------------
    sample = """
    <html><body>
    <p>PRICING SUPPLEMENT dated January 15, 2026</p>
    <h2>Key Terms</h2>
    <p>Issuer: JPMorgan Chase Financial Company LLC. CUSIP: 48133YHT4.</p>
    <h2>Contingent Coupon Payment</h2>
    <p>The notes pay a contingent coupon of 9.15% per annum if observed above the barrier.</p>
    <h2>Hypothetical Examples</h2>
    <p>If the underlying closes at 50% of its initial value, the barrier used in this example is 60%.</p>
    <h2>Estimated Value of the Notes</h2>
    <p>Our estimated value of the notes is $972.30 per $1,000 stated principal amount.</p>
    </body></html>
    """
    doc = normalize_html(sample)
    sections = split_sections(doc)
    print("\nstage3 sections found:", sorted(sections))
    assert "cover" in sections
    assert "key_terms" in sections
    assert "coupon_terms" in sections
    assert "hypothetical_examples" in sections
    assert "estimated_value" in sections

    coupon_text = text_for_sections(doc, sections, ["coupon_terms"])
    assert "9.15%" in coupon_text
    assert "60%" not in coupon_text, "barrier % from Hypothetical Examples leaked into coupon_terms"
    print("stage3: coupon_terms text does not leak the Hypothetical Examples barrier -- PASS")

    ev_span = sections["estimated_value"][0]
    recovered = doc.source[ev_span.source_span[0]:ev_span.source_span[1]]
    assert "972.30" in recovered
    print(f"stage3 section span resolves to source containing: {'972.30' in recovered}")

    # -- stage 4: sub-block ----------------------------------------------------
    anchor_text_pos = doc.text.find("9.15%")
    block = sub_block(doc, text_offset=anchor_text_pos, window=40)
    print(f"\nstage4 sub_block (+/-40 chars): {block.text!r}")
    assert "9.15%" in block.text
    recovered_src = doc.source[block.source_span[0]:block.source_span[1]]
    assert "9.15" in recovered_src

    anchor_source_pos = doc.offset_map.resolve(anchor_text_pos, anchor_text_pos + 5)[0]
    block2 = sub_block(doc, source_offset=anchor_source_pos, window=40)
    assert "9.15%" in block2.text
    print("stage4 source_offset -> text window round-trip: PASS")

    print("\nreduce self-check: PASS")
