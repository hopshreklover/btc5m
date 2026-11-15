"""Fair value of an Up/Down window.

The market pays 1 if the oracle price at the end of the window is >= the price
at its start (the strike). With no drift over a few minutes, the probability of
that is F(ln(S/K) / (sigma * sqrt(tau))), where sigma is short-horizon
volatility and F is a fat-tailed CDF. Everything here is pure Python.
"""

from __future__ import annotations

import math


# --- Student-t CDF (regularised incomplete beta, Lentz continued fraction) ----

def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-13:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(x: float, nu: float) -> float:
    if x == 0.0:
        return 0.5
    tail = 0.5 * betainc(nu / 2.0, 0.5, nu / (nu + x * x))
    return 1.0 - tail if x > 0 else tail


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def unit_cdf(z: float, nu: float) -> float:
    """CDF of a unit-variance variable: Student-t with `nu` dof, or normal if nu >= 200."""
    if nu >= 200.0 or nu <= 2.0:
        return norm_cdf(z)
    return t_cdf(z * math.sqrt(nu / (nu - 2.0)), nu)


def prob_up(spot: float, strike: float, tau_s: float, sigma: float, nu: float = 4.0) -> float:
    """P(final >= strike) given `tau_s` seconds left and per-sqrt-second vol `sigma`."""
    if spot <= 0 or strike <= 0:
        raise ValueError("prices must be positive")
    if tau_s <= 0 or sigma <= 0:
        return 1.0 if spot >= strike else 0.0
    return unit_cdf(math.log(spot / strike) / (sigma * math.sqrt(tau_s)), nu)


# --- volatility ----------------------------------------------------------------

class VolEstimator:
    """Time-aware EWMA of squared log returns, in per-second variance units.

    Blends a fast and a slow half-life so a quiet minute cannot collapse the
    estimate and a burst still shows up quickly. Irregular tick spacing is
    handled by normalising each squared return by its own dt.
    """

    def __init__(
        self,
        fast_halflife_s: float = 120.0,
        slow_halflife_s: float = 1800.0,
        fast_weight: float = 0.6,
        floor: float = 2e-5,
        min_samples: int = 90,
        max_gap_s: float = 20.0,
    ) -> None:
        self._hl = (fast_halflife_s, slow_halflife_s)
        self._w = fast_weight
        self._floor = floor
        self._min = min_samples
        self._max_gap = max_gap_s
        self._var = [0.0, 0.0]
        self._wsum = [0.0, 0.0]
        self._prev: tuple[float, float] | None = None
        self.samples = 0

    def update(self, ts: float, price: float) -> None:
        if price <= 0:
            return
        prev = self._prev
        if prev is None:
            self._prev = (ts, price)
            return
        dt = ts - prev[0]
        if dt <= 0:
            return  # duplicate or out-of-order tick
        self._prev = (ts, price)
        if dt > self._max_gap:
            return  # feed gap: do not turn a reconnect into one giant return
        x = math.log(price / prev[1]) ** 2 / dt
        for i, hl in enumerate(self._hl):
            decay = 0.5 ** (dt / hl)
            self._var[i] = decay * self._var[i] + (1.0 - decay) * x
            self._wsum[i] = decay * self._wsum[i] + (1.0 - decay)
        self.samples += 1

    @property
    def ready(self) -> bool:
        return self.samples >= self._min

    @property
    def sigma(self) -> float:
        """Per-sqrt-second volatility of log returns (bias-corrected, floored)."""
        if self.samples == 0:
            return self._floor
        fast = self._var[0] / self._wsum[0]
        slow = self._var[1] / self._wsum[1]
        return max(math.sqrt(self._w * fast + (1.0 - self._w) * slow), self._floor)
