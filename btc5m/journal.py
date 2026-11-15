"""Files the bot leaves behind: a trade log, a per-window log, and a raw event
recording that `backtest` can replay."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Journal:
    TRADE_COLS = [
        "time", "window", "side", "action", "shares", "price", "notional", "fee",
        "limit", "p_fair", "p_used", "edge", "note",
    ]
    WINDOW_COLS = [
        "start", "slug", "strike", "final", "winner", "buys", "misses", "spent",
        "fees", "pnl", "bankroll", "note",
    ]

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _append(self, name: str, cols: list[str], row: dict) -> None:
        path = self.dir / name
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            if new:
                wr.writeheader()
            wr.writerow(row)

    def trade(self, now: float, window, fill) -> None:
        o = fill.order
        self._append("trades.csv", self.TRADE_COLS, {
            "time": _iso(now), "window": _iso(window.start), "side": o.side,
            "action": o.action, "shares": round(fill.shares, 4),
            "price": round(fill.price, 4), "notional": round(fill.notional, 4),
            "fee": round(fill.fee, 5), "limit": o.limit, "p_fair": round(o.p_fair, 4),
            "p_used": round(o.p_used, 4), "edge": round(o.edge, 4), "note": fill.note,
        })

    def window(self, res: dict) -> None:
        self._append("windows.csv", self.WINDOW_COLS, {**res, "start": _iso(res["start"])})


class Recorder:
    """Append every event as one JSON line, one file per UTC day."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._day = ""
        self._fh = None

    def write(self, ev: dict) -> None:
        day = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y%m%d")
        if day != self._day:
            self.close()
            self._day = day
            self._fh = (self.dir / f"events-{day}.jsonl").open("a", encoding="utf-8")
        self._fh.write(json.dumps(ev, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def read_events(paths: list[str | Path]) -> Iterator[dict]:
    for path in sorted(Path(p) for p in paths):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
