"""
Gate for zero-token guidance anchors (issue #187). Same convention as
`test_candidates.py` / `test_normalize.py`: stdlib only, run directly, exit
0 = pass. Runs OFFLINE against three checked-in redacted press-release
fixtures under `fixtures/`.

Every acceptance criterion in #187 is checked here:

  * `SECTION_ANCHORS`, `SENTENCE_ANCHORS`, `RANGE_PATTERNS` are named module
    constants;
  * `find_candidates(text)` returns `{sentence, span, anchor}` records whose
    span, sliced out of the input, is exactly the sentence;
  * a sentence inside a PSLRA safe-harbour paragraph is excluded even when it
    carries a verb anchor and a range;
  * a document with no anchor hits returns an empty list and does not raise;
  * the count and per-anchor hit table come back alongside the records;
  * this gate runs offline against three redacted fixtures.

Run:  python tools/edgar_scrubber/test_guidance_anchors.py
"""
from pathlib import Path

import guidance_anchors as ga

FIX = Path(__file__).resolve().parent / "fixtures"

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def spans_slice_to_sentence(text, cands):
    return all(text[c["span"][0]:c["span"][1]] == c["sentence"] for c in cands)


# --------------------------------------------------------------------------- #
section("the three anchor tables are named module constants, each entry commented")
# --------------------------------------------------------------------------- #
check("SECTION_ANCHORS is a non-empty dict", isinstance(ga.SECTION_ANCHORS, dict) and ga.SECTION_ANCHORS)
check("SENTENCE_ANCHORS is a non-empty dict", isinstance(ga.SENTENCE_ANCHORS, dict) and ga.SENTENCE_ANCHORS)
check("RANGE_PATTERNS is a non-empty dict", isinstance(ga.RANGE_PATTERNS, dict) and ga.RANGE_PATTERNS)

# --------------------------------------------------------------------------- #
section("acme fixture: outlook section + verb anchors, boilerplate excluded")
# --------------------------------------------------------------------------- #
acme = load("guidance_acme_q3.txt")
res = ga.find_candidates(acme)
check("three guidance sentences found", res.count == 3)
check("count equals len(candidates)", res.count == len(res.candidates))
check("every record has sentence, span and anchor",
      all({"sentence", "span", "anchor"} <= set(c) for c in res.candidates))
check("every span slices back to its sentence", spans_slice_to_sentence(acme, res.candidates))
check("the $1.20-$1.30 billion guidance is a candidate",
      any("$1.20 billion to $1.30 billion" in c["sentence"] for c in res.candidates))
check("the between $4.10 and $4.30 guidance is a candidate",
      any("between $4.10 and $4.30" in c["sentence"] for c in res.candidates))
check("the 18% to 20% margin guidance is a candidate",
      any("18% to 20%" in c["sentence"] for c in res.candidates))
check("the boilerplate $9.00-$9.50 billion sentence is EXCLUDED, though it has a verb anchor and a range",
      not any("$9.00 billion to $9.50 billion" in c["sentence"] for c in res.candidates))
check("per-anchor hit table returned, keyed by every defined anchor",
      set(res.anchor_hits) ==
      set(ga.SECTION_ANCHORS) | set(ga.SENTENCE_ANCHORS) | set(ga.RANGE_PATTERNS))
check("the outlook section anchor registered hits", res.anchor_hits["outlook"] >= 3)
check("hit counts sum to at least the candidate count", sum(res.anchor_hits.values()) >= res.count)

# --------------------------------------------------------------------------- #
section("globex fixture: no heading, sentence anchors alone carry it")
# --------------------------------------------------------------------------- #
globex = load("guidance_globex_fy.txt")
gres = ga.find_candidates(globex)
check("two guidance sentences found with no outlook heading present", gres.count == 2)
check("every span slices back to its sentence", spans_slice_to_sentence(globex, gres.candidates))
check("the 8% to 10% revenue-growth guidance is a candidate",
      any("8% to 10%" in c["sentence"] for c in gres.candidates))
check("the single-paragraph safe-harbour statement produced no candidate",
      not any("no obligation to update" in c["sentence"] for c in gres.candidates))

# --------------------------------------------------------------------------- #
section("initech fixture: historical results only -> empty list, no raise")
# --------------------------------------------------------------------------- #
initech = load("guidance_initech_results.txt")
ires = ga.find_candidates(initech)
check("a release with no forward-looking numbers returns zero candidates", ires.count == 0)
check("empty candidate list, not None", ires.candidates == [])
check("all-zero hit table on an empty result", set(ires.anchor_hits.values()) == {0})

# --------------------------------------------------------------------------- #
section("degenerate inputs do not raise")
# --------------------------------------------------------------------------- #
check("empty string returns zero candidates", ga.find_candidates("").count == 0)
check("whitespace-only returns zero candidates", ga.find_candidates("   \n\n  ").count == 0)
check("a lone number sentence with no anchor is not a candidate",
      ga.find_candidates("Net income rose to $5 million.").count == 0)

# --------------------------------------------------------------------------- #
section("no model, no network")
# --------------------------------------------------------------------------- #
_src = Path(ga.__file__).read_text(encoding="utf-8")
_forbidden = ("requests", "urllib", "http.client", "httpx", "socket",
              "ollama_client", "edgar_client")
check("module source imports no network or model library",
      not any(f"import {m}" in _src or f"from {m}" in _src for m in _forbidden))

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nguidance_anchors gate: PASS")
