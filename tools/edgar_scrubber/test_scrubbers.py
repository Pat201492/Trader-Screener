"""
Gate for the per-form scrubber registry (issue #212, carved from #209). Same
convention as `test_crawl.py`: stdlib only, no network, run directly, exit 0 =
pass.

Every #212 acceptance criterion is checked here:

  * a Scrubber record carries id, form type, display name, one-line purpose, a
    query template (q + fixed forms) and a field_source of "taxonomy"/"spec";
  * field_source is "taxonomy" for the XBRL-tagged forms (10-K, 10-Q, 8-K) and
    "spec" for the prose form (424B2);
  * the registry ships at least 10-K, 10-Q, 8-K and 424B2, and the three
    existing saved queries resolve to the 424B2 scrubber unchanged;
  * load_scrubbers() validates every entry and raises naming the offending id;
  * building a SavedQuery from a scrubber + issuer list + date range produces
    the shape crawl accepts, proved by a JSON round-trip.

Run:  python tools/edgar_scrubber/test_scrubbers.py
"""

import json
import os
import tempfile

import crawl as cr
import scrubbers as sc

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def write_registry(entries):
    """Write a throwaway registry file and return its path."""
    fd, path = tempfile.mkstemp(prefix="scrubbers-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": "test", "scrubbers": entries}, f)
    return path


VALID_TAXONOMY = {
    "id": "10-k", "form_type": "10-K", "display_name": "10-K annual report",
    "purpose": "Annual report.", "q": "", "forms": ["10-K"],
    "field_source": "taxonomy",
}


# --------------------------------------------------------------------------- #
# 1. Record shape
# --------------------------------------------------------------------------- #
def test_record_shape():
    section("A Scrubber carries id, form, name, one-line purpose, template, field_source (#212)")
    s = sc.Scrubber.from_dict(VALID_TAXONOMY)
    check("has id", s.id == "10-k")
    check("has form_type", s.form_type == "10-K")
    check("has display_name", s.display_name == "10-K annual report")
    check("has one-line purpose", s.purpose and "\n" not in s.purpose)
    check("query template exposes q", hasattr(s, "q"))
    check("query template exposes fixed forms", s.forms == ("10-K",))
    check("field_source is taxonomy/spec", s.field_source in sc.FIELD_SOURCES)


# --------------------------------------------------------------------------- #
# 2. field_source per form family
# --------------------------------------------------------------------------- #
def test_field_source_by_form():
    section("field_source: taxonomy for tagged forms, spec for prose (#212)")
    scrubbers = sc.load_scrubbers()
    for fid in ("10-k", "10-q", "8-k"):
        check(f"{fid} is taxonomy (XBRL-tagged form)", scrubbers[fid].field_source == "taxonomy")
    check("424b2 is spec (prose form, field spec)", scrubbers["424b2"].field_source == "spec")


# --------------------------------------------------------------------------- #
# 3. Registry ships the four forms; existing queries resolve to 424B2 unchanged
# --------------------------------------------------------------------------- #
def test_registry_ships_forms_and_resolves_queries():
    section("Registry ships 10-K/10-Q/8-K/424B2; the 3 saved queries resolve to 424b2 (#212)")
    scrubbers = sc.load_scrubbers()
    for fid in ("10-k", "10-q", "8-k", "424b2"):
        check(f"registry defines {fid}", fid in scrubbers)

    queries = sc.stored_queries()
    check("found the existing saved queries", len(queries) >= 3)
    for q in queries:
        s = sc.resolve_scrubber(q, scrubbers)
        check(f"saved query {q.id!r} resolves to 424b2 unchanged",
              s is not None and s.id == "424b2")


# --------------------------------------------------------------------------- #
# 4. load_scrubbers validates and raises naming the offending id
# --------------------------------------------------------------------------- #
def test_load_validates_and_names_offender():
    section("A malformed entry fails at load, naming the offending id (#212)")

    def expect_error(label, entries, offender):
        path = write_registry(entries)
        try:
            sc.load_scrubbers(path)
            check(label, False)
        except sc.ScrubberError as exc:
            check(f"{label} (raises naming {offender!r})", offender in str(exc))
        finally:
            os.remove(path)

    bad_source = dict(VALID_TAXONOMY, id="bad-src", field_source="nonsense")
    expect_error("bad field_source rejected", [bad_source], "bad-src")

    multiline = dict(VALID_TAXONOMY, id="bad-purpose", purpose="line one\nline two")
    expect_error("multi-line purpose rejected", [multiline], "bad-purpose")

    form_missing = dict(VALID_TAXONOMY, id="bad-forms", form_type="10-K", forms=["10-Q"])
    expect_error("form_type absent from forms rejected", [form_missing], "bad-forms")

    dupes = [VALID_TAXONOMY, dict(VALID_TAXONOMY)]
    expect_error("duplicate id rejected", dupes, "10-k")

    # a valid registry loads clean
    path = write_registry([VALID_TAXONOMY])
    try:
        loaded = sc.load_scrubbers(path)
        check("valid registry loads", list(loaded) == ["10-k"])
    finally:
        os.remove(path)


# --------------------------------------------------------------------------- #
# 5. build_query -> the shape crawl accepts, proved by a JSON round-trip
# --------------------------------------------------------------------------- #
def test_build_query_round_trip():
    section("Scrubber + issuer list + date range -> SavedQuery, round-trips (#212)")
    scrubbers = sc.load_scrubbers()
    s = scrubbers["424b2"]
    q = s.build_query("my-run", ciks=["0000019617"], startdt="2025-01-01", enddt="2025-12-31")

    check("build_query returns a crawl.SavedQuery", isinstance(q, cr.SavedQuery))
    check("template q carried through", q.q == s.q)
    check("template forms carried through", tuple(q.forms) == s.forms)
    check("operator issuer knob carried through", tuple(q.ciks) == ("0000019617",))
    check("operator date knobs carried through", (q.startdt, q.enddt) == ("2025-01-01", "2025-12-31"))

    # round-trip: dict -> JSON -> dict -> SavedQuery, same shape crawl accepts
    restored = cr.SavedQuery.from_dict(json.loads(json.dumps(q.to_dict())))
    check("round-trips to an equal SavedQuery", restored.to_dict() == q.to_dict())
    check("restored query resolves back to 424b2",
          sc.resolve_scrubber(restored, scrubbers).id == "424b2")


def main():
    tests = [
        test_record_shape,
        test_field_source_by_form,
        test_registry_ships_forms_and_resolves_queries,
        test_load_validates_and_names_offender,
        test_build_query_round_trip,
    ]
    for t in tests:
        t()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nEDGAR scrubber registry (#212): PASS")


if __name__ == "__main__":
    main()
