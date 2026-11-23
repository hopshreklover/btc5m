"""A read-only results page. Standard library only; it never touches the bot,
it just reads the CSV logs each time the page is loaded."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analysis

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#667085;--line:#e3e6ec;
--good:#0a7f4f;--bad:#c0362c;--accent:#2f5fd0}
@media (prefers-color-scheme:dark){:root{--bg:#101216;--card:#191c22;--ink:#e8eaee;
--mute:#98a1b2;--line:#2a2f39;--good:#3ecf8e;--bad:#ff7b6b;--accent:#7ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:28px 0 8px}
.sub{color:var(--mute);font-size:13px;margin:4px 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:10px}
.tile{padding:12px 14px}.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.tile span{color:var(--mute);font-size:12px}
.card{padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:600}th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}.good{color:var(--good)}.bad{color:var(--bad)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
border:1px solid var(--line);margin-left:8px;vertical-align:middle}
.note{color:var(--mute);font-size:13px;margin-top:8px}
svg{width:100%;height:auto;display:block}
"""


def _money(x: float | None, sign: bool = True) -> str:
    if x is None:
        return "–"
    cls = "good" if x > 0 else "bad" if x < 0 else ""
    text = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return f'<span class="{cls}">{text}</span>' if sign else text


def _p3(x) -> str:
    """A probability, or a dash when the strategy did not use one (resting bids)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "–"
    return "–" if v != v else f"{v:.3f}"


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.1f}%"


def _chart(points: list[float]) -> str:
    if len(points) < 2:
        return '<p class="note">Not enough traded windows for a chart yet.</p>'
    w, h, pad = 900.0, 220.0, 8.0
    lo, hi = min(points + [0.0]), max(points + [0.0])
    span = (hi - lo) or 1.0

    def xy(i: int, v: float) -> str:
        x = pad + (w - 2 * pad) * i / (len(points) - 1)
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        return f"{x:.1f},{y:.1f}"

    zero = pad + (h - 2 * pad) * (1 - (0 - lo) / span)
    line = " ".join(xy(i, v) for i, v in enumerate(points))
    return (
        f'<svg viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="Cumulative profit">'
        f'<line x1="{pad}" x2="{w - pad}" y1="{zero:.1f}" y2="{zero:.1f}" '
        f'stroke="var(--line)" stroke-width="1"/>'
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="{line}"/></svg>'
    )


def _table(head: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="note">Nothing yet.</p>'
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="card"><table><tr>{th}</tr>{body}</table></div>'


def _buckets(rows: list[dict], unit: str) -> str:
    out = []
    for b in rows:
        if not b["buys"]:
            continue
        hi = b["hi"] if unit else min(b["hi"], 1.0)
        out.append([
            f"{b['lo']:g}–{hi:g}{unit}", str(b["buys"]), f"{b['staked']:,.2f}",
            f"{b['avg_price']:.3f}", _p3(b['predicted']), f"{b['won']:.3f}",
            _money(b["hold_pnl"]), _pct(b["return"]),
        ])
    return _table(["Bucket", "Buys", "Staked", "Avg price", "Predicted", "Actually won",
                   "Hold P&L", "Return"], out)


def _pairs_section(directory: Path) -> str:
    """Paper trades of the nested 5m/15m pair strategy (`python -m btc5m pairs`)."""
    import csv as _csv

    root = directory.parent / "pairs"
    if not root.exists():
        return ""

    def rows(path: Path) -> list[dict]:
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                return list(_csv.DictReader(fh))
        except OSError:
            return []

    table, recent = [], []
    tot_n = tot_open = 0
    tot_pnl = tot_staked = 0.0
    for coin_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        trades = rows(coin_dir / "trades.csv")
        results = rows(coin_dir / "results.csv")
        settled = rows(coin_dir / "settled.csv")
        pnl = sum(float(r["pnl"]) for r in results)
        staked = sum(float(r["shares"]) * float(r["cost"]) for r in results)
        wrong = sum(r["pair"] != r["official_pair"] for r in results)
        two = sum(float(r["payoff"]) >= 2 for r in results)
        table.append([escape(coin_dir.name.upper()), str(len(settled)), str(len(trades)),
                      str(len(results)), _money(pnl), f"{staked:,.2f}",
                      _pct(pnl / staked if staked else None), str(two), str(wrong)])
        tot_n += len(results)
        tot_open += len(trades) - len(results)
        tot_pnl += pnl
        tot_staked += staked
        for r in trades[-5:]:
            recent.append([escape(coin_dir.name.upper()),
                           escape(datetime.fromtimestamp(float(r["ts"]), timezone.utc).strftime("%m-%d %H:%M:%S")),
                           escape(r["pair"]), f"{float(r['shares']):.2f}", f"{float(r['p5']):.3f}",
                           f"{float(r['p15']):.3f}", f"{float(r['cost']):.4f}"])
    if not table:
        return ""
    recent.sort(key=lambda r: r[1], reverse=True)
    return (
        "<h2>Nested 5m/15m pairs (paper)</h2>"
        "<p class='note'>Buys the last 5-minute market and the 15-minute market it ends with, "
        "on the side that pays at least $1 whatever happens ($2 when the price finishes between "
        "the two starting prices), only when the pair costs under $0.99 with fees on two "
        f"consecutive order-book snapshots. Settled so far: {tot_n} trades, P&amp;L "
        f"{_money(tot_pnl)} on {tot_staked:,.2f} staked; {tot_open} waiting for results.</p>"
        + _table(["Coin", "Markets watched", "Paper buys", "Settled", "P&L", "Staked", "Return",
                  "Paid $2", "Wrong pairing"], table)
        + (_table(["Coin", "Time (UTC)", "Pair", "Shares", "5m price", "15m price", "Cost/pair"],
                  recent[:10]) if recent else ""))


def _check_section(directory: Path) -> str:
    """The daily 'holding up?' table written by `check`, if there is one."""
    from . import check

    res = check.load(directory.parent)
    if not res or not res.get("days"):
        return ""
    rows, tot = [], [0, 0.0, 0.0, 0, 0.0, 0.0]
    for d in res["days"][::-1]:
        r, p = d.get("replay") or {}, d.get("paper") or {}
        rows.append([escape(str(d["date"])), str(r.get("fills", 0)), f"{r.get('staked', 0):,.0f}",
                     _money(r.get("pnl", 0.0)), _money(r.get("touch_pnl", 0.0)),
                     str(p.get("fills", 0)), _money(p.get("pnl", 0.0)),
                     _money(p.get("official_pnl", 0.0)) if p.get("official_n") else "–"])
        tot[0] += r.get("fills", 0); tot[1] += r.get("staked", 0.0); tot[2] += r.get("pnl", 0.0)
        tot[3] += p.get("fills", 0); tot[4] += p.get("pnl", 0.0); tot[5] += p.get("official_pnl", 0.0)
    rows.append(["<b>total</b>", str(tot[0]), f"{tot[1]:,.0f}", _money(tot[2]), "",
                 str(tot[3]), _money(tot[4]), _money(tot[5])])
    return (
        f"<h2>Holding up against real results</h2>"
        f"<p class='note'>Updated {escape(str(res.get('generated')))} UTC for strategy "
        f"'{escape(str(res.get('strategy')))}'. Replay: the strategy run over each day's real "
        f"trades and official winners. Paper: this bot's own trades, settled by its own prices and "
        f"by the official winners. Touch-only: what the generous fill rule would have added.</p>"
        + _table(["Day (UTC)", "Replay fills", "Staked", "Replay P&L", "Touch-only",
                  "Paper fills", "Paper P&L (own)", "Paper P&L (official)"], rows))


def page(directory: str | Path, mode: str) -> str:
    windows, trades = analysis.load(directory)
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="60"><title>btc5m results</title>'
            f"<style>{CSS}</style></head><body><main>")
    if not windows:
        return (head + f"<h1>btc5m <span class='badge'>{escape(mode)}</span></h1>"
                f"<p class='sub'>No windows logged yet. Checked {stamp}.</p>"
                + _check_section(Path(directory)) + _pairs_section(Path(directory))
                + "</main></body></html>")
    s = analysis.summary(windows)
    d = analysis.detail(windows, trades)
    age = now - (analysis._ts(windows[-1]["start"]) + 300)
    state = ("running" if age < 900 else f"no new window for {age / 60:.0f} min")
    tiles = [
        ("Profit / loss", _money(s["total_pnl"])),
        ("Without best 3 windows", _money(s["pnl_without_best_3"])),
        ("Return on stake", _pct(s["return_on_stake"])),
        ("Winning windows", _pct(s["win_rate"])),
        ("Windows traded", f"{s['windows_traded']} / {s['windows_seen']}"),
        ("Fees paid", _money(s["fees_paid"], sign=False)),
        ("Worst drawdown", _money(s["max_drawdown"], sign=False)),
        ("Outcome mismatches", str(s["outcome_mismatches"])),
    ]
    tile_html = "".join(f'<div class="tile"><b>{v}</b><span>{escape(k)}</span></div>'
                        for k, v in tiles)
    recent_w = [[
        escape(r["start"][5:16].replace("T", " ")), escape(r["winner"]), escape(r["buys"]),
        escape(r["misses"]), f"{float(r['spent']):,.2f}", _money(float(r["pnl"])),
        escape(r["note"][:60]),
    ] for r in windows[-15:][::-1]]
    recent_t = [[
        escape(t["time"][5:19].replace("T", " ")), escape(t["action"]), escape(t["side"]),
        f"{float(t['shares']):.2f}", f"{float(t['price']):.3f}", _p3(t['p_used']),
        f"{float(t['edge']):+.3f}", escape(t["note"][:40]),
    ] for t in trades[-15:][::-1]]
    money = "Real money." if mode == "live" else "Pretend money: fills are simulated."
    return head + (
        f"<h1>btc5m <span class='badge'>{escape(mode)}</span>"
        f"<span class='badge'>{escape(state)}</span></h1>"
        f"<p class='sub'>{money} {escape(str(s['first_window']))} to "
        f"{escape(str(s['last_window']))}. Page built {stamp}; refreshes every minute.</p>"
        f'<div class="tiles">{tile_html}</div>'
        + _check_section(Path(directory)) + _pairs_section(Path(directory)) +
        f"<h2>Cumulative profit by traded window</h2><div class='card'>{_chart(analysis.equity(windows))}</div>"
        f"<h2>Buys by entry price</h2>{_buckets(d['by_price'], '')}"
        f"<h2>Buys by seconds left at entry</h2>{_buckets(d['by_time_left'], 's')}"
        f"<p class='note'>Predicted is the win chance the bot acted on; Actually won is how often "
        f"those shares paid out. Hold P&amp;L ignores early exits "
        f"({d['sells']} sells, {d['exit_proceeds']:,.2f} proceeds; {d['missed_orders']} orders "
        f"did not fill).</p>"
        f"<h2>Latest windows</h2>"
        + _table(["Window (UTC)", "Winner", "Buys", "Missed", "Staked", "P&L", "Note"], recent_w)
        + "<h2>Latest orders</h2>"
        + _table(["Time (UTC)", "Action", "Side", "Shares", "Price", "Used p", "Edge", "Note"],
                 recent_t)
        + "</main></body></html>"
    )


def serve(directory: str | Path, mode: str, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/":
                self.send_error(404)
                return
            try:
                body = page(directory, mode).encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - a half-written CSV row must not kill the page
                body = f"<p>Could not build the page: {escape(repr(exc))}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"dashboard for {directory} on http://{host}:{port}/ (read-only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
