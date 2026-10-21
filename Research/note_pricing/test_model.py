"""Per-issuer pricing model (#367): recovers planted functions, flags the rest.

Run:  python -m pytest Research/note_pricing/test_model.py
"""
import os
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import model as m  # noqa: E402


def _issuer(f, n=300, noise=0.2, seed=1, start=date(2025, 1, 1), shift=None, cik="0000000001"):
    """Synthetic notes priced by a known function f(barrier, tenor, vol, rate, nund)."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        d = start + timedelta(days=int(i * 600 / n))
        b, t, v = rng.uniform(50, 90), rng.uniform(0.5, 5), rng.uniform(15, 80)
        rate, nu = rng.uniform(3.5, 5.0), int(rng.integers(1, 4))
        c = f(b, t, v, rate, nu)
        if shift and d >= shift[0]:
            c += shift[1]
        rows.append({"accession": f"{cik}-{i}", "issuer": "Bank", "issuer_cik": cik, "pricing_date": d.isoformat(),
                     "coupon": c + rng.normal(0, noise), "barrier": b, "tenor": t, "vol": v, "rate": rate,
                     "n_underlyings": nu, "estimated_value": 960.0})
    return rows


LINEAR = lambda b, t, v, r, n: 30 - 0.2 * b + 0.15 * v + 0.8 * r + 1.0 * n
QUADRATIC = lambda b, t, v, r, n: 25 - 0.25 * b + 0.002 * (b - 70) ** 2 + 0.1 * v + 0.001 * v * (90 - b) + 0.5 * n


def test_bayes_poly_recovers_a_planted_quadratic():
    rows = _issuer(QUADRATIC, n=400, noise=0.1)
    X, names = m._features(rows, True)
    y = np.array([r["coupon"] for r in rows])
    scores = {k: m.held_out(X, y, k) for k in m.KS}
    assert scores[2][0] < scores[1][0]                  # order 2 beats order 1 held out
    fit = m.BayesPoly(2).fit(X, y)
    x0 = np.array([[70, 1, 30, 4.2, 1]])
    assert abs(fit.predict(x0)[0][0] - QUADRATIC(70, 1, 30, 4.2, 1)) < 0.3


def test_linear_issuer_is_consistent_and_picks_a_low_order():
    res = m.fit_issuer(_issuer(LINEAR, noise=0.3))
    assert res["verdict"] == "consistent" and res["r2"] > 0.9 and res["k"] in (1, 2)
    assert res["regime_shifts"] == [] and len(res["surface"]["coupon"]) == len(res["surface"]["barrier"])


def test_noise_issuer_is_not_consistent():
    res = m.fit_issuer(_issuer(lambda *a: 10.0, noise=4.0))
    assert res["verdict"] == "not_consistent"
    assert any("R^2" in r for r in res["reasons"]) and any("RMSE" in r for r in res["reasons"])


def test_a_changed_algorithm_shows_as_regime_shifts():
    # The bank re-prices everything +3 pp from 2025-07, then -3 pp again from 2026-01.
    rows = _issuer(LINEAR, n=600, noise=0.2, shift=(date(2025, 7, 1), 3.0))
    for r in rows:
        if r["pricing_date"] >= "2026-01-01":
            r["coupon"] -= 6.0
    res = m.fit_issuer(rows)
    assert len(res["regime_shifts"]) >= 2 and res["verdict"] == "not_consistent"


def test_too_few_notes_is_insufficient():
    assert m.fit_issuer(_issuer(LINEAR, n=20))["verdict"] == "insufficient"


def test_missing_rate_drops_the_input_not_the_rows():
    rows = _issuer(LINEAR)
    rows[0]["rate"] = None
    res = m.fit_issuer(rows)
    assert res["rate_used"] is False and "rate" not in res["inputs"] and res["n"] == len(rows)


def test_fit_all_groups_by_issuer():
    rows = _issuer(LINEAR, cik="A") + _issuer(QUADRATIC, cik="B", seed=2)
    out = m.fit_all(rows)
    assert set(out["issuers"]) == {"A", "B"} and out["rule"] == m.RULE
