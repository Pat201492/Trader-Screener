# The Yield Curve as a Leading Indicator (NY Fed)
**Source:** *The Yield Curve as a Leading Indicator* — Federal Reserve Bank of New York (Estrella–Mishkin probit model) — https://www.newyorkfed.org/research/capital_markets/ycfaq

## What it is
A regulator-grade macro model that compresses the entire term structure into a single number: the probability of a U.S. recession 12 months ahead. It regresses NBER recession dates on the **10-year minus 3-month Treasury term spread** using a probit specification developed by Arturo Estrella and Frederic Mishkin. The NY Fed publishes the resulting recession-probability series monthly. Its durability is the selling point — every U.S. recession since 1969 was preceded by a yield-curve inversion (10Y below 3M), and the model has stayed continuously maintained for decades. It is the authoritative anchor for a rates/yield-curve macro regime.

## Core concepts
- **Term spread as forward signal.** A steep positive spread reflects expectations of growth and future rate hikes; a flat or inverted spread implies markets expect the Fed to cut into a slowdown. The curve inverts *before* the recession, giving lead time.
- **Inversion threshold.** The signal is the sign of the spread: **spread < 0** (3-month yield above 10-year) is inversion. It is a leading, not coincident, indicator — typically 12+ months of lead.
- **Probit, not linear.** A binary outcome (recession/no-recession) is mapped through the standard normal CDF Φ, so probabilities stay in [0, 1].
- **Monthly averages.** The NY Fed feeds monthly-average constant-maturity rates, not daily ticks, dampening noise.
- **Known false positive.** The spread narrowly inverted in 1966–67 without a recession following — the model's one notable false alarm — so treat it as probabilistic, not deterministic.

## Key metrics/signals it defines + how to read/compute them
**Formula (verified across secondary sources; see caveat):**
`P(recession in 12m) = Φ(−0.5333 − 0.6629 × spread)`, where `spread` is the 10Y−3M term spread in **percentage points** and Φ is the standard normal CDF. Coefficients α = −0.5333, β = −0.6629 come from Estrella–Mishkin (1998), estimated on monthly U.S. data ~1959–1995.

Reference mapping (computed from the formula):

| Spread | P(recession) |
|---|---|
| +2.0 | ~3% |
| +1.0 | ~12% |
| 0.0 | ~30% |
| −0.5 | ~42% |
| −0.82 | ~50% (threshold) |
| −1.0 | ~55% |
| −2.0 | ~79% |

Read it as: a flat curve already implies ~30% odds; roughly an **−80 bp inversion** crosses the historically dangerous 50% line.

**Compute pipeline:** pull FRED series **T10Y3M** (10Y CMT minus 3M CMT, daily, percent, already the spread), resample to a monthly average, apply the probit. Derive a **risk-on/risk-off regime flag**, e.g. risk-off when P ≥ ~30% or spread < 0; risk-on otherwise.

## How it maps to the Trader Screener
1. **§2f Macro context — "Rates / yield curve"** becomes the headline **recession-probability regime column** on the macro tab: display the monthly P(recession) value plus a risk-on/risk-off badge derived from the spread sign and the 30–50% probability bands.
2. **§2f "Other FRED series"** — list **T10Y3M** explicitly as an ingested series; the existing `fred.py` collector already pulls FRED, so this is a one-line series add, then the probit is computed downstream (zero new data cost, ✅ free tier).
3. **Regime flag gating default filters (§4 Default filters).** When the flag flips risk-off, tighten the default screen — e.g. bias toward defensive **§2d Sector**, quality (ROIC, composite score), and away from high-**§2b Beta**/high-momentum (**§2a**) names; risk-on relaxes it.
4. **Shared macro tab with §2f VIX.** Pair the slow, structural recession-probability gauge with the fast **VIX** tail-risk gauge so the macro header carries one leading (curve) and one coincident (vol) regime signal.

## Actionable takeaways
- Ingest T10Y3M via `fred.py`; store both the raw monthly spread and the computed P(recession).
- Surface P(recession) as the macro-tab headline number with a color-coded regime badge (green <30%, amber 30–50%, red ≥50%).
- Wire the regime flag into default-filter defaults, not as a hard block — let users override.
- Treat inversion as a 12-month lead warning, not a sell trigger; recessions historically arrive after the curve re-steepens.

## Open questions
- **Coefficient currency:** I verified α = −0.5333 / β = −0.6629 (Estrella–Mishkin 1998) via multiple secondary sources but could **not** load the live NY Fed page or PDF (HTTP 403) to confirm the *currently published* coefficients; the NY Fed may re-estimate on a longer sample (through ~2009+). Flag for re-verification before shipping the exact constants.
- Monthly value only, or also a daily approximation from live T10Y3M for a faster flag?
- Regime thresholds (30% vs 50%) — expose as user-tunable, or hard-code the historical 50% line?
- Should the false-positive caveat (1966–67) be shown in-UI to avoid over-trusting a single macro number?

## Sources
- Federal Reserve Bank of New York — *The Yield Curve as a Leading Indicator* (FAQ + monthly data): https://www.newyorkfed.org/research/capital_markets/ycfaq
- Estrella & Mishkin — *The Yield Curve as a Predictor of U.S. Recessions* (NY Fed, Current Issues): https://www.newyorkfed.org/medialibrary/media/research/current_issues/ci2-7.pdf
- FRED — T10Y3M (10Y CMT minus 3M CMT): https://fred.stlouisfed.org/series/T10Y3M
- Federal Reserve (FEDS Notes) — *Predicting Recession Probabilities Using the Slope of the Yield Curve*: https://www.federalreserve.gov/econres/notes/feds-notes/predicting-recession-probabilities-using-the-slope-of-the-yield-curve-20180301.html
- St. Louis Fed — *What Is the Probability of a Recession? The Message from Yield Spreads*: https://www.stlouisfed.org/on-the-economy/2023/sep/what-probability-recession-message-yield-spreads