"""Is it holding up? A daily check against real results.

`python -m btc5m check --days 14`:
  1. downloads the latest markets (their real trades and official winners) and
     one-second prices into data/history-rolling/ (resumable; old days pruned);
  2. replays the configured strategy over those days, exactly as `history` does;
  3. re-settles the paper bot's own log with the official winners;
  4. writes data/check.json, which the dashboard shows day by day.

Meant to run once a day from a timer. Nothing here can place an order.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import analysis
from .config import WINDOW_S, Config
from .history import (Log, Prices, _iso, fetch_prices, fetch_windows, read_index, read_windows,
                      run_replay)
from .journal import Journal
from .strategy import DOWN, UP

TOUCH = re.compile(r"touch-only (\d+) pnl ([-+0-9.]+)")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def prune(store: Path, keep_from: int) -> None:
    """Drop stored markets and prices older than keep_from (an epoch second)."""
    cutoff_day = _day(keep_from).replace("-", "")
    for path in store.glob("windows-*.jsonl.gz"):
        if path.name[8:16] < cutoff_day:
            path.unlink()
    index = store / "index.csv"
    if index.exists():
        rows = [r for s, r in sorted(read_index(store).items()) if s >= keep_from]
        if rows:
            with index.open("w", newline="", encoding="utf-8") as fh:
                wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
                wr.writeheader()
                wr.writerows(rows)
    prices = store / "prices.csv"
    if prices.exists():
        p = Prices.load(prices)
        if p.values and p.t0 < keep_from - 2 * 86400:
            tmp = prices.with_suffix(".tmp")
            with tmp.open("w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow(["ts", "price"])
                for i, v in enumerate(p.values):
                    if p.t0 + i >= keep_from - 3600:
                        wr.writerow([p.t0 + i, v])
            tmp.replace(prices)


def by_day(directory: Path, index: dict[int, dict] | None = None) -> dict[str, dict]:
    """Per UTC day: markets traded, fills, money staked, logged P&L, the P&L of the
    same trades settled with the official winners (when `index` is given) and the
    generous 'touch-only' extra logged by the rest strategy."""
    windows, trades = analysis.load(directory)
    pos: dict[int, dict] = {}
    fills: dict[int, int] = {}
    for t in trades:
        shares = float(t["shares"])
        if shares <= 0:
            continue
        s = int(analysis._ts(t["window"]))
        p = pos.setdefault(s, {"cash": 0.0, UP: 0.0, DOWN: 0.0})
        if t["action"] == "BUY":
            p["cash"] -= float(t["notional"]) + float(t["fee"])
            p[t["side"]] += shares
            fills[s] = fills.get(s, 0) + 1
        else:
            p["cash"] += float(t["notional"]) - float(t["fee"])
            p[t["side"]] = max(p[t["side"]] - shares, 0.0)
    out: dict[str, dict] = {}
    for w in windows:
        s = int(analysis._ts(w["start"]))
        d = out.setdefault(_day(s), {"markets": 0, "traded": 0, "fills": 0, "staked": 0.0,
                                     "pnl": 0.0, "official_pnl": 0.0, "official_n": 0,
                                     "wrong_winner": 0, "touch_n": 0, "touch_pnl": 0.0})
        d["markets"] += 1
        m = TOUCH.search(w.get("note", ""))
        if m:
            d["touch_n"] += int(m.group(1))
            d["touch_pnl"] += float(m.group(2))
        if int(w["buys"]) <= 0:
            continue
        d["traded"] += 1
        d["fills"] += fills.get(s, 0)
        d["staked"] += float(w["spent"])
        d["pnl"] += float(w["pnl"])
        if index is not None and s in index and s in pos:
            official = index[s]["winner"]
            d["official_n"] += 1
            d["wrong_winner"] += w["winner"] != official
            d["official_pnl"] += pos[s]["cash"] + pos[s][official]
    for d in out.values():
        for k in ("staked", "pnl", "official_pnl", "touch_pnl"):
            d[k] = round(d[k], 2)
    return out


def run_check(cfg: Config, days: float = 14.0, *, fetch: bool = True, log: Log = print) -> dict:
    base = Path(cfg.data_dir)
    store = base / "history-rolling"
    hi = int(time.time() - 900) // WINDOW_S * WINDOW_S
    lo = hi - int(days * 86400) // WINDOW_S * WINDOW_S
    log(f"check: {_iso(lo)} to {_iso(hi)} UTC, strategy '{cfg.strategy}'")
    if fetch:
        store.mkdir(parents=True, exist_ok=True)
        prune(store, lo)
        fetch_prices(lo - 3600, hi + 310, store / "prices.csv", log)
        asyncio.run(fetch_windows(list(range(lo, hi, WINDOW_S)), store, cfg, log))
    index = {s: r for s, r in read_index(store).items() if lo <= s < hi}
    prices = Prices.load(store / "prices.csv") if (store / "prices.csv").exists() else None
    result: dict = {"generated": _iso(time.time()), "strategy": cfg.strategy,
                    "from": _iso(lo), "to": _iso(hi), "markets": len(index), "days": []}
    replay_days: dict[str, dict] = {}
    if index and prices is not None and prices.values:
        out = base / "check-replay"
        for name in ("trades.csv", "windows.csv"):
            (out / name).unlink(missing_ok=True)
        quiet = replace(cfg, status_interval_s=0.0, kill_file="")
        run_replay(quiet, read_windows(store, lo, hi), prices, lo - 3600, hi + 10,
                   feed_lag_s=1.4, shift_s=-1, stamp_delay_s=2.3, journal=Journal(out))
        replay_days = by_day(out)
    mode = "paper-rest" if cfg.strategy == "rest" else "paper"
    paper_days = by_day(base / mode, index) if (base / mode / "windows.csv").exists() else {}
    for day in sorted(set(replay_days) | {d for d in paper_days if d >= _day(lo)}):
        result["days"].append({"date": day, "replay": replay_days.get(day),
                               "paper": paper_days.get(day)})
    tmp = base / "check.json.tmp"
    tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
    tmp.replace(base / "check.json")
    log(render(result))
    return result


def render(result: dict) -> str:
    lines = [f"holding up? strategy '{result['strategy']}', {result['from']} to {result['to']}",
             "  date        replay: fills   staked      P&L  touch-only | paper: fills   staked  "
             "P&L (own)  P&L (official)"]
    tot = {"rf": 0, "rs": 0.0, "rp": 0.0, "pf": 0, "ps": 0.0, "pp": 0.0, "po": 0.0}
    for d in result["days"]:
        r, p = d["replay"] or {}, d["paper"] or {}
        tot["rf"] += r.get("fills", 0); tot["rs"] += r.get("staked", 0); tot["rp"] += r.get("pnl", 0)
        tot["pf"] += p.get("fills", 0); tot["ps"] += p.get("staked", 0)
        tot["pp"] += p.get("pnl", 0); tot["po"] += p.get("official_pnl", 0)
        lines.append(
            f"  {d['date']}        {r.get('fills', 0):5d} {r.get('staked', 0):8.0f} {r.get('pnl', 0):+8.2f}"
            f"  {r.get('touch_pnl', 0):+9.2f} |       {p.get('fills', 0):5d} {p.get('staked', 0):8.0f}"
            f"  {p.get('pnl', 0):+9.2f}  {p.get('official_pnl', 0):+14.2f}")
    lines.append(f"  total             {tot['rf']:5d} {tot['rs']:8.0f} {tot['rp']:+8.2f}            |"
                 f"       {tot['pf']:5d} {tot['ps']:8.0f}  {tot['pp']:+9.2f}  {tot['po']:+14.2f}")
    return "\n".join(lines)


def load(directory: str | Path) -> dict | None:
    path = Path(directory) / "check.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

