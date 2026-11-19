"""Event-driven core. Feed it events; it tracks windows, asks the strategy for
orders, hands them to a broker and settles each window.

It never reads a clock or a socket: time is whatever the events say. That is
what lets the exact same engine run live, on paper and over a recording.

Events are plain dicts (so they serialise to JSON lines unchanged):
  {"t": "px",    "ts", "src": "chainlink" | "spot", "p", "pts"}   price tick
  {"t": "win",   "ts", "start", "end", "slug", "up", "down", "fee_rate",
                 "fee_exp", "min_size", "taker_delay_s"?, "strike"?} market found
  {"t": "book",  "ts", "tok", "bids": [[p, s]...], "asks": [...]}  full book
  {"t": "delta", "ts", "tok", "side": "BUY" | "SELL", "p", "s"}    one level
  {"t": "resolved", "ts", "tok"}                                   winning token
  {"t": "clock", "ts"}                                             heartbeat
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .book import Book
from .broker import Fill, WindowView
from .config import Config
from .model import VolEstimator
from .rest import RestingQuoter
from .risk import Risk
from .strategy import DOWN, SIDES, UP, Fair, decide, fair_value


@dataclass
class Window:
    start: int
    end: int
    slug: str
    tokens: dict[str, str]
    fee_rate: float
    fee_exp: float
    min_size: float
    taker_delay_s: float = 0.0
    strike: float | None = None
    strike_note: str = ""
    final: float | None = None
    final_note: str = ""
    official: str | None = None          # winner reported by the exchange
    books: dict[str, Book] = field(default_factory=lambda: {UP: Book(), DOWN: Book()})
    held: dict[str, float] = field(default_factory=lambda: {UP: 0.0, DOWN: 0.0})
    cash: float = 0.0                    # net cash flow of this window
    spent: float = 0.0                   # gross buys, fees included
    fees: float = 0.0
    buys: int = 0
    misses: int = 0
    last_order_ts: float = 0.0
    started: bool = False
    settled: bool = False
    sigma_start: float = float("nan")    # the bot's volatility estimate when the market opened


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


class Engine:
    def __init__(
        self,
        cfg: Config,
        broker,
        journal=None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.journal = journal
        self.log = log
        self.risk = Risk(cfg)
        self.vol = VolEstimator(
            cfg.vol_fast_halflife_s, cfg.vol_slow_halflife_s, cfg.vol_fast_weight,
            cfg.vol_floor, cfg.vol_min_samples,
        )
        self.now = 0.0
        self.windows: dict[int, Window] = {}
        self._by_token: dict[str, tuple[Window, str]] = {}
        self.cl: float | None = None      # last oracle price
        self.cl_ts = 0.0                  # when it arrived
        self.cl_pts = 0.0                 # the oracle's own timestamp for it
        self._ticks: deque[tuple[float, float]] = deque()   # recent (oracle time, price)
        self.skew: float | None = None    # arrival time - oracle timestamp (EWMA)
        self.spot: float | None = None
        self.spot_ts = 0.0
        self.basis = 0.0
        self.basis_n = 0
        self._last_decide = 0.0
        self._last_status = 0.0
        self.idle_reason = "starting"
        self.last_fair: Fair | None = None
        self.results: list[dict] = []
        self.fills: list[Fill] = []
        self.on_fair: Callable | None = None   # research hook: (window, fair, price)
        self.quoter = RestingQuoter(cfg) if cfg.strategy == "rest" else None

    # ------------------------------------------------------------------ events
    def on_event(self, ev: dict) -> None:
        ts = float(ev["ts"])
        if ts > self.now:
            self.now = ts
        kind = ev["t"]
        if kind == "px":
            self._on_price(ev)
        elif kind == "book":
            hit = self._by_token.get(ev["tok"])
            if hit:
                hit[0].books[hit[1]].set_snapshot(ev["bids"], ev["asks"], ts)
        elif kind == "delta":
            hit = self._by_token.get(ev["tok"])
            if hit:
                hit[0].books[hit[1]].apply(ev["side"], ev["p"], ev["s"], ts)
        elif kind == "win":
            self._on_window(ev)
        elif kind == "resolved":
            hit = self._by_token.get(ev["tok"])
            if hit:
                hit[0].official = hit[1]
        elif kind == "trade":
            hit = self._by_token.get(ev["tok"])
            if hit and self.quoter and not hit[0].settled:
                for fill in self.quoter.on_trade(hit[0], hit[1], float(ev["p"]),
                                                 float(ev.get("s") or 0.0), ts):
                    self._apply_fill(fill)
        self._housekeeping()
        self._maybe_trade()
        self._status()

    def _on_window(self, ev: dict) -> None:
        start = int(ev["start"])
        if start in self.windows:
            return
        w = Window(
            start=start, end=int(ev["end"]), slug=ev.get("slug", ""),
            tokens={UP: ev["up"], DOWN: ev["down"]},
            fee_rate=float(ev.get("fee_rate", self.cfg.default_fee_rate)),
            fee_exp=float(ev.get("fee_exp", self.cfg.default_fee_exponent)),
            min_size=float(ev.get("min_size", 5.0)),
            taker_delay_s=float(ev.get("taker_delay_s", 0.0)),
        )
        if ev.get("strike"):
            w.strike, w.strike_note = float(ev["strike"]), "from market data"
        self.windows[start] = w
        self._by_token[w.tokens[UP]] = (w, UP)
        self._by_token[w.tokens[DOWN]] = (w, DOWN)

    def _on_price(self, ev: dict) -> None:
        ts, p = float(ev["ts"]), float(ev["p"])
        if p <= 0:
            return
        if ev["src"] == "spot":
            self.spot, self.spot_ts = p, ts
            return
        pts = float(ev.get("pts", ts))
        self.vol.update(pts, p)
        self.cl, self.cl_ts, self.cl_pts = p, ts, pts
        keep = self.cfg.twap_s + 10.0
        self._ticks.append((pts, p))
        while self._ticks and self._ticks[0][0] < pts - keep:
            self._ticks.popleft()
        lag = ts - pts
        self.skew = lag if self.skew is None else 0.9 * self.skew + 0.1 * lag
        if self.spot is not None and ts - self.spot_ts <= self.cfg.max_spot_age_s:
            a = 1.0 - 0.5 ** (1.0 / max(self.cfg.basis_halflife_s, 1.0))
            diff = p - self.spot
            self.basis = diff if self.basis_n == 0 else self.basis + a * (diff - self.basis)
            self.basis_n += 1
        grace = self.cfg.strike_grace_s
        for w in self.windows.values():
            if w.strike is None and not w.strike_note and pts >= w.start:
                mark = self._boundary_price(w.start, p)
                if pts - w.start <= grace and mark is not None:
                    w.strike = mark
                    w.strike_note = "exact" if pts == w.start else f"+{pts - w.start:.1f}s"
                else:
                    w.strike_note = "missed"   # joined too late to know it: do not guess
            if w.final is None and pts >= w.end:
                mark = self._boundary_price(w.end, p)
                w.final = p if mark is None else mark
                w.final_note = ("approx" if mark is None else
                                "exact" if pts == w.end else f"+{pts - w.end:.1f}s")

    def _boundary_price(self, boundary: float, tick: float) -> float | None:
        """The settlement price at a window boundary: the tick itself, or the
        average of the ticks over the `twap_s` seconds up to the boundary. None
        if not enough of that stretch was seen."""
        span = self.cfg.twap_s
        if span <= 0:
            return tick
        vals = [p for t, p in self._ticks if boundary - span < t <= boundary]
        first = next((t for t, _ in self._ticks if t > boundary - span), None)
        if len(vals) < span / 4 or first is None or first > boundary - span + 5.0:
            return None
        return sum(vals) / len(vals)

    def settle_terms(self, w: Window, spot: float) -> tuple[float, float]:
        """(strike, seconds) for the pricing model, which thinks in terms of one
        price at one instant.

        With an averaged settlement price the thing being predicted is the mean
        over the last `twap_s` seconds. Before that stretch starts, its
        uncertainty is that of a price twap_s*2/3 seconds earlier than the end.
        Inside it, part of the average is already fixed: what is left to decide
        is whether the rest averages above the level that would lift the whole
        mean to the strike, and an average over r seconds is as uncertain as a
        price r/3 seconds ahead.
        """
        span = self.cfg.twap_s
        if span <= 0:
            return w.strike, w.end - self.now
        # Time left as of the price in use: the faster exchange feed is current,
        # the oracle's last tick is already a little old.
        left = w.end - (self.now if self._spot_usable() else self.cl_pts)
        if left >= span:
            return w.strike, left - span * 2.0 / 3.0
        if left <= 0:
            return w.strike, 0.0
        seen = [p for t, p in self._ticks if t > w.end - span]
        fixed = sum(seen) / len(seen) if seen else spot
        need = (span * w.strike - (span - left) * fixed) / left
        return min(max(need, spot * 0.9), spot * 1.1), left / 3.0

    # ------------------------------------------------------------ housekeeping
    def _view(self, start: int) -> WindowView | None:
        w = self.windows.get(start)
        if w is None:
            return None
        return WindowView(w.end, w.books, w.fee_rate, w.fee_exp, w.taker_delay_s)

    def _housekeeping(self) -> None:
        for fill in self.broker.poll(self.now, self._view):
            self._apply_fill(fill)
        for w in list(self.windows.values()):
            if not w.started and self.now >= w.start:
                w.started = True
                w.sigma_start = self.vol.sigma if self.vol.ready else float("nan")
                self.risk.on_window_start(self.now)
            if not w.settled and self.now >= w.end:
                waited = self.now - w.end
                if w.final is not None and (w.official or waited >= 1.0 or w.buys == 0):
                    self._settle(w)
                elif waited >= self.cfg.settle_timeout_s:
                    self._settle(w)
            if w.settled and self.now - w.end > 120:
                del self.windows[w.start]
                for tok in w.tokens.values():
                    self._by_token.pop(tok, None)

    def _apply_fill(self, f: Fill) -> None:
        o = f.order
        self.fills.append(f)
        w = self.windows.get(o.window)
        if w is None:
            self.log(f"!! fill for unknown window {o.window}: {f}")
            return
        if f.shares <= 0:
            w.misses += 1
            self.log(f"{_hms(self.now)} MISS {o.action} {o.side} limit {o.limit:.3f} ({f.note})")
        else:
            if o.action == "BUY":
                w.held[o.side] += f.shares
                w.cash -= f.notional + f.fee
                w.spent += f.notional + f.fee
                w.buys += 1
            else:
                w.held[o.side] = max(w.held[o.side] - f.shares, 0.0)
                w.cash += f.notional - f.fee
            w.fees += f.fee
            self.log(
                f"{_hms(self.now)} FILL {o.action} {o.side} {f.shares:.2f} @ {f.price:.3f} "
                f"fee {f.fee:.3f} | fair {o.p_fair:.3f} used {o.p_used:.3f} edge {o.edge:+.3f}"
            )
        if self.journal:
            self.journal.trade(self.now, w, f)

    def _settle(self, w: Window) -> None:
        w.settled = True
        note = ""
        if w.official:
            winner = w.official
            if w.strike is not None and w.final is not None:
                mine = UP if w.final >= w.strike else DOWN
                if mine != winner:
                    note = f"MISMATCH: exchange says {winner}, local prices say {mine}"
        elif w.strike is not None and w.final is not None:
            winner = UP if w.final >= w.strike else DOWN
        elif w.strike is not None and self.cl is not None:
            winner, note = (UP if self.cl >= w.strike else DOWN), "no closing tick: estimated"
        elif w.strike is None:
            winner, note = "?", "sat out: strike not seen"
        else:
            winner, note = "?", "outcome unknown"
        if self.quoter:
            extra = self.quoter.touch_note(w, winner)
            note = f"{note}; {extra}" if note and extra else (note or extra)
        traded = w.buys > 0
        pnl = w.cash + (w.held.get(winner, 0.0) if winner in SIDES else 0.0)
        if traded and winner == "?":
            note = "outcome unknown: P&L counts open shares as worthless"
        self.risk.on_settle(self.now, pnl, traded)
        res = {
            "start": w.start, "slug": w.slug, "strike": w.strike, "final": w.final,
            "winner": winner, "buys": w.buys, "misses": w.misses, "spent": round(w.spent, 4),
            "fees": round(w.fees, 4), "pnl": round(pnl, 4),
            "bankroll": round(self.risk.bankroll, 4), "note": note,
            "sigma": w.sigma_start,
        }
        self.results.append(res)
        if traded or note:
            self.log(
                f"{_hms(self.now)} SETTLE {_hms(w.start)} winner {winner} "
                f"pnl {pnl:+.2f} bankroll {self.risk.bankroll:.2f} {note}"
            )
        if self.journal:
            self.journal.window(res)

    # ------------------------------------------------------------------ trading
    def sizing_bankroll(self) -> float:
        """Capital that bet sizes are based on. Without compounding, wins are banked
        rather than reinvested; losses still shrink it."""
        if self.cfg.compound:
            return self.risk.bankroll
        return min(self.risk.bankroll, self.cfg.bankroll)

    def _spot_usable(self) -> bool:
        c = self.cfg
        return (c.spot_lead and self.spot is not None and self.basis_n >= 20
                and self.now - self.spot_ts <= c.max_spot_age_s)

    def price_now(self) -> float | None:
        if self._spot_usable():
            return self.spot + self.basis
        return self.cl

    def active(self) -> Window | None:
        for w in self.windows.values():
            if w.start <= self.now < w.end:
                return w
        return None

    def _maybe_trade(self) -> None:
        c = self.cfg
        if self.now - self._last_decide < c.decide_interval_s:
            return
        self._last_decide = self.now
        w = self.active()
        if w is None:
            self.idle_reason = "no active market"
            return
        if self.quoter:
            self._quote(w)
            return
        if self.broker.busy:
            self.idle_reason = "order in flight"
            return
        spot = self.price_now()
        # Data-quality gates: without these nothing is known well enough to act on.
        if w.strike is None:
            self.idle_reason = f"no strike ({w.strike_note or 'waiting'})"
            return
        if spot is None or self.now - self.cl_ts > c.max_price_age_s:
            self.idle_reason = "price feed stale"
            return
        if not self.vol.ready:
            self.idle_reason = f"warming up volatility ({self.vol.samples}/{c.vol_min_samples})"
            return
        if self.skew is not None and abs(self.skew) > c.max_clock_skew_s:
            self.idle_reason = f"clock skew {self.skew:+.1f}s vs oracle"
            return
        if any(self.now - b.ts > c.max_book_age_s for b in w.books.values()):
            self.idle_reason = "order book stale"
            return
        tau = w.end - self.now
        # A delayed oracle feed means the price is already old when it arrives.
        age = self.skew if (spot == self.cl and self.skew) else 0.0
        age = max(age, 0.0) + w.taker_delay_s   # the exchange also makes takers wait
        strike, horizon = self.settle_terms(w, spot)
        fair = fair_value(c, spot, strike, horizon, self.vol.sigma, price_age_s=age)
        self.last_fair = fair
        if self.on_fair:
            self.on_fair(w, fair, spot)
        block = self.risk.blocked(self.now)
        if tau > (w.end - w.start) - c.no_trade_first_s or tau < c.no_trade_last_s:
            block = block or "outside trading part of the window"
        elif w.buys >= c.max_trades_per_window:
            block = block or "max trades this window"
        elif self.now - w.last_order_ts < c.trade_cooldown_s:
            block = block or "cool-down after order"
        order = decide(
            c, window=w.start, now=self.now, fair=fair, books=w.books, tokens=w.tokens,
            held=w.held, fee_rate=w.fee_rate, fee_exp=w.fee_exp, min_size=w.min_size,
            bankroll=self.sizing_bankroll(), window_budget_left=c.max_window_usd - w.spent,
            allow_entry=block is None,
        )
        if order is None:
            self.idle_reason = block or "no edge"
            return
        w.last_order_ts = self.now
        self.idle_reason = "order sent"
        self.broker.submit(order, self._view(w.start))

    def _quote(self, w: Window) -> None:
        """The resting-bid strategy: no model involved, only data-quality gates."""
        block = self.risk.blocked(self.now)
        if w.strike is None:
            block = block or f"no strike ({w.strike_note or 'waiting'})"
        elif any(self.now - b.ts > self.cfg.max_book_age_s for b in w.books.values()):
            block = block or "order book stale"
        elif w.spent >= self.quoter.budget() - 0.5:
            block = block or "window budget used"
        elif self.cfg.rest_max_sigma > 0 and (not self.vol.ready
                                               or self.vol.sigma > self.cfg.rest_max_sigma):
            block = block or "bitcoin too volatile"
        self.idle_reason = self.quoter.update(w, self.now, block)
        if block:
            self.idle_reason = block

    # ------------------------------------------------------------------- status
    def _status(self) -> None:
        if self.cfg.status_interval_s <= 0 or self.now - self._last_status < self.cfg.status_interval_s:
            return
        self._last_status = self.now
        w = self.active()
        if w is None:
            self.log(f"{_hms(self.now)} idle: {self.idle_reason}")
            return

        def q(side: str) -> str:
            b, a = w.books[side].best_bid(), w.books[side].best_ask()
            return f"{b if b is not None else '-'}/{a if a is not None else '-'}"

        f = self.last_fair
        held = ", ".join(f"{s} {n:.1f}" for s, n in w.held.items() if n > 0) or "flat"
        self.log(
            f"{_hms(self.now)} t-{w.end - self.now:3.0f}s px {self.cl or 0:.2f} "
            f"strike {w.strike or 0:.2f} pUp {f.p_up if f else float('nan'):.3f} "
            f"| Up {q(UP)} Down {q(DOWN)} | {held} | bank {self.risk.bankroll:.2f} "
            f"| {self.idle_reason}"
        )

    def book_snapshots(self, depth: int) -> list[dict]:
        """Top `depth` levels of each live book, as replayable `book` events."""
        out = []
        for w in self.windows.values():
            if not (w.start - 5 <= self.now < w.end):
                continue
            for side in SIDES:
                book = w.books[side]
                if not book.bids and not book.asks:
                    continue
                out.append({
                    "t": "book", "ts": round(self.now, 3), "tok": w.tokens[side],
                    "bids": [list(x) for x in book.bids_sorted()[:depth]],
                    "asks": [list(x) for x in book.asks_sorted()[:depth]],
                })
        return out

    def summary(self) -> dict:
        r = self.risk
        traded = [x for x in self.results if x["buys"] > 0]
        return {
            "windows_seen": len(self.results),
            "windows_traded": r.windows_traded,
            "win_rate": round(r.windows_won / r.windows_traded, 4) if r.windows_traded else None,
            "total_pnl": round(r.total_pnl, 2),
            "fees_paid": round(sum(x["fees"] for x in traded), 2),
            "staked": round(sum(x["spent"] for x in traded), 2),
            "return_on_stake": (
                round(r.total_pnl / sum(x["spent"] for x in traded), 4) if traded else None
            ),
            "missed_orders": sum(x["misses"] for x in self.results),
            "final_bankroll": round(r.bankroll, 2),
        }
