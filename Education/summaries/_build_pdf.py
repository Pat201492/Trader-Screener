"""Convert the digested-summary markdown files to styled PDFs.

Pure-Python (markdown + xhtml2pdf), mirroring ../_build_pdf.py, with two
adjustments for this folder's content:
  * a Unicode TTF (DejaVuSans, via matplotlib; falls back to Arial) is embedded
    so math/greek/arrow glyphs (§ × → √ ≈ ≥ ⅓ …) render instead of tofu;
  * the data-cost emoji (✅ 🟡 🔴) are mapped to text tags xhtml2pdf can draw.

Run from this folder:
    python _build_pdf.py                 # build every *.md here
    python _build_pdf.py somefile.md     # build one
"""
import os
import sys
import glob
import markdown
from xhtml2pdf import pisa

HERE = os.path.dirname(os.path.abspath(__file__))

# ✅ already-collected/free · 🟡 computable from free OHLCV · 🔴 needs paid feed
EMOJI = {"✅": "[free]", "\U0001F7E1": "[computable]", "\U0001F534": "[paid]"}


def _fonts():
    """Return (regular, bold, italic) TTF paths — DejaVuSans if available, else Arial."""
    try:
        import matplotlib
        d = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
        reg = os.path.join(d, "DejaVuSans.ttf")
        if os.path.exists(reg):
            return reg, os.path.join(d, "DejaVuSans-Bold.ttf"), os.path.join(d, "DejaVuSans-Oblique.ttf")
    except Exception:
        pass
    w = r"C:\Windows\Fonts"
    return os.path.join(w, "arial.ttf"), os.path.join(w, "arialbd.ttf"), os.path.join(w, "ariali.ttf")


_FONTS_READY = False


def _register_fonts():
    """Register a Unicode TTF family with ReportLab so xhtml2pdf embeds it by name."""
    global _FONTS_READY
    if _FONTS_READY:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    reg, bold, ital = _fonts()
    pdfmetrics.registerFont(TTFont("Body", reg))
    pdfmetrics.registerFont(TTFont("Body-Bold", bold))
    pdfmetrics.registerFont(TTFont("Body-Italic", ital))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic")
    _FONTS_READY = True


def _css():
    return """
@page { size: letter; margin: 2.2cm 2cm; }
body { font-family: 'Body', Helvetica, Arial, sans-serif; font-size: 10.5pt; color: #1a1a1a; line-height: 1.45; }
h1 { font-family: 'Body'; font-size: 19pt; color: #0b3d6b; border-bottom: 2px solid #0b3d6b; padding-bottom: 4px; margin-top: 18px; }
h2 { font-family: 'Body'; font-size: 14pt; color: #0b3d6b; margin-top: 16px; border-bottom: 1px solid #c9d6e3; padding-bottom: 2px; }
h3 { font-family: 'Body'; font-size: 11.5pt; color: #1b5e8c; margin-top: 12px; }
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
    _register_fonts()
    src = os.path.join(HERE, md_name)
    out = os.path.join(HERE, os.path.splitext(md_name)[0] + ".pdf")
    with open(src, "r", encoding="utf-8") as f:
        md_text = f.read()
    for e, t in EMOJI.items():
        md_text = md_text.replace(e, t)
    html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists", "toc"])
    html = f"<html><head><meta charset='utf-8'><style>{_css()}</style></head><body>{html_body}</body></html>"
    with open(out, "w+b") as fh:
        result = pisa.CreatePDF(html, dest=fh, encoding="utf-8")
    if result.err:
        raise SystemExit(f"PDF generation failed for {md_name} with {result.err} error(s)")
    print(f"Wrote {os.path.basename(out)} ({os.path.getsize(out)} bytes)")


def main():
    targets = sys.argv[1:] or sorted(
        n for p in glob.glob(os.path.join(HERE, "*.md"))
        for n in [os.path.basename(p)]
        if n != "README.md"
    )
    for md_name in targets:
        build(md_name)


if __name__ == "__main__":
    main()
