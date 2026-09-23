"""
Validation-session run report (issue #165, part of #95).

#105 built the human-in-loop loop, #106 the exemplar store, #108 the eval
harness -- and none of it had been RUN. #165 is the run: validate ~20 filings
across >=4 issuers and find out whether "ten validated minutes removes hours of
GPU time" (VALIDATION_UI.md) actually holds. This module turns the labels a
session leaves behind into the numbers that answer that question, so the read
is a by-product of the sitting rather than a second manual pass.

Three of the run's acceptance criteria are derivable from the recorded verdicts
alone -- no model, no network, no re-extraction -- and that is what lives here:

  * PER-FIELD ACCEPT RATE (`field_accept_rates`) -- of the values the model
    produced that a human ruled on, the share accepted as-is. This is an UPPER
    BOUND on precision, not precision itself: a REJECT verdict records that a
    field was absent but not whether the model hallucinated a value there, so
    hallucinations on absent fields are invisible to a verdict-only read. The
    authoritative per-field precision (with the absence axis) comes from
    `eval_harness.run_eval`, which re-runs the extractor against the held-out
    gold -- see `render_eval_markdown` for that half of the report.

  * COMPOUNDING SPLIT (`compounding_split`) -- accept rate over the first half
    of the validated documents vs the second half. #105 writes an exemplar the
    instant a verdict lands, so if the loop compounds, later documents should
    accept at a higher rate than earlier ones. This is the VALIDATION_UI.md
    claim, measured from the session's own order.

  * PER-DOCUMENT WALL-CLOCK (`per_document_timings`) -- the span from a
    document's first verdict to its last, so "ten validated minutes per
    document" can be confirmed or corrected. NULL when the session ran without
    a clock (`ValidationSession(clock=None)`); `validate_ui.py` always passes
    one, so a real sitting is always timed.

stdlib only. Run the self-check:  python tools/edgar_scrubber/session_report.py --selftest
"""

import argparse
import sys
from dataclasses import dataclass, field as _dc_field
from datetime import datetime

try:  # package import: tools.edgar_scrubber.session_report
    from .validation import ValidationStore, ACCEPT, CORRECT, REJECT
    from .field_spec import load_specs
except ImportError:  # standalone: python tools/edgar_scrubber/session_report.py
    from validation import ValidationStore, ACCEPT, CORRECT, REJECT
    from field_spec import load_specs


# --------------------------------------------------------------------------- #
# Per-field accept rate (verdict-only -- an upper bound on precision)
# --------------------------------------------------------------------------- #

@dataclass
class FieldAccept:
    """One field's verdict tally over a session. `accept_rate` is the share of
    PRODUCED values (accept + correct) the human accepted unchanged -- an upper
    bound on precision, because reject records absence without recording whether
    the model hallucinated a value there."""

    field: str
    accept: int = 0
    correct: int = 0
    reject: int = 0

    @property
    def produced(self):
        return self.accept + self.correct

    @property
    def accept_rate(self):
        return self.accept / self.produced if self.produced else None

    def as_dict(self):
        return {"field": self.field, "accept": self.accept, "correct": self.correct,
                "reject": self.reject, "produced": self.produced,
                "accept_rate": self.accept_rate}


def field_accept_rates(store, session_id, spec=None):
    """Per-field `FieldAccept` over every verdict in the session. When `spec` is
    given, every spec field appears (even one that never got a verdict), so a
    field the session skipped is visible as a gap rather than silently missing."""
    tallies = {}
    if spec is not None:
        for name in spec.field_names():
            tallies[name] = FieldAccept(field=name)
    for row in store.session_verdict_rows(session_id):
        fa = tallies.setdefault(row["field"], FieldAccept(field=row["field"]))
        if row["verdict"] == ACCEPT:
            fa.accept += 1
        elif row["verdict"] == CORRECT:
            fa.correct += 1
        elif row["verdict"] == REJECT:
            fa.reject += 1
    return [tallies[name] for name in sorted(tallies)]


# --------------------------------------------------------------------------- #
# Compounding: accept rate over the first half vs the second half
# --------------------------------------------------------------------------- #

@dataclass
class HalfStats:
    label: str
    documents: int = 0
    accept: int = 0
    produced: int = 0

    @property
    def accept_rate(self):
        return self.accept / self.produced if self.produced else None

    def as_dict(self):
        return {"label": self.label, "documents": self.documents,
                "accept": self.accept, "produced": self.produced,
                "accept_rate": self.accept_rate}


@dataclass
class Compounding:
    first: HalfStats
    second: HalfStats

    @property
    def delta(self):
        """Second-half accept rate minus first-half -- positive means later
        documents accepted more often, the compounding VALIDATION_UI.md claims.
        None when either half produced no ruled-on values."""
        a, b = self.first.accept_rate, self.second.accept_rate
        return b - a if (a is not None and b is not None) else None

    def as_dict(self):
        return {"first": self.first.as_dict(), "second": self.second.as_dict(),
                "delta": self.delta}


def compounding_split(store, session_id):
    """Split the validated documents down the middle in validation order and
    compare accept rate across the halves. With an odd count the extra document
    falls in the second half."""
    docs = store.validated_documents(session_id)
    by_doc = {}
    for row in store.session_verdict_rows(session_id):
        key = (row["accession"], row["document"])
        acc, prod = by_doc.get(key, (0, 0))
        if row["verdict"] == ACCEPT:
            acc, prod = acc + 1, prod + 1
        elif row["verdict"] == CORRECT:
            prod += 1
        by_doc[key] = (acc, prod)

    mid = len(docs) // 2
    first = HalfStats(label="first half")
    second = HalfStats(label="second half")
    for i, d in enumerate(docs):
        half = first if i < mid else second
        half.documents += 1
        acc, prod = by_doc.get((d["accession"], d["document"]), (0, 0))
        half.accept += acc
        half.produced += prod
    return Compounding(first=first, second=second)


# --------------------------------------------------------------------------- #
# Per-document wall-clock, from verdict timestamps
# --------------------------------------------------------------------------- #

@dataclass
class DocTiming:
    accession: str
    document: str
    issuer: str = None
    verdicts: int = 0
    first_at: str = None
    last_at: str = None
    seconds: float = None

    def as_dict(self):
        return {"accession": self.accession, "document": self.document,
                "issuer": self.issuer, "verdicts": self.verdicts,
                "first_at": self.first_at, "last_at": self.last_at,
                "seconds": self.seconds}


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def per_document_timings(store, session_id):
    """Wall-clock per document: the span from a document's first recorded
    verdict to its last. `seconds` is None when the session ran without a clock
    or a document has a single verdict (no span to measure)."""
    order = {(d["accession"], d["document"]): (i, d.get("issuer"))
             for i, d in enumerate(store.validated_documents(session_id))}
    stamps = {}
    for row in store.session_verdict_rows(session_id):
        key = (row["accession"], row["document"])
        t = _parse_iso(row["created_at"])
        first, last, n = stamps.get(key, (None, None, 0))
        n += 1
        if t is not None:
            first = t if first is None or t < first else first
            last = t if last is None or t > last else last
        stamps[key] = (first, last, n)

    out = []
    for key, (first, last, n) in stamps.items():
        idx, issuer = order.get(key, (len(order), None))
        seconds = (last - first).total_seconds() if (first and last and last > first) else None
        out.append((idx, DocTiming(
            accession=key[0], document=key[1], issuer=issuer, verdicts=n,
            first_at=first.isoformat() if first else None,
            last_at=last.isoformat() if last else None, seconds=seconds)))
    out.sort(key=lambda p: p[0])
    return [t for _, t in out]


def timing_summary(timings):
    """Median and mean seconds/document over the documents that were timed."""
    secs = sorted(t.seconds for t in timings if t.seconds is not None)
    if not secs:
        return {"timed_documents": 0, "median_seconds": None, "mean_seconds": None}
    n = len(secs)
    median = secs[n // 2] if n % 2 else (secs[n // 2 - 1] + secs[n // 2]) / 2
    return {"timed_documents": n, "median_seconds": median,
            "mean_seconds": sum(secs) / n}


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #

def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _secs(x):
    if x is None:
        return "n/a"
    m, s = divmod(int(round(x)), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def render_eval_markdown(report):
    """Render an `eval_harness.EvalReport` (criterion 3, the held-out baseline)
    as a per-field table. This is the authoritative precision -- it re-ran the
    extractor against held-out gold -- unlike the verdict-only accept rate."""
    lines = [
        f"## Held-out eval baseline (`{report.report_id}`)",
        "",
        f"- spec: `{report.spec_id}` v`{report.spec_version}`  "
        f"local model: `{report.local_model or 'n/a'}`",
        f"- exemplar set: `{report.exemplar_set_version or 'n/a'}`  "
        f"rule set: `{report.rule_set_version or 'n/a'}`",
        f"- documents scored: {report.n_documents}  "
        f"sessions: {', '.join(report.held_out_sessions) or 'n/a'}",
        "",
        "| field | precision | recall | span acc | escalation | absence FP |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(report.fields):
        m = report.fields[name]
        lines.append(
            f"| {name} | {_pct(m.get('precision'))} | {_pct(m.get('recall'))} "
            f"| {_pct(m.get('span_accuracy'))} | {_pct(m.get('escalation_rate'))} "
            f"| {_pct(m.get('absence_fp_rate'))} |")
    return "\n".join(lines)


def render_session_report(store, session_id, spec=None, *, eval_report=None):
    """The full run report for #165 as markdown: per-field accept rate, the
    compounding split, per-document wall-clock, and -- when an
    `eval_harness.EvalReport` is supplied -- the held-out precision baseline."""
    accepts = field_accept_rates(store, session_id, spec)
    comp = compounding_split(store, session_id)
    timings = per_document_timings(store, session_id)
    tsum = timing_summary(timings)
    docs = store.validated_documents(session_id)
    issuers = sorted({d.get("issuer") for d in docs if d.get("issuer")})

    lines = [
        f"# Validation run report -- session `{session_id}`",
        "",
        f"- documents validated: {len(docs)}",
        f"- issuers: {len(issuers)} ({', '.join(issuers) or 'n/a'})",
        f"- per-document time: median {_secs(tsum['median_seconds'])}, "
        f"mean {_secs(tsum['mean_seconds'])} over {tsum['timed_documents']} timed doc(s)",
        "",
        "## Compounding: first half vs second half",
        "",
        "Accept rate over the earlier validated documents vs the later ones. If "
        "the loop compounds (exemplars written during the session help), the "
        "second half accepts at a higher rate.",
        "",
        "| half | documents | accepted / produced | accept rate |",
        "|---|---|---|---|",
        f"| {comp.first.label} | {comp.first.documents} | "
        f"{comp.first.accept} / {comp.first.produced} | {_pct(comp.first.accept_rate)} |",
        f"| {comp.second.label} | {comp.second.documents} | "
        f"{comp.second.accept} / {comp.second.produced} | {_pct(comp.second.accept_rate)} |",
        f"| **delta** | | | **{_pct(comp.delta) if comp.delta is not None else 'n/a'}** |",
        "",
        "## Per-field accept rate (verdict-only)",
        "",
        "Upper bound on precision: a reject records absence, not whether the "
        "model hallucinated there. Authoritative precision is in the held-out "
        "eval section below.",
        "",
        "| field | accept | correct | reject | accept rate |",
        "|---|---|---|---|---|",
    ]
    for fa in accepts:
        lines.append(f"| {fa.field} | {fa.accept} | {fa.correct} | {fa.reject} "
                     f"| {_pct(fa.accept_rate)} |")

    lines += ["", "## Per-document wall-clock", "",
              "| # | issuer | accession | verdicts | time |",
              "|---|---|---|---|---|"]
    for i, t in enumerate(timings, 1):
        lines.append(f"| {i} | {t.issuer or 'n/a'} | {t.accession} | "
                     f"{t.verdicts} | {_secs(t.seconds)} |")

    if eval_report is not None:
        lines += ["", render_eval_markdown(eval_report)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(argv=None):
    p = argparse.ArgumentParser(description="Validation-session run report (#165)")
    p.add_argument("--session", help="session id to report on")
    p.add_argument("--store", default=None, help="ValidationStore path (default: local home)")
    p.add_argument("--spec", default=None,
                   help="spec id (from field_specs/) so skipped fields still show")
    p.add_argument("--selftest", action="store_true", help="run the offline self-check and exit")
    args = p.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not args.session:
        p.error("--session is required (or pass --selftest)")

    spec = None
    if args.spec:
        specs = load_specs()
        spec = specs.get(args.spec)
        if spec is None:
            p.error(f"unknown spec {args.spec!r}; known: {', '.join(sorted(specs)) or '(none)'}")
    with ValidationStore(args.store, readonly=True) as store:
        print(render_session_report(store, args.session, spec))
    return 0


# --------------------------------------------------------------------------- #
# Self-check -- in-memory store, no model, no network (mirrors test_validation)
# --------------------------------------------------------------------------- #

def _selftest():
    from validation import FieldVerdict, ValidationSession

    class _Spec:
        spec_id = "selftest"
        form_type = "424B2"

        def field_names(self):
            return ["issuer", "aggregate_principal", "barrier_pct"]

        def field(self, name):
            return object()

    clock = {"t": 0}

    def tick():
        clock["t"] += 30
        return datetime.fromtimestamp(clock["t"]).isoformat()

    store = ValidationStore(":memory:")
    spec = _Spec()
    sess = ValidationSession(store, spec, session_id="st", target_n=4, clock=tick)

    # Four docs, two issuers. Early docs get more corrects, later ones more
    # accepts -- a compounding delta the report must surface as positive.
    plan = [
        ("acc1", "JPM", [ACCEPT, CORRECT, CORRECT]),
        ("acc2", "JPM", [ACCEPT, CORRECT, REJECT]),
        ("acc3", "Citi", [ACCEPT, ACCEPT, CORRECT]),
        ("acc4", "Citi", [ACCEPT, ACCEPT, ACCEPT]),
    ]
    for acc, issuer, verdicts in plan:
        for field, v in zip(spec.field_names(), verdicts):
            if v == ACCEPT:
                fv = FieldVerdict(field=field, verdict=ACCEPT, value="x")
            elif v == CORRECT:
                fv = FieldVerdict.correct(field, "y")
            else:
                fv = FieldVerdict.reject(field)
            sess.record_verdict(acc, "424b2.htm", fv, issuer=issuer)
        sess.complete_document(acc, "424b2.htm", issuer=issuer)

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + ": " + msg)
        ok = ok and cond

    rates = {fa.field: fa for fa in field_accept_rates(store, "st", spec)}
    check(rates["issuer"].accept == 4 and rates["issuer"].accept_rate == 1.0,
          "issuer accepted on all four documents")
    check(rates["barrier_pct"].correct == 2 and rates["barrier_pct"].reject == 1,
          "barrier_pct tally counts corrects and rejects")

    comp = compounding_split(store, "st")
    check(comp.first.documents == 2 and comp.second.documents == 2,
          "compounding split halves the four documents")
    check(comp.delta is not None and comp.delta > 0,
          "second half accepts more than first (compounding delta positive)")

    timings = per_document_timings(store, "st")
    check(len(timings) == 4, "a timing row per document")
    check(all(t.seconds is not None and t.seconds > 0 for t in timings),
          "each document has a positive wall-clock from verdict timestamps")
    check([t.accession for t in timings] == ["acc1", "acc2", "acc3", "acc4"],
          "timings come back in validation order")

    md = render_session_report(store, "st", spec)
    check("Compounding" in md and "Per-document wall-clock" in md,
          "markdown report renders every section")

    store.close()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
