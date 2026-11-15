"""Local order book for one outcome token, plus the taker fee curve."""

from __future__ import annotations

from collections.abc import Iterable


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    """Polymarket taker fee in USD per share: rate * (p * (1 - p)) ** exponent.

    Same formula the official SDK uses when it sizes market buys. Highest at
    0.50, zero at the extremes.
    """
    if rate <= 0 or not 0.0 < price < 1.0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def _key(price: float) -> float:
    return round(float(price), 4)


class Book:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts: float = 0.0  # last update time

    def set_snapshot(
        self, bids: Iterable[Iterable[float]], asks: Iterable[Iterable[float]], ts: float
    ) -> None:
        self.bids = {_key(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {_key(p): float(s) for p, s in asks if float(s) > 0}
        self.ts = ts

    def apply(self, side: str, price: float, size: float, ts: float) -> None:
        """Set the resting size at a level. `side` is BUY (bids) or SELL (asks)."""
        levels = self.bids if side == "BUY" else self.asks
        k = _key(price)
        if size <= 0:
            levels.pop(k, None)
        else:
            levels[k] = float(size)
        self.ts = ts

    def consume(self, side: str, price: float, size: float) -> None:
        """Remove liquidity we took ourselves (paper fills), without touching `ts`."""
        levels = self.bids if side == "BUY" else self.asks
        k = _key(price)
        left = levels.get(k, 0.0) - size
        if left <= 1e-9:
            levels.pop(k, None)
        else:
            levels[k] = left

    def asks_sorted(self) -> list[tuple[float, float]]:
        return sorted(self.asks.items())

    def bids_sorted(self) -> list[tuple[float, float]]:
        return sorted(self.bids.items(), reverse=True)

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def crossed(self) -> bool:
        b, a = self.best_bid(), self.best_ask()
        return b is not None and a is not None and b >= a
