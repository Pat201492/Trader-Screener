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
        purpose: 'Real-data test (#341): 424B2 issuance per underlying from EDGAR search metadata, sampled barrier levels, pipeline prices. Matches elevated- vs low-issuance observations on size/ADV (and options OI once point-in-time history exists) and tests forward realized vol and barrier pinning across 27 declared trials, against a shifted-issuance placebo. Last run and coverage are read from Research/dealer_hedging_results.json.',
        status: 'working',
        inputs: ['edgar_scrubber.filing_date', 'edgar_scrubber.underlyings', 'prices', 'realized_vol', 'options_oi', 'adv'],
        output_field: 'dealer_hedging_verdict',
        last_run: null,
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
        evidence: 'Real-data run (2026-10-21), second pass (#354/#355): 51 underlyings -- 47 single stocks plus SPY/QQQ/IWM/DIA -- x 500 trading days in common (2024-10-21 to 2026-10-19; recently listed names cap the shared calendar). 212,914 424B2 filings counted from EDGAR search metadata over three years (an index counts only notes whose title leads with it), 1,576 sampled notes with barrier levels (key-terms extractor, no model); prices from Stock-Data-Pipeline. Matched on size + ADV (point-in-time options OI began 2026-10-20). Controls: zero or bottom-third issuance that day. All 27 of 27 trials scored; 19 show a difference but in no consistent direction (10 less pinning, 4 more pinning, 4 higher vol, 1 lower vol). 20 placebos with each underlying\'s own issuance shifted in time produced 8-23 confirming trials (median 18), so the canonical 19 is typical of no effect at all (empirical p=0.476). The first pass (1 year, stocks only, 2026-10-20) agreed: 16 of 18, p=0.333. Full table: Research/dealer_hedging_results.json.',
        verdict: 'Refuted (empirical p=0.48 against the declared 0.3 bar; every trial scored). Near their barriers, underlyings with elevated recent structured-note issuance do not behave differently from matched low-issuance underlyings in any way that survives the timing placebo: the differences point both ways and are as common when each underlying\'s issuance is moved to the wrong dates. Whatever separates heavily-noted names comes from which names they are, not from hedging flow tied to when notes are issued. Remaining limit: matching on size + ADV only -- re-run from the Dealer Hedging tab once a few months of point-in-time options OI have accumulated.'
      }
    ];
  }

  // Insider trades (#314): the /api/insider-trades and /rollups contract
  // (Stock-Data-Pipeline #57/#58, pipeline-mock/insider.py), over synthetic rows
  // dated relative to today, with a 3-insider cluster and one hidden bad row.
  const INSIDERS = (function () {
    const people = [['Jane Rivera', 'Chief Executive Officer'], ['Tom Okafor', 'Director'], ['Ana Petrov', 'CFO'],
      ['Lee Wong', 'Director'], ['Sam Patel', '10% Owner'], ['Kim Duarte', 'EVP, General Counsel']];
    const tks = [['AAPL', 'Apple Inc.'], ['NVDA', 'NVIDIA Corporation'], ['MSFT', 'Microsoft Corp'],
      ['AMZN', 'Amazon.com, Inc.'], ['TSLA', 'Tesla, Inc.']];
    const day = (n) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
    const rows = [];
    for (let i = 0; i < 40; i++) {
      const [t, co] = tks[i % tks.length], [nm, ti] = people[(i * 7) % people.length];
      const code = ['S', 'S', 'P', 'S', 'A', 'P', 'M'][i % 7], sh = 500 + ((i * 1237) % 50000), px = 20 + ((i * 53) % 800);
      const acc = `0000${100000 + i}-26-${String(i).padStart(6, '0')}`;
      rows.push({ filing_id: acc, ticker: t, company_name: co, insider_name: nm, insider_title: ti,
        transaction_date: day(i % 40), transaction_type: code, shares: sh, price_per_share: px,
        total_value: sh * px, sec_url: `https://www.sec.gov/Archives/edgar/data/1/${acc.replace(/-/g, '')}/${acc}-index.html`, suspect: false });
    }
    people.slice(0, 3).forEach(([nm, ti], j) => rows.push({ filing_id: `0000200000-26-00000${j}`, ticker: 'CLST',
      company_name: 'Cluster Holdings Inc.', insider_name: nm, insider_title: ti, transaction_date: day(4 + j * 3),
      transaction_type: 'P', shares: 10000, price_per_share: 12.5, total_value: 125000, sec_url: '', suspect: false }));
    rows.push({ filing_id: '0000300000-26-000001', ticker: 'SLBT', company_name: 'Suspect Data Co.',
      insider_name: 'Ching-Dong Wang', insider_title: 'Chief Executive Officer', transaction_date: day(2),
      transaction_type: 'P', shares: 4545306, price_per_share: 2272653, total_value: 10329903316818, sec_url: '', suspect: true });
    return rows;
  })();
  function insiderQuery(path) {
    const q = new URLSearchParams(path.split('?')[1] || '');
    const to = q.get('to') || new Date().toISOString().slice(0, 10);
    const from = q.get('from') || new Date(Date.parse(to) - 30 * 864e5).toISOString().slice(0, 10);
    const types = (q.get('type') || '').split(',').filter(Boolean), tk = (q.get('ticker') || '').toUpperCase();
    const role = q.get('role'), min = Number(q.get('min_value') || 0), inc = q.get('include_suspect') === '1';
    const roleWords = { officer: ['officer', 'chief', 'cfo', 'evp', 'svp', 'president'], director: ['director'], ten_pct: ['10%'] };
    return INSIDERS.filter(r => r.transaction_date >= from && r.transaction_date <= to && (inc || !r.suspect)
      && (!types.length || types.includes(r.transaction_type)) && (!tk || r.ticker === tk)
      && (!role || (roleWords[role] || [role]).some(w => r.insider_title.toLowerCase().includes(w)))
      && (r.total_value || 0) >= min);
  }
  function insiderTrades(path) {
    const q = new URLSearchParams(path.split('?')[1] || '');
    const rows = insiderQuery(path).slice().sort(q.get('sort') === 'value'
      ? (a, b) => b.total_value - a.total_value : (a, b) => b.transaction_date.localeCompare(a.transaction_date));
    const off = Number(q.get('offset') || 0), lim = Math.min(Number(q.get('limit') || 200), 2000);
    const page = rows.slice(off, off + lim);
    return { count: page.length, total: rows.length, rows: page };
  }
  function insiderRollups(path) {
    const rows = insiderQuery(path.replace(/([?&])(type|ticker|min_value)=[^&]*/g, '$1'));
    const buys = rows.filter(r => r.transaction_type === 'P');
    const sold = {};
    rows.filter(r => r.transaction_type === 'S').forEach(r => { const s = sold[r.ticker] ||= { ticker: r.ticker, company_name: r.company_name, sell_value: 0, sell_count: 0, insiders: new Set() };
      s.sell_value += r.total_value; s.sell_count++; s.insiders.add(r.insider_name); });
    const byT = {}; buys.forEach(r => (byT[r.ticker] ||= []).push(r));
    // 3+ DISTINCT insiders within any 14-day window (same rule as the pipeline).
    const clusters = [];
    Object.entries(byT).forEach(([t, rs]) => {
      rs = rs.slice().sort((a, b) => a.transaction_date.localeCompare(b.transaction_date));
      let best = null;
      rs.forEach((a, i) => {
        const end = new Date(Date.parse(a.transaction_date) + 14 * 864e5).toISOString().slice(0, 10);
        const win = rs.slice(i).filter(b => b.transaction_date <= end);
        const n = new Set(win.map(b => b.insider_name)).size;
        if (n >= 3 && (!best || n > best.n)) best = { n, win };
      });
      if (best) clusters.push({ ticker: t, company_name: best.win[0].company_name, insiders: best.n,
        buy_value: best.win.reduce((s, r) => s + r.total_value, 0), first_date: best.win[0].transaction_date,
        last_date: best.win[best.win.length - 1].transaction_date });
    });
    return { top_buys: buys.slice().sort((a, b) => b.total_value - a.total_value).slice(0, 25),
      most_sold: Object.values(sold).map(s => ({ ...s, insiders: s.insiders.size })).sort((a, b) => b.sell_value - a.sell_value).slice(0, 25),
      clusters };
  }

  // Route a jget() path to a synthetic response. Unknown paths → {} (renders as
  // empty/– rather than throwing).
  window.__DEMO__ = function demoGet(path) {
    if (path === '/health') return { ok: true };
    if (path === '/api/macro') return macro();
    if (path.indexOf('/api/stocks?') === 0) return { stocks: STOCKS };
    if (path === '/api/integrity') return integrity();
    if (path === '/api/commodities') return { commodities: [] };
    if (path.indexOf('/api/insider-trades/rollups') === 0) return insiderRollups(path);
    if (path.indexOf('/api/insider-trades') === 0) return insiderTrades(path);
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
