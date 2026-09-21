#!/usr/bin/env python3
"""
Ask Claude what a filing actually carries, and get back a FIELD TEMPLATE.

Why this exists: `field_specs/424b2_structured_note.json` was written by hand,
one field at a time, from filings someone read. That is fine for the note type
we already know and useless for the one we do not -- a new issuer, a new
product, a term that only shows up in half the shelf. `scan_availability`
answers "does this filing carry the fields we already named"; nothing answered
"what ELSE is in here worth naming".

So this reads one filing and returns the grabbable fields it can SEE: the label
the value sits under, the value as printed, its unit and type, and the sentence
it came from. Two uses, same output:

  * seed a spec -- `as_field_spec()` turns the template into the anchors/
    patterns shape `field_spec.load_spec` reads, so a new product type starts
    from something a model found rather than from a blank file;
  * grade the local model -- every entry carries a verbatim quote, which
    `locate()` resolves to offsets in the filing's own text. A template value
    the quote cannot be found for is dropped, not reported, for exactly the
    reason the rest of this tool drops unlocatable values: a number nothing can
    check is not evidence.

This is the Claude rung of #104 pointed at the SPEC rather than at a field. It
is a deliberate second code path from `extraction_ladder`'s escalation: that one
asks "what is `barrier_pct` in this document" through the OpenAI-compatible
shim, one field at a time, and must stay cheap. This asks one open question per
filing and wants the answer schema-constrained, so it uses the Anthropic SDK's
structured outputs directly.

Run:
    python tools/edgar_scrubber/claude_template.py <accession> [--cik CIK]
"""
import json
import os
import re

# The template is worth having only if it comes back in the shape the rest of
# this file assumes. Structured outputs enforce that server-side, so a malformed
# answer is retried by the API rather than parsed hopefully here.
TEMPLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "product_type": {
            "type": "string",
            "description": "What this note IS, in the filing's own words "
                           "(e.g. 'autocallable contingent coupon note', "
                           "'buffered return enhanced note'). Empty if unclear.",
        },
        "fields": {
            "type": "array",
            "description": "Every economic term this filing states a value for.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "snake_case identifier, e.g. coupon_barrier_pct",
                    },
                    "label": {
                        "type": "string",
                        "description": "The label this value is printed under in "
                                       "the filing, verbatim (e.g. 'Barrier Amount').",
                    },
                    "value": {
                        "type": "string",
                        "description": "The value as printed, verbatim, with no "
                                       "reformatting (e.g. '60.00%', '46660RRA4').",
                    },
                    "unit": {
                        "type": "string",
                        "description": "percent_of_initial, usd_per_1000, date, "
                                       "cusip, percent_per_annum, or '' if unitless.",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["number", "percent", "currency", "date",
                                 "string", "list"],
                    },
                    "section": {
                        "type": "string",
                        "description": "The heading it appears under (e.g. 'Key Terms').",
                    },
                    "quote": {
                        "type": "string",
                        "description": "The complete sentence or table line the "
                                       "value appears in, copied EXACTLY from the "
                                       "filing, including the value itself. This is "
                                       "checked against the source text; a quote "
                                       "that is paraphrased will be rejected.",
                    },
                    "spec_field": {
                        "type": "string",
                        "description": "The name from the existing spec this "
                                       "corresponds to, or '' if this term is not "
                                       "in the spec yet.",
                    },
                },
                "required": ["name", "label", "value", "unit", "type", "section",
                             "quote", "spec_field"],
                "additionalProperties": False,
            },
        },
        "absent_spec_fields": {
            "type": "array",
            "description": "Spec fields this note type does not carry at all -- "
                           "not 'I could not find it', but 'this product has no "
                           "such term'. A growth note has no coupon barrier.",
            "items": {"type": "string"},
        },
        "notes": {
            "type": "string",
            "description": "Anything about this filing's structure a later reader "
                           "would want to know. Empty if nothing stands out.",
        },
    },
    "required": ["product_type", "fields", "absent_spec_fields", "notes"],
    "additionalProperties": False,
}

SYSTEM = """You are reading one SEC Form 424B2 pricing supplement for a \
structured note, and cataloguing every economic term it states a value for.

Copy values and quotes EXACTLY as printed. Do not normalize, round, reformat, \
or convert anything -- "60.00%" is not "0.6", and "on or about April 9, 2026" \
is not "2026-04-09". Every quote must be a literal substring of the document \
you were given; it is checked, and an entry whose quote cannot be found in the \
source is discarded.

Catalogue only terms this filing actually states. A term that appears in a \
hypothetical example, a payout illustration, or a risk-factor restatement is \
not a stated term -- take the value from where the filing DEFINES it (usually \
"Key Terms"), and if a term is only illustrated, leave it out. Do not invent a \
field to fill a gap: a note that pays no coupon has no coupon rate, and saying \
so in absent_spec_fields is the correct answer.

Where a term is stated per review date rather than once (call values, call \
premium amounts), report the schedule as a single list-typed field, and quote \
the line that establishes it."""


def _client(api_key=None):
    """The Anthropic SDK client. Imported here so the module loads (and its
    schema stays inspectable) on a machine that never installed it."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "the Claude template needs the anthropic SDK: pip install -r "
            "tools/edgar_scrubber/requirements-claude.txt") from exc
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; see SETUP.md.")
    return anthropic.Anthropic(api_key=key)


def model_name():
    return os.environ.get("ANTHROPIC_TEMPLATE_MODEL", "claude-opus-5")


def build_prompt(text, spec_fields=(), max_chars=400_000):
    """The one user turn: the filing, plus the names we already have.

    The spec names are supplied so `spec_field` can be filled in and
    `absent_spec_fields` means something. They are NOT supplied as a list to
    fill in -- handing a model the answer key is how you get every field
    confidently reported present.
    """
    body = text if len(text) <= max_chars else text[:max_chars]
    known = ", ".join(spec_fields) or "(none)"
    truncated = ("\n\n[The document was truncated at "
                 f"{max_chars:,} characters.]" if len(text) > max_chars else "")
    return (
        f"Fields the existing spec already names: {known}\n\n"
        "Catalogue this filing. Use the existing name in `spec_field` when a "
        "term you find is one of those, and leave `spec_field` empty when it is "
        "a term the spec has no name for yet.\n\n"
        f"<filing>\n{body}{truncated}\n</filing>"
    )


def request_template(text, spec_fields=(), *, client=None, model=None,
                     max_chars=400_000):
    """One call. Returns the parsed template dict, unlocated.

    Streamed because the filing is large and the answer can be too: a
    non-streaming request at this max_tokens risks an HTTP timeout, which would
    throw away a call that had already been paid for.
    """
    client = client or _client()
    model = model or model_name()
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": TEMPLATE_SCHEMA},
        },
        messages=[{"role": "user",
                   "content": build_prompt(text, spec_fields, max_chars)}],
    ) as stream:
        message = stream.get_final_message()

    # A refusal is a successful HTTP 200 with no content -- reading content[0]
    # unconditionally would raise IndexError and report itself as a bug here.
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request "
                           f"({getattr(message.stop_details, 'category', None)})")
    body = next((b.text for b in message.content if b.type == "text"), None)
    if body is None:
        raise RuntimeError(f"no text in the response (stop_reason={message.stop_reason})")
    out = json.loads(body)
    out["model"] = message.model
    out["usage"] = {"input_tokens": message.usage.input_tokens,
                    "output_tokens": message.usage.output_tokens}
    return out


_WS = re.compile(r"\s+")


def _find_loose(text, snippet):
    """Offsets of `snippet` in `text`, tolerating whitespace differences only.

    Normalization collapses runs of whitespace, so a quote copied from a table
    row can differ from the source by a space without being a paraphrase. Any
    other difference is a real mismatch and stays a miss -- that is the check
    doing its job.
    """
    if not snippet:
        return None
    at = text.find(snippet)
    if at >= 0:
        return (at, at + len(snippet))

    # Map every non-space character back to its offset in the original, then
    # search the space-free projection. Whitespace differences vanish; a changed
    # word does not.
    keep = [(i, c) for i, c in enumerate(text) if not c.isspace()]
    flat = "".join(c for _, c in keep)
    needle = _WS.sub("", snippet)
    if not needle:
        return None
    at = flat.find(needle)
    if at < 0:
        return None
    return (keep[at][0], keep[at + len(needle) - 1][0] + 1)


def locate(template, text):
    """Resolve every entry's quote (and its value inside that quote) to offsets.

    An entry whose quote is not in the document is dropped into `unlocatable`
    rather than returned as a finding. This is the same rule the coverage matrix
    applies to the local model: a value nothing can point at in the source is
    not evidence, no matter which model produced it.
    """
    located, unlocatable = [], []
    for entry in template.get("fields") or []:
        span = _find_loose(text, entry.get("quote") or "")
        if span is None:
            unlocatable.append({**entry, "reason": "quote not found in the document"})
            continue
        # The value's own span, resolved INSIDE the quote so a value that
        # appears fifty times ("$1,000") lands on the occurrence quoted.
        vspan = _find_loose(text[span[0]:span[1]], entry.get("value") or "")
        value_span = ([span[0] + vspan[0], span[0] + vspan[1]] if vspan else None)
        located.append({**entry, "span": [span[0], span[1]],
                        "value_span": value_span})
    return {**template, "fields": located, "unlocatable": unlocatable}


def as_field_spec(template, spec_id="424b2.claude_draft"):
    """The template in the shape `field_spec.load_spec` reads.

    A DRAFT: the anchors come from one filing, so they describe that issuer's
    wording and nothing else. It is a starting point for a spec, which is a
    different claim from being one.
    """
    fields = []
    for entry in template.get("fields") or []:
        anchors = [a for a in {entry.get("label"), entry.get("section")} if a]
        fields.append({
            "name": entry["name"],
            "type": entry.get("type") or "string",
            "unit": entry.get("unit") or None,
            "anchors": anchors,
            "description": f"{entry.get('label')} (drafted from one filing)",
        })
    return {"spec_id": spec_id, "form": "424B2",
            "product_type": template.get("product_type") or None,
            "drafted_by": template.get("model"), "fields": fields}


def main():  # pragma: no cover - CLI
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import server

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("accession")
    ap.add_argument("--cik")
    ap.add_argument("--document")
    ap.add_argument("--spec", action="store_true",
                    help="print the draft field spec instead of the template")
    args = ap.parse_args()

    doc = server.load_document(args.cik, args.accession, args.document)
    import field_spec as fs
    spec = fs.load_spec(Path(__file__).resolve().parent / "field_specs" /
                        "424b2_structured_note.json")
    template = locate(request_template(doc["text"], [f.name for f in spec.fields]),
                      doc["text"])
    print(json.dumps(as_field_spec(template) if args.spec else template, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
