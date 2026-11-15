"""Everything that talks to Polymarket, via the official `polymarket-client` SDK.

Market data needs no account. Orders are only ever sent by LiveBroker, which is
only constructed by `python -m btc5m live`.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

from .book import fee_per_share
from .broker import Fill, PaperBroker
from .config import WINDOW_S, Config
from .engine import Engine
from .journal import Journal, Recorder
from .strategy import Order

SLUG = "btc-updown-5m-{start}"
ORACLE_SYMBOL = "btc/usd"

Log = Callable[[str], None]


# ----------------------------------------------------------------- discovery
def window_event(event, start: int, cfg: Config) -> dict:
    """Turn the SDK's Event for one 5-minute market into the engine's `win` event."""
    if not event.markets:
        raise ValueError(f"event {event.slug} has no markets")
    market = event.markets[0]
    tokens: dict[str, str] = {}
    for outcome in (market.outcomes.yes, market.outcomes.no):
        label = (outcome.label or "").strip().lower()
        if label in ("up", "down") and outcome.token_id:
            tokens[label] = str(outcome.token_id)
    if set(tokens) != {"up", "down"}:
        raise ValueError(f"{event.slug}: expected Up/Down outcomes with token ids")
    trading = market.trading
    if trading.fees_enabled is False:
        rate, exponent = 0.0, 1.0
    elif trading.fee_schedule is not None:
        rate, exponent = float(trading.fee_schedule.rate), float(trading.fee_schedule.exponent)
    else:  # not reported: assume the documented crypto schedule rather than zero
        rate, exponent = cfg.default_fee_rate, cfg.default_fee_exponent
    end = market.state.end_date
    if end is not None and abs(end.timestamp() - (start + WINDOW_S)) > 1:
        raise ValueError(f"{event.slug}: market ends at {end}, expected {start + WINDOW_S}")
    out = {
        "t": "win", "ts": time.time(), "start": start, "end": start + WINDOW_S,
        "slug": event.slug or "", "up": tokens["up"], "down": tokens["down"],
        "fee_rate": rate, "fee_exp": exponent,
        "min_size": float(trading.minimum_order_size or 5),
        "taker_delay_s": max(
            float(getattr(trading, "taker_delay_ms", None) or 0) / 1000.0,
            float(getattr(trading, "seconds_delay", None) or 0),
        ),
    }
    strike = (event.metadata or {}).get("priceToBeat")
    try:
        if strike is not None and float(strike) > 0:  # type: ignore[arg-type]
            out["strike"] = float(strike)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    return out


def market_events(ev, ts: float) -> list[dict]:
    """Turn one SDK market-stream event into engine events."""
    kind = getattr(ev, "type", None)
    p = ev.payload
    if kind == "book":
        return [{
            "t": "book", "ts": ts, "tok": str(p.asset_id),
            "bids": [[float(l.price), float(l.size)] for l in p.bids],
            "asks": [[float(l.price), float(l.size)] for l in p.asks],
        }]
    if kind == "price_change":
        return [
            {"t": "delta", "ts": ts, "tok": str(c.asset_id), "side": c.side,
             "p": float(c.price), "s": float(c.size)}
            for c in p.price_changes
        ]
    if kind == "market_resolved" and p.winning_asset_id:
        return [{"t": "resolved", "ts": ts, "tok": str(p.winning_asset_id)}]
    return []


# -------------------------------------------------------------------- streams
class _Retry:
    """Exponential backoff that does not flood the log with the same error."""

    def __init__(self, log: Log, what: str) -> None:
        self._log, self._what, self._n = log, what, 0

    def ok(self) -> None:
        if self._n:
            self._log(f"{self._what}: reconnected")
        self._n = 0

    def failed(self, exc: BaseException) -> float:
        self._n += 1
        if self._n in (1, 5) or self._n % 25 == 0:
            self._log(f"!! {self._what}: {exc!r} (attempt {self._n}, still retrying)")
        return min(2.0 * 2 ** (self._n - 1), 30.0)


class FeedStalled(Exception):
    pass


async def _events(handle, stall_s: float):
    """Iterate a subscription, but treat prolonged silence as a failure. A stream
    can stay connected and simply stop delivering; resubscribing fixes that."""
    while True:
        try:
            yield await asyncio.wait_for(handle.__anext__(), timeout=stall_s)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise FeedStalled(f"silent for {stall_s:.0f}s") from None


async def _oracle_task(pub, q: asyncio.Queue, log: Log, stall_s: float = 15.0) -> None:
    from polymarket.streams import CryptoPricesSpec

    spec = CryptoPricesSpec(topic="prices.crypto.chainlink", symbols=[ORACLE_SYMBOL])
    retry = _Retry(log, "oracle price stream")
    while True:
        delay = 2.0
        try:
            handle = await pub.subscribe(spec)
            try:
                async for ev in _events(handle, stall_s):
                    p = ev.payload
                    if p.symbol.lower() != ORACLE_SYMBOL:
                        continue
                    retry.ok()
                    q.put_nowait({
                        "t": "px", "ts": time.time(), "src": "chainlink",
                        "p": float(p.value), "pts": p.timestamp / 1000.0,
                    })
            finally:
                await handle.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the feed alive
            delay = retry.failed(exc)
        await asyncio.sleep(delay)


async def _auth_oracle_task(secure, q: asyncio.Queue, log: Log, stall_s: float = 15.0) -> None:
    """Authenticated price stream (the SDK's replacement for the public one)."""
    from polymarket.streams import CryptoPriceSpec

    spec = CryptoPriceSpec(symbols=["btcusd"], provider="chainlink")
    retry = _Retry(log, "authenticated price stream")
    while True:
        delay = 2.0
        try:
            handle = await secure.subscribe(spec)
            try:
                async for ev in _events(handle, stall_s):
                    p = ev.payload
                    retry.ok()
                    if ev.type == "update":
                        q.put_nowait({
                            "t": "px", "ts": time.time(), "src": "chainlink",
                            "p": float(p.value), "pts": p.timestamp.timestamp(),
                        })
                    else:  # history snapshot: warms up volatility, may recover the strike
                        for pt in p.data:
                            t = pt.timestamp.timestamp()
                            q.put_nowait({"t": "px", "ts": t, "src": "chainlink",
                                          "p": float(pt.value), "pts": t})
            finally:
                await handle.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            delay = retry.failed(exc)
        await asyncio.sleep(delay)


async def _book_task(pub, win: dict, q: asyncio.Queue, log: Log, stall_s: float = 15.0) -> None:
    from polymarket.streams import MarketSpec

    spec = MarketSpec(token_ids=[win["up"], win["down"]], custom_feature_enabled=True)
    until = win["end"] + 90
    retry = _Retry(log, f"book stream {win['slug']}")
    while time.time() < until:
        try:
            handle = await pub.subscribe(spec)
            try:
                while True:
                    left = until - time.time()
                    if left <= 0:
                        return
                    try:
                        ev = await asyncio.wait_for(
                            handle.__anext__(), timeout=min(left, stall_s + 5.0))
                    except asyncio.TimeoutError:
                        if win["start"] <= time.time() < win["end"]:
                            raise FeedStalled(f"silent for {stall_s + 5.0:.0f}s") from None
                        continue          # quiet before the open or after the close is normal
                    retry.ok()
                    for out in market_events(ev, time.time()):
                        q.put_nowait(out)
            finally:
                await handle.close()
        except StopAsyncIteration:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await asyncio.sleep(min(retry.failed(exc), 5.0))


async def _window_manager(pub, q: asyncio.Queue, cfg: Config, log: Log) -> None:
    known: set[int] = set()
    tasks: set[asyncio.Task] = set()
    warned: set[int] = set()
    while True:
        now = time.time()
        cur = int(now // WINDOW_S) * WINDOW_S
        for start in (cur, cur + WINDOW_S):
            if start in known or start > now + 45:
                continue
            try:
                event = await pub.get_event(slug=SLUG.format(start=start))
                win = window_event(event, start, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - not listed yet, or a network blip
                if start not in warned and now > start:
                    if not warned:
                        log("!! Polymarket is not reachable from here yet. If this persists, "
                            "check that this network and region can reach polymarket.com.")
                    warned.add(start)
                    log(f"!! cannot load market {SLUG.format(start=start)}: {exc!r}")
                continue
            known.add(start)
            q.put_nowait(win)
            task = asyncio.create_task(_book_task(pub, win, q, log, cfg.feed_stall_s))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        known = {s for s in known if s >= cur - WINDOW_S}
        await asyncio.sleep(2)


async def _spot_task(q: asyncio.Queue, log: Log) -> None:
    """Coinbase BTC-USD ticker: an optional faster read on where the oracle is heading."""
    import json

    import websockets

    retry = _Retry(log, "spot feed")
    while True:
        delay = 2.0
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                await ws.send(json.dumps(
                    {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}
                ))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "ticker" and msg.get("price"):
                        retry.ok()
                        q.put_nowait({"t": "px", "ts": time.time(), "src": "spot",
                                      "p": float(msg["price"])})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            delay = retry.failed(exc)
        await asyncio.sleep(delay)


async def _clock_task(q: asyncio.Queue) -> None:
    while True:
        q.put_nowait({"t": "clock", "ts": time.time()})
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------- live orders
class LiveBroker:
    """Sends real fill-and-kill market orders. One order in flight at a time."""

    def __init__(self, client, cfg: Config, log: Log) -> None:
        self._client = client
        self._cfg = cfg
        self._log = log
        self._task: asyncio.Task | None = None
        self._done: list[Fill] = []
        # window start -> (fee rate, fee exponent); the runner points this at the engine
        self.fee_lookup: Callable[[int], tuple[float, float]] = lambda _start: (
            cfg.default_fee_rate, cfg.default_fee_exponent)

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def submit(self, order: Order) -> None:
        self._task = asyncio.get_running_loop().create_task(self._place(order))

    def poll(self, now: float, view) -> list[Fill]:
        done, self._done = self._done, []
        return done

    def _halt(self, why: str) -> None:
        """An order may or may not exist: stop opening positions until a human looks."""
        self._log(f"!! HALTING NEW ENTRIES: {why}")
        try:
            Path(self._cfg.kill_file).write_text(why + "\n", encoding="utf-8")
        except OSError as exc:
            self._log(f"!! could not write kill file: {exc!r}")

    async def _place(self, o: Order) -> None:
        try:
            await self._place_inner(o)
        except Exception as exc:  # noqa: BLE001 - never lose track of an order silently
            self._halt(f"unexpected error handling an order: {exc!r}; check the account")
            self._done.append(Fill(o, time.time(), 0.0, 0.0, 0.0, f"UNKNOWN: {exc!r}"))

    async def _place_inner(self, o: Order) -> None:
        try:
            if o.action == "BUY":
                resp = await self._client.place_market_order(
                    token_id=o.token, side="BUY", amount=f"{o.usd:.2f}",
                    max_spend=f"{o.usd:.2f}", max_price=f"{o.limit:.4f}", order_type="FAK",
                )
            else:
                resp = await self._client.place_market_order(
                    token_id=o.token, side="SELL", shares=f"{o.shares:.2f}",
                    min_price=f"{o.limit:.4f}", order_type="FAK",
                )
        except Exception as exc:  # noqa: BLE001
            import polymarket.errors as errs

            # Errors that mean the order was definitely not placed. Anything else
            # (timeouts, dropped connections) leaves its fate unknown.
            not_placed = tuple(
                cls for name in (
                    "RequestRejectedError", "UserInputError", "RateLimitError",
                    "InsufficientLiquidityError", "InsufficientAllowanceError",
                    "SigningError", "CancelledSigningError",
                ) if isinstance(cls := getattr(errs, name, None), type)
            )
            if isinstance(exc, not_placed):
                self._done.append(Fill(o, time.time(), 0.0, 0.0, 0.0, f"rejected: {exc!r}"))
            else:
                self._halt(f"order outcome unknown after {exc!r}; check the account")
                self._done.append(Fill(o, time.time(), 0.0, 0.0, 0.0, f"UNKNOWN: {exc!r}"))
            return
        if not resp.ok:
            self._done.append(Fill(o, time.time(), 0.0, 0.0, 0.0, f"{resp.code}: {resp.message}"))
            return
        making, taking = float(resp.making_amount), float(resp.taking_amount)
        usd, shares = (making, taking) if o.action == "BUY" else (taking, making)
        note = f"order {resp.order_id} {resp.status}"
        if resp.status != "matched" or shares <= 0:
            shares, usd, extra = await self._await_match(resp.order_id, o)
            note += extra
        if shares > 0 and not 0.0 < usd / shares <= 1.0:
            self._halt(f"order {resp.order_id}: implausible fill {shares} shares for ${usd}")
            note += " IMPLAUSIBLE AMOUNTS"
        price = usd / shares if shares else 0.0
        fee = shares * fee_per_share(price, *self.fee_lookup(o.window))
        self._done.append(Fill(o, time.time(), shares, usd, fee, note + " (fee estimated)"))

    async def _await_match(self, order_id: str, o: Order) -> tuple[float, float, str]:
        """A 'delayed' order is matched a moment later: ask what happened to it."""
        for _ in range(12):
            await asyncio.sleep(0.5)
            try:
                order = await self._client.get_order(order_id=order_id)
            except Exception:  # noqa: BLE001
                continue
            matched = float(order.size_matched)
            if matched > 0 or order.status.lower() not in ("live", "delayed"):
                # Average price is not reported here: book it at the limit (worst case).
                return matched, matched * o.limit, " (priced at limit)"
        self._halt(f"order {order_id} still unresolved after 6s; check the account")
        return 0.0, 0.0, " UNRESOLVED"


# ------------------------------------------------------------------- the loop
async def run(
    cfg: Config, *, live: bool, auth_prices: bool = False, log: Log = print,
    holder: list | None = None,
) -> Engine | None:
    """Run until cancelled. The engine is appended to `holder` as soon as it exists,
    so the caller can print a summary even after Ctrl+C."""
    from polymarket import AsyncPublicClient, AsyncSecureClient

    mode = "live" if live else "paper"
    journal = Journal(Path(cfg.data_dir) / mode)
    recorder = (Recorder(Path(cfg.data_dir) / "recordings", cfg.record_keep_days)
                if cfg.record else None)
    last_snap = 0
    q: asyncio.Queue = asyncio.Queue()
    secure = None
    tasks: list[asyncio.Task] = []
    async with AsyncPublicClient() as pub:
        try:
            if live or auth_prices:
                key = os.environ.get("POLYMARKET_PRIVATE_KEY")
                if not key:
                    raise SystemExit("set POLYMARKET_PRIVATE_KEY in the environment first")
                secure = await AsyncSecureClient.create(
                    private_key=key, wallet=os.environ.get("POLYMARKET_WALLET") or None
                )
            if live:
                broker = LiveBroker(secure, cfg, log)
            else:
                broker = PaperBroker(cfg.paper_latency_s)
            engine = Engine(cfg, broker, journal, log)
            if holder is not None:
                holder.append(engine)
            if live:
                def fees(start: int) -> tuple[float, float]:
                    w = engine.windows.get(start)
                    if w is None:
                        return cfg.default_fee_rate, cfg.default_fee_exponent
                    return w.fee_rate, w.fee_exp
                broker.fee_lookup = fees

            jobs = [
                (_auth_oracle_task(secure, q, log, cfg.feed_stall_s) if auth_prices
                 else _oracle_task(pub, q, log, cfg.feed_stall_s)),
                _window_manager(pub, q, cfg, log),
                _clock_task(q),
            ]
            if cfg.spot_lead:
                jobs.append(_spot_task(q, log))
            tasks = [asyncio.create_task(j) for j in jobs]

            def died(task: asyncio.Task) -> None:
                if not task.cancelled() and task.exception() is not None:
                    log(f"!! background task stopped: {task.exception()!r}. Restart the bot.")

            for task in tasks:
                task.add_done_callback(died)
            log(f"running in {mode.upper()} mode; bankroll {cfg.bankroll:.2f}, "
                f"max {cfg.max_window_usd:.2f}/window, stop after {cfg.daily_loss_limit:.2f} "
                f"daily loss. Ctrl+C to stop.")
            last_px = time.time()
            while True:
                ev = await q.get()
                if ev["t"] == "px" and ev["src"] == "chainlink":
                    last_px = ev["ts"]
                elif ev["t"] == "clock" and ev["ts"] - last_px > 60:
                    last_px = ev["ts"]
                    log("!! no oracle prices for 60s. The public price stream may be "
                        "unavailable; try --auth-prices (needs POLYMARKET_PRIVATE_KEY).")
                engine.on_event(ev)
                if recorder:
                    # Compact recording: every price tick, plus one top-of-book
                    # snapshot per second instead of every raw book update.
                    if ev["t"] in ("px", "win", "resolved"):
                        recorder.write(ev)
                    elif ev["t"] == "clock" and int(ev["ts"]) != last_snap:
                        last_snap = int(ev["ts"])
                        for snap in engine.book_snapshots(cfg.record_depth):
                            recorder.write(snap)
        except asyncio.CancelledError:
            return holder[0] if holder else None
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if recorder:
                recorder.close()
            if secure is not None:
                await secure.close()
    return engine
