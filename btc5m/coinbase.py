"""Download recent BTC-USD trades from Coinbase's public API and reduce them to a
one-price-per-second series. Used for calibration and the offline simulator;
no account or key involved."""

from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

URL = "https://api.exchange.coinbase.com/products/BTC-USD/trades?limit=1000"


def _get(url: str) -> tuple[list[dict], str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "btc5m/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp), resp.headers.get("cb-after")
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 5:
                raise
            wait = 2.0 * (attempt + 1)
            print(f"  request failed ({exc}); retrying in {wait:.0f}s")
            time.sleep(wait)
    raise AssertionError("unreachable")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fetch_seconds(hours: float, log=print) -> list[tuple[int, float]]:
    """Last trade price at each whole second for the past `hours`, forward-filled."""
    last: dict[int, tuple[float, float]] = {}   # second -> (trade time, price)
    cursor: str | None = None
    newest = oldest = None
    target = None
    pages = 0
    while True:
        trades, after = _get(URL + (f"&after={cursor}" if cursor else ""))
        if not trades:
            break
        pages += 1
        for tr in trades:
            t = _epoch(tr["time"])
            sec = int(t) + 1          # the price known *at* second boundary `sec`
            if sec not in last or t > last[sec][0]:
                last[sec] = (t, float(tr["price"]))
        page_new, page_old = _epoch(trades[0]["time"]), _epoch(trades[-1]["time"])
        newest = page_new if newest is None else newest
        oldest = page_old
        target = target or newest - hours * 3600
        if pages % 25 == 0:
            log(f"  {pages} pages, back to {datetime.fromtimestamp(oldest).isoformat(timespec='seconds')}")
        if oldest <= target or not after:
            break
        cursor = after
        time.sleep(0.12)
    if not last:
        return []
    out: list[tuple[int, float]] = []
    price = None
    for sec in range(min(last), max(last) + 1):
        if sec in last:
            price = last[sec][1]
        if price is not None:
            out.append((sec, price))
    return out


def save_series(series: list[tuple[int, float]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["ts", "price"])
        wr.writerows(series)


def load_series(path: str | Path) -> list[tuple[int, float]]:
    with Path(path).open(encoding="utf-8") as fh:
        rd = csv.reader(fh)
        next(rd)
        return [(int(r[0]), float(r[1])) for r in rd if r]
