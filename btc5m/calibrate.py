"""Check the pricing model against what actually happened.

For every complete 5-minute window in a one-second price series, compare the
model's P(up) at several points inside the window with the real outcome. A
calibrated model's 70% calls come true about 70% of the time.
"""

from __future__ import annotations

import math

from .config import WINDOW_S, Config
from .model import VolEstimator, prob_up

TAUS = (240, 180, 120, 60, 30, 10)
MULTS = (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4)
NUS = (3.0, 4.0, 6.0, 200.0)


def series_from_recordings(paths: list[str]) -> list[tuple[int, float]]:
    """One-second oracle price series from files written by paper/live sessions.

    This is the series the market actually settles on, so it is the better input
    once a few days have been recorded. Gaps longer than 5 seconds are left as
    gaps (windows touching them are skipped) instead of being papered over.
    """
    from .journal import read_events

    ticks: dict[int, float] = {}
    for ev in read_events(paths):
        if ev.get("t") == "px" and ev.get("src") == "chainlink":
            ticks[int(round(ev.get("pts", ev["ts"])))] = float(ev["p"])
    out: list[tuple[int, float]] = []
    prev: int | None = None
    for sec in sorted(ticks):
        if prev is not None and 1 < sec - prev <= 5:
            out.extend((s, ticks[prev]) for s in range(prev + 1, sec))
        out.append((sec, ticks[sec]))
        prev = sec
    return out


def samples(series: list[tuple[int, float]], cfg: Config) -> list[tuple[int, float, float, float, bool]]:
    """(tau, price, strike, sigma, went_up) for each checkpoint of each window."""
    vol = VolEstimator(
        cfg.vol_fast_halflife_s, cfg.vol_slow_halflife_s, cfg.vol_fast_weight,
        cfg.vol_floor, cfg.vol_min_samples,
    )
    price: dict[int, float] = {}
    sigma: dict[int, float] = {}
    for ts, p in series:
        vol.update(ts, p)
        price[ts] = p
        if vol.ready:
            sigma[ts] = vol.sigma
    out = []
    first = series[0][0]
    start = first - first % WINDOW_S + WINDOW_S
    while start + WINDOW_S in price:
        if start in price:
            strike, up = price[start], price[start + WINDOW_S] >= price[start]
            for tau in TAUS:
                t = start + WINDOW_S - tau
                if t in sigma and t in price:
                    out.append((tau, price[t], strike, sigma[t], up))
        start += WINDOW_S
    return out


def score(rows, mult: float, nu: float) -> tuple[float, float]:
    """(Brier score, log loss); lower is better. Always guessing 50% scores 0.25 / 0.693."""
    brier = loss = 0.0
    for tau, s, k, sig, up in rows:
        p = prob_up(s, k, tau, sig * mult, nu)
        y = 1.0 if up else 0.0
        brier += (p - y) ** 2
        q = min(max(p, 1e-4), 1 - 1e-4)
        loss -= math.log(q) if up else math.log(1 - q)
    n = max(len(rows), 1)
    return brier / n, loss / n


def report(series: list[tuple[int, float]], cfg: Config) -> dict:
    rows = samples(series, cfg)
    if not rows:
        raise SystemExit("not enough data: need at least a few complete 5-minute windows")
    grid = {(m, nu): score(rows, m, nu) for m in MULTS for nu in NUS}
    best = min(grid, key=lambda key: grid[key][1])
    mine = score(rows, cfg.vol_mult, cfg.student_nu)

    buckets: dict[int, list[float]] = {}
    for tau, s, k, sig, up in rows:
        p = prob_up(s, k, tau, sig * cfg.vol_mult, cfg.student_nu)
        b = buckets.setdefault(min(int(p * 10), 9), [0, 0.0, 0.0])
        b[0] += 1
        b[1] += p
        b[2] += 1.0 if up else 0.0
    by_tau = {tau: score([r for r in rows if r[0] == tau], cfg.vol_mult, cfg.student_nu)
              for tau in TAUS}
    return {
        "samples": len(rows), "current": mine, "best": (best, grid[best]),
        "grid": grid, "buckets": buckets, "by_tau": by_tau,
    }


def render(rep: dict, cfg: Config) -> str:
    lines = [
        f"{rep['samples']} checkpoints from about {rep['samples'] // len(TAUS)} windows",
        "",
        f"current settings (vol_mult={cfg.vol_mult}, student_nu={cfg.student_nu}): "
        f"Brier {rep['current'][0]:.4f}, log loss {rep['current'][1]:.4f}   "
        "(always-50% baseline: 0.2500 / 0.6931)",
        "",
        "predicted vs. what happened (current settings):",
        "  bucket      n   avg predicted   actually up",
    ]
    for b in sorted(rep["buckets"]):
        n, sp, sy = rep["buckets"][b]
        lines.append(f"  {b / 10:.1f}-{(b + 1) / 10:.1f}  {int(n):5d}       {sp / n:.3f}          {sy / n:.3f}")
    lines += ["", "by time left (current settings):", "  seconds left   Brier   log loss"]
    for tau, (br, ll) in rep["by_tau"].items():
        lines.append(f"  {tau:8d}      {br:.4f}   {ll:.4f}")
    lines += ["", "log loss by vol_mult (rows) and student_nu (columns):",
              "  mult   " + "".join(f"nu={nu:<7g}" for nu in NUS)]
    for m in MULTS:
        lines.append(f"  {m:<6g} " + "".join(f"{rep['grid'][(m, nu)][1]:<10.4f}" for nu in NUS))
    (bm, bnu), (bbr, bll) = rep["best"]
    lines += ["", f"best on this sample: vol_mult={bm}, student_nu={bnu:g} "
                  f"(Brier {bbr:.4f}, log loss {bll:.4f})"]
    return "\n".join(lines)
