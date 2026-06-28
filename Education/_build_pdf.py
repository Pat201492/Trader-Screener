"""Convert education markdown docs to styled PDFs.

Pure-Python (markdown + xhtml2pdf), no system deps. Run from this folder:
    python _build_pdf.py                 # build all docs in DOCS
    python _build_pdf.py somefile.md     # build one (output: somefile.pdf)
"""
import os
import sys
import markdown
from xhtml2pdf import pisa

HERE = os.path.dirname(os.path.abspath(__file__))

# Markdown docs that have a PDF companion. Add new ones here.
DOCS = [
    "Trading_and_Exchanges_Harris_Detailed.md",
    "Derivatives_Types_and_Trading_Signals.md",
]

CSS = """
@page { size: letter; margin: 2.2cm 2cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; color: #1a1a1a; line-height: 1.45; }
h1 { font-size: 19pt; color: #0b3d6b; border-bottom: 2px solid #0b3d6b; padding-bottom: 4px; margin-top: 18px; }
h2 { font-size: 14pt; color: #0b3d6b; margin-top: 16px; border-bottom: 1px solid #c9d6e3; padding-bottom: 2px; }
h3 { font-size: 11.5pt; color: #1b5e8c; margin-top: 12px; }
p { margin: 5px 0; }
ul, ol { margin: 4px 0 8px 0; }
li { margin: 2px 0; }
strong { color: #0b3d6b; }
blockquote { background: #eef3f8; border-left: 4px solid #1b5e8c; margin: 8px 0; padding: 6px 10px; color: #243b53; font-style: italic; }
code { background: #f0f0f0; padding: 1px 3px; font-family: Courier, monospace; font-size: 9.5pt; }
hr { border: none; border-top: 1px solid #ccc; margin: 14px 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 9.5pt; }
th { background: #0b3d6b; color: #fff; text-align: left; padding: 5px 7px; }
td { border: 1px solid #cdd7e1; padding: 5px 7px; vertical-align: top; }
tr:nth-child(even) td { background: #f4f7fa; }
a { color: #1b5e8c; text-decoration: none; }
"""

def build(md_name):
    src = os.path.join(HERE, md_name)
    out = os.path.join(HERE, os.path.splitext(md_name)[0] + ".pdf")
    with open(src, "r", encoding="utf-8") as f:
        md_text = f.read()
    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
    )
    html = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html_body}</body></html>"
    with open(out, "w+b") as fh:
        result = pisa.CreatePDF(html, dest=fh, encoding="utf-8")
    if result.err:
        raise SystemExit(f"PDF generation failed for {md_name} with {result.err} error(s)")
    print(f"Wrote {out} ({os.path.getsize(out)} bytes)")

def main():
    targets = sys.argv[1:] or DOCS
    for md_name in targets:
        build(md_name)

if __name__ == "__main__":
    main()
