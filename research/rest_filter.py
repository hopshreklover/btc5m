"""Resting bids on the favourite, optionally filtered by a forecast of the favourite's value.

Forecast = logistic blend of the market's own price (previous second's trades)
and the model (direct exchange feed), with weights fitted on the tuning weeks.
"""
import sys

import numpy as np

import calib
import rlab


def lagged_market(d):
    up_terms = np.where(np.isnan(d["vwap_up"]), 1 - d["vwap_dn"],
                        np.where(np.isnan(d["vwap_dn"]), d["vwap_up"],
                                 (d["vwap_up"] + 1 - d["vwap_dn"]) / 2))
    prev = np.r_[np.nan, up_terms[:-1]]
    same = np.r_[False, d["start"][1:] == d["start"][:-1]]
    prev[~same] = np.nan
    # carry the last known price forward within a market
    out = prev.copy()
    for i in range(1, len(out)):
        if np.isnan(out[i]) and same[i]:
            out[i] = out[i - 1]
    return out


def blended(d, w):
    m = lagged_market(d)
    s = np.c_[d["start"], d["t"], m, d["p"], d["up_won"]]
    X = calib.feats(s, "both")
    p = 1 / (1 + np.exp(-X @ w))
    p[np.isnan(m)] = np.nan
    return p


def rest(d, p_up, margin, rule, lo=0.10, hi=0.20, t0=8, t1=240):
    won = {"up": d["up_won"], "dn": 1 - d["up_won"]}
    rets = []
    for s in ("up", "dn"):              # s = the long-shot side whose buyers we sell to
        q = d["ask_" + s]
        fav_fair = (1 - p_up) if s == "up" else p_up
        m = (~np.isnan(q)) & (q >= lo) & (q < hi) & (d["t"] >= t0) & (d["t"] < t1)
        if margin is not None:
            m &= ~np.isnan(fav_fair) & (fav_fair - (1 - q) >= margin)
        if rule == "strict":
            m &= d["max_" + s] > q + 1e-9
        idx = np.nonzero(m)[0]
        if not len(idx):
            continue
        _, first = np.unique(d["start"][idx], return_index=True)
        i = idx[first]
        rets.append((q[i] - won[s][i]) / (1 - q[i]))
    r = np.concatenate(rets) if rets else np.zeros(0)
    return r


tr_s = calib.sample(rlab.load("tr_spot_s2.3.npy"))
W = calib.fit(calib.feats(tr_s, "both"), tr_s[:, 4])
print("blend weights (fitted on tuning weeks):", np.round(W, 3))
sets = [("tuning", "tr")] + [(a, a) for a in sys.argv[1:]]
for label, prefix in sets:
    d = rlab.load(f"{prefix}_spot_s2.3.npy")
    hours = len(np.unique(d["start"])) * 5 / 60
    pb = blended(d, W)
    pm = d["p"]
    print(f"===== {label}: {hours:.0f} h")
    for name, p in (("no filter", None), ("blend", pb), ("model only", pm)):
        margins = [None] if p is None else [-0.04, -0.02, 0.0, 0.01, 0.02, 0.04]
        for mg in margins:
            line = f"  {name:10s} margin {'' if mg is None else f'{mg:+.2f}':>5}: "
            for rule in ("strict", "generous"):
                r = rest(d, p if p is not None else np.zeros(d["n"]), mg, rule)
                se = r.std() / np.sqrt(len(r)) * 100 if len(r) > 1 else 0
                line += (f"{rule} {r.mean() * 100:+5.2f}% +-{se:4.2f} ({len(r) / hours:4.1f}/h, "
                         f"$ {r.sum() * 10:+7.1f})   ")
            print(line)
