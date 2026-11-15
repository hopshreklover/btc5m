"""Order execution. PaperBroker fills against the local book; the live broker
(in polymarket_io.py) has the same interface: submit() and poll()."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

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
    taker_delay_s: float = 0.0   # exchange-imposed wait before a taker order matches


@dataclass
class _Pending:
    order: Order
    due: float
    # price -> size that has been continuously available since the decision
    avail: dict[float, float] = field(default_factory=dict)


def _floor2(x: float) -> float:
    return math.floor(x * 100.0 + 1e-9) / 100.0


class PaperBroker:
    """Fill-and-kill simulation that assumes the bot is slow and unlucky.

    An order only fills against size that was already resting at an acceptable
    price when the decision was made AND stayed there, untouched, for the whole
    wait (the bot's own latency plus any taker delay the exchange imposes).
    Quotes that are pulled or shrink in between are gone, and liquidity that
    shows up later does not count. Whatever is taken is removed from the local
    book so the same resting size cannot be hit twice.
    """

    def __init__(self, latency_s: float = 0.6) -> None:
        self.latency_s = latency_s
        self._pending: list[_Pending] = []

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    @staticmethod
    def _levels(o: Order, book: Book) -> dict[float, float]:
        if o.action == "BUY":
            return {p: s for p, s in book.asks.items() if p <= o.limit + 1e-9}
        return {p: s for p, s in book.bids.items() if p >= o.limit - 1e-9}

    def submit(self, order: Order, view: WindowView) -> None:
        self._pending.append(_Pending(
            order, order.ts + self.latency_s + view.taker_delay_s,
            self._levels(order, view.books[order.side]),
        ))

    def poll(self, now: float, view: Callable[[int], WindowView | None]) -> list[Fill]:
        fills: list[Fill] = []
        keep: list[_Pending] = []
        for pend in self._pending:
            w = view(pend.order.window)
            if w is not None:
                current = self._levels(pend.order, w.books[pend.order.side])
                for price in pend.avail:
                    pend.avail[price] = min(pend.avail[price], current.get(price, 0.0))
            if now >= pend.due:
                fills.append(self._match(pend, now, w))
            else:
                keep.append(pend)
        self._pending = keep
        return fills

    @staticmethod
    def _match(pend: _Pending, now: float, w: WindowView | None) -> Fill:
        o = pend.order
        if w is None or now >= w.end:
            return Fill(o, now, 0.0, 0.0, 0.0, "window closed before fill")
        book = w.books[o.side]
        shares = notional = fee = 0.0
        if o.action == "BUY":
            for price in sorted(pend.avail):
                f = fee_per_share(price, w.fee_rate, w.fee_exp)
                left = o.usd - notional - fee
                take = _floor2(min(pend.avail[price], left / (price + f)))
                if take <= 0:
                    continue
                book.consume("SELL", price, take)
                shares += take
                notional += take * price
                fee += take * f
        else:
            for price in sorted(pend.avail, reverse=True):
                take = _floor2(min(pend.avail[price], o.shares - shares))
                if take <= 0:
                    continue
                book.consume("BUY", price, take)
                shares += take
                notional += take * price
                fee += take * fee_per_share(price, w.fee_rate, w.fee_exp)
        return Fill(o, now, shares, notional, fee, "" if shares else "quotes gone before fill")
