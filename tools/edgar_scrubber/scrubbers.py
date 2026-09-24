"""
EDGAR Scrubber as a REGISTRY of per-form scrubbers (issue #212, carved from
#209).

"EDGAR Scrubber" was one 424B2-shaped tool because that was the proof of
concept. It is really a COLLECTION: one scrubber per form type. A `Scrubber`
is a stored, versioned EDGAR search bound to a form type -- it fixes the `q`
string and `forms` filter, and leaves the issuer list and date range as the
only operator-facing knobs. Combined with those two knobs it builds a
`crawl.SavedQuery`, the exact shape `crawl` already accepts (#100).

WHAT to scrub is data, not code: the scrubbers live in `scrubbers.json`
(form-type keyed), same convention as `field_specs/*.json` (#102). Adding a
form is a JSON edit -- no code change. This module is the thin runtime that
loads that registry, validates every entry at load (so a malformed registry
fails at startup, not at click time), resolves a stored saved query back to
the scrubber that owns its form, and builds a `SavedQuery` from a scrubber
plus an issuer list and a date range.

stdlib only. Run the self-check:  python tools/edgar_scrubber/scrubbers.py
"""

import json
from dataclasses import dataclass
from pathlib import Path

try:  # package import: tools.edgar_scrubber.scrubbers
    from .crawl import SavedQuery, load_query
except ImportError:  # standalone: python tools/edgar_scrubber/scrubbers.py
    from crawl import SavedQuery, load_query

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "scrubbers.json"
DEFAULT_QUERIES_DIR = Path(__file__).resolve().parent / "queries"

# A scrubber's fields come from one of two sources, and the choice is not
# cosmetic -- it decides whether a downstream field list can be generated or
# must be authored:
#
#   "taxonomy" -- the form's facts are XBRL-tagged (10-K, 10-Q, 8-K). A tagged
#                 form already STATES its own contents: the fields are read out
#                 of the filer's own taxonomy, nothing to hand-write.
#   "spec"     -- the form is prose that carries no machine-readable field
#                 list (424B2 structured notes). A prose form does NOT state
#                 its contents, so the fields have to be authored as a field
#                 spec (#102) and pulled by the model.
FIELD_SOURCES = ("taxonomy", "spec")


class ScrubberError(ValueError):
    """A registry entry failed to load or did not match the required shape.
    Raised at load time, naming the offending id, so a bad registry breaks a
    process at startup rather than at click time."""


@dataclass(frozen=True)
class Scrubber:
    """One per-form scrubber: a stored, versioned EDGAR search bound to a form
    type. `q` + `forms` are the fixed query template; issuer list and date
    range are supplied per run by the operator (see `build_query`)."""

    id: str
    form_type: str
    display_name: str
    purpose: str          # one line -- no newlines (checked in validate)
    q: str                # query-template full-text string ("" = form filter only)
    forms: tuple          # fixed `forms` filter, form_type included
    field_source: str     # one of FIELD_SOURCES

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            form_type=d["form_type"],
            display_name=d["display_name"],
            purpose=d["purpose"],
            q=d.get("q") or "",
            forms=tuple(d.get("forms", ())),
            field_source=d["field_source"],
        )

    def to_dict(self):
        return {
            "id": self.id,
            "form_type": self.form_type,
            "display_name": self.display_name,
            "purpose": self.purpose,
            "q": self.q,
            "forms": list(self.forms),
            "field_source": self.field_source,
        }

    def build_query(self, query_id, ciks=(), startdt=None, enddt=None, excludeCiks=()):
        """Combine this scrubber's fixed template (`q`, `forms`) with the two
        operator knobs (issuer list, date range) into a `crawl.SavedQuery` --
        the exact shape `crawl` already accepts. This is the only place a
        scrubber turns into a runnable query."""
        return SavedQuery(
            id=query_id,
            q=self.q or None,
            forms=self.forms,
            startdt=startdt,
            enddt=enddt,
            ciks=tuple(ciks),
            excludeCiks=tuple(excludeCiks),
        )

    def owns_query(self, query):
        """True if `query` (a `SavedQuery`) is a stored search over this
        scrubber's form -- i.e. its `forms` filter is exactly this scrubber's
        form type. Lets the existing 424B2 saved queries resolve to the 424B2
        scrubber WITHOUT being rewritten."""
        return tuple(query.forms) == (self.form_type,)


def _validate(entry_id, s):
    """Raise `ScrubberError` (naming `entry_id`) unless `s` is a well-formed
    scrubber. Called for every entry at load."""
    def bad(msg):
        raise ScrubberError(f"scrubber {entry_id!r}: {msg}")

    if not (isinstance(s.id, str) and s.id):
        bad("id must be a non-empty string")
    if not (isinstance(s.form_type, str) and s.form_type):
        bad("form_type must be a non-empty string")
    if not (isinstance(s.display_name, str) and s.display_name):
        bad("display_name must be a non-empty string")
    if not (isinstance(s.purpose, str) and s.purpose):
        bad("purpose must be a non-empty string")
    if "\n" in s.purpose:
        bad("purpose must be a single line")
    if not isinstance(s.q, str):
        bad("q must be a string")
    if not s.forms or not all(isinstance(f, str) and f for f in s.forms):
        bad("forms must be a non-empty list of non-empty strings")
    if s.form_type not in s.forms:
        bad(f"form_type {s.form_type!r} must appear in forms {list(s.forms)!r}")
    if s.field_source not in FIELD_SOURCES:
        bad(f"field_source must be one of {FIELD_SOURCES!r}, got {s.field_source!r}")


def load_scrubbers(path=None):
    """Load and validate the scrubber registry. Every entry is validated here,
    at load, and any failure raises `ScrubberError` naming the offending id --
    a malformed registry fails at startup, not when someone clicks the tool.

    Returns a dict keyed by scrubber id (registry order is preserved by
    dict-insertion order in 3.7+)."""
    path = Path(path or DEFAULT_REGISTRY_PATH)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    entries = raw["scrubbers"] if isinstance(raw, dict) else raw
    scrubbers = {}
    for entry in entries:
        entry_id = entry.get("id", "<missing id>")
        try:
            s = Scrubber.from_dict(entry)
        except (KeyError, TypeError) as exc:
            raise ScrubberError(f"scrubber {entry_id!r}: malformed entry ({exc})")
        _validate(entry_id, s)
        if s.id in scrubbers:
            raise ScrubberError(f"scrubber {s.id!r}: duplicate id in registry")
        scrubbers[s.id] = s
    return scrubbers


def resolve_scrubber(query, scrubbers):
    """Return the scrubber that owns `query`'s form, or None. `query` may be a
    `SavedQuery` or a path to a saved-query JSON."""
    if not isinstance(query, SavedQuery):
        query = load_query(query)
    for s in scrubbers.values():
        if s.owns_query(query):
            return s
    return None


def stored_queries(queries_dir=None):
    """Every saved query on disk, as `SavedQuery` objects (issuer/date-bound
    searches an operator has already stored)."""
    queries_dir = Path(queries_dir or DEFAULT_QUERIES_DIR)
    out = []
    if queries_dir.is_dir():
        for p in sorted(queries_dir.glob("*.json")):
            out.append(load_query(p))
    return out


def _self_check():
    scrubbers = load_scrubbers()
    print(f"loaded {len(scrubbers)} scrubber(s): {', '.join(scrubbers)}")
    for q in stored_queries():
        s = resolve_scrubber(q, scrubbers)
        print(f"  query {q.id!r} -> scrubber {s.id if s else None!r}")


if __name__ == "__main__":
    _self_check()
