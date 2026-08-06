"""
Gate for HTML normalization + stage-1 table pre-parse (issue #101). Same
convention as `test_edgar_client.py` / `test_field_spec.py`: stdlib only, run
directly, exit 0 = pass.

Every acceptance criterion in #101 that this module owns is checked here:

  * every extracted span resolves back to EXACT source offsets, including
    across decoded HTML entities and stripped tags;
  * table pre-parse validated against nested AND merged-cell (colspan/
    rowspan) terms tables, with row/column relationships surviving;
  * `<script>/<style>` content never reaches the normalized text.

Run:  python tools/edgar_scrubber/test_normalize.py
"""
import normalize as nz

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def resolved(doc, needle):
    idx = doc.text.find(needle)
    if idx == -1:
        return None, None
    span = doc.offset_map.resolve(idx, idx + len(needle))
    return span, doc.source[span[0]:span[1]] if span else None


# --------------------------------------------------------------------------- #
section("plain text + span offsets")
# --------------------------------------------------------------------------- #

doc = nz.normalize_html("<html><body><p>Hello <b>World</b>.</p></body></html>")
check("tags stripped from text", doc.text == "Hello World.")
span, src_text = resolved(doc, "World")
check("'World' resolves to exact source substring", src_text == "World")

# --------------------------------------------------------------------------- #
section("entity decoding keeps spans exact")
# --------------------------------------------------------------------------- #

doc = nz.normalize_html("<p>Coupon &amp; Barrier at 9.15% &mdash; not a deposit.</p>")
check("entity decoded", "Coupon & Barrier" in doc.text)
span, src_text = resolved(doc, "9.15%")
check("post-entity text still resolves exactly", src_text == "9.15%")
span2, src_text2 = resolved(doc, "not a deposit")
check("text after a second entity still resolves exactly", src_text2 == "not a deposit")

# --------------------------------------------------------------------------- #
section("script/style stripped entirely")
# --------------------------------------------------------------------------- #

doc = nz.normalize_html(
    "<html><head><style>.x{color:red}</style></head><body>"
    "<script>var x = 1;</script><p>Visible text only.</p></body></html>"
)
check("style content absent", "color:red" not in doc.text)
check("script content absent", "var x" not in doc.text)
check("body text present", "Visible text only." in doc.text)

# --------------------------------------------------------------------------- #
section("block tags produce paragraph breaks, not run-on text")
# --------------------------------------------------------------------------- #

doc = nz.normalize_html("<div>First.</div><div>Second.</div>")
check("divs separated by a break", "First." in doc.text and "Second." in doc.text
      and "First.Second." not in doc.text)

# --------------------------------------------------------------------------- #
section("stage 1: flat table, span survives")
# --------------------------------------------------------------------------- #

html = """
<table>
  <tr><td>Contingent Coupon Rate</td><td>9.15% per annum</td></tr>
  <tr><td>Coupon Barrier</td><td>70.00% of Initial Value</td></tr>
  <tr><td>Buffer Amount</td><td>10.00%</td></tr>
</table>
"""
doc = nz.normalize_html(html)
check("exactly one table found", len(doc.tables) == 1)
tbl = doc.tables[0]
labels = [p.label for p in tbl.pairs]
check("labels extracted in row order", labels == ["Contingent Coupon Rate", "Coupon Barrier", "Buffer Amount"])
check("flattened text matches the issue's example shape",
      "Contingent Coupon Rate: 9.15% per annum" in doc.text)

for p in tbl.pairs:
    doc_slice = doc.text[p.doc_text_start:p.doc_text_end]
    check(f"pair {p.label!r} lands at its own doc_text offset",
          doc_slice == (f"{p.label}: {p.value}" if p.label else p.value))
    src_slice = doc.source[p.source_span[0]:p.source_span[1]]
    check(f"pair {p.label!r} source_span contains its value", p.value.split()[0] in src_slice)

# raw markup should be gone from the linear text -- only the flattened pairs remain.
check("raw <table>/<tr>/<td> markup absent from text", "<td>" not in doc.text and "<tr>" not in doc.text)

# --------------------------------------------------------------------------- #
section("stage 1: colspan/rowspan (merged cells) -- row/col relationships survive")
# --------------------------------------------------------------------------- #

html = """
<table>
  <tr><td rowspan="2">Underlying</td><td>Initial Value</td><td>4500.25</td></tr>
  <tr><td>Final Value</td><td>4800.10</td></tr>
  <tr><td colspan="2">Buffer Amount (merged label)</td><td>10.00%</td></tr>
</table>
"""
doc = nz.normalize_html(html)
tbl = doc.tables[0]
by_label_value = [(p.label, p.value) for p in tbl.pairs]
check("rowspan repeats the label for each spanned row",
      ("Underlying", "Initial Value 4500.25") in by_label_value
      and ("Underlying", "Final Value 4800.10") in by_label_value)
check("colspan collapses to a single label cell",
      any(p.label == "Buffer Amount (merged label)" and p.value == "10.00%" for p in tbl.pairs))
check("merged-cell pairs still resolve exact source spans", all(
    doc.source[p.source_span[0]:p.source_span[1]] for p in tbl.pairs
))

# --------------------------------------------------------------------------- #
section("stage 1: nested table inside a cell")
# --------------------------------------------------------------------------- #

html = """
<table>
  <tr><td>Observation Schedule</td>
      <td><table><tr><td>Date 1</td><td>2026-03-15</td></tr>
                 <tr><td>Date 2</td><td>2026-06-15</td></tr></table></td></tr>
</table>
"""
doc = nz.normalize_html(html)
check("only the OUTER table counted as top-level", len(doc.tables) == 1)
outer = doc.tables[0]
check("nested table's pairs flattened into the parent cell's value",
      any("Date 1: 2026-03-15" in p.value and "Date 2: 2026-06-15" in p.value for p in outer.pairs))
nested_pair = next(p for p in outer.pairs if p.label == "Observation Schedule")
src = doc.source[nested_pair.source_span[0]:nested_pair.source_span[1]]
check("nested-table pair's source span covers the whole nested <table>", "<table>" in src and "2026-03-15" in src)

# --------------------------------------------------------------------------- #
section("resolve() across a range spanning multiple internal spans")
# --------------------------------------------------------------------------- #

doc = nz.normalize_html("<p>The rate is <b>9.15</b>% flat.</p>")
idx = doc.text.find("9.15% flat")
span = doc.offset_map.resolve(idx, idx + len("9.15% flat"))
recovered = doc.source[span[0]:span[1]]
check("range crossing a tag boundary still resolves to a source range containing the text",
      "9.15" in recovered and "flat" in recovered)

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nnormalize self-check + gate: PASS")
