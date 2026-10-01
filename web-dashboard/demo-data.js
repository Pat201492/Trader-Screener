/* Demo-mode data provider for the Trader-Screener dashboard.
 *
 * The dashboard normally fetches from a live pipeline API (default
 * http://127.0.0.1:8000). GitHub Pages is static — no server — so this file
 * fabricates SCHEMA-VALID synthetic responses so the whole UI is clickable with
 * no backend. It is NOT real market data (clearly labelled in the UI). Only the
 * raw upstream fields are supplied; the dashboard computes the derived views
 * (magic rank, smart-money score, sizing, ranks) itself, exactly as in prod.
 *
 * Values are deterministic per ticker (seeded PRNG) so the demo looks stable
 * across reloads. Wire-up: index.html sets DEMO_ON and routes jget() here.
 */
(function () {
  // --- deterministic PRNG (mulberry32), seeded by a string hash ---
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rng(seed) { let a = seed >>> 0; return function () { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  const f = (v, d) => Number(v.toFixed(d));
  const between = (r, lo, hi, d = 2) => f(lo + (hi - lo) * r(), d);
  const iBetween = (r, lo, hi) => Math.round(lo + (hi - lo) * r());
  const chance = (r, p) => r() < p;

  // ticker, name, sector  (a spread across sectors; a handful of ETFs)
  const UNIVERSE = [
    ['AAPL', 'Apple Inc.', 'Technology'], ['MSFT', 'Microsoft Corp.', 'Technology'],
    ['NVDA', 'NVIDIA Corp.', 'Technology'], ['AVGO', 'Broadcom Inc.', 'Technology'],
    ['GOOGL', 'Alphabet Inc.', 'Communication Services'], ['META', 'Meta Platforms', 'Communication Services'],
    ['AMZN', 'Amazon.com', 'Consumer Discretionary'], ['TSLA', 'Tesla Inc.', 'Consumer Discretionary'],
    ['HD', 'Home Depot', 'Consumer Discretionary'], ['MCD', "McDonald's", 'Consumer Discretionary'],
    ['JPM', 'JPMorgan Chase', 'Financials'], ['BAC', 'Bank of America', 'Financials'],
    ['V', 'Visa Inc.', 'Financials'], ['MA', 'Mastercard', 'Financials'],
    ['UNH', 'UnitedHealth', 'Health Care'], ['JNJ', 'Johnson & Johnson', 'Health Care'],
    ['LLY', 'Eli Lilly', 'Health Care'], ['PFE', 'Pfizer', 'Health Care'],
    ['XOM', 'Exxon Mobil', 'Energy'], ['CVX', 'Chevron', 'Energy'], ['COP', 'ConocoPhillips', 'Energy'],
    ['CAT', 'Caterpillar', 'Industrials'], ['DE', 'Deere & Co.', 'Industrials'], ['GE', 'GE Aerospace', 'Industrials'],
    ['PG', 'Procter & Gamble', 'Consumer Staples'], ['KO', 'Coca-Cola', 'Consumer Staples'],
    ['COST', 'Costco', 'Consumer Staples'], ['WMT', 'Walmart', 'Consumer Staples'],
    ['NEE', 'NextEra Energy', 'Utilities'], ['DUK', 'Duke Energy', 'Utilities'],
    ['LIN', 'Linde plc', 'Materials'], ['FCX', 'Freeport-McMoRan', 'Materials'],
    ['PLD', 'Prologis', 'Real Estate'], ['AMT', 'American Tower', 'Real Estate'],
    ['ORCL', 'Oracle Corp.', 'Technology'], ['CRM', 'Salesforce', 'Technology'],
    ['ADBE', 'Adobe Inc.', 'Technology'], ['NFLX', 'Netflix', 'Communication Services'],
    ['SPY', 'SPDR S&P 500 ETF', 'ETF'], ['QQQ', 'Invesco QQQ ETF', 'ETF'],
    ['XLE', 'Energy Select SPDR', 'ETF'], ['IWM', 'iShares Russell 2000', 'ETF'],
  ];
  const ETFS = new Set(['SPY', 'QQQ', 'XLE', 'IWM', 'XLF', 'GLD']);

  function mkStock([ticker, name, sector]) {
    const R = rng(hash(ticker));
    const isEtf = ETFS.has(ticker);
    const price = between(R, 24, 640, 2);
    const adv = iBetween(R, 4e5, 5e7);              // avg daily volume (shares)
    const dollar_volume = Math.round(adv * price);   // liquidity leg
    const atr = between(R, price * 0.008, price * 0.045, 2);
    const hasChain = chance(R, 0.8);                 // most names have an options chain
    const total_oi = hasChain ? iBetween(R, 200, 90000) : null;
    const total_volume = hasChain ? iBetween(R, 40, 60000) : null;
    const callShare = between(R, 0.35, 0.7, 2);
    const hasSide = hasChain && chance(R, 0.7);
    // smart-money legs — only a subset of names have informed-flow data
    const hasCongress = chance(R, 0.35), hasInsider = chance(R, 0.4), hasNews = chance(R, 0.6);

    let roicV = null;  // captured so roc_greenblatt derives from it without an extra R draw
    return {
      ticker, name, sector,
      price,
      mkt_cap: Math.round(between(R, 3e9, 3.2e12, 0)),
      pe: isEtf ? null : between(R, 8, 55, 1),
      roic: isEtf ? null : (roicV = between(R, 0.03, 0.42, 3)),
      // Greenblatt ROC (EBIT / (NWC + net PP&E)), the Magic Formula quality leg (#248)
      roc_greenblatt: roicV == null ? null : +(roicV * 1.4).toFixed(3),
      ebit_ev_yield: isEtf ? null : between(R, 0.02, 0.16, 3),
      score: between(R, -20, 60, 1),                 // valuation-model output
      upside: between(R, -25, 65, 1),
      rank: null,                                     // computed client-side
      // liquidity / microstructure
      adv, dollar_volume,
      spread: between(R, 0.01, 0.9, 2),
      depth: iBetween(R, 200, 90000),
      resiliency: between(R, 0.2, 0.98, 2),
      tco: between(R, 2, 65, 1),                      // total cost of ownership (bps)
      expense_ratio: isEtf ? between(R, 0.0003, 0.0075, 4) : null,
      etf_dq_flag: isEtf ? (chance(R, 0.2) ? 'stale_holdings' : null) : null,
      // risk / momentum
      beta: between(R, 0.55, 1.85, 2),
      atr, atr_pct: f(atr / price * 100, 2),
      realized_vol: between(R, 12, 62, 1),
      max_drawdown: -between(R, 6, 55, 1),
      mom_factor: between(R, -0.35, 0.55, 3),
      rs_percentile: iBetween(R, 3, 99),
      // implied vol
      iv_rank: iBetween(R, 4, 96),
      iv_percentile: iBetween(R, 4, 96),
      // options chain (raw)
      total_oi, total_volume,
      call_oi: hasSide ? Math.round(total_oi * callShare) : null,
      put_oi: hasSide ? Math.round(total_oi * (1 - callShare)) : null,
      call_volume: hasSide ? Math.round(total_volume * between(R, 0.3, 0.75, 2)) : null,
      put_volume: hasSide ? Math.round(total_volume * between(R, 0.25, 0.7, 2)) : null,
      skew_25d: hasChain ? between(R, 0.9, 1.35, 2) : null,
      put_call_oi: hasChain ? between(R, 0.35, 1.45, 2) : null,
      oi_max_strike: hasChain ? f(price * between(R, 0.9, 1.12, 2), 0) : null,
      oi_max_strike_oi: hasChain ? iBetween(R, 500, 40000) : null,
      // smart-money inputs
      congress_buys_90d: hasCongress ? iBetween(R, 0, 6) : null,
      congress_sells_90d: hasCongress ? iBetween(R, 0, 4) : null,
      insider_buys_90d: hasInsider ? iBetween(R, 0, 5) : null,
      insider_sells_90d: hasInsider ? iBetween(R, 0, 6) : null,
      news_sentiment_30d: hasNews ? between(R, -0.8, 0.85, 2) : null,
      committee_conflict: hasCongress && chance(R, 0.25),
      // Magic Formula input provenance (issue #270). Appended last so the draws
      // above (and the stable per-ticker look) are unchanged. ETFs carry none.
      ...(isEtf ? { magic_source: null, magic_period_end: null, magic_derived: '', roc_nwc_floored: false }
                : magicProvenance(R)),
    };
  }

  // Where this row's Magic Formula inputs came from — filing (xbrl), yfinance
  // fallback, or mixed — plus any derived-rule list and the NWC-floored flag.
  function magicProvenance(R) {
    const roll = R();
    const source = roll < 0.55 ? 'xbrl' : roll < 0.78 ? 'yfinance' : roll < 0.9 ? 'mixed' : null;
    const rules = [];
    if (source === 'xbrl' || source === 'mixed') {
      if (chance(R, 0.4)) rules.push('ebit=pretax+interest');
      if (chance(R, 0.3)) rules.push('short_term_debt=0');
    }
    return {
      magic_source: source,
      magic_period_end: (source === 'xbrl' || source === 'mixed') ? '2025-06-28' : null,
      magic_derived: rules.join(','),
      roc_nwc_floored: source != null && chance(R, 0.25),
    };
  }

  const STOCKS = UNIVERSE.map(mkStock);
  const byTicker = (t) => STOCKS.find((s) => s.ticker === t);

  function macro() {
    return { vix: 16.4, vix9d: 15.2, vix3m: 18.1, t10y3m_monthly: 0.42, asof: '2026-08-01' };
  }

  function integrity() {
    const now = Date.UTC(2026, 7, 1, 9, 30) / 1000;
    return {
      generated: { fundamentals: now, model: now, options: now - 3600, exposure: now - 7200 },
      coverage: { universe: 512, fundamentals: 498, model: 471, options_tickers: STOCKS.filter((s) => s.total_oi != null).length, exposure_tickers: 88 },
      model_completeness: { 'ROIC + EBIT/EV': '471 / 498', 'IV Rank': '120 / 498', 'Smart-money legs': '186 / 498' },
      quality_summary: { 'High coverage (>90%)': '11 fields', 'Partial (50-90%)': '4 fields', 'Low coverage (<50%)': '2 fields' },
      commodities: { roots: 9, exposures_mapped: 88 },
    };
  }

  function optionsSummary(t) {
    const r = byTicker(t); if (!r) return {};
    return {
      ticker: t, price: r.price, iv_rank: r.iv_rank, iv_percentile: r.iv_percentile,
      skew_25d: r.skew_25d, put_call_oi: r.put_call_oi, total_oi: r.total_oi, total_volume: r.total_volume,
      call_oi: r.call_oi, put_oi: r.put_oi, call_volume: r.call_volume, put_volume: r.put_volume,
      oi_max_strike: r.oi_max_strike, oi_max_strike_oi: r.oi_max_strike_oi,
      expirations: ['2026-08-21', '2026-09-18', '2026-12-18'],
    };
  }

  function toolsGather() {
    return [
      {
        id: 'edgar-scrubber',
        kind: 'gather',
        name: 'EDGAR Scrubber (424B2 structured notes)',
        // Exploratory, project-local: writes only to its local tool store, never
        // the pipeline (#109). graduatedTo null = still local. last_run is old on
        // purpose so the drift flag (local + unused) shows in demo mode.
        purpose: 'Extracts 424B2 structured-note terms into a LOCAL tool store while a Research project explores them; graduates upstream (and retires locally) once a field proves out.',
        status: 'working',
        source: 'SEC EDGAR',
        refresh_cadence: 'on-demand (human-supervised)',
        last_run: '2026-04-20T09:30:00Z',
        output_path: '~/.edgar-scrubber/store/extractions.sqlite (local)',
        graduatedTo: null,
        doc_link: 'https://github.com/Pat201492/Trader-Screener/blob/master/tools/edgar_scrubber/README.md'
      },
      {
        id: 'fred-ingest',
        kind: 'gather',
        name: 'FRED Data Fetcher',
        purpose: 'Collects macroeconomic data from Federal Reserve',
        status: 'working',
        source: 'FRED API',
        refresh_cadence: 'daily',
        last_run: '2026-08-04T08:00:00Z',
        doc_link: '#'
      }
    ];
  }

  function toolsModels() {
    return [
      {
        id: 'magic-formula',
        kind: 'model',
        name: 'Magic Formula Rank',
        purpose: 'Greenblatt composite: rank(ROC) + rank(EBIT/EV)',
        status: 'working',
        inputs: ['fundamentals', 'valuation'],
        output_field: 'magic_rank',
        last_run: '2026-08-04T09:30:00Z',
        doc_link: 'https://github.com/Pat201492/Trader-Screener/blob/master/Education/summaries/greenblatt-magic-formula.md'
      },
      {
        id: 'smart-money-score',
        kind: 'model',
        name: 'Smart Money Score',
        purpose: 'Congress + insider + news sentiment composite',
        status: 'building',
        inputs: ['congress_trades', 'insider_trades', 'news_sentiment'],
        output_field: 'smart_money_score',
        last_run: '2026-08-04T09:30:00Z',
        doc_link: '#'
      },
      {
        id: 'dealer-hedging-barrier-test',
        kind: 'model',
        name: 'Dealer-Hedging Barrier Causal Test',
        purpose: 'Propensity-matches elevated- vs low-issuance underlyings on size/ADV/options-OI and tests forward realized-vol/barrier-pinning behavior; calibrates the confirm/refute bar empirically against placebo null universes rather than an independence-assuming chance floor (#131).',
        status: 'working',
        inputs: ['edgar_scrubber.filing_date', 'edgar_scrubber.underlyings', 'prices', 'realized_vol', 'options_oi', 'adv'],
        output_field: 'dealer_hedging_verdict',
        last_run: '2026-08-07T00:00:00Z',
        doc_link: 'https://github.com/Pat201492/Trader-Screener/blob/master/Research/dealer_hedging_causal_test.py'
      }
    ];
  }

  // Research registry — hypothesis projects (issue #97). Mirrors
  // research-projects.json so demo mode ships one sample project end-to-end
  // (congress-buy-clusters, refuted with a full verdict) plus one deliberately
  // incomplete project (uoa-direction, missing falsifier + trialCount) to exercise
  // the warning-badge path.
  function researchProjects() {
    return [
      {
        id: 'congress-buy-clusters',
        title: 'Congress buy-clusters precede 90-day outperformance',
        hypothesis: 'A cluster of congressional purchases in a stock over the trailing 90 days predicts that stock beating its sector-median return over the next 90 days.',
        falsifier: 'If the top-decile congress-buy basket fails to beat the sector-median forward 90-day return by at least 2 percentage points out-of-sample — after the deflation correction for the declared trial count — the hypothesis is refuted.',
        tools: [
          { id: 'smart-money-score', label: 'Smart Money Score' },
          { id: 'edgar-scrubber', label: 'EDGAR Scrubber' }
        ],
        trialCount: 12,
        lookahead: {
          status: 'passed',
          note: 'STOCK Act filings lagged to their disclosure date (~45d), not the trade date. A-vs-B truncation test in lookahead-gate/ passed: no future filing date leaks into any forward 90-day window.'
        },
        status: 'refuted',
        evidence: 'Out-of-sample the top-decile congress-buy basket returned +0.4% vs the sector median over 90 days across all 12 declared trials. The raw in-sample signal (+3.1%) did not survive the deflation correction or the out-of-sample split.',
        verdict: 'Refuted. The edge is gone by the time a purchase is public — the ~45-day STOCK Act filing lag means the move is already priced. Written up in full precisely because it was killed: a refuted hypothesis with a clean falsifier is worth more than a supported one with a fuzzy bar.'
      },
      {
        id: 'uoa-direction',
        title: 'Unusual call-side options activity predicts a 5-day move',
        hypothesis: 'Unusual call-side options activity (UOA) precedes a positive 5-day price move in the underlying.',
        tools: [
          { id: 'smart-money-score', label: 'Smart Money Score' }
        ],
        lookahead: { status: 'na', note: 'Intraday options-flow window not yet wired into the lookahead-gate/ truncation harness — posture undeclared until it is.' },
        status: 'proposed',
        verdict: ''
      },
      {
        id: 'dealer-hedging-barrier-causal-test',
        title: 'Elevated issuance and realized-vol/pinning behavior near clustered barrier levels (Part B, #131)',
        hypothesis: 'Underlyings with elevated recent issuance of barrier-linked structured notes show different realized-vol/price behavior near their clustered barrier levels than matched low-issuance controls, after controlling for underlying size, liquidity (ADV), and options open interest.',
        falsifier: 'No statistically- (p<0.05) and economically- (>=0.2 vol-pts realized-vol or >=5pp pinning-rate) meaningful, consistently-signed difference between elevated-issuance and matched low-issuance observations, in excess of what the declared 27-trial grid produces on independently-seeded NULL placebo universes — refuted. Declared up front because the 27 trials are NOT independent (overlapping day-ranges/tickers across window/proximity/horizon cells), so a naive alpha×trialCount chance floor understates the true false-positive rate; the bar is calibrated empirically instead.',
        tools: [
          { id: 'edgar-scrubber', label: 'EDGAR Scrubber' },
          { id: 'dealer-hedging-barrier-test', label: 'Dealer-Hedging Barrier Causal Test' }
        ],
        trialCount: 27,
        lookahead: {
          status: 'passed',
          note: 'Every signal (issuance intensity, near-barrier flag, ADV/OI matching covariates) lags to the prior close. Knowledge date for an issuance event is EDGAR\'s acceptance timestamp (filing date), never the note\'s earlier pricing date — asserted directly (a plain A-vs-B truncation test can\'t catch a wrong-but-still-past date field, only an absolute future-index read). Truncation gate, same-day-peek check, and the knowledge-date check all pass in Research/dealer_hedging_causal_test.py.'
        },
        status: 'refuted',
        evidence: '27 declared trials (3 issuance windows x 3 barrier-proximity defs x 3 horizons) run on a neutral synthetic reference universe (no dealer-hedging mechanism planted) — this repo has no live pipeline connection, so the run is against a portable stdlib-only fixture standing in for scrubber-shaped issuance events plus pipeline-shaped prices/vol/OI/ADV, same posture as lookahead-gate/ and pipeline-mock/. 9 of 27 trials were underpowered (<20 matched pairs, the tightest-window/proximity cells) and excluded. Of the 18 scored trials, 7 nominally "confirmed" a vol/pinning difference at p<0.05 — but because the trials overlap in day-range and tickers across cells they are not independent, so the naive alpha x 27 ~= 1.35 chance floor is the wrong bar (it would have called this same null fixture "confirmed"). Calibrated empirically instead: 8 independently-seeded placebo universes with zero planted effect, run through the identical 27-trial grid, produced 4-14 confirming trials each — comfortably bracketing the canonical run\'s 7 (empirical p~=0.78). A separate power check (a universe with a deliberately planted dealer-hedging effect) confirms the matching + paired-test procedure does detect a real effect when one exists, so this null result means the fixture showed nothing, not that the test is blind.',
        verdict: 'Refuted. After propensity-style matching on size/ADV/options-OI, the difference in forward realized vol and barrier-pinning rate between elevated- and low-issuance observations is statistically indistinguishable from what the same correlated 27-trial grid produces on a placebo universe with no dealer-hedging mechanism at all. Written up including the two real bugs the build caught along the way, because they\'re the reason the verdict can be trusted: a percentile-rank tie-break that silently labeled zero-issuance tickers as "elevated" (fixed by ranking only among nonzero issuers), and a naive independent-trials chance floor that would have called this exact neutral fixture "confirmed" at 7/18 trials (fixed by empirical placebo calibration). Only synthetic-fixture evidence exists here — #131 has no live pipeline connection in this repo — so the natural next step is a live run against real EDGAR 424B2 filings and real options OI, not a re-run of this fixture.'
      },
      {
        id: 'structured-note-issuance',
        title: '424B2 structured-note issuance — descriptive map + dealer-hedging test',
        hypothesis: "Aggregate 424B2 structured-note issuance on an underlying creates measurable dealer hedging exposure — issuers hedge the book's embedded short-put/short-call profile in the underlying and its options, leaving a footprint concentrated near barrier/autocall levels and around observation dates. Prediction: underlyings with elevated recent issuance show different realized-vol and price behavior near clustered barrier levels than matched low-issuance controls.",
        falsifier: 'No difference between high-issuance and matched low-issuance underlyings’ realized-vol/price behavior near clustered barrier levels — after controlling for size, liquidity and options open interest, and after the Bonferroni-style correction for the declared 18 trials (|t| ≥ 2.7) — refutes it.',
        tools: [
          { id: 'edgar-scrubber', label: 'EDGAR Scrubber' }
        ],
        trialCount: 18,
        lookahead: {
          status: 'passed',
          note: '424B2 filings carry a pricing_date (in the document) and an EDGAR acceptance timestamp (file_date), and they differ. The knowledge date used everywhere is the acceptance timestamp, never pricing_date. Research/structured_note_issuance/test_causal_test.py adapts the lookahead-gate/ A-vs-B truncation test to this filing-event signal and passed — 0 mismatches for the shipped acceptance-date-keyed feature; a deliberately leaky pricing_date-keyed sibling was caught, proving the gate has teeth.'
        },
        status: 'inconclusive',
        evidence: 'Part A (issuance-by-underlying, barrier/autocall clustering vs spot, issuer markup league table) shipped separately as issue #130 — tools/edgar_scrubber/research_maps.py, gated against synthetic multi-issuer/basket-note data — regardless of Part B per the project’s acceptance criteria. Part B’s full pipeline (feature computation, matched-control selection, 18 declared trials, verdict logic, look-ahead gate) is implemented and gated against a synthetic universe to prove the mechanism, but has not been run against real filings: no live EDGAR crawl has populated the local scrubber store yet, and no real price/realized-vol/options-OI/ADV universe has been wired in.',
        verdict: 'Inconclusive — no data yet. Zero real trials have been scored because the local OutputStore is empty and no real underlying universe has been supplied. This is the honest current state, not a placeholder — Part A ships today regardless.'
      }
    ];
  }

  // Route a jget() path to a synthetic response. Unknown paths → {} (renders as
  // empty/– rather than throwing).
  window.__DEMO__ = function demoGet(path) {
    if (path === '/health') return { ok: true };
    if (path === '/api/macro') return macro();
    if (path.indexOf('/api/stocks?') === 0) return { stocks: STOCKS };
    if (path === '/api/integrity') return integrity();
    if (path === '/api/commodities') return { commodities: [] };
    let m;
    if ((m = path.match(/^\/api\/stocks\/([^/]+)\/commodities$/))) return { ticker: decodeURIComponent(m[1]), exposures: [] };
    if ((m = path.match(/^\/api\/stocks\/([^/]+)$/))) { const r = byTicker(decodeURIComponent(m[1])); return r ? Object.assign({}, r) : {}; }
    if ((m = path.match(/^\/api\/options\/([^/]+)\/([^/]+)$/))) return { ticker: decodeURIComponent(m[1]), expiration: m[2], contracts: [] };
    if ((m = path.match(/^\/api\/options\/([^/]+)$/))) return optionsSummary(decodeURIComponent(m[1]));
    if (path.match(/^\/api\/commodities\//)) return {};
    return {};
  };

  window.__DEMO_TOOLS_GATHER__ = toolsGather;
  window.__DEMO_TOOLS_MODELS__ = toolsModels;
  window.__DEMO_RESEARCH__ = researchProjects;
})();
