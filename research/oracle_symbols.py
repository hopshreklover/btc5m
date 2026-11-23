"""Listen to the public oracle price stream for a few seconds and count ticks per symbol.

  python research/oracle_symbols.py [SECONDS] [SYMBOL ...]
"""
import asyncio
import collections
import sys


async def main(seconds, symbols):
    from polymarket import AsyncPublicClient
    from polymarket.streams import CryptoPricesSpec

    counts, last = collections.Counter(), {}
    async with AsyncPublicClient() as pub:
        handle = await pub.subscribe(CryptoPricesSpec(topic="prices.crypto.chainlink", symbols=symbols))

        async def read():
            async for ev in handle:
                counts[str(ev.payload.symbol)] += 1
                last[str(ev.payload.symbol)] = float(ev.payload.value)

        try:
            await asyncio.wait_for(read(), seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            await handle.close()
    for sym in symbols:
        print(f"{sym:10s} ticks {counts.get(sym, 0):4d}  last {last.get(sym)}")
    extra = set(counts) - set(symbols)
    if extra:
        print("other symbols received:", sorted(extra))


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 15
    syms = sys.argv[2:] or ["btc/usd", "eth/usd", "sol/usd", "xrp/usd"]
    asyncio.run(main(secs, syms))
