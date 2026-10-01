"""
Contract test for the mock pipeline (issue #81). Same convention as
`lookahead-gate/ab_truncation_test.py`: stdlib only, run directly, exit 0 =
pass. Starts the real HTTP server on an ephemeral port, hits every route
web-dashboard/index.html calls, and asserts every field listed in issue #81's
"Fields the front-end reads that the pipeline must supply" is actually
present -- i.e. this is the check that the "-- / thin chain" gap is closed,
not just that the server boots.

Run:  python pipeline-mock/test_mock_server.py
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server as mock_server
from sample_data import OPTIONS

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
failures = []


def check(label, cond):
    if cond:
        print(f"  [ok] {label}")
    else:
        print(f"  [FAIL] {label}")
        failures.append(label)


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), mock_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        run_checks()
    finally:
        httpd.shutdown()
        thread.join(timeout=2)

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nMock pipeline contract test: PASS")


def run_checks():
    print("/health")
    status, body = get("/health")
    check("200 + ok:true", status == 200 and body.get("ok") is True)

    print("/api/stocks -- liquidity (#37), momentum (#40), volatility/sizing (#43/#44),"
          " smart-money (#41), IV/options-flow joined columns (#45/#46)")
    status, body = get("/api/stocks?limit=1000")
    check("200", status == 200)
    rows = body["stocks"]
    check("non-empty universe", len(rows) > 0)
    by_ticker = {r["ticker"]: r for r in rows}

    STOCKS_ROW_FIELDS = [
        "adv", "dollar_volume",
        "return_1m", "return_3m", "return_6m", "return_12m", "mom_factor",
        "atr", "atr_pct", "realized_vol", "max_drawdown",
        "ebit_ev_yield", "roic", "roc_greenblatt", "mkt_cap",
    ]
    for field in STOCKS_ROW_FIELDS:
        present = any(r.get(field) is not None for r in rows if not r.get("is_etf") or field in ("mkt_cap",))
        check(f"/api/stocks rows carry non-null `{field}` for at least one row", present)

    print("\nMagic Formula input provenance (issue #270): magic_source / magic_period_end"
          " / magic_derived / roc_nwc_floored on /api/stocks rows")
    check("some row has magic_source == 'xbrl'", any(r.get("magic_source") == "xbrl" for r in rows))
    check("some row has magic_source == 'yfinance'", any(r.get("magic_source") == "yfinance" for r in rows))
    check("some row has magic_source == 'mixed'", any(r.get("magic_source") == "mixed" for r in rows))
    check("some row has no magic_source (shows '–')", any(r.get("magic_source") is None for r in rows))
    check("some row carries a magic_period_end",
          any(r.get("magic_period_end") for r in rows))
    check("some row carries a non-empty magic_derived rule list",
          any((r.get("magic_derived") or "").strip() for r in rows))
    check("some row has roc_nwc_floored True (working capital floored at 0, #28)",
          any(r.get("roc_nwc_floored") is True for r in rows))

    check("ETF `holdings` present (basket-liquidity fallback path, QQQ)",
          isinstance(by_ticker.get("QQQ", {}).get("holdings"), list) and len(by_ticker["QQQ"]["holdings"]) > 0)
    check("ETF `basket_dollar_volume` present (precomputed upstream path, SPY)",
          by_ticker.get("SPY", {}).get("basket_dollar_volume") is not None)

    SMART_MONEY_FIELDS = ["congress_buys_90d", "congress_sells_90d", "insider_buys_90d",
                           "insider_sells_90d", "committee_conflict", "news_sentiment_30d"]
    for field in SMART_MONEY_FIELDS:
        present = any(r.get(field) is not None for r in rows)
        check(f"smart-money field `{field}` present for at least one row", present)
    check("smart-money fields absent (null) for at least one row (partial coverage is realistic)",
          any(r.get("congress_buys_90d") is None for r in rows))

    IV_JOINED_FIELDS = ["iv_rank", "iv_percentile", "atm_iv_30d", "iv_history_days",
                         "skew_25d", "put_call_oi", "oi_max_strike", "total_oi", "total_volume"]
    for field in IV_JOINED_FIELDS:
        present = any(r.get(field) is not None for r in rows)
        check(f"options-joined field `{field}` present on /api/stocks rows for at least one row", present)

    print("\nIV Rank / IV Percentile warming-up gate (issue #45/#81 acceptance:"
          " flagged warming until ~3-6mo / ~1yr of history)")
    newco, rampup, aapl = by_ticker["NEWCO"], by_ticker["RAMPUP"], by_ticker["AAPL"]
    check("NEWCO (45d history) is below both the IVP (126d) and IVR (252d) warm thresholds",
          newco["iv_history_days"] < 126 and newco["iv_history_days"] < 252)
    check("RAMPUP (140d history) has cleared IVP but not IVR",
          rampup["iv_history_days"] >= 126 and rampup["iv_history_days"] < 252)
    check("AAPL (300d history) has cleared both",
          aapl["iv_history_days"] >= 252)

    print("\n/api/options/{ticker} summary -- issue #45/#46 fields")
    status, body = get("/api/options/AAPL")
    check("200", status == 200)
    sm = body["summary"]
    OPT_SUMMARY_FIELDS = ["iv_rank", "iv_percentile", "atm_iv_30d", "iv_history_days",
                           "skew_25d", "put_call_oi", "oi_max_strike", "total_oi", "total_volume",
                           "call_oi", "put_oi", "call_volume", "put_volume"]
    for field in OPT_SUMMARY_FIELDS:
        check(f"summary.{field} present", sm.get(field) is not None)
    check("expirations non-empty", len(body["expirations"]) > 0)

    print("\nchain-liquidity gate (issue #46: >=500 OI / >=100 vol) -- NEWCO must read as thin,"
          " AAPL must clear it")
    status, thin_body = get("/api/options/NEWCO")
    thin_sm = thin_body["summary"]
    check("NEWCO total_oi/total_volume are BELOW the gate (thin chain)",
          thin_sm["total_oi"] < 500 or thin_sm["total_volume"] < 100)
    check("AAPL total_oi/total_volume CLEAR the gate", sm["total_oi"] >= 500 and sm["total_volume"] >= 100)

    print("\n/api/options/{ticker}/{expiration} -- per-contract greeks (issue #81: only `delta`"
          " shipped today; gamma/theta/vega/rho are the gap)")
    exp = body["expirations"][0]
    status, chain_body = get(f"/api/options/AAPL/{exp}")
    check("200", status == 200)
    contracts = chain_body["contracts"]
    check("contracts non-empty", len(contracts) > 0)
    GREEKS = ["delta", "gamma", "theta", "vega", "rho"]
    for g in GREEKS:
        check(f"per-contract `{g}` present on every contract", all(c.get(g) is not None for c in contracts))
    check("both call and put contracts present",
          any(c["type"] == "call" for c in contracts) and any(c["type"] == "put" for c in contracts))

    print("\n404 for a ticker outside the liquidity-gated options universe (graceful degradation"
          " path, issue #48's 'equity-only sheet still coherent')")
    status, _ = get("/api/options/MICRO")
    check("404, not a 500 or malformed payload", status == 404)
    check("MICRO really has no options entry in the fixture", "MICRO" not in OPTIONS)

    print("\n/api/macro -- issue #39 fields")
    status, macro = get("/api/macro")
    check("200", status == 200)
    for field in ["vix", "vixcls", "vix9d", "vix3m", "t10y3m"]:
        check(f"macro.{field} present", macro.get(field) is not None)

    print("\n/api/stocks/{ticker} single-row detail (tear sheet, issue #48)")
    status, row = get("/api/stocks/NVDA")
    check("200", status == 200)
    check("ticker matches", row.get("ticker") == "NVDA")
    status, _ = get("/api/stocks/NOPE")
    check("unknown ticker -> 404", status == 404)


if __name__ == "__main__":
    main()
