"""Does the model add information beyond the market's own price?

Per market, take one row at each checkpoint (30, 60, ... 270 s). Market price =
Up-price of the PREVIOUS second's trades (so it is known at decision time).
Fit logistic regressions on the tuning weeks, score log loss on last week.
"""
import sys

import numpy as np

import rlab

CHECK = np.arange(30, 300, 30)


def logit(p):
    p = np.clip(p, 0.005, 0.995)
    return np.log(p / (1 - p))


def sample(d):
    up_terms = np.where(np.isnan(d["vwap_up"]), 1 - d["vwap_dn"],
                        np.where(np.isnan(d["vwap_dn"]), d["vwap_up"],
                                 (d["vwap_up"] + 1 - d["vwap_dn"]) / 2))
    rows = []
    starts = d["start"]
    bounds = np.r_[0, np.nonzero(np.diff(starts))[0] + 1, len(starts)]
    for a, b in zip(bounds[:-1], bounds[1:]):
        t = d["t"][a:b]
        for c in CHECK:
            j = np.searchsorted(t, c)
            if j <= 0 or j >= len(t) or t[j] - c > 5 or np.isnan(up_terms[a + j - 1]):
                continue
            i = a + j
            rows.append((starts[i], d["t"][i], up_terms[i - 1], d["p"][i], d["up_won"][i]))
    return np.array(rows)


def fit(X, y, iters=50, l2=1e-3):
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + l2 * w
        H = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.eye(len(w))
        w -= np.linalg.solve(H, g)
    return w


def feats(s, kind):
    tl = (300 - s[:, 1]) / 300          # fraction of the market left
    lm, lp = logit(s[:, 2]), logit(s[:, 3])
    one = np.ones(len(s))
    if kind == "market":
        return np.c_[one, lm, lm * tl]
    if kind == "model":
        return np.c_[one, lp, lp * tl]
    return np.c_[one, lm, lm * tl, lp, lp * tl]


def loss(X, y, w, starts):
    p = np.clip(1 / (1 + np.exp(-X @ w)), 1e-6, 1 - 1e-6)
    ll = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    u, inv = np.unique(starts, return_inverse=True)
    per = np.bincount(inv, ll) / np.bincount(inv)
    return ll.mean(), per


for feed in sys.argv[1:]:
    tr = sample(rlab.load(f"tr_{feed}_s2.3.npy"))
    te = sample(rlab.load(f"te_{feed}_s2.3.npy"))
    print(f"===== feed {feed}: {len(tr)} tuning rows, {len(te)} test rows")
    res = {}
    for kind in ("market", "model", "both"):
        w = fit(feats(tr, kind), tr[:, 4])
        l_tr, _ = loss(feats(tr, kind), tr[:, 4], w, tr[:, 0])
        l_te, per = loss(feats(te, kind), te[:, 4], w, te[:, 0])
        res[kind] = per
        print(f"  {kind:7s} weights {np.round(w, 3)}  log loss tuning {l_tr:.4f}  last week {l_te:.4f}")
    d = res["market"] - res["both"]
    print(f"  'both' beats 'market' on last week by {d.mean():.5f} per row "
          f"(+- {d.std() / np.sqrt(len(d)):.5f}, by market; t {d.mean() / (d.std() / np.sqrt(len(d))):.2f})")
    for c in (60, 150, 240, 270):
        m = np.abs(te[:, 1] - c) < 15
        for kind in ("market", "both"):
            w = fit(feats(tr, kind), tr[:, 4])
            l, _ = loss(feats(te[m], kind), te[m, 4], w, te[m, 0])
            print(f"    at ~{c}s  {kind:7s} log loss {l:.4f}", end="")
        print()
