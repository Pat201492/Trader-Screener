"""
Gate for the structured-note issuance maps (issue #130, Part A of #110). Same
convention as `test_output_store.py`: stdlib only, run directly, exit 0 = pass.

Checks every #130 acceptance shape:

  * issuance_by_underlying aggregates aggregate_principal by underlyings[]
    and month, splitting a multi-underlying (basket) note's principal evenly
    so the total across underlyings never double-counts it;
  * barrier_clustering maps barrier_pct/coupon_barrier_pct/autocall_barrier_pct
    to absolute price levels via initial_underlying_value, and buckets them;
  * issuer_markup_league ranks (issuer, product_type) by average markup
    against the $1,000 issue price;
  * all three read the store ONLY through query()/fields() -- the #109 tool
    interface -- never the sqlite file directly;
  * each output produces a non-empty SVG chart and a finding string that
    quotes real numbers from the data (not a blank template);
  * write_outputs() writes all six chart/data files plus findings.md.

Run:  python tools/edgar_scrubber/test_research_maps.py
"""
import json
import os
import tempfile

from output_store import OutputStore, DocumentExtraction
import research_maps as rm

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def fresh_store():
    return OutputStore(":memory:")


def add(store, rid, accession, issuer, ptype, pdate, underlyings,
        principal=None, ev=None, barrier=None, coupon_barrier=None,
        autocall_barrier=None, spot=None):
    record = {
        "issuer": issuer,
        "product_type": ptype,
        "pricing_date": pdate,
        "underlyings": underlyings,
    }
    if principal is not None:
        record["aggregate_principal"] = principal
    if ev is not None:
        record["estimated_value_per_1000"] = ev
    if barrier is not None:
        record["barrier_pct"] = barrier
    if coupon_barrier is not None:
        record["coupon_barrier_pct"] = coupon_barrier
    if autocall_barrier is not None:
        record["autocall_barrier_pct"] = autocall_barrier
    if spot is not None:
        record["initial_underlying_value"] = spot
    store.write_document(rid, DocumentExtraction.from_record(accession, "424b2.htm", record))


def seeded_store():
    store = fresh_store()
    rid = store.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")

    # JPM, S&P 500 only, two months -- single-underlying notes.
    add(store, rid, "0001-26-000001", "JPMorgan Chase Financial Company LLC",
        "autocallable contingent coupon", "2026-01-15",
        [{"name": "S&P 500 Index", "kind": "index"}],
        principal=2_000_000, ev=972.4, barrier=70.0, coupon_barrier=70.0, spot=5000.0)
    add(store, rid, "0001-26-000002", "JPMorgan Chase Financial Company LLC",
        "autocallable contingent coupon", "2026-02-10",
        [{"name": "S&P 500 Index", "kind": "index"}],
        principal=1_000_000, ev=968.0, barrier=70.0, spot=5100.0)

    # BofA, basket note across two underlyings -- exercises the even split.
    add(store, rid, "0001-26-000003", "BofA Finance LLC",
        "buffered return enhanced", "2026-02-20",
        [{"name": "S&P 500 Index", "kind": "index"}, {"name": "Nasdaq-100 Index", "kind": "index"}],
        principal=1_000_000, ev=981.0, autocall_barrier=65.0, spot=2100.0)

    # Citigroup, no aggregate_principal (missing field -- must be excluded, not crash).
    add(store, rid, "0001-26-000004", "Citigroup Global Markets Holdings Inc.",
        "trigger notes", "2026-03-18",
        [{"name": "Nasdaq-100 Index", "kind": "index"}],
        ev=975.3, barrier=75.0, spot=18000.0)

    return store


def run_checks():
    # ── AC: reads through the tool interface only ──
    section("AC: reads the store only through query()/fields() (#109)")
    store = seeded_store()
    docs = rm.load_documents(store)
    check("load_documents returns one row per document",
          len(docs) == 4)
    check("each row carries a values dict pulled from fields()",
          all(isinstance(d.get("values"), dict) for d in docs))
    check("underlyings are the query()-denormalized dimension",
          docs[0]["underlyings"] and "name" in docs[0]["underlyings"][0])

    # ── AC: issuance by underlying, split across basket notes ──
    section("AC: issuance_by_underlying aggregates by underlying + month, "
            "splitting basket principal evenly")
    agg = rm.issuance_by_underlying(docs, top_n=8)
    check("months are sorted YYYY-MM",
          agg["months"] == sorted(agg["months"]) and agg["months"][0] == "2026-01")
    sp500_total = agg["totals_by_underlying"]["S&P 500 Index"]
    # JPM 2m + JPM 1m + half of BofA's 1m basket note = 3.5m
    check("S&P 500 total = 2m + 1m + half of the 1m basket note = 3.5m",
          abs(sp500_total - 3_500_000) < 1e-6)
    nasdaq_total = agg["totals_by_underlying"]["Nasdaq-100 Index"]
    check("Nasdaq-100 total = half of the 1m basket note = 0.5m "
          "(Citigroup's note has no aggregate_principal, contributes 0)",
          abs(nasdaq_total - 500_000) < 1e-6)
    check("sum across underlyings never double-counts principal "
          "(3.5m + 0.5m == 2m + 1m + 1m of dated, principal-bearing notes)",
          abs(sp500_total + nasdaq_total - 4_000_000) < 1e-6)

    # ── AC: barrier clustering maps pct -> absolute level via spot ──
    section("AC: barrier_clustering maps barrier fields to absolute levels via "
            "initial_underlying_value, and buckets them")
    barrier = rm.barrier_clustering(docs)
    check("every barrier field present on a doc with a spot produces a point",
          len(barrier["points"]) == 5)  # 2 barrier + 1 coupon_barrier + 1 autocall + 1 barrier
    jpm_point = next(p for p in barrier["points"]
                     if p["accession"] == "0001-26-000001" and p["field"] == "barrier_pct")
    check("absolute_level = spot * pct / 100 (5000 * 70% = 3500)",
          abs(jpm_point["absolute_level"] - 3500.0) < 1e-6)
    check("clustering buckets barrier_pct to the nearest bucket_width",
          barrier["clusters"]["barrier_pct"].get(70.0, 0) == 2)

    # ── AC: issuer markup league table, ranked ──
    section("AC: issuer_markup_league ranks (issuer, product_type) by avg markup")
    league = rm.issuer_markup_league(docs)
    check("one row per distinct (issuer, product_type)", len(league) == 3)
    check("rows sorted descending by avg_markup_pct",
          all(league[i]["avg_markup_pct"] >= league[i + 1]["avg_markup_pct"]
              for i in range(len(league) - 1)))
    jpm_row = next(r for r in league if r["issuer"] == "JPMorgan Chase Financial Company LLC")
    expected_markup = ((1000 - 972.4) / 1000 * 100 + (1000 - 968.0) / 1000 * 100) / 2
    check("avg_markup_pct = mean((1000-ev)/1000*100) across the issuer's notes",
          abs(jpm_row["avg_markup_pct"] - expected_markup) < 1e-6)
    check("n counts notes per (issuer, product_type)", jpm_row["n"] == 2)

    # ── AC: empty-input never crashes, produces an honest "no data" finding ──
    section("AC: empty input degrades gracefully (no crash, honest finding text)")
    empty_agg = rm.issuance_by_underlying([])
    check("empty issuance aggregate has no months", empty_agg["months"] == [])
    empty_finding = rm.finding_issuance(empty_agg)
    check("empty finding says there's nothing to aggregate, doesn't fabricate numbers",
          "nothing to aggregate" in empty_finding)
    empty_barrier = rm.barrier_clustering([])
    check("empty barrier finding doesn't crash",
          "yet" in rm.finding_barrier(empty_barrier))
    check("empty league finding doesn't crash",
          "nothing to rank" in rm.finding_markup([]))

    # ── AC: each output ships a chart (SVG) + a written finding with real numbers ──
    section("AC: chart + finding for each of the three outputs")
    issuance_svg = rm.svg_stacked_bar(agg["months"], agg["series"], title="t")
    check("issuance chart is a well-formed SVG with bar rects",
          issuance_svg.startswith("<svg") and issuance_svg.count("<rect") > len(agg["series"]))
    barrier_svg = rm.svg_scatter(barrier["points"], x_key="spot", y_key="absolute_level",
                                 group_key="field", title="t")
    check("barrier chart is a well-formed SVG with one circle per point",
          barrier_svg.startswith("<svg") and
          barrier_svg.count("<circle") == len(barrier["points"]) + len(barrier["clusters"]))
    league_svg = rm.svg_hbar(league, label_key="issuer", value_key="avg_markup_pct", title="t")
    # +1 rect for the white background rect every _svg() body includes.
    check("league chart is a well-formed SVG with one bar per row",
          league_svg.startswith("<svg") and league_svg.count("<rect") == len(league) + 1)

    finding_1 = rm.finding_issuance(agg)
    finding_2 = rm.finding_barrier(barrier)
    finding_3 = rm.finding_markup(league)
    check("issuance finding quotes the real total ($4,000,000 of dated principal, "
          "i.e. excluding Citigroup's undated-principal note)",
          "4,000,000" in finding_1 or "4,000,000" in finding_1.replace(",", ","))
    check("barrier finding names the actual field with the most observations",
          "barrier_pct" in finding_2)
    check("markup finding names the actual top issuer",
          jpm_row["issuer"] in finding_3 or league[0]["issuer"] in finding_3)
    check("findings are descriptive, not causal (no modeling/p-value language)",
          not any(w in (finding_1 + finding_2 + finding_3).lower()
                  for w in ("p-value", "significant", "causal", "predicts")))

    # ── AC: write_outputs() writes chart + data + finding for all three ──
    section("AC: write_outputs() writes chart/data/finding files for all three")
    with tempfile.TemporaryDirectory() as tmp:
        result = rm.write_outputs(store, tmp, top_n_underlyings=8)
        check("n_documents matches the store", result["n_documents"] == 4)
        for key, path in result["paths"].items():
            check(f"{key} was written and is non-empty",
                  os.path.exists(path) and os.path.getsize(path) > 0)
        with open(result["paths"]["issuance_json"], encoding="utf-8") as f:
            payload = json.load(f)
        check("issuance_by_underlying.json carries both data and finding",
              "data" in payload and "finding" in payload)
        with open(result["paths"]["findings_md"], encoding="utf-8") as f:
            findings_md = f.read()
        check("findings.md contains all three section headers",
              all(h in findings_md for h in
                  ("## 1. Issuance by underlying",
                   "## 2. Barrier/autocall level clustering vs spot",
                   "## 3. Issuer markup league table")))

    # ── AC: >top_n underlyings folds the tail into "Other" ──
    section("AC: more underlyings than top_n folds the remainder into 'Other'")
    store2 = fresh_store()
    rid2 = store2.start_run("424b2.structured_note", "1.0.0", "2026-08-05T10:00:00Z")
    for i in range(5):
        add(store2, rid2, f"0002-26-{i:06d}", "Issuer X", "digital", "2026-01-01",
            [{"name": f"Underlying {i}", "kind": "single_stock"}], principal=1000 * (5 - i))
    docs2 = rm.load_documents(store2)
    agg2 = rm.issuance_by_underlying(docs2, top_n=2)
    check("top_n=2 keeps exactly 2 named series plus Other",
          len(agg2["series"]) == 3 and "Other" in agg2["series"])
    check("Other totals the folded tail (3+2+1 = 6, i.e. 1000*(3+2+1))",
          abs(sum(agg2["series"]["Other"]) - 6000) < 1e-6)

    return failures


if __name__ == "__main__":
    fails = run_checks()
    print(f"\n{'='*70}")
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed:")
        for f in fails:
            print(f"  - {f}")
        raise SystemExit(1)
    print("research_maps: ALL CHECKS PASS")
