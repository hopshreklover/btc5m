import sys
import numpy as np
import rlab


def rest(d, rule, one_side, lo=0.10, hi=0.20, t0=8, t1=240):
    won = {"up": d["up_won"], "dn": 1 - d["up_won"]}
    cand = []
    for s in ("up", "dn"):
        q = d["ask_" + s]
        m = (~np.isnan(q)) & (q >= lo) & (q < hi) & (d["t"] >= t0) & (d["t"] < t1)
        if rule == "strict":
            m &= d["max_" + s] > q + 1e-9
        idx = np.nonzero(m)[0]
        if not len(idx):
            continue
        _, first = np.unique(d["start"][idx], return_index=True)
        i = idx[first]
        cand.append(np.c_[d["start"][i], d["t"][i], q[i], won[s][i]])
    c = np.concatenate(cand)
    if one_side:                       # keep only the earliest fill in each market
        order = np.lexsort((c[:, 1], c[:, 0]))
        c = c[order]
        keep = np.r_[True, c[1:, 0] != c[:-1, 0]]
        c = c[keep]
    q, won_s = c[:, 2], c[:, 3]
    r = (q - won_s) / (1 - q)                       # per $ on the favourite bought at 1-q
    # per market P&L for a $10 stake per fill
    u, inv = np.unique(c[:, 0], return_inverse=True)
    per_mkt = np.bincount(inv, r * 10)
    return r, per_mkt


for prefix, label in [(a.split(":")[0], a.split(":")[1]) for a in sys.argv[1:]]:
    for st in ("1.3", "2.3", "3.3"):
        d = rlab.load(f"{prefix}_spot_s{st}.npy")
        hours = len(np.unique(d["start"])) * 5 / 60
        line = f"{label:9s} stamp {st}: "
        for one in (False, True):
            for rule in ("strict", "generous"):
                r, pm = rest(d, rule, one)
                se = pm.std() * np.sqrt(len(pm))
                line += (f"{'1 side' if one else 'both '} {rule[:3]} {r.mean() * 100:+5.2f}% "
                         f"${pm.sum():+6.0f}+-{se:3.0f} {len(r) / hours:4.1f}/h | ")
        print(line)
