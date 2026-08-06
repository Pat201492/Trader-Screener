"""
Exemplar store: validated spans -> few-shot prompt assembly (issue #106, part
of #95). Blocked on, and built directly on top of, #105's `ValidationStore` --
every verdict written there lands here immediately (`ValidationStore.
write_exemplar`, called from `ValidationSession._write_exemplar`); this module
is the SELECTION and ASSEMBLY layer on top of that raw storage.

The mechanism that actually learns: not fine-tuning. A 7B does not
weight-update from ten validated documents, but it conditions hard on
well-chosen exemplars, and that works from example #1. When #95 says "the
model learns what I want it to grab", this module is the machinery that
delivers it.

Keyed by (form, issuer, field) -- issuer is in the key deliberately. 424B2
templates are near-identical WITHIN an issuer and materially different ACROSS
them (JPM, Citi, BofA Finance, Bank of Montreal share neither heading
structure nor vocabulary -- #99). Pooled exemplars make the model worse on
each. `form` keeps the pooled fallback tier from mixing unrelated form types
that happen to share a field name.

Fallback chain when a key is thin (`ExemplarProvider.resolve`):

    (form, issuer, field)  -- exact issuer, exact field
        -> (form, *, field)  -- pooled across every OTHER issuer, same form
            -> field description only  -- empty exemplar block; the field's
               DESCRIPTION/ANCHORS/BOUNDS (always present, see
               `extraction_ladder.build_messages`) is the only guidance left

Degrades gracefully, always produces a prompt: an unseen issuer never raises,
it just falls one tier.

Selection, not accumulation (`select_exemplars`). Context is 8k on the local
7B (#103), shared with the document chunk -- a prompt padded with twenty
near-identical accepts is worse than one with four well-chosen ones:

  * prefer CORRECTIONS over accepts -- a correction encodes a boundary the
    model got wrong; an accept only confirms what it already knew. More
    information per token.
  * prefer DIVERSITY -- collapse exemplars that teach the same shape (same
    anchor + same value type) so a genuinely different product type or value
    format survives instead of being crowded out by near-duplicates.
  * keep at least one NEGATIVE when one exists -- absence is a valid answer,
    and this is the exemplar that suppresses a hallucinated value.
  * CAP the count per field, evict on this ranking -- not recency alone.
  * exemplars stay minimal (snippet + answer, via `validation.render_exemplar`)
    -- not the full surrounding section.

Prompt ordering is load-bearing (`extraction_ladder.build_messages`,
`static_prefix`). llama.cpp and Ollama reuse KV cache for identical prompt
PREFIXES:

    [ static: system prompt ]                    <- identical across all calls
    [ static: schema / field spec ]               <- identical
    [ static: exemplars for (issuer, field) ]     <- identical within an issuer
    [ VARYING: document chunk ]                   <- must be last

With this ordering ~600 tokens of exemplars prefill ONCE per issuer instead of
once per document. `ExtractionLadder._check_static_prefix` asserts the static
prefix is byte-identical across calls within an issuer, or this saving
disappears silently during some future refactor (#111).

Versioning: every `ExemplarSet` carries a `version` -- a short content hash of
the exact rendered lines selected, in order. `_lookup_exemplars` in
`extraction_ladder.py` reads it off the specific set actually used and stamps
it into #104's provenance log on every extraction, so when quality moves you
can tell whether exemplars, model, or documents changed -- three different
fixes.

Backlog: at ~500+ validated labels per form a LoRA on the 7B becomes viable
and would cut prompt size and latency further. NOT in scope here -- few-shot
has to be working and measured first (#108), and the labels do not exist yet.
Noted so the store (#105's SQLite table) exports a training set later rather
than being reshaped for it: it already carries every field this needs
(kind, value, anchor, span_text, accession, document).

stdlib only. Run the self-check:  python tools/edgar_scrubber/exemplars.py
"""

import hashlib
from dataclasses import dataclass

try:  # package import: tools.edgar_scrubber.exemplars
    from .validation import ValidationStore
except ImportError:  # standalone: python tools/edgar_scrubber/exemplars.py
    from validation import ValidationStore


POSITIVE, CORRECTED, NEGATIVE = "positive", "corrected", "negative"
_KIND_RANK = {CORRECTED: 0, POSITIVE: 1, NEGATIVE: 2}

# The tier a resolved set actually came from -- stamped into the version hash
# so a set that fell back reads differently from one that didn't, even if the
# rendered lines happened to coincide.
TIER_ISSUER = "issuer"        # exact (form, issuer, field)
TIER_ISSUER_FORM = "issuer+form"  # exact issuer, topped up from the pool
TIER_FORM = "form"            # pooled across every other issuer
TIER_NONE = "none"            # nothing at all -- field description only


# --------------------------------------------------------------------------- #
# Rows + selection policy
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExemplarRow:
    """One candidate exemplar, as stored by #105's `ValidationStore.
    write_exemplar` and read back by `exemplar_rows`. `rendered` is the exact
    line `build_messages` will print; the rest is what selection reasons
    about."""

    issuer: str
    kind: str            # positive | corrected | negative
    rendered: str
    value: object
    anchor: str
    span_text: str
    accession: str
    document: str

    @classmethod
    def from_dict(cls, d):
        return cls(issuer=d.get("issuer"), kind=d["kind"], rendered=d["rendered"],
                    value=d.get("value"), anchor=d.get("anchor"),
                    span_text=d.get("span_text"), accession=d.get("accession"),
                    document=d.get("document"))


def _diversity_key(row):
    """What counts as 'teaches the same thing' for de-duplication: a negative
    is always one slot (there is only one way to say 'absent'); a positive/
    corrected collapses by (anchor, value shape) so twenty near-identical
    accepts under the same label do not crowd out a different product type or
    a differently-shaped value."""
    if row.kind == NEGATIVE:
        return (NEGATIVE,)
    return (row.anchor, type(row.value).__name__)


def select_exemplars(rows, *, cap=4):
    """The #106 selection policy: rank corrections ahead of accepts, collapse
    near-duplicates, reserve one slot for a negative when one is available,
    cap the total. Eviction is by this ranking, not recency -- `rows` may
    arrive in any order (the store returns most-recent-first; that only
    matters as a tie-break within a kind)."""
    if cap <= 0 or not rows:
        return []

    ranked = sorted(rows, key=lambda r: _KIND_RANK.get(r.kind, 9))
    negative = next((r for r in ranked if r.kind == NEGATIVE), None)
    reserve = 1 if negative is not None else 0
    budget = max(0, cap - reserve)

    picked, seen = [], set()
    for r in ranked:
        if r.kind == NEGATIVE:
            continue
        if len(picked) >= budget:
            break
        key = _diversity_key(r)
        if key in seen:
            continue
        seen.add(key)
        picked.append(r)

    if negative is not None:
        picked.append(negative)
    return picked[:cap]


# --------------------------------------------------------------------------- #
# Keys, sets, versioning
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExemplarKey:
    form: str
    issuer: str
    field: str


def _version_hash(form, issuer, field, tier, lines):
    """A short, deterministic content hash of the exact set that was
    assembled: same form/issuer/field/tier/lines -> same version, any change
    to any of them -> a different one. Stamped into #104's provenance
    (`Provenance.exemplar_set`) so a quality regression can be traced to a
    specific exemplar set rather than guessed at."""
    h = hashlib.sha1()
    h.update(f"{form}|{issuer}|{field}|{tier}".encode("utf-8"))
    for line in lines:
        h.update(b"\x00")
        h.update(line.encode("utf-8"))
    return h.hexdigest()[:10]


@dataclass(frozen=True)
class ExemplarSet:
    """The result of resolving one (form, issuer, field): which tier answered
    it, the selected rows in final prompt order, and the version stamped into
    provenance. `rows` is empty (tier=TIER_NONE) when the fallback chain
    bottoms out -- the caller still gets a set, never an error."""

    key: ExemplarKey
    tier: str
    rows: tuple
    version: str

    def lines(self):
        return [r.rendered for r in self.rows]

    @classmethod
    def empty(cls, key):
        return cls(key=key, tier=TIER_NONE, rows=(),
                    version=_version_hash(key.form, key.issuer, key.field, TIER_NONE, []))


# --------------------------------------------------------------------------- #
# Provider -- the ladder's exemplars= plug point
# --------------------------------------------------------------------------- #

class ExemplarProvider:
    """Bound to one form (one `FieldSpec` = one form), this is what
    `ExtractionLadder(exemplars=...)` is constructed with. It is callable as
    `provider(issuer, field)` -- the exact shape
    `extraction_ladder._lookup_exemplars` already calls for a plain store --
    and additionally exposes `.resolve(issuer, field) -> ExemplarSet`, which
    `_lookup_exemplars` detects and prefers so the per-call version reaches
    provenance.
    """

    def __init__(self, store, form, *, cap=4, thin_below=2):
        self.store = store
        self.form = form
        self.cap = cap
        self.thin_below = thin_below  # fewer RAW verdicts for the issuer than this -> thin

    def _rows(self, field, *, issuer=None, exclude_issuer=None):
        raw = self.store.exemplar_rows(self.form, field, issuer=issuer,
                                       exclude_issuer=exclude_issuer)
        return [ExemplarRow.from_dict(d) for d in raw]

    def resolve(self, issuer, field):
        """The fallback chain: (form, issuer, field) -> (form, *, field) ->
        empty. Never raises -- an unseen issuer just falls a tier, per #106's
        acceptance ("fallback chain covers unseen issuers without erroring")."""
        key = ExemplarKey(self.form, issuer, field)
        if not issuer:
            return ExemplarSet.empty(key)

        # "Thin" is measured on RAW validated support (how many verdicts this
        # (issuer, field) has), not on the post-selection count -- selection
        # can legitimately dedup several real corrections down to one
        # rendered line (same anchor + value shape) without the key actually
        # being under-supported.
        raw_exact = self._rows(field, issuer=issuer)
        exact = select_exemplars(raw_exact, cap=self.cap)
        if len(raw_exact) >= self.thin_below:
            if not exact:
                return ExemplarSet.empty(key)
            lines = [r.rendered for r in exact]
            return ExemplarSet(key=key, tier=TIER_ISSUER, rows=tuple(exact),
                               version=_version_hash(self.form, issuer, field,
                                                     TIER_ISSUER, lines))

        raw_pooled = self._rows(field, exclude_issuer=issuer)
        combined = select_exemplars(list(raw_exact) + list(raw_pooled), cap=self.cap)

        if not combined:
            return ExemplarSet.empty(key)

        tier = TIER_ISSUER_FORM if exact else TIER_FORM
        lines = [r.rendered for r in combined]
        return ExemplarSet(key=key, tier=tier, rows=tuple(combined),
                           version=_version_hash(self.form, issuer, field, tier, lines))

    def __call__(self, issuer, field):
        """Ladder-compatible callable fallback (plain lines, no version) --
        `resolve()` is what `extraction_ladder._lookup_exemplars` actually
        prefers when it detects this class; `__call__` keeps this a drop-in
        `ValidationStore` replacement for any caller that only wants lines."""
        es = self.resolve(issuer, field)
        return es.lines() if es.rows else None


if __name__ == "__main__":
    import sys

    try:
        from validation import ValidationSession, FieldVerdict
        from field_spec import load_specs
        import extraction_ladder as el
    except ImportError:
        from .validation import ValidationSession, FieldVerdict
        from .field_spec import load_specs
        from . import extraction_ladder as el

    failures = []

    def check(label, cond):
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    spec = load_specs()["structured_note"]
    FORM = spec.form_type
    FIELD = "barrier_pct"
    JPM = "JPMorgan Chase Financial Company LLC"
    CITI = "Citigroup Global Markets Holdings Inc."

    store = ValidationStore(":memory:")
    session = ValidationSession(store, spec, session_id="s1", target_n=50)

    print("Seeding JPM with a correction + a reject (a thin-but-real key)")
    session.record_verdict("0001", "424b2.htm",
                           FieldVerdict.correct(FIELD, 70.0), issuer=JPM)
    session.complete_document("0001", "424b2.htm", issuer=JPM)
    session.record_verdict("0002", "424b2.htm",
                           FieldVerdict.reject(FIELD), issuer=JPM)
    session.complete_document("0002", "424b2.htm", issuer=JPM)

    provider = ExemplarProvider(store, FORM, cap=4, thin_below=3)

    jpm_set = provider.resolve(JPM, FIELD)
    check("JPM key resolves from its own tier when it has any exemplars "
          "(below thin_below but not empty)", jpm_set.tier in (TIER_ISSUER, TIER_ISSUER_FORM))
    check("JPM's correction and reject both made it in",
          {r.kind for r in jpm_set.rows} == {CORRECTED, NEGATIVE})

    citi_set = provider.resolve(CITI, FIELD)
    check("unseen issuer (Citi) does not error and falls back to the pooled "
          "(form, *, field) tier", citi_set.tier == TIER_FORM)
    check("Citi's fallback picks up JPM's exemplars (same form, pooled)",
          len(citi_set.rows) > 0)

    unseen_field_set = provider.resolve(CITI, "some_field_never_validated")
    check("a field with zero exemplars anywhere degrades to empty, not an error",
          unseen_field_set.tier == TIER_NONE and unseen_field_set.rows == ())

    print("\nSelection policy: cap, diversity, corrections-first, negative kept")
    many_rows = (
        [ExemplarRow(JPM, POSITIVE, f"barrier_pct: {v} [Barrier: {v}%]", v,
                     "Barrier", f"{v}%", "acc", "doc")
         for v in (60.0, 61.0, 62.0, 63.0)]  # same anchor+type -> collapse to 1
        + [ExemplarRow(JPM, CORRECTED, "barrier_pct: 70.0 [70.00%]  (corrected)",
                       70.0, "Coupon Barrier", "70.00%", "acc", "doc")]
        + [ExemplarRow(JPM, NEGATIVE, "barrier_pct: absent in a prior filing", None,
                       None, None, "acc", "doc")]
    )
    picked = select_exemplars(many_rows, cap=3)
    check("cap respected", len(picked) <= 3)
    check("correction ranked first", picked[0].kind == CORRECTED)
    check("near-duplicate positives collapsed to one (diversity)",
          sum(1 for r in picked if r.kind == POSITIVE) <= 1)
    check("negative kept even under a tight cap", any(r.kind == NEGATIVE for r in picked))

    print("\nVersioning: stable for the same set, changes when the set changes")
    v1 = provider.resolve(JPM, FIELD).version
    v2 = provider.resolve(JPM, FIELD).version
    check("same (issuer, field) with no new verdicts -> same version", v1 == v2)
    session.record_verdict("0003", "424b2.htm",
                           FieldVerdict.correct(FIELD, 65.0), issuer=JPM)
    session.complete_document("0003", "424b2.htm", issuer=JPM)
    v3 = provider.resolve(JPM, FIELD).version
    check("a new verdict changes the version", v3 != v1)

    print("\nProvenance handoff: ExtractionLadder stamps the resolved version, "
          "not a static constructor default")

    class _ScriptedClient:
        def __init__(self, value, span):
            self.value, self.span = value, span

        def chat_completion(self, messages, model=None, response_format=None, **kw):
            import json as _json
            key = next(iter(response_format["json_schema"]["schema"]["properties"]))
            body = _json.dumps({key: {"v": self.value, "s": list(self.span), "c": 0.9}})
            return {"choices": [{"message": {"content": body}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5}}

    ladder = el.ExtractionLadder(
        spec, exemplars=provider,
        local_client=_ScriptedClient(65.0, (0, 5)), local_model="qwen2.5:7b",
        exemplar_set_version="fallback-if-unresolved",
    )
    r = ladder.extract(FIELD, text="Barrier: 65.00% of the Initial Value.",
                       issuer=JPM, accession="0003", document="424b2.htm")
    expected_version = provider.resolve(JPM, FIELD).version
    check("provenance carries the resolved set's own version, not the "
          "ladder's constructor fallback",
          ladder.log.entries[-1].provenance["exemplar_set"] == expected_version)

    print("\nStatic-prefix ordering: byte-identical within an issuer across "
          "two different document chunks")
    field_def = spec.field(FIELD)
    lines_a = provider.resolve(JPM, FIELD).lines()
    full_static = el.static_prefix(field_def, lines_a)
    check("static_prefix starts with the system prompt",
          full_static.startswith(el.SYSTEM_PROMPT))
    static_user_part = full_static[len(el.SYSTEM_PROMPT) + 2:]  # past "\n\n"

    msgs_a = el.build_messages(field_def, "Document A text goes here.", exemplars=lines_a)
    msgs_b = el.build_messages(field_def, "A COMPLETELY DIFFERENT document B.",
                               table_context="k: v", exemplars=lines_a)
    check("both calls' system message is the shared constant",
          msgs_a[0]["content"] == el.SYSTEM_PROMPT == msgs_b[0]["content"])
    check("both calls' user message starts with the SAME static prefix, "
          "regardless of table_context/source text",
          msgs_a[1]["content"].startswith(static_user_part) and
          msgs_b[1]["content"].startswith(static_user_part))
    check("the two user messages differ only after the static prefix "
          "(the varying document chunk)",
          msgs_a[1]["content"] != msgs_b[1]["content"])
    check("varying document chunk lands last, after the static exemplar block",
          msgs_a[1]["content"].endswith("SOURCE TEXT:\nDocument A text goes here.") and
          msgs_b[1]["content"].endswith(
              "SOURCE TEXT:\nA COMPLETELY DIFFERENT document B."))

    # A second call for the same issuer/field with a different document must
    # not trip the ladder's own guard -- the STATIC part hasn't moved.
    r2 = ladder.extract(FIELD, text="A different chunk of source text entirely.",
                        issuer=JPM, accession="0004", document="424b2.htm")
    check("ladder's own static-prefix guard does not fire across documents "
          "for the same issuer (only the doc chunk varied)", r2 is not None)

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nexemplars self-check: PASS")
