"""Bid below the best bid? Usage: grid_offset.py SET [labels;...]"""
import itertools
import multiprocessing as mp
import sys
from dataclasses import replace

sys.path.insert(0, ".")
from btc5m.config import Config
from btc5m.history import Prices, read_windows, run_replay

SETS = {"tuning": ("data/research/old", "data/research/prices.csv", 1792389900, 1794204300),
        "last": ("data/research/last", "data/research/prices.csv", 1794204300, 1794809100),
        "fresh": ("data/research/new", "data/research/new/prices.csv", 1794809100, 1795021500)}
PR = {}


def label(p):
    off, band, fills = p
    return f"offset {off:.2f} band {band[0]:.2f}-{band[1]:.2f} x{fills}"


def one(args):
    name, (off, band, fills) = args
    store, pfile, lo, hi = SETS[name]
    if pfile not in PR:
        PR[pfile] = Prices.load(pfile)
    cfg = replace(Config(), strategy="rest", status_interval_s=0.0, kill_file="",
                  daily_loss_limit=1e9, max_loss_streak=10**6, min_bankroll=-1e9,
                  rest_offset=off, rest_fav_min=band[0], rest_fav_max=band[1],
                  rest_max_fills=fills, max_window_usd=max(30.0, 10.0 * fills))
    eng = run_replay(cfg, read_windows(store, lo, hi), PR[pfile], lo - 3600, hi + 10,
                     feed_lag_s=1.4, shift_s=-1, stamp_delay_s=2.3)
    pnl = [r["pnl"] for r in eng.results if r["buys"] > 0]
    staked = sum(r["spent"] for r in eng.results)
    fills_n = sum(r["buys"] for r in eng.results)
    n = len(pnl)
    mean = sum(pnl) / n if n else 0.0
    sd = (sum((x - mean) ** 2 for x in pnl) / max(n - 1, 1)) ** 0.5
    return (off, band, fills), dict(pnl=sum(pnl), se=sd * n ** 0.5, staked=staked, mkts=n,
                                    mkts_h=n / ((hi - lo) / 3600), fills_h=fills_n / ((hi - lo) / 3600))


if __name__ == "__main__":
    name = sys.argv[1]
    grid = list(itertools.product((0.0, 0.01, 0.02, 0.03, 0.05), ((0.80, 0.90), (0.70, 0.95)), (1, 3)))
    if len(sys.argv) > 2:
        want = set(sys.argv[2].split(";"))
        grid = [p for p in grid if label(p) in want]
    with mp.Pool(min(20, len(grid))) as pool:
        res = pool.map(one, [(name, p) for p in grid])
    print(f"===== {name} (no daily loss limit)")
    for p, m in sorted(res, key=lambda r: -r[1]["pnl"] / max(r[1]["se"], 1)):
        print(f"  {label(p):34s} P&L {m['pnl']:+8.1f} +-{m['se']:6.1f} (t {m['pnl'] / max(m['se'], 1):+5.2f})"
              f"  ret {m['pnl'] / max(m['staked'], 1) * 100:+5.2f}%  markets/h {m['mkts_h']:4.1f}  fills/h {m['fills_h']:4.1f}")
