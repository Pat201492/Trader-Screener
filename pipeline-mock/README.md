# pipeline-mock — reference implementation for issue #81

The real data pipeline lives in a separate repo, [Stock-Data-Pipeline](https://github.com/Pat201492/Stock-Data-Pipeline),
not checked out alongside this one — so this repo can't edit its `server.py`/`options.py`/`fred.py`
directly. Issue #81 found that every "add upstream" step from issues #35–#48 was still outstanding
there, which is why the screener columns and Options tab render `–` / "thin chain" against a real
backend even though the front-end itself is correct.

This directory is a **portable, stdlib-only reference implementation** of every field those issues'
"Upstream contract" tables (see `Project folder/Project.md`) specify — the same posture as
[`lookahead-gate/lookahead_lag.py`](../lookahead-gate/lookahead_lag.py) (issue #42): not the real
pipeline, but the concrete spec + a runnable fixture, so (a) the front-end can be developed/tested
locally today, and (b) whoever ports this into Stock-Data-Pipeline has exact field names/shapes/gate
thresholds to implement against, not just prose.

## Run it

```
python pipeline-mock/server.py        # serves on http://127.0.0.1:8000
```

Open `web-dashboard/index.html` — its API field already defaults to `http://127.0.0.1:8000`, so no
config change is needed. The screener columns and Options tab should render real values; a few
tickers are deliberately seeded to exercise the edge-case UI states instead of just the happy path:

| Ticker | Exercises |
|---|---|
| `NEWCO` | IV Rank **and** IV Percentile both "warming up" (45d of snapshot history) + a thin options chain (`< 500 OI` / `< 100 vol`) |
| `RAMPUP` | IV Percentile ready (140d ≥ 126d threshold), IV Rank still "warming" (< 252d) — isolates the partial-warmup state |
| `AAPL` | Fully warmed IV history, flattening skew (`skew_25d=1.02`), call-side UOA (`⚠ UOA` flagged, call-direction) |
| `MSFT` | Put/call ratio at the capitulation/fear extreme (`≥1.2`) |
| `SPY` | ETF basket liquidity via the **precomputed** `basket_dollar_volume` upstream field |
| `QQQ` | ETF basket liquidity via the **holdings-array fallback** (`holdings:[{ticker,weight,adv,price}]`) |
| `MICRO` | Below the liquidity floor at higher `$ vol floor` settings; **no options coverage at all** (outside the liquidity-gated options universe — 404 from `/api/options/MICRO`, exercises the tear sheet's "equity-only, options unavailable" degrade path) |
| `TSLA` | Bearish momentum + low IV Rank (buy-premium hint) + `committee_conflict:true` |
| `KO` | Momentum near zero → range-bound/mean-reverting thesis (Stop Dist/Size render `mean-revert`, not a number) |

## Routes

Mirrors the subset of the real `server.py` FastAPI shape the front-end calls (see
`ARCHITECTURE.md`):

- `GET /health`
- `GET /api/stocks?limit=&offset=`
- `GET /api/stocks/{ticker}`
- `GET /api/macro`
- `GET /api/options/{ticker}`
- `GET /api/options/{ticker}/{expiration}`

Routes the front-end also calls but that are out of scope for issue #81 (`/api/commodities`,
`/api/stocks/{ticker}/commodities`, `/api/integrity`) are **not** implemented — those tabs will show
the front-end's existing "unreachable" error state, which is the correct behavior for an endpoint
that isn't part of this fixture.

## Test

```
python pipeline-mock/test_mock_server.py
```

Starts the real server on an ephemeral port and asserts every field issue #81 lists is present,
including the IV warming-up staging and the chain-liquidity gate (`≥500 OI` / `≥100 vol`) correctly
reading `NEWCO` as thin and `AAPL` as clear. Exit 0 = pass, same convention as
`lookahead-gate/ab_truncation_test.py`.

## Porting this into the real pipeline

`sample_data.py` is fixture data, not a spec to copy verbatim — the actual computations (ADV,
returns, ATR, IV Rank/Percentile off a real `iv_snapshots` history, Greeks from Polygon, etc.) belong
in Stock-Data-Pipeline's real collectors, per the "Upstream contract" tables already documented in
`Project folder/Project.md`. What *should* port over unchanged is the **response shape**: field
names, nesting (`{"stocks":[...]}`, `{"summary":..., "expirations":[...]}`,
`{"contracts":[...]}`), and null-vs-absent semantics (a field that hasn't been collected yet is
`null`, never omitted — the front-end's `fmt()`/`pct()` helpers render `null` as `–` by design).
