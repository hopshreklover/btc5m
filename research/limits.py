"""Does the daily loss limit / loss-streak pause help the resting-bid strategy?"""
import sys
from dataclasses import replace

sys.path.insert(0, ".")
from btc5m.config import Config
from btc5m.history import Prices, read_windows, run_replay

SETS = {"tuning": ("data/research/old", "data/research/prices.csv", 1792389900, 1794204300),
        "last week": ("data/research/last", "data/research/prices.csv", 1794204300, 1794809100),
        "fresh": ("data/research/new", "data/research/new/prices.csv", 1794809100, 1795021500)}
VARIANTS = {
    "current: $10, stop at -$40/day, pause after 4 losses": {},
    "$10, no daily stop, no pause": dict(daily_loss_limit=1e9, max_loss_streak=10**6),
    "$5, stop at -$40/day, pause after 4 losses": dict(rest_usd=5.0),
    "$5, no daily stop, no pause": dict(rest_usd=5.0, daily_loss_limit=1e9, max_loss_streak=10**6),
}
which = sys.argv[1:] or ["tuning"]
for label in which:
    store, pfile, lo, hi = SETS[label]
    prices = Prices.load(pfile)
    for name, over in VARIANTS.items():
        cfg = replace(Config(), strategy="rest", status_interval_s=0.0, kill_file="", **over)
        eng = run_replay(cfg, read_windows(store, lo, hi), prices, lo - 3600, hi + 10,
                         feed_lag_s=1.4, shift_s=-1, stamp_delay_s=2.3)
        s = eng.summary()
        days = {}
        for r in eng.results:
            d = int(r["start"] // 86400)
            days[d] = days.get(d, 0.0) + r["pnl"]
        worst = min(days.values())
        print(f"[{label}] {name:52s} fills {sum(r['buys'] for r in eng.results):5d}  P&L {s['total_pnl']:+8.2f}"
              f"  staked {s['staked']:8.0f}  ret {(s['return_on_stake'] or 0) * 100:+5.2f}%  worst day {worst:+7.2f}"
              f"  lowest bankroll {min([cfg.bankroll] + [r['bankroll'] for r in eng.results]):7.2f}")
