"""
Offline end-to-end gate for the guidance chain (issue #215, EPIC #95).

Proves #185/#187/#188/#189 COMPOSE on committed, real-shaped SEC input, with no
network and no model. The chain, driven here on the committed fixtures:

  1. LOCATE the earnings release (#185, `earnings_releases`) -- from a committed
     submissions history + accession manifest, via metadata alone, pick the
     Item-2.02 8-K filings and their EX-99 press-release exhibit.
  2. FIND candidates (#187, `guidance_anchors`) -- reduce the located release body
     to the handful of forward-looking sentences that carry a number.
  3. EXTRACT guidance (#188, `guidance_extract`) -- turn those sentences into a
     `GuidanceRecord` through the ladder, the model call standing in for a
     `FakeChatClient` scripted per issuer so NO model is required.
  4. JOIN to a reported actual (#189, `guidance_join`) -- match the guided period
     against reported XBRL facts (loaded through the real `facts_from_concept`
     ingest path) and score it.

Same convention as the sibling gates (`test_guidance_extract.py`): stdlib only,
run directly, exit 0 = pass, no network. The one thing that would reach a network
-- a real model call -- is a `FakeChatClient` returning a scripted payload in the
exact shape `ollama_client.OllamaClient.chat_completion` returns, so the extractor
cannot tell it from a live local/Claude call.

Run:  python tools/edgar_scrubber/test_guidance_e2e.py
"""
import json
import os
from pathlib import Path

import guidance_extract as ge
from guidance_anchors import (find_candidates, SECTION_ANCHORS, SENTENCE_ANCHORS,
                              RANGE_PATTERNS)
from guidance_join import join_guidance_to_actual, NO_ACTUAL_YET
from earnings_releases import earnings_release_filings, press_release_document
from revenue_series import facts_from_concept
from edgar_client import cik10
from facts_store import FactsStore
from extraction_ladder import WIRE_VALUE_KEY, WIRE_SPAN_KEY, WIRE_CONF_KEY

FIX = Path(__file__).resolve().parent / "fixtures"
failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def load_text(name):
    return (FIX / name).read_text(encoding="utf-8")


def load_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Offline by construction: neither live-mode switch is read, and nothing here
# builds a real EdgarClient or OllamaClient. Pop the switches so the run is
# hermetic regardless of the caller's environment (#215 AC5).
# --------------------------------------------------------------------------- #
for _var in ("EDGAR_LIVE", "EDGAR_USER_AGENT"):
    os.environ.pop(_var, None)


class FakeChatClient:
    """Same shape as OllamaClient.chat_completion -- no network. Returns the
    scripted payload every call and records the messages it was handed, so the
    extractor cannot tell it from a real local/Claude call."""

    def __init__(self, payload, usage=None):
        self.payload = payload
        self.usage = usage or {"prompt_tokens": 180, "completion_tokens": 24}
        self.calls = []

    def chat_completion(self, messages, model=None, temperature=None,
                        max_tokens=None, top_p=None, response_format=None):
        self.calls.append({"messages": messages, "model": model})
        return {"choices": [{"message": {"content": json.dumps(self.payload)}}],
                "usage": self.usage}


class FakeEdgarClient:
    """Serves the committed submissions history, a fixed accession manifest, and
    a per-accession release body -- the same three reads `earnings_releases` and
    the body fetch make against a live `EdgarClient`, off committed bytes."""

    def __init__(self, submissions, bodies):
        self._submissions = submissions
        self._bodies = bodies  # accession -> body text

    def submissions(self, cik):
        return self._submissions

    def filing_index(self, cik, accession):
        # A minimal manifest carrying the primary 8-K and its EX-99.1 exhibit;
        # `earnings_releases` recognises the release from the EX-99 filename.
        return {"directory": {"item": [
            {"name": "d_8k.htm", "type": "text.gif", "size": "9000"},
            {"name": "ex99-1.htm", "type": "text.gif", "size": "12000"},
        ]}}

    def archive_document(self, cik, accession, filename):
        return self._bodies[accession].encode("utf-8")


def _entry(value, span, conf=0.9):
    """One wire-schema field entry in the ladder's {value, span, confidence}
    shape. `span` is the offset pair into the source document (#106/#111 offset
    convention) -- taken from a real candidate's span below, never invented."""
    e = {WIRE_VALUE_KEY: value, WIRE_SPAN_KEY: list(span) if span else None}
    if conf is not None:
        e[WIRE_CONF_KEY] = conf
    return e


def guidance_payload(*, metric, period_label, low, high, span, basis=None):
    """A scripted extraction payload. Every field points its span at the SAME
    guidance sentence (the offsets `guidance_anchors` reported for it), so the
    recovered record's source offsets land inside that sentence."""
    return {
        "metric": _entry(metric, span),
        "period_label": _entry(period_label, span),
        "low": _entry(low, span),
        "high": _entry(high, span),
        "basis": _entry(basis, span if basis else None, conf=0.5),
    }


# --------------------------------------------------------------------------- #
# The committed submissions history is a single filer (CIK 12345) with several
# earnings 8-Ks. We attach one committed release body per Item-2.02 accession:
# two carry numeric guidance, one carries none.
# --------------------------------------------------------------------------- #
SUBMISSIONS = load_json("earnings_submissions.json")
CIK = SUBMISSIONS["cik"]
# The store and the join key on the zero-padded CIK10 (`facts_from_concept` and
# `join_guidance_to_actual` both pad), so guidance must be written under it too.
CIK_PADDED = cik10(CIK)

# The three Item-2.02 accessions in the committed history, oldest first.
RESULTS_ACCESSIONS = [
    "0000012345-26-000001",
    "0000012345-26-000002",
    "0000012345-26-000003",
]

# accession -> (fixture file, expected extraction). Initech carries no numeric
# guidance, so it has no scripted payload -- the chain must yield zero records.
ACME_ACCN, GLOBEX_ACCN, INITECH_ACCN = RESULTS_ACCESSIONS

BODIES = {
    ACME_ACCN: load_text("guidance_acme_q3.txt"),
    GLOBEX_ACCN: load_text("guidance_globex_fy.txt"),
    INITECH_ACCN: load_text("guidance_initech_results.txt"),
}

# What each release's numeric guidance is, and the marker text of the sentence it
# is taken from -- used both to script the model and to assert the recovered span
# lands inside that sentence.
GUIDANCE_FIXTURES = {
    ACME_ACCN: {
        "name": "ACME (Q4 revenue range)",
        "sentence_marker": "$1.20 billion to $1.30 billion",
        "metric": "revenue", "period_label": "Q4 fiscal 2026",
        "low": 1.20, "high": 1.30, "basis": None,
    },
    GLOBEX_ACCN: {
        "name": "Globex (full-year revenue growth range)",
        "sentence_marker": "8% to 10%",
        "metric": "revenue", "period_label": "FY2026",
        "low": 8.0, "high": 10.0, "basis": None,
    },
}
NO_GUIDANCE_ACCESSIONS = {INITECH_ACCN: "Initech (historical results only)"}


def candidate_span_for(text, marker):
    """The (start, end) span `guidance_anchors` reports for the candidate
    sentence containing `marker` -- the offsets a real model would be handed and
    would echo back in its span slot."""
    for c in find_candidates(text).candidates:
        if marker in c["sentence"]:
            return c["span"]
    return None


# --------------------------------------------------------------------------- #
section("step 1 -- locate the earnings releases from committed metadata (#185)")
# --------------------------------------------------------------------------- #
client = FakeEdgarClient(SUBMISSIONS, BODIES)
filings = earnings_release_filings(client, CIK)
located = {f["accession"]: f for f in filings}

check("every Item-2.02 accession was located as an earnings release",
      all(a in located for a in RESULTS_ACCESSIONS))
check("each located release resolves to an EX-99 press-release exhibit",
      all(press_release_document(located[a]) is not None for a in RESULTS_ACCESSIONS))
check("the release exhibit is EX-99.1 by convention",
      press_release_document(located[ACME_ACCN])["name"] == "ex99-1.htm")

# --------------------------------------------------------------------------- #
section("steps 2-4 -- find, extract and join, per located release")
# --------------------------------------------------------------------------- #
store = FactsStore(":memory:")
anchor_totals = {name: 0 for group in
                 (SECTION_ANCHORS, SENTENCE_ANCHORS, RANGE_PATTERNS)
                 for name in group}


def run_chain(accession, expect_payload):
    """Locate body -> find candidates -> extract -> return (record, candidates).
    Folds this release's per-anchor hits into `anchor_totals`."""
    exhibit = press_release_document(located[accession])
    body = client.archive_document(CIK, accession, exhibit["name"]).decode("utf-8")

    found = find_candidates(body)
    for name, n in found.anchor_hits.items():
        anchor_totals[name] += n

    payload = expect_payload if expect_payload is not None else guidance_payload(
        metric="revenue", period_label="none", low=0, high=0, span=(0, 1))
    extractor = ge.GuidanceExtractor(
        local_client=FakeChatClient(payload), local_model="qwen2.5:7b",
        store=store)
    rec = extractor.extract(body, cik=CIK_PADDED, issuer=str(CIK),
                            accession=accession, document=exhibit["name"])
    return rec, found, body


# -- the two releases that carry numeric guidance --------------------------- #
for accession, fx in GUIDANCE_FIXTURES.items():
    span = candidate_span_for(BODIES[accession], fx["sentence_marker"])
    payload = guidance_payload(metric=fx["metric"], period_label=fx["period_label"],
                               low=fx["low"], high=fx["high"], span=span,
                               basis=fx["basis"])
    rec, found, body = run_chain(accession, payload)

    check(f"{fx['name']}: a numeric range was recovered",
          rec is not None and rec.low == fx["low"] and rec.high == fx["high"])
    check(f"{fx['name']}: source offsets land inside the guidance sentence",
          rec is not None and rec.span is not None
          and fx["sentence_marker"] in body[rec.span[0]:rec.span[1]])
    check(f"{fx['name']}: the recovered span is one the anchors located",
          rec is not None and rec.span == span)

# -- the release that carries no numeric guidance (#215 AC3) ---------------- #
for accession, name in NO_GUIDANCE_ACCESSIONS.items():
    rec, found, body = run_chain(accession, None)
    check(f"{name}: the anchors find no candidate sentence", found.count == 0)
    check(f"{name}: the chain invents no guidance record", rec is None)

check("no-guidance issuer contributes zero guidance records to the store",
      len(store.guidance_for(CIK_PADDED)) == 2)

# --------------------------------------------------------------------------- #
section("step 4 -- join guidance to reported actuals (#189)")
# --------------------------------------------------------------------------- #
# Reported actuals for the two guided periods, ingested through the REAL
# `facts_from_concept` path from a companyconcept-shaped payload (the same shape
# the committed `revenue_rfcc_320193.json` fixture carries). The committed XBRL
# fixtures cover Apple's past fiscal years, which no fixture release guides; these
# actuals are the reported counterparts of the guided FY2026 periods so the join
# has something to resolve against.
actuals_concept = {
    "taxonomy": "us-gaap",
    "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "units": {"USD": [
        # Globex full-year FY2026 (annual duration) -- matches "fiscal 2026".
        {"start": "2025-10-01", "end": "2026-09-30", "val": 9_400_000_000,
         "accn": "0000012345-26-000200", "fy": 2026, "fp": "FY", "form": "10-K"},
        # ACME Q4 FY2026 (quarter duration, fp Q4) -- matches "Q4 fiscal 2026".
        {"start": "2026-07-01", "end": "2026-09-30", "val": 1_280_000_000,
         "accn": "0000012345-26-000201", "fy": 2026, "fp": "Q4", "form": "10-Q"},
    ]},
}
store.put_facts(facts_from_concept(CIK, actuals_concept))

joins = {(j.metric, j.period_label): j for j in join_guidance_to_actual(store, CIK)}
check("both guided periods produced a join", len(joins) == 2)
acme_join = joins.get(("revenue", "Q4 fiscal 2026"))
globex_join = joins.get(("revenue", "FY2026"))
check("the ACME quarter guidance resolved to a reported actual",
      acme_join is not None and acme_join.actual is not None
      and acme_join.verdict != NO_ACTUAL_YET)
check("the Globex full-year guidance resolved to a reported actual",
      globex_join is not None and globex_join.actual is not None
      and globex_join.verdict != NO_ACTUAL_YET)

# The committed real-XBRL fixture ingests through the same path.
apple_facts = facts_from_concept("0000320193", load_json("revenue_rfcc_320193.json"))
check("the committed Apple companyconcept fixture ingests to reported facts",
      len(apple_facts) > 0)

# --------------------------------------------------------------------------- #
section("anchor coverage across the committed fixtures (#215 AC4)")
# --------------------------------------------------------------------------- #
print("  per-anchor hit counts (all committed release fixtures):")
for name in sorted(anchor_totals):
    print(f"    {name:<22} {anchor_totals[name]}")
check("at least one anchor fired across the committed fixtures",
      sum(anchor_totals.values()) > 0)

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nguidance_e2e gate: PASS")
