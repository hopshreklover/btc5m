# btc5m

A trading bot for Polymarket's "Bitcoin Up or Down – 5 Minutes" markets.

Each market pays $1 per share on **Up** if the Chainlink BTC/USD price at the
end of a 5-minute window is at or above the price at its start, otherwise on
**Down**. Both prices are 60-second averages (the market's rules name a
60-second TWAP stream), not the price at the exact second; `twap_s` sets this.
The bot prices that bet from the live oracle price, compares it with
the order book, and only trades when the book is offering a price that is still
good after fees and after assuming the market moves against it while the order
is in flight.

It starts in **paper mode** (pretend money, real market data). Real orders are
only sent by the `live` command, and only with an explicit flag.

> Nothing here is a promise of profit. These markets are crowded with fast,
> well-funded traders, and the fee is highest exactly where the outcome is most
> uncertain. Treat paper results as the evidence, and expect them to be
> unflattering.

## Setup

Needs Python 3.11 or newer.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

(On macOS/Linux use `.venv/bin/python`.) Then check everything works:

```bash
.venv\Scripts\python -m pytest tests -q
```

## Commands

| Command | What it does |
| --- | --- |
| `python -m btc5m paper` | Trade the live markets with pretend money. Logs to `data/paper/`; records prices plus one book snapshot per second to `data/recordings/` (compressed, 14 days kept). |
| `python -m btc5m report` | Summarise logged results, then break the buys down by entry price and by time left: what the bot predicted versus how often it actually won. `--mode NAME` reads another log folder. |
| `python -m btc5m dashboard --host 0.0.0.0 --port 8501` | Serve the same results as a read-only web page (refreshes every minute). Defaults to localhost only. |
| `python -m btc5m history --days 7` | Replay the bot over real history: every real trade and the official winner of each past market, with the bot's price feed delayed by 0.4, 1.4 and 2.4 seconds (the live feed measured 1.4), once for each settlement rule (`instant` and `avg60`). Downloads public data (resumable) into `data/history/`; results land in `data/history-RULE-lagN/` for `report --mode history-avg60-lag1.4`. Prices, sizes and winners are real; that the bot would have reached each quote first is an assumption. It also re-settles the paper and live logs with the official winners. |
| `python -m btc5m backtest "data/recordings/*.jsonl"` | Replay recordings through the same engine, e.g. with different settings. |
| `python -m btc5m fetch --hours 24` | Download recent BTC-USD prices from Coinbase (public, no account). |
| `python -m btc5m calibrate` | Score the pricing model against those prices, or against the bot's own recorded oracle prices with `--recordings "data/recordings/*.jsonl"`. |
| `python -m btc5m sim` | Offline end-to-end run against a made-up market maker. |
| `python -m btc5m live --i-accept-real-money-risk` | Trade with real money. |

Settings: copy `config.example.toml` to `config.toml` and pass
`--config config.toml` before the command. Every setting and its default is in
`btc5m/config.py`.

## How it decides

1. **Strike.** The average of the oracle ticks over the 60 seconds up to the
   window start (or the market's own "price to beat" when it is published). If
   the bot joined too late to see most of that minute, it sits the window out
   rather than guess. The final price is the same average at the window end,
   and the pricing accounts for part of that average already being fixed
   during the last minute.
2. **Fair value.** `P(up) = F(ln(price / strike) / (vol × √seconds_left))`,
   with `vol` from a blend of fast and slow moving averages of squared returns
   and `F` a fat-tailed (Student-t) distribution.
3. **Conservative probability.** The price is assumed to move one standard
   deviation against the trade during the order's latency before the edge is
   measured. Close to expiry and close to the strike this wipes out most
   apparent edges, which is the point.
4. **Edge after fees.** For each ask level: `edge = P − price − fee`, with
   Polymarket's taker fee `rate × (p × (1 − p))^exponent` read from the market.
   It takes levels only while `edge ≥ min_edge`.
5. **Size.** Fractional Kelly, capped per order, per window and by bankroll.
6. **Exit.** If the bid ever exceeds what the shares are worth by `exit_edge`
   after fees, it sells; otherwise it holds to settlement.

It will not trade when: the oracle feed is older than 3 seconds, the book is
stale, the local clock disagrees with the oracle's by more than 2.5 seconds,
volatility has not warmed up, the window is in its first 8 or last 6 seconds,
the price is outside 0.08–0.92, the daily loss limit is hit, a loss streak
triggered a cool-down, or a file named `STOP` exists in the working folder.

## What is better than the usual versions of this bot

- **Fees are in the decision**, not discovered afterwards.
- **A priced bet instead of a rule of thumb** ("buy whichever side leads with 30
  seconds left"): the bot only acts when the book disagrees with a calibrated
  probability by more than costs.
- **Adverse-move and latency buffer**, so it does not pay 0.95 for a coin flip
  that looks decided for half a second.
- **Hard risk limits** that can only block trades, never add them.
- **One engine for paper, live and replay**, so a backtest exercises the code
  that trades.
- **Paper fills assume the bot is slow and unlucky**: an order only fills
  against size that was already quoted when the decision was made and stayed
  there for the whole wait (the bot's latency plus the exchange's taker delay).
  Pulled quotes are gone; liquidity that appears later does not count.
- **Flat staking by default**: bets are sized off the starting bankroll, so a
  good run does not inflate later bets and the results. Set `compound = true`
  to reinvest.
- **Streams that go silent are restarted** after 15 seconds instead of leaving
  the bot idle.
- **It checks itself**: local outcome vs. the exchange's resolution, logged as
  `MISMATCH` and counted by `report`.

## Going live

1. Run `paper` for several days. Look at `report`. If return on stake is not
   clearly positive after fees over a few hundred traded windows, do not go
   live. Zero `outcome_mismatches` is a precondition.
2. Fund a Polymarket account you are legally allowed to use where you live.
   The bot does not and will not work around regional restrictions.
3. Put the wallet key in the environment (never in a file in this folder):
   `POLYMARKET_PRIVATE_KEY`, and `POLYMARKET_WALLET` if you trade through a
   wallet other than the key's default one.
4. Start small: `--bankroll 50` with a `config.toml` lowering `max_trade_usd`
   and `max_window_usd`.
5. `bankroll` is the bot's own ledger, not your wallet balance. Winning shares
   have to be redeemed on Polymarket before that money is spendable again; the
   bot does not redeem.

If an order's outcome is ever unknown (network error mid-order, implausible
fill amounts, an order that never resolves), the bot writes the `STOP` file and
stops opening positions until you delete it.

## Known limits

- **The Polymarket connection has not been run against the live exchange.**
  It was written against the official SDK (`polymarket-client` 0.18) and
  tested against that SDK's own types, but the network this was built on
  blocks polymarket.com. The first paper session is the real integration test:
  watch for the strike appearing, books updating and `SETTLE` lines agreeing
  with the site.
- The live order path is tested only against a stand-in client. Fees on live
  fills are estimated from the fee formula; the exchange's figures are the truth.
- The public oracle price stream is marked deprecated in the SDK. If no prices
  arrive, `--auth-prices` switches to the authenticated stream (needs the key).
- `sim` uses invented order books. Its profit means nothing about the real
  market; it exists to test the plumbing.
- `calibrate` uses Coinbase prices, not the Chainlink stream the market settles
  on. Close, not identical. The default `vol_mult = 1.15` comes from one day of
  Coinbase data (288 windows), where the unadjusted model was slightly
  overconfident. Re-run it on your own recordings after a few days of paper
  trading: `python -m btc5m calibrate --recordings "data/recordings/*.jsonl"`.

## Layout

```
btc5m/
  config.py         settings
  model.py          volatility estimate and P(up)
  book.py           order book and fee curve
  strategy.py       edge, sizing, entry/exit decisions (pure functions)
  risk.py           account-level limits
  broker.py         paper fills
  engine.py         window lifecycle, settlement, glue
  polymarket_io.py  market discovery, streams, live orders (official SDK)
  journal.py        CSV logs and event recording
  analysis.py       where the profit or loss came from
  dashboard.py      read-only results page
  history.py        replay over real past trades and official results
  coinbase.py       price download
  calibrate.py      model scoring
  sim.py            synthetic market for offline runs
tests/              48 tests, no network needed
```
