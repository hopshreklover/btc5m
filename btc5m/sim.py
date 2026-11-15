"""Offline simulator: real BTC prices, a made-up market maker.

This exists to exercise the whole pipeline without Polymarket and to sanity
check behaviour (for example: against a market maker that prices as well as
the bot does, the bot should hardly trade). The order books are synthetic, so
profit here says NOTHING about profit on the real exchange.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

from .broker import PaperBroker
from .config import WINDOW_S, Config
from .engine import Engine
from .model import VolEstimator, prob_up


def synth_events(
    series: list[tuple[int, float]],
    cfg: Config,
    *,
    mm_lag_s: int = 3,          # how stale the market maker's price is
    mm_noise: float = 0.01,     # random error in its fair value
    mm_spread: float = 0.02,    # quoted spread on the Up token
    depth: float = 150.0,       # shares per level
    levels: int = 3,
    seed: int = 7,
) -> Iterator[dict]:
    rng = random.Random(seed)
    vol = VolEstimator(
        cfg.vol_fast_halflife_s, cfg.vol_slow_halflife_s, cfg.vol_fast_weight,
        cfg.vol_floor, cfg.vol_min_samples,
    )
    strikes: dict[int, float] = {}
    announced: set[int] = set()

    def win(ts: float, start: int) -> dict:
        announced.add(start)
        return {
            "t": "win", "ts": ts, "start": start, "end": start + WINDOW_S,
            "slug": f"sim-{start}", "up": f"UP-{start}", "down": f"DOWN-{start}",
            "fee_rate": cfg.default_fee_rate, "fee_exp": cfg.default_fee_exponent,
            "min_size": 5.0,
        }

    for i, (ts, price) in enumerate(series):
        start = ts - ts % WINDOW_S
        if start not in announced:
            yield win(ts, start)
        if start + WINDOW_S - ts <= 30 and start + WINDOW_S not in announced:
            yield win(ts, start + WINDOW_S)
        if ts % WINDOW_S == 0:
            strikes[ts] = price
            strikes.pop(ts - 2 * WINDOW_S, None)
        vol.update(ts, price)
        yield {"t": "px", "ts": ts + 0.05, "src": "chainlink", "p": price, "pts": float(ts)}

        strike = strikes.get(start)
        if strike is None or ts == start:
            continue
        stale = series[max(i - mm_lag_s, 0)][1]
        tau = start + WINDOW_S - ts
        fair = prob_up(stale, strike, tau, vol.sigma * cfg.vol_mult, cfg.student_nu)
        fair = min(max(fair + rng.gauss(0.0, mm_noise), 0.03), 0.97)
        bids, asks = [], []
        for k in range(levels):
            bid = round(fair - mm_spread / 2 - 0.01 * k, 2)
            ask = round(fair + mm_spread / 2 + 0.01 * k, 2)
            if bid >= 0.01:
                bids.append([bid, depth])
            if ask <= 0.99:
                asks.append([ask, depth])
        if bids and asks and bids[0][0] >= asks[0][0]:
            asks = [[round(a + 0.01, 2), s] for a, s in asks if a + 0.01 <= 0.99]
        yield {"t": "book", "ts": ts + 0.2, "tok": f"UP-{start}", "bids": bids, "asks": asks}
        yield {
            "t": "book", "ts": ts + 0.2, "tok": f"DOWN-{start}",
            "bids": [[round(1 - a, 2), s] for a, s in asks],
            "asks": [[round(1 - b, 2), s] for b, s in bids],
        }


def run_sim(series: list[tuple[int, float]], cfg: Config, log=print, **mm) -> Engine:
    engine = Engine(cfg, PaperBroker(cfg.paper_latency_s), journal=None, log=log)
    for ev in synth_events(series, cfg, **mm):
        engine.on_event(ev)
    return engine
