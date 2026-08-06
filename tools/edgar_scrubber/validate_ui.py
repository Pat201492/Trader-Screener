"""
Human-in-loop validation UI (issue #105) -- the terminal front end for
`validation.py`.

The screen is two panes (see #105): LEFT, the rendered document scrolled to the
field's section with the candidate span highlighted; RIGHT, the proposed value
with accept / correct / reject. One document at a time, ALL its fields shown in
sequence -- field-at-a-time across documents would mean re-reading the same
filing once per field.

Keyboard-first, because this is repetitive batch work and a mouse-driven UI
makes 20 documents feel like 100:

    a  accept        value and span both right -> positive exemplar + rule seed
    c  correct       wrong value or wrong span -> mark the true span (most valuable label)
    r  reject        field absent -> negative exemplar (absence is a valid answer)
    A  accept-all    accept this and every remaining field on the document
    n  next field    (leave undecided for now)
    p  prev field
    N  next document (only once every field has a verdict)
    s  seed rule     promote a stabilized anchor to a #107 rule seed
    q  quit          saves; the session resumes exactly here

The pure rendering (`render_screen`) lives here and is import-safe with no
network dependency, so it is tested directly. The live wiring (EdgarClient,
the ollama/Claude clients, the extraction ladder) is imported lazily inside
`main`, so `import validate_ui` -- and the `--demo` walkthrough -- work without
`requests` or a running model.

    python tools/edgar_scrubber/validate_ui.py --demo         # offline walkthrough
    python tools/edgar_scrubber/validate_ui.py --user-agent "Name you@x.com" \\
        --query queries/424b2-jpm-2025-pilot.json --n 20      # live
"""

import argparse
import textwrap

try:  # package import: tools.edgar_scrubber.validate_ui
    from .validation import (
        ACCEPT, CORRECT, REJECT, FieldProposal, FieldVerdict, RenderDocument,
        ValidationSession, ValidationStore, render_highlight, locate_span,
    )
except ImportError:  # standalone
    from validation import (
        ACCEPT, CORRECT, REJECT, FieldProposal, FieldVerdict, RenderDocument,
        ValidationSession, ValidationStore, render_highlight, locate_span,
    )

# ANSI: reverse video for the highlighted span, dim for chrome. Suppressed
# entirely when `color=False` (tests, or a dumb terminal).
_REVERSE = "\x1b[7m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


# --------------------------------------------------------------------------- #
# Pure rendering -- the two-pane screen as a string
# --------------------------------------------------------------------------- #

def progress_bar(done, total, width=20):
    total = max(total, 1)
    filled = min(width, round(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {done}/{total}"


def _wrap_highlight(highlight, width, *, color):
    """Wrap the left-pane window to `width` columns, keeping the highlighted
    span carved out so it can be reverse-video'd wherever it lands across the
    wrap. Returns `(lines, span_line_index)` -- the index lets the caller
    scroll the visible window to the span rather than to the top."""
    hi_on = _REVERSE if color else "["
    hi_off = _RESET if color else "]"

    if not highlight.located:
        body = highlight.before
        lines = []
        for para in body.split("\n"):
            lines.extend(textwrap.wrap(para, width) or [""])
        return (["(no span located -- prime candidate to correct or reject)", ""]
                + lines), 0

    # Carry the span through wrapping with sentinel markers, then translate each
    # marker to a highlight escape in a stateful pass. Tracking offsets by
    # summing wrapped-line lengths would drift, because splitting on "\n" drops
    # the newline characters -- so the markers travel WITH the text instead.
    # `inside` carries the highlight across a wrap boundary when the span is
    # longer than one line.
    START, END = "\x00", "\x01"
    marked = highlight.before + START + highlight.span_text + END + highlight.after

    out, span_line, inside = [], 0, False
    for para in marked.split("\n"):
        wrapped = textwrap.wrap(para, width, drop_whitespace=False,
                                replace_whitespace=False) or [""]
        for line in wrapped:
            starts_inside = inside
            if START in line and not span_line:
                span_line = len(out)
            if START in line:
                inside = True
            if END in line:
                inside = False
            disp = line.replace(START, hi_on).replace(END, hi_off)
            prefix = hi_on if starts_inside else ""
            suffix = hi_off if inside else ""   # still open at line end -> close it
            out.append(prefix + disp + suffix)
    return out, span_line


def render_screen(session, proposal, render_doc, *, field_index, total_fields,
                  verdict=None, section=None, width=96, doc_lines=12, color=True):
    """The full two-pane screen for one field of one document, as a string.

    Left column: the document windowed around the candidate span, highlighted.
    Right column: the field, its proposed value, provenance/confidence, flags,
    and the verdict keys. Pure and deterministic -- `validate_ui`'s interactive
    loop prints exactly this.
    """
    left_w = width * 3 // 5
    right_w = width - left_w - 3

    prog = session.progress()
    bold = (_BOLD if color else "")
    reset = (_RESET if color else "")
    header = (f"{bold}EDGAR validation{reset}  session={session.session_id}  "
              f"doc {progress_bar(prog['validated'], prog['target'], 12)}  "
              f"field {field_index + 1}/{total_fields}")
    rule = "-" * width

    hl = render_highlight(render_doc, proposal.source_span, window=max(200, left_w * doc_lines // 2))
    wrapped, span_line = _wrap_highlight(hl, left_w, color=color)
    if hl.located:
        start = max(0, span_line - doc_lines // 2)
        left_lines = wrapped[start:start + doc_lines]
    else:
        left_lines = wrapped[:doc_lines]
    left_title = f"DOCUMENT" + (f"  (section: {section})" if section else "")

    right_lines = _field_pane(proposal, verdict, right_w, color=color)

    left_col = [left_title, "-" * left_w] + left_lines
    body = []
    n = max(len(left_col), len(right_lines))
    for i in range(n):
        l = left_col[i] if i < len(left_col) else ""
        r = right_lines[i] if i < len(right_lines) else ""
        body.append(f"{_pad(l, left_w)} | {r}")

    footer = _verdict_strip(session, proposal, verdict, color=color)
    return "\n".join([header, rule, *body, rule, footer])


def _field_pane(proposal, verdict, width, *, color):
    bold = (_BOLD if color else "")
    dim = (_DIM if color else "")
    reset = (_RESET if color else "")
    unit = f" ({proposal.unit})" if proposal.unit else ""
    lines = [f"{bold}FIELD{reset}  {proposal.field}{unit}"]

    shown_value = proposal.value
    lines.append(f"proposed: {shown_value!r}")
    prov = proposal.provenance or proposal.rung or "?"
    conf = f"  conf {proposal.confidence:.2f}" if proposal.confidence is not None else ""
    lines.append(f"{dim}from: {prov}{conf}{reset}")
    if proposal.source_span:
        lines.append(f"{dim}span: source[{proposal.source_span[0]}..{proposal.source_span[1]}]{reset}")
    else:
        lines.append(f"{dim}span: (none located){reset}")

    if proposal.flags:
        for fl in proposal.flags[:3]:
            lines.append(f"! {fl.get('severity', '?')}: {fl.get('message', fl.get('code', ''))}")
    else:
        lines.append("flags: (none)")

    lines.append("")
    if verdict is not None:
        mark = {ACCEPT: "accepted", CORRECT: "CORRECTED", REJECT: "REJECTED"}[verdict.verdict]
        lines.append(f"{bold}verdict: {mark}{reset}")
        if verdict.verdict != REJECT and verdict.value != proposal.value:
            lines.append(f"  -> {verdict.value!r}")
    else:
        lines.append("[a]ccept   [c]orrect   [r]eject")
        lines.append("[A]ccept-all  [n]ext  [p]rev  [N]ext doc")

    wrapped = []
    for line in lines:
        wrapped.extend(_wrap_keep_ansi(line, width))
    return wrapped


def _verdict_strip(session, proposal, verdict, *, color):
    # ASCII only -- a bullet separator mojibakes on a cp1252 Windows console.
    return "[a]ccept  [c]orrect  [r]eject  [A]ll  [n]ext  [N]ext-doc  [q]uit  [?]help"


def _pad(s, width):
    return s + " " * max(0, width - _visible_len(s))


def _visible_len(s):
    """Length ignoring ANSI escape sequences, so column padding stays aligned."""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\x1b":
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out += 1
        i += 1
    return out


def _wrap_keep_ansi(line, width):
    if _visible_len(line) <= width:
        return [line]
    # simple wrap that ignores ANSI in the width count; good enough for the
    # short right-pane strings.
    return textwrap.wrap(line, width) or [""]


# --------------------------------------------------------------------------- #
# Interactive loop
# --------------------------------------------------------------------------- #

# Returned by `validate_document` when the validator presses `q`: quit the whole
# session rather than finish the current document. `_run_live` must break on it
# -- otherwise its `while not complete` loop re-selects the same uncompleted
# accession and re-runs the full extraction ladder on every field, forever.
QUIT = object()


def _section_for(sections, render_doc, source_span):
    """Which split-section the candidate span falls in -- the "scrolled to the
    relevant section" label. Best-effort; None if it can't be placed."""
    if not sections or source_span is None:
        return None
    ts = render_doc.text_span_for_source(source_span)
    if ts is None:
        return None
    t0 = ts[0]
    for name, spans in sections.items():
        for s in spans:
            if s.text_start <= t0 < s.text_end:
                return name
    return None


def validate_document(session, proposals, render_doc, *, accession, document,
                      issuer, sections=None, color=True, read=input,
                      write=print):
    """Drive the verdict loop over one document's proposals. `read`/`write` are
    injectable so a test can script the keyboard; the default is the real
    terminal. Returns the dict of {field: FieldVerdict} recorded."""
    verdicts = {}
    i = 0
    accept_all = False

    while i < len(proposals):
        p = proposals[i]
        existing = verdicts.get(p.field)

        if accept_all and existing is None:
            fv = FieldVerdict.accept(p)
            session.record_verdict(accession, document, fv, issuer=issuer,
                                   render_doc=render_doc)
            verdicts[p.field] = fv
            i += 1
            continue

        section = _section_for(sections, render_doc, p.source_span)
        write(render_screen(session, p, render_doc, field_index=i,
                            total_fields=len(proposals), verdict=existing,
                            section=section, color=color))
        cmd = (read("> ") or "").strip()

        if cmd == "a":
            fv = FieldVerdict.accept(p)
            session.record_verdict(accession, document, fv, issuer=issuer, render_doc=render_doc)
            verdicts[p.field] = fv
            i += 1
        elif cmd == "A":
            accept_all = True
            fv = FieldVerdict.accept(p)
            session.record_verdict(accession, document, fv, issuer=issuer, render_doc=render_doc)
            verdicts[p.field] = fv
            i += 1
        elif cmd == "c":
            fv = _prompt_correction(p, render_doc, read, write)
            session.record_verdict(accession, document, fv, issuer=issuer, render_doc=render_doc)
            verdicts[p.field] = fv
            i += 1
        elif cmd == "r":
            fv = FieldVerdict.reject(p.field)
            session.record_verdict(accession, document, fv, issuer=issuer, render_doc=render_doc)
            verdicts[p.field] = fv
            i += 1
        elif cmd == "n":
            i += 1
        elif cmd == "p":
            i = max(0, i - 1)
        elif cmd in ("N", ""):
            ok, missing = session.all_fields_verdicted(accession, document)
            if ok:
                break
            write(f"  {len(missing)} field(s) still undecided: {', '.join(missing[:6])}"
                  + (" ..." if len(missing) > 6 else ""))
            # jump to the first undecided field
            for j, pp in enumerate(proposals):
                if pp.field in missing:
                    i = j
                    break
        elif cmd == "s":
            if _offer_rule_seeds(session, issuer, read, write) == 0:
                write("  (no rule seeds ready yet -- an anchor stabilizes only "
                      "after it repeats across several validated docs)")
        elif cmd == "q":
            return QUIT
        elif cmd in ("?", "h"):
            write(_HELP)
        else:
            write("  (unrecognized -- a/c/r/A/n/p/N/s/q, ? for help)")

    ok, _ = session.all_fields_verdicted(accession, document)
    if ok:
        session.complete_document(accession, document, issuer=issuer)
        _offer_rule_seeds(session, issuer, read, write)
    return verdicts


def _prompt_correction(proposal, render_doc, read, write):
    """CORRECT: the validator names the true value and, optionally, the true
    span by pasting the text it sits in. The snippet is resolved to exact source
    offsets through the reduction map (`locate_span`)."""
    write(f"  correcting {proposal.field} (proposed {proposal.value!r})")
    raw = (read("  true value (blank = keep proposed value): ") or "").strip()
    value = _coerce_like(raw, proposal.value) if raw else proposal.value

    snippet = (read("  paste the text the value sits in (blank = keep span): ") or "").strip()
    source_span = proposal.source_span
    if snippet:
        near = None
        if proposal.source_span:
            ts = render_doc.text_span_for_source(proposal.source_span)
            near = ts[0] if ts else None
        mark = locate_span(render_doc, snippet, near_text_offset=near)
        if mark is None:
            write("  (snippet not found in the document -- keeping the proposed span)")
        else:
            source_span = mark.source_span
            write(f"  span -> source[{source_span[0]}..{source_span[1]}] "
                  f"= {render_doc.source_text(source_span)!r}")
    return FieldVerdict.correct(proposal.field, value, source_span=source_span)


def _coerce_like(raw, template):
    """Parse a typed correction into the type the proposal used, so a corrected
    number stays a number in the store and the exemplar."""
    if isinstance(template, bool):
        return raw.strip().lower() in ("true", "yes", "y", "1")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(raw)
        except ValueError:
            pass
    if isinstance(template, float):
        try:
            return float(raw)
        except ValueError:
            pass
    if template is None:
        # infer: numeric-looking corrections become numbers
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return raw
    return raw


def _offer_rule_seeds(session, issuer, read, write):
    """Surface any (issuer, field) whose anchor has just stabilized -- "promote
    this to a rule?" (#107). Called on document completion and on demand via the
    `s` key. Appears once per field: the answer is recorded either way. Returns
    the number of seeds offered so the `s` key can report when none are ready."""
    seeds = session.pending_rule_seeds(issuer)
    for seed in seeds:
        write(f"\n  rule seed ready: {seed.field} anchors on {seed.anchor!r} "
              f"in {seed.support}/{seed.total} validated docs.")
        ans = (read(f"  promote {seed.field} to a #107 rule seed? [y/N] ") or "").strip().lower()
        session.mark_seed(issuer, seed.field, seed.anchor, promoted=ans in ("y", "yes"))
        write("  -> seeded." if ans in ("y", "yes") else "  -> dismissed.")
    return len(seeds)


_HELP = """
  a  accept       value + span both correct
  c  correct      fix the value and/or mark the true span
  r  reject       field is absent in this filing
  A  accept-all   accept this and every remaining field
  n / p           next / previous field
  N (or Enter)    next document (needs every field decided)
  s               seed rule -- promote a stabilized anchor to a #107 rule seed
  q               quit -- session resumes exactly here
"""


# --------------------------------------------------------------------------- #
# Demo -- offline walkthrough, no network, no model
# --------------------------------------------------------------------------- #

_DEMO_HTML = """
<html><body>
<p>Issuer: JPMorgan Chase Financial Company LLC. CUSIP: 48133YHT4.</p>
<p>Pricing Date: January 15, 2026. Maturity Date: January 18, 2029.</p>
<p>These securities are not deposits and are not FDIC insured.</p>
<h2>Contingent Coupon</h2>
<p>Contingent Coupon Rate: 9.15% per annum, payable quarterly.</p>
<h2>Downside Protection</h2>
<p>Barrier: 70.00% of the Initial Value. Principal is at risk.</p>
<h2>Estimated Value of the Notes</h2>
<p>Our estimated value of the notes is $972.30 per $1,000 stated principal amount.</p>
</body></html>
"""


def _demo_proposals(render_doc):
    """Hand-built proposals so the demo shows the screen and the full verdict
    flow with no model in the loop. One is deliberately wrong (the barrier),
    to exercise the correct path and span highlighting."""
    def span(snippet):
        m = locate_span(render_doc, snippet)
        return m.source_span if m else None

    return [
        FieldProposal("issuer", "JPMorgan Chase Financial Company LLC",
                      source_span=span("JPMorgan Chase Financial Company LLC"),
                      provenance="model:qwen2.5-7b", confidence=0.94, rung="local"),
        FieldProposal("cusip", "48133YHT4", source_span=span("48133YHT4"),
                      provenance="model:qwen2.5-7b", confidence=0.88, rung="local"),
        FieldProposal("contingent_coupon_rate", 9.15, source_span=span("9.15%"),
                      provenance="model:qwen2.5-7b", confidence=0.9, rung="local",
                      unit="percent_per_annum"),
        # WRONG on purpose: grabbed 972.30 from the estimated-value line instead
        # of the 70.00% barrier -- exactly the silent wrong answer span
        # highlighting is meant to catch.
        FieldProposal("barrier_pct", 97.23, source_span=span("972.30"),
                      provenance="model:qwen2.5-7b", confidence=0.41, rung="local",
                      unit="percent_of_initial",
                      flags=[{"severity": "warn", "code": "out_of_bounds",
                              "message": "barrier_pct=97.23 above expected range"}]),
        FieldProposal("estimated_value_per_1000", 972.30, source_span=span("972.30"),
                      provenance="model:qwen2.5-7b", confidence=0.96, rung="local",
                      unit="usd_per_1000"),
    ]


def demo(color=True):
    try:
        from normalize import normalize_html
        from field_spec import load_specs
    except ImportError:
        from .normalize import normalize_html
        from .field_spec import load_specs

    spec = load_specs()["structured_note"]
    nd = normalize_html(_DEMO_HTML)
    render_doc = RenderDocument.from_normalized(nd)
    try:
        from reduce import split_sections
    except ImportError:
        from .reduce import split_sections
    sections = split_sections(nd)

    store = ValidationStore(":memory:")
    session = ValidationSession(store, spec, session_id="demo", target_n=1,
                               rule_seed_threshold=1)
    proposals = _demo_proposals(render_doc)

    print("Offline demo -- 5 fields of one 424B2. Try: a, c, r, A, N.\n"
          "The barrier_pct proposal is wrong on purpose (grabbed 972.30, the "
          "estimated value, not the 70.00% barrier) -- 'c' then paste 70.00% to "
          "correct the span.\n")
    validate_document(session, proposals, render_doc, accession="0000-DEMO",
                      document="424b2.htm", issuer="JPMorgan Chase Financial Company LLC",
                      sections=sections, color=color)
    print("\nverdicts recorded:")
    for v in store.verdicts_for("demo", "0000-DEMO", "424b2.htm"):
        print(f"  {v.field:<26} {v.verdict:<8} {v.value!r}")
    print("\nexemplars written (these are what document 2 would benefit from):")
    for field in ("barrier_pct", "estimated_value_per_1000"):
        for e in store.exemplars_for("JPMorgan Chase Financial Company LLC", field):
            print(f"  {field}: {e}")


# --------------------------------------------------------------------------- #
# Live wiring
# --------------------------------------------------------------------------- #

def _build_live(args):
    """Wire the real EdgarClient + extraction ladder. Imported lazily so the
    module (and the demo) load without `requests`/a running model."""
    from crawl import load_query, default_state_path, CrawlState, Crawler
    from edgar_client import EdgarClient
    from document_expand import expand_accession
    from field_spec import load_specs, detect_population
    from extraction_ladder import ExtractionLadder
    from ollama_client import OllamaClient, OllamaConfig
    from normalize import normalize_html
    from reduce import split_sections
    from validation import LadderExtractor

    query = load_query(args.query)
    client = EdgarClient(args.user_agent, args.cache_dir)
    state_path = default_state_path(query.id, args.state_dir)

    # Resume or run the crawl enough to have a frontier of accessions.
    crawler = Crawler(client, query, state_path=state_path)
    if not crawler.state.accessions:
        crawler.run()
    crawl_state = crawler.state

    specs = load_specs()
    local_cfg = OllamaConfig.from_env({})
    local_client = OllamaClient(local_cfg)
    claude_client = None
    if not args.local_only:
        claude_client = OllamaClient(OllamaConfig.for_claude())

    store = ValidationStore(args.store)
    return {
        "client": client, "crawl_state": crawl_state, "specs": specs,
        "local_client": local_client, "claude_client": claude_client,
        "store": store, "expand_accession": expand_accession,
        "normalize_html": normalize_html, "split_sections": split_sections,
        "detect_population": detect_population, "ExtractionLadder": ExtractionLadder,
        "LadderExtractor": LadderExtractor,
    }


def _run_live(args):
    ctx = _build_live(args)
    store = ctx["store"]
    specs = ctx["specs"]

    # Population is per-document; pick the spec once the first primary is read.
    default_spec = specs.get("structured_note") or next(iter(specs.values()))
    session = ValidationSession(store, default_spec, session_id=args.session,
                               target_n=args.n, crawl_state=ctx["crawl_state"],
                               clock=_utc_now)

    # Which ACCESSIONS are held out is decided up front, from the full crawl
    # candidate pool, so the stratified pick (#108) is not biased by
    # validation order. The actual `mark_held_out` call happens per-document
    # below, once `expand_accession` resolves the real primary document name
    # -- the crawl metadata's guessed `document` is not always what
    # `document_expand.expand_accession` picks as primary (it inspects every
    # manifest item, not just the full-text-search hit's guess), and marking
    # against the wrong document name would silently fail to exclude it.
    # Either way this happens BEFORE that document's first verdict, which is
    # the actual correctness requirement (see eval_harness.py's docstring).
    held_out_meta = {}
    if args.reserve_held_out:
        from eval_harness import select_held_out
        accessions = ctx["crawl_state"].accessions
        candidates = [{"accession": acc, "issuer": meta.get("issuer")}
                     for acc, meta in accessions.items()]
        selected = select_held_out(candidates, fraction=args.reserve_held_out,
                                   seed=args.held_out_seed)
        held_out_meta = {c["accession"]: c for c in selected}
        print(f"Selected {len(selected)}/{len(candidates)} accessions for the #108 held-out "
             f"eval set (fraction={args.reserve_held_out}, seed={args.held_out_seed}); "
             f"they validate normally but never write an exemplar or seed a rule.")

    while not session.progress()["complete"]:
        nxt = session.next_unvalidated()
        if nxt is None:
            print("No more unvalidated documents in the crawl frontier.")
            break
        accession, meta = nxt
        cik = meta.get("cik")
        print(f"\nExpanding {accession} (cik={cik}) ...")
        bundle = ctx["expand_accession"](ctx["client"], cik, accession)
        primary = bundle.primary()
        if primary is None or primary.normalized is None:
            print("  no readable primary document; marking skipped.")
            session.complete_document(accession, primary.name if primary else "unknown",
                                      issuer=meta.get("issuer"))
            continue

        det = ctx["detect_population"](primary.normalized.text, specs)
        spec = det.spec or default_spec
        session.spec = spec

        if accession in held_out_meta and not session.is_held_out(accession, primary.name):
            picked = held_out_meta[accession]
            session.mark_held_out(accession, primary.name, issuer=meta.get("issuer"),
                                  product_type=det.population, stratum=picked.get("stratum"))
            print(f"  reserved for the #108 held-out eval set (stratum={picked.get('stratum')})")

        render_doc = RenderDocument.from_normalized(primary.normalized)
        sections = ctx["split_sections"](primary.normalized)
        ladder = ctx["ExtractionLadder"](
            spec, exemplars=store,
            local_client=ctx["local_client"], claude_client=ctx["claude_client"])
        extractor = ctx["LadderExtractor"](spec, ladder, sections=sections)
        ex107 = bundle.ex107.as_dict() if bundle.ex107 else None
        proposals = extractor.propose(render_doc, issuer=meta.get("issuer"),
                                      ex107=ex107, accession=accession,
                                      document=primary.name)
        outcome = validate_document(session, proposals, render_doc, accession=accession,
                         document=primary.name, issuer=meta.get("issuer"),
                         sections=sections, color=not args.no_color)
        if outcome is QUIT:
            print("\nQuit -- session saved; resume here with the same --session id.")
            break

    print(f"\nDone. {session.progress()}")


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Human-in-loop 424B2 validation UI (issue #105)")
    ap.add_argument("--demo", action="store_true",
                    help="offline walkthrough: one canned 424B2, no network, no model")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI highlighting")
    ap.add_argument("--query", help="path to a saved crawl query JSON (see queries/*.json)")
    ap.add_argument("--user-agent", help="'<name> <email>' -- required for live use")
    ap.add_argument("--cache-dir", default=".edgar-cache")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--store", default=None, help="validation store path (default: local home)")
    ap.add_argument("--session", default="default", help="session id to resume")
    ap.add_argument("--n", type=int, default=20, help="target number of documents (N)")
    ap.add_argument("--local-only", action="store_true",
                    help="never escalate to Claude (#104 local-only mode)")
    ap.add_argument("--reserve-held-out", type=float, default=None,
                    help="reserve this fraction of the crawl frontier for the #108 held-out "
                         "eval set, stratified by issuer, before any of it is validated "
                         "(e.g. 0.15). Reserved documents still get verdicts -- they just "
                         "never write an exemplar or seed a rule.")
    ap.add_argument("--held-out-seed", type=int, default=0,
                    help="seed for --reserve-held-out's selection (default: 0, deterministic)")
    args = ap.parse_args(argv)

    if args.demo:
        demo(color=not args.no_color)
        return
    if not (args.query and args.user_agent):
        ap.error("live mode needs --query and --user-agent (or pass --demo)")
    _run_live(args)


if __name__ == "__main__":
    main()
