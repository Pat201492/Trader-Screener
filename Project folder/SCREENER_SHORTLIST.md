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
| Options | [ ] | 🔴 no | needs option-chain feed (greeks/IV) |
| Futures | [ ] | 🔴 no | yfinance won't cover well |
| Forex | [ ] | 🔴 no | data-source TBD |
| Crypto | [ ] | 🟡 partial | yfinance has some; CME futures separate |

**Decision:** which classes ship in v1? → __________________________

---

## 2. Metrics — the screener columns

For each: **Want?** [ ] · **Use:** F(ilter) / S(ort) / B(oth) / D(isplay) · **Priority:** P1/P2/P3 · **Notes.**
Strike out what you don't care about. Add rows for anything the reading surfaces that's missing.

### 2a. Price & momentum  🟡 (computable from free OHLCV)
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Last price | [ ] | | | |
| % change (1d / 1w / 1m) | [ ] | | | |
| Return 1/3/6/12-month | [ ] | | | momentum ranking |
| Relative strength rank (vs universe) | [ ] | | | |
| 52-week range / % off high-low | [ ] | | | |
| Moving averages (50/200d) + cross | [ ] | | | trend |
| RSI | [ ] | | | overbought/oversold |
| MACD | [ ] | | | |
| VWAP | [ ] | | | intraday → 🔴 if real-time |

### 2b. Volatility  🟡 / 🔴
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| ATR / ATR% | [ ] | | | sizing + stops |
| Realized (historical) volatility | [ ] | | | |
| Beta (vs SPY) | [ ] | | | |
| Implied volatility (IV) | [ ] | | | 🔴 needs options feed |
| IV rank / percentile | [ ] | | | 🔴 |

### 2c. Liquidity & microstructure  🟡 / 🔴
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Average daily volume (ADV) | [ ] | | | |
| Dollar volume (ADV × price) | [ ] | | | tradeability filter |
| Bid/ask spread | [ ] | | | 🔴 needs quote feed |
| Market depth / book | [ ] | | | 🔴 real-time only |
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
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Rates / yield curve | [ ] | | | regime filter |
| VIX | [ ] | | | tail-risk gauge |
| Other FRED series | [ ] | | | list: ____ |

### 2g. Risk / sizing  🟡
| Metric | Want? | Use | Priority | Notes |
|---|---|---|---|---|
| Suggested position size (ATR-based) | [ ] | | | from QuantInsti reading |
| Stop distance (ATR multiple) | [ ] | | | |
| Max drawdown (historical) | [ ] | | | |

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

**Data feed decision:** [ ] free yfinance is enough  ·  [ ] need paid (which: Polygon / Alpaca / Tiingo / IBKR) — because: ____________________

---

## 5. Open questions to resolve while reading
- [ ] Which instrument classes are realistically worth the data cost?
- [ ] Is my horizon nightly-compatible, or do I truly need intraday?
- [ ] Which metrics are decision-drivers vs just nice-to-look-at? (Only P1s become default columns.)
- [ ] Does the smart-money moat deserve its own dedicated view/filter?
