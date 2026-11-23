"""`python -m btc5m pairs`: paper-trade the nested 5m/15m Bitcoin pair on live data.

Places no orders. Streams the oracle (for the starting prices), reads the four
order books once a second during the last 5 minutes of every 15-minute market,
makes paper buys with `pairs.PairPaper`, and settles them with the official
results. Files in OUT (default data/pairs):
  books-YYYYMMDD.csv   snapshots where either pairing cost <= 1.05
  trades.csv           paper buys
  results.csv          settled paper buys, with official and computed strikes
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .history import GAMMA, _get_json, parse_market
from .pairs import PairPaper, TickBuffer, choose_pair, pair_quote

LOG_BELOW = 1.05


def _append(path: Path, row: list, header: list) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerow(row)


def _best_ask(book) -> tuple[float | None, float]:
    if not book.asks:
        return None, 0.0
    lvl = min(book.asks, key=lambda l: float(l.price))
    return float(lvl.price), float(lvl.size)


async def _tokens(slug: str) -> dict | None:
    for _ in range(30):
        try:
            payload = await asyncio.to_thread(_get_json, GAMMA.format(slug=slug), 2)
            ev = payload[0] if isinstance(payload, list) and payload else None
            if ev and ev.get("markets"):
                m = ev["markets"][0]
                names = [str(x).lower() for x in json.loads(m["outcomes"])]
                toks = [str(x) for x in json.loads(m["clobTokenIds"])]
                return {"up": toks[names.index("up")], "down": toks[names.index("down")]}
        except Exception:  # noqa: BLE001 - not listed yet or a network blip
            pass
        await asyncio.sleep(2)
    return None


async def _settle(pp: PairPaper, s15: int, est: dict, out: Path, log) -> None:
    await asyncio.sleep(max(0.0, s15 + 900 + 150 - time.time()))
    for _ in range(20):
        try:
            p15 = await asyncio.to_thread(_get_json, GAMMA.format(slug=f"btc-updown-15m-{s15}"), 2)
            p5 = await asyncio.to_thread(_get_json, GAMMA.format(slug=f"btc-updown-5m-{s15 + 600}"), 2)
            w15 = parse_market(p15, s15, Config(), 900)
            w5 = parse_market(p5, s15 + 600, Config(), 300)
        except Exception:  # noqa: BLE001
            w15 = w5 = None
        if w15 and w5:
            _append(out / "settled.csv",
                    [s15, w5["strike"], w15["strike"], w5["winner"], w15["winner"],
                     w5["final"], w15["final"], est.get("k5"), est.get("k15")],
                    ["start15", "strike5", "strike15", "winner5", "winner15", "final5", "final15",
                     "k5_est", "k15_est"])
            for t in pp.settle(s15, w5["winner"], w15["winner"]):
                official = choose_pair(float(w5["strike"]), float(w15["strike"]), 0.0)
                _append(out / "results.csv",
                        [s15, t.pair, official, t.shares, round(t.cost, 4), t.payoff,
                         round(t.pnl, 4)],
                        ["start15", "pair", "official_pair", "shares", "cost", "payoff", "pnl"])
                total = sum(x.pnl for x in pp.trades if x.pnl is not None)
                log(f"SETTLE pair {s15}: {t.pair} paid {t.payoff:.0f}/pair, pnl {t.pnl:+.2f}, "
                    f"total {total:+.2f} on {sum(1 for x in pp.trades if x.pnl is not None)} trades")
            return
        await asyncio.sleep(30)


async def run_pairs(out: Path, pp: PairPaper, log=print) -> None:
    from polymarket import AsyncPublicClient

    from .polymarket_io import _oracle_task

    out.mkdir(parents=True, exist_ok=True)
    buf = TickBuffer()
    q: asyncio.Queue = asyncio.Queue()
    done: set[int] = set()

    async def feed() -> None:
        while True:
            ev = await q.get()
            if ev.get("t") == "px":
                buf.add(float(ev["pts"]), float(ev["p"]))

    async with AsyncPublicClient() as pub:
        tasks = [asyncio.create_task(_oracle_task(pub, q, log)), asyncio.create_task(feed())]
        log(f"pairs: paper only; buy when the pair costs <= {pp.max_cost} incl. fees, "
            f"${pp.usd:g} per trade, skip strikes within ${pp.strike_margin:g}")
        try:
            while True:
                now = time.time()
                s15 = int(now // 900 * 900)
                if now - s15 < 600 or s15 in done:
                    await asyncio.sleep(1.0)
                    continue
                done.add(s15)
                m5 = await _tokens(f"btc-updown-5m-{s15 + 600}")
                m15 = await _tokens(f"btc-updown-15m-{s15}")
                if not m5 or not m15:
                    log(f"pairs {s15}: markets not found")
                    continue
                est: dict = {}
                asyncio.create_task(_settle(pp, s15, est, out, log))
                ids = {m5["up"]: ("5m", "up"), m5["down"]: ("5m", "down"),
                       m15["up"]: ("15m", "up"), m15["down"]: ("15m", "down")}
                path = out / f"books-{datetime.fromtimestamp(s15, timezone.utc):%Y%m%d}.csv"
                seen = logged = 0
                while time.time() < s15 + 900 - 2:
                    t = time.time()
                    try:
                        books = await pub.get_order_books(token_ids=list(ids))
                    except Exception as exc:  # noqa: BLE001
                        log(f"pairs books: {exc!r}")
                        await asyncio.sleep(1.0)
                        continue
                    asks = {ids[str(b.asset_id)]: _best_ask(b) for b in books if str(b.asset_id) in ids}
                    if len(asks) < 4:
                        await asyncio.sleep(1.0)
                        continue
                    k15, k5 = buf.twap(s15), buf.twap(s15 + 600)
                    est.update(k5=k5, k15=k15)
                    seen += 1
                    ca, sa, _, _ = pair_quote(asks, "a")
                    cb, sb, _, _ = pair_quote(asks, "b")
                    if min(ca, cb) <= LOG_BELOW:
                        logged += 1
                        _append(path, [round(t, 2), s15, round(t - s15 - 600, 1), round(ca, 4), sa,
                                       round(cb, 4), sb, k5, k15],
                                ["ts", "start15", "sec5", "cost_a", "size_a", "cost_b", "size_b",
                                 "k5_est", "k15_est"])
                    trade = pp.on_snapshot(t, s15, asks, k5, k15)
                    if trade:
                        _append(out / "trades.csv",
                                [round(t, 2), s15, trade.pair, trade.shares, trade.p5, trade.p15,
                                 round(trade.cost, 4), k5, k15],
                                ["ts", "start15", "pair", "shares", "p5", "p15", "cost", "k5_est",
                                 "k15_est"])
                        log(f"PAPER BUY pair {s15}: {trade.pair} {trade.shares:.2f} at "
                            f"{trade.p5:.3f} + {trade.p15:.3f} (cost {trade.cost:.4f}/pair)")
                    await asyncio.sleep(max(0.0, 1.0 - (time.time() - t)))
                k5_txt = f"{k5:.2f}" if k5 else "?"
                k15_txt = f"{k15:.2f}" if k15 else "?"
                log(f"{datetime.fromtimestamp(s15, timezone.utc):%m-%d %H:%M} pair: {seen} snapshots, "
                    f"{logged} <= {LOG_BELOW}; strikes est 5m {k5_txt} 15m {k15_txt}")
        finally:
            for task in tasks:
                task.cancel()
