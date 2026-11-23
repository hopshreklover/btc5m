"""`python -m btc5m pairs`: paper-trade the nested 5m/15m pairs on live data.

Places no orders. For each coin (default Bitcoin, Ethereum, Solana, XRP) it
streams the coin's oracle price (for the starting prices), reads the four order
books once a second during the last 5 minutes of every 15-minute market, makes
paper buys with `pairs.PairPaper`, and settles them with the official results.
Files in OUT/<coin>/ (default data/pairs/<coin>/):
  books-YYYYMMDD.csv   snapshots where the safe pairing cost <= 1.01
  trades.csv           paper buys
  settled.csv          official strikes and winners, with the bot's own estimates
  results.csv          settled paper buys
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
from .pairs import PairPaper, PairTrade, TickBuffer, choose_pair, pair_quote

LOG_BELOW = 1.01      # log the safe pairing (by the estimated strikes) when it costs this or less;
                      # the other pairing is always cheap (both together are all four tokens, ~$2)


def _append(path: Path, row: list, header: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


async def _oracle(pub, bufs: dict, log) -> None:
    """One subscription for every coin's oracle price; ticks go to that coin's buffer."""
    from polymarket.streams import CryptoPricesSpec

    from .polymarket_io import _events, _Retry

    symbols = {f"{c}/usd": c for c in bufs}
    retry = _Retry(log, "pairs oracle stream")
    while True:
        delay = 2.0
        try:
            handle = await pub.subscribe(CryptoPricesSpec(topic="prices.crypto.chainlink",
                                                          symbols=list(symbols)))
            try:
                async for ev in _events(handle, 30.0):
                    p = ev.payload
                    coin = symbols.get(str(p.symbol).lower())
                    if coin:
                        retry.ok()
                        bufs[coin].add(p.timestamp / 1000.0, float(p.value))
            finally:
                await handle.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the feed alive
            delay = retry.failed(exc)
        await asyncio.sleep(delay)


def _rows(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _num(x) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def resume(coin_dir: Path, pp: PairPaper, now: float, lookback_s: float = 6 * 3600) -> dict[int, dict]:
    """After a restart: put back paper buys that have no result yet, and return the
    recent markets (start -> estimated strikes) that still need settling."""
    settled = {int(r["start15"]) for r in _rows(coin_dir / "settled.csv")}
    done = {(int(r["start15"]), r["pair"]) for r in _rows(coin_dir / "results.csv")}
    todo: dict[int, dict] = {}
    for r in _rows(coin_dir / "trades.csv"):
        s15 = int(r["start15"])
        if (s15, r["pair"]) in done or s15 in settled:
            continue
        pp.restore(PairTrade(s15, r["pair"], float(r["shares"]), float(r["p5"]), float(r["p15"]),
                             float(r["cost"]), float(r["ts"])))
        todo.setdefault(s15, {"k5": _num(r.get("k5_est")), "k15": _num(r.get("k15_est"))})
    for path in sorted(coin_dir.glob("books-*.csv"))[-2:]:
        for r in _rows(path):
            s15 = int(r["start15"])
            if s15 in settled or now - s15 > lookback_s or s15 + 900 > now:
                continue
            todo[s15] = {"k5": _num(r.get("k5_est")), "k15": _num(r.get("k15_est"))}
    return todo


async def _settle(coin: str, pp: PairPaper, s15: int, est: dict, out: Path, log) -> None:
    await asyncio.sleep(max(0.0, s15 + 900 + 150 - time.time()))
    for _ in range(20):
        try:
            p15 = await asyncio.to_thread(_get_json, GAMMA.format(slug=f"{coin}-updown-15m-{s15}"), 2)
            p5 = await asyncio.to_thread(_get_json, GAMMA.format(slug=f"{coin}-updown-5m-{s15 + 600}"), 2)
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
                        [s15, t.pair, official, t.shares, round(t.cost, 4), t.payoff, round(t.pnl, 4)],
                        ["start15", "pair", "official_pair", "shares", "cost", "payoff", "pnl"])
                done = [x for x in pp.trades if x.pnl is not None]
                log(f"SETTLE {coin} {s15}: pair {t.pair} paid {t.payoff:.0f}, pnl {t.pnl:+.2f}; "
                    f"{coin} total {sum(x.pnl for x in done):+.2f} on {len(done)} trades")
            return
        await asyncio.sleep(30)


async def _coin_loop(pub, coin: str, pp: PairPaper, buf: TickBuffer, out: Path, log) -> None:
    done: set[int] = set()
    while True:
        now = time.time()
        s15 = int(now // 900 * 900)
        if now - s15 < 600 or s15 in done:
            await asyncio.sleep(1.0)
            continue
        done.add(s15)
        m5 = await _tokens(f"{coin}-updown-5m-{s15 + 600}")
        m15 = await _tokens(f"{coin}-updown-15m-{s15}")
        if not m5 or not m15:
            log(f"pairs {coin} {s15}: markets not found")
            continue
        est: dict = {}
        asyncio.create_task(_settle(coin, pp, s15, est, out, log))
        ids = {m5["up"]: ("5m", "up"), m5["down"]: ("5m", "down"),
               m15["up"]: ("15m", "up"), m15["down"]: ("15m", "down")}
        path = out / f"books-{datetime.fromtimestamp(s15, timezone.utc):%Y%m%d}.csv"
        seen = logged = 0
        k5 = k15 = None
        while time.time() < s15 + 900 - 2:
            t = time.time()
            try:
                books = await pub.get_order_books(token_ids=list(ids))
            except Exception as exc:  # noqa: BLE001
                log(f"pairs {coin} books: {exc!r}")
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
            safe = choose_pair(k5, k15, (k15 or 0.0) * pp.strike_margin_bp / 1e4)
            if safe and (ca if safe == "a" else cb) <= LOG_BELOW:
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
                        ["ts", "start15", "pair", "shares", "p5", "p15", "cost", "k5_est", "k15_est"])
                log(f"PAPER BUY {coin} {s15}: pair {trade.pair} {trade.shares:.2f} at "
                    f"{trade.p5:.3f} + {trade.p15:.3f} (cost {trade.cost:.4f}/pair)")
            await asyncio.sleep(max(0.0, 1.0 - (time.time() - t)))
        log(f"{datetime.fromtimestamp(s15, timezone.utc):%m-%d %H:%M} {coin}: {seen} snapshots, "
            f"{logged} <= {LOG_BELOW}; strikes est 5m {k5} 15m {k15}")


async def run_pairs(out: Path, papers: dict[str, PairPaper], log=print) -> None:
    from polymarket import AsyncPublicClient

    bufs = {c: TickBuffer() for c in papers}
    async with AsyncPublicClient() as pub:
        tasks = [asyncio.create_task(_oracle(pub, bufs, log))]
        for c, pp in papers.items():
            todo = resume(out / c, pp, time.time())
            if todo:
                log(f"pairs {c}: resuming {len(todo)} unsettled market(s), "
                    f"{sum(1 for t in pp.trades if t.payoff is None)} open paper trade(s)")
            for s15, est in todo.items():
                tasks.append(asyncio.create_task(_settle(c, pp, s15, est, out / c, log)))
        tasks += [asyncio.create_task(_coin_loop(pub, c, pp, bufs[c], out / c, log))
                  for c, pp in papers.items()]
        any_pp = next(iter(papers.values()))
        log(f"pairs: paper only, coins {', '.join(papers)}; buy when a pair costs <= "
            f"{any_pp.max_cost} incl. fees, ${any_pp.usd:g} per trade, skip strikes within "
            f"{any_pp.strike_margin_bp:g} bp")
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
