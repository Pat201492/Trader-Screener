#!/usr/bin/env python3
"""
Local tool-runner service for the Trader Screener dashboard.

Why this exists: the Tools workspace was a CATALOG -- every card was a link to a
README, so "open the tool" meant "go read a repo and install it by hand". This
serves the same static dashboard AND a small JSON API next to it, so a card can
actually RUN its tool and show the result in the page.

Ownership boundary is unchanged (ARCHITECTURE.md, #109): this process is a
READER of shared pipeline data and a writer only of the scrubber's own LOCAL
store (`OutputStore` enforces that at open time). It stands up nothing
always-on -- it is a local dev service you start when you want to use a tool,
not a second collector.

stdlib only, one origin (static + API on the same port, so no CORS).

Run:
    python tools/server.py                  # http://127.0.0.1:8137/
    python tools/server.py --port 9000
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRUBBER = REPO_ROOT / "tools" / "edgar_scrubber"
sys.path.insert(0, str(SCRUBBER))

DEFAULT_HOME = Path.home() / ".edgar-scrubber"
DEFAULT_FIELDS = [
    "issuer", "cusip", "underlyings", "product_type", "contingent_coupon_rate",
    "coupon_barrier_pct", "barrier_pct", "maturity_date",
    "estimated_value_per_1000",
]

# Docs a card may link to. An allowlist, not a path join on user input -- this
# server binds to loopback but still has no business reading outside the repo.
DOC_ROOTS = ("tools/", "Research/", "Education/", "Project folder/")


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Run registry
# --------------------------------------------------------------------------- #

class RunRegistry:
    """In-memory record of runs this process started. The extractions
    themselves are durable (they land in the scrubber's sqlite store); this is
    just the live progress view a browser polls."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs = {}
        self._seq = 0

    def create(self, kind, params):
        with self._lock:
            self._seq += 1
            run_id = f"{kind}-{self._seq:03d}"
            docs = params.get("limit", 0)
            per_doc = len(params.get("fields") or [])
            self._runs[run_id] = {
                "id": run_id, "kind": kind, "params": params,
                "status": "queued", "started_at": utcnow(), "finished_at": None,
                "log": [], "documents": [], "error": None, "store_run_id": None,
                "stop_requested": False,
                # Documents AND fields: a run of 5 filings x 9 fields moves 45
                # times, and a bar that only steps on document boundaries sits
                # frozen for minutes at a stretch.
                "progress": {"done": 0, "total": docs,
                             "fields_done": 0, "fields_total": docs * per_doc,
                             "current": None, "started_epoch": None,
                             "seconds_elapsed": 0, "seconds_remaining": None},
            }
            return run_id

    def tick_field(self, run_id, accession, field, elapsed):
        """One field finished. Elapsed time is measured, not assumed, so the
        estimate reflects THIS machine and this document size."""
        with self._lock:
            p = self._runs[run_id]["progress"]
            p["fields_done"] += 1
            p["current"] = {"accession": accession, "field": field}
            p["seconds_elapsed"] = round(elapsed, 1)
            if p["fields_done"] and p["fields_total"]:
                per = elapsed / p["fields_done"]
                left = max(0, p["fields_total"] - p["fields_done"])
                p["seconds_remaining"] = round(per * left)

    def request_stop(self, run_id):
        """Ask a run to stop at its next field boundary.

        Cooperative, not a kill: the in-flight model call finishes and the
        document it belongs to is written out, so stopping never leaves a
        half-written document in the store. Everything already extracted stays
        -- stopping is not undoing.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return False
            if run["status"] in ("done", "error", "stopped"):
                return False
            run["stop_requested"] = True
            return True

    def stop_requested(self, run_id):
        with self._lock:
            return bool(self._runs.get(run_id, {}).get("stop_requested"))

    def update(self, run_id, **fields):
        with self._lock:
            self._runs[run_id].update(fields)

    def log(self, run_id, message):
        with self._lock:
            self._runs[run_id]["log"].append({"t": utcnow(), "message": message})

    # Events, as opposed to log lines: a log line is prose for a human reading
    # afterwards, an event is the structured record of one decision -- which
    # field, what came back, and whether it was kept. The distinction matters
    # because "the run dropped that value and here is why" is the thing you
    # cannot reconstruct from a finished document, which only shows survivors.
    EVENT_CAP = 500

    def event(self, run_id, kind, **payload):
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            evs = run.setdefault("events", [])
            evs.append({"n": run.get("_evseq", 0), "t": utcnow(),
                        "kind": kind, **payload})
            run["_evseq"] = run.get("_evseq", 0) + 1
            # Bounded: a 450-filing run emits thousands, and this registry is
            # the live view, not the record. The store is the record.
            if len(evs) > self.EVENT_CAP:
                del evs[:len(evs) - self.EVENT_CAP]

    def events_since(self, run_id, after=-1):
        """Events numbered above `after`, so a poller fetches only what is new."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            evs = [e for e in run.get("events", []) if e["n"] > after]
            return json.loads(json.dumps({
                "run_id": run_id, "status": run["status"],
                "progress": run["progress"], "error": run.get("error"),
                # A poller that fell behind the cap needs to know it has a hole
                # rather than quietly rendering a gap as continuity.
                "dropped_before": (run["events"][0]["n"] if run.get("events") else 0),
                "events": evs,
            }))

    def add_document(self, run_id, doc):
        with self._lock:
            r = self._runs[run_id]
            r["documents"].append(doc)
            r["progress"]["done"] = len(r["documents"])

    def get(self, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            return json.loads(json.dumps(r)) if r else None

    # A run's RESULT is the bulky part -- extracted documents, scanned rows, a
    # whole field template. The list view is a status board polled every couple
    # of seconds, so it carries none of them and carries the one line of log a
    # reader actually wants: the most recent.
    _LIST_DROP = ("log", "documents", "rows", "proposals", "template",
                  "summary_fields", "events", "_evseq")

    def list(self):
        with self._lock:
            out = []
            for r in sorted(self._runs.values(), key=lambda x: x["id"], reverse=True):
                row = {k: v for k, v in r.items() if k not in self._LIST_DROP}
                row["last_log"] = (r["log"][-1]["message"] if r["log"] else None)
                out.append(row)
            return json.loads(json.dumps(out))


RUNS = RunRegistry()


# --------------------------------------------------------------------------- #
# The EDGAR scrubber run
# --------------------------------------------------------------------------- #

def edgar_urls(cik, accession, document=None):
    """sec.gov URLs for a filing. The tool should never be a dead end: whatever
    it extracted, the filing it came from is one click away, on EDGAR itself."""
    if not cik or not accession:
        return {}
    nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    out = {"filing_url": f"{base}/{accession}-index.htm", "folder_url": f"{base}/"}
    if document:
        out["document_url"] = f"{base}/{document}"
    return out


# EDGAR form codes are wildly varied -- "S-1", "DEF 14A", "13F-HR", "SC 13D/A",
# "SCHEDULE 13G" -- so search keeps NO allowlist of them and takes any
# well-formed code. "Well-formed" is the shape EDGAR itself uses: an
# alphanumeric lead, then letters, digits, spaces, dots, slashes or dashes, 20
# characters at most. That admits every real form and rejects an injected
# "10-K;DROP" before it reaches the query.
FORM_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 ./-]{0,19}$")


def normalize_forms(forms):
    """Split (if a string), upper-case and validate every form code.

    Upper-casing is safe -- EDGAR is case-insensitive on form and the registry
    stores them upper -- and each code is checked against `FORM_CODE_RE`.
    Anything malformed raises `ValueError` naming the offending code, which the
    search route surfaces as a 400."""
    if isinstance(forms, str):
        forms = [f.strip() for f in forms.split(",") if f.strip()]
    out = []
    for f in (forms or []):
        code = str(f).strip().upper()
        if not code:
            continue
        if not FORM_CODE_RE.match(code):
            raise ValueError(f"not a valid EDGAR form code: {f!r}")
        out.append(code)
    return out


def edgar_search_url(q, forms, ciks, startdt, enddt):
    """The same query in EDGAR's own full-text search UI, so a search here can
    be carried over to sec.gov rather than re-typed."""
    frag = []
    if q:
        frag.append("q=" + quote(q, safe=""))
    if forms:
        frag.append("forms=" + quote(",".join(forms), safe=""))
    if ciks:
        frag.append("ciks=" + quote(",".join(c.zfill(10) for c in ciks), safe=""))
    if startdt and enddt:
        frag.append("dateRange=custom")
        frag.append(f"startdt={startdt}")
        frag.append(f"enddt={enddt}")
    return "https://www.sec.gov/edgar/search/#/" + "&".join(frag)


def resolve_issuer_ciks(tokens, resolve_ticker):
    """Map issuer tokens -- each either a bare CIK or a ticker -- to CIKs.

    A digit string is already a CIK and passes through untouched; anything else
    is a ticker and is resolved through `resolve_ticker` (the injected
    `EdgarClient.ticker_to_cik`), so a ticker becomes a CIK BEFORE the query is
    built and there is no second ticker->CIK map to keep in step. Blanks are
    dropped. An unknown ticker raises whatever `resolve_ticker` raises
    (`EdgarLookupError`), which the caller surfaces as a 4xx naming the ticker.
    """
    out = []
    for tok in tokens:
        tok = (tok or "").strip()
        if not tok:
            continue
        out.append(tok if tok.isdigit() else resolve_ticker(tok))
    return out


def edgar_search(params):
    """One page of EDGAR full-text search, flattened for the UI.

    Cheap and synchronous on purpose: this is the "what is out there" step the
    user drives, not extraction. No model, no document fetch -- just the efts
    hit list, so a query can be adjusted a few times before spending minutes of
    local model time on it.
    """
    from edgar_client import EdgarClient, parse_hit, search_total

    # Params first, environment second: an empty form is the user's error and
    # should say so, not report a missing env var they did not ask about.
    q = (params.get("q") or "").strip()
    forms = normalize_forms(params.get("forms"))
    ciks = params.get("ciks") or []
    if isinstance(ciks, str):
        ciks = [c.strip() for c in ciks.split(",") if c.strip()]
    exclude = set(params.get("excludeCiks") or [])
    if not q and not forms and not ciks:
        raise ValueError("give at least a search phrase, a form type, or a CIK")

    max_results = max(1, min(int(params.get("max_results", 25)), 100))

    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set. The SEC fair-access policy requires a "
            "contactable '<name> <email>' on every request; see SETUP.md."
        )
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    client = EdgarClient(user_agent=ua, cache_dir=str(home / "cache"))

    # An issuer knob may carry a ticker (AAPL) or a bare CIK; resolve tickers to
    # CIKs here, reusing the client's own ticker_to_cik, so efts (which filters
    # only on numeric CIKs) sees CIKs regardless of what the operator typed.
    if ciks:
        ciks = resolve_issuer_ciks(ciks, client.ticker_to_cik)

    extra = {}
    if ciks:
        # efts filters server-side on `ciks`; zero-padding is what the API wants.
        extra["ciks"] = ",".join(c.zfill(10) for c in ciks)

    page = client.full_text_search(q=q or None, forms=forms or None,
                                    startdt=params.get("startdt") or None,
                                    enddt=params.get("enddt") or None, **extra)
    total, saturated = search_total(page)

    hits, seen = [], set()
    for h in ((page.get("hits", {}) or {}).get("hits", []) or []):
        info = parse_hit(h)
        if not info.get("accession") or info["accession"] in seen:
            continue
        if exclude & set(info.get("ciks") or []):
            continue
        seen.add(info["accession"])
        row = {k: info.get(k) for k in
               ("accession", "document", "issuer", "cik", "form",
                "file_date", "tickers")}
        row.update(edgar_urls(row.get("cik"), row.get("accession"), row.get("document")))
        hits.append(row)
        if len(hits) >= max_results:
            break

    return {"total": total, "saturated": saturated, "returned": len(hits),
            "hits": hits,
            "search_url": edgar_search_url(q, forms, ciks,
                                            params.get("startdt"), params.get("enddt")),
            "query": {"q": q, "forms": forms, "ciks": ciks,
                      "startdt": params.get("startdt"), "enddt": params.get("enddt")}}


def edgar_search_daily(params):
    """Sample `per_day` filings on each of the last `days` days that HAVE any.

    A flat search returns whatever the relevance ranking hands back, which
    clusters: ten filings from one busy week tells you about that week, not
    about time. Sampling a fixed number per day gives every day equal weight, so
    an average over the series is an average over TIME rather than over filing
    volume.

    "Available" days only: weekends and holidays have no 424B2s, and a market
    calendar is not something to hard-code here -- a day with zero hits is
    skipped and the walk continues backwards, so `days=90` means 90 days with
    filings, not 90 calendar days of which a third are empty. `max_lookback`
    bounds the walk so a quiet query cannot page backwards forever.
    """
    from datetime import date, timedelta

    per_day = max(1, min(int(params.get("per_day", 5)), 100))
    days = max(1, min(int(params.get("days", 90)), 365))
    max_lookback = max(days, min(int(params.get("max_lookback", days * 2 + 30)), 1000))

    end = params.get("enddt")
    cursor = date.fromisoformat(end) if end else date.today()
    floor_ = date.fromisoformat(params["startdt"]) if params.get("startdt") else None

    hits, by_day, empty_days, scanned = [], [], 0, 0
    while len(by_day) < days and scanned < max_lookback:
        day = cursor.isoformat()
        if floor_ and cursor < floor_:
            break
        scanned += 1
        cursor -= timedelta(days=1)

        # Weekends never carry filings; skipping them costs nothing and keeps
        # the request count (and the SEC's rate budget) proportional to signal.
        if (cursor + timedelta(days=1)).weekday() >= 5:
            continue

        page = edgar_search({**params, "startdt": day, "enddt": day,
                             "max_results": per_day})
        if not page["hits"]:
            empty_days += 1
            continue
        for h in page["hits"]:
            h["sample_day"] = day
        hits.extend(page["hits"])
        by_day.append({"day": day, "sampled": len(page["hits"]),
                       "available": page["total"], "saturated": page["saturated"]})

    by_day.reverse()
    hits.reverse()
    q = (params.get("q") or "").strip()
    forms = normalize_forms(params.get("forms"))
    ciks = params.get("ciks") or []
    if isinstance(ciks, str):
        ciks = [c.strip() for c in ciks.split(",") if c.strip()]
    return {
        "mode": "daily", "hits": hits, "returned": len(hits),
        "total": sum(d["available"] for d in by_day),
        "saturated": any(d["saturated"] for d in by_day),
        "days": by_day, "days_covered": len(by_day),
        "days_scanned": scanned, "days_empty": empty_days,
        "per_day": per_day,
        "search_url": edgar_search_url(q, forms, ciks,
                                        by_day[0]["day"] if by_day else None,
                                        by_day[-1]["day"] if by_day else None),
        "query": {"q": q, "forms": forms, "ciks": ciks, "per_day": per_day,
                   "days": days},
    }


def meta_issuer(targets):
    """The issuer of the first target -- exemplars are keyed by issuer, so this
    is only used to report how much this run has been taught before it starts."""
    return (targets[0].get("issuer") if targets else None)


def scrubber_run(run_id, targets, fields, home, self_consistency=1,
                 shadow_external=False, candidate_select=False):
    """expand -> reduce -> extraction ladder -> local store, over a caller-chosen
    list of filings.

    `targets` is what the user picked in the search UI -- `[{accession, cik,
    issuer, file_date}, ...]`. Discovery (which filings) is a separate,
    cheap step from extraction (what is in them), so a query can be tried
    several times before any model time is spent.

    Local rungs only: Claude escalation stays off here (it costs money per
    document and this is a button in a dashboard). A field the confidence gate
    wanted to escalate comes back flagged `gated_no_claude` rather than
    silently resolved -- same contract as `extraction_ladder`'s local-only mode.
    """
    import field_spec
    from edgar_client import EdgarClient
    from document_expand import expand_accession
    from reduce import split_sections
    from extraction_ladder import ExtractionLadder
    from ollama_client import OllamaClient, OllamaConfig
    from output_store import OutputStore, DocumentExtraction, FieldValue
    from validation import RenderDocument, LadderExtractor, FieldProposal

    t_start = time.time()
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set. The SEC fair-access policy requires a "
            "contactable '<name> <email>' on every request; see SETUP.md."
        )

    cache_dir = home / "cache"
    RUNS.update(run_id, status="running")
    client = EdgarClient(user_agent=ua, cache_dir=str(cache_dir))
    RUNS.log(run_id, f"{len(targets)} filing(s) selected")

    spec = field_spec.load_spec(SCRUBBER / "field_specs" / "424b2_structured_note.json")
    if fields == "all":
        field_names = [f.name for f in spec.fields]
    else:
        field_names = [f for f in fields if spec.field(f) is not None]

    # Same exemplar provider as the check (see preview_document): every span a
    # human confirmed for this (issuer, field) rides along in the rung-3 prompt,
    # so a run gets better as the store is taught rather than repeating the
    # same misreads on every filing.
    vstore = validation_store()
    taught = sum(1 for f in field_names if vstore(meta_issuer(targets), f))
    if taught:
        RUNS.log(run_id, f"{taught} field(s) carry human-confirmed examples from "
                         f"earlier filings")
    # Two accuracy switches, both off by default because both cost model calls.
    #
    # self_consistency=2 samples the local model twice per field and escalates on
    # disagreement. The gate signal for it already exists and has recorded
    # "single sample, not checked" on every run so far, because the default is 1.
    # It catches VARIANCE, not bias: a field the model gets wrong the same way
    # every time -- estimated_value_per_1000 returning the bounds midpoint on 21
    # of 25 filings -- sails through it. The span gate is what catches that.
    #
    # shadow_external asks the model for a field the EX-107 exhibit already
    # answered and records whether it agreed (#164). One field of 33, but the
    # only place in this system where model output can be scored against truth.
    #
    # candidate_select (#177/#178) enumerates the plausible values in code first
    # and asks the model for an INDEX instead of a figure, so a value it returns
    # is one the filing states. It was built, tested and unreachable: #178 owned
    # the rung and #179 owned the store contract, and no issue owned letting a
    # run switch it on.
    # Learn this issuer's boilerplate before extracting anything (#101's
    # build_boilerplate_model, which has been written and never called from a
    # run). 424B2s from one issuer are near-identical templates with the terms
    # swapped, so a number every filing prints identically is template text and a
    # number that varies is a term -- the strongest candidate filter available,
    # and it costs no tokens.
    #
    # The pre-pass only normalizes; EdgarClient is cache-first, so the main loop
    # below re-reads the same documents from cache rather than refetching. Needs
    # at least 3 filings: build_boilerplate_model returns nothing for a corpus of
    # one, correctly, since nothing is yet "cross-filing".
    boilerplate = None
    if candidate_select and len(targets) >= 3:
        try:
            from reduce import build_boilerplate_model
            corpus = []
            for m in targets:
                try:
                    b = expand_accession(client, m["cik"], m["accession"])
                    prim = b.primary()
                    if prim is not None and prim.normalized is not None:
                        corpus.append(prim.normalized)
                except Exception:
                    continue          # a filing we cannot read just is not in the corpus
            if len(corpus) >= 3:
                model = build_boilerplate_model(meta_issuer(targets), corpus)
                boilerplate = model.is_boilerplate
                RUNS.log(run_id, "boilerplate model: %d paragraph(s) common to >=80%% "
                                 "of %d filing(s) will not be offered as candidates"
                                 % (len(model.boilerplate_hashes), model.doc_count))
        except Exception as exc:
            RUNS.log(run_id, "boilerplate model unavailable (%s: %s) -- candidates "
                             "fall back to anchor proximity alone"
                             % (type(exc).__name__, exc))

    samples = max(1, int(self_consistency))
    ladder = ExtractionLadder(
        spec, local_client=OllamaClient(OllamaConfig.from_env({})),
        claude_client=None, claude_enabled=False, exemplars=vstore,
        self_consistency_samples=samples,
        shadow_external=bool(shadow_external),
        candidate_select=bool(candidate_select),
        boilerplate=boilerplate)
    if candidate_select:
        RUNS.log(run_id, "candidate-select ON -- the model picks from values "
                         "located in the filing rather than producing its own")
    RUNS.log(run_id, "self-consistency: %d sample(s)%s"
                     % (samples, " -- disagreement escalates" if samples > 1
                        else " -- gate signal inert at 1"))
    if shadow_external:
        RUNS.log(run_id, "external shadow ON -- the model is asked for "
                         "aggregate_principal too and agreement is recorded")

    store = OutputStore(home / "store" / "extractions.sqlite")
    store_run_id = store.start_run(spec.spec_id, getattr(spec, "version", "1"),
                                   utcnow(), note=f"dashboard run {run_id}")
    RUNS.update(run_id, store_run_id=store_run_id)

    stopped = False
    try:
        for meta in targets:
            if RUNS.stop_requested(run_id):
                stopped = True
                RUNS.log(run_id, "stop requested -- no further filings will be started")
                break
            accession = meta["accession"]
            RUNS.event(run_id, "doc_start", accession=accession,
                       issuer=meta.get("issuer"),
                       filed=meta.get("file_date") or meta.get("sample_day"))
            cik = meta.get("cik")
            if not cik:
                RUNS.log(run_id, f"{accession}: no CIK on the selection -- skipped")
                RUNS.add_document(run_id, {
                    "accession": accession, "document": None, "issuer": meta.get("issuer"),
                    "filed": meta.get("file_date"), "skipped": True, "fields": [],
                })
                continue
            RUNS.log(run_id, f"expanding {accession}")
            bundle = expand_accession(client, cik, accession)
            primary = bundle.primary()
            if primary is None or primary.normalized is None:
                RUNS.log(run_id, f"{accession}: no readable primary document -- skipped")
                RUNS.add_document(run_id, {
                    "accession": accession, "document": None, "issuer": meta.get("issuer"),
                    "filed": meta.get("file_date"), "skipped": True, "fields": [],
                })
                continue

            nd = primary.normalized
            low_text = nd.text.lower()
            sections = split_sections(nd)
            ex107 = bundle.ex107.as_dict() if bundle.ex107 else None
            RUNS.log(run_id, f"{accession}: {primary.name} "
                             f"({len(nd.text):,} chars, {len(sections)} sections)")

            out, field_values, underlyings, product_type = [], [], [], None
            # The model's own reading of the issuer, kept as a fallback for the
            # store's issuer column. A run started from an explicit selection
            # carries no EDGAR display name (resolve_targets passes the
            # {accession, cik} objects straight through), so meta has no issuer
            # and the column went NULL -- on 75 of 201 documents in the local
            # store, all of them runs #0016-#0018. The model had read it
            # correctly on every one of those filings.
            model_issuer = None
            extractor = LadderExtractor(spec, ladder, sections=sections)
            render_doc = RenderDocument.from_normalized(nd)
            # Deterministic first step: the filing's own labelled terms
            # ("Threshold Price:: $244.678, which is equal to 70% of the
            # starting price"). A field it reads with certainty never reaches
            # the model; everything else falls through exactly as before.
            # SCRUBBER_KEY_TERMS=0 turns it off (model-only, for comparison).
            key_terms = {}
            if os.environ.get("SCRUBBER_KEY_TERMS", "1") != "0":
                try:
                    from key_terms import extract_key_terms
                    key_terms = extract_key_terms(nd.text, field_names)
                except Exception as exc:          # never let it sink a document
                    RUNS.log(run_id, f"{accession}: key-terms step failed "
                                     f"({type(exc).__name__}: {exc}) -- model only")
                if key_terms:
                    RUNS.log(run_id, f"{accession}: key terms read "
                                     f"{len(key_terms)} field(s) without the model: "
                                     f"{', '.join(sorted(key_terms))}")
            for name in field_names:
                if RUNS.stop_requested(run_id):
                    stopped = True
                    RUNS.log(run_id, f"stop requested -- {accession} written with "
                                     f"{len(out)} of {len(field_names)} fields")
                    break
                # Emitted BEFORE the call, not after: a local model field takes
                # tens of seconds, and "reading barrier_pct" for forty seconds
                # is the difference between a run that looks alive and one that
                # looks hung.
                RUNS.event(run_id, "field_start", accession=accession, field=name)
                try:
                    kt = key_terms.get(name)
                    if kt is not None:
                        # Read off the filing's own term line: same proposal
                        # shape as the model's, so the span / bounds / format
                        # checks below judge it exactly the same way.
                        p = FieldProposal(
                            field=name, value=kt.value, raw_value=kt.raw,
                            source_span=tuple(kt.span), rung="terms",
                            provenance=f"terms:{kt.method}:{kt.label}", confidence=1.0,
                            unit=getattr(spec.field(name), "unit", None),
                            flags=[f.as_dict() for f in spec.check_value(name, kt.value, ex107)])
                        if getattr(kt, "bound", None) == "min":
                            # A preliminary filing's "at least 13.50% per
                            # annum": the floor, not the final figure.
                            p.flags.append({
                                "field": name, "code": "preliminary_minimum",
                                "severity": "info",
                                "message": "the filing states only a minimum (\"at least "
                                           f"{kt.raw}\"); the final figure is set on the "
                                           "pricing date and may be higher"})
                    else:
                        # One field at a time through LadderExtractor so the stored
                        # span is in ORIGINAL document coordinates (it resolves the
                        # model's context-relative offsets back through the #101
                        # reduction map). A span nothing can point at is not evidence.
                        p = extractor.propose(render_doc, issuer=meta.get("issuer"),
                                              ex107=ex107, accession=accession,
                                              document=primary.name, fields=[name])[0]
                except Exception as exc:  # one bad field must not kill the document
                    out.append({"field": name, "error": f"{type(exc).__name__}: {exc}"})
                    RUNS.event(run_id, "field_error", accession=accession, field=name,
                               detail=f"{type(exc).__name__}: {exc}")
                    RUNS.tick_field(run_id, accession, name, time.time() - t_start)
                    continue
                # Same span repair the preview shows: what gets STORED as this
                # value's evidence has to be text that actually states it,
                # otherwise the store fills up with spans that point nowhere and
                # the #105 review loop has nothing to review.
                fdef = spec.field(name)
                # The vocabulary gate stops a MODEL inventing a value for a term
                # the filing never uses. A key-terms value was read off the
                # filing's own text with its span, so it is exempt -- JPMorgan
                # states one "Threshold Price" that also governs the coupon, and
                # never writes "coupon barrier", yet 70% is what it says.
                vocab = None if p.rung == "terms" else field_vocabulary_present(low_text, fdef)
                p_flags = list(p.flags or [])
                element_spans = None
                stored_value = p.value
                if vocab is False:
                    span, span_method = None, None
                elif isinstance(p.value, list):
                    # Array fields resolve per element (#167). A serialized
                    # array is never a verbatim substring of a filing, so the
                    # whole-value path marked every one of them unlocatable and
                    # no span-based check could see inside them.
                    named, kinds = classify_array_members(p.value)
                    kept, dropped, element_spans = locate_array_elements(
                        nd.text, named, near=anchor_positions(low_text, fdef))
                    if kinds:
                        p_flags.append({
                            "field": p.field, "code": "classification_in_array",
                            "severity": "warn",
                            "message": "dropped %s from the list: a classification "
                                       "is not a member. The kind belongs in its "
                                       "own field, not a slot in the array."
                                       % ", ".join(repr(k) for k in kinds),
                        })
                    if dropped:
                        p_flags.append({
                            "field": p.field, "code": "element_span_unlocatable",
                            "severity": "warn",
                            "message": "dropped %s: not stated verbatim anywhere in "
                                       "the document text, so no span could be "
                                       "verified for %s"
                                       % (", ".join(repr(d) for d in dropped),
                                          "it" if len(dropped) == 1 else "them"),
                        })
                    # What survives is what gets stored: a list whose every
                    # member the filing actually states.
                    stored_value = kept
                    span = (tuple(element_spans[str(kept[0]).strip()]["span"])
                            if kept else None)
                    span_method = "per-element" if kept else None
                else:
                    # Locate the value as the FILING prints it, not as we store
                    # it. A date is normalized to ISO before it gets here, and
                    # searching a filing for "2026-08-31" never matches because
                    # the document says "August 31, 2026" -- which withheld every
                    # date on run #0019.
                    locate_target = p.raw_value if p.raw_value is not None else p.value
                    span, span_method = locate_value(
                        nd.text, locate_target, p.source_span,
                        near=anchor_positions(low_text, fdef))
                if vocab is False and p.value not in (None, "", []):
                    # The filing never uses this field's vocabulary. A value
                    # here is almost certainly invented to fill the slot, and
                    # it must not enter the store looking like a reading.
                    p_flags.append({
                        "field": p.field, "code": "field_absent_from_document",
                        "severity": "warn",
                        "message": "none of this field's anchors appear in the filing; "
                                   "this note type likely does not carry it, so the "
                                   "value is unsupported",
                    })
                elif span is None and stored_value not in (None, "", []):
                    # An array reports per element above; this is the scalar case.
                    if not isinstance(stored_value, list):
                        p_flags.append({
                            "field": p.field, "code": "span_unlocatable",
                            "severity": "warn",
                            "message": "the value is not stated verbatim anywhere in "
                                       "the document text; no span could be verified",
                        })
                flags = [f.get("code") for f in p_flags]
                gated = "gated_no_claude" in flags
                stored_value, withheld_flag = withhold_unsupported_value(
                    fdef, stored_value, span, flags)
                if withheld_flag:
                    p_flags.append(withheld_flag)
                    flags = [f.get("code") for f in p_flags]
                field_values.append(FieldValue(
                    field=p.field, value=stored_value, unit=p.unit,
                    span=tuple(span) if span else None,
                    provenance=(f"{p.provenance}+span:{span_method}"
                                if span_method else p.provenance),
                    confidence=p.confidence, flags=p_flags,
                    # The chosen value's count, never the list (#179): the
                    # candidates are scratch and reach neither the store nor the
                    # run event; the count does, and 0 marks the free-form path.
                    candidate_count=getattr(p, "candidate_count", 0)))
                if name == "underlyings" and isinstance(stored_value, list):
                    kinds_seen = classify_array_members(p.value)[1]
                    underlyings = [{"name": str(u),
                                    "kind": kinds_seen[0] if kinds_seen else None}
                                   for u in stored_value]
                if name == "product_type" and p.value:
                    product_type = str(p.value)
                # Only a value the filing actually supports: `stored_value` is
                # post-gate, so an issuer nothing could locate never becomes the
                # key that exemplars are filed under.
                if name == "issuer" and stored_value:
                    model_issuer = str(stored_value)
                # Per-field, not per-document: a 9-field document is minutes of
                # local model time, and a progress view that only ticks when the
                # whole document lands looks hung.
                RUNS.log(run_id, f"  {name} = {str(p.value)[:60]} "
                                 f"[{p.rung}{', gated' if gated else ''}]")
                # The verdict, as its own event. `kept` is the whole point: a
                # finished document lists only survivors, so a value the run
                # produced and then threw away leaves no trace anywhere else.
                empty = p.value in (None, "", [])
                withheld = ("value_withheld_no_span" in flags
                            or "value_withheld_out_of_bounds" in flags
                            or "value_withheld_invalid_format" in flags
                            or "value_withheld_not_in_document" in flags)
                dropped = ("field_absent_from_document" in flags
                           or "span_unlocatable" in flags
                           or "element_span_unlocatable" in flags
                           or withheld)
                RUNS.event(
                    run_id, "field_done", accession=accession, field=name,
                    # The model's value, not the stored one: a figure the run
                    # produced and then withheld leaves no trace anywhere else.
                    value=(str(p.value)[:120] if not empty else None),
                    rung=p.rung, gated=gated, confidence=p.confidence,
                    span=list(span) if span else None,
                    # The count the value was chosen from, never the list (#179).
                    candidate_count=getattr(p, "candidate_count", 0),
                    kept=bool(span) and not dropped,
                    verdict=("no value" if empty else
                             "withheld" if withheld else
                             "dropped" if dropped else
                             "kept" if span else "no span"),
                    reason=("the filing never uses this field's wording"
                            if "field_absent_from_document" in flags else
                            "no span supports this figure, and every span-less "
                            "figure on the sample was wrong (#163)"
                            if withheld else
                            "the value is not stated verbatim in the document"
                            if "span_unlocatable" in flags else None),
                    seconds=round(time.time() - t_start, 1))
                RUNS.tick_field(run_id, accession, name, time.time() - t_start)
                out.append({
                    "field": name, "value": p.value, "unit": p.unit,
                    "rung": p.rung, "confidence": p.confidence, "gated": gated,
                    "anchor": label_before(nd.text, span, fdef),
                    "likely_absent": vocab is False,
                    "span": list(span) if span else None,
                    "span_source": span_method,
                    "span_text": nd.text[span[0]:span[1]] if span else None,
                    "candidate_count": getattr(p, "candidate_count", 0),
                    "flags": flags,
                })

            # One document write, carrying the queryable dimensions -- notably
            # the FILING date, which is what a daily sample gets averaged over.
            # Without it the extractions are undated and no series can be built.
            # EDGAR's display name when the run came from a search; otherwise the
            # model's own reading. Exemplars, rules and coverage are all keyed by
            # issuer, so a NULL here does not merely look untidy -- it silently
            # detaches everything the run produced from the issuer it belongs to.
            doc_issuer = meta.get("issuer") or model_issuer
            store.write_document(store_run_id, DocumentExtraction(
                accession=accession, document=primary.name,
                issuer=doc_issuer, product_type=product_type,
                filing_date=meta.get("file_date") or meta.get("sample_day"),
                underlyings=underlyings, fields=field_values))

            RUNS.add_document(run_id, {
                "accession": accession, "document": primary.name,
                "issuer": doc_issuer, "filed": meta.get("file_date"),
                "skipped": False, "fields": out,
            })
            RUNS.event(run_id, "doc_done", accession=accession,
                       document=primary.name, issuer=doc_issuer,
                       fields=len(out),
                       kept=sum(1 for f in out if f.get("span")),
                       written=True)

            if stopped:
                break

        RUNS.update(run_id, status="stopped" if stopped else "done",
                    finished_at=utcnow(), summary=ladder.log.summary())
        RUNS.log(run_id, "run stopped by request -- everything extracted so far is saved"
                 if stopped else "run complete")
    finally:
        store.close()
        vstore.close()


# --------------------------------------------------------------------------- #
# Testing: the run comparison, as JSON
#
# compare_runs.py (#180) already computes this and prints a table. The dashboard
# needs the same numbers as data, so this wraps that module rather than
# recomputing the stats -- two implementations of "how many values did this field
# produce" would drift, and the CLI is the one with tests.
# --------------------------------------------------------------------------- #

def compare_runs_json(run_a, run_b, field=None):
    """Per-field before/after for two runs. Read-only."""
    sys.path.insert(0, str(REPO_ROOT / "tools")) if str(REPO_ROOT / "tools") not in sys.path else None
    import compare_runs as cr

    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    store = home / "store" / "extractions.sqlite"
    conn = cr.open_readonly(str(store))
    try:
        cr.require_run(conn, run_a)
        cr.require_run(conn, run_b)
        a = cr.field_stats(conn, run_a)
        b = cr.field_stats(conn, run_b)
    finally:
        conn.close()

    fields = sorted(set(a) | set(b))
    rows = []
    for f in fields:
        sa, sb = a.get(f), b.get(f)

        def side(s):
            if not s:
                return {"n": 0, "present": 0, "spanned": 0, "distinct": 0,
                        "flags": 0, "dominated": False, "top": None}
            dom = cr.dominant_value(s)
            return {
                "n": s["n"], "present": s["present"], "spanned": s["spanned"],
                "distinct": s["distinct"], "flags": s["flags"],
                "dominated": bool(cr.is_dominated(s)),
                "top": ({"value": cr._display_value(dom[0]), "count": dom[1],
                         "fraction": round(dom[2], 3)} if dom else None),
            }

        row = {"field": f, "a": side(sa), "b": side(sb)}
        if field and f == field:
            row["values"] = {
                "a": [{"value": cr._display_value(k), "count": c}
                      for k, c in (sa or {"values": {}})["values"].most_common(25)],
                "b": [{"value": cr._display_value(k), "count": c}
                      for k, c in (sb or {"values": {}})["values"].most_common(25)],
            }
        rows.append(row)

    return {"run_a": run_a, "run_b": run_b, "field": field, "rows": rows,
            "store": str(store)}


# --------------------------------------------------------------------------- #
# Taxonomy-sourced forms
#
# The registry (#209/#212) lists four form types and marks each with a
# `field_source`. 424B2 is `spec`: its terms are prose, so a hand-authored field
# spec is the only way to name them. 10-K, 10-Q and 8-K are `taxonomy`: every
# number in their financial statements carries an SEC-taxonomy tag, so the filing
# itself states what each value is (#210/#218).
#
# Nothing read `field_source` before this. Every extraction path loaded the 424B2
# spec from a hardcoded path, so picking 10-Q in the UI would have fetched
# quarterly reports and then tried to find `barrier_pct` in them. That is why no
# run of any form but 424B2 exists -- not an untested path, an absent one.
#
# A taxonomy run needs no model and no ladder: the facts are already structured
# and already attributed, so there is nothing to gate and no span to earn. It
# writes to FactsStore (#183), which is period-keyed, rather than OutputStore,
# which is document-keyed and would need span and spec nulled out on every row.
# --------------------------------------------------------------------------- #

def scrubber_for_forms(forms):
    """The registry entry owning these form types, or None.

    None means "no scrubber claims this". For a NON-EMPTY form list that is an
    error the caller raises on (a form nothing extracts must not be scrubbed as
    a 424B2 note); an EMPTY list also returns None and the caller keeps the
    424B2 spec default, which the saved 424B2 queries rely on.
    """
    try:
        import scrubbers as scrubbers_mod
    except ImportError:
        return None
    reg = scrubbers_mod.load_scrubbers()
    wanted = {str(f).strip().upper() for f in (forms or []) if str(f).strip()}
    for s in reg.values():
        if wanted and wanted <= {f.upper() for f in s.forms}:
            return s
    return None


def taxonomy_run(run_id, targets, home, form_type=None):
    """Extract a taxonomy-sourced form: XBRL facts in, FactRecords out.

    Zero tokens and no model. `companyfacts` is fetched once per filer (the
    client is cache-first, so repeated filers cost nothing) and each filing's own
    tagged concepts are read off it -- never a concept the filing does not tag.
    """
    from edgar_client import EdgarClient
    from facts_store import FactRecord, FactsStore
    from taxonomy_fields import fields_for_filing
    from calc_linkbase import find_cal_url, parse_cal

    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set. The SEC fair-access policy requires a "
            "contactable '<name> <email>' on every request; see SETUP.md."
        )
    client = EdgarClient(user_agent=ua, cache_dir=str(home / "cache"))
    RUNS.update(run_id, status="running")

    facts_cache, written, skipped = {}, 0, 0
    # Honour the caller's home. FactsStore() with no path goes to the default
    # local root, which means a test cannot direct it anywhere and would write
    # into the real store -- the run has a `home` for exactly this reason.
    store = FactsStore(path=str(home / "store" / "facts.sqlite"))
    try:
        for meta in targets:
            if RUNS.stop_requested(run_id):
                RUNS.log(run_id, "stop requested -- no further filings started")
                break
            cik, accession = str(meta["cik"]), meta["accession"]
            # A bare {accession, cik} matched no fact at all: companyfacts is
            # filtered on the form, and the form was "" (a 20-F read 0 facts
            # of its 531, 2026-10-19). Same fallback as xml_run.
            form = (meta.get("form") or meta.get("form_type")
                    or filing_meta(client, cik, accession)[0] or form_type or "").upper()
            try:
                if cik not in facts_cache:
                    facts_cache[cik] = client.company_facts(cik)
                result = fields_for_filing(facts_cache[cik], form, accession)
            except Exception as exc:
                skipped += 1
                RUNS.log(run_id, "%s: %s: %s" % (accession, type(exc).__name__, exc))
                RUNS.event(run_id, "doc_done", accession=accession, written=False,
                           reason=str(exc)[:160])
                continue

            records = []
            skipped_occ = 0
            for tf in result:
                for occ in tf.occurrences:
                    # taxonomy_fields already names these the way FactsStore does.
                    end = occ.get("period_end")
                    # An instant fact (a balance-sheet point) states only an end;
                    # the store's contract is start == end for those. A fact with
                    # no end at all has no period, so it cannot be stored
                    # period-keyed -- count it rather than invent a date.
                    if not end:
                        skipped_occ += 1
                        continue
                    records.append(FactRecord(
                        cik=cik, concept=tf.concept, unit=occ.get("unit") or "",
                        period_start=occ.get("period_start") or end,
                        period_end=end,
                        fiscal_year=occ.get("fiscal_year"),
                        fiscal_period=occ.get("fiscal_period") or "",
                        form=form, accession=accession, value=occ.get("value"),
                        source="xbrl", filed=occ.get("filed")))
            if skipped_occ:
                RUNS.log(run_id, "%s: %d fact(s) carried no period end and were "
                                 "not stored" % (accession, skipped_occ))
            if records:
                store.put_facts(records)
            written += len(records)

            # Store the filing's own calculation roll-ups beside its facts (#262),
            # so the standard layer can flag a value that does not equal the sum
            # the filer says it equals. A filing with no `_cal.xml`, or any
            # fetch/parse error, logs one line naming the accession and never
            # fails the run.
            try:
                cal_url = find_cal_url(client, cik, accession)
                if cal_url is None:
                    RUNS.log(run_id, "%s: no calculation linkbase (_cal.xml); "
                                     "no roll-ups stored" % accession)
                else:
                    arcs = parse_cal(client.get_text(cal_url))
                    store.put_arcs(cik, accession, arcs)
                    RUNS.log(run_id, "%s: %d calculation arc(s) stored"
                                     % (accession, len(arcs)))
            except Exception as exc:
                RUNS.log(run_id, "%s: calculation linkbase skipped: %s: %s"
                                 % (accession, type(exc).__name__, exc))

            RUNS.add_document(run_id, {
                "accession": accession, "document": None,
                "issuer": meta.get("issuer"), "filed": meta.get("file_date"),
                "skipped": False,
                "fields": [{"field": tf.concept, "value": len(tf.occurrences),
                            "rung": "xbrl"} for tf in result],
            })
            RUNS.log(run_id, "%s: %d concept(s), %d fact(s)"
                             % (accession, len(result), len(records)))
            RUNS.event(run_id, "doc_done", accession=accession,
                       fields=len(result), kept=len(records), written=True)

        RUNS.update(run_id, status="done", finished_at=utcnow(),
                    facts_written=written, filings_skipped=skipped)
        RUNS.log(run_id, "run complete -- %d fact(s) from %d filing(s), %d skipped"
                         % (written, len(targets) - skipped, skipped))
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Structured-XML forms (#286, built on #283's xml_forms + FormsStore)
#
# A third `field_source`, alongside `spec` and `taxonomy`. Forms 3/4/5 (insider
# ownership) are neither prose a model reads nor XBRL-tagged financials -- they
# are a dedicated XML form document whose own schema names every field, so
# `xml_forms.read_filing` lifts them exactly, with no model and no field spec.
# The rows land in FormsStore (#283), which is keyed by the filing they came
# from, not by period (facts) or document+field (extractions).
# --------------------------------------------------------------------------- #

def pick_xml_rows(bundle, form):
    """The first XML document in `bundle` that `xml_forms.read_filing` accepts
    for `form`, as `(document_name, rows)`; `(None, None)` when the filing
    carries no XML this form's reader can read.

    The ownership forms ship their data as the primary `.xml` document, but the
    same loop also handles a form whose data lives in a separate XML exhibit:
    every `.xml` item is tried until one reads. A 13F-HR, for one, keeps its
    holdings in a separate information-table document while its cover fields
    (period of report, filing manager) live in `primary_doc.xml`; that primary
    document's text is handed to every read attempt, so the information-table
    reader can stamp the cover fields onto each holding. A wrong-schema document
    (an XBRL instance, a calc/label sidecar, or -- for 13F -- the cover page
    itself) raises `XmlFormError` and is skipped, as does any item that is not
    well-formed XML."""
    import xml_forms
    xml_docs = []
    for doc in bundle.documents:
        name = doc.name or ""
        if not name.lower().endswith(".xml"):
            continue
        raw = doc.raw_bytes
        if raw is None:
            continue
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        xml_docs.append((name, text))

    primary_text = next(
        (text for name, text in xml_docs if name.lower() == "primary_doc.xml"),
        None)

    for name, text in xml_docs:
        try:
            rows = xml_forms.read_filing(form, text, primary_text=primary_text)
        except Exception:
            # Not this form's schema, or not well-formed -- try the next item.
            continue
        return name, rows
    return None, None


_SUBMISSIONS_CACHE = {}


def filing_meta(client, cik, accession):
    """(form, filing_date) for one accession from the filer's submissions feed
    (data.sec.gov/submissions, recent filings), or (None, None) if the feed is
    unavailable or the accession is not in its recent window. Cached per CIK
    for the life of the process -- a run over one filer reads the feed once."""
    key = str(cik).lstrip("0")
    feed = _SUBMISSIONS_CACHE.get(key)
    if feed is None:
        try:
            recent = (client.submissions(cik).get("filings") or {}).get("recent") or {}
            feed = {a: (f, d) for a, f, d in zip(recent.get("accessionNumber") or [],
                                                 recent.get("form") or [],
                                                 recent.get("filingDate") or [])}
        except Exception:
            return (None, None)       # not cached: a transient failure retries next time
        _SUBMISSIONS_CACHE[key] = feed
    return feed.get(accession, (None, None))


def xml_run(run_id, targets, home, form_type=None):
    """Read structured-XML filings (Forms 3/4/5 today) into FormsStore.

    No model, no field spec and no OutputStore write: each accession is
    expanded, its XML form document is picked out (`pick_xml_rows`) and handed to
    `xml_forms.read_filing`, and the resulting rows are written to the local
    FormsStore keyed by the filing. A filing carrying no XML any reader accepts
    is logged and skipped; the run still finishes.

    `form_type` is the scrubber's own form (e.g. "4"), used as the reader form
    when a target carries no `form` of its own -- a selection out of the search
    UI sends `{accession, cik}` objects with no form on them.
    """
    from edgar_client import EdgarClient
    from document_expand import expand_accession
    from forms_store import FormsStore

    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set. The SEC fair-access policy requires a "
            "contactable '<name> <email>' on every request; see SETUP.md."
        )
    client = EdgarClient(user_agent=ua, cache_dir=str(home / "cache"))
    RUNS.update(run_id, status="running")

    store = FormsStore(path=str(home / "store" / "forms.sqlite"))
    written, skipped, stopped, rows_total = 0, 0, False, 0
    try:
        for meta in targets:
            if RUNS.stop_requested(run_id):
                stopped = True
                RUNS.log(run_id, "stop requested -- no further filings started")
                break
            accession = meta["accession"]
            cik = meta.get("cik")
            # A selection carries only {accession, cik}. Falling back to the
            # scrubber's own form stamped a SCHEDULE 13G as "SCHEDULE 13D" (one
            # entry owns all four 13D/G forms) and left every filed date null,
            # so date filters on the forms API dropped the rows (2026-10-19).
            # The filer's submissions feed states both for the accession.
            sub_form, sub_filed = filing_meta(client, cik, accession) if cik else (None, None)
            form = (meta.get("form") or meta.get("form_type") or sub_form
                    or form_type or "").strip().upper()
            filed = meta.get("file_date") or meta.get("sample_day") or sub_filed
            RUNS.event(run_id, "doc_start", accession=accession,
                       issuer=meta.get("issuer"), filed=filed)

            def skip(reason):
                RUNS.log(run_id, "%s: %s -- skipped" % (accession, reason))
                RUNS.add_document(run_id, {
                    "accession": accession, "document": None,
                    "issuer": meta.get("issuer"), "filed": filed,
                    "skipped": True, "fields": []})
                RUNS.event(run_id, "doc_done", accession=accession,
                           written=False, reason=reason[:160])

            if not cik:
                skipped += 1
                skip("no CIK on the selection")
                continue
            try:
                bundle = expand_accession(client, cik, accession)
            except Exception as exc:
                skipped += 1
                skip("%s: %s" % (type(exc).__name__, exc))
                continue

            document, rows = pick_xml_rows(bundle, form)
            if document is None:
                skipped += 1
                skip("no readable XML for form %s" % (form or "?"))
                continue

            store.put_filing(run_id, accession, form, cik, filed, rows)
            written += 1
            rows_total += len(rows)
            RUNS.add_document(run_id, {
                "accession": accession, "document": document,
                "issuer": meta.get("issuer"), "filed": filed, "skipped": False,
                "fields": [{"field": "rows", "value": len(rows), "rung": "xml"}]})
            RUNS.log(run_id, "%s: %s -> %d row(s)" % (accession, document, len(rows)))
            RUNS.event(run_id, "doc_done", accession=accession, document=document,
                       fields=len(rows), kept=len(rows), written=True)

        RUNS.update(run_id, status="stopped" if stopped else "done",
                    finished_at=utcnow(), rows_written=written,
                    rows_total=rows_total, filings_skipped=skipped)
        RUNS.log(run_id, "run complete -- %d filing(s) read (%d row(s)), %d skipped"
                         % (written, rows_total, skipped))
    finally:
        store.close()


def resolve_targets(params):
    """What to extract, from any of the three ways a caller can say it:

      * `accessions`: an explicit selection out of a search result (what the UI
        sends -- the user ticked these rows);
      * `search`: a query to run first, then extract its first `limit` hits;
      * `query`: the id of a saved query in `queries/` (the pre-built presets).

    An explicit selection is never re-searched -- the rows the user saw are the
    rows that get extracted.

    The ceiling is deliberately high (a 90-day x 5/day sample is 450 filings)
    and deliberately not infinite: at roughly 40s per filing that is already a
    multi-hour run, which the caller is told about before starting.
    """
    limit = max(1, min(int(params.get("limit", 3)), 1000))

    picked = params.get("accessions")
    if picked:
        out = []
        for item in picked[:limit]:
            if isinstance(item, str):
                raise ValueError(f"accession {item!r} needs its CIK -- send "
                                 "{'accession': ..., 'cik': ...} objects")
            if not item.get("accession") or not item.get("cik"):
                raise ValueError("each selection needs both 'accession' and 'cik'")
            out.append(item)
        return out, {"source": "selection", "limit": limit}

    search_params = params.get("search")
    if search_params:
        search_params = dict(search_params)
        search_params.setdefault("max_results", limit)
        result = (edgar_search_daily(search_params)
                  if search_params.get("mode") == "daily" else edgar_search(search_params))
        return result["hits"][:limit], {"source": "search", "limit": limit,
                                        "query": result["query"],
                                        "total": result["total"]}

    from crawl import load_query
    query_name = params.get("query", "424b2-jpm-2025-pilot")
    query_path = SCRUBBER / "queries" / f"{Path(query_name).name}.json"
    if not query_path.exists():
        raise FileNotFoundError(f"no saved query named {query_name!r}")
    saved = load_query(query_path)
    result = edgar_search({"q": saved.q, "forms": list(saved.forms or []),
                            "ciks": list(saved.ciks or []),
                            "excludeCiks": list(saved.excludeCiks or []),
                            "startdt": saved.startdt, "enddt": saved.enddt,
                            "max_results": limit})
    return result["hits"][:limit], {"source": "saved-query", "query": query_name,
                                     "limit": limit, "total": result["total"]}


def accuracy_switches(params):
    """The two opt-in accuracy settings, from params or the environment.

    Environment as well as params so a run can be turned up without editing the
    request the dashboard sends: SCRUBBER_SELF_CONSISTENCY=2 and
    SCRUBBER_SHADOW_EXTERNAL=1.
    """
    def _env_int(name, default):
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    samples = params.get("self_consistency")
    if samples is None:
        samples = _env_int("SCRUBBER_SELF_CONSISTENCY", 1)
    samples = max(1, min(int(samples), 5))     # 5 is already 5x the model time

    shadow = params.get("shadow_external")
    if shadow is None:
        shadow = os.environ.get("SCRUBBER_SHADOW_EXTERNAL", "").strip().lower() \
            in ("1", "true", "yes", "on")

    cands = params.get("candidate_select")
    if cands is None:
        cands = os.environ.get("SCRUBBER_CANDIDATE_SELECT", "").strip().lower() \
            in ("1", "true", "yes", "on")
    return samples, bool(shadow), bool(cands)


def run_forms(params):
    """The form list a run is routed by: explicit `forms`, else the search's,
    else the forms the selected hits name themselves.

    The dashboard's Run sends the ticked search hits with no `forms`; each hit
    carries its own `form`. Without that last step, ticking Form 4 or 10-K hits
    and pressing Run took the 424B2 default -- the local model on a filing it
    cannot read (found 2026-10-19). A selection spanning two extractors is
    refused: one run reads one kind of filing."""
    forms = params.get("forms") or (params.get("search") or {}).get("forms") or []
    if isinstance(forms, str):
        forms = [f.strip() for f in forms.split(",") if f.strip()]
    if forms:
        return forms
    forms = sorted({str(a.get("form")).strip().upper()
                    for a in (params.get("accessions") or [])
                    if isinstance(a, dict) and a.get("form")})
    if len({getattr(scrubber_for_forms([f]), "id", f) for f in forms}) > 1:
        raise ValueError("one run per extractor: the selection mixes %s" % ", ".join(forms))
    return forms


def start_scrubber_run(params):
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    fields = params.get("fields") or DEFAULT_FIELDS

    # Which path this run takes comes from the registry, not from an assumption,
    # and it is resolved BEFORE any SEC request: a form nothing extracts must
    # fail loudly here rather than be quietly scrubbed as a 424B2 structured
    # note. `spec` (424B2) goes through the prose ladder; `taxonomy`
    # (10-K/10-Q/8-K) reads the filing's own XBRL tags and needs no model at
    # all. An empty form list keeps today's 424B2 default, because the saved
    # 424B2 queries carry no explicit `forms` here and rely on it.
    forms = run_forms(params)
    scrubber = scrubber_for_forms(forms)
    if scrubber is None and forms:
        raise ValueError("no scrubber extracts %s" % ", ".join(str(f) for f in forms))
    source = getattr(scrubber, "field_source", "spec") or "spec"

    targets, provenance = resolve_targets(params)
    if not targets:
        raise ValueError("nothing to extract -- the query returned no filings")
    samples, shadow, cands = accuracy_switches(params)

    run_id = RUNS.create("edgar-scrubber", {**provenance, "fields": fields,
                                             "limit": len(targets),
                                             "self_consistency": samples,
                                             "shadow_external": shadow,
                                             "candidate_select": cands,
                                             "form_type": getattr(scrubber, "form_type", None),
                                             "field_source": source})
    if source == "taxonomy":
        RUNS.log(run_id, "%s is taxonomy-sourced -- reading the filing's own XBRL "
                         "tags, no model and no field spec"
                         % getattr(scrubber, "form_type", "this form"))
    elif source == "xml":
        RUNS.log(run_id, "%s is a structured-XML form -- lifting its own XML "
                         "document, no model and no field spec"
                         % getattr(scrubber, "form_type", "this form"))

    def worker():
        try:
            if source == "taxonomy":
                taxonomy_run(run_id, targets, home,
                             form_type=getattr(scrubber, "form_type", None))
                return
            if source == "xml":
                xml_run(run_id, targets, home,
                        form_type=getattr(scrubber, "form_type", None))
                return
            scrubber_run(run_id, targets, fields, home,
                         self_consistency=samples, shadow_external=shadow,
                         candidate_select=cands)
        except Exception as exc:
            RUNS.update(run_id, status="error", finished_at=utcnow(),
                        error=f"{type(exc).__name__}: {exc}")
            RUNS.log(run_id, traceback.format_exc().strip().splitlines()[-1])

    threading.Thread(target=worker, daemon=True, name=f"run-{run_id}").start()
    return run_id


# --------------------------------------------------------------------------- #
# Reading one filing + hand-marked spans (the #105 loop, in the browser)
# --------------------------------------------------------------------------- #

def load_document(cik, accession, document=None):
    """The normalized text of one filing, plus its section index.

    Text, not HTML: this is the SAME string the extraction ladder sees, so an
    offset a human marks here is an offset the ladder can be taught with. Handing
    the browser the raw filing HTML instead would mean marking spans in a
    coordinate system nothing downstream uses.
    """
    from edgar_client import EdgarClient
    from document_expand import expand_accession
    from reduce import split_sections

    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError("EDGAR_USER_AGENT is not set; see SETUP.md.")
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    client = EdgarClient(user_agent=ua, cache_dir=str(home / "cache"))

    bundle = expand_accession(client, cik, accession)
    doc = bundle.by_name(document) if document else None
    if doc is None or doc.normalized is None:
        doc = bundle.primary()
    if doc is None or doc.normalized is None:
        raise ValueError(f"{accession}: no readable document to open")

    import field_spec
    spec = field_spec.load_spec(SCRUBBER / "field_specs" / "424b2_structured_note.json")

    nd = doc.normalized
    sections = [{"name": name, "start": s.text_start, "end": s.text_end}
                for name, spans in split_sections(nd).items() for s in spans]
    sections.sort(key=lambda s: s["start"])
    return {
        "accession": accession, "cik": cik, "document": doc.name,
        "chars": len(nd.text), "text": nd.text, "sections": sections,
        # Shipped with the document so "where do I look" is answered before the
        # first model call, not after it.
        "cues": document_cues(nd.text, spec),
        "manifest": bundle.manifest(),
        "ex107": bundle.ex107.as_dict() if bundle.ex107 else None,
        **edgar_urls(cik, accession, doc.name),
    }


# Phrases that mark where numbers live in a 424B2 regardless of which field
# they belong to. The spec's own per-field `anchors` say "this label means this
# field"; these say "a number near here is probably a term worth reading" --
# which is what you want when the labelled anchor is absent or worded oddly,
# and what makes the document skimmable by eye.
UNIT_CUES = [
    ("per annum", "rate"), ("per year", "rate"), ("annually", "rate"),
    ("per quarter", "rate"), ("quarterly", "rate"), ("monthly", "rate"),
    ("percent", "percent"), ("%", "percent"),
    ("per $1,000", "per-note"), ("$1,000 principal", "per-note"),
    ("principal amount", "per-note"),
    ("of the initial", "level"), ("of the Initial Stock Price", "level"),
    ("greater than or equal to", "level"), ("less than", "level"),
]

_NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")

# "Label::" or "Label:" sitting immediately before a value, anchored to the END
# of the preceding text.
_LABEL_RE = re.compile(r"([A-Z][A-Za-z0-9 ()/$&.,'’–-]{2,40}?)\s*:{1,2}\s*$")


def label_before(text, span, field=None, window=220):
    """The label a value sits under -- "Interest Barrier", "CUSIP", "Maturity Date".

    `validation.derive_anchor` exists for this and is used by the terminal
    validation loop, but its fallback splits the preceding LINE on its first
    colon. These filings put a whole key-terms table on one line
    ("... Valuation Date:: April 6, 2026 Maturity Date:: April 9, 2026 CUSIP::
    48136CYQ6"), so the first colon is several labels too early and the anchor
    comes back as a sentence. That function is left alone -- its own tests pin
    its behavior -- and this reads the label ADJACENT to the value instead:
    a canonical spec anchor if one sits just before, else the last "Label:"
    immediately preceding it.
    """
    if not span:
        return None
    pre = text[max(0, span[0] - window):span[0]]
    if field is not None:
        low = pre.lower()
        best = None
        for a in (getattr(field, "anchors", None) or []):
            key = a.lower().rstrip(": ").strip()
            at = low.rfind(key) if key else -1
            if at >= 0 and (best is None or at > best[0]):
                best = (at, a.rstrip(": ").strip())
        if best:
            return best[1]
    m = _LABEL_RE.search(pre)
    if m:
        return m.group(1).strip(" .,")

    # Prose states the term AFTER the number as often as before it -- "70.00%
    # of the Initial Stock Price, which we refer to as the Interest Barrier".
    # Looking only backwards misses those entirely.
    if field is not None:
        post = text[span[1]:span[1] + window].lower()
        best = None
        for a in (getattr(field, "anchors", None) or []):
            key = a.lower().rstrip(": ").strip()
            at = post.find(key) if key else -1
            if at >= 0 and (best is None or at < best[0]):
                best = (at, a.rstrip(": ").strip())
        if best:
            return best[1]

    # No label either side: say so. A sentence fragment dressed up as a label is
    # worse than an empty cell -- it reads as evidence that the value was found
    # somewhere structured when it was not.
    return None


def document_cues(text, spec, fields=None, max_per_cue=40):
    """Where to look: every place this filing says something that usually sits
    next to a term worth reading.

    Two sources, deliberately kept apart:

      * the spec's per-field `anchors` ("Interest Barrier:", "Coupon Rate:") --
        a labelled hit, so the field is known and the number after it is a
        candidate value;
      * generic unit phrases ("per annum", "% ", "per $1,000") -- unlabelled,
        but they are where numbers live, which is what you need when the
        issuer worded the label differently than the spec expects.

    Each hit carries the nearest number after it, so a cue is one click from a
    marked span rather than just a place to scroll to.
    """
    wanted = set(fields) if fields else None
    hits = []
    low = text.lower()

    def scan(needle, kind, field=None, cue_kind=None):
        start, found = 0, 0
        n = needle.lower()
        while found < max_per_cue:
            at = low.find(n, start)
            if at < 0:
                break
            tail = text[at + len(needle): at + len(needle) + 90]
            num = _NUM_RE.search(tail)
            hits.append({
                "span": [at, at + len(needle)],
                "cue": text[at:at + len(needle)],
                "kind": kind, "field": field, "cue_kind": cue_kind,
                "value_guess": num.group(0) if num else None,
                "value_span": ([at + len(needle) + num.start(),
                                at + len(needle) + num.end()] if num else None),
                "snippet": text[max(0, at - 70): at + len(needle) + 90].replace("\n", " "),
            })
            start = at + len(needle)
            found += 1

    for f in spec.fields:
        if wanted and f.name not in wanted:
            continue
        for a in (getattr(f, "anchors", None) or []):
            scan(a, "anchor", field=f.name)

    for phrase, cue_kind in UNIT_CUES:
        scan(phrase, "unit", cue_kind=cue_kind)

    hits.sort(key=lambda h: (h["span"][0], h["kind"] != "anchor"))
    return hits


def field_vocabulary_present(low_text, field):
    """Does this field's own wording appear in the filing at all?

    Structured notes are not one product. An autocallable has no buffer, a
    growth note has no coupon barrier, and a filing that never says "buffer"
    cannot state one. Asked for a number anyway, a model produces one -- so
    this is checked BEFORE any value is believed.

    Returns None for a field the spec gives no anchors for (nothing to test),
    which is different from False (tested, and the vocabulary is absent).
    """
    anchors = getattr(field, "anchors", None) or []
    if not anchors:
        return None
    return any(a.lower().rstrip(": ").strip() in low_text for a in anchors)


def anchor_positions(low_text, field):
    """Every offset where one of this field's anchors occurs -- the places a
    value for it could legitimately be stated."""
    out = []
    for a in (getattr(field, "anchors", None) or []):
        key = a.lower().rstrip(": ").strip()
        if not key:
            continue
        start = 0
        while True:
            at = low_text.find(key, start)
            if at < 0:
                break
            out.append(at + len(key))
            start = at + len(key)
    return sorted(out)


# Fields whose value is a figure: a number, a rate, a date. For these, a span is
# a FAILING gate rather than advisory (#163).
#
# The measurement behind this: on run #0018, across 25 Citigroup filings,
# estimated_value_per_1000 scored 2/6 correct WITH a located span and 0/19
# WITHOUT one. Not most of the span-less values were wrong -- all of them were.
# A located span is not sufficient (33-64% correct) but its absence was a
# perfect negative filter on that sample, and the ladder was already recording
# it: `span_unlocatable` named exactly the values the model had invented, and
# the runner stored them anyway.
#
# String and enum fields are deliberately excluded. issuer and cusip already
# score well and locate reliably, and gating them would cost coverage to catch
# nothing. Arrays are excluded here because they resolve per element instead
# (#167), which is a stricter check than this one.
VALUE_TYPED = frozenset(("number", "percent", "date"))


def withhold_unsupported_value(fdef, value, span, flag_codes):
    """Refuse to store a figure nothing in the filing points at (#163).

    Returns `(value, flag_or_None)`. A withheld value becomes None and the
    reason is recorded, so a reader can tell the field was attempted.

    The two failure modes stay distinct, because they are different facts:

      * `field_absent_from_document` -- the filing does not carry this field.
        Already flagged upstream; nothing is added here, and the value is
        withheld because there was nothing to read.
      * `value_withheld_no_span` -- the filing may well state it, but the model
        produced something no span supports. That is a model failure, not a
        document property, and conflating the two would lose the distinction
        the store needs to tell "this note type has no buffer" from "we could
        not read the buffer".

    This deliberately reduces apparent coverage. A field dropping from 84%
    present to 24% present is the correct reading of the same evidence, not a
    regression -- the other 60% were values nothing could check.
    """
    if fdef is None:
        return value, None
    if value in (None, "", []):
        return value, None
    # A string whose declared format fails (a CUSIP with a bad check digit) is
    # wrong by the spec's own statement, exactly like an out-of-bounds number.
    if "invalid_format" in flag_codes:
        return None, {
            "field": fdef.name, "code": "value_withheld_invalid_format",
            "severity": "warn",
            "message": ("withheld %r: fails this field's declared format (%s)"
                        % (str(value)[:80], getattr(fdef, "format", None))),
        }
    # A format-typed field is a literal code (a CUSIP): it must appear in the
    # filing as printed. Measured 2026-10-13: the model returned "424B21000" for
    # a BofA note -- built from the form type, flagged span_unlocatable, and it
    # happens to pass the CUSIP check digit (a 1-in-10 coincidence), so the
    # checksum alone cannot be the gate. Not found in the text means not stored.
    if getattr(fdef, "format", None) and ("span_unlocatable" in flag_codes
                                           or "element_span_unlocatable" in flag_codes):
        return None, {
            "field": fdef.name, "code": "value_withheld_not_in_document",
            "severity": "warn",
            "message": ("withheld %r: a %s is a literal code and does not appear "
                        "in the filing" % (str(value)[:80], fdef.format)),
        }
    if getattr(fdef, "type", None) not in VALUE_TYPED:
        return value, None

    # A value the spec's own bounds reject is not saved by having a span.
    # Measured on run #0019: with the bounds no longer stated in the prompt,
    # estimated_value_per_1000 came back 0 on 20 of 25 filings -- and 0 HAS a
    # span, because a zero digit appears all over a filing. So the span gate
    # passed it and only out_of_bounds objected, while the value was stored
    # anyway. The spec declares 900-1000; a 0 is wrong by the spec's own
    # statement, and storing a figure we already know is wrong is the same
    # mistake as storing one nothing points at.
    if "out_of_bounds" in flag_codes:
        return None, {
            "field": fdef.name, "code": "value_withheld_out_of_bounds",
            "severity": "warn",
            "message": ("withheld %r: outside the range this field declares, so "
                        "it is wrong by the spec's own statement. A span does "
                        "not rescue it -- a bare digit locates anywhere (#163)."
                        % (value if not isinstance(value, str) else value[:80])),
        }

    if span is not None:
        return value, None
    if "field_absent_from_document" in flag_codes:
        return None, None          # already explained, and correctly so
    return None, {
        "field": fdef.name, "code": "value_withheld_no_span", "severity": "warn",
        "message": ("withheld %r: no span in this filing supports it. Every "
                    "span-less value of this kind on the 25-filing sample was "
                    "wrong (#163), so it is not stored as a value."
                    % (value if not isinstance(value, str) else value[:80])),
    }


# Tokens that classify an underlying rather than name one (#167). The model
# appends these to the array -- ["GE Vernova Inc.", "single_stock"] -- so a
# consumer counting basket size sees two underlyings where the note has one, and
# grouping by underlying grows a phantom "single_stock" bucket.
#
# Matched against the WHOLE element, never as a substring: "Nasdaq-100 Index®"
# is a real index name and must survive, while a bare "index" is a kind.
CLASSIFICATION_TOKENS = frozenset((
    "single_stock", "single stock", "common stock", "common shares",
    "ordinary shares", "index", "indices", "equity index", "etf", "fund",
    "basket", "worst_of", "worst of", "adr", "ads", "stock", "share", "shares",
    "equity", "reference stock", "reference asset", "underlying",
    "underlying stock", "underlying asset", "n/a", "none",
))


def classify_array_members(values):
    """Split an array value into (names, kinds) (#167).

    A classification is not an underlying. If the kind is worth capturing it
    deserves its own field, which is what `underlying_type` is for -- it does
    not deserve a slot in the list of what the note references.
    """
    names, kinds = [], []
    for v in values or []:
        text = str(v).strip()
        if not text:
            continue
        (kinds if text.lower().strip(".") in CLASSIFICATION_TOKENS
         else names).append(text)
    return names, kinds


def locate_array_elements(text, values, near=None):
    """Resolve a span for EACH member of an array value (#167).

    Returns `(kept, dropped, spans)`. `kept` is the members the filing actually
    states, `dropped` is those it does not, and `spans` maps a kept member to
    where it was found.

    Why per element: span logic locates *the value*, and a JSON array is never a
    verbatim substring of a filing, so every array field came back
    `span_unlocatable` -- 25 of 25 for `underlyings`. That made the field
    invisible to every check built on span support, including the gate in #163.
    `["Zoetis Inc."]` is correct and `["GE Vernova Inc.", "single_stock"]` is
    contaminated, and nothing could tell them apart.
    """
    kept, dropped, spans = [], [], {}
    for member in values or []:
        name = str(member).strip()
        if not name:
            continue
        span, method = locate_value(text, name, None, near=near)
        if span:
            kept.append(member)
            spans[name] = {"span": list(span), "method": method}
        else:
            dropped.append(member)
    return kept, dropped, spans


def locate_value(text, value, hint_span=None, near=None):
    """Find where `value` is actually stated in `text`.

    The model's own span is not trustworthy -- measured against real filings it
    routinely returns an empty range or one thousands of characters wide, which
    is exactly what the #144 span gate exists to catch. A highlight drawn from
    that span would show the user confident-looking evidence for a value the
    model did not read there.

    So the span shown is EARNED: the value's own text is located in the
    document, preferring an occurrence near where the model claimed to look. A
    number is tried in the forms filings actually print it in (70, 70.0,
    70.00%, 1,000). Nothing found -> no highlight and the caller says so,
    rather than pointing at a sentence that does not contain the answer.

    Returns `(span, method)`; method is "model" | "value-match" | None.
    """
    if value is None or isinstance(value, (list, dict, bool)):
        return None, None


    # A model span is only believed when the text under it really says the value.
    raw = str(value).strip()
    if hint_span and 0 <= hint_span[0] < hint_span[1] <= len(text):
        under = text[hint_span[0]:hint_span[1]]
        if raw and raw.lower() in under.lower() and len(under) <= max(120, len(raw) * 6):
            return tuple(hint_span), "model"

    candidates = []
    if isinstance(value, (int, float)):
        n = float(value)
        whole = int(n) if n == int(n) else None
        for form in ({f"{whole:,}", str(whole), f"{whole}.00", f"{whole}.0",
                      f"{whole:,}.00"} if whole is not None else set()):
            candidates.append(form)
        candidates.extend({str(n), f"{n:.2f}", f"{n:,.2f}"})
    else:
        candidates.append(raw)
        # Values often carry the label the filing does not ("70.00% of initial").
        if len(raw) > 12:
            candidates.append(raw[:60])
    candidates = [c for c in dict.fromkeys(candidates) if c and len(c) >= 2]

    low = text.lower()
    best = None
    for cand in candidates:
        start = 0
        while True:
            at = low.find(cand.lower(), start)
            if at < 0:
                break
            span = (at, at + len(cand))
            # A match near one of the field's own labels is evidence; the same
            # digits elsewhere in a 60k-character filing are a coincidence. When
            # anchor positions are known they outrank the model's guess.
            if near:
                distance = min(abs(at - n) for n in near)
            elif hint_span:
                distance = abs(at - hint_span[0])
            else:
                distance = at
            # Nearest wins; on a tie the LONGER match wins, so a highlight lands
            # on the whole printed number ("70.00", "$979.00") rather than the
            # bare digits inside it.
            rank = (distance, -(span[1] - span[0]))
            if best is None or rank < best[0]:
                best = (rank, span)
            start = at + len(cand)
    return (best[1], "value-match") if best else (None, None)


def preview_document(params, run_id=None):
    """Dry run on ONE filing: what would this extract, and from WHERE?

    Same ladder as a real run, but nothing is written to the output store --
    the result is a list of proposals with spans in ORIGINAL document
    coordinates (`LadderExtractor` resolves them back through the reduction
    map), so the viewer can highlight the exact text each value came from.

    This is the check that belongs before a 450-filing sample: a field pulling
    from the wrong sentence is obvious when you can see the sentence, and
    invisible in a table of values.
    """
    import field_spec
    from extraction_ladder import ExtractionLadder
    from ollama_client import OllamaClient, OllamaConfig
    from reduce import split_sections
    from validation import RenderDocument, LadderExtractor

    doc = load_document(params.get("cik"), params.get("accession"),
                        params.get("document"))
    from edgar_client import EdgarClient
    from document_expand import expand_accession
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    client = EdgarClient(user_agent=ua, cache_dir=str(home / "cache"))
    bundle = expand_accession(client, params.get("cik"), params.get("accession"))
    primary = bundle.by_name(doc["document"]) or bundle.primary()
    nd = primary.normalized

    spec = field_spec.load_spec(SCRUBBER / "field_specs" / "424b2_structured_note.json")
    fields = params.get("fields") or DEFAULT_FIELDS
    fields = [f for f in fields if spec.field(f) is not None]

    # Hand the ladder the validation store as its exemplar provider: spans a
    # human marked in earlier filings are read back on rung 3 for this
    # (issuer, field), so a check reflects what the tool has been TAUGHT, not
    # just what the base model guesses. Without this the marking UI would be a
    # write-only diary.
    vstore = validation_store()
    ladder = ExtractionLadder(
        spec, local_client=OllamaClient(OllamaConfig.from_env({})),
        claude_client=None, claude_enabled=False, exemplars=vstore)
    extractor = LadderExtractor(spec, ladder, sections=split_sections(nd))
    render_doc = RenderDocument.from_normalized(nd)
    ex107 = bundle.ex107.as_dict() if bundle.ex107 else None

    text = nd.text
    low_text = text.lower()
    out = []
    t0 = time.time()
    # One field at a time even though propose() takes a list: a 9-field check is
    # half a minute of model time, and the caller gets each highlight as it
    # lands instead of a blank wait and then everything at once.
    for name in fields:
        if run_id and RUNS.stop_requested(run_id):
            RUNS.log(run_id, "check stopped -- fields already done are kept")
            break
        p = extractor.propose(render_doc, issuer=params.get("issuer"), ex107=ex107,
                              accession=params.get("accession"),
                              document=doc["document"], fields=[name])[0]
        # Does this field's own vocabulary appear in the filing AT ALL? Plenty
        # of structured notes carry no coupon barrier, no autocall, no buffer --
        # the honest answer there is "not in this note", and a model asked for
        # a number produces one anyway.
        fdef = spec.field(p.field)
        anchor_present = field_vocabulary_present(low_text, fdef)
        if anchor_present is False:
            # Nothing in this filing can be evidence for a field it never
            # mentions. Matching the digits elsewhere manufactures exactly the
            # false confidence this is meant to prevent -- the first cut
            # "located" buffer_pct 0.7 in a filing that never says "buffer".
            span, method = None, None
        else:
            span, method = locate_value(text, p.value, p.source_span,
                                        near=anchor_positions(low_text, fdef))
        # The label the value sits under ("Interest Barrier", "Coupon Rate") --
        # the same anchor #107 induces rules from. Shown next to the value
        # because a number with its label is checkable at a glance and a bare
        # number is not.
        anchor = label_before(text, span, fdef)
        out.append({
            "field": p.field, "value": p.value, "unit": p.unit,
            "rung": p.rung, "confidence": p.confidence,
            "provenance": p.provenance,
            "anchor": anchor,
            "anchor_present": anchor_present,
            "likely_absent": anchor_present is False,
            "span": list(span) if span else None,
            "span_source": method,
            "model_span": list(p.source_span) if p.source_span else None,
            "span_text": text[span[0]:span[1]] if span else None,
            # Context around the hit is what makes a highlight checkable: the
            # value alone reads as right far more often than it is.
            "context": (text[max(0, span[0] - 160):span[1] + 160] if span else None),
            "flags": [f.get("code") for f in (p.flags or [])],
        })
        if run_id:
            RUNS.update(run_id, proposals=list(out),
                        located=sum(1 for x in out if x["span"]))
            RUNS.log(run_id, f"  {name} = {str(p.value)[:60]} "
                             f"[{p.rung}{', span ' + method if method else ', no span'}]")
            RUNS.tick_field(run_id, params.get("accession"), name, time.time() - t0)

    return {"accession": params.get("accession"), "document": doc["document"],
            "issuer": params.get("issuer"), "proposals": out,
            "located": sum(1 for p in out if p["span"]), "total": len(out)}


def scan_availability(targets, fields, run_id=None):
    """Which fields does each of these filings actually CARRY?

    No model: this is a vocabulary check per field per filing (does any of the
    field's spec anchors occur in the text), so it costs one cached fetch per
    filing and runs in seconds rather than the ~40s/filing extraction takes.
    That ordering is the point -- you find out a query returns growth notes
    with no coupon terms BEFORE spending an hour extracting coupon fields from
    them.

    The per-field summary reports how universal a field is ACROSS the scanned
    filings. That is a measurement of this sample, not a statement about what
    any rule requires: "present in 100% of 20 filings" is evidence, not a legal
    determination, and it is labelled as such.
    """
    import field_spec
    spec = field_spec.load_spec(SCRUBBER / "field_specs" / "424b2_structured_note.json")
    names = [f for f in (fields or [f.name for f in spec.fields]) if spec.field(f)]

    rows, t0 = [], time.time()
    for meta in targets:
        if run_id and RUNS.stop_requested(run_id):
            RUNS.log(run_id, "scan stopped -- filings already scanned are kept")
            break
        try:
            # Deliberately NOT meta["document"]: a full-text hit names the file
            # the phrase matched in, which is routinely the 300-character
            # EX-FILING FEES exhibit rather than the prospectus. Scanning that
            # for note terms reports every field absent -- correctly, and
            # uselessly. Analysis always reads the primary document.
            doc = load_document(meta.get("cik"), meta.get("accession"))
        except Exception as exc:
            rows.append({"accession": meta.get("accession"), "issuer": meta.get("issuer"),
                         "filed": meta.get("file_date") or meta.get("sample_day"),
                         "error": f"{type(exc).__name__}: {exc}", "present": {}})
            if run_id:
                RUNS.tick_field(run_id, meta.get("accession"), "fetch", time.time() - t0)
            continue

        low = doc["text"].lower()
        present = {}
        for name in names:
            got = field_vocabulary_present(low, spec.field(name))
            present[name] = ("untestable" if got is None else
                             "present" if got else "absent")
        rows.append({
            "accession": doc["accession"], "document": doc["document"],
            "issuer": meta.get("issuer"),
            "filed": meta.get("file_date") or meta.get("sample_day"),
            "chars": doc["chars"], "present": present,
            "document_url": doc.get("document_url"),
        })
        if run_id:
            RUNS.event(run_id, "doc_done", accession=doc["accession"],
                       issuer=meta.get("issuer"), document=doc["document"],
                       fields=len(present),
                       kept=sum(1 for v in present.values() if v == "present"),
                       verdict=", ".join(
                           f"{n} absent" for n, v in present.items() if v == "absent")
                       or "every field's wording is present")
            RUNS.update(run_id, rows=list(rows))
            RUNS.tick_field(run_id, doc["accession"], "scan", time.time() - t0)

    scanned = [r for r in rows if not r.get("error")]
    summary = []
    for name in names:
        testable = [r for r in scanned if r["present"].get(name) != "untestable"]
        got = sum(1 for r in testable if r["present"][name] == "present")
        rate = (got / len(testable)) if testable else None
        summary.append({
            "field": name, "present": got, "testable": len(testable),
            "rate": round(rate, 3) if rate is not None else None,
            # Measured across THIS sample, not a claim about what is mandated.
            # A coin flip is not "most": 50% means the field tracks the product
            # type, which is the thing this table exists to reveal.
            "class": (None if rate is None else
                      "universal" if rate >= 0.95 else
                      "common" if rate >= 0.6 else
                      "product-specific"),
        })
    summary.sort(key=lambda s: (-(s["rate"] or 0), s["field"]))
    return {"rows": rows, "summary": summary, "fields": names,
            "scanned": len(scanned), "failed": len(rows) - len(scanned)}


def start_availability(params):
    """Background scan so the browser gets a progress bar and a stop."""
    targets, provenance = resolve_targets(params)
    if not targets:
        raise ValueError("nothing to scan -- the query returned no filings")
    fields = params.get("fields")
    run_id = RUNS.create("availability", {**provenance, "limit": len(targets),
                                           "fields": ["scan"]})
    RUNS.update(run_id, rows=[])
    # One tick per FILING here (the unit of work is a fetch, not a field).
    RUNS.update(run_id, progress={**RUNS.get(run_id)["progress"],
                                   "fields_total": len(targets),
                                   "total": len(targets)})

    def worker():
        try:
            RUNS.update(run_id, status="running")
            RUNS.log(run_id, f"scanning {len(targets)} filing(s) for field availability")
            result = scan_availability(targets, fields, run_id=run_id)
            RUNS.update(run_id,
                        status="stopped" if RUNS.stop_requested(run_id) else "done",
                        finished_at=utcnow(), rows=result["rows"],
                        summary_fields=result["summary"], scanned=result["scanned"])
            RUNS.log(run_id, f"scanned {result['scanned']} filing(s), "
                             f"{result['failed']} failed")
        except Exception as exc:
            RUNS.update(run_id, status="error", finished_at=utcnow(),
                        error=f"{type(exc).__name__}: {exc}")
            RUNS.log(run_id, traceback.format_exc().strip().splitlines()[-1])

    threading.Thread(target=worker, daemon=True, name=f"scan-{run_id}").start()
    return run_id


def claude_template(params, run_id=None):
    """What does this filing carry that we have no name for yet?

    The availability scan answers the same question against the fields we
    already named, and can only ever return a subset of them. This reads the
    filing once with Claude and comes back with the terms it can see, each tied
    to the sentence it came from -- which is what makes a template checkable
    rather than a suggestion.
    """
    import claude_template as ct
    import field_spec as fs

    doc = load_document(params.get("cik"), params.get("accession"),
                        params.get("document"))
    spec = fs.load_spec(SCRUBBER / "field_specs" / "424b2_structured_note.json")
    if run_id:
        RUNS.log(run_id, f"reading {doc['accession']} with {ct.model_name()}")
        RUNS.event(run_id, "doc_start", accession=doc["accession"],
                   issuer=params.get("issuer"),
                   detail=f"{doc['chars']:,} chars to {ct.model_name()}")
    t0 = time.time()
    template = ct.request_template(doc["text"], [f.name for f in spec.fields])
    template = ct.locate(template, doc["text"])
    if run_id:
        for entry in template["fields"]:
            RUNS.event(run_id, "field_done", accession=doc["accession"],
                       field=entry["name"], value=str(entry.get("value"))[:120],
                       span=entry.get("span"), kept=True, verdict="kept")
        for entry in template["unlocatable"]:
            RUNS.event(run_id, "field_done", accession=doc["accession"],
                       field=entry["name"], value=str(entry.get("value"))[:120],
                       kept=False, verdict="dropped",
                       reason="the quote it gave is not in the filing")
        RUNS.tick_field(run_id, doc["accession"], "template", time.time() - t0)

    known = {f.name for f in spec.fields}
    # The split the reader actually wants: which of these is a field we already
    # know how to ask for, and which is a term the spec has no name for.
    for entry in template["fields"]:
        entry["known"] = bool(entry.get("spec_field")) and entry["spec_field"] in known
    template.update({
        "accession": doc["accession"], "document": doc["document"],
        "issuer": params.get("issuer"), "document_url": doc.get("document_url"),
        "new_fields": sum(1 for e in template["fields"] if not e["known"]),
        "draft_spec": ct.as_field_spec(template),
    })
    return template


def start_claude_template(params):
    """Tracked like every other job, so the browser gets progress and a stop."""
    run_id = RUNS.create("claude-template",
                         {"limit": 1, "fields": ["template"],
                          "accession": params.get("accession"),
                          "document": params.get("document")})
    RUNS.update(run_id, template=None)

    def worker():
        try:
            RUNS.update(run_id, status="running")
            result = claude_template(params, run_id=run_id)
            RUNS.update(run_id, status="done", finished_at=utcnow(),
                        template=result)
            RUNS.log(run_id, f"{len(result['fields'])} field(s) located, "
                             f"{result['new_fields']} not in the spec, "
                             f"{len(result['unlocatable'])} dropped as unlocatable")
        except Exception as exc:
            RUNS.update(run_id, status="error", finished_at=utcnow(),
                        error=f"{type(exc).__name__}: {exc}")
            RUNS.log(run_id, traceback.format_exc().strip().splitlines()[-1])

    threading.Thread(target=worker, daemon=True).start()
    return run_id


# --------------------------------------------------------------------------- #
# Free Research
#
# Same shape as the scrubber above -- a POST starts a tracked run, the browser
# watches it through the shared registry, and the results land in a LOCAL store.
# A different store, though: Free Research keeps its own under
# ~/.free-research, so the two tools cannot overwrite each other's idea of what
# a filing said. Neither writes shared pipeline data.
# --------------------------------------------------------------------------- #

def _fr():
    """The Free Research modules, imported lazily.

    The dashboard must still start when the local model is not installed, so
    these imports happen at the point of use rather than at module load.

    The package addresses its siblings as `tools.<name>`, so the REPO ROOT has
    to be importable, not just `tools/`. Script mode puts this file's own
    directory on sys.path and not its parent, so put the parent there too --
    the same shim `tools/edgar_scrubber/conftest.py` applies for flat imports,
    pointed one level up.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools.free_research import chartdata, fill as fill_mod
    from tools.free_research import store as store_mod, template as template_mod
    from tools.free_research.sources import edgar as edgar_source
    return store_mod, template_mod, fill_mod, edgar_source, chartdata


def fr_edgar_client():
    """An EdgarClient for a Free Research pull, cached beside the scrubber's."""
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set. The SEC fair-access policy requires a "
            "contactable '<name> <email>' on every request; see SETUP.md."
        )
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    return EdgarClient(user_agent=ua, cache_dir=str(home / "cache"))


def fr_store():
    """The Free Research store, rooted by env the way the scrubber's home is."""
    store_mod = _fr()[0]
    home = os.environ.get("FREE_RESEARCH_HOME") or None
    return store_mod.Store(home=home)


def fr_templates():
    """Every template that validates, as the dashboard needs to show them."""
    template_mod = _fr()[1]
    return [{"id": t["id"], "title": t.get("title") or t["id"],
             "description": t.get("description"),
             "slots": [{"name": s["name"], "kind": s["kind"],
                        "columns": list(s["columns"])}
                       for s in template_mod.slots(t)]}
            for t in template_mod.list_templates()]


def fr_brief_view(brief_id):
    """A brief, its template, and the points for each of its chart slots.

    Assembled here rather than in the page: the chart values are read from the
    stored rows, and the browser should never be the thing that decides what a
    number on an axis is.
    """
    store_mod, template_mod, _, _, chartdata = _fr()
    store = fr_store()
    brief = store.read_brief(brief_id)
    rows = store.read_rows(brief["pull_id"])
    try:
        tpl = template_mod.load_by_id(brief["template_id"])
    except Exception:
        tpl = None
    charts = {}
    for name, value in (brief.get("slots") or {}).items():
        if isinstance(value, dict) and "x" in value and "y" in value:
            result = chartdata.points_for(value, rows)
            result["note"] = chartdata.summarize(result)
            charts[name] = result
    return {"brief": brief, "pull": store.read_pull(brief["pull_id"]),
            "template": tpl, "charts": charts, "row_count": len(rows)}


def start_fr_pull(params):
    """Background EDGAR pull into the Free Research store."""
    _, _, _, edgar_source, _ = _fr()
    forms = params.get("forms") or []
    ciks = params.get("ciks") or []
    tickers = params.get("tickers") or []
    if not ciks and not tickers:
        raise ValueError("a pull needs at least one CIK or ticker")

    run_id = RUNS.create("free-research-pull",
                         {"forms": forms, "ciks": ciks, "tickers": tickers,
                          "since": params.get("since"), "until": params.get("until"),
                          "limit": len(ciks) + len(tickers), "fields": ["pull"]})

    def worker():
        try:
            RUNS.update(run_id, status="running")
            RUNS.log(run_id, "pulling %d target(s) from EDGAR"
                             % (len(ciks) + len(tickers)))
            RUNS.event(run_id, "doc_start", detail="EDGAR submissions")
            client = fr_edgar_client()
            record = edgar_source.pull(
                fr_store(), client, forms=forms, since=params.get("since"),
                until=params.get("until"), ciks=ciks, tickers=tickers)
            RUNS.update(run_id, status="done", finished_at=utcnow(),
                        pull_id=record["id"], row_count=record.get("row_count", 0),
                        unresolved_tickers=record.get("unresolved_tickers") or [])
            RUNS.event(run_id, "field_done", field="pull", kept=True,
                       verdict="kept", value="%d row(s)" % record.get("row_count", 0))
            RUNS.log(run_id, "wrote %d row(s) to pull %s"
                             % (record.get("row_count", 0), record["id"]))
        except Exception as exc:
            RUNS.update(run_id, status="error", finished_at=utcnow(),
                        error="%s: %s" % (type(exc).__name__, exc))
            RUNS.log(run_id, traceback.format_exc().strip().splitlines()[-1])

    threading.Thread(target=worker, daemon=True, name="fr-pull-%s" % run_id).start()
    return run_id


def start_fr_brief(params):
    """Background fill of one template against one completed pull."""
    _, template_mod, fill_mod, _, _ = _fr()
    pull_id = params.get("pull_id")
    template_id = params.get("template_id") or "filing_brief"
    store = fr_store()
    # Checked BEFORE the run is created: a run that could only ever fail is
    # noise in the run list, and the caller deserves the error now, not in a
    # log line thirty seconds from now.
    if not pull_id or not store.has_pull(pull_id):
        raise LookupError("no such pull: %r" % pull_id)
    tpl = template_mod.load_by_id(template_id)

    run_id = RUNS.create("free-research-brief",
                         {"pull_id": pull_id, "template_id": template_id,
                          "limit": 1,
                          "fields": [s["name"] for s in template_mod.slots(tpl)]})

    def worker():
        try:
            RUNS.update(run_id, status="running")
            RUNS.log(run_id, "filling %d slot(s) of %s"
                             % (len(template_mod.slots(tpl)), template_id))
            client = fill_mod.local_client()
            brief = fill_mod.fill(store, client, pull_id, tpl)
            # Every slot is an event, kept or dropped. A slot that produced a
            # value and then lost it for being uncheckable is a fact that
            # exists nowhere else -- the stored brief only keeps survivors.
            for name in (brief.get("slots") or {}):
                RUNS.event(run_id, "field_done", field=name, kept=True,
                           verdict="kept")
            for d in (brief.get("dropped") or []):
                RUNS.event(run_id, "field_done", field=d["slot"], kept=False,
                           verdict="dropped", reason=d["reason"])
            RUNS.update(run_id,
                        status="stopped" if RUNS.stop_requested(run_id) else "done",
                        finished_at=utcnow(), brief_id=brief["id"],
                        dropped=brief.get("dropped") or [])
            kept = len(brief.get("slots") or {})
            RUNS.log(run_id, "brief %s: %d slot(s) kept, %d dropped"
                             % (brief["id"], kept, len(brief.get("dropped") or [])))
        except Exception as exc:
            RUNS.update(run_id, status="error", finished_at=utcnow(),
                        error="%s: %s" % (type(exc).__name__, exc))
            RUNS.log(run_id, traceback.format_exc().strip().splitlines()[-1])

    threading.Thread(target=worker, daemon=True, name="fr-brief-%s" % run_id).start()
    return run_id


# --------------------------------------------------------------------------- #
# Research assistant: proposal batches (issue #285)
#
# The assistant (research_assistant.py, #280) is a PLANNER -- it turns a question
# into proposed EDGAR full-text searches and never runs them. This is the gate
# between a proposal and a run: nothing the model suggests touches the SEC until
# the user approves THIS batch by its id. The server holds each batch, lets the
# user edit it while it is still only a proposal, and on approval runs every
# stored search through the same `edgar_search` the search box uses -- full-text
# search only, `max_results` hard-capped at 20 so an approval can never fan out
# into an unbounded crawl.
#
# In-memory like the run registry: a proposal is a pending decision, not a
# record. The caps (5 searches a batch, 20 batches a chat, one batch running at a
# time) bound how much a single approval -- or a runaway planner loop -- can
# spend.
# --------------------------------------------------------------------------- #

# The hard ceiling on hits an approved search may fetch. 20 is enough to judge
# whether a query is on target without turning one approval into a bulk pull;
# `edgar_search` would otherwise default to 25 and allow up to 100.
RESEARCH_MAX_RESULTS = 20


def research_home():
    """`~/.edgar-scrubber/research/` (honouring EDGAR_SCRUBBER_HOME), the one
    place the assistant's chats, batches and audit log live."""
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    return home / "research"


def _atomic_write_json(path, obj):
    """Write JSON through a temp file and rename, so a crash mid-write never
    leaves a half-written record that a restart would choke on."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)


class ResearchBatchStore:
    """Proposed search batches and their chats, kept so a restart loses nothing.

    Thread-safe. A batch is `{batch_id, chat_id, provider, searches, status}`
    where `status` walks "proposed" -> "running" -> "ran" (and back to
    "proposed" if a run errors, so a transient failure is retryable). Only a
    "proposed" batch may be edited or approved; everything else is a 409.

    A chat holds the running `history` fed back to the planner -- metadata-only
    summaries of past hits (form, filer, CIK, filed date, accession), never any
    filing text -- plus the cost `estimates` the user has seen, each of which is
    the gate on a paid Claude plan: no estimate, no paid call.

    With `root` set every batch and chat is written under it and reloaded on
    construction, and every estimate/plan/edit/approval appends one line to
    `audit.jsonl`; with `root` None the store is pure in-memory (the
    proposal-batch tests use that to count without touching disk)."""

    MAX_SEARCHES = 5
    MAX_BATCHES_PER_CHAT = 20

    def __init__(self, root=None):
        self._lock = threading.Lock()
        self._batches = {}
        self._chats = {}
        self._seq = 0
        # One batch may run at a time per server: an approval spends SEC
        # requests, and two approvals racing would blow the rate budget. A
        # non-blocking acquire means a second approve is refused (409), not
        # queued behind the first.
        self._running = threading.Lock()
        self._root = Path(root) if root is not None else None
        if self._root is not None:
            self._batches_dir = self._root / "batches"
            self._chats_path = self._root / "chats.json"
            self._audit_path = self._root / "audit.jsonl"
            self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self):
        """Reload batches and chats written by an earlier process."""
        self._batches_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(self._batches_dir.glob("batch-*.json")):
            try:
                b = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            bid = b.get("batch_id")
            if not bid:
                continue
            # A batch caught mid-run by a restart never finished and never took
            # the run lock in this process: return it to "proposed" so it can be
            # approved again rather than sit wedged as "running" forever.
            if b.get("status") == "running":
                b["status"] = "proposed"
            # Likewise an extraction caught mid-start by a restart: its run began
            # in a process that is gone, so return the proposal to "proposed"
            # rather than leave it wedged as "starting" and un-approvable.
            ex = b.get("extract")
            if isinstance(ex, dict) and ex.get("status") == "starting":
                ex["status"] = "proposed"
                ex["run_id"] = None
            self._batches[bid] = b
            m = re.match(r"batch-(\d+)$", bid)
            if m:
                self._seq = max(self._seq, int(m.group(1)))
        try:
            chats = json.loads(self._chats_path.read_text(encoding="utf-8"))
            if isinstance(chats, dict):
                self._chats = chats
        except (OSError, json.JSONDecodeError):
            pass

    def _persist_batch(self, batch_id):
        if self._root is None:
            return
        b = self._batches.get(batch_id)
        if b is not None:
            _atomic_write_json(self._batches_dir / ("%s.json" % batch_id), b)

    def _persist_chats(self):
        if self._root is None:
            return
        _atomic_write_json(self._chats_path, dict(self._chats))

    def _audit(self, event, chat_id, batch_id, provider, searches):
        """Append exactly one line recording a decision: estimate, plan, edit or
        approval, with the searches as they stood at that moment."""
        if self._root is None:
            return
        rec = {"ts": utcnow(), "event": event, "chat_id": chat_id,
               "batch_id": batch_id, "provider": provider,
               "searches": list(searches or [])}
        with open(self._audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _audit_extract(self, chat_id, batch_id, provider, extract, run_id):
        """Append one line recording an extraction approval: which form, which
        accessions, and the run it started. Extraction spends GPU time on the
        spec path, so it belongs on the same audit trail as a paid search."""
        if self._root is None:
            return
        rec = {"ts": utcnow(), "event": "extract-approve", "chat_id": chat_id,
               "batch_id": batch_id, "provider": provider,
               "form": (extract or {}).get("form"),
               "accessions": list((extract or {}).get("accessions") or []),
               "run_id": run_id}
        with open(self._audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _copy(self, batch_id):
        b = self._batches.get(batch_id)
        return json.loads(json.dumps(b)) if b else None

    def _new_chat(self, chat_id):
        return {"chat_id": chat_id, "history": [], "estimates": {}}

    # -- chats: history and cost estimates ------------------------------------

    def history(self, chat_id):
        """The running transcript for `chat_id` (metadata only), for the next
        plan/estimate call. Empty for a chat with no turns yet."""
        with self._lock:
            c = self._chats.get(chat_id)
            return json.loads(json.dumps(c["history"])) if c else []

    def append_history(self, chat_id, messages):
        with self._lock:
            c = self._chats.setdefault(chat_id, self._new_chat(chat_id))
            c["history"].extend(messages)
            self._persist_chats()

    def record_estimate(self, chat_id, provider, estimate):
        """Record a cost estimate the user has now seen, returning its id. A paid
        Claude plan must quote one of these, so a cost is shown before a spend."""
        with self._lock:
            c = self._chats.setdefault(chat_id, self._new_chat(chat_id))
            estimate_id = "est-" + uuid.uuid4().hex[:12]
            c["estimates"][estimate_id] = {
                "estimate_id": estimate_id, "provider": provider,
                "created_at": utcnow(),
                **{k: estimate.get(k) for k in
                   ("model", "input_tokens", "output_tokens", "dollars")}}
            self._persist_chats()
            self._audit("estimate", chat_id, None, provider, [])
            return estimate_id

    def has_estimate(self, chat_id, estimate_id):
        with self._lock:
            c = self._chats.get(chat_id)
            return bool(c and estimate_id in c["estimates"])

    def consume_estimate(self, chat_id, estimate_id):
        """Spend a recorded estimate so it cannot gate a second paid call."""
        with self._lock:
            c = self._chats.get(chat_id)
            if c and estimate_id in c["estimates"]:
                del c["estimates"][estimate_id]
                self._persist_chats()

    # -- batches --------------------------------------------------------------

    def create(self, chat_id, provider, searches, reply="", dropped=None,
               extract=None):
        """Store a proposed batch. Raises ValueError (surfaced as 400) past the
        per-chat cap, so a runaway planner cannot pile up batches forever.

        `extract` is the optional follow-up extraction proposal (already
        augmented with its extractor kind and estimate); it rides on the same
        batch as the searches but has its own approval and its own status."""
        with self._lock:
            n = sum(1 for b in self._batches.values() if b["chat_id"] == chat_id)
            if n >= self.MAX_BATCHES_PER_CHAT:
                raise ValueError(
                    "chat %r already holds %d batches (the limit)"
                    % (chat_id, self.MAX_BATCHES_PER_CHAT))
            self._seq += 1
            batch_id = "batch-%04d" % self._seq
            self._batches[batch_id] = {
                "batch_id": batch_id, "chat_id": chat_id, "provider": provider,
                "searches": list(searches), "status": "proposed",
                "reply": reply or "", "dropped": list(dropped or []),
                "hits": None, "created_at": utcnow(), "ran_at": None,
                "extract": extract,
            }
            self._persist_batch(batch_id)
            self._audit("plan", chat_id, batch_id, provider, searches)
            return self._copy(batch_id)

    def get(self, batch_id):
        with self._lock:
            return self._copy(batch_id)

    def replace_searches(self, batch_id, searches):
        """Replace a PROPOSED batch's searches with the user's edited list.

        Returns (batch, None) on success; (None, 404) for an unknown id; (None,
        409) once the batch has left "proposed" -- the searches that ran are the
        record of what ran, and editing them after the fact would rewrite it."""
        with self._lock:
            b = self._batches.get(batch_id)
            if b is None:
                return None, 404
            if b["status"] != "proposed":
                return None, 409
            b["searches"] = list(searches)
            self._persist_batch(batch_id)
            self._audit("edit", b["chat_id"], batch_id, b.get("provider"), searches)
            return self._copy(batch_id), None

    def begin_approve(self, batch_id):
        """Claim a proposed batch for running. Returns (searches, None) on
        success, having set the batch "running" and taken the server-wide run
        lock; else (None, 404) / (None, 409). The caller MUST pair every success
        with exactly one `finish_approve` or `abort_approve` to release it."""
        if not self._running.acquire(blocking=False):
            return None, 409          # another batch is mid-run on this server
        try:
            with self._lock:
                b = self._batches.get(batch_id)
                if b is None:
                    self._running.release()
                    return None, 404
                if b["status"] != "proposed":
                    self._running.release()
                    return None, 409
                b["status"] = "running"
                self._persist_batch(batch_id)
                return list(b["searches"]), None
        except BaseException:
            self._running.release()
            raise

    def finish_approve(self, batch_id, hits):
        """Record the hits and mark the batch "ran", audit the approval, then
        release the run lock."""
        try:
            with self._lock:
                b = self._batches.get(batch_id)
                if b is not None:
                    b["hits"] = hits
                    b["status"] = "ran"
                    b["ran_at"] = utcnow()
                    self._persist_batch(batch_id)
                    self._audit("approve", b["chat_id"], batch_id,
                                b.get("provider"), b["searches"])
                return self._copy(batch_id)
        finally:
            self._running.release()

    def abort_approve(self, batch_id):
        """A run failed before it finished: return the batch to "proposed" so it
        can be approved again, and release the run lock."""
        try:
            with self._lock:
                b = self._batches.get(batch_id)
                if b is not None and b["status"] == "running":
                    b["status"] = "proposed"
                    self._persist_batch(batch_id)
        finally:
            self._running.release()

    # -- extraction follow-ups ------------------------------------------------

    def result_filings(self, chat_id):
        """Every filing this chat's RAN batches surfaced, keyed by accession ->
        {accession, cik, issuer, form, file_date}.

        Two jobs in one place: it is the set an extract proposal's accessions
        are validated against (a filing the user never saw cannot be scrubbed),
        and it is where the CIK each approved accession needs to start a run
        comes from -- the proposal itself carries only the accession string."""
        with self._lock:
            out = {}
            for b in self._batches.values():
                if b.get("chat_id") != chat_id:
                    continue
                for grp in (b.get("hits") or []):
                    for h in ((grp.get("result") or {}).get("hits") or []):
                        acc = h.get("accession")
                        if acc and acc not in out:
                            out[acc] = {k: h.get(k) for k in
                                        ("accession", "cik", "issuer", "form",
                                         "file_date")}
            return json.loads(json.dumps(out))

    def begin_extract(self, batch_id):
        """Claim a batch's extract proposal for running, flipping it to
        "starting". Returns (extract, chat_id, provider, None) on success; else
        (None, None, None, 404) for an unknown batch or one with no extract
        proposal, or (None, None, None, 409) once it has already been started.

        Pair every success with exactly one finish_extract or abort_extract."""
        with self._lock:
            b = self._batches.get(batch_id)
            if b is None:
                return None, None, None, 404
            ex = b.get("extract")
            if not isinstance(ex, dict):
                return None, None, None, 404
            if ex.get("status") != "proposed":
                return None, None, None, 409
            ex["status"] = "starting"
            self._persist_batch(batch_id)
            return (json.loads(json.dumps(ex)), b.get("chat_id"),
                    b.get("provider"), None)

    def finish_extract(self, batch_id, run_id):
        """Record the started run on the extract proposal and audit it."""
        with self._lock:
            b = self._batches.get(batch_id)
            if b is not None and isinstance(b.get("extract"), dict):
                b["extract"]["status"] = "started"
                b["extract"]["run_id"] = run_id
                self._persist_batch(batch_id)
                self._audit_extract(b.get("chat_id"), batch_id,
                                    b.get("provider"), b["extract"], run_id)
            return self._copy(batch_id)

    def abort_extract(self, batch_id):
        """A start failed before the run began: return the proposal to
        "proposed" so it can be approved again."""
        with self._lock:
            b = self._batches.get(batch_id)
            if b is not None and isinstance(b.get("extract"), dict) \
                    and b["extract"].get("status") == "starting":
                b["extract"]["status"] = "proposed"
                b["extract"]["run_id"] = None
                self._persist_batch(batch_id)


RESEARCH = ResearchBatchStore(research_home())


def build_research_provider(name):
    """A research_assistant Provider from a short name ("ollama" / "claude").

    A seam, not just a dispatch: the tests replace this wholesale with a fake
    provider so `/plan` is exercisable with no model, no key, and no network."""
    import research_assistant as ra
    key = (name or "").strip().lower()
    if key in ("", "ollama", "local"):
        from ollama_client import OllamaClient, OllamaConfig
        return ra.OllamaProvider(OllamaClient(OllamaConfig.from_env({})))
    if key == "claude":
        return ra.ClaudeProvider()
    raise ValueError("unknown research provider: %r" % name)


def _require_chat_and_message(params):
    """(chat_id, message), both stripped and present, or ValueError (400)."""
    chat_id = (params.get("chat_id") or "").strip()
    message = params.get("message")
    if not chat_id:
        raise ValueError("chat_id is required")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required")
    return chat_id, message


def _is_paid_provider(name):
    """True for a provider that bills per call -- Claude today. Keyed on the
    NAME, not an instance, so the gate never has to build (let alone call) the
    provider to decide a paid plan needs an estimate first."""
    return (name or "").strip().lower() == "claude"


def summarize_hits_for_history(results):
    """One metadata-only line per hit -- form, filer, CIK, filed date, accession
    -- as a single `user` turn to fold back into the chat history. Deliberately
    NOT the filing text: the planner proposes queries from what was found, and
    the document bodies are never fed to the model."""
    lines = []
    for item in results or []:
        for h in (item.get("result", {}) or {}).get("hits", []) or []:
            lines.append(
                "- %s | %s | CIK %s | filed %s | %s" % (
                    h.get("form") or "?", h.get("issuer") or "?",
                    h.get("cik") or "?", h.get("file_date") or "?",
                    h.get("accession") or "?"))
    if not lines:
        return {"role": "user",
                "content": "EDGAR results (metadata only): no hits."}
    return {"role": "user",
            "content": "EDGAR results (metadata only), one line per hit:\n"
                       + "\n".join(lines)}


def research_providers():
    """GET /providers: each research provider the chat can pick, with whether it
    is usable right now and -- when it is not -- a human reason. Ollama is a local
    model and always offered; Claude bills per call and is gated on
    claude_available() (no key, or no SDK, makes it unavailable with a reason).
    The picker uses this to disable Claude and show why, before any call."""
    import research_assistant as ra
    ok, reason = ra.claude_available()
    return {"providers": [
        {"name": "ollama", "label": "Ollama", "available": True, "reason": ""},
        {"name": "claude", "label": "Claude", "available": bool(ok),
         "reason": "" if ok else reason},
    ]}


def research_estimate(params):
    """POST /estimate: what the NEXT plan() for this chat would cost, plus an
    `estimate_id` the user's paid plan must quote. No provider CALL -- only the
    text that would be sent is measured -- and no SEC request."""
    import research_assistant as ra
    chat_id, message = _require_chat_and_message(params)
    name = params.get("provider")
    provider = build_research_provider(name)
    history = RESEARCH.history(chat_id)
    estimate = ra.estimate_cost(provider, message, history=history)
    estimate_id = RESEARCH.record_estimate(chat_id, name, estimate)
    return {**estimate, "estimate_id": estimate_id}


# The spec ladder spends roughly this much local model time per field on an
# average filing -- the figure the "Start extraction" card quotes so a user is
# not surprised by a multi-hour run. Taxonomy and XML forms read the filing's
# own structure and spend no model time, so only `spec` carries an estimate.
EXTRACT_SECONDS_PER_FIELD = 30


def describe_extract(extract):
    """Augment a validated extract proposal with the extractor that would run it
    and, for a `spec` form, the model-time estimate the card shows.

    `kind` is the owning scrubber's field_source (`spec`, `taxonomy` or `xml`),
    or None when no scrubber owns the form -- the proposal is still stored (the
    card can say so), but its approval will 400 on the run's own refusal. The
    estimate is accessions x fields x 30s, matching the issue's rule of thumb and
    `start_scrubber_run`'s default field list."""
    out = dict(extract)
    scrubber = scrubber_for_forms([extract.get("form")])
    kind = getattr(scrubber, "field_source", None) if scrubber else None
    out["kind"] = kind
    out["status"] = "proposed"
    out["run_id"] = None
    n = len(extract.get("accessions") or [])
    if kind == "spec":
        fields = len(DEFAULT_FIELDS)
        out["fields"] = fields
        out["estimate_seconds"] = n * fields * EXTRACT_SECONDS_PER_FIELD
    return out


def research_plan(params):
    """POST /plan: (status, body). Ask the chosen provider for a batch and STORE
    it, proposed. This spends a model call and nothing else -- the searches it
    proposes do not touch the SEC until the batch is approved by id.

    A paid provider (Claude) is GATED: without an `estimate_id` this chat has
    already seen, the call is refused 409 before any provider is built or asked,
    so no money is ever spent on a cost the user has not been shown. Ollama is
    free and needs no estimate. Raises ValueError (400) for a missing field or
    past the per-chat cap."""
    import research_assistant as ra
    chat_id, message = _require_chat_and_message(params)
    name = params.get("provider")
    if _is_paid_provider(name):
        estimate_id = params.get("estimate_id")
        if not estimate_id or not RESEARCH.has_estimate(chat_id, estimate_id):
            return 409, {"error": "a paid Claude plan needs an estimate_id this "
                                  "chat has seen; POST /estimate first"}
    # The history fed to the planner is the chat's running transcript -- past
    # questions, replies and metadata-only hit summaries -- never filing text.
    history = RESEARCH.history(chat_id)
    provider = build_research_provider(name)
    # The accessions this chat has already surfaced: an extract proposal may name
    # only these, so a filing the user never saw is dropped, not scrubbed.
    known = RESEARCH.result_filings(chat_id)
    result = ra.plan(provider, message, history=history, known_accessions=set(known))
    extract = describe_extract(result.extract.to_dict()) if result.extract else None
    batch = RESEARCH.create(chat_id, name, [s.to_dict() for s in result.searches],
                            reply=result.reply, dropped=result.dropped,
                            extract=extract)
    RESEARCH.append_history(chat_id, [
        {"role": "user", "content": message},
        {"role": "assistant", "content": result.reply or ""},
    ])
    if _is_paid_provider(name):
        RESEARCH.consume_estimate(chat_id, params.get("estimate_id"))
    return 200, batch


def research_edit(batch_id, params):
    """PUT /batch/{id}: (status, body). Re-validates the edited searches with
    the planner's own `validate_searches`; a single bad entry (an invalid form
    code, or a 6th search past the cap) rejects the WHOLE edit as 400 rather
    than quietly keeping the good ones -- the user approves what they see."""
    import research_assistant as ra
    kept, dropped = ra.validate_searches(params.get("searches"))
    if dropped:
        return 400, {"error": "edit rejected: every search must be valid",
                     "dropped": dropped}
    batch, err = RESEARCH.replace_searches(batch_id, [s.to_dict() for s in kept])
    if err == 404:
        return 404, {"error": "no such batch: %r" % batch_id}
    if err == 409:
        return 409, {"error": "this batch is no longer a proposal and cannot be edited"}
    return 200, batch


def approve_research_batch(batch_id):
    """POST /batch/{id}/approve: (status, body). Runs every stored search through
    `edgar_search` -- full-text search only, `max_results` capped at 20 -- and
    stores the hits. A second approve (the batch already ran, or another batch is
    running) is a 409, never a re-run."""
    searches, err = RESEARCH.begin_approve(batch_id)
    if err == 404:
        return 404, {"error": "no such batch: %r" % batch_id}
    if err == 409:
        return 409, {"error": "batch cannot be approved: it already ran, or "
                              "another batch is running on this server"}
    try:
        results = []
        for s in searches:
            # Only the efts fields, and max_results pinned to the 20-hit cap: an
            # approval runs the searches the user saw, at a bounded size, never a
            # larger query the stored batch could carry.
            p = {k: s.get(k) for k in ("q", "forms", "ciks", "startdt", "enddt")}
            p["max_results"] = RESEARCH_MAX_RESULTS
            results.append({"query": s, "result": edgar_search(p)})
    except Exception:
        RESEARCH.abort_approve(batch_id)
        raise
    batch = RESEARCH.finish_approve(batch_id, results)
    # Fold a metadata-only summary of what was found back into the chat, so the
    # next plan() reasons from the results without ever seeing filing text.
    if batch is not None:
        RESEARCH.append_history(batch["chat_id"],
                                [summarize_hits_for_history(results)])
    return 200, batch


def approve_research_extract(batch_id):
    """POST /extract/{id}/approve: (status, body). Start a scrubber run over the
    exact accessions the batch's extract proposal named, through the same
    `start_scrubber_run` the search UI uses.

    A form no scrubber owns is refused with the run's own 400; an already-started
    (or absent) proposal is a 404/409, never a second run. On success the body
    carries the run_id so the browser can follow it in the EDGAR Scrubber tab."""
    extract, chat_id, provider, err = RESEARCH.begin_extract(batch_id)
    if err == 404:
        return 404, {"error": "no such batch, or it carries no extract "
                              "proposal: %r" % batch_id}
    if err == 409:
        return 409, {"error": "this extract proposal has already been started"}

    # Resolve each accession back to the filing it came from: the run needs the
    # CIK, which the proposal (accession-only) does not carry. Validation already
    # guaranteed every accession appeared in this chat's results.
    filings = RESEARCH.result_filings(chat_id)
    targets = []
    for acc in extract.get("accessions") or []:
        f = filings.get(acc)
        if f and f.get("cik"):
            targets.append({"accession": acc, "cik": f.get("cik"),
                            "issuer": f.get("issuer"),
                            "file_date": f.get("file_date")})
    if not targets:
        RESEARCH.abort_extract(batch_id)
        return 400, {"error": "none of the proposed accessions resolve to a "
                              "filing with a CIK in this chat's results"}

    try:
        run_id = start_scrubber_run({"accessions": targets,
                                     "forms": [extract.get("form")],
                                     "limit": len(targets)})
    except Exception as exc:
        # A form no scrubber owns raises here (the run's own 400), before any
        # SEC request -- return the proposal to "proposed" so it is not wedged.
        RESEARCH.abort_extract(batch_id)
        return 400, {"error": f"{type(exc).__name__}: {exc}"}

    batch = RESEARCH.finish_extract(batch_id, run_id)
    return 200, {"run_id": run_id, "batch": batch}


def start_preview(params):
    """Run a check in the background so the browser can watch it.

    Registered in the same registry as an extraction run, so it gets the same
    progress reporting and the same cooperative stop for free -- a check is a
    smaller run, not a different kind of thing.
    """
    fields = params.get("fields") or DEFAULT_FIELDS
    run_id = RUNS.create("preview", {"limit": 1, "fields": fields,
                                      "accession": params.get("accession"),
                                      "document": params.get("document")})
    RUNS.update(run_id, proposals=[], located=0)

    def worker():
        try:
            RUNS.update(run_id, status="running")
            RUNS.log(run_id, f"checking {params.get('accession')}")
            result = preview_document(params, run_id=run_id)
            RUNS.update(run_id,
                        status="stopped" if RUNS.stop_requested(run_id) else "done",
                        finished_at=utcnow(), proposals=result["proposals"],
                        located=result["located"])
            RUNS.log(run_id, f"{result['located']} of {len(result['proposals'])} "
                             f"value(s) located in the text")
        except Exception as exc:
            RUNS.update(run_id, status="error", finished_at=utcnow(),
                        error=f"{type(exc).__name__}: {exc}")
            RUNS.log(run_id, traceback.format_exc().strip().splitlines()[-1])

    threading.Thread(target=worker, daemon=True, name=f"preview-{run_id}").start()
    return run_id


def validation_store(readonly=False):
    from validation import ValidationStore
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    return ValidationStore(home / "store" / "validation.sqlite", readonly=readonly)


def save_annotation(params):
    """One hand-marked span: the human says "THIS text is this field".

    It lands as a CORRECT verdict plus a `corrected` exemplar, which is what the
    ladder already reads on rung 3 (`extraction_ladder._lookup_exemplars`) --
    so marking spans by hand is not a side note, it is how the local model gets
    taught the boundaries it keeps missing (#105/#106). Same (session,
    accession, document, field) marked again replaces the earlier span rather
    than accumulating contradictory ground truth.
    """
    from validation import FieldVerdict, render_exemplar

    # "This filing has no coupon barrier" is an answer, not a missing answer.
    # It rules the field absent for this document and writes a NEGATIVE
    # exemplar, which teaches the ladder that null is valid here and suppresses
    # the number a model would otherwise invent to fill the slot.
    if params.get("absent"):
        for required in ("accession", "document", "field"):
            if not params.get(required):
                raise ValueError(f"'{required}' is required")
        session = params.get("session") or "dashboard"
        field = params["field"]
        rendered = render_exemplar("negative", field=field)
        store = validation_store()
        try:
            store.ensure_session(session, spec_id="424b2.structured_note", now=utcnow())
            store.write_verdict(session, params["accession"], params["document"],
                                FieldVerdict.reject(field, note=params.get("note")),
                                now=utcnow())
            held_out = store.is_held_out(session, params["accession"], params["document"])
            taught = bool(params.get("issuer")) and not held_out
            if taught:
                store.write_exemplar(params.get("issuer"), field, "negative", rendered,
                                     accession=params["accession"],
                                     document=params["document"],
                                     form="424b2.structured_note", now=utcnow())
        finally:
            store.close()
        return {"saved": True, "field": field, "value": None, "span": None,
                "session": session, "verdict": "reject", "exemplar": taught,
                "held_out": held_out, "rendered": rendered, "absent": True}

    for required in ("accession", "document", "field", "span"):
        if not params.get(required):
            raise ValueError(f"'{required}' is required")
    span = params["span"]
    if (not isinstance(span, (list, tuple)) or len(span) != 2
            or not all(isinstance(x, int) for x in span) or span[0] >= span[1]):
        raise ValueError("'span' must be [start, end] character offsets with start < end")

    session = params.get("session") or "dashboard"
    field = params["field"]
    value = params.get("value")
    if value is None:
        value = params.get("span_text")

    # Accepting the model's own proposal and marking a span by hand are
    # different training signals: a `corrected` exemplar teaches a boundary the
    # model got WRONG and outranks plain positives in the rung-3 prompt
    # (`exemplars_for`), so an accept must not be filed as a correction.
    accepted = bool(params.get("accepted"))
    verdict = (FieldVerdict.accept(type("P", (), {"field": field, "value": value,
                                                   "source_span": tuple(span)})())
               if accepted else
               FieldVerdict.correct(field, value, source_span=tuple(span),
                                    note=params.get("note")))

    kind = "positive" if accepted else "corrected"
    span_text = params.get("span_text")
    # The prompt line has to be the shape the ladder renders elsewhere
    # (`render_exemplar`), not raw span text -- it carries the value verbatim
    # next to the snippet so the model can copy the confirmed boundary.
    rendered = render_exemplar(kind, field=field, value=value,
                               anchor=params.get("anchor"), span_text=span_text)

    store = validation_store()
    try:
        store.ensure_session(session, spec_id="424b2.structured_note", now=utcnow())
        store.write_verdict(session, params["accession"], params["document"],
                            verdict, now=utcnow())
        # A document reserved for the eval set must never teach (#108) --
        # exemplars from it would leak the answers into the prompt that is
        # later scored against them.
        held_out = store.is_held_out(session, params["accession"], params["document"])
        taught = bool(params.get("issuer")) and not held_out
        if taught:
            store.write_exemplar(params.get("issuer"), field, kind, rendered,
                                 value=value, anchor=params.get("anchor"),
                                 span_text=span_text,
                                 accession=params["accession"],
                                 document=params["document"],
                                 form="424b2.structured_note", now=utcnow())
            examples = len(store.exemplars_for(params.get("issuer"), field))
        else:
            examples = 0
    finally:
        store.close()
    return {"saved": True, "field": field, "value": value, "span": list(span),
            "session": session, "verdict": verdict.verdict,
            "exemplar": taught, "held_out": held_out, "rendered": rendered,
            "examples_for_field": examples}


def delete_annotation(params):
    for required in ("accession", "document", "field"):
        if not params.get(required):
            raise ValueError(f"'{required}' is required")
    store = validation_store()
    try:
        removed = store.clear_verdict(params.get("session") or "dashboard",
                                       params["accession"], params["document"],
                                       params["field"])
    finally:
        store.close()
    return {"removed": removed, "field": params["field"]}


def list_annotations(accession, document, session="dashboard"):
    store = validation_store()
    try:
        verdicts = store.verdicts_for(session, accession, document)
    finally:
        store.close()
    return [{"field": v.field, "verdict": v.verdict, "value": v.value,
             "span": list(v.source_span) if v.source_span else None,
             "anchor": v.anchor, "note": v.note}
            for v in verdicts]


# --------------------------------------------------------------------------- #
# Store browsing (past runs, no model needed)
# --------------------------------------------------------------------------- #

def store_path():
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    return home / "store" / "extractions.sqlite"


def open_store_readonly():
    from output_store import OutputStore
    p = store_path()
    if not p.exists():
        return None
    return OutputStore(p, readonly=True)


def store_runs():
    store = open_store_readonly()
    if store is None:
        return {"store": str(store_path()), "exists": False, "runs": []}
    try:
        rows = [dict(r) if not isinstance(r, dict) else r for r in store.runs()]
        return {"store": str(store_path()), "exists": True, "runs": rows}
    finally:
        store.close()


def store_daily(field=None, run_id=None):
    """Per-filing-day mean/min/max of the numeric fields in the store.

    This is what a daily sample is FOR: `documents.filing_date` joined to the
    numeric extractions, one row per day, so a series can be read off rather
    than eyeballed out of a document list. Days are the filing's own date, not
    the run's -- re-extracting last quarter tomorrow must not move the series.

    Only rows with a `value_num` count: a field the model returned as text (or
    could not read) is excluded from the mean rather than silently coerced,
    and `n` reports how many actually backed each day's number.
    """
    import sqlite3
    p = store_path()
    if not p.exists():
        return {"store": str(p), "exists": False, "series": []}
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where, args = ["d.filing_date IS NOT NULL", "e.value_num IS NOT NULL"], []
        if field:
            where.append("e.field = ?")
            args.append(field)
        if run_id:
            where.append("e.run_id = ?")
            args.append(run_id)
        rows = [dict(r) for r in conn.execute(
            "SELECT d.filing_date AS day, e.field AS field, COUNT(*) AS n, "
            "       AVG(e.value_num) AS mean, MIN(e.value_num) AS min, "
            "       MAX(e.value_num) AS max, COUNT(DISTINCT e.accession) AS filings "
            "FROM extractions e JOIN documents d "
            "  ON d.run_id = e.run_id AND d.accession = e.accession "
            " AND d.document = e.document "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY d.filing_date, e.field ORDER BY d.filing_date, e.field", args)]
        fields = [r["field"] for r in conn.execute(
            "SELECT DISTINCT field FROM extractions WHERE value_num IS NOT NULL "
            "ORDER BY field")]
    except sqlite3.Error as exc:
        return {"store": str(p), "exists": True, "series": [], "error": str(exc)}
    finally:
        conn.close()
    for r in rows:
        r["mean"] = round(r["mean"], 4) if r["mean"] is not None else None
    return {"store": str(p), "exists": True, "series": rows,
            "numeric_fields": fields, "field": field}


def store_coverage(min_documents=1):
    """Per (issuer, field): how often we actually get this, and how often we can
    point at where it came from.

    Reliability is not "did the model return something" -- it returns something
    almost always. The number that matters is how often the value is BOTH
    non-null and locatable in the filing's own text, because an unlocatable
    value is one nobody has checked and nothing can check. So each cell reports
    attempts, values, located, and the flags that explain the gap.

    Issuer-scoped because that is how the extraction behaves: 424B2 templates
    are issuer-specific, exemplars are keyed by issuer, and a field that is
    reliable for one bank routinely is not for another. An aggregate over all
    issuers would hide exactly the thing worth knowing.
    """
    import sqlite3
    p = store_path()
    if not p.exists():
        return {"store": str(p), "exists": False, "rows": []}

    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT d.issuer AS issuer, e.field AS field, "
            "       COUNT(*) AS attempts, "
            "       SUM(CASE WHEN e.value_json NOT IN ('null','\"\"','[]') "
            "                 AND e.value_json IS NOT NULL THEN 1 ELSE 0 END) AS values_got, "
            "       SUM(CASE WHEN e.span_start IS NOT NULL THEN 1 ELSE 0 END) AS located, "
            "       SUM(CASE WHEN e.flags_json LIKE '%span_unlocatable%' THEN 1 ELSE 0 END) AS unlocatable, "
            "       SUM(CASE WHEN e.flags_json LIKE '%gated_no_claude%' THEN 1 ELSE 0 END) AS gated, "
            "       COUNT(DISTINCT e.accession) AS documents "
            "FROM extractions e JOIN documents d "
            "  ON d.run_id = e.run_id AND d.accession = e.accession "
            " AND d.document = e.document "
            "WHERE d.issuer IS NOT NULL "
            "GROUP BY d.issuer, e.field")]
    except sqlite3.Error as exc:
        return {"store": str(p), "exists": True, "rows": [], "error": str(exc)}
    finally:
        conn.close()

    # What a human has confirmed for this (issuer, field) lives in the OTHER
    # store; it is the difference between "the model is consistent" and "we
    # know it is right", so the matrix carries both rather than implying one
    # from the other.
    taught, absent = {}, {}
    vpath = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser() \
        / "store" / "validation.sqlite"
    if vpath.exists():
        vconn = sqlite3.connect(f"file:{vpath}?mode=ro", uri=True)
        try:
            for issuer, field, n in vconn.execute(
                    "SELECT issuer, field, COUNT(*) FROM exemplars "
                    "WHERE kind IN ('positive','corrected') GROUP BY issuer, field"):
                taught[(issuer, field)] = n
            # Fields a human ruled ABSENT for this issuer. Not every note has a
            # coupon barrier or an autocall; scoring "this note does not have
            # one" as "we cannot extract it" would push the tool to invent
            # values to make its own numbers look better.
            for issuer, field, n in vconn.execute(
                    "SELECT issuer, field, COUNT(*) FROM exemplars "
                    "WHERE kind = 'negative' GROUP BY issuer, field"):
                absent[(issuer, field)] = n
        except sqlite3.Error:
            pass
        finally:
            vconn.close()

    out = []
    for r in rows:
        if r["documents"] < min_documents:
            continue
        attempts = r["attempts"] or 0
        r["taught"] = taught.get((r["issuer"], r["field"]), 0)
        r["absent_marked"] = absent.get((r["issuer"], r["field"]), 0)
        # Rate over filings the field could apply to. A field ruled absent is
        # removed from the denominator rather than counted as a miss -- "this
        # note has no coupon barrier" is an answer, and the alternative is a
        # score that rewards inventing one.
        applicable = max(0, attempts - r["absent_marked"])
        r["applicable"] = applicable
        r["value_rate"] = round((r["values_got"] or 0) / applicable, 3) if applicable else None
        r["located_rate"] = round((r["located"] or 0) / applicable, 3) if applicable else None
        out.append(r)
    out.sort(key=lambda r: (r["issuer"] or "", -(r["located_rate"] or 0), r["field"]))

    issuers = sorted({r["issuer"] for r in out})
    fields = sorted({r["field"] for r in out})
    return {"store": str(p), "exists": True, "rows": out,
            "issuers": issuers, "fields": fields,
            "min_documents": min_documents}


def store_extractions(run_id=None):
    """Every document/field row, newest run first. Read straight out of sqlite
    rather than through `query()` so a browser can page the raw grid."""
    import sqlite3
    p = store_path()
    if not p.exists():
        return {"store": str(p), "exists": False, "documents": []}
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where, args = "", []
        if run_id:
            where, args = " WHERE e.run_id = ?", [run_id]
        # `documents` is where issuer/product_type/filing_date live. Without this
        # join the grid can only say WHICH accession carried a value, not WHOSE
        # -- and "which issuer is doing what" is the question this view exists
        # to answer, since 424B2 templates are issuer-specific.
        rows = [dict(r) for r in conn.execute(
            "SELECT e.*, r.started_at, r.note, "
            "d.issuer AS doc_issuer, d.product_type AS doc_product_type, "
            "d.filing_date AS doc_filing_date FROM extractions e "
            "LEFT JOIN runs r ON r.run_id = e.run_id "
            "LEFT JOIN documents d ON d.run_id = e.run_id "
            "  AND d.accession = e.accession AND d.document IS e.document"
            f"{where} ORDER BY r.started_at DESC, e.rowid DESC LIMIT 4000", args)]
    except sqlite3.Error as exc:
        return {"store": str(p), "exists": True, "documents": [], "error": str(exc)}
    finally:
        conn.close()

    docs = {}
    for r in rows:
        key = (r.get("run_id"), r.get("accession"), r.get("document"))
        d = docs.setdefault(key, {
            "run_id": r.get("run_id"), "accession": r.get("accession"),
            "document": r.get("document"), "started_at": r.get("started_at"),
            "note": r.get("note"), "issuer": r.get("doc_issuer"),
            "product_type": r.get("doc_product_type"),
            "filing_date": r.get("doc_filing_date"), "fields": [],
        })
        try:
            value = json.loads(r["value_json"]) if r.get("value_json") is not None else r.get("value_num")
        except (ValueError, TypeError):
            value = r.get("value_json")
        try:
            flags = json.loads(r["flags_json"]) if r.get("flags_json") else []
        except (ValueError, TypeError):
            flags = []
        d["fields"].append({
            "field": r.get("field"), "value": value, "unit": r.get("unit"),
            "confidence": r.get("confidence"), "provenance": r.get("provenance"),
            "span": [r.get("span_start"), r.get("span_end")],
            "flags": [f.get("code") if isinstance(f, dict) else str(f) for f in (flags or [])],
            # The count the value was chosen from, never the list (#179). 0 marks
            # the free-form fallback. A candidate list is scratch and is not stored,
            # so there is none to surface here.
            "candidate_count": r.get("candidate_count"),
        })
    out = list(docs.values())
    issuers = sorted({d["issuer"] for d in out if d.get("issuer")})
    return {"store": str(p), "exists": True, "documents": out, "issuers": issuers}


# The taxonomy path (#233) writes to facts.sqlite, not extractions.sqlite -- a
# period is not a document (#183). Without a reader for it, a 10-K/10-Q/8-K run
# completes and then shows nothing anywhere in the dashboard.

def facts_path():
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    return home / "store" / "facts.sqlite"


# The structured-XML path (#286) writes to forms.sqlite -- a row store keyed by
# the filing (FormsStore, #283), not a period (facts) or a document+field
# (extractions). The Results tab reads this per form; without a reader an XML run
# would complete and then show nothing in the dashboard.

def forms_path():
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    return home / "store" / "forms.sqlite"


def store_forms(form=None, cik=None, date_from=None, date_to=None):
    """Structured-XML filing rows from FormsStore, for one form type, optionally
    narrowed by filer (`cik`) and by a filed-date range (`date_from`/`date_to`,
    inclusive ISO ``YYYY-MM-DD``).

    Read-only. Returns the matching `documents` (each with its decoded reader
    rows) plus a flattened `rows` list for the table -- every row tagged with the
    filing it came from -- and `columns`, the union of row keys in first-seen
    order, so the Results tab can render one column per key with a stable header.
    """
    from forms_store import FormsStore
    p = forms_path()
    form = (form or "").strip().upper() or None
    if not p.exists() or form is None:
        return {"store": str(p), "exists": p.exists(), "form": form,
                "documents": [], "columns": [], "rows": []}

    store = FormsStore(path=str(p), readonly=True)
    try:
        docs = store.query(form, cik=cik, date_from=date_from, date_to=date_to)
    finally:
        store.close()

    columns, seen, flat = [], set(), []
    for d in docs:
        for r in d["rows"]:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    columns.append(k)
            flat.append({**r, "_accession": d["accession"],
                         "_cik": d["cik"], "_filed": d["filed"]})
    return {"store": str(p), "exists": True, "form": form,
            "documents": docs, "columns": columns, "rows": flat}


# Statement headliners first, so the top of the grid is the income statement a
# reader expects rather than whatever sorts first alphabetically. Anything else
# the filing tags follows, alphabetically.
HEADLINE_CONCEPTS = [
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:CostOfGoodsAndServicesSold",
    "us-gaap:GrossProfit",
    "us-gaap:OperatingExpenses",
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:NetIncomeLoss",
    "us-gaap:EarningsPerShareBasic",
    "us-gaap:EarningsPerShareDiluted",
    "us-gaap:Assets",
    "us-gaap:Liabilities",
    "us-gaap:StockholdersEquity",
    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
]

# Period windows, in days. A 10-K tags quarterly and year-to-date durations as
# well as the fiscal year, and they share a period end with it; binning by
# length keeps a Q4 number from landing in a fiscal-year column.
_PERIOD_WINDOWS = {"annual": (350, 380), "quarterly": (80, 100)}

_FILER_NAMES = {}


def _filer_name_from_store(cik):
    """The filer's name from the facts store's `filers` directory, or None. A
    read-only, missing-store-safe lookup: no store, no `filers` table, or no row
    for this CIK all return None rather than raise."""
    p = facts_path()
    if not p.exists():
        return None
    import sqlite3
    from edgar_client import cik10
    try:
        key = cik10(cik)
    except (ValueError, TypeError):
        key = str(cik)
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT name FROM filers WHERE cik = ?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def filer_name(cik):
    """The filer's name, from the facts store's `filers` directory (#238) or,
    failing that, the companyfacts body a per-filer run already cached.

    The `filers` table is checked FIRST: cross-filer frames ingest fills it and
    carries no companyfacts, so it is the only name source for a frames-only
    filer. Cache-only on purpose otherwise: this is a read view, and a read must
    never reach out to EDGAR. A filer known to neither shows its CIK.
    """
    if cik in _FILER_NAMES:
        return _FILER_NAMES[cik]
    name = _filer_name_from_store(cik)
    if name:
        _FILER_NAMES[cik] = name
        return name
    name = None
    try:
        from edgar_client import HttpCache, cik10
        home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
        body = HttpCache(str(home / "cache")).get(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")
        if body:
            name = json.loads(body).get("entityName")
    except Exception:
        name = None
    _FILER_NAMES[cik] = name
    return name


def _days(start, end):
    from datetime import date
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def store_facts(cik=None, period="annual", form=None):
    """Reported XBRL facts as a statement grid: concept rows x period columns.

    Latest version per period (supersession collapsed, as `facts_for` does), so
    a restated number shows its current value -- with the period flagged, since
    a restatement is worth seeing rather than silently absorbing. Instants
    (balance-sheet points) sit alongside durations in the same column by their
    end date.

    `form` scopes everything to the filings of one form type, so the 10-K view
    shows what 10-Ks said and never a 10-Q's number for the same period. The
    filter applies BEFORE supersession is collapsed, for the same reason.
    """
    import json as _json
    import sqlite3
    p = facts_path()
    if not p.exists():
        return {"store": str(p), "exists": False, "filers": [], "rows": [],
                "form": form}
    if period not in _PERIOD_WINDOWS:
        raise ValueError(f"period must be one of {sorted(_PERIOD_WINDOWS)}")
    form = (form or "").strip().upper() or None
    fwhere, fargs = ("form = ?", [form]) if form else ("1 = 1", [])

    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        filers = [dict(r) for r in conn.execute(
            "SELECT cik, COUNT(*) AS facts, COUNT(DISTINCT accession) AS filings, "
            "       GROUP_CONCAT(DISTINCT form) AS forms, "
            "       MIN(period_end) AS first, MAX(period_end) AS last "
            f"FROM facts WHERE {fwhere} GROUP BY cik ORDER BY COUNT(*) DESC", fargs)]
        # CIKs are stored in the one canonical 10-digit form (#252). Match a
        # caller's CIK (either form) against that, and display bare.
        from edgar_client import cik10
        known = {f["cik"] for f in filers}
        try:
            want = cik10(cik) if cik else None
        except (ValueError, TypeError):
            want = str(cik)
        chosen = want if want in known else (filers[0]["cik"] if filers else None)
        restated = {(r["concept"], r["period_start"], r["period_end"])
                    for r in conn.execute(
                        "SELECT concept, period_start, period_end FROM facts "
                        f"WHERE cik = ? AND {fwhere} "
                        "GROUP BY concept, period_start, period_end "
                        "HAVING COUNT(DISTINCT value_json) > 1", [chosen, *fargs])}
        # Latest version per (concept, period), the same ordering facts_for uses.
        raw = conn.execute(
            f"SELECT * FROM facts WHERE cik = ? AND {fwhere} "
            "ORDER BY concept, period_end, accession DESC, ingested_seq DESC",
            [chosen, *fargs]).fetchall()
    except sqlite3.Error as exc:
        return {"store": str(p), "exists": True, "filers": [], "rows": [],
                "form": form, "error": str(exc)}
    finally:
        conn.close()
    from edgar_client import cik_bare
    for f in filers:
        f["name"] = filer_name(f["cik"])
        f["cik"] = cik_bare(f["cik"])
    if not chosen:
        return {"store": str(p), "exists": True, "filers": [], "rows": [],
                "form": form}

    facts, seen = [], set()
    for r in raw:
        key = (r["concept"], r["period_start"], r["period_end"])
        if key in seen:
            continue
        seen.add(key)
        facts.append({"concept": r["concept"], "unit": r["unit"],
                      "period_start": r["period_start"], "period_end": r["period_end"],
                      "accession": r["accession"],
                      "value": _json.loads(r["value_json"])
                      if r["value_json"] is not None else None})

    lo, hi = _PERIOD_WINDOWS[period]
    rows, periods = {}, set()
    for f in facts:
        d = _days(f["period_start"], f["period_end"])
        if d is None or not (d == 0 or lo <= d <= hi):
            continue
        end = f["period_end"]
        row = rows.setdefault((f["concept"], f["unit"]), {
            "concept": f["concept"], "unit": f["unit"],
            "kind": "instant" if d == 0 else "duration",
            "values": {}, "accessions": {}, "restated": []})
        # Two windows of the same length can share an end (a 52- and a
        # 53-week year, say). The later filing wins, as it does in facts_for.
        if end in row["accessions"] and row["accessions"][end] > f["accession"]:
            continue
        row["values"][end] = f["value"]
        row["accessions"][end] = f["accession"]
        if (f["concept"], f["period_start"], end) in restated:
            row["restated"].append(end)
        if d:
            periods.add(end)

    # Columns are the fiscal period ends the durations define. An instant dated
    # anywhere else (cover-page share count, public float at mid-year) would
    # otherwise add a column of its own that is empty for every other row, so
    # those points ride along as `other` instead of widening the grid.
    if not periods:
        periods = {e for r in rows.values() for e in r["values"]}
    for r in rows.values():
        r["other"] = {e: v for e, v in r["values"].items() if e not in periods}
        r["values"] = {e: v for e, v in r["values"].items() if e in periods}

    rank = {c: i for i, c in enumerate(HEADLINE_CONCEPTS)}
    ordered = sorted(rows.values(), key=lambda r: (
        rank.get(r["concept"], len(rank)), r["concept"], r["unit"] or ""))
    return {"store": str(p), "exists": True, "filers": filers,
            "cik": cik_bare(chosen), "name": filer_name(chosen),
            "period": period, "form": form,
            "periods": sorted(periods), "rows": ordered,
            "headline": [c for c in HEADLINE_CONCEPTS
                         if any(r["concept"] == c for r in ordered)]}


# --------------------------------------------------------------------------- #
# Canonical layer: standardized (one filer) and compare (one field, many filers)
# --------------------------------------------------------------------------- #

def _validate_as_of(as_of):
    """Return `as_of` unchanged (or None when blank), raising ValueError on any
    string that is not an ISO date. Validated up front so a malformed `as_of`
    is a 400 naming the fault, never a silent all-rows result."""
    if as_of in (None, ""):
        return None
    from datetime import date
    try:
        date.fromisoformat(as_of)
    except (TypeError, ValueError):
        raise ValueError(
            f"as_of must be an ISO date (YYYY-MM-DD), got {as_of!r}")
    return as_of


def _cached_ticker_map():
    """`TICKER -> bare CIK` from the cached `company_tickers.json`, or `{}` when
    it was never fetched. Cache-only on purpose: a read view must never reach out
    to EDGAR, so a ticker the cache does not know resolves to nothing (and is
    reported as unresolved) rather than triggering a network call."""
    from edgar_client import HttpCache, EdgarClient, cik_bare
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    body = HttpCache(str(home / "cache")).get(EdgarClient.TICKERS_URL)
    if not body:
        return {}
    raw = json.loads(body)
    rows = raw.values() if isinstance(raw, dict) else raw
    out = {}
    for row in rows:
        tick = (row.get("ticker") or "").strip().upper()
        if tick:
            try:
                out[tick] = cik_bare(row["cik_str"])
            except (KeyError, ValueError):
                continue
    return out


def _resolve_tickers(tokens):
    """Resolve a list of ticker tokens to bare CIKs against the cached map.
    Returns `(ciks, unresolved)`; a token the cache cannot place is kept in
    `unresolved` rather than dropped silently."""
    tmap = _cached_ticker_map()
    ciks, unresolved = [], []
    for tok in tokens:
        key = (tok or "").strip().upper()
        if not key:
            continue
        cik = tmap.get(key)
        if cik:
            ciks.append(cik)
        else:
            unresolved.append(tok.strip())
    return ciks, unresolved


def _facts_ciks(store):
    """Every CIK the facts table holds, busiest first. Uses the store's own
    connection (read path) so it sees exactly the rows the resolvers do."""
    return [r["cik"] for r in store._conn.execute(
        "SELECT cik, COUNT(*) AS c FROM facts GROUP BY cik ORDER BY c DESC")]


def _as_of_facts(store, cik, facts, as_of):
    """Restrict a filer's facts to what was KNOWN on `as_of`. For each fact,
    `FactsStore.as_of` supplies the version whose `filed <= as_of` (a restatement
    filed later is not yet visible). A fact with no `filed` date (a frames row)
    cannot be placed in time and is excluded. Returns `(kept, excluded_count)`
    where `excluded_count` is how many were dropped for lacking a filed date."""
    kept, excluded = [], 0
    for f in facts:
        if f["filed"] is None:
            excluded += 1
            continue
        picked = store.as_of(cik, f["concept"], f["period_start"],
                             f["period_end"], as_of)
        if picked is not None:
            kept.append(picked)
    return kept, excluded


def store_standardized(cik=None, period="annual", as_of=None):
    """One filer's statements in STANDARD fields, not raw us-gaap concepts.

    Each `CANONICAL` field the filer has becomes one row, its value resolved
    per period through `canonical_concepts.resolve` (era-partitioned concepts
    collapsed to one value each). `concepts` records which us-gaap concept
    supplied each period's value, so a standard label never hides the tag behind
    it. `period` bins by duration exactly as `store_facts` does. With `as_of`,
    each value is what was known on that date; frames facts (no filed date) drop
    out and are counted in `excluded_no_filed_date`.
    """
    from facts_store import FactsStore
    import canonical_concepts as cc
    from calc_linkbase import check_rollups

    if period not in _PERIOD_WINDOWS:
        raise ValueError(f"period must be one of {sorted(_PERIOD_WINDOWS)}")
    as_of = _validate_as_of(as_of)

    p = facts_path()
    empty = {"store": str(p), "exists": p.exists(), "cik": None, "name": None,
             "period": period, "periods": [], "rows": [],
             "excluded_no_filed_date": 0}
    if not p.exists():
        return empty

    store = FactsStore(path=str(p), readonly=True)
    try:
        present = _facts_ciks(store)
        if not present:
            return {**empty, "exists": True}
        want = None
        if cik:
            from edgar_client import cik10
            try:
                want = cik10(cik)
            except (ValueError, TypeError):
                want = str(cik)
        chosen = want if want in present else present[0]

        facts = store.facts_for(chosen)
        excluded = 0
        if as_of:
            facts, excluded = _as_of_facts(store, chosen, facts, as_of)

        lo, hi = _PERIOD_WINDOWS[period]

        def in_window(e):
            d = _days(e["period_start"], e["period_end"])
            return d is not None and (d == 0 or lo <= d <= hi)

        # The filer's own roll-ups, used to check the standard layer (#262): a
        # value whose filing says it equals a sum of other lines, and does not,
        # is flagged. Arcs are keyed by accession, so the period's resolved value
        # is checked against the arcs of the very filing that reported it. The
        # period's `{concept: value}` comes from the same facts the rows resolve
        # from. Cached by accession so one filing's arcs are fetched once.
        period_values = {}
        for f in facts:
            period_values.setdefault(
                (f["period_start"], f["period_end"]), {})[f["concept"]] = f["value"]
        arcs_by_accession = {}

        def _arcs(accession):
            if accession not in arcs_by_accession:
                arcs_by_accession[accession] = store.arcs_for(chosen, accession)
            return arcs_by_accession[accession]

        rows, periods = [], set()
        for field in cc.all_fields():
            entries = [e for e in cc.resolve(facts, field) if in_window(e)]
            if not entries:
                continue
            values, concepts, checks = {}, {}, {}
            for e in entries:
                end = e["period_end"]
                values[end] = e["value"]
                concepts[end] = e["concept"]
                periods.add(end)
                # Check only when the resolved concept is a PARENT in the arcs of
                # the filing that reported it -- a field with no applicable
                # roll-up carries no `checks` entry.
                arcs = _arcs(e["accession"])
                pv = period_values.get((e["period_start"], end), {})
                for res in check_rollups(pv, arcs):
                    if res["parent"] == e["concept"]:
                        checks[end] = {"ok": res["ok"],
                                       "expected": res["expected"],
                                       "reported": res["reported"]}
                        break
            row = {"field": field, "values": values, "concepts": concepts}
            if checks:
                row["checks"] = checks
            rows.append(row)

        from edgar_client import cik_bare
        return {"store": str(p), "exists": True, "cik": cik_bare(chosen),
                "name": filer_name(chosen), "period": period,
                "periods": sorted(periods), "rows": rows,
                "excluded_no_filed_date": excluded}
    finally:
        store.close()


def store_compare(field=None, period_end=None, tickers=None, as_of=None, fy=None):
    """One STANDARD field ranked across filers, for a fiscal year or one exact
    period end.

    Exactly one of `fy` and `period_end` must be given (both, or neither, is a
    ValueError -> HTTP 400):

    * `fy=<year>` ranks each filer's ANNUAL figure for that fiscal year. "Annual"
      is a duration of `_PERIOD_WINDOWS["annual"]` days (350-380), and the year a
      filing belongs to is SEC's frames alignment: a period end in
      `[<fy>-01-01 + 1 month, <fy+1>-01-01 + 1 month)`, i.e. `[<fy>-02-01,
      <fy+1>-02-01)`. So a fiscal year ending 2025-01-26 is FY2024, and Apple
      (Sep), Microsoft (Jun) and Walmart (Jan) all rank alongside a December
      filer. A filer with several matches keeps its latest `period_end`.
    * `period_end=<date>` keeps the original behaviour: the value reported for
      exactly that end date, longest span winning a shared end.

    Every filer in the store (or, with `tickers`, only those it names) is resolved
    to one value for `field` via `canonical_concepts.resolve`, and the rows are
    sorted by value descending. Each row carries its own `period_end`. `tickers`
    is a comma list resolved to CIKs from the cached `company_tickers.json` ONLY
    -- an unknown ticker lands in `unresolved`, never a network call. With
    `as_of`, a value filed after that date is not shown, and frames facts (no
    filed date) are excluded and counted in `excluded_no_filed_date`.
    """
    from facts_store import FactsStore
    import canonical_concepts as cc

    if not field or field not in cc.all_fields():
        raise ValueError(
            f"unknown field {field!r}; known: {', '.join(sorted(cc.all_fields()))}")
    has_fy = fy not in (None, "")
    has_pe = period_end not in (None, "")
    if has_fy == has_pe:
        raise ValueError("exactly one of fy or period_end is required")
    year, win_lo, win_hi = None, None, None
    if has_fy:
        try:
            year = int(fy)
        except (TypeError, ValueError):
            raise ValueError(f"fy must be a year, got {fy!r}")
        # SEC frames alignment: a fiscal year ending in the month after Jan of
        # <fy+1> (e.g. 2025-01-26) still belongs to <fy>.
        win_lo, win_hi = "%04d-02-01" % year, "%04d-02-01" % (year + 1)
    as_of = _validate_as_of(as_of)

    scope, unresolved = None, []
    if tickers:
        scope, unresolved = _resolve_tickers(tickers.split(","))

    p = facts_path()
    base = {"store": str(p), "exists": p.exists(), "field": field,
            "period_end": period_end, "fy": year, "rows": [],
            "unresolved": unresolved, "excluded_no_filed_date": 0}
    if not p.exists():
        return base

    alo, ahi = _PERIOD_WINDOWS["annual"]

    def is_annual_in_window(f):
        d = _days(f["period_start"], f["period_end"])
        return (d is not None and alo <= d <= ahi
                and win_lo <= f["period_end"] < win_hi)

    store = FactsStore(path=str(p), readonly=True)
    try:
        from edgar_client import cik_bare
        concepts = cc.input_concepts(field)
        ciks = [str(c) for c in scope] if scope is not None else _facts_ciks(store)

        rows, excluded = [], 0
        for cik in ciks:
            facts = store.facts_for(cik)
            if has_fy:
                relevant = [f for f in facts
                            if f["concept"] in concepts and is_annual_in_window(f)]
            else:
                relevant = [f for f in facts
                            if f["concept"] in concepts
                            and f["period_end"] == period_end]
            if as_of:
                relevant, dropped = _as_of_facts(store, cik, relevant, as_of)
                excluded += dropped
            if not relevant:
                continue
            match = cc.resolve(relevant, field)
            if has_fy:
                match = [e for e in match if win_lo <= e["period_end"] < win_hi]
                if not match:
                    continue
                # Several annual filings land in one fiscal-year window (a filer
                # that shifted its year end): the latest end is the current one.
                best = max(match, key=lambda e: (
                    e["period_end"], _days(e["period_start"], e["period_end"]) or 0))
            else:
                match = [e for e in match if e["period_end"] == period_end]
                if not match:
                    continue
                # A filer can report the same end under two durations (a Q4 and
                # the fiscal year); take the longest span so the annual number
                # ranks.
                best = max(match, key=lambda e: _days(e["period_start"],
                                                       e["period_end"]) or 0)
            rows.append({"cik": cik_bare(cik), "name": filer_name(cik),
                         "value": best["value"], "concept": best["concept"],
                         "period_end": best["period_end"]})

        rows.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0)))
        return {**base, "rows": rows, "excluded_no_filed_date": excluded}
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

def loaded_runtime(base_url, model):
    """What the ollama server ACTUALLY loaded, from /api/ps.

    The probe reports the profile it *selected*; this reports what is resident.
    They diverge silently and expensively: a context the server sized itself
    (the model's trained 32k rather than the 8k this tool budgets for) times
    the parallel slots overruns 12GB of VRAM, and ollama quietly runs the
    remainder on the CPU -- roughly a 10x slowdown that reads as "the local
    model is just slow" rather than as a misconfiguration. `size` vs
    `size_vram` is the only place that shows up.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base_url}/api/ps", timeout=3) as resp:
            ps = json.load(resp)
    except Exception:
        return None
    for m in ps.get("models", []):
        if not model_pulled(model, [m.get("name")]):
            continue
        total, vram = m.get("size") or 0, m.get("size_vram") or 0
        offload = max(0, total - vram)
        return {
            "loaded": True,
            "context_length": m.get("context_length"),
            "size_gb": round(total / 1e9, 2),
            "vram_gb": round(vram / 1e9, 2),
            "cpu_offload_gb": round(offload / 1e9, 2),
            "cpu_offload_pct": round(100 * offload / total) if total else 0,
        }
    return {"loaded": False}


_MODEL_START = {"state": "idle", "message": None, "started_at": None}
_MODEL_START_LOCK = threading.Lock()


def _ollama_base_and_model():
    from ollama_client import OllamaConfig
    cfg = OllamaConfig.from_env({})
    return cfg.base_url.rsplit("/v1", 1)[0], cfg.model


def _ollama_up(base, timeout=2):
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=timeout) as resp:
            return json.load(resp)
    except Exception:
        return None


def _find_ollama():
    """The ollama binary: PATH first, then the Windows per-user install dir."""
    import shutil
    exe = shutil.which("ollama")
    if exe:
        return exe
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(cand) if cand.exists() else None


def _launch_ollama_serve(exe):
    """Start `ollama serve` detached, so it outlives this request (and this
    server). Output goes to a log beside the scrubber's store."""
    import subprocess
    home = Path(os.environ.get("EDGAR_SCRUBBER_HOME", DEFAULT_HOME)).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    log = open(home / "ollama-serve.log", "ab")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, "serve"], stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def model_pulled(model, names):
    """Ollama lists a model by its full tag ("edgar-qwen7b:latest"); OLLAMA_MODEL
    is usually written bare ("edgar-qwen7b"). Both name the same model, so an
    exact match reported an installed model as "not pulled"."""
    def norm(n):
        n = (n or "").strip()
        return n[:-len(":latest")] if n.endswith(":latest") else n
    return norm(model) in {norm(n) for n in names}


def start_local_model(*, up=None, find=None, launch=None, warm=None, sleep=None,
                      wait_s=30):
    """The "Start local model" button. Brings Ollama up if it is down, then loads
    the scrubber's model into memory so the first extraction does not pay for the
    load. Never pulls a model (several GB): a missing one is reported with the
    exact command. Runs synchronously; the page polls /api/health meanwhile.
    Returns {"ok", "state", "message"}. The seams are for tests."""
    import time as _time
    import urllib.request
    up = up or _ollama_up
    find = find or _find_ollama
    launch = launch or _launch_ollama_serve
    sleep = sleep or _time.sleep
    base, model = _ollama_base_and_model()

    def _warm():
        req = urllib.request.Request(
            f"{base}/api/generate",
            data=json.dumps({"model": model, "prompt": "", "keep_alive": "30m"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp.read()
    warm = warm or _warm

    tags = up(base)
    if tags is None:
        exe = find()
        if not exe:
            return {"ok": False, "state": "no-ollama",
                    "message": "Ollama is not installed (no `ollama` on PATH or in "
                               "%LOCALAPPDATA%\\Programs\\Ollama). Install it from https://ollama.com."}
        launch(exe)
        waited = 0
        while tags is None and waited < wait_s:
            sleep(1)
            waited += 1
            tags = up(base)
        if tags is None:
            return {"ok": False, "state": "ollama-timeout",
                    "message": f"started Ollama but it did not answer at {base} within {wait_s}s"}

    names = [m.get("name") for m in tags.get("models", [])]
    if not model_pulled(model, names):
        return {"ok": False, "state": "model-missing",
                "message": f"Ollama is up, but {model} is not downloaded. Run: ollama pull {model}"
                           " (or build the 8k-context model: ollama create edgar-qwen7b -f "
                           "tools/edgar_scrubber/Modelfile.qwen2.5-7b, and set OLLAMA_MODEL=edgar-qwen7b)"}
    try:
        warm()
    except Exception as exc:
        return {"ok": False, "state": "load-failed",
                "message": f"Ollama is up but loading {model} failed: {type(exc).__name__}: {exc}"}
    return {"ok": True, "state": "ready", "message": f"{model} loaded"}


def start_local_model_async():
    """Kick start_local_model() on a thread; one at a time. The page polls
    /api/health (which carries `model_start`) for progress."""
    with _MODEL_START_LOCK:
        if _MODEL_START["state"] == "starting":
            return dict(_MODEL_START)
        _MODEL_START.update(state="starting", message="starting Ollama / loading model",
                            started_at=utcnow())

    def _go():
        try:
            res = start_local_model()
        except Exception as exc:
            res = {"ok": False, "state": "error", "message": f"{type(exc).__name__}: {exc}"}
        with _MODEL_START_LOCK:
            _MODEL_START.update(state=res["state"], message=res["message"])

    threading.Thread(target=_go, daemon=True, name="model-start").start()
    return dict(_MODEL_START)


def health():
    ok_ollama, model, detail, runtime = False, None, None, None
    try:
        from ollama_client import OllamaConfig
        import urllib.request
        cfg = OllamaConfig.from_env({})
        model = cfg.model
        base = cfg.base_url.rsplit("/v1", 1)[0]
        with urllib.request.urlopen(f"{base}/api/tags", timeout=3) as resp:
            tags = json.load(resp)
        names = [m["name"] for m in tags.get("models", [])]
        ok_ollama = model_pulled(model, names)
        detail = None if ok_ollama else f"{model} not pulled (have: {', '.join(names[:4])})"
        runtime = loaded_runtime(base, model)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"

    return {
        "ok": True,
        "runner": "edgar-scrubber",
        "ollama": {"ok": ok_ollama, "model": model, "detail": detail,
                   "runtime": runtime},
        "model_start": dict(_MODEL_START),
        "edgar_user_agent": bool(os.environ.get("EDGAR_USER_AGENT", "").strip()),
        "store": {"path": str(store_path()), "exists": store_path().exists()},
        "queries": sorted(p.stem for p in (SCRUBBER / "queries").glob("*.json")),
    }


# --------------------------------------------------------------------------- #
# Markdown -> HTML (enough to read a README in a browser tab)
# --------------------------------------------------------------------------- #

_MD_STYLE = """
body{background:#0d1117;color:#e6edf3;font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;
     max-width:900px;margin:0 auto;padding:32px 20px}
a{color:#2f81f7} code{background:#161b22;padding:1px 5px;border-radius:4px;font-size:13px}
pre{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;overflow-x:auto}
pre code{background:none;padding:0} h1,h2,h3{border-bottom:1px solid #30363d;padding-bottom:6px}
table{border-collapse:collapse;width:100%} td,th{border:1px solid #30363d;padding:6px 10px;text-align:left}
blockquote{border-left:3px solid #30363d;margin:0;padding-left:12px;color:#8b949e}
.back{display:inline-block;margin-bottom:18px;color:#8b949e;text-decoration:none}
"""


def markdown_to_html(md, title):
    """Deliberately small: headings, fences, inline code, links, lists, tables,
    rules. A card links here so the doc opens in the app instead of sending the
    reader to GitHub -- it does not need to be a full CommonMark renderer."""
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def inline(s):
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return s

    out, in_code, in_list, in_table = [], False, False, False
    for line in md.splitlines():
        if line.startswith("```"):
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(esc(line))
            continue
        if in_list and not line.lstrip().startswith(("- ", "* ")):
            out.append("</ul>")
            in_list = False
        if in_table and not line.strip().startswith("|"):
            out.append("</table>")
            in_table = False

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            continue
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
            continue
        if stripped.startswith(">"):
            out.append(f"<blockquote>{inline(stripped.lstrip('> '))}</blockquote>")
            continue
        if set(stripped) <= set("-*_") and len(stripped) >= 3:
            out.append("<hr>")
            continue
        out.append(f"<p>{inline(stripped)}</p>")
    for tag, flag in (("</code></pre>", in_code), ("</ul>", in_list), ("</table>", in_table)):
        if flag:
            out.append(tag)

    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title>"
            f"<style>{_MD_STYLE}</style></head><body>"
            f"<a class='back' href='javascript:history.back()'>← back to the dashboard</a>"
            + "\n".join(out) + "</body></html>")


def serve_doc(rel_path):
    if not any(rel_path.startswith(root) for root in DOC_ROOTS) or ".." in rel_path:
        return None
    target = (REPO_ROOT / rel_path).resolve()
    if REPO_ROOT not in target.parents or not target.is_file():
        return None
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix.lower() in (".md", ".markdown"):
        return markdown_to_html(text, target.name)
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{target.name}</title>" \
           f"<style>{_MD_STYLE}</style></head><body><pre>{text}</pre></body></html>"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

# Text types we serve are UTF-8 on the wire, and say so. Binary types carry no
# charset -- there is no such thing as a UTF-8 PNG.
TEXT_TYPES = frozenset((
    "text/html", "text/css", "text/plain", "text/markdown", "text/csv",
    "application/javascript", "application/json", "image/svg+xml",
    "application/xml", "text/xml",
))


def with_charset(ctype):
    """`text/html` -> `text/html; charset=utf-8`, for text types only.

    Without this the dashboard renders only because index.html carries a <meta
    charset> in its first 1024 bytes and the browser sniffs it -- a second line
    of defence doing the first one's job. In #162 a double-encoded file took a
    screenshot to notice and the transport layer had to be ruled out first;
    declaring the charset removes that ambiguity, and covers files served
    without a <meta> tag of their own, the API responses included.
    """
    if not ctype:
        return ctype
    base = ctype.split(";", 1)[0].strip().lower()
    if "charset=" in ctype.lower() or base not in TEXT_TYPES:
        return ctype
    return "%s; charset=utf-8" % base


class Handler(SimpleHTTPRequestHandler):
    server_version = "TraderScreenerTools/1.0"

    def guess_type(self, path):
        """Static files, with the charset declared. Binary types untouched."""
        return with_charset(SimpleHTTPRequestHandler.guess_type(self, path))

    def end_headers(self):
        # Dev server: the dashboard is edited while it is open, and a browser
        # holding a cached index.html silently runs code that no longer matches
        # this process's API. Never cache anything here.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        SimpleHTTPRequestHandler.end_headers(self)

    def _send(self, status, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", with_charset(ctype))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, default=str), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/api/health":
            return self._json(health())
        if path == "/api/tools":
            manifest = json.loads((REPO_ROOT / "web-dashboard" / "tools-manifest.json")
                                  .read_text(encoding="utf-8"))
            for t in manifest:
                t["runnable"] = t.get("id") in ("edgar-scrubber", "free-research")
            return self._json(manifest)
        if path == "/api/tools/edgar-scrubber/fields":
            import field_spec
            spec = field_spec.load_spec(SCRUBBER / "field_specs" / "424b2_structured_note.json")
            return self._json({
                "spec_id": spec.spec_id,
                "default": DEFAULT_FIELDS,
                "fields": [{"name": f.name, "type": getattr(f, "type", None),
                            "unit": getattr(f, "unit", None),
                            "sections": list(getattr(f, "sections", []) or []),
                            "description": getattr(f, "description", None)}
                           for f in spec.fields],
            })
        if path == "/api/tools/edgar-scrubber/queries":
            out = []
            from crawl import load_query
            for p in sorted((SCRUBBER / "queries").glob("*.json")):
                try:
                    sq = load_query(p)
                except Exception:
                    continue
                out.append({"id": sq.id, "q": sq.q, "forms": list(sq.forms or []),
                            "ciks": list(sq.ciks or []), "startdt": sq.startdt,
                            "enddt": sq.enddt})
            return self._json(out)
        if path == "/api/tools/edgar-scrubber/forms":
            try:
                import scrubbers as scrubbers_mod
                return self._json({"forms": scrubbers_mod.supported_forms()})
            except ImportError:
                return self._json({"error": "scrubbers registry not available"}, 503)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/tools/edgar-scrubber/scrubbers":
            try:
                import scrubbers as scrubbers_mod
                scrubbers = scrubbers_mod.load_scrubbers()
                queries = scrubbers_mod.stored_queries()
                out = []
                for s in scrubbers.values():
                    has_queries = any(s.owns_query(q) for q in queries)
                    entry = s.to_dict()
                    entry["has_stored_queries"] = has_queries
                    out.append(entry)
                return self._json(out)
            except ImportError:
                return self._json({"error": "scrubbers registry not available"}, 503)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/tools/testing/compare":
            a = (qs.get("run_a") or [None])[0]
            b = (qs.get("run_b") or [None])[0]
            if not a or not b:
                return self._json({"error": "run_a and run_b are both required"}, 400)
            try:
                return self._json(compare_runs_json(
                    a, b, (qs.get("field") or [None])[0]))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 404)
        if path == "/api/tools/free-research/templates":
            try:
                return self._json(fr_templates())
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/tools/free-research/pulls":
            try:
                return self._json(fr_store().list_pulls())
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/tools/free-research/briefs":
            try:
                return self._json(fr_store().list_briefs())
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/tools/free-research/brief":
            brief_id = (qs.get("id") or [None])[0]
            if not brief_id:
                return self._json({"error": "brief id is required"}, 400)
            try:
                return self._json(fr_brief_view(brief_id))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 404)
        if path == "/api/tools/research/providers":
            # Which providers the picker may offer, and why Claude is disabled
            # when it is. No provider is built or called -- availability only.
            try:
                return self._json(research_providers(), 200)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        m = re.fullmatch(r"/api/tools/research/batch/([\w.-]+)", path)
        if m:
            # Persisted, so a batch created before a restart answers here after
            # it, with whatever status it left off at.
            batch = RESEARCH.get(m.group(1))
            return self._json(batch) if batch \
                else self._json({"error": "no such batch: %r" % m.group(1)}, 404)
        if path == "/api/runs":
            return self._json(RUNS.list())
        # The activity feed. Incremental by design: `after` is the last event
        # number the browser already has, so a poll every second costs one line
        # per thing that actually happened rather than the whole run.
        m = re.fullmatch(r"/api/runs/([\w.-]+)/events", path)
        if m:
            try:
                after = int((qs.get("after") or ["-1"])[0])
            except ValueError:
                return self._json({"error": "after must be an integer"}, 400)
            out = RUNS.events_since(m.group(1), after)
            return self._json(out) if out else self._json({"error": "no such run"}, 404)
        m = re.fullmatch(r"/api/runs/([\w.-]+)", path)
        if m:
            run = RUNS.get(m.group(1))
            return self._json(run) if run else self._json({"error": "no such run"}, 404)
        if path == "/api/tools/edgar-scrubber/document":
            try:
                return self._json(load_document((qs.get("cik") or [None])[0],
                                                 (qs.get("accession") or [None])[0],
                                                 (qs.get("document") or [None])[0]))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/edgar-scrubber/annotations":
            try:
                return self._json(list_annotations((qs.get("accession") or [None])[0],
                                                    (qs.get("document") or [None])[0],
                                                    (qs.get("session") or ["dashboard"])[0]))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/store/runs":
            return self._json(store_runs())
        if path == "/api/store/coverage":
            try:
                return self._json(store_coverage(
                    int((qs.get("min_documents") or ["1"])[0])))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/store/daily":
            return self._json(store_daily((qs.get("field") or [None])[0],
                                           (qs.get("run_id") or [None])[0]))
        if path == "/api/store/facts":
            try:
                return self._json(store_facts((qs.get("cik") or [None])[0],
                                              (qs.get("period") or ["annual"])[0],
                                              (qs.get("form") or [None])[0]))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/store/forms":
            try:
                return self._json(store_forms(
                    (qs.get("form") or [None])[0],
                    (qs.get("cik") or [None])[0],
                    (qs.get("from") or [None])[0],
                    (qs.get("to") or [None])[0]))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/store/standardized":
            try:
                return self._json(store_standardized(
                    (qs.get("cik") or [None])[0],
                    (qs.get("period") or ["annual"])[0],
                    (qs.get("as_of") or [None])[0]))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/store/compare":
            try:
                return self._json(store_compare(
                    (qs.get("field") or [None])[0],
                    (qs.get("period_end") or [None])[0],
                    (qs.get("tickers") or [None])[0],
                    (qs.get("as_of") or [None])[0],
                    (qs.get("fy") or [None])[0]))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/store/extractions":
            return self._json(store_extractions((qs.get("run_id") or [None])[0]))
        if path == "/api/docs":
            rel = (qs.get("path") or [""])[0]
            html = serve_doc(rel)
            if html is None:
                return self._send(404, "<h1>404 — no such doc</h1>", "text/html")
            return self._send(200, html, "text/html; charset=utf-8")
        if path.startswith("/api/"):
            return self._json({"error": "unknown endpoint"}, 404)

        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/web-dashboard/index.html")
            self.end_headers()
            return
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        path = urlparse(self.path).path

        # A POST here spends real resources: SEC requests under this machine's
        # user-agent and writes to the local store. The server is loopback-only,
        # but any page in the browser can POST to localhost -- so a request
        # carrying a foreign Origin is refused rather than served.
        origin = self.headers.get("Origin")
        if origin:
            allowed = {f"http://{self.headers.get('Host', '')}",
                       f"https://{self.headers.get('Host', '')}"}
            if origin not in allowed:
                return self._json({"error": f"cross-origin POST refused (Origin: {origin})"}, 403)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            params = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "body must be JSON"}, 400)

        if path == "/api/tools/edgar-scrubber/model/start":
            return self._json(start_local_model_async(), 202)
        if path == "/api/tools/edgar-scrubber/search":
            try:
                return self._json(edgar_search_daily(params)
                                  if params.get("mode") == "daily"
                                  else edgar_search(params))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/free-research/pull":
            try:
                return self._json({"run_id": start_fr_pull(params)}, 202)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/free-research/brief":
            # A brief naming a pull that is not here fails HERE, with a 4xx and
            # no run created: a run that could only ever fail is noise in the
            # run list, and the caller should hear about it now.
            try:
                return self._json({"run_id": start_fr_brief(params)}, 202)
            except LookupError as exc:
                return self._json({"error": str(exc)}, 404)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/research/estimate":
            # What the next plan() would cost, plus an estimate_id the user's
            # paid plan must quote. Measures text only -- no provider call.
            try:
                return self._json(research_estimate(params), 200)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/research/plan":
            # Stores a proposed batch; spends a model call, no SEC request. A
            # paid provider is refused 409 without an estimate_id (see the gate).
            # The searches do not run until the batch is approved by id below.
            try:
                status, body = research_plan(params)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json(body, status)
        m = re.fullmatch(r"/api/tools/research/batch/([\w.-]+)/approve", path)
        if m:
            try:
                status, body = approve_research_batch(m.group(1))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json(body, status)
        m = re.fullmatch(r"/api/tools/research/extract/([\w.-]+)/approve", path)
        if m:
            # Starts a scrubber run over the proposal's accessions; a form no
            # scrubber owns is the run's own 400 (see approve_research_extract).
            try:
                status, body = approve_research_extract(m.group(1))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json(body, status)
        m = re.fullmatch(r"/api/runs/([\w.-]+)/stop", path)
        if m:
            ok = RUNS.request_stop(m.group(1))
            return self._json({"stopping": ok, "run_id": m.group(1)},
                              200 if ok else 409)
        if path == "/api/tools/edgar-scrubber/availability":
            try:
                return self._json({"run_id": start_availability(params)}, 202)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/edgar-scrubber/claude-template":
            try:
                if params.get("sync"):
                    return self._json(claude_template(params))
                return self._json({"run_id": start_claude_template(params)}, 202)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/edgar-scrubber/preview":
            try:
                # `sync` blocks and returns the proposals directly (handy from a
                # shell); the dashboard takes the tracked job so it can show
                # progress and stop it.
                if params.get("sync"):
                    return self._json(preview_document(params))
                return self._json({"run_id": start_preview(params)}, 202)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/edgar-scrubber/annotate":
            try:
                return self._json(save_annotation(params))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/edgar-scrubber/annotate/delete":
            try:
                return self._json(delete_annotation(params))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/tools/edgar-scrubber/run":
            try:
                run_id = start_scrubber_run(params)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json({"run_id": run_id}, 202)
        return self._json({"error": "unknown endpoint"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path

        # Same cross-origin refusal as POST: a PUT edits a stored proposal, which
        # is a state change any page in the browser could try to drive.
        origin = self.headers.get("Origin")
        if origin:
            allowed = {f"http://{self.headers.get('Host', '')}",
                       f"https://{self.headers.get('Host', '')}"}
            if origin not in allowed:
                return self._json({"error": f"cross-origin PUT refused (Origin: {origin})"}, 403)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            params = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "body must be JSON"}, 400)

        m = re.fullmatch(r"/api/tools/research/batch/([\w.-]+)", path)
        if m:
            try:
                status, body = research_edit(m.group(1), params)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json(body, status)
        return self._json({"error": "unknown endpoint"}, 404)

    def log_message(self, fmt, *args):
        line = fmt % args
        if "/api/runs/" in line:
            return  # progress polling is every second; don't drown the console
        sys.stderr.write(f"{self.address_string()} - {line}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--port", type=int, default=8137)
    ap.add_argument("--host", default="127.0.0.1")
    # Accuracy switches, off by default because both spend model time. Set here
    # they apply to every run this process starts, so turning them on is one
    # command rather than an edit to what the dashboard posts.
    ap.add_argument("--self-consistency", type=int, default=None, metavar="N",
                    help="sample the local model N times per field and escalate "
                         "on disagreement (default 1 = off; catches variance, "
                         "not a bias the model repeats every time)")
    ap.add_argument("--shadow-external", action="store_true",
                    help="also ask the model for fields the EX-107 exhibit "
                         "already answered, and record whether it agreed (#164)")
    ap.add_argument("--candidate-select", action="store_true",
                    help="enumerate the plausible values in code and ask the "
                         "model for an INDEX, so a returned value is one the "
                         "filing states (#177/#178)")
    args = ap.parse_args()

    # argparse -> env, which is where accuracy_switches() reads its defaults, so
    # a per-request value can still override a process-wide one.
    if args.self_consistency is not None:
        os.environ["SCRUBBER_SELF_CONSISTENCY"] = str(max(1, args.self_consistency))
    if args.shadow_external:
        os.environ["SCRUBBER_SHADOW_EXTERNAL"] = "1"
    if args.candidate_select:
        os.environ["SCRUBBER_CANDIDATE_SELECT"] = "1"

    # The Windows console is cp1252 by default; a non-ASCII byte in a startup
    # banner should never be what stops a server from running.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    mimetypes.add_type("application/javascript", ".js")
    handler = partial(Handler, directory=str(REPO_ROOT))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)

    h = health()
    print(f"Trader Screener tools  ->  http://{args.host}:{args.port}/")
    print(f"  ollama            : {'ok' if h['ollama']['ok'] else 'NOT READY'} "
          f"({h['ollama']['model']}){'' if h['ollama']['ok'] else ' -- ' + str(h['ollama']['detail'])}")
    print(f"  EDGAR_USER_AGENT  : {'set' if h['edgar_user_agent'] else 'MISSING (live runs will fail)'}")
    print(f"  local store       : {h['store']['path']} "
          f"({'exists' if h['store']['exists'] else 'not created yet'})")
    _s, _sh, _cs = accuracy_switches({})
    print(f"  self-consistency  : {_s} sample(s)"
          f"{'' if _s > 1 else '  (off -- gate signal inert at 1)'}")
    print(f"  external shadow   : {'ON' if _sh else 'off'}")
    print(f"  candidate-select  : {'ON' if _cs else 'off'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
