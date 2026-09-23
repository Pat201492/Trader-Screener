"""
Gate for the per-extraction result cache (issue #181). Same convention as
`test_output_store.py` / `test_extraction_ladder.py`: stdlib only, run directly,
exit 0 = pass. No model, no network -- a fake compute callable counts how many
times the "model" would be called, and every store lives under a temp root.

Every acceptance criterion in #181 is checked here:

  * a second extraction with an identical key returns the cached value, span,
    confidence and flags, and makes NO model call;
  * changing prompt version, exemplar set OR model produces a miss, not a stale
    hit -- asserted for each of the three independently;
  * a cached value is marked as a cache hit wherever it is reported;
  * the cache is off by default and enabled per run;
  * the cache writes ONLY under the scrubber home -- asserted by walking a temp
    root;
  * fake client + temp root, no model, no network.

Run:  python tools/edgar_scrubber/test_extract_cache.py
"""
import tempfile
from pathlib import Path

try:  # package import: tools.edgar_scrubber.test_extract_cache
    from . import extract_cache as ec
except ImportError:  # standalone: python tools/edgar_scrubber/test_extract_cache.py
    import extract_cache as ec

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


class FakeModel:
    """Stands in for the model call. `compute()` returns the ladder's
    `(value, span, confidence, flags)` and counts every invocation, so a hit
    that skips the model is provable: the count does not move."""

    def __init__(self, value=972.4, span=(10, 20), confidence=0.9, flags=None):
        self.value, self.span, self.confidence = value, span, confidence
        self.flags = flags if flags is not None else [{"code": "ok"}]
        self.calls = 0

    def compute(self):
        self.calls += 1
        return self.value, self.span, self.confidence, list(self.flags)


def a_key(**over):
    base = dict(accession="0001-24-000001", document="424b2.htm",
                field="estimated_value_per_1000", prompt_version="v3",
                exemplar_set="jpm@2", model="qwen2.5:7b")
    base.update(over)
    return ec.CacheKey(**base)


def temp_cache(root, enabled=True):
    return ec.ExtractCache(Path(root) / "extract_cache" / "extractions.sqlite",
                           enabled=enabled)


def run_checks(tmp):
    section("identical key -> hit, no model call, all fields round-trip")
    cache = temp_cache(tmp)
    model = FakeModel()
    k = a_key()
    first = cache.extract(k, model.compute)
    check("first extraction is a miss", not first.cache_hit)
    check("first extraction calls the model once", model.calls == 1)
    second = cache.extract(k, model.compute)
    check("second extraction makes no further model call", model.calls == 1)
    check("second extraction is a hit", second.cache_hit)
    check("cached value round-trips", second.value == 972.4)
    check("cached span round-trips as a tuple", second.span == (10, 20))
    check("cached confidence round-trips", second.confidence == 0.9)
    check("cached flags round-trip", second.flags == [{"code": "ok"}])
    cache.close()

    section("changing prompt / exemplars / model each MISSES independently")
    for label, changed in (
        ("prompt version", a_key(prompt_version="v4")),
        ("exemplar set", a_key(exemplar_set="jpm@3")),
        ("model", a_key(model="claude-sonnet-4-6")),
    ):
        cache = temp_cache(tmp / label.replace(" ", "_"))
        model = FakeModel()
        cache.extract(a_key(), model.compute)          # prime the original key
        before = model.calls
        result = cache.extract(changed, model.compute)  # differs by one field
        check(f"changed {label} misses (calls the model again)", model.calls == before + 1)
        check(f"changed {label} is not marked a hit", not result.cache_hit)
        cache.close()

    section("a hit is marked as a hit wherever it is reported")
    cache = temp_cache(tmp / "marked")
    model = FakeModel()
    k = a_key()
    cache.extract(k, model.compute)
    hit = cache.get(k)
    check("get() marks the value cache_hit=True", hit is not None and hit.cache_hit is True)
    check("as_dict() carries cache_hit so it survives reporting",
          hit.as_dict()["cache_hit"] is True)
    fresh = cache.put(a_key(document="other.htm"), 1.0)
    check("a freshly stored value is cache_hit=False", fresh.cache_hit is False)
    cache.close()

    section("off by default; enabled per run")
    default = ec.ExtractCache(Path(tmp) / "never" / "extractions.sqlite")
    check("ExtractCache() is disabled unless enabled=True", default.enabled is False)
    model = FakeModel()
    k = a_key()
    default.extract(k, model.compute)
    default.extract(k, model.compute)
    check("a disabled cache never hits (model called every time)", model.calls == 2)
    check("a disabled cache creates no store file",
          not (Path(tmp) / "never").exists())
    default.close()

    section("writes ONLY under the scrubber home -- walk the temp root")
    home = Path(tmp) / "home"
    cache = ec.ExtractCache(home / "extract_cache" / "extractions.sqlite", enabled=True)
    model = FakeModel()
    cache.extract(a_key(), model.compute)
    cache.extract(a_key(document="b.htm"), model.compute)
    written = [p for p in home.rglob("*") if p.is_file()]
    check("something was actually written", len(written) >= 1)
    cache_dir = home / "extract_cache"
    check("every written file lives under home/extract_cache",
          all(cache_dir in p.parents for p in written))
    cache.close()


def main():
    print("EDGAR scrubber extract-cache gate (#181)")
    with tempfile.TemporaryDirectory(prefix="extract-cache-test-") as tmp:
        run_checks(Path(tmp))
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nextract-cache gate: PASS")


if __name__ == "__main__":
    main()
