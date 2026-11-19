"""Does the resting-bid strategy's P&L depend on Bitcoin's volatility (and hour of day)?"""
import multiprocessing as mp
import sys
from dataclasses import replace

import numpy as np

sys.path.insert(0, ".")
from btc5m.config import Config
from btc5m.history import Prices, read_windows, run_replay

SETS = {"tuning": ("data/research/old", "data/research/prices.csv", 1792389900, 1794204300),
        "last": ("data/research/last", "data/research/prices.csv", 1794204300, 1794809100),
        "fresh": ("data/research/new", "data/research/new/prices.csv", 1794809100, 1795021500)}


def run(name, over=None):
    store, pfile, lo, hi = SETS[name]
    cfg = replace(Config(), strategy="rest", status_interval_s=0.0, kill_file="",
                  daily_loss_limit=1e9, max_loss_streak=10**6, min_bankroll=-1e9, **(over or {}))
    eng = run_replay(cfg, read_windows(store, lo, hi), Prices.load(pfile), lo - 3600, hi + 10,
                     feed_lag_s=1.4, shift_s=-1, stamp_delay_s=2.3)
    return name, [(r["start"], r["sigma"], r["buys"], r["spent"], r["pnl"]) for r in eng.results]


if __name__ == "__main__":
    with mp.Pool(3) as pool:
        out = dict(pool.starmap(run, [(n,) for n in SETS]))
    np.save("data/research/volstudy.npy", np.array([(n, *r) for n, rows in out.items() for r in rows],
                                                   dtype=object), allow_pickle=True)
    tr = np.array([r for r in out["tuning"] if r[2] > 0 and r[1] == r[1]], dtype=float)
    edges = np.quantile(tr[:, 1], [0.2, 0.4, 0.6, 0.8])
    print("volatility quintile edges (per sqrt-second, from tuning):", np.round(edges * 1e5, 2), "x1e-5")
    print("No daily loss limit here, so every market counts. cell: P&L per traded market, $ (n, SE)")
    for name, rows in out.items():
        a = np.array([r for r in rows if r[2] > 0 and r[1] == r[1]], dtype=float)
        q = np.searchsorted(edges, a[:, 1])
        line = f"  {name:7s} total {a[:, 4].sum():+8.1f} on {len(a)} mkts | "
        for k in range(5):
            v = a[q == k, 4]
            line += f"Q{k + 1} {v.mean() if len(v) else 0:+5.2f} ({len(v)}, {v.std() / np.sqrt(max(len(v), 1)):.2f})  "
        print(line)
    print("by hour of day (UTC), all periods: P&L per traded market")
    allr = np.array([r for rows in out.values() for r in rows if r[2] > 0], dtype=float)
    hrs = ((allr[:, 0] % 86400) // 3600).astype(int)
    for h0 in range(0, 24, 4):
        m = (hrs >= h0) & (hrs < h0 + 4)
        v = allr[m, 4]
        print(f"  {h0:02d}-{h0 + 4:02d}h  {v.mean():+5.2f} ({len(v)}, SE {v.std() / np.sqrt(len(v)):.2f})")
