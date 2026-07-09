"""
Reference/mock implementation of the Stock-Data-Pipeline API surface that
`web-dashboard/index.html` reads (issue #81: "Pipeline gap -- screener
columns + Options tab show blanks until upstream fields are supplied").

The real pipeline lives in a separate repo (Stock-Data-Pipeline) that isn't
checked out alongside this one, so its `server.py`/`options.py`/`fred.py`
cannot be edited from here. This module is the same kind of artifact as
`lookahead-gate/lookahead_lag.py` (issue #42): a portable, stdlib-only
reference implementation of the documented "Upstream contract" tables in
`Project folder/Project.md`, meant to (a) unblock local front-end dev/testing
today and (b) be the concrete spec whoever ports this into the real pipeline
implements against.

Routes (mirrors the subset of `server.py`'s FastAPI shape the front-end
calls -- see ARCHITECTURE.md "API already exposed by the old server"):
  GET /health                          -> {"ok": true}
  GET /api/stocks?limit=&offset=       -> {"stocks": [...]}
  GET /api/stocks/{ticker}             -> one row (404 if unknown)
  GET /api/macro                       -> {"vix":..., "t10y3m":..., ...}
  GET /api/options/{ticker}            -> {"summary":..., "expirations":[...]}
  GET /api/options/{ticker}/{exp}      -> {"contracts": [...]}

Run:  python pipeline-mock/server.py [port, default 8000]
Then point web-dashboard/index.html's API field at http://127.0.0.1:8000
(that's already the page's default value).
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from sample_data import STOCKS, OPTIONS, MACRO

STOCKS_BY_TICKER = {r["ticker"]: r for r in STOCKS}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)

        if parts == ["health"]:
            return self._send_json({"ok": True})

        if parts == ["api", "stocks"]:
            rows = STOCKS
            offset = int(qs.get("offset", ["0"])[0])
            limit = int(qs.get("limit", [str(len(rows))])[0])
            return self._send_json({"stocks": rows[offset:offset + limit], "total": len(rows)})

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "stocks":
            row = STOCKS_BY_TICKER.get(parts[2].upper())
            if row is None:
                return self._send_json({"error": f"unknown ticker {parts[2]!r}"}, status=404)
            return self._send_json(row)

        if parts == ["api", "macro"]:
            return self._send_json(MACRO)

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "options":
            entry = OPTIONS.get(parts[2].upper())
            if entry is None:
                return self._send_json(
                    {"error": f"{parts[2]!r} is not in the options feed (liquidity-gated subset only)"},
                    status=404,
                )
            return self._send_json({"summary": entry["summary"], "expirations": entry["expirations"]})

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "options":
            entry = OPTIONS.get(parts[2].upper())
            chain = entry["chains"].get(parts[3]) if entry else None
            if chain is None:
                return self._send_json({"error": "unknown ticker/expiration"}, status=404)
            return self._send_json({"contracts": chain})

        return self._send_json({"error": f"not implemented in the mock: {parsed.path}"}, status=404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"pipeline-mock serving {len(STOCKS)} tickers ({len(OPTIONS)} with options coverage) "
          f"on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
