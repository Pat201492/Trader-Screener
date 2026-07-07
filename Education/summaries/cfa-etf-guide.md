# A Comprehensive Guide to Exchange-Traded Funds (CFA Institute Research Foundation)
**Source:** Hill, Kashner & Nadig, *A Comprehensive Guide to ETFs (2nd ed.), Module 1: ETF Features and Evolving Landscape*, CFA Institute Research Foundation, 2025 — https://rpc.cfainstitute.org/sites/default/files/docs/research-reports/hill_rf_brief_2025_etfs-evolving_module-1_2ed_online.v2.pdf

## What it is
Module 1 of the CFA Institute Research Foundation's 2025 ETF guide — the authoritative, product-agnostic treatment of how ETFs are structured and how they trade. It covers the ETF as a hybrid vehicle ($11T+ globally by late 2024), the creation/redemption mechanism, US legal structures, fees, and the ETF ecosystem. The load-bearing insight for a screener: an ETF's real tradeability lives in its underlying basket, not its on-screen volume, because of primary-market plumbing that stocks don't have.

## Core concepts
- **Hybrid vehicle.** Trades like a stock on the secondary market (exchange) but is a pooled '40 Act fund like a mutual fund. Holdings are disclosed daily — the most transparent pooled structure.
- **Primary vs secondary market.** Investors trade existing shares among themselves; the issuer isn't involved. Only **Authorized Participants (APs)** — large broker/dealers, usually market makers — create or redeem shares with the issuer.
- **Creation/redemption.** The issuer publishes a daily **creation basket** of underlying securities. The AP delivers the basket in kind and receives a **creation unit** (traditionally 50,000 ETF shares); redemption reverses this. Bond/illiquid ETFs may use cash-in-lieu.
- **AP arbitrage keeps price ≈ NAV.** If the ETF trades rich vs the basket's fair value, APs sell ETF / buy basket / create; if cheap, buy ETF / redeem / sell basket. In Exhibit 9, an AP buys the $40.00 basket and sells ETF shares at a $40.20 bid, pocketing ~18 cents after ~2-cent costs.
- **Arbitrage band ("gap").** The premium/discount needed before APs step in; its width scales with the **liquidity of the underlying securities** and creation fees — as tight as 1 cent for liquid holdings, wide for illiquid ones.

## Key metrics/signals it defines + how to read/compute them
- **Screen ADV / dollar volume** — real, but a *floor* not a ceiling on tradeability. An AP "can sell 50,000 ETF shares without needing to have them in inventory" by sourcing the basket, so a thin-volume ETF on liquid holdings is still highly tradeable.
- **Underlying/implied liquidity** — aggregate each constituent's own dollar ADV × basket weight; this is the ETF's true capacity.
- **NAV premium/discount** = (market price − NAV) / NAV; intraday proxy is iNAV/IIV/IOPV (intraday indicative value). Large or persistent values flag stale/closed underlying markets or stress.
- **Tracking difference/error** — the gap between ETF and index return; daily holdings let you verify the ETF "tracks its benchmark."
- **Total cost of ownership** = expense ratio + bid/ask spread + premium/discount paid + creation/redemption costs. Avg US ETF fee ~17 bps (asset-weighted, mid-2024); creation fees run $50 for mega-caps/Treasuries up to 2% for cash-in-lieu.
- **Market turnover / holding period** = assets ÷ average daily dollar volume — separates trading vehicles (SPY) from buy-and-hold (IVV/VOO).

## How it maps to the Trader Screener
- **§2c "Dollar volume (ADV × price)"** (the tradeability filter): for ETF rows, do NOT filter on the ETF's own screen dollar volume. Compute liquidity from the underlying holdings — sum each constituent's dollar ADV weighted by basket weight. A naive ADV floor wrongly rejects liquid ETFs (e.g., a young S&P 500 ETF with low volume but a mega-cap basket). Screen ADV is a lower bound only.
- **§2c "Average daily volume (ADV)" + "Bid/ask spread"**: keep ADV as display for ETFs, never as a hard exclusion. The achievable spread is set by the arbitrage band, which narrows with underlying liquidity — so spread, not ADV, is the correct ETF liquidity gate. Universe + holdings are already collected, so basket liquidity is free to compute.
- **§2d "Data-quality score"**: add an ETF-specific flag = persistent/large NAV premium/discount + wide tracking difference vs index. These signal stale marks, closed foreign markets, or structural drift — demote such ETFs.
- **§2d valuation (P/E, "Upside % (DCF/comps)")**: meaningless for funds. For ETF rows, replace them with total cost of ownership (expense ratio + spread + premium/discount + creation cost) as the fund analog of "valuation."

## Actionable takeaways
- Branch liquidity logic by instrument type: stocks → screen ADV; ETFs → basket/underlying liquidity + spread.
- Never hard-reject an ETF on low screen volume; low ADV on liquid holdings is fine.
- Ingest NAV/iNAV and index returns to compute premium/discount and tracking difference; feed both into §2d data-quality.
- For ETF rows, blank out P/E, ROIC, and upside; surface expense ratio + TCO instead.
- Underlying-liquidity is a free (🟡/✅) computation since holdings already exist.

## Open questions
- Data source for iNAV/NAV and per-constituent ADV to compute premium/discount, tracking difference, and basket liquidity on the free nightly pipeline?
- Thresholds for "large/persistent" premium/discount and tracking difference before demotion?
- How to handle actively managed / non-transparent ETFs whose exact daily basket isn't published?
- Should market turnover (assets ÷ dollar volume) be a display column to separate trading vs buy-and-hold ETFs?

## Sources
- Hill, J. M., E. Kashner, and D. Nadig. 2025. *A Comprehensive Guide to ETFs (2nd ed.), Module 1: ETF Features and Evolving Landscape.* CFA Institute Research Foundation. https://rpc.cfainstitute.org/sites/default/files/docs/research-reports/hill_rf_brief_2025_etfs-evolving_module-1_2ed_online.v2.pdf (creation/redemption pp. 15–19; ETF arbitrage & Exhibit 9 pp. 17–18; liquidity & price discovery pp. 8–9; costs/spreads/premiums pp. 11, 19; fees & TCO components p. 13; turnover/holding period pp. 28–29).