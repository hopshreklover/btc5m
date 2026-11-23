"""Nested-market pair strategy (paper only).

Each 15-minute Bitcoin market ends at the same moment as the last 5-minute market
inside it, and both settle on the same final price (checked on 2,678 pairs). With
k5 / k15 their starting prices:

  k5 <= k15:  A = 5m Up + 15m Down  pays $1, or $2 if k5 <= final < k15
  k5 >  k15:  B = 5m Down + 15m Up  pays $1, or $2 if k15 <= final < k5

so the right pair can never pay $0. Buying it for less than $1 after taker fees
is a locked-in profit, provided both legs are actually bought.

Starting prices are not published during the market, so they are computed here
from the oracle ticks the same way the market does (a 60-second average); pairs
whose starting prices are too close to call are skipped. A paper buy needs the
pair to be cheap on two consecutive order-book snapshots, at sizes and prices
still available on the second one, so quotes that vanish are not counted.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

FEE_RATE = 0.07


def fee(p: float, rate: float = FEE_RATE) -> float:
    return rate * p * (1.0 - p)


def twap(ticks, boundary: float, span: float = 60.0) -> float | None:
    """Average oracle price over the `span` seconds up to `boundary`, or None if
    too little of that stretch was seen. `ticks` holds (oracle time, price)."""
    vals = [p for t, p in ticks if boundary - span < t <= boundary]
    if len(vals) < span / 4:
        return None
    first = min(t for t, _ in ticks if boundary - span < t <= boundary)
    if first > boundary - span + 5.0:
        return None
    return sum(vals) / len(vals)


def choose_pair(k5: float | None, k15: float | None, margin: float) -> str | None:
    """'a', 'b', or None when either strike is unknown or they are within `margin`
    of each other (the computed strike can differ from the official one by ~$1-2)."""
    if k5 is None or k15 is None or abs(k5 - k15) < margin:
        return None
    return "a" if k5 < k15 else "b"


def legs(pair: str) -> tuple[str, str]:
    """(5m token side, 15m token side) for a pairing."""
    return ("up", "down") if pair == "a" else ("down", "up")


def pair_quote(asks: dict, pair: str, rate: float = FEE_RATE) -> tuple[float, float, float, float]:
    """(cost per pair incl. fees, shares available, price 5m leg, price 15m leg).
    `asks` maps ("5m"|"15m", "up"|"down") -> (best ask, size) or (None, 0)."""
    s5, s15 = legs(pair)
    (p5, z5), (p15, z15) = asks[("5m", s5)], asks[("15m", s15)]
    if p5 is None or p15 is None:
        return 9.0, 0.0, 0.0, 0.0
    return p5 + p15 + fee(p5, rate) + fee(p15, rate), min(z5, z15), p5, p15


@dataclass
class PairTrade:
    start15: int
    pair: str
    shares: float
    p5: float
    p15: float
    cost: float          # per pair, fees included
    ts: float
    payoff: float | None = None

    @property
    def pnl(self) -> float | None:
        return None if self.payoff is None else self.shares * (self.payoff - self.cost)


@dataclass
class PairPaper:
    """Decides paper buys from successive order-book snapshots of one nested pair."""
    max_cost: float = 0.99       # buy only below this per pair, fees included
    min_shares: float = 5.0
    usd: float = 20.0            # per pair trade
    max_trades: int = 1          # per 15-minute market
    strike_margin_bp: float = 0.5   # skip when the starting prices are closer than this many
                                    # basis points of the price (0.5 bp ~ $3 on Bitcoin)
    trades: list[PairTrade] = field(default_factory=list)
    _pending: dict = field(default_factory=dict)   # start15 -> (pair, cost, p5, p15) seen last time
    _count: dict = field(default_factory=dict)

    def on_snapshot(self, ts: float, start15: int, asks: dict, k5: float | None,
                    k15: float | None) -> PairTrade | None:
        margin = (k15 or 0.0) * self.strike_margin_bp / 1e4
        pair = choose_pair(k5, k15, margin)
        if pair is None or self._count.get(start15, 0) >= self.max_trades:
            self._pending.pop(start15, None)
            return None
        cost, size, p5, p15 = pair_quote(asks, pair)
        prev = self._pending.get(start15)
        cheap = cost <= self.max_cost and size >= self.min_shares
        if not cheap:
            self._pending.pop(start15, None)
            return None
        if prev is None or prev[0] != pair:
            self._pending[start15] = (pair, cost, p5, p15)
            return None                      # wait one snapshot: the quotes must persist
        # second cheap snapshot in a row: buy at the worse of the two prices seen
        q5, q15 = max(p5, prev[2]), max(p15, prev[3])
        unit = q5 + q15 + fee(q5) + fee(q15)
        if unit > self.max_cost:
            self._pending[start15] = (pair, cost, p5, p15)
            return None
        shares = min(size, self.usd / unit)
        if shares < self.min_shares:
            return None
        self._pending.pop(start15, None)
        self._count[start15] = self._count.get(start15, 0) + 1
        trade = PairTrade(start15, pair, round(shares, 2), q5, q15, unit, ts)
        self.trades.append(trade)
        return trade

    def settle(self, start15: int, winner5: str, winner15: str) -> list[PairTrade]:
        """Official winners ('UP'/'DOWN') for both markets; returns the settled trades."""
        out = []
        for t in self.trades:
            if t.start15 != start15 or t.payoff is not None:
                continue
            s5, s15 = legs(t.pair)
            t.payoff = float((winner5 == s5.upper()) + (winner15 == s15.upper()))
            out.append(t)
        return out


class TickBuffer:
    """Recent oracle ticks, enough for the starting prices of the current pair."""

    def __init__(self, keep_s: float = 1500.0) -> None:
        self.keep = keep_s
        self.ticks: deque[tuple[float, float]] = deque()

    def add(self, pts: float, price: float) -> None:
        self.ticks.append((pts, price))
        while self.ticks and self.ticks[0][0] < pts - self.keep:
            self.ticks.popleft()

    def twap(self, boundary: float, span: float = 60.0) -> float | None:
        if not self.ticks or self.ticks[-1][0] < boundary:
            return None
        return twap(self.ticks, boundary, span)
