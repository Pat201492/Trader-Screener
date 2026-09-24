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
import normalize

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


def load_normalized(name):
    """Real committed release fixtures are raw EX-99.1 HTML; `find_candidates`
    runs on the NORMALIZED text, exactly as the live chain feeds it."""
    return normalize.normalize_html((FIX / name).read_text(encoding="utf-8")).text


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
section("#225: 'Outlook' section anchor and passive-voice sentence anchors")
# --------------------------------------------------------------------------- #
check("'Outlook' is a recognised section anchor",
      ga._section_of_heading("Outlook") == "outlook")
check("'Financial Outlook' is a recognised section anchor",
      ga._section_of_heading("Financial Outlook") == "outlook")
check("'Guidance' remains a recognised section anchor",
      ga._section_of_heading("Full-Year Guidance") == "guidance_heading")
check("passive 'is expected to be' is a recognised sentence anchor",
      ga.SENTENCE_ANCHORS["expected_to_be"].search("Revenue is expected to be $1 billion"))
check("passive 'are expected to be' is a recognised sentence anchor",
      ga.SENTENCE_ANCHORS["expected_to_be"].search("margins are expected to be 74%"))
# A passive sentence with a number, no heading in scope, is still a candidate.
_passive = ("ACME CORP RESULTS\n\n"
            "Revenue is expected to be between $4.10 and $4.30 for the full year.")
check("a passive sentence alone carries a candidate with no section heading",
      ga.find_candidates(_passive).count == 1)

# --------------------------------------------------------------------------- #
section("#225: point-estimate-with-tolerance converts to {low, high}")
# --------------------------------------------------------------------------- #
_tr = ga.tolerance_range("Revenue is expected to be $108.0 billion, plus or minus 2%.")
check("'$108.0 billion plus or minus 2%' low is 105.84e9",
      _tr is not None and abs(_tr["low"] - 105.84e9) < 1.0)
check("'$108.0 billion plus or minus 2%' high is 110.16e9",
      _tr is not None and abs(_tr["high"] - 110.16e9) < 1.0)

_bp = ga.tolerance_range("gross margins are expected to be 74.0%, plus or minus 50 basis points.")
check("'74.0% plus or minus 50 basis points' -> 73.5 to 74.5 (absolute pp)",
      _bp is not None and _bp["low"] == 73.5 and _bp["high"] == 74.5)
_pct = ga.tolerance_range("74.0%, plus or minus 50 percent")
check("'74.0% plus or minus 50 percent' -> 37.0 to 111.0 (relative), NOT 73.5/74.5",
      _pct is not None and _pct["low"] == 37.0 and _pct["high"] == 111.0
      and _pct != _bp)
check("prose with no tolerance phrasing yields None",
      ga.tolerance_range("Revenue grew to $5 million.") is None)
check("a tolerance sentence is a numeric candidate on its own",
      "tolerance" in [n for n, p in ga.RANGE_PATTERNS.items()
                      if p.search("$108.0 billion, plus or minus 2%")])

# --------------------------------------------------------------------------- #
section("#225: the three committed REAL fixtures -- CSCO / NVDA / AAPL")
# --------------------------------------------------------------------------- #
csco = ga.find_candidates(load_normalized("guidance_csco_20260812.txt"))
nvda = ga.find_candidates(load_normalized("guidance_nvda_20260826.txt"))
aapl = ga.find_candidates(load_normalized("guidance_aapl_20260730.txt"))

check("CSCO fixture yields its six numeric-guidance candidates", csco.count == 6)
check("CSCO 'Revenue: $18.0 billion - $18.2 billion' is a candidate",
      any("$18.0 billion - $18.2 billion" in c["sentence"] for c in csco.candidates))
check("CSCO GAAP EPS $1.08 to $1.10 is a candidate",
      any("$1.08 to $1.10" in c["sentence"] for c in csco.candidates))

check("NVDA fixture now yields at least one candidate (was zero)", nvda.count >= 1)
check("NVDA candidate carries the $108.0 billion revenue outlook",
      any("$108.0 billion" in c["sentence"] for c in nvda.candidates))
check("NVDA revenue candidate converts to the 105.84e9-110.16e9 range",
      any((tr := ga.tolerance_range(c["sentence"])) is not None
          and abs(tr["low"] - 105.84e9) < 1.0 and abs(tr["high"] - 110.16e9) < 1.0
          for c in nvda.candidates if "$108.0 billion" in c["sentence"]))

check("AAPL fixture still yields zero (no numeric guidance)", aapl.count == 0)

# Per-anchor hit counts across all three real fixtures, summed. Locked so a
# future anchor change that silently drops coverage on real filings fails here.
real_totals = {name: 0 for group in
               (ga.SECTION_ANCHORS, ga.SENTENCE_ANCHORS, ga.RANGE_PATTERNS)
               for name in group}
for res_ in (csco, nvda, aapl):
    for name, n in res_.anchor_hits.items():
        real_totals[name] += n
EXPECTED_REAL_TOTALS = {
    "guidance_heading": 6,
    "for_period": 2,
    "dollar_range": 6,
    "percent_range": 2,
    "outlook": 2,
    "expected_to_be": 2,
    "tolerance": 2,
}
print("  per-anchor hit totals across CSCO/NVDA/AAPL:")
for name in sorted(real_totals):
    print(f"    {name:<22} {real_totals[name]}")
check("per-anchor hit totals across the three real fixtures match the locked table",
      {k: v for k, v in real_totals.items() if v} == EXPECTED_REAL_TOTALS)

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
