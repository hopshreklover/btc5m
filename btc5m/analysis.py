"""Read the bot's CSV logs and work out where the profit or loss came from."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

PRICE_BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))
TIME_BUCKETS = ((200, 301), (100, 200), (30, 100), (0, 30))   # seconds left at entry


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load(directory: str | Path) -> tuple[list[dict], list[dict]]:
    d = Path(directory)
    return _rows(d / "windows.csv"), _rows(d / "trades.csv")


def summary(windows: list[dict]) -> dict:
    traded = [r for r in windows if int(r["buys"]) > 0]
    pnl = [float(r["pnl"]) for r in traded]
    peak = equity = drawdown = 0.0
    for x in pnl:
        equity += x
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    staked = sum(float(r["spent"]) for r in traded)
    top3 = sum(sorted(pnl, reverse=True)[:3])
    return {
        "windows_seen": len(windows),
        "windows_traded": len(traded),
        "win_rate": round(sum(x > 0 for x in pnl) / len(pnl), 4) if pnl else None,
        "total_pnl": round(sum(pnl), 2),
        "pnl_without_best_3": round(sum(pnl) - top3, 2) if len(pnl) > 3 else None,
        "median_window_pnl": round(sorted(pnl)[len(pnl) // 2], 2) if pnl else None,
        "fees_paid": round(sum(float(r["fees"]) for r in traded), 2),
        "staked": round(staked, 2),
        "return_on_stake": round(sum(pnl) / staked, 4) if staked else None,
        "max_drawdown": round(drawdown, 2),
        "sat_out": sum("sat out" in r["note"] for r in windows),
        "outcome_mismatches": sum("MISMATCH" in r["note"] for r in windows),
        "first_window": windows[0]["start"] if windows else None,
        "last_window": windows[-1]["start"] if windows else None,
    }


def _bucket_table(buys: list[dict], key: str, buckets) -> list[dict]:
    out = []
    for lo, hi in buckets:
        rows = [b for b in buys if lo <= b[key] < hi]
        staked = sum(b["cost"] for b in rows)
        pnl = sum(b["hold_pnl"] for b in rows)
        out.append({
            "lo": lo, "hi": hi, "buys": len(rows), "staked": round(staked, 2),
            "avg_price": round(sum(b["price"] for b in rows) / len(rows), 3) if rows else None,
            "predicted": round(sum(b["p_used"] for b in rows) / len(rows), 3) if rows else None,
            "won": round(sum(b["won"] for b in rows) / len(rows), 3) if rows else None,
            "hold_pnl": round(pnl, 2),
            "return": round(pnl / staked, 3) if staked else None,
        })
    return out


def detail(windows: list[dict], trades: list[dict]) -> dict:
    """Break filled buys down by entry price and by time left.

    `predicted` is the conservative win probability the bot acted on; `won` is
    how often those shares actually paid out. If `won` is not at least the
    average price paid, there was no edge in that bucket. P&L here is on a
    hold-to-settlement basis, so it ignores early exits.
    """
    winner = {r["start"]: r["winner"] for r in windows}
    start = {r["start"]: _ts(r["start"]) for r in windows}
    buys, sells, missed = [], [], 0
    for t in trades:
        shares = float(t["shares"])
        if shares <= 0:
            missed += 1
            continue
        if t["action"] == "SELL":
            sells.append(float(t["notional"]) - float(t["fee"]))
            continue
        win = winner.get(t["window"])
        if win not in ("UP", "DOWN"):
            continue
        won = 1.0 if t["side"] == win else 0.0
        cost = float(t["notional"]) + float(t["fee"])
        buys.append({
            "price": float(t["price"]), "p_used": float(t["p_used"]), "won": won,
            "cost": cost, "hold_pnl": shares * won - cost,
            "left": 300.0 - (_ts(t["time"]) - start[t["window"]]),
        })
    return {
        "buys": len(buys), "sells": len(sells), "missed_orders": missed,
        "exit_proceeds": round(sum(sells), 2),
        "by_price": _bucket_table(buys, "price", PRICE_BUCKETS),
        "by_time_left": _bucket_table(buys, "left", TIME_BUCKETS),
    }


def equity(windows: list[dict]) -> list[float]:
    out, total = [], 0.0
    for r in windows:
        if int(r["buys"]) > 0:
            total += float(r["pnl"])
            out.append(round(total, 2))
    return out


def render(directory: str | Path) -> str:
    windows, trades = load(directory)
    if not windows:
        return f"nothing recorded yet in {directory}"
    lines = [f"{k:>20}: {v}" for k, v in summary(windows).items()]
    d = detail(windows, trades)
    lines += ["", f"buys {d['buys']}, sells {d['sells']} (proceeds {d['exit_proceeds']}), "
                  f"missed orders {d['missed_orders']}"]
    for title, key, unit in (("by entry price", "by_price", ""),
                             ("by seconds left at entry", "by_time_left", "s")):
        lines += ["", f"{title}  (P&L as if every buy were held to settlement)",
                  "  bucket        buys   staked  avg price  predicted    won   hold P&L  return"]
        for b in d[key]:
            if not b["buys"]:
                continue
            label = f"{b['lo']:g}-{min(b['hi'], 1.0 if not unit else b['hi']):g}{unit}"
            lines.append(
                f"  {label:<12} {b['buys']:5d} {b['staked']:8.2f}    {b['avg_price']:.3f}"
                f"      {b['predicted']:.3f}  {b['won']:.3f}  {b['hold_pnl']:9.2f}  {b['return']:+.3f}"
            )
    return "\n".join(lines)
