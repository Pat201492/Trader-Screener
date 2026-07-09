# Screener Shortlist — Project Spec Input

> Fill this in **as you read** the Education resources. The output of this file *is* the Project screener spec.
> For each item: mark whether you want it, how you'd use it (Filter / Sort / Both / Display-only), and a priority (P1 must-have → P3 nice-to-have).
>
> **Cost column legend:** ✅ already collected by the old pipeline (free) · 🟡 computable from free OHLCV (yfinance) · 🔴 needs a paid/intraday feed.
>
> Reading that feeds this: [CME Futures Fundamentals](https://www.cmegroup.com/company/futures-fundamentals.html) · [Bookmap Microstructure](https://bookmap.com/blog/what-is-financial-market-microstructure) · [CME Intro to Options](https://www.cmegroup.com/education/courses/introduction-to-options) · [Cboe Options Institute](https://www.cboe.com/optionsinstitute/) · [CFA ETF Guide](https://rpc.cfainstitute.org/sites/default/files/docs/research-reports/hill_rf_brief_2025_etfs-evolving_module-1_2ed_online.v2.pdf) · [QuantInsti Risk Mgmt](https://blog.quantinsti.com/trading-risk-management/)

---

## 1. Instrument coverage — what does the screener cover?

Mark the ones you actually want to screen. (Be honest — each new class is a data-source decision.)

| Instrument | Cover it? | Have data now? | Notes / why |
|---|---|---|---|
| Equities (stocks) | [ ] | ✅ yes | core; old screener already does this |
| ETFs | [ ] | ✅ yes | already have universe + holdings |
| Options | ✅ | 🔴 paid (Polygon) | **decided (issue #45):** IV Rank/Percentile overlay + chain lookup, liquidity-gated subset of the equity/ETF universe — not a separate screened instrument class |
| Futures | [ ] | 🔴 no | yfinance won't cover well |
| Forex | [ ] | 🔴 no | data-source TBD |
| Crypto | [ ] | 🟡 partial | yfinance has some; CME futures separate |

**Decision:** which classes ship in v1? → Equities + ETFs (core screener) + options as a **per-underlying overlay** (IV Rank/Percentile columns + chain tab) on liquidity-gated names, fed by a paid feed (Polygon). Futures/forex/crypto remain undecided.

---

## 2. Metrics — the screener columns

For each: **Want?** [ ] · **Use:** F(ilter) / S(ort) / B(oth) / D(isplay) · **Priority:** P1/P2/P3 · **Notes.**
Strike out what you don't care about. Add rows for anything the reading surfaces that's missing.

### 2a. Price & momentum  🟡 (computable from free OHLCV)
> **Decided (issue #40):** trailing returns + the academically-correct momentum factor — cumulative return **t-12→t-2**, skipping the most recent month (Fama-French prior(2,12); including t-1 contaminates with short-term reversal). Converted to a **cross-sectional RS percentile** vs. the universe, re-ranked monthly. RS breakpoints computed from **liquid names only** (`passesLiquidityFloor()`) — no name failing the $ vol floor (issue #37) can surface via a momentum/RS sort.
> **Decided (issue #42):** every metric in this section is **lagged to the prior close** — computed on OHLCV through the previous period only, never same-day high/low/close (Chan). Validated by the A-vs-B truncation test (see `Project.md` § Look-ahead discipline layer, `lookahead-gate/`).
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Last price | [ ] | | | |
| % change (1d / 1w / 1m) | [ ] | | | |
| Return 1/3/6/12-month | ✅ | B | P1 | shipped as `return_1m/3m/6m/12m`; ohlcv-derived |
| Momentum factor (t-12→t-2, skip-month) | ✅ | B | P1 | shipped as `mom_factor`; Fama-French prior(2,12) |
| Relative strength rank (vs universe) | ✅ | B | P1 | shipped as `rs_percentile`; liquid-names-only breakpoints |
| 52-week range / % off high-low | [ ] | | | |
| Moving averages (50/200d) + cross | [ ] | | | trend |
| RSI | [ ] | | | overbought/oversold |
| MACD | [ ] | | | |
| VWAP | [ ] | | | intraday → 🔴 if real-time |

### 2b. Volatility  🟡 / 🔴
> **Decided (issue #42):** ATR, realized vol, and beta are OHLCV-derived and get the same **prior-close lag** discipline as §2a — see `Project.md` § Look-ahead discipline layer.
> **Decided (issue #45):** IV Rank/Percentile shipped as the required normalizer, side-by-side, never raw IV alone.
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| ATR / ATR% | [ ] | | | sizing + stops |
| Realized (historical) volatility | [ ] | | | |
| Beta (vs SPY) | [ ] | | | |
| Implied volatility (IV) | ✅ | D | P1 | 🔴 paid feed; raw input only — never shown as a standalone column, always beside IVR/IVP |
| IV rank / percentile | ✅ | B | P1 | shipped as `iv_rank`/`iv_percentile`; 🔴 paid feed + nightly IV-snapshot job; warming-up until history accrues |

### 2c. Liquidity & microstructure  🟡 / 🔴
> **Decided (issue #37):** liquidity is the **first-class gate** every other signal passes through. ADV + dollar volume shipped as default columns **and** a hard floor. Dollar volume proxies only **1 of Harris's 4 dimensions — depth**; spread (width), immediacy, resiliency need a quote feed = **not in v1**. ETFs gate on **basket liquidity**, not screen ADV (CFA ETF Guide).
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Average daily volume (ADV) | ✅ | B | P1 | shipped; ohlcv-derived. For ETFs a floor never a ceiling (basket rules) |
| Dollar volume (ADV × price) | ✅ | B | P1 | **hard tradeability gate** + default column; ETF rows judged on basket liquidity |
| ETF basket liquidity (Σ constituent $ADV × weight) | ✅ | F | P1 | free from already-collected holdings; the ETF's true capacity |
| Bid/ask spread (width) | ✅ | D | — | 🔴 needs quote feed — column present, **flagged not-in-v1** |
| Market depth / book | ✅ | D | — | 🔴 real-time only — present, **not-in-v1** |
| Resiliency (recovery after imbalance) | ✅ | D | — | 🔴 quote feed — present, **not-in-v1** |
| Float / shares outstanding | [ ] | | | ✅ have |

### 2d. Fundamentals & valuation  ✅ (already collected — free)
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Composite score (model.json) | [ ] | | | existing |
| Upside % (DCF/comps) | [ ] | | | existing |
| P/E | [ ] | | | |
| ROIC | [ ] | | | |
| Market cap / cap size | [ ] | | | |
| Sector | [ ] | | | |
| Data-quality score | [ ] | | | |

### 2e. Smart-money — YOUR MOAT  ✅ (already collected — free)
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Congress trades (recent buy/sell) | [ ] | | | unique edge |
| Insider trades (EDGAR) | [ ] | | | |
| Committee-conflict cross-tab | [ ] | | | rare; lean in |
| News sentiment | [ ] | | | from news.py |

### 2f. Macro context  ✅ (FRED — already collected)
> **Decided (issue #39):** shipped as a macro tab pairing fast/coincident (VIX regime bucket + term-structure
> inversion) with slow/leading (T10Y3M → Estrella–Mishkin recession probit). Soft-gates the screener's default
> $-volume + cap floors — never a hard block. Probit constants flagged for re-verification; inversion labeled
> a 12-month leading warning, not a sell trigger.
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Rates / yield curve | ✅ | B | P1 | T10Y3M → monthly probit → P(recession) badge (green/amber/red) |
| VIX | ✅ | B | P1 | VIXCLS regime bucket (calm/normal/elevated/stress/panic) + VIX/VIX3M term-structure inversion flag |
| Other FRED series | [ ] | | | list: ____ |

### 2g. Risk / sizing  🟡
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Suggested position size (ATR-based) | [ ] | | | from QuantInsti reading |
| Stop distance (ATR multiple) | [ ] | | | |
| Max drawdown (historical) | [ ] | | | |

### 2h. Options / derivatives signals  🔴 (needs an options-chain feed — **decided (issue #45): Polygon.io**, paid)
> Only relevant if §1 includes options. Background: [Derivatives — Types & Signals](../Education/Derivatives_Types_and_Trading_Signals.md) · [IV Rank/Percentile — OIC](../Education/summaries/iv-rank-iv-percentile-oic.md). Raw IV doesn't compare across names — **IV Rank/percentile is the must-have normalizer.**
> **Decided (issue #45):** IV Rank + IV Percentile shipped as joined screener columns, fed by a nightly IV-snapshot job (fixed 30-day-ATM definition) started now. Flagged "warming up" per ticker until ~3-6mo (IVP) / ~1yr (IVR) of snapshots accrue. IVR≥70/≤30 wired to sell/buy-premium hints, soft-gated under the macro VIX regime (issue #39) — a risk-off regime caveats the sell-premium hint rather than suppressing it.
> **Decided (issue #46):** Skew, put/call ratio, OI-by-strike and UOA shipped as joined screener columns (`skew_25d`, `put_call_oi`, `oi_max_strike`, `uoa`), all gated on chain liquidity (`optionsChainLiquid()` — ≥500 OI / ≥100 vol, fail-safe closed on unknown) since thin chains give unreliable readings. UOA + put/call extremes also feed `computeSmartMoneyScores()` as a 4th weighted leg — the options-side twin of the equity moat (issue #41). Greeks (Δ/Γ/Θ/V/ρ) ship as a per-contract dashboard (Options tab chain table + a nearest-ATM Greeks card on the stock detail page) — deliberately **not** a screener column.
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Implied volatility (IV) | ✅ | D | P1 | expected move; rises into events. Raw value — never a standalone column, always shown beside IVR/IVP |
| **IV Rank / IV Percentile** | ✅ | B | P1 | shipped as `iv_rank`/`iv_percentile`; "warming up" until nightly-snapshot history accrues (see §2b) |
| Volatility skew (put vs call IV) | ✅ | B | P1 | shipped as `skew_25d` (25Δ put/call IV ratio); flattening toward/under 1.05 is the notable tell, not the negative skew itself |
| Put/Call ratio | ✅ | B | P1 | shipped as `put_call_oi` (OI-based); contrarian, extremes flagged at ≥1.2 / ≤0.5; feeds the smart-money view |
| Open interest (by strike) | ✅ | B | P2 | shipped as `oi_max_strike`/`oi_max_strike_oi` (screener column) + magnet highlight in the full per-expiry chain table |
| Unusual options activity (UOA) | ✅ | B | P1 | shipped as `uoa` (volume ≥2× standing OI, directionally leaned by call/put-side split where available); feeds the smart-money view as the options-side twin |
| Greeks (delta/gamma/theta/vega/rho) | ✅ | D | P2 | shipped as a **per-position dashboard**, not a screener column — full per-contract Δ/Γ/Θ/V/ρ in the Options tab chain table, plus a nearest-ATM call/put Greeks card on the stock detail page |
| IV term structure (contango/backwardation) | [ ] | | | near vs far event risk |
| VIX / macro vol | [ ] | | | regime filter; shares FRED macro tab |

---

## 3. Trading style — sanity check
What's your actual horizon? It decides which metrics above are P1.
- [ ] Day trading (intraday) → needs 🔴 real-time feed, VWAP, depth, IV
- [ ] Swing (days–weeks) → momentum, ATR, RSI, vol; nightly data OK
- [ ] Position (weeks–months) → fundamentals + momentum; current pipeline mostly fits
- [ ] Mix: ____________________

> **Reality check:** if you're swing/position, the current free nightly pipeline + 🟡 OHLCV metrics covers ~80%. Only day-trading forces the 🔴 paid feed.

---

## 4. OUTPUT — the actual spec (fill last)

After reading + marking above, distill to this. *This* is what gets built.

**Instruments v1:** ____________________

**Top 10 screener columns (the default view):**
1. ____________________
2. ____________________
3. ____________________
4. ____________________
5. ____________________
6. ____________________
7. ____________________
8. ____________________
9. ____________________
10. ___________________

**Default filters:** ____________________

**Default sort:** ____________________

**Data feed decision:** [ ] free yfinance is enough  ·  [x] need paid (which: **Polygon.io**, options-chain feed) — because (issue #45): yfinance options are delayed/snapshot with no Greeks; IV Rank/Percentile is a must-have normalizer that needs a reliable daily IV snapshot + precomputed Greeks, which yfinance doesn't provide.

---

## 5. Open questions to resolve while reading
- [ ] Which instrument classes are realistically worth the data cost?
- [ ] Is my horizon nightly-compatible, or do I truly need intraday?
- [ ] Which metrics are decision-drivers vs just nice-to-look-at? (Only P1s become default columns.)
- [ ] Does the smart-money moat deserve its own dedicated view/filter?
