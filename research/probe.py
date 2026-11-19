"""Research probe: run the replay without trading and record, at every second
where real trades happened, what the model thought and what the market charged.

usage: probe.py STORE PRICES LO HI OUT [feed_lag] [spot_lag|none] [twap]
"""
import sys
import time
from dataclasses import replace

import numpy as np

sys.path.insert(0, ".")
from btc5m.config import Config
from btc5m.history import Prices, read_index, read_windows, run_replay
from btc5m.strategy import DOWN, UP


def _lv(book):
    """(vwap, worst) over all ask levels: every real buy print that second."""
    if not book.asks:
        return float("nan"), float("nan")
    v = sum(book.asks.values())
    return sum(p * s for p, s in book.asks.items()) / v, max(book.asks)


def probe(store, prices, lo, hi, feed_lag=1.4, spot_lag=None, twap=60.0, stamp=2.3, **over):
    cfg = replace(Config(), status_interval_s=0.0, kill_file="", max_trades_per_window=0,
                  spot_lead=spot_lag is not None, max_clock_skew_s=6.0, twap_s=twap, **over)
    idx = read_index(store)
    rows = []
    nan = float("nan")

    def on_fair(w, fair, spot):
        bu, bd = w.books[UP], w.books[DOWN]
        au, ad = bu.best_ask(), bd.best_ask()
        if au is None and ad is None:
            return
        win = idx.get(w.start)
        if win is None:
            return
        rows.append((
            w.start, bu.ts - w.start, fair.p_up, fair.p_up_lo, fair.p_up_hi, fair.sigma,
            au if au is not None else nan, bu.asks.get(au, 0.0) if au is not None else 0.0,
            ad if ad is not None else nan, bd.asks.get(ad, 0.0) if ad is not None else 0.0,
            spot, w.strike, 1.0 if win["winner"] == UP else 0.0,
            1.0 if (bu.crossed() or bd.crossed()) else 0.0,
            *_lv(bu), *_lv(bd),
        ))

    t = time.time()
    run_replay(cfg, read_windows(store, lo, hi), prices, lo - 3600, hi + 10,
               feed_lag_s=feed_lag, shift_s=-1, stamp_delay_s=stamp, spot_lag_s=spot_lag,
               on_fair=on_fair)
    a = np.array(rows, dtype=np.float64)
    print(f"probe {lo}..{hi} feed {feed_lag} spot {spot_lag} stamp {stamp}: {len(a)} rows in {time.time() - t:.0f}s")
    return a


COLS = ["start", "t", "p", "p_lo", "p_hi", "sigma", "ask_up", "size_up", "ask_dn", "size_dn",
        "spot", "strike", "up_won", "crossed", "vwap_up", "max_up", "vwap_dn", "max_dn"]

if __name__ == "__main__":
    store, prices_path, lo, hi, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
    feed = float(sys.argv[6]) if len(sys.argv) > 6 else 1.4
    spot = None if len(sys.argv) <= 7 or sys.argv[7] == "none" else float(sys.argv[7])
    twap = float(sys.argv[8]) if len(sys.argv) > 8 else 60.0
    stamp = float(sys.argv[9]) if len(sys.argv) > 9 else 2.3
    np.save(out, probe(store, Prices.load(prices_path), lo, hi, feed, spot, twap, stamp))
