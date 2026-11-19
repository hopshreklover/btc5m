"""Find which Polymarket Up/Down series exist, and download their history.

  python research/series_fetch.py probe
  python research/series_fetch.py fetch SLUG_PREFIX LENGTH_S DAYS OUT_DIR
      e.g. fetch eth-updown-5m 300 14 data/series/eth5m

Public data only (market descriptions, trades, official results).
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")
from btc5m.config import Config
from btc5m.history import GAMMA, _get_json, fetch_windows, parse_market

CANDIDATES = [(f"{coin}-updown-{n}", secs) for coin in ("btc", "eth", "sol", "xrp", "doge", "bnb")
              for n, secs in (("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14400))]


def probe() -> None:
    now = int(time.time())
    for prefix, length in CANDIDATES:
        start = now // length * length - 2 * length          # a market that should have closed
        try:
            payload = _get_json(GAMMA.format(slug=f"{prefix}-{start}"), tries=2)
        except Exception as exc:  # noqa: BLE001
            print(f"{prefix:16s} error {exc!r}")
            continue
        win = parse_market(payload, start, Config(), length)
        if win is None:
            print(f"{prefix:16s} -")
        else:
            print(f"{prefix:16s} exists: volume ${win['volume']:,.0f}, winner {win['winner']}, "
                  f"fee rate {win['fee_rate']}")


def fetch(prefix: str, length: int, days: float, out: str) -> None:
    hi = int(time.time() - 1800) // length * length
    lo = hi - int(days * 86400) // length * length
    asyncio.run(fetch_windows(list(range(lo, hi, length)), out, Config(), concurrency=6,
                              slug=prefix + "-{start}", length=length))


if __name__ == "__main__":
    if sys.argv[1] == "probe":
        probe()
    else:
        fetch(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5])
