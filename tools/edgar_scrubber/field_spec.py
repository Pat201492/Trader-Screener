"""
424B2 field spec — runtime loader, population detector, validator, wire format.

Issue #102 ("the designatable information"). WHAT to pull is data, not code: the
field definitions live in `field_specs/*.json` (form-type keyed). Adding a field
is a JSON edit — no code change, no redeploy. This module is the thin runtime
that:

  * loads the spec files and validates each against the canonical shape
    registered in schema_registry (#86 role) — the shape is NOT declared inline;
  * detects which of the two 424B2 populations a document is (structured note vs
    shelf takedown) and returns the matching spec;
  * validates an extraction record against the spec's per-field bounds and
    cross-checks, surfacing every violation as a flag — never silently dropping;
  * maps the short wire keys #104 uses on the model boundary back to canonical
    names, and guards that a wire key never reaches a stored record.

stdlib only. Run the self-check:  python tools/edgar_scrubber/field_spec.py
"""

import json
from dataclasses import dataclass, field as _dc_field
from datetime import date
from pathlib import Path

try:  # package import: tools.edgar_scrubber.field_spec
    from .schema_registry import REGISTRY, SPEC_SHAPE_NAME
except ImportError:  # standalone: python tools/edgar_scrubber/field_spec.py
    from schema_registry import REGISTRY, SPEC_SHAPE_NAME

DEFAULT_SPEC_DIR = Path(__file__).resolve().parent / "field_specs"


class SpecError(ValueError):
    """A spec file failed to load or did not match the registered shape."""


# ── Field definition ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldDefinition:
    """One field from a spec's `fields[]`. Thin typed view over the JSON entry;
    the raw dict is kept so #105 exemplars / #107 rules added later ride along."""

    name: str
    type: str
    extraction_path: str
    sections: tuple
    unit: str = None
    required: bool = False
    wire_key: str = None
    anchors: tuple = ()
    enum: tuple = None
    item_type: str = None
    item_enum: tuple = None
    bounds: dict = None
    description: str = ""
    # #144: {field, equals, flag_code?, severity?, message?} -- waives
    # `required` when another record value (usually filing_stage) equals
    # `equals`. See FieldSpec._required_waived / validate_record.
    required_unless: dict = None
    raw: dict = _dc_field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d["name"],
            type=d["type"],
            extraction_path=d["extraction_path"],
            sections=tuple(d.get("sections", ())),
            unit=d.get("unit"),
            required=bool(d.get("required", False)),
            wire_key=d.get("wire_key"),
            anchors=tuple(d.get("anchors", ())),
            enum=tuple(d["enum"]) if d.get("enum") is not None else None,
            item_type=d.get("item_type"),
            item_enum=tuple(d["item_enum"]) if d.get("item_enum") is not None else None,
            bounds=d.get("bounds"),
            description=d.get("description", ""),
            required_unless=d.get("required_unless"),
            raw=d,
        )


# ── Validation flag ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Flag:
    """A validation violation. Surfaced, never silently dropped — the record is
    kept as-is and carried with its flags. These bounds are #104's primary
    confidence signal: checkable, unlike a model's self-reported confidence."""

    field: str
    code: str        # missing_required | type_mismatch | out_of_bounds |
                     # enum_violation | cross_check_failed | wire_key_leak | unknown_field
    severity: str    # error | warn | info
    message: str

    def as_dict(self):
        return {"field": self.field, "code": self.code,
                "severity": self.severity, "message": self.message}


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_date(v):
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v.strip())
    except ValueError:
        return None


# ── Form-type spec ──────────────────────────────────────────────────────────

@dataclass
class FieldSpec:
    spec_id: str
    form_type: str
    population: str
    version: str
    description: str
    detection: dict
    sections: tuple
    fields: tuple  # of FieldDefinition
    cross_checks: tuple
    raw: dict = _dc_field(default_factory=dict, repr=False)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_dict(cls, d):
        return cls(
            spec_id=d["spec_id"],
            form_type=d["form_type"],
            population=d["population"],
            version=d["version"],
            description=d.get("description", ""),
            detection=d["detection"],
            sections=tuple(d.get("sections", ())),
            fields=tuple(FieldDefinition.from_dict(f) for f in d["fields"]),
            cross_checks=tuple(d.get("cross_checks", ())),
            raw=d,
        )

    # -- lookups -----------------------------------------------------------

    def field(self, name):
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def field_names(self):
        return [f.name for f in self.fields]

    def fields_for_section(self, section):
        """Which fields route into `section` — the #101 side of the contract."""
        return [f for f in self.fields if section in f.sections]

    def fields_by_path(self, extraction_path):
        return [f for f in self.fields if f.extraction_path == extraction_path]

    # -- detection ---------------------------------------------------------

    def detection_score(self, text):
        """Sum of matched signal weights (case-insensitive substring)."""
        low = text.lower()
        score = 0
        for sig in self.detection.get("signals", []):
            if sig["pattern"].lower() in low:
                score += sig.get("weight", 1)
        return score

    @property
    def min_score(self):
        return self.detection.get("min_score", 1)

    # -- wire format (#104) ------------------------------------------------

    def wire_legend(self):
        """{wire_key: canonical_name} for fields that carry a distinct wire key.
        This is the decode map #104 sends alongside the short-key model output."""
        return {f.wire_key: f.name for f in self.fields if f.wire_key}

    def from_wire(self, payload):
        """Translate a short-key model payload back to canonical names. Keys that
        are already canonical pass through; unknown keys are preserved so a
        downstream unknown_field flag can catch them."""
        legend = self.wire_legend()
        out = {}
        for k, v in payload.items():
            out[legend.get(k, k)] = v
        return out

    def to_wire(self, record):
        """Canonical -> short-key, for building #104's request example. Inverse
        of from_wire for known fields."""
        canon = {f.name: f.wire_key for f in self.fields if f.wire_key}
        return {canon.get(k, k): v for k, v in record.items()}

    def assert_canonical(self, record):
        """Guard: a wire key must NEVER reach a stored record. Raises if the
        record still carries any short transport key."""
        leaked = [f.wire_key for f in self.fields
                  if f.wire_key and f.wire_key != f.name and f.wire_key in record]
        if leaked:
            raise SpecError(
                f"wire keys reached a stored record (must be mapped back first): "
                f"{sorted(leaked)}"
            )

    # -- record validation -------------------------------------------------

    def validate_record(self, record, ex107=None, filing_stage=None):
        """Check a canonical-keyed extraction record against the spec.

        Returns a list of Flag. The record is NEVER modified or dropped — a bad
        extraction is caught here and travels flagged, so it can be held back
        from a chart (#110) rather than corrupting it. `ex107` is the optional
        EX-107 fee-exhibit dict for external cross-checks (#101). `filing_stage`
        is the optional `detect_filing_stage()` result ("preliminary" | "final");
        it waives a field's `required_unless` the same way an explicit
        `record["filing_stage"]` would (#144) -- pass it, or set the record key,
        whichever the caller already has on hand.
        """
        flags = []
        known = set(self.field_names())
        waiver_context = dict(record)
        if filing_stage is not None:
            waiver_context["filing_stage"] = filing_stage

        for f in self.fields:
            present = f.name in record
            val = record.get(f.name)

            if not present or val is None:
                if f.required:
                    waiver = self._required_waiver(f, waiver_context)
                    if waiver is None:
                        flags.append(Flag(f.name, "missing_required", "error",
                                          f"required field {f.name} is missing"))
                    else:
                        flags.append(waiver)
                continue

            flags.extend(self._check_value(f, val))

        # keys the spec does not know about — catches typos and leaked wire keys.
        for k in record:
            if k in known:
                continue
            legend = self.wire_legend()
            if k in legend:
                flags.append(Flag(k, "wire_key_leak", "error",
                                  f"wire key {k!r} present in a record; map to "
                                  f"{legend[k]!r} before storing"))
            else:
                flags.append(Flag(k, "unknown_field", "warn",
                                  f"field {k!r} is not defined in spec {self.spec_id}"))

        flags.extend(self._check_cross(record, ex107))
        return flags

    def _required_waiver(self, f, waiver_context):
        """None if `f.required_unless` doesn't apply or isn't declared; else
        the Flag a waived-but-still-null required field gets instead of
        `missing_required` (#144) -- still surfaced, just as the specific,
        non-error thing it is: e.g. `estimated_value_per_1000` genuinely has
        no point value yet on a preliminary filing, so this is not a defect."""
        ru = f.required_unless
        if not ru or waiver_context.get(ru["field"]) != ru.get("equals"):
            return None
        return Flag(
            f.name,
            ru.get("flag_code", "conditionally_unavailable"),
            ru.get("severity", "info"),
            ru.get("message", f"{f.name} is not required while "
                              f"{ru['field']}={ru.get('equals')!r}"),
        )

    def check_value(self, name, value, ex107=None):
        """Bounds/type/enum flags for ONE extracted value in isolation --
        #104's confidence-gate signal, usable before a full record exists (the
        ladder gates a field right after extracting it, not after the whole
        document is assembled). A cross-check that needs a second in-record
        field (date_after, lte, ...) can't fire here -- that needs
        validate_record. `external_equals` against `ex107` only needs this one
        field, so it DOES fire here: that is the gate's cross-check signal.
        """
        f = self.field(name)
        if f is None:
            return [Flag(name, "unknown_field", "warn",
                         f"field {name!r} is not defined in spec {self.spec_id}")]
        flags = list(self._check_value(f, value))
        flags.extend(self._check_cross({name: value}, ex107))
        return flags

    def _check_value(self, f, val):
        flags = []

        # type
        if f.type in ("number", "percent"):
            if not _is_number(val):
                flags.append(Flag(f.name, "type_mismatch", "warn",
                                  f"{f.name} expected number, got {type(val).__name__}"))
                return flags
        elif f.type == "date":
            if _parse_date(val) is None:
                flags.append(Flag(f.name, "type_mismatch", "warn",
                                  f"{f.name} is not an ISO YYYY-MM-DD date: {val!r}"))
                return flags
        elif f.type == "enum":
            if f.enum is not None and val not in f.enum:
                flags.append(Flag(f.name, "enum_violation", "warn",
                                  f"{f.name}={val!r} not in {list(f.enum)}"))
        elif f.type == "array":
            if not isinstance(val, list):
                flags.append(Flag(f.name, "type_mismatch", "warn",
                                  f"{f.name} expected array, got {type(val).__name__}"))
                return flags
            for i, item in enumerate(val):
                if f.item_type == "date" and _parse_date(item) is None:
                    flags.append(Flag(f.name, "type_mismatch", "warn",
                                      f"{f.name}[{i}] is not an ISO date: {item!r}"))
                if f.item_enum is not None and item not in f.item_enum:
                    flags.append(Flag(f.name, "enum_violation", "warn",
                                      f"{f.name}[{i}]={item!r} not in {list(f.item_enum)}"))
        elif f.type == "string":
            if not isinstance(val, str):
                flags.append(Flag(f.name, "type_mismatch", "warn",
                                  f"{f.name} expected string, got {type(val).__name__}"))

        # bounds — only meaningful for numerics that passed the type check.
        if f.bounds and _is_number(val):
            lo = f.bounds.get("min")
            hi = f.bounds.get("max")
            if lo is not None and val < lo:
                flags.append(Flag(f.name, "out_of_bounds", "warn",
                                  f"{f.name}={val} below min {lo}"))
            if hi is not None and val > hi:
                flags.append(Flag(f.name, "out_of_bounds", "warn",
                                  f"{f.name}={val} above max {hi}"))
        return flags

    def _check_cross(self, record, ex107):
        flags = []
        for c in self.cross_checks:
            rule = c.get("rule")
            sev = c.get("severity", "warn")
            msg = c.get("message", f"cross-check {rule} failed")

            if rule in ("date_after", "date_after_or_equal"):
                a = _parse_date(record.get(c["field"]))
                b = _parse_date(record.get(c["reference"]))
                if a is None or b is None:
                    continue  # can't compare; a type flag already fired if wrong
                ok = a > b if rule == "date_after" else a >= b
                if not ok:
                    flags.append(Flag(c["field"], "cross_check_failed", sev, msg))

            elif rule in ("lte", "lt", "gte", "gt"):
                a = record.get(c["field"])
                b = record.get(c["reference"])
                if not (_is_number(a) and _is_number(b)):
                    continue
                ok = {"lte": a <= b, "lt": a < b,
                      "gte": a >= b, "gt": a > b}[rule]
                if not ok:
                    flags.append(Flag(c["field"], "cross_check_failed", sev, msg))

            elif rule == "external_equals":
                src = (ex107 or {}) if c.get("source") == "ex107" else {}
                a = record.get(c["field"])
                b = src.get(c.get("source_field", c["field"]))
                if not (_is_number(a) and _is_number(b)):
                    continue  # exhibit absent — nothing to check against
                tol = c.get("tolerance_pct", 0) / 100.0
                allowed = abs(b) * tol
                if abs(a - b) > allowed:
                    flags.append(Flag(c["field"], "cross_check_failed", sev, msg))

        return flags


# ── Loader ──────────────────────────────────────────────────────────────────

def load_spec(path):
    """Load one spec file and validate it against the registered shape (#86).
    Raises SpecError if the JSON is bad or the shape does not match — a malformed
    spec fails loudly at load time, not silently at extraction time."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SpecError(f"could not read spec {path}: {e}") from e

    valid, errors = REGISTRY.validate(SPEC_SHAPE_NAME, data)
    if not valid:
        raise SpecError(f"spec {path.name} does not match {SPEC_SHAPE_NAME}: "
                        + "; ".join(errors))
    return FieldSpec.from_dict(data)


def load_specs(spec_dir=None):
    """Load every *.json spec in the directory, keyed by population. All specs
    load at runtime from data; nothing here is hard-coded per form type."""
    spec_dir = Path(spec_dir) if spec_dir else DEFAULT_SPEC_DIR
    specs = {}
    for p in sorted(spec_dir.glob("*.json")):
        spec = load_spec(p)
        if spec.population in specs:
            raise SpecError(f"duplicate population {spec.population!r} "
                            f"({specs[spec.population].spec_id} vs {spec.spec_id})")
        specs[spec.population] = spec
    if not specs:
        raise SpecError(f"no spec files found in {spec_dir}")
    return specs


# ── Population detection ─────────────────────────────────────────────────────

@dataclass
class Detection:
    spec: FieldSpec          # best-matching spec (None if nothing cleared threshold)
    population: str
    score: int
    scores: dict             # population -> score, for transparency / #104
    confident: bool          # best cleared its own min_score AND beat runner-up

    def as_dict(self):
        return {"population": self.population, "score": self.score,
                "scores": self.scores, "confident": self.confident,
                "spec_id": self.spec.spec_id if self.spec else None}


def detect_population(text, specs=None):
    """Pick the 424B2 population for a document by scoring each spec's detection
    signals. Structured-note markers are specific and weighted high; shelf
    takedown is the lower-signal default. Returns a Detection; the caller applies
    Detection.spec.

    `confident` is False when nothing cleared threshold or two populations tied —
    the caller can route those to review or the model rather than trust a guess.
    """
    if specs is None:
        specs = load_specs()

    scores = {pop: spec.detection_score(text) for pop, spec in specs.items()}
    if not scores:
        return Detection(None, None, 0, {}, False)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_pop, best_score = ranked[0]
    best_spec = specs[best_pop]

    runner_up = ranked[1][1] if len(ranked) > 1 else -1
    cleared = best_score >= best_spec.min_score
    confident = cleared and best_score > runner_up

    if not cleared:
        return Detection(None, best_pop, best_score, scores, False)
    return Detection(best_spec, best_pop, best_score, scores, confident)


# ── Preliminary vs final detection (#144) ────────────────────────────────────
#
# The other half of #144: a preliminary 424B2 discloses `estimated_value_per_1000`
# as a range ("expected to be between $962.60 and $992.60") with the point value
# "set forth in the final pricing supplement" -- it does not exist in the document
# yet, so a null there is CORRECT, not a missing extraction. A final pricing
# supplement states the determined value outright. SEC form convention marks the
# distinction on the cover page in fixed legend text, so -- like detect_population
# -- this is zero-token and deterministic, never a model guess about its own
# document.

_PRELIMINARY_SIGNALS = (
    "subject to completion",
    "preliminary pricing supplement",
    "preliminary terms",
    "information in this preliminary pricing supplement is not complete",
    "may be changed",
    "expected to be between",   # the range phrasing itself -- see module docstring
)

_FINAL_SIGNALS = (
    "final pricing supplement",
    "final terms",
    "as of the pricing date was",   # determined-value phrasing, past tense
    "as of the trade date was",
)


def detect_filing_stage(text):
    """Returns "preliminary" or "final" from the SEC-mandated cover-page
    legend, or None when neither is found (caller should not assume either --
    an absent signal is not evidence of "final").

    Preliminary signals are checked first: a document can carry both a
    boilerplate reference to "the final pricing supplement" (describing what
    is still to come) AND its own "Subject to Completion" legend, and the
    legend is the authoritative one.
    """
    low = text.lower()
    if any(s in low for s in _PRELIMINARY_SIGNALS):
        return "preliminary"
    if any(s in low for s in _FINAL_SIGNALS):
        return "final"
    return None


# ── Convenience: canonical field-name inventory (#86) ────────────────────────

def canonical_field_names(specs=None):
    """Every canonical field name across all populations. These live here and in
    the registry (#86); the short wire keys are a transport detail and are
    deliberately excluded."""
    if specs is None:
        specs = load_specs()
    names = {}
    for pop, spec in specs.items():
        names[pop] = spec.field_names()
    return names


if __name__ == "__main__":
    specs = load_specs()
    print(f"loaded {len(specs)} population(s): {sorted(specs)}")
    for pop, spec in specs.items():
        by = {p: len(spec.fields_by_path(p)) for p in
              ("fixed-anchor", "table-resident", "variable")}
        print(f"  {pop:<16} {len(spec.fields):>2} fields  paths={by}")

    note_text = ("Our estimated value of the notes ... contingent coupon ... "
                 "the notes will be automatically called ... buffer amount ... "
                 "worst performing underlying ... not a deposit")
    d = detect_population(note_text, specs)
    print(f"\ndetect(structured-note text) -> {d.population} "
          f"score={d.score} confident={d.confident}")

    shelf_text = ("Price to Public ... Underwriting Discount ... Net Proceeds "
                  "... Use of Proceeds ... shares of common stock")
    d2 = detect_population(shelf_text, specs)
    print(f"detect(shelf text)            -> {d2.population} "
          f"score={d2.score} confident={d2.confident}")

    prelim_text = ("PRELIMINARY PRICING SUPPLEMENT Subject to Completion. The "
                   "estimated initial value of the notes as of the trade date is "
                   "expected to be between $962.60 and $992.60. It will be set "
                   "forth in the final pricing supplement.")
    final_text = ("PRICING SUPPLEMENT. The estimated value of the notes as of "
                  "the pricing date was $972.30 per $1,000 note.")
    print(f"\ndetect_filing_stage(preliminary text) -> {detect_filing_stage(prelim_text)!r}")
    print(f"detect_filing_stage(final text)       -> {detect_filing_stage(final_text)!r}")
    assert detect_filing_stage(prelim_text) == "preliminary"
    assert detect_filing_stage(final_text) == "final"

    note = specs["structured_note"]
    waived = note.validate_record(
        {"estimated_value_low": 962.60, "estimated_value_high": 992.60},
        filing_stage="preliminary",
    )
    waiver_flag = next(f for f in waived if f.field == "estimated_value_per_1000")
    print(f"\npreliminary record, no point value -> "
          f"{waiver_flag.code!r} ({waiver_flag.severity})")
    assert waiver_flag.code == "unavailable_on_preliminary" and waiver_flag.severity == "info"

    print("\nfield_spec self-check: PASS")
