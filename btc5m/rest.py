"""The "rest" strategy: keep a bid resting on the favourite and let long-shot
buyers trade against it.

Why. Replaying four weeks of real trades, buying cheap long-shot shares
(0.10-0.20) lost 15-25% per dollar: people overpay for them. A resting bid on
the favourite at 1-q is the same thing as an offer on the long shot at q, so it
sits on the other side of that flow. Makers pay no fee on these markets. On the
three tuning weeks this made +0.3% (strict fills) to +1.0% (generous fills) per
dollar; on the held-out week +1.0% to +1.7%; about 13 fills an hour. With one
side per market (below), on 708 fresh markets nothing was fitted to (Nov 16-18):
+1.9% strict, +3.0% generous, about 12 fills an hour. The uncertainty is in the
fills, which trade data cannot settle.

Rules (all in Config, `rest_*`):
  - between rest_from_s and rest_until_s into the market, when a side's best bid
    is within [rest_fav_min, rest_fav_max], keep a bid for rest_usd at that best
    bid (joining the queue), moving it when the best bid moves;
  - one side per market: once a side fills, the other side's bid is cancelled
    (holding both at 0.80+ would lock in a loss);
  - up to rest_max_fills orders of rest_usd per market (an order stays up until it
    is completely filled; the next one goes up rest_refill_s after a fill), within
    max_window_usd; cancel everything at rest_until_s;
  - orders take paper_latency_s to appear, move or disappear.

Paper fills. A trade is a print in either token; a trade in the long shot at
price x is a trade in the favourite at 1-x. A resting bid at b
  - fills STRICTLY when the favourite trades below b: the sweep went through our
    level, so our order was used up wherever it sat in the queue. This is what
    the paper ledger books;
  - is TOUCHED when the favourite trades exactly at b: filled only if our place
    in the queue came up. Not booked; counted per market so the generous
    figure can be read off the log.
Paper-only for now: real resting orders need order-lifecycle handling (partial
fills, cancel/fill races) that the live broker does not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .broker import Fill
from .strategy import DOWN, SIDES, UP, Order


@dataclass
class _Quote:
    price: float          # bid price on the favourite
    shares: float
    active_from: float    # when the exchange would have it
    filled: float = 0.0
    cancel_at: float = 1e18


@dataclass
class _MarketState:
    quotes: dict[str, list[_Quote]] = field(default_factory=lambda: {UP: [], DOWN: []})
    done: set[str] = field(default_factory=set)        # sides that have had a fill
    side: str | None = None                            # the side this market is locked to
    spent: float = 0.0                                 # money paid for fills so far
    next_ok: float = 0.0                               # no new order before this time
    touch: list[tuple[str, float, float]] = field(default_factory=list)  # side, price, shares


class RestingQuoter:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.state: dict[int, _MarketState] = {}

    # ----------------------------------------------------------- quoting
    def update(self, w, now: float, blocked: str | None) -> str:
        """Re-quote one market; returns a short status for the log."""
        c = self.cfg
        st = self.state.setdefault(w.start, _MarketState())
        t = now - w.start
        left = self.budget() - st.spent
        quoting = []
        for side in SIDES:
            want = None
            if (blocked is None and st.side in (None, side) and left > 0.5
                    and c.rest_from_s <= t < c.rest_until_s):
                bid = w.books[side].best_bid()
                ask = w.books[side].best_ask()
                if (bid is not None and c.rest_fav_min <= bid <= c.rest_fav_max
                        and (ask is None or ask > bid)):
                    want = bid
            live = [q for q in st.quotes[side] if q.cancel_at > now]
            st.quotes[side] = live
            pending = [q for q in live if q.cancel_at >= 1e17]
            current = pending[-1].price if pending else None
            if want is None:
                if pending:
                    self._cancel(st, side, now)
                continue
            quoting.append(f"{side} {want:.2f}")
            if (current is None or abs(current - want) > 1e-9) and now >= st.next_ok:
                if pending:
                    self._cancel(st, side, now)
                shares = round(min(c.rest_usd, left) / want, 2)
                if shares >= max(w.min_size, 5.0):
                    st.quotes[side].append(_Quote(want, shares, now + c.paper_latency_s))
        return ("resting bid " + ", ".join(quoting)) if quoting else "no favourite in band"

    def budget(self) -> float:
        c = self.cfg
        return min(c.rest_usd * c.rest_max_fills, c.max_window_usd)

    def _cancel(self, st: _MarketState, side: str, now: float) -> None:
        """A cancel also takes paper_latency_s: until then the old order can still fill."""
        until = now + self.cfg.paper_latency_s
        for q in st.quotes[side]:
            q.cancel_at = min(q.cancel_at, until)
        st.quotes[side] = [q for q in st.quotes[side] if q.cancel_at > now]

    # ------------------------------------------------------------ trades
    def on_trade(self, w, side_traded: str, price: float, size: float, now: float) -> list[Fill]:
        """A real trade in one of this market's tokens. Returns paper fills."""
        st = self.state.get(w.start)
        if st is None:
            return []
        fills = []
        for fav in SIDES:
            # the favourite's price for this trade
            px = price if side_traded == fav else round(1.0 - price, 4)
            for q in list(st.quotes[fav]):
                if now < q.active_from or now >= q.cancel_at:
                    continue
                if px < q.price - 1e-9:                    # swept through our level
                    take = round(min(q.shares - q.filled, size), 2)
                    if take <= 0:
                        continue
                    q.filled += take
                    size -= take
                    st.spent += take * q.price
                    fills.append(self._fill(w, fav, q.price, take, now))
                    st.done.add(fav)
                    if st.side is None:
                        st.side = fav
                        other = DOWN if fav == UP else UP
                        self._cancel(st, other, now)   # still fillable until the cancel lands
                    if q.filled >= q.shares - 1e-9:
                        st.quotes[fav].remove(q)
                        st.next_ok = now + self.cfg.rest_refill_s
                    if st.spent >= self.budget() - 0.5:
                        for s in SIDES:
                            self._cancel(st, s, now)
                elif abs(px - q.price) <= 1e-9:            # at our level: queue unknown
                    st.touch.append((fav, q.price, min(q.shares, size)))
        return fills

    def _fill(self, w, side: str, price: float, shares: float, now: float) -> Fill:
        o = Order(w.start, side, "BUY", w.tokens[side], round(shares * price, 4), shares,
                  price, now, float("nan"), float("nan"), 0.0)
        return Fill(o, now, shares, shares * price, 0.0, "resting bid (strict fill)")

    # -------------------------------------------------------- settlement
    def touch_note(self, w, winner: str) -> str:
        """What the generous rule would have added (first touch per side not filled)."""
        st = self.state.pop(w.start, None)
        if st is None or not st.touch:
            return ""
        seen, pnl, n = set(), 0.0, 0
        for side, price, shares in st.touch:
            if side in seen or side in st.done:
                continue
            seen.add(side)
            n += 1
            pnl += shares * ((1.0 if winner == side else 0.0) - price)
        return f"touch-only {n} pnl {pnl:+.2f}" if n else ""
