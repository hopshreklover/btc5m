"""Engine-level grid for the resting-bid strategy. Usage: grid_rest.py SET [only=label,...]"""
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
PRICES = {}


def cfg_for(usd, fills, band, until):
    return replace(Config(), strategy="rest", status_interval_s=0.0, kill_file="",
                   rest_usd=usd, rest_max_fills=fills, rest_fav_min=band[0], rest_fav_max=band[1],
                   rest_until_s=until, max_window_usd=max(30.0, usd * fills),
                   max_trade_usd=min(15.0, max(30.0, usd * fills)), daily_loss_limit=4 * usd)


def one(args):
    setname, params = args
    store, pfile, lo, hi = SETS[setname]
    if pfile not in PRICES:
        PRICES[pfile] = Prices.load(pfile)
    cfg = cfg_for(*params)
    eng = run_replay(cfg, read_windows(store, lo, hi), PRICES[pfile], lo - 3600, hi + 10,
                     feed_lag_s=1.4, shift_s=-1, stamp_delay_s=2.3)
    days, bank = {}, [cfg.bankroll]
    staked = pnl = 0.0
    fills = 0
    for r in eng.results:
        d = int(r["start"] // 86400)
        days[d] = days.get(d, 0.0) + r["pnl"]
        bank.append(r["bankroll"])
        staked += r["spent"]; pnl += r["pnl"]; fills += r["buys"]
    hours = (hi - lo) / 3600
    se = (sum((v - pnl / len(days)) ** 2 for v in days.values()) / max(len(days) - 1, 1)) ** 0.5 * len(days) ** 0.5
    return params, dict(pnl=pnl, staked=staked, ret=pnl / staked * 100 if staked else 0, fills_h=fills / hours,
                        worst_day=min(days.values()) if days else 0, low=min(bank), se=se,
                        days_neg=sum(v < 0 for v in days.values()), ndays=len(days))


def label(p):
    usd, fills, band, until = p
    return f"${usd:<3g} x{fills} band {band[0]:.2f}-{band[1]:.2f} until {until}"


if __name__ == "__main__":
    setname = sys.argv[1]
    grid = list(itertools.product((10.0, 20.0, 30.0), (1, 2, 3),
                                  ((0.80, 0.90), (0.75, 0.92), (0.85, 0.95)), (240.0, 270.0)))
    if len(sys.argv) > 2:
        want = set(sys.argv[2].split(";"))
        grid = [p for p in grid if label(p) in want]
    with mp.Pool(min(20, len(grid))) as pool:
        res = pool.map(one, [(setname, p) for p in grid])
    res.sort(key=lambda r: -r[1]["pnl"])
    print(f"===== {setname}")
    for p, m in res:
        print(f"  {label(p):34s} P&L {m['pnl']:+8.2f} +-{m['se']:6.1f}  staked {m['staked']:8.0f}  "
              f"ret {m['ret']:+5.2f}%  fills/h {m['fills_h']:5.1f}  worst day {m['worst_day']:+7.1f}  "
              f"lowest bankroll {m['low']:6.1f}  losing days {m['days_neg']}/{m['ndays']}")
