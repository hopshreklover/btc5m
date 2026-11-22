"""Watch the nested 5m/15m Bitcoin pair on the real order books. Places no orders.

During the last 5 minutes of each 15-minute market S, once a second it reads the
order books of the four tokens (5m Up/Down for the market starting at S+600,
15m Up/Down) and logs both pairings whenever one costs <= LOG_BELOW including
taker fees:
  A = 5m Up + 15m Down   (pays >= $1 when strike5 <= strike15)
  B = 5m Down + 15m Up   (pays >= $1 when strike5 >  strike15)
After both markets settle it records their official strikes and winners, so
`pair_report.py` can say which opportunities were real and what they paid.

  python research/pair_monitor.py [OUT_DIR]      (default data/pairs)
"""
import asyncio
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
from btc5m.config import Config
from btc5m.history import GAMMA, _get_json, parse_market

LOG_BELOW = 1.05
FEE = 0.07


def fee(p):
    return FEE * p * (1 - p)


def best_ask(book):
    """(price, size) of the cheapest ask, or (None, 0)."""
    if not book.asks:
        return None, 0.0
    lvl = min(book.asks, key=lambda l: float(l.price))
    return float(lvl.price), float(lvl.size)


def append(path, row, header):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerow(row)


async def market(slug, start, length):
    """Market description, retried until the exchange lists it."""
    for _ in range(30):
        try:
            payload = await asyncio.to_thread(_get_json, GAMMA.format(slug=slug), 2)
            ev = payload[0] if isinstance(payload, list) and payload else None
            if ev and ev.get("markets"):
                m = ev["markets"][0]
                import json as _j
                names = [str(x).lower() for x in _j.loads(m["outcomes"])]
                toks = [str(x) for x in _j.loads(m["clobTokenIds"])]
                meta = ev.get("eventMetadata") or {}
                return {"up": toks[names.index("up")], "down": toks[names.index("down")],
                        "strike": meta.get("priceToBeat")}
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2)
    return None


async def settle_later(start15, out):
    """Wait for both markets to resolve, then record strikes and winners."""
    await asyncio.sleep(max(0.0, start15 + 900 + 150 - time.time()))
    for _ in range(20):
        try:
            p15 = await asyncio.to_thread(_get_json, GAMMA.format(slug=f"btc-updown-15m-{start15}"), 2)
            p5 = await asyncio.to_thread(_get_json, GAMMA.format(slug=f"btc-updown-5m-{start15 + 600}"), 2)
            w15 = parse_market(p15, start15, Config(), 900)
            w5 = parse_market(p5, start15 + 600, Config(), 300)
            if w15 and w5:
                append(out / "settled.csv",
                       [start15, w5["strike"], w15["strike"], w5["winner"], w15["winner"],
                        w5["final"], w15["final"]],
                       ["start15", "strike5", "strike15", "winner5", "winner15", "final5", "final15"])
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(30)


async def main(out):
    from polymarket import AsyncPublicClient

    out.mkdir(parents=True, exist_ok=True)
    done = set()
    async with AsyncPublicClient() as pub:
        while True:
            now = time.time()
            s15 = int(now // 900 * 900)
            if now - s15 < 600 or s15 in done:
                await asyncio.sleep(1.0)
                continue
            done.add(s15)
            m5 = await market(f"btc-updown-5m-{s15 + 600}", s15 + 600, 300)
            m15 = await market(f"btc-updown-15m-{s15}", s15, 900)
            if not m5 or not m15:
                print(f"{s15}: markets not found", flush=True)
                continue
            asyncio.create_task(settle_later(s15, out))
            toks = [m5["up"], m5["down"], m15["up"], m15["down"]]
            day = datetime.fromtimestamp(s15, timezone.utc).strftime("%Y%m%d")
            path = out / f"books-{day}.csv"
            seen = logged = 0
            while time.time() < s15 + 900 - 2:
                t = time.time()
                try:
                    books = await pub.get_order_books(token_ids=toks)
                except Exception as exc:  # noqa: BLE001
                    print(f"books: {exc!r}", flush=True)
                    await asyncio.sleep(1.0)
                    continue
                by = {str(b.asset_id): b for b in books}
                (u5, su5), (d5, sd5) = best_ask(by[m5["up"]]), best_ask(by[m5["down"]])
                (u15, su15), (d15, sd15) = best_ask(by[m15["up"]]), best_ask(by[m15["down"]])
                seen += 1
                ca = u5 + d15 + fee(u5) + fee(d15) if u5 is not None and d15 is not None else 9.0
                cb = d5 + u15 + fee(d5) + fee(u15) if d5 is not None and u15 is not None else 9.0
                if min(ca, cb) <= LOG_BELOW:
                    logged += 1
                    append(path, [round(t, 2), s15, round(t - s15 - 600, 1), round(ca, 4),
                                  min(su5, sd15), round(cb, 4), min(sd5, su15),
                                  m5["strike"] is not None, m15["strike"] is not None],
                           ["ts", "start15", "sec5", "cost_a", "size_a", "cost_b", "size_b",
                            "strike5_known", "strike15_known"])
                await asyncio.sleep(max(0.0, 1.0 - (time.time() - t)))
            print(f"{datetime.fromtimestamp(s15, timezone.utc):%m-%d %H:%M} pair: {seen} snapshots, "
                  f"{logged} at or under {LOG_BELOW}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/pairs")))
