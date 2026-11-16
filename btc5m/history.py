"""Replay the bot against real history: real trades, real winners.

For every past 5-minute market this downloads, from public endpoints:
  - the market itself (tokens, fees, the official start price, end price and
    winner),
  - every taker trade that actually happened in it (time, price, size),
and a one-second BTC price series to stand in for the oracle feed.

The replay then gives the bot's normal engine one look per second at the prices
that really traded in that second, and lets it buy at most what really traded.
Winners are the official ones.

What this can and cannot show. Each real trade proves that amount was
available at that price at that moment, so prices and sizes are real. What is
assumed: that the quote was already resting when the bot decided (0.6 s
earlier), and that the bot would have got there ahead of whoever really took
it. The honest way to read the result is by `feed lag`: how old the bot's price
information is allowed to be. An edge that survives a 2-3 second handicap is
a pricing edge; one that only exists at 0 is a speed race the bot may not win.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import heapq
import json
import time
import urllib.error
import urllib.request
from array import array
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import analysis
from .broker import PaperBroker
from .config import WINDOW_S, Config
from .engine import Engine
from .journal import Journal
from .strategy import DOWN, UP

GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{start}"
KLINES = ("https://data-api.binance.vision/api/v3/klines"
          "?symbol=BTCUSDT&interval=1s&limit=1000&startTime={ms}")
INDEX_COLS = ["start", "winner", "strike", "final", "prints", "volume"]
MAX_WAIT_S = 0.9      # decision-to-fill wait the one-second trade data can represent
Log = Callable[[str], None]


def _get_json(url: str, tries: int = 5):
    req = urllib.request.Request(url, headers={"User-Agent": "btc5m/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


# ------------------------------------------------------------------- prices
class Prices:
    """Price known at each whole second, forward-filled across gaps."""

    def __init__(self, t0: int, values: array) -> None:
        self.t0, self.values = t0, values

    @property
    def end(self) -> int:
        return self.t0 + len(self.values) - 1

    def at(self, ts: int) -> float | None:
        i = ts - self.t0
        return self.values[i] if 0 <= i < len(self.values) else None

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[int, float]]) -> Prices:
        values = array("d")
        t0 = last_t = None
        for ts, price in pairs:
            if t0 is None:
                t0 = ts
            elif ts <= last_t:
                continue
            else:
                values.extend([values[-1]] * (ts - last_t - 1))
            values.append(price)
            last_t = ts
        return cls(t0 or 0, values)

    @classmethod
    def load(cls, path: str | Path) -> Prices:
        def rows() -> Iterator[tuple[int, float]]:
            with Path(path).open(encoding="utf-8") as fh:
                rd = csv.reader(fh)
                next(rd, None)
                for r in rd:
                    if len(r) == 2:
                        yield int(r[0]), float(r[1])
        return cls.from_pairs(rows())


def kline_rows(klines: list) -> list[tuple[int, float]]:
    """A candle that opens at second o closes at o+1: its close is the price
    known at boundary o+1."""
    return [(int(k[0]) // 1000 + 1, float(k[4])) for k in klines]


def fetch_prices(start: int, end: int, path: str | Path, log: Log = print) -> None:
    """Append one-second BTC prices for [start, end] to `path`; resumes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    have = Prices.load(path) if path.exists() else None
    if have and have.values and not have.t0 <= start <= have.end + 1:
        path.unlink()                      # a different period: start over
        have = None
    t = have.end + 1 if have and have.values else start
    new = not path.exists()
    pages = 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        if new:
            wr.writerow(["ts", "price"])
        while t <= end:
            rows = kline_rows(_get_json(KLINES.format(ms=(t - 1) * 1000)))
            rows = [r for r in rows if t <= r[0] <= end]
            if not rows:
                break
            wr.writerows(rows)
            t = rows[-1][0] + 1
            pages += 1
            if pages % 100 == 0:
                log(f"  prices: back-filled to {_iso(t)}")
            time.sleep(0.05)


# ------------------------------------------------------------------ markets
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _listed(value) -> list:
    return json.loads(value) if isinstance(value, str) else list(value or [])


def parse_market(payload, start: int, cfg: Config) -> dict | None:
    """Reduce the exchange's description of one past market to what the replay
    needs. None if it is missing or has no official winner yet."""
    ev = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(ev, dict) or not ev.get("markets"):
        return None
    m = ev["markets"][0]
    names = [str(x).strip().lower() for x in _listed(m.get("outcomes"))]
    tokens = [str(x) for x in _listed(m.get("clobTokenIds"))]
    settle = [float(x) for x in _listed(m.get("outcomePrices"))]
    if sorted(names) != ["down", "up"] or len(tokens) != 2 or len(settle) != 2:
        return None
    up_i, down_i = names.index("up"), names.index("down")
    if not m.get("closed") or max(settle) < 0.99 or min(settle) > 0.01:
        return None                        # not settled (or settled 50/50): skip
    meta = ev.get("eventMetadata") or {}
    fees = m.get("feeSchedule") or {}
    return {
        "start": start, "slug": ev.get("slug", ""), "cid": m.get("conditionId", ""),
        "up": tokens[up_i], "down": tokens[down_i],
        "winner": UP if settle[up_i] > 0.5 else DOWN,
        "strike": meta.get("priceToBeat"), "final": meta.get("finalPrice"),
        "fee_rate": float(fees.get("rate", cfg.default_fee_rate)),
        "fee_exp": float(fees.get("exponent", cfg.default_fee_exponent)),
        "min_size": float(m.get("orderMinSize") or 5.0),
        "taker_delay_s": float(m.get("secondsDelay") or 0.0),
        "volume": round(float(m.get("volumeNum") or m.get("volume") or 0.0), 2),
    }


def add_print(agg: dict, win: dict, ts: float, token: str, side: str, price: float,
              size: float) -> None:
    """Fold one real taker trade into per-second totals.

    Every trade is stored as an Up price `u` and a direction: 1 if the taker
    ended up long Up (bought Up, or sold Down), else 0. Because the two tokens
    share one book, a direction-1 trade shows an Up ask at u and a Down bid at
    1-u; a direction-0 trade shows an Up bid at u and a Down ask at 1-u.
    """
    sec = int(ts) - win["start"]
    if not 0 <= sec < WINDOW_S or size <= 0 or not 0 < price < 1:
        return
    is_up = token == win["up"]
    if not is_up and token != win["down"]:
        return
    buy = side.upper().endswith("BUY")
    key = (sec, 1 if is_up == buy else 0, round((price if is_up else 1.0 - price) * 1000))
    agg[key] = agg.get(key, 0.0) + size


async def _fetch_window(pub, start: int, cfg: Config) -> dict | None:
    win = parse_market(await asyncio.to_thread(_get_json, GAMMA.format(start=start)), start, cfg)
    if win is None:
        return None
    agg: dict = {}
    async for page in pub.list_trades(condition_id=win["cid"], taker_only=True, page_size=1000):
        for tr in page.items:
            add_print(agg, win, tr.timestamp.timestamp(), str(tr.asset_id), str(tr.side),
                      float(tr.price), float(tr.size))
        if getattr(page, "limit_reached", False):
            win["truncated"] = True
    win["prints"] = [[s, d, u, round(size, 2)] for (s, d, u), size in sorted(agg.items())]
    return win


def read_index(directory: str | Path) -> dict[int, dict]:
    path = Path(directory) / "index.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {int(r["start"]): r for r in csv.DictReader(fh)}


async def fetch_windows(starts: list[int], directory: str | Path, cfg: Config,
                        log: Log = print, concurrency: int = 4) -> None:
    """Download markets and their trades into `directory`; skips what is there."""
    from polymarket import AsyncPublicClient

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    done = read_index(directory)
    todo = [s for s in starts if s not in done]
    log(f"markets: {len(starts) - len(todo)} already downloaded, {len(todo)} to fetch")
    index = directory / "index.csv"
    new_index = not index.exists()
    failed = skipped = 0

    async def one(pub, start: int):
        for attempt in range(3):
            try:
                return await _fetch_window(pub, start, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network or rate limit: retry
                err = exc
                await asyncio.sleep(3.0 * (attempt + 1))
        return err

    async with AsyncPublicClient() as pub:
        with index.open("a", newline="", encoding="utf-8") as ifh:
            iw = csv.DictWriter(ifh, fieldnames=INDEX_COLS)
            if new_index:
                iw.writeheader()
            for i in range(0, len(todo), concurrency):
                batch = todo[i:i + concurrency]
                for start, win in zip(batch, await asyncio.gather(*(one(pub, s) for s in batch))):
                    if isinstance(win, Exception):
                        failed += 1
                        log(f"  !! {_iso(start)} failed: {win!r}")
                        continue
                    if win is None:
                        skipped += 1
                        continue
                    day = datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%d")
                    with gzip.open(directory / f"windows-{day}.jsonl.gz", "at",
                                   encoding="utf-8") as fh:
                        fh.write(json.dumps(win, separators=(",", ":")) + "\n")
                    iw.writerow({"start": start, "winner": win["winner"],
                                 "strike": win["strike"], "final": win["final"],
                                 "prints": len(win["prints"]), "volume": win["volume"]})
                ifh.flush()
                if (i // concurrency) % 25 == 24:
                    log(f"  markets: {i + len(batch)}/{len(todo)} (up to {_iso(batch[-1])})")
    log(f"markets: finished; {failed} failed, {skipped} had no official result")


def read_windows(directory: str | Path, lo: int, hi: int) -> Iterator[dict]:
    """Stored markets with lo <= start < hi, oldest first, one at a time (a
    week of trades does not fit in a small server's memory at once). A market
    stored out of order, by a later retry, is left out."""
    last = -1
    for path in sorted(Path(directory).glob("windows-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if lo <= row["start"] < hi and row["start"] > last:
                        last = row["start"]
                        yield row
        except (EOFError, OSError):
            continue                        # cut off mid-write: keep what was read


# ------------------------------------------------------------------- replay
def proxy_check(index: Iterable[dict], prices: Prices, shifts=(-3, -2, -1, 0)) -> list[dict]:
    """How well the stand-in price series reproduces the official results, for
    each candidate time shift (official price at t ~ stand-in at t + shift)."""
    rows = [r for r in index if r.get("strike") not in (None, "", "None")
            and r.get("final") not in (None, "", "None")]
    out = []
    for shift in shifts:
        errs, agree, n = [], 0, 0
        for r in rows:
            s = int(r["start"])
            a, b = prices.at(s + shift), prices.at(s + WINDOW_S + shift)
            if a is None or b is None:
                continue
            real = float(r["final"]) - float(r["strike"])
            errs.append(abs(real - (b - a)))
            agree += (UP if b >= a else DOWN) == r["winner"]
            n += 1
        if n:
            errs.sort()
            out.append({"shift_s": shift, "windows": n, "median_abs_error": round(errs[n // 2], 2),
                        "p90_abs_error": round(errs[int(n * 0.9)], 2),
                        "winner_agreement": round(agree / n, 4)})
    return out


def _books(win: dict, prints: list, ts: float) -> tuple[dict, dict]:
    up_asks, up_bids, dn_asks, dn_bids = [], [], [], []
    for _sec, direction, u, size in prints:
        p = u / 1000.0
        q = round(1.0 - p, 4)
        if direction:
            up_asks.append([p, size])
            dn_bids.append([q, size])
        else:
            up_bids.append([p, size])
            dn_asks.append([q, size])
    return ({"t": "book", "ts": ts, "tok": win["up"], "bids": up_bids, "asks": up_asks},
            {"t": "book", "ts": ts, "tok": win["down"], "bids": dn_bids, "asks": dn_asks})


def market_events(windows: Iterable[dict], cfg: Config) -> Iterator[tuple[dict, bool]]:
    """(event, may_decide) pairs for the stored markets, in time order.

    Trades stamped second t are treated as happening at t+0.5. Their prices
    appear to the bot one decision-to-fill wait earlier, it gets a single
    decision, the fill is checked at t+0.5 and the quotes then disappear.
    """
    pending: dict | None = None            # previous window's result announcement
    for win in windows:
        start, end = win["start"], win["start"] + WINDOW_S
        opening = {
            "t": "win", "ts": start - 0.4, "start": start, "end": end, "slug": win["slug"],
            "up": win["up"], "down": win["down"], "fee_rate": win["fee_rate"],
            "fee_exp": win["fee_exp"], "min_size": win["min_size"],
            "taker_delay_s": win.get("taker_delay_s", 0.0),
        }
        if pending and pending["ts"] <= opening["ts"]:
            yield pending, False
            pending = None
        yield opening, False
        if pending:
            yield pending, False
        wait = min(cfg.paper_latency_s + win.get("taker_delay_s", 0.0), MAX_WAIT_S)
        by_sec: dict[int, list] = {}
        for p in win["prints"]:
            by_sec.setdefault(p[0], []).append(p)
        for sec in sorted(by_sec):
            if sec < 1:
                continue                   # would overlap the previous market's result
            fill_at = start + sec + 0.5
            seen_at = fill_at - wait - 0.01
            up, down = _books(win, by_sec[sec], seen_at)
            yield up, False
            yield down, True
            yield {"t": "clock", "ts": fill_at}, False
            gone = fill_at + 0.001
            yield {"t": "book", "ts": gone, "tok": win["up"], "bids": [], "asks": []}, False
            yield {"t": "book", "ts": gone, "tok": win["down"], "bids": [], "asks": []}, False
        # Announced just before the closing price tick so the official winner,
        # not the stand-in series, settles the market. Nothing trades this late.
        pending = {"t": "resolved", "ts": end - 0.2, "tok": win[win["winner"].lower()]}
    if pending:
        yield pending, False


def price_events(prices: Prices, lo: int, hi: int, feed_lag_s: float,
                 shift_s: int) -> Iterator[tuple[dict, bool]]:
    """The stand-in oracle feed: the value stamped second b arrives feed_lag_s later."""
    for b in range(lo, hi + 1):
        p = prices.at(b + shift_s)
        if p is not None:
            yield {"t": "px", "ts": b + feed_lag_s, "src": "chainlink", "p": p,
                   "pts": float(b)}, False


class _ReplayEngine(Engine):
    may_decide = True

    def _maybe_trade(self) -> None:
        if self.may_decide:
            super()._maybe_trade()


def run_replay(cfg: Config, windows: Iterable[dict], prices: Prices, lo: int, hi: int, *,
               feed_lag_s: float, shift_s: int = 0, journal: Journal | None = None,
               log: Log = lambda _m: None) -> Engine:
    engine = _ReplayEngine(cfg, PaperBroker(cfg.paper_latency_s), journal, log=log)
    stream = heapq.merge(
        price_events(prices, lo, hi, feed_lag_s, shift_s), market_events(windows, cfg),
        key=lambda pair: pair[0]["ts"],
    )
    for ev, may_decide in stream:
        engine.may_decide = may_decide
        engine.on_event(ev)
    return engine


# ---------------------------------------------------------------------- CLI
def run(cfg: Config, days: float, lags: list[float], *, fetch: bool = True,
        replay: bool = True, log: Log = print) -> list[dict]:
    from dataclasses import replace

    base = Path(cfg.data_dir)
    store = base / "history"
    hi = int(time.time() - 900) // WINDOW_S * WINDOW_S     # leave time for results to post
    lo = hi - int(days * 86400) // WINDOW_S * WINDOW_S
    warmup = lo - 3600
    log(f"period: {_iso(lo)} to {_iso(hi)} UTC ({(hi - lo) // WINDOW_S} markets)")
    if fetch:
        fetch_prices(warmup, hi + 10, store / "prices.csv", log)
        asyncio.run(fetch_windows(list(range(lo, hi, WINDOW_S)), store, cfg, log))
    if not replay:
        return []
    prices = Prices.load(store / "prices.csv")
    index = [r for s, r in sorted(read_index(store).items()) if lo <= s < hi]
    if not index or not prices.values:
        log("nothing downloaded for this period")
        return []
    checks = proxy_check(index, prices)
    log("\nstand-in price series versus official results (move over each market):")
    for c in checks:
        log(f"  shift {c['shift_s']:+d}s: median error ${c['median_abs_error']}, 9 in 10 within "
            f"${c['p90_abs_error']}, same winner {c['winner_agreement'] * 100:.1f}% "
            f"of {c['windows']}")
    shift = min(checks, key=lambda c: c["median_abs_error"])["shift_s"] if checks else 0
    log(f"using shift {shift:+d}s")
    ups = sum(r["winner"] == UP for r in index)
    log(f"official winners: UP {ups}, DOWN {len(index) - ups}")
    results = []
    quiet = replace(cfg, status_interval_s=0.0, kill_file="")
    for lag in lags:
        mode = f"history-lag{lag:g}"
        out = base / mode
        for name in ("trades.csv", "windows.csv"):
            (out / name).unlink(missing_ok=True)
        t = time.time()
        run_replay(quiet, read_windows(store, lo, hi), prices, warmup, hi + 10,
                   feed_lag_s=lag, shift_s=shift, journal=Journal(out))
        rows, trades = analysis.load(out)
        s = analysis.summary(rows) if rows else {}
        d = analysis.detail(rows, trades) if rows else {}
        s.update(feed_lag_s=lag, mode=mode, buys=d.get("buys", 0),
                 missed_orders=d.get("missed_orders", 0))
        results.append(s)
        log(f"\n=== feed lag {lag:g}s: price information about {lag + 1.5 - shift:g}s old when "
            f"the real trade happened ({time.time() - t:.0f}s to run) ===")
        log(analysis.render(out))
    return results
