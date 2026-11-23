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
| `python -m btc5m paper` | Trade the live markets with pretend money. Logs to `data/paper/` (`data/paper-rest/` with `strategy = "rest"`); records prices, trades and one book snapshot per second to `data/recordings/` (compressed, 14 days kept). |
| `python -m btc5m report` | Summarise logged results, then break the buys down by entry price and by time left: what the bot predicted versus how often it actually won. `--mode NAME` reads another log folder. |
| `python -m btc5m dashboard --host 0.0.0.0 --port 8501` | Serve the same results as a read-only web page (refreshes every minute). Defaults to localhost only. |
| `python -m btc5m history --days 7` | Replay the bot over real history: every real trade and the official winner of each past market, with the bot's price feed delayed by 0.4, 1.4 and 2.4 seconds (the live feed measured 1.4), once for each settlement rule (`instant` and `avg60`). Downloads public data (resumable) into `data/history/`; results land in `data/history-RULE-lagN/` for `report --mode history-avg60-lag1.4`. Prices, sizes and winners are real; that the bot would have reached each quote first is an assumption. It also re-settles the paper and live logs with the official winners. |
| `python -m btc5m check --days 14` | Is it holding up? Downloads the latest markets (resumable), replays the configured strategy over the last 14 days of real trades and official winners, re-settles the paper log with the official winners, and writes `data/check.json`, which the dashboard shows day by day. Meant to run every few hours from a timer. |
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

## What four weeks of real history say

Tested on every real trade and official result from Oct 19 to Nov 16 (8,000
markets): rules were tuned on the first three weeks and the last week was used
once, as a test.

- **Buying at the ask on the model's say-so does not work**, with the oracle
  feed or a 2-second-faster exchange feed, at any threshold, time of window or
  price band that still trades 3+ times an hour. Results swing from profit to
  loss with the assumptions you cannot check (fill price within a second,
  timestamp alignment) and flip sign between periods. The market's own price is
  a better predictor than the model.
- **Two traps in replays** were found and fixed: published trade times run about
  2.3 s behind the real trade, and a book rebuilt from one second's trades can
  look crossed exactly when the price moved after the decision. Either one, left
  in, turns a losing rule into a winning one.
- **Cheap long-shot shares are overpriced**: buying them at 0.10–0.20 lost
  15–25% per dollar in every week. The one consistently non-negative rule found
  is to sit on the other side of that: the `rest` strategy below.

## Strategies

`strategy = "take"` (default) is everything in "How it decides" above.

`strategy = "rest"` (paper only) keeps a bid on the favourite while its best bid
is 0.80–0.90, from 8 s to 240 s into the market, $10 per order, one fill per
market (once a side fills, the other side's bid is cancelled: holding both at
0.80+ would lock in a loss). That is the same as an offer on the long shot at 0.10–0.20.
Makers pay no fee here. Paper fills are strict: a fill is booked only when the
favourite trades below the bid, so the whole queue at that price was used up.
Trades exactly at the bid are logged per market as `touch-only` with what they
would have made. Real-trade replay, per dollar staked:

| Period | Strict fills | Generous fills (touches count) | Fills/hour |
| --- | --- | --- | --- |
| Oct 19 – Nov 9 (tuning) | +0.4%, +$206 ± $339 | +1.1% | 11.5 |
| Nov 9 – 16 (first test) | +1.2%, +$243 ± $195 | +2.0% | 11.7 |
| Nov 16 – 18 (fresh: 708 markets nothing was fitted to) | +1.9%, +$133 ± $114 | +3.0% | 11.8 |

Dollar figures are for $10 per fill, taking the first fill in each market.
Positive in all three periods; five weeks combined is about 1.4 standard errors
above zero, so encouraging rather than proven.

**Replaying the bot's own code is less kind.** With orders filled completely
(an earlier version stopped after 5 shares, which happened to dodge large
sweeps), the default rule made +$76 on the tuning weeks, +$234 on the first
test week and −$93 on the fresh days, with the daily loss limit cutting in
often. Swings of about ±$100 every three days are as large as the edge itself.
Bigger orders, several orders per market and wider price bands were tried
(54 combinations): none held up on the test periods, and most went through a
$200 bankroll. `python -m btc5m check` (below) keeps testing the strategy on
each new day; that, not these replays, is the evidence to wait for.

Also tried and rejected (tuned on the older weeks first): sitting out when
Bitcoin is volatile (P&L does not depend on it), bidding 1–5 cents below the
best bid (worse at every depth), selling to favourite buyers instead (−4% to
−15% per dollar: they are the informed side), and restricting fills to parts
of the market (the last minute loses in every period; no earlier slice wins
in all of them). Replaying the bot's own recordings of real order books and
trades (Nov 18–19, 290 markets) with a queue-position fill model
(`rest_fill = "queue"`): −2.4% per dollar.

Other markets (resting bid on the favourite, strict fills, earlier 2/3 → later
1/3 of each period; `research/maker_scan.py`): Ethereum 5m −1.4% → −3.0%,
Solana 5m −2.7% → −5.6%, XRP 5m −2.1% → −5.3% (14 days each); Bitcoin 15m
−3.1% → −0.2% (28 days), Ethereum/Solana/XRP 15m negative or too few fills.
Long-shot overpricing is weaker outside Bitcoin 5m, and on Solana long shots
were if anything cheap. Also rejected on Bitcoin 5m: buying near-certain
favourites (0.90–0.99) in the last minute, which trades under 2 times an hour
and loses at the worst traded price.

**One structural edge found: nested markets.** Each 15-minute Bitcoin market
ends at the same moment as the last 5-minute market inside it, and in all
2,678 pairs over 28 days both settled on exactly the same final price. So
5-minute Up + 15-minute Down (when the 5-minute starting price is at or below
the 15-minute one; the opposite pair otherwise) always pays at least $1, and
$2 when Bitcoin finishes between the two starting prices (31% of markets).
From real trades (`research/nested_arb.py`): the pair cost under $1 after fees
about 14 times a day at the cheapest prices traded (+11% to +15% per pair), but
only about 3 times a day at the worst prices traded (+0.6% to +5%). Both legs
have to be bought before other traders take them, so `research/pair_monitor.py`
(service `btc5m-pairs`, no orders) now records the real order books every
second in the last 5 minutes of each 15-minute market; `research/pair_report.py`
scores them against the official results. Starting prices are not published
during the market, so a live version would have to compute them from the
oracle, as the bot already does.

Small, and not yet proven: which of the two columns is closer to the truth
depends on queue position, which trade data cannot show. Paper results land in
`data/paper-rest/`; the `live` command refuses this strategy until the live
broker handles resting orders.

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
  rest.py           the resting-bid strategy and its paper fills
  check.py          the recurring check against real results
  coinbase.py       price download
  calibrate.py      model scoring
  sim.py            synthetic market for offline runs
tests/              60 tests, no network needed
```
