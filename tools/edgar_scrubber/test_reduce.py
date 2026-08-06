"""
Gate for stages 2-4 of the #101 four-stage reduction (boilerplate dedup,
section split, sub-block routing). Same convention as the other
`test_*.py` gates: stdlib only, no network, run directly, exit 0 = pass.

Every #101 acceptance criterion these stages own is checked here:

  * boilerplate dedup reports MEASURED reduction per issuer (not just "some
    paragraphs removed" -- an actual before/after char count);
  * section splitter validated across FOUR issuers (JPM, Citi, GS, Barclays)
    that share no heading vocabulary -- a splitter tuned on one issuer's
    headings must not quietly fail on the others;
  * a field's section text never leaks content from a DIFFERENT section
    (the "barrier pulled from Hypothetical Examples instead of Key Terms"
    failure mode #101 calls out is structurally impossible);
  * every span survives resolution back to exact source offsets THROUGH ALL
    FOUR STAGES chained together (normalize -> boilerplate strip -> section
    split -> sub-block).

Run:  python tools/edgar_scrubber/test_reduce.py
"""
import reduce as rd
from normalize import normalize_html

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
section("stage 2: boilerplate dedup -- measured reduction per issuer")
# --------------------------------------------------------------------------- #

BOILERPLATE = (
    "<p>The notes are unsecured and unsubordinated debt obligations. "
    "The notes are not bank deposits and are not insured by the FDIC "
    "or any other governmental agency.</p>"
)

issuer_docs = [
    normalize_html(f"<html><body><h2>Key Terms</h2><p>Issuer: Filer {i}.</p>{BOILERPLATE}"
                    f"<p>Filing-specific note {i} detail.</p></body></html>")
    for i in range(5)
]
model = rd.build_boilerplate_model("ACME", issuer_docs, threshold=0.8)
# both the FDIC boilerplate paragraph AND the identical "Key Terms" heading
# are legitimately present in 5/5 docs -- boilerplate detection is about
# frequency, not about guessing which paragraphs are "supposed to" repeat.
check("boilerplate paragraph identified (present in 5/5 docs)",
      any("FDIC" in model.sample_text[h] for h in model.boilerplate_hashes))
check("doc_count recorded", model.doc_count == 5)

reduced0 = rd.strip_boilerplate(issuer_docs[0], model)
check("report carries a measured reduction_pct", reduced0.report.reduction_pct > 0)
check("boilerplate text removed from output", "FDIC" not in reduced0.text)
check("filing-specific text kept", "Filing-specific note 0 detail." in reduced0.text)
print(f"  measured: {reduced0.report.original_chars} -> {reduced0.report.kept_chars} chars "
      f"({reduced0.report.reduction_pct:.1f}% reduction)")

# a single-document corpus has nothing to compare against -- must not treat
# every paragraph as boilerplate.
solo_model = rd.build_boilerplate_model("SOLO", [issuer_docs[0]], threshold=0.8)
check("single-doc corpus produces zero boilerplate hashes", len(solo_model.boilerplate_hashes) == 0)

# a paragraph repeated WITHIN one document must not count as cross-filing.
repeat_doc = normalize_html(
    "<html><body><p>Repeated line.</p><p>Repeated line.</p><p>Repeated line.</p>"
    "<p>Only in doc A.</p></body></html>"
)
other_doc = normalize_html("<html><body><p>Only in doc B.</p></body></html>")
repeat_model = rd.build_boilerplate_model("X", [repeat_doc, other_doc], threshold=0.8)
check("in-doc repetition doesn't fake cross-filing frequency (needs 2/2, has 1/2)",
      len(repeat_model.boilerplate_hashes) == 0)

# --------------------------------------------------------------------------- #
section("stage 3: section split across FOUR issuers with disjoint heading vocab")
# --------------------------------------------------------------------------- #

def build_issuer_doc(issuer, key_terms_h, coupon_h, call_h, downside_h, ev_h, hyp_h,
                      barrier_pct, coupon_pct):
    html = f"""
    <html><body>
    <p>PRICING SUPPLEMENT dated January 15, 2026 -- {issuer}</p>
    <h2>{key_terms_h}</h2>
    <p>Issuer: {issuer}. CUSIP: 00000AAA0. Underlying: S&amp;P 500 Index.</p>
    <h2>{coupon_h}</h2>
    <p>The notes pay a contingent rate of {coupon_pct}% per annum if the underlying
    closes at or above the coupon barrier on the related observation date.</p>
    <h2>{call_h}</h2>
    <p>The notes will be called automatically if the underlying closes at or above
    the initial value on any potential call observation date.</p>
    <h2>{downside_h}</h2>
    <p>If the notes are not called and the final value is below the barrier level of
    {barrier_pct}%, investors are exposed to the downside performance of the underlying.</p>
    <h2>{hyp_h}</h2>
    <p>In this hypothetical example only, assume a barrier of 45.00% purely for
    illustration -- this number does not describe the actual barrier above.</p>
    <h2>{ev_h}</h2>
    <p>Our estimated value of the notes is $968.10 per $1,000 stated principal amount.</p>
    <h2>Calculation Agent</h2>
    <p>{issuer} will act as calculation agent.</p>
    </body></html>
    """
    return normalize_html(html)


ISSUERS = {
    "JPM": dict(key_terms_h="Key Terms", coupon_h="Contingent Coupon",
                call_h="Automatic Call", downside_h="Buffer Amount",
                ev_h="Estimated Value of the Notes", hyp_h="Hypothetical Payout Profile",
                barrier_pct="70.00", coupon_pct="9.15"),
    "Citi": dict(key_terms_h="Summary of Terms", coupon_h="Interest Payment",
                 call_h="Redemption Feature", downside_h="Principal at Risk",
                 ev_h="Estimated Value", hyp_h="Hypothetical Examples",
                 barrier_pct="65.00", coupon_pct="8.40"),
    "GS": dict(key_terms_h="General Terms", coupon_h="Coupon Payment",
               call_h="Call Feature", downside_h="Downside Scenario",
               ev_h="Determining the Estimated Value", hyp_h="Hypothetical Return Scenarios",
               barrier_pct="60.00", coupon_pct="10.25"),
    "Barclays": dict(key_terms_h="Indicative Terms", coupon_h="Contingent Interest Rate",
                      call_h="Autocall Feature", downside_h="Barrier Event",
                      ev_h="Estimated Value of the Securities", hyp_h="Hypothetical Payout",
                      barrier_pct="72.50", coupon_pct="9.80"),
}

REQUIRED_SECTIONS = ("cover", "key_terms", "coupon_terms", "call_terms",
                      "downside_terms", "hypothetical_examples", "estimated_value",
                      "calculation_agent")

issuer_sections = {}
for issuer, cfg in ISSUERS.items():
    doc = build_issuer_doc(issuer, **cfg)
    sections = rd.split_sections(doc)
    issuer_sections[issuer] = (doc, sections, cfg)

    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    check(f"{issuer}: every canonical section detected despite unique heading vocab "
          f"({'; '.join(f'{k}={v!r}' for k, v in cfg.items() if k.endswith('_h'))})",
          not missing)
    if missing:
        print(f"    missing: {missing}, found: {sorted(sections)}")

    downside_text = rd.text_for_sections(doc, sections, ["downside_terms"])
    check(f"{issuer}: downside_terms does NOT leak the Hypothetical Examples' 45.00% figure",
          "45.00" not in downside_text)
    check(f"{issuer}: downside_terms DOES contain its own barrier figure ({cfg['barrier_pct']}%)",
          cfg["barrier_pct"] in downside_text)

    coupon_text = rd.text_for_sections(doc, sections, ["coupon_terms"])
    check(f"{issuer}: coupon_terms contains its own coupon rate ({cfg['coupon_pct']}%)",
          cfg["coupon_pct"] in coupon_text)
    check(f"{issuer}: coupon_terms does not contain the estimated value figure",
          "968.10" not in coupon_text)

    ev_span = sections["estimated_value"][0]
    src = doc.source[ev_span.source_span[0]:ev_span.source_span[1]]
    check(f"{issuer}: estimated_value section span resolves to source containing 968.10",
          "968.10" in src)

# --------------------------------------------------------------------------- #
section("sections_for_spec -- bridges to a field_spec's own section vocabulary")
# --------------------------------------------------------------------------- #

try:
    import field_spec as fs
    specs = fs.load_specs()
    note_spec = specs["structured_note"]
    doc, sections, _ = issuer_sections["JPM"]
    routed = rd.sections_for_spec(sections, note_spec.sections)
    check("routed sections are a subset of the spec's own vocabulary",
          set(routed).issubset(set(note_spec.sections)))
    check("coupon_terms (in spec vocab) survives routing", "coupon_terms" in routed)
except ImportError:
    print("  [skip] field_spec not importable standalone here")

# --------------------------------------------------------------------------- #
section("stage 4: sub-block routing (warm path)")
# --------------------------------------------------------------------------- #

doc, sections, cfg = issuer_sections["JPM"]
idx = doc.text.find(f"{cfg['coupon_pct']}%")
block = rd.sub_block(doc, text_offset=idx, window=30)
check("sub_block text contains the anchor value", f"{cfg['coupon_pct']}%" in block.text)
check("sub_block source_span resolves to source text containing the value",
      cfg["coupon_pct"] in doc.source[block.source_span[0]:block.source_span[1]])

try:
    rd.sub_block(doc, window=30)
    check("sub_block rejects missing anchor", False)
except ValueError:
    check("sub_block rejects missing anchor", True)

try:
    rd.sub_block(doc, text_offset=0, source_offset=0, window=30)
    check("sub_block rejects BOTH anchors given at once", False)
except ValueError:
    check("sub_block rejects BOTH anchors given at once", True)

# --------------------------------------------------------------------------- #
section("end-to-end: a span survives ALL FOUR STAGES back to exact source offsets")
# --------------------------------------------------------------------------- #

# Build a two-filing "corpus" for one issuer so stage 2 has something to
# learn boilerplate from, then run every remaining stage on filing A and
# confirm a value pulled from deep in the pipeline (sub-block, inside a
# section, inside boilerplate-stripped text) still resolves to its own exact
# spot in the ORIGINAL raw HTML.
shared_boiler = ("<p>These securities involve risks not associated with an investment "
                  "in conventional debt securities. See Risk Factors.</p>")

filing_a_html = f"""
<html><body>
<p>PRICING SUPPLEMENT -- Filing A</p>
{shared_boiler}
<h2>Key Terms</h2>
<p>Issuer: JPMorgan Chase Financial Company LLC.</p>
{shared_boiler}
<h2>Contingent Coupon</h2>
<p>The contingent coupon rate is 9.15% per annum, payable if the underlying
closes at or above the coupon barrier.</p>
</body></html>
"""
filing_b_html = f"""
<html><body>
<p>PRICING SUPPLEMENT -- Filing B</p>
{shared_boiler}
<h2>Key Terms</h2>
<p>Issuer: JPMorgan Chase Financial Company LLC.</p>
{shared_boiler}
<h2>Contingent Coupon</h2>
<p>The contingent coupon rate is 7.25% per annum, payable if the underlying
closes at or above the coupon barrier.</p>
</body></html>
"""

doc_a = normalize_html(filing_a_html)
doc_b = normalize_html(filing_b_html)
corpus_model = rd.build_boilerplate_model("JPM", [doc_a, doc_b], threshold=0.8)
check("shared boilerplate paragraph learned from the 2-filing corpus",
      len(corpus_model.boilerplate_hashes) >= 1)

# stage 2
stripped = rd.strip_boilerplate(doc_a, corpus_model)
check("stage2: boilerplate gone from filing A", "Risk Factors" not in stripped.text)
check("stage2: filing-specific coupon text survives", "9.15%" in stripped.text)

# stage 3, run on stage 2's output
sections = rd.split_sections(stripped)
check("stage3: coupon_terms found on the boilerplate-stripped document", "coupon_terms" in sections)
coupon_span = sections["coupon_terms"][0]
recovered = doc_a.source[coupon_span.source_span[0]:coupon_span.source_span[1]]
check("stage3 (post-stage2) section span still resolves into filing A's ORIGINAL source",
      "9.15" in recovered)

# stage 4, sub-block on the reduced document
idx = stripped.text.find("9.15%")
block = rd.sub_block(stripped, text_offset=idx, window=25)
check("stage4 (post-stage2+3 chain) sub_block contains the value", "9.15%" in block.text)
final_source = doc_a.source[block.source_span[0]:block.source_span[1]]
check("stage4 span resolves ALL THE WAY back to filing A's original source bytes",
      "9.15" in final_source)
check("the recovered source range is NOT the whole document (reduction actually narrowed it)",
      block.source_span[1] - block.source_span[0] < len(filing_a_html))

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nreduce self-check + gate: PASS")
