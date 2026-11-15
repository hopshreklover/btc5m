"""Order execution. PaperBroker fills against the local book; the live broker
(in polymarket_io.py) has the same two-method interface: submit() and poll()."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .book import Book, fee_per_share
from .strategy import Order


@dataclass(frozen=True)
class Fill:
    order: Order
    ts: float
    shares: float       # 0 means the order did not fill
    notional: float     # shares * price (USD), before fees
    fee: float
    note: str = ""

    @property
    def price(self) -> float:
        return self.notional / self.shares if self.shares else 0.0


@dataclass(frozen=True)
class WindowView:
    """What a broker needs to know about a window to fill against it."""
    end: float
    books: dict[str, Book]
    fee_rate: float
    fee_exp: float


class PaperBroker:
    """Fill-and-kill simulation with a decision-to-fill delay.

    The order is matched against the book as it looks `latency_s` after the
    decision, never the book the decision was made on, and whatever it takes is
    removed from the local book so the same resting size cannot be hit twice.
    """

    def __init__(self, latency_s: float = 0.6) -> None:
        self.latency_s = latency_s
        self._pending: list[Order] = []

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def submit(self, order: Order) -> None:
        self._pending.append(order)

    def poll(self, now: float, view: Callable[[int], WindowView | None]) -> list[Fill]:
        due = [o for o in self._pending if now >= o.ts + self.latency_s]
        if not due:
            return []
        self._pending = [o for o in self._pending if o not in due]
        return [self._match(o, now, view(o.window)) for o in due]

    @staticmethod
    def _match(o: Order, now: float, w: WindowView | None) -> Fill:
        if w is None or now >= w.end:
            return Fill(o, now, 0.0, 0.0, 0.0, "window closed before fill")
        book = w.books[o.side]
        shares = notional = fee = 0.0
        if o.action == "BUY":
            for price, size in book.asks_sorted():
                if price > o.limit + 1e-9:
                    break
                f = fee_per_share(price, w.fee_rate, w.fee_exp)
                left = o.usd - notional - fee
                take = math.floor(min(size, left / (price + f)) * 100.0 + 1e-9) / 100.0
                if take <= 0:
                    break
                book.consume("SELL", price, take)
                shares += take
                notional += take * price
                fee += take * f
        else:
            for price, size in book.bids_sorted():
                if price < o.limit - 1e-9:
                    break
                take = math.floor(min(size, o.shares - shares) * 100.0 + 1e-9) / 100.0
                if take <= 0:
                    break
                book.consume("BUY", price, take)
                shares += take
                notional += take * price
                fee += take * fee_per_share(price, w.fee_rate, w.fee_exp)
        return Fill(o, now, shares, notional, fee, "" if shares else "price moved away")
