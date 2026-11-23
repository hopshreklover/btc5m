"""Command line: python -m btc5m <command>."""

from __future__ import annotations

import argparse
import asyncio
import glob
import signal
import sys
from pathlib import Path

from .config import Config, load_config

LIVE_FLAG = "--i-accept-real-money-risk"


def _cfg(args: argparse.Namespace, **extra: object) -> Config:
    over = {
        "bankroll": getattr(args, "bankroll", None),
        "min_edge": getattr(args, "min_edge", None),
        "data_dir": getattr(args, "data_dir", None),
        "spot_lead": True if getattr(args, "spot_lead", False) else None,
    }
    over.update(extra)
    return load_config(args.config, **over)


def _print_summary(summary: dict) -> None:
    print("\n--- summary ---")
    for key, value in summary.items():
        print(f"{key:>18}: {value}")


def cmd_run(args: argparse.Namespace, live: bool) -> int:
    from .polymarket_io import run

    cfg = _cfg(args)
    if live:
        if not args.i_accept_real_money_risk:
            print(f"Live mode places real orders with real money. Re-run with {LIVE_FLAG}.")
            return 2
        print(
            "LIVE MODE: real orders will be sent.\n"
            f"  bankroll {cfg.bankroll:.2f} | max per order {cfg.max_trade_usd:.2f} | "
            f"max per window {cfg.max_window_usd:.2f} | daily loss stop {cfg.daily_loss_limit:.2f}\n"
            f"  create a file named '{cfg.kill_file}' in this folder to block new entries."
        )
    holder: list = []

    def stop(*_args: object) -> None:      # systemd stop: shut down like Ctrl+C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(run(cfg, live=live, auth_prices=args.auth_prices, holder=holder))
    except KeyboardInterrupt:
        pass
    if holder:
        _print_summary(holder[0].summary())
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from .broker import PaperBroker
    from .engine import Engine
    from .journal import read_events

    cfg = _cfg(args, status_interval_s=0.0)
    files = [f for pattern in args.files for f in glob.glob(pattern)]
    if not files:
        print("no recording files matched")
        return 2
    engine = Engine(cfg, PaperBroker(cfg.paper_latency_s),
                    log=print if args.verbose else (lambda _m: None))
    n = 0
    for ev in read_events(files):
        engine.on_event(ev)
        n += 1
    print(f"replayed {n} events from {len(files)} file(s)")
    _print_summary(engine.summary())
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from .coinbase import fetch_seconds, save_series

    series = fetch_seconds(args.hours)
    save_series(series, args.out)
    print(f"saved {len(series)} seconds of BTC-USD prices to {args.out}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from .calibrate import render, report
    from .coinbase import load_series

    cfg = _cfg(args)
    if args.recordings:
        from .calibrate import series_from_recordings

        files = [f for pattern in args.recordings for f in glob.glob(pattern)]
        series = series_from_recordings(files)
        print(f"using {len(series)} seconds of recorded oracle prices from {len(files)} file(s)")
    else:
        series = load_series(args.prices)
    if len(series) < 1200:
        print("not enough price data to calibrate (need at least 20 minutes)")
        return 2
    print(render(report(series, cfg), cfg))
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    from .coinbase import load_series
    from .sim import run_sim

    cfg = _cfg(args, status_interval_s=0.0, twap_s=0.0)   # the made-up market is instant-price
    series = load_series(args.prices)
    print("SIMULATION with a synthetic market maker. Profit here is NOT evidence of "
          "profit on Polymarket.")
    engine = run_sim(
        series, cfg, log=print if args.verbose else (lambda _m: None),
        mm_lag_s=args.mm_lag, mm_noise=args.mm_noise, mm_spread=args.mm_spread, seed=args.seed,
    )
    _print_summary(engine.summary())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .analysis import render

    print(render(Path(args.data_dir or Config().data_dir) / args.mode))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from .history import run

    cfg = _cfg(args)
    lags = [float(x) for x in args.lags.split(",") if x.strip()]
    results = run(cfg, args.days, lags, fetch=not args.no_fetch, replay=not args.fetch_only,
                  stamp_delay=args.stamp_delay)
    if results:
        print("\n--- real-history replay, by settlement rule and feed lag ---")
        print("  rule     lag  traded   win%      P&L  w/o best 3   staked  return  drawdown  "
              "mismatches")
        for r in results:
            print(f"  {r['rule']:<8} {r['feed_lag_s']:>3g} {r.get('windows_traded', 0):>7} "
                  f"{(r.get('win_rate') or 0) * 100:>6.1f} {r.get('total_pnl', 0):>8.2f} "
                  f"{r.get('pnl_without_best_3') or 0:>11.2f} {r.get('staked', 0):>8.2f} "
                  f"{(r.get('return_on_stake') or 0) * 100:>6.1f}% {r.get('max_drawdown', 0):>9.2f} "
                  f"{r.get('outcome_mismatches', 0):>11}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .check import run_check

    run_check(_cfg(args), args.days, fetch=not args.no_fetch)
    return 0


def cmd_pairs(args: argparse.Namespace) -> int:
    import signal

    from .pairs import PairPaper
    from .pairs_live import run_pairs

    def stop(*_a: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    coins = [c.strip().lower() for c in args.coins.split(",") if c.strip()]
    papers = {c: PairPaper(max_cost=args.max_cost, usd=args.usd,
                           strike_margin_bp=args.strike_margin_bp) for c in coins}
    out = Path(args.data_dir or Config().data_dir) / "pairs"
    try:
        asyncio.run(run_pairs(out, papers, log=lambda m: print(m, flush=True)))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve

    serve(Path(args.data_dir or Config().data_dir) / args.mode, args.mode, args.host, args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="btc5m", description=__doc__)
    ap.add_argument("--config", help="TOML file overriding defaults (see config.example.toml)")
    ap.add_argument("--data-dir", help="where logs and recordings go (default: data)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def run_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--bankroll", type=float)
        p.add_argument("--min-edge", type=float)
        p.add_argument("--spot-lead", action="store_true",
                       help="also use Coinbase's ticker as a faster price signal")
        p.add_argument("--auth-prices", action="store_true",
                       help="use the authenticated oracle price stream (needs POLYMARKET_PRIVATE_KEY)")

    run_opts(sub.add_parser("paper", help="trade live markets with pretend money"))
    live = sub.add_parser("live", help="trade live markets with real money")
    run_opts(live)
    live.add_argument(LIVE_FLAG, action="store_true")

    bt = sub.add_parser("backtest", help="replay recordings made by paper/live")
    bt.add_argument("files", nargs="+", help="events-*.jsonl files or glob patterns")
    bt.add_argument("--bankroll", type=float)
    bt.add_argument("--min-edge", type=float)
    bt.add_argument("-v", "--verbose", action="store_true")

    fe = sub.add_parser("fetch", help="download recent BTC prices from Coinbase")
    fe.add_argument("--hours", type=float, default=24)
    fe.add_argument("--out", default="data/btc_1s.csv")

    ca = sub.add_parser("calibrate", help="score the pricing model on downloaded prices")
    ca.add_argument("--prices", default="data/btc_1s.csv")
    ca.add_argument("--recordings", nargs="+", metavar="FILE",
                    help="use oracle prices recorded by paper/live sessions instead")

    si = sub.add_parser("sim", help="offline run against a synthetic market maker")
    si.add_argument("--prices", default="data/btc_1s.csv")
    si.add_argument("--mm-lag", type=int, default=3)
    si.add_argument("--mm-noise", type=float, default=0.01)
    si.add_argument("--mm-spread", type=float, default=0.02)
    si.add_argument("--seed", type=int, default=7)
    si.add_argument("--bankroll", type=float)
    si.add_argument("--min-edge", type=float)
    si.add_argument("-v", "--verbose", action="store_true")

    rp = sub.add_parser("report", help="summarise logged results")
    rp.add_argument("--mode", default="paper",
                    help="which log folder under the data dir (paper, live, or an archived one)")

    hi = sub.add_parser("history", help="replay the bot over real past trades and results")
    hi.add_argument("--days", type=float, default=7.0)
    hi.add_argument("--lags", default="0.4,1.4,2.4",
                    help="price-feed delays to test, in seconds; the live feed measured 1.4 "
                         "(default 0.4,1.4,2.4)")
    hi.add_argument("--stamp-delay", type=float, default=2.3,
                    help="seconds that published trade times run behind the real trade "
                         "(measured: 2.3)")
    hi.add_argument("--no-fetch", action="store_true", help="use what is already downloaded")
    hi.add_argument("--fetch-only", action="store_true", help="download, do not replay")
    hi.add_argument("--bankroll", type=float)
    hi.add_argument("--min-edge", type=float)

    ck = sub.add_parser("check", help="daily: replay the strategy and re-settle the paper "
                                      "log against the latest real results")
    ck.add_argument("--days", type=float, default=14.0)
    ck.add_argument("--no-fetch", action="store_true", help="use what is already downloaded")

    pa = sub.add_parser("pairs", help="paper-trade the nested 5m/15m Bitcoin pair (no orders)")
    pa.add_argument("--max-cost", type=float, default=0.99, help="per pair, fees included")
    pa.add_argument("--usd", type=float, default=20.0, help="per paper trade")
    pa.add_argument("--strike-margin-bp", type=float, default=0.5,
                    help="skip when the two starting prices are closer than this (basis points)")
    pa.add_argument("--coins", default="btc,eth,sol,xrp")

    da = sub.add_parser("dashboard", help="serve a read-only results page")
    da.add_argument("--mode", default="paper")
    da.add_argument("--host", default="127.0.0.1")
    da.add_argument("--port", type=int, default=8501)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "paper":
            return cmd_run(args, live=False)
        if args.cmd == "live":
            return cmd_run(args, live=True)
        return {"backtest": cmd_backtest, "fetch": cmd_fetch, "calibrate": cmd_calibrate,
                "sim": cmd_sim, "report": cmd_report, "dashboard": cmd_dashboard,
                "history": cmd_history, "check": cmd_check, "pairs": cmd_pairs}[args.cmd](args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
