"""
Canonical shape registry for the EDGAR field-spec layer (issue #102).

This is the pipeline-side embodiment of the canonical registry (#86, the JS
`schema-registry.js`). The registry holds *shapes* — the meta-schema that says
what a valid field definition and what a valid form-type spec look like — so the
shape is defined ONCE here and never re-declared inline inside each spec file.

The JSON spec files under `field_specs/` are validated against the registered
`edgar.field_spec` shape at load time (see field_spec.load_spec). Adding a field
to a spec is a data edit; it does not touch this file.

Why here and not in `schema-registry.js`:
  - The consumers of the field spec — section routing (#101), extraction and the
    Claude escalation (#104), exemplar mining (#105), rule promotion (#107) — are
    all in this Python edgar_scrubber pipeline.
  - #86's JS registry validates the *front-end* data contracts (screener columns,
    tool manifest, research projects). This module plays the identical role for
    the *extraction* contract and mirrors its `SchemaRegistry` / object-validator
    design so the two stay recognizably the same pattern.
  - `field_spec_shape_as_data()` re-exports the shape as plain JSON so the #86 JS
    registry can adopt the exact same definition without re-typing it.

Run the self-check:  python tools/edgar_scrubber/schema_registry.py
"""

# Closed vocabularies shared across every spec. Declared once; specs reference
# these by string. Extending a vocabulary is a deliberate edit to this list.

# Expected extraction path per field — drives #107 (regex-first). fixed-anchor
# and table-resident should be served by rules; variable genuinely needs the model.
EXTRACTION_PATHS = ("fixed-anchor", "table-resident", "variable")

# Field value types. "percent" and "number" are both numeric but carry different
# default bounds intent; "date" is an ISO-8601 (YYYY-MM-DD) string.
FIELD_TYPES = ("string", "number", "percent", "date", "enum", "array", "boolean")

# Element types allowed inside an array field. Superset of FIELD_TYPES: an array
# may hold structured items (e.g. underlyings[] = [{name, kind}, ...]).
ITEM_TYPES = FIELD_TYPES + ("object",)

# Severity levels a validation flag can carry.
FLAG_SEVERITIES = ("error", "warn", "info")

SPEC_SHAPE_NAME = "edgar.field_spec"
FIELD_SHAPE_NAME = "edgar.field_definition"

_MISSING = object()


def _type_ok(t, val):
    """True if `val` is an acceptable Python value for declared type `t`."""
    if t in ("string", "enum", "date"):
        return isinstance(val, str)
    if t in ("number", "percent"):
        # bool is a subclass of int — reject it explicitly.
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t == "boolean":
        return isinstance(val, bool)
    if t == "array":
        return isinstance(val, list)
    if t == "object":
        return isinstance(val, dict)
    return True


class ObjectValidator:
    """Validate that a dict matches a set of required/typed fields.

    Mirrors the ObjectValidator in #86's schema-registry.js: each entry in
    `fields` is {type, required?, nullable?, enum?, item_type?, validator?}.
    `validator` is a callable(value) -> (valid: bool, errors: list[str]).

    Returns (valid: bool, errors: list[str]).
    """

    def __init__(self, fields):
        self.fields = fields

    def validate(self, obj):
        errors = []
        if not isinstance(obj, dict):
            return (False, ["Expected an object"])

        for name, spec in self.fields.items():
            val = obj.get(name, _MISSING)
            missing = val is _MISSING

            if spec.get("required") and missing:
                errors.append(f"Missing required field: {name}")
                continue
            if missing:
                continue  # optional + absent

            if val is None:
                if spec.get("nullable"):
                    continue
                errors.append(f"Field {name}: null not allowed (field is not nullable)")
                continue

            t = spec.get("type")
            if t and not _type_ok(t, val):
                errors.append(f"Field {name}: expected {t}, got {type(val).__name__}")
                continue

            if spec.get("enum") and val not in spec["enum"]:
                errors.append(f"Field {name}: value {val!r} not in {list(spec['enum'])}")

            if spec.get("item_type") and isinstance(val, list):
                for i, item in enumerate(val):
                    if not _type_ok(spec["item_type"], item):
                        errors.append(
                            f"Field {name}[{i}]: expected {spec['item_type']}, "
                            f"got {type(item).__name__}"
                        )

            fn = spec.get("validator")
            if fn:
                ok, sub = fn(val)
                if not ok:
                    errors.extend(f"Field {name}: {e}" for e in sub)

        return (len(errors) == 0, errors)


# ── Field-definition shape ──────────────────────────────────────────────────
#
# One entry in a spec's `fields[]`. This is the "spec shape" the acceptance
# criterion requires be registered, not declared inline.

def _validate_bounds(val):
    if not isinstance(val, dict):
        return (False, ["bounds must be an object"])
    errs = []
    for k in val:
        if k not in ("min", "max"):
            errs.append(f"unknown bounds key {k!r} (allowed: min, max)")
    for k in ("min", "max"):
        if k in val and not isinstance(val[k], (int, float)):
            errs.append(f"bounds.{k} must be numeric")
    if "min" in val and "max" in val and val["min"] > val["max"]:
        errs.append("bounds.min > bounds.max")
    return (len(errs) == 0, errs)


FIELD_DEFINITION_FIELDS = {
    # canonical name — the ONLY name that may reach a stored record (#104).
    "name": {"type": "string", "required": True},
    "type": {"type": "string", "required": True, "enum": FIELD_TYPES},
    "extraction_path": {"type": "string", "required": True, "enum": EXTRACTION_PATHS},
    # which document sections may contain this field — routing per #101.
    "sections": {"type": "array", "required": True, "item_type": "string"},

    "unit": {"type": "string", "required": False},
    "required": {"type": "boolean", "required": False},
    # short transport key for #104 to cut decode tokens. TRANSPORT ONLY — mapped
    # back to `name` in code; must never reach a stored record.
    "wire_key": {"type": "string", "required": False},
    # literal labels that precede the value for fixed-anchor fields (#107 rules).
    "anchors": {"type": "array", "required": False, "item_type": "string"},
    # allowed values when type == enum.
    "enum": {"type": "array", "required": False},
    # element type/enum when type == array.
    "item_type": {"type": "string", "required": False, "enum": ITEM_TYPES},
    "item_enum": {"type": "array", "required": False},
    # per-field sanity bounds → #104's primary confidence signal.
    "bounds": {"type": "object", "required": False, "validator": _validate_bounds},
    "description": {"type": "string", "required": False},
    # populated once #105 has run; promoted regex once #107 has run. Absent until then.
    "exemplars": {"type": "array", "required": False},
    "rule": {"type": "object", "required": False},
}

_field_definition_validator = ObjectValidator(FIELD_DEFINITION_FIELDS)


# ── Form-type spec shape ────────────────────────────────────────────────────

def _validate_detection(val):
    if not isinstance(val, dict):
        return (False, ["detection must be an object"])
    errs = []
    signals = val.get("signals")
    if not isinstance(signals, list) or not signals:
        errs.append("detection.signals must be a non-empty array")
    else:
        for i, s in enumerate(signals):
            if not isinstance(s, dict) or "pattern" not in s:
                errs.append(f"detection.signals[{i}] must be an object with a 'pattern'")
    if "min_score" in val and not isinstance(val["min_score"], (int, float)):
        errs.append("detection.min_score must be numeric")
    return (len(errs) == 0, errs)


def _validate_fields_array(val):
    if not isinstance(val, list) or not val:
        return (False, ["fields must be a non-empty array"])
    errs = []
    seen = set()
    seen_wire = set()
    for i, fd in enumerate(val):
        ok, sub = _field_definition_validator.validate(fd)
        if not ok:
            errs.extend(f"fields[{i}]: {e}" for e in sub)
            continue
        name = fd["name"]
        if name in seen:
            errs.append(f"fields[{i}]: duplicate field name {name!r}")
        seen.add(name)
        wk = fd.get("wire_key")
        if wk:
            if wk in seen_wire:
                errs.append(f"fields[{i}]: duplicate wire_key {wk!r}")
            seen_wire.add(wk)
    return (len(errs) == 0, errs)


FIELD_SPEC_FIELDS = {
    "spec_id": {"type": "string", "required": True},
    "form_type": {"type": "string", "required": True},
    "population": {"type": "string", "required": True},
    "version": {"type": "string", "required": True},
    "description": {"type": "string", "required": False},
    # automatic A/B detection between the two 424B2 populations.
    "detection": {"type": "object", "required": True, "validator": _validate_detection},
    # the section vocabulary this spec routes fields into (#101).
    "sections": {"type": "array", "required": False, "item_type": "string"},
    "fields": {"type": "array", "required": True, "validator": _validate_fields_array},
    # cross-field / cross-exhibit sanity checks (e.g. maturity > pricing, EX-107).
    "cross_checks": {"type": "array", "required": False},
}

_field_spec_validator = ObjectValidator(FIELD_SPEC_FIELDS)


# ── Registry ────────────────────────────────────────────────────────────────

class SchemaRegistry:
    """Named-shape registry. Same role as #86's SchemaRegistry, for the
    extraction contract instead of the front-end contract."""

    def __init__(self):
        self._shapes = {}

    def register(self, name, validator):
        self._shapes[name] = validator
        return self

    def has(self, name):
        return name in self._shapes

    def get(self, name):
        return self._shapes.get(name)

    def names(self):
        return list(self._shapes)

    def validate(self, name, obj):
        v = self._shapes.get(name)
        if v is None:
            return (False, [f"Unknown shape: {name}"])
        return v.validate(obj)


# Module-level canonical registry. Shapes registered once, at import.
REGISTRY = SchemaRegistry()
REGISTRY.register(FIELD_SHAPE_NAME, _field_definition_validator)
REGISTRY.register(SPEC_SHAPE_NAME, _field_spec_validator)


def register_into(external_registry):
    """Adopt the field-spec shapes into another registry (e.g. #86's).

    `external_registry` only needs a `.register(name, validator)` method. Lets
    the canonical registry own these shapes without this module re-declaring them.
    """
    external_registry.register(FIELD_SHAPE_NAME, _field_definition_validator)
    external_registry.register(SPEC_SHAPE_NAME, _field_spec_validator)
    return external_registry


def field_spec_shape_as_data():
    """The field-spec shape as language-neutral JSON-serializable data.

    Re-exported so #86 (JS) can validate against the identical definition rather
    than re-typing it. `validator` callables are represented by name only.
    """
    def strip(fields):
        out = {}
        for k, v in fields.items():
            entry = {kk: vv for kk, vv in v.items() if kk != "validator"}
            if "validator" in v:
                entry["validator"] = v["validator"].__name__
            out[k] = entry
        return out

    return {
        "vocabularies": {
            "extraction_paths": list(EXTRACTION_PATHS),
            "field_types": list(FIELD_TYPES),
            "flag_severities": list(FLAG_SEVERITIES),
        },
        FIELD_SHAPE_NAME: strip(FIELD_DEFINITION_FIELDS),
        SPEC_SHAPE_NAME: strip(FIELD_SPEC_FIELDS),
    }


if __name__ == "__main__":
    # Self-check: the shapes are well-formed and the registry answers.
    assert REGISTRY.has(SPEC_SHAPE_NAME)
    assert REGISTRY.has(FIELD_SHAPE_NAME)
    ok, errs = REGISTRY.validate(
        FIELD_SHAPE_NAME,
        {"name": "x", "type": "percent", "extraction_path": "table-resident",
         "sections": ["key_terms"], "bounds": {"min": 0, "max": 100}},
    )
    assert ok, errs
    bad_ok, _ = REGISTRY.validate(
        FIELD_SHAPE_NAME,
        {"name": "x", "type": "banana", "extraction_path": "table-resident",
         "sections": []},
    )
    assert not bad_ok
    import json as _json
    print(_json.dumps(field_spec_shape_as_data(), indent=2))
    print("\nschema_registry self-check: PASS")
