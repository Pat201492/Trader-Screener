"""
Per-issuer pricing function (#367): does each bank price its notes with one
stable algorithm?

For every issuer, the coupon a note pays is modelled as a function of what a
pricing desk sees on the pricing date:

    coupon = f(barrier, tenor, volatility, rate, number of underlyings)

f is a polynomial of order k in the standardized inputs, all cross terms
included, fitted by Bayesian linear regression: a zero-mean Gaussian prior on
the coefficients (precision alpha) and Gaussian noise (precision beta), both
set by maximising the evidence (MacKay's fixed-point updates). k in {1, 2, 3}
is chosen by 5-fold HELD-OUT error -- never in-sample fit, which always
prefers the largest k.

DECLARED RULE (fixed before any real data was fitted, 2026-10-21): an issuer
is CONSISTENT with one algorithm when
  * held-out R^2 >= 0.5            (its terms are mostly a function of inputs),
  * held-out RMSE <= 2.0 pp        (the function predicts coupons usefully),
  * at most 1 regime shift         (the standard note's fitted coupon jumps
                                    between consecutive quarters by more than
                                    2x their combined posterior sd).
Otherwise NOT CONSISTENT, with every failed condition listed. An issuer with
fewer than MIN_NOTES usable notes is INSUFFICIENT.

The standard note -- 70% barrier, 1-year tenor, 30% vol, one underlying, the
window's median rate -- is priced by every issuer's function in every window,
so banks can be compared on the same product.

  python Research/note_pricing/model.py      # -> Research/note_pricing/results.json
"""
import itertools
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FEATURES = ("barrier", "tenor", "vol", "rate", "n_underlyings")
KS = (1, 2, 3)
FOLDS = 5
MIN_NOTES = 40
MIN_WINDOW = 15
RULE = {"min_r2": 0.5, "max_rmse_pp": 2.0, "max_regime_shifts": 1}
STANDARD_NOTE = {"barrier": 70.0, "tenor": 1.0, "vol": 30.0, "n_underlyings": 1}


# ── Data ───────────────────────────────────────────────────────────────────

def load_rows(conn):
    """Usable notes: a coupon, a barrier (or coupon barrier), a tenor and a
    volatility. A missing rate (FRED unavailable) drops the rate input for
    the whole fit rather than the row."""
    out = []
    for r in conn.execute(
            "SELECT accession, issuer, issuer_cik, pricing_date, coupon_rate, "
            "COALESCE(barrier_pct, coupon_barrier_pct), tenor_years, COALESCE(vol60_max, vol20_max), "
            "rate_1y, n_underlyings, estimated_value FROM notes"):
        acc, issuer, cik, pricing, coupon, barrier, tenor, vol, rate, n, ev = r
        if None in (coupon, barrier, tenor, vol) or tenor <= 0:
            continue
        out.append({"accession": acc, "issuer": issuer, "issuer_cik": cik, "pricing_date": pricing,
                    "coupon": float(coupon), "barrier": float(barrier), "tenor": float(tenor),
                    "vol": float(vol), "rate": None if rate is None else float(rate),
                    "n_underlyings": int(n or 1), "estimated_value": ev})
    return out


def _features(rows, use_rate):
    names = [f for f in FEATURES if use_rate or f != "rate"]
    X = np.array([[r[f] for f in names] for r in rows], dtype=float)
    return X, names


# ── Bayesian polynomial regression ─────────────────────────────────────────

def _poly(Z, k):
    """Design matrix: 1, then every monomial of the columns of Z up to total
    degree k (cross terms included)."""
    cols = [np.ones(len(Z))]
    d = Z.shape[1]
    for deg in range(1, k + 1):
        for combo in itertools.combinations_with_replacement(range(d), deg):
            cols.append(np.prod(Z[:, combo], axis=1))
    return np.column_stack(cols)


class BayesPoly:
    """Evidence-maximising Bayesian linear regression on polynomial features."""

    def __init__(self, k):
        self.k = k

    def fit(self, X, y, iters=100):
        self.mu, self.sd = X.mean(axis=0), X.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        self.ymu = y.mean()
        Phi = _poly((X - self.mu) / self.sd, self.k)
        t = y - self.ymu
        n, m = Phi.shape
        alpha, beta = 1.0, 1.0 / max(np.var(t), 1e-6)
        PtP, Ptt = Phi.T @ Phi, Phi.T @ t
        eig = np.linalg.eigvalsh(PtP)
        for _ in range(iters):
            A = alpha * np.eye(m) + beta * PtP
            mN = beta * np.linalg.solve(A, Ptt)
            lam = beta * eig
            gamma = float(np.sum(lam / (lam + alpha)))
            alpha_new = gamma / max(float(mN @ mN), 1e-12)
            resid = t - Phi @ mN
            beta_new = max(n - gamma, 1e-6) / max(float(resid @ resid), 1e-12)
            if abs(alpha_new - alpha) < 1e-6 * alpha and abs(beta_new - beta) < 1e-6 * beta:
                alpha, beta = alpha_new, beta_new
                break
            alpha, beta = alpha_new, beta_new
        self.alpha, self.beta = alpha, beta
        self.SN = np.linalg.inv(alpha * np.eye(m) + beta * PtP)
        self.mN = beta * self.SN @ Ptt
        return self

    def predict(self, X):
        """(mean, sd) -- sd includes the noise."""
        Phi = _poly((np.atleast_2d(X) - self.mu) / self.sd, self.k)
        mean = Phi @ self.mN + self.ymu
        var = 1.0 / self.beta + np.einsum("ij,jk,ik->i", Phi, self.SN, Phi)
        return mean, np.sqrt(var)


def held_out(X, y, k, folds=FOLDS, seed=0):
    """(RMSE, R^2) over `folds`-fold cross-validation."""
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(y))
    parts = np.array_split(idx, folds)
    pred = np.empty(len(y))
    for i in range(folds):
        test = parts[i]
        train = np.concatenate([parts[j] for j in range(folds) if j != i])
        pred[test] = BayesPoly(k).fit(X[train], y[train]).predict(X[test])[0]
    resid = y - pred
    rmse = float(np.sqrt(np.mean(resid ** 2)))
    ss = float(np.sum((y - y.mean()) ** 2)) or 1e-12
    return rmse, 1.0 - float(np.sum(resid ** 2)) / ss


# ── One issuer ─────────────────────────────────────────────────────────────

def _quarter(iso):
    y, m = int(iso[:4]), int(iso[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


def _standard_x(names, rate):
    std = dict(STANDARD_NOTE, rate=rate if rate is not None else 0.0)
    return np.array([[std[f] for f in names]], dtype=float)


def fit_issuer(rows):
    """The issuer's model, windows, standard-note series, residuals, verdict."""
    n = len(rows)
    out = {"n": n}
    if n < MIN_NOTES:
        out.update(verdict="insufficient", reasons=[f"{n} usable notes (< {MIN_NOTES})"])
        return out
    use_rate = all(r["rate"] is not None for r in rows)
    X, names = _features(rows, use_rate)
    y = np.array([r["coupon"] for r in rows])
    # Drop inputs that never vary for this issuer (e.g. single-underlying only).
    keep = [i for i in range(X.shape[1]) if np.std(X[:, i]) > 0]
    X, names = X[:, keep], [names[i] for i in keep]
    scores = {k: held_out(X, y, k) for k in KS if _poly(np.zeros((1, X.shape[1])), k).shape[1] < n * 0.8}
    k = min(scores, key=lambda kk: (round(scores[kk][0], 6), kk))
    rmse, r2 = scores[k]
    model = BayesPoly(k).fit(X, y)
    fitted, _ = model.predict(X)
    out.update(k=k, inputs=names, rate_used=use_rate, cv={str(kk): {"rmse": v[0], "r2": v[1]} for kk, v in scores.items()},
               rmse=rmse, r2=r2,
               residuals=[{"accession": r["accession"], "pricing_date": r["pricing_date"], "actual": r["coupon"],
                           "fitted": round(float(f), 4)} for r, f in zip(rows, fitted)])
    # Rolling quarterly windows: the same k refitted per quarter; the
    # standard note priced by each quarter's function.
    by_q = {}
    for r in rows:
        by_q.setdefault(_quarter(r["pricing_date"]), []).append(r)
    windows = []
    for q in sorted(by_q):
        wr = by_q[q]
        if len(wr) < MIN_WINDOW:
            windows.append({"quarter": q, "n": len(wr), "skipped": True})
            continue
        Xw = np.array([[r[f] for f in names] for r in wr], dtype=float)
        yw = np.array([r["coupon"] for r in wr])
        mw = BayesPoly(min(k, 2)).fit(Xw, yw)
        rates = sorted(r["rate"] for r in wr if r["rate"] is not None)
        rate = rates[len(rates) // 2] if rates else None
        mean, sd = mw.predict(_standard_x(names, rate))
        windows.append({"quarter": q, "n": len(wr), "standard_note": round(float(mean[0]), 4),
                        "sd": round(float(sd[0]), 4)})
    shifts = []
    live = [w for w in windows if not w.get("skipped")]
    for a, b in zip(live, live[1:]):
        if abs(b["standard_note"] - a["standard_note"]) > 2 * math.hypot(a["sd"], b["sd"]):
            shifts.append(f"{a['quarter']}->{b['quarter']}")
    out.update(windows=windows, regime_shifts=shifts)
    reasons = []
    if r2 < RULE["min_r2"]:
        reasons.append(f"held-out R^2 {r2:.2f} < {RULE['min_r2']}")
    if rmse > RULE["max_rmse_pp"]:
        reasons.append(f"held-out RMSE {rmse:.2f} pp > {RULE['max_rmse_pp']}")
    if len(shifts) > RULE["max_regime_shifts"]:
        reasons.append(f"{len(shifts)} regime shifts > {RULE['max_regime_shifts']}")
    out.update(verdict="not_consistent" if reasons else "consistent", reasons=reasons)
    # A coupon surface over barrier x vol for the 3D chart (tenor 1y, one
    # underlying, median rate).
    rates = sorted(r["rate"] for r in rows if r["rate"] is not None)
    rate = rates[len(rates) // 2] if rates else None
    bars, vols = list(range(50, 95, 5)), list(range(15, 85, 5))
    grid = []
    for b in bars:
        row = []
        for v in vols:
            x = dict(STANDARD_NOTE, barrier=float(b), vol=float(v), rate=rate if rate is not None else 0.0)
            row.append(round(float(model.predict(np.array([[x[f] for f in names]]))[0][0]), 4))
        grid.append(row)
    out["surface"] = {"barrier": bars, "vol": vols, "coupon": grid}
    return out


def fit_all(rows):
    by = {}
    for r in rows:
        by.setdefault(r["issuer_cik"], []).append(r)
    res = {}
    for cik, rs in sorted(by.items()):
        rs.sort(key=lambda r: r["pricing_date"])
        res[cik] = {"issuer": rs[0]["issuer"], **fit_issuer(rs)}
    return {"rule": RULE, "standard_note": STANDARD_NOTE, "features": FEATURES, "issuers": res}


def main():
    import data as npd
    from datetime import datetime, timezone
    conn = npd.connect()
    res = fit_all(load_rows(conn))
    res["ran_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res["coverage"] = npd.coverage(conn)
    out = HERE / "results.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    for cik, r in res["issuers"].items():
        print(f"{r['issuer'][:34]:34s} n={r['n']:4d} {r['verdict']:15s} "
              + (f"k={r['k']} R2={r['r2']:.2f} RMSE={r['rmse']:.2f} shifts={len(r['regime_shifts'])}" if "k" in r else "")
              + (f"  ({'; '.join(r['reasons'])})" if r.get("reasons") else ""))
    print("->", out)


if __name__ == "__main__":
    main()
