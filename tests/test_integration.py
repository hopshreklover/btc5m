"""Simulator, calibration, recording round-trip, and compatibility with the
installed Polymarket SDK (no network needed for any of it)."""

import asyncio
import inspect
import math
import random
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from btc5m.broker import PaperBroker
from btc5m.calibrate import report
from btc5m.config import Config
from btc5m.engine import Engine
from btc5m.journal import Journal, Recorder, read_events
from btc5m.sim import run_sim, synth_events
from btc5m.strategy import UP, Order

CFG = Config(status_interval_s=0.0, kill_file="")
T0 = 1_800_000_000 - 1_800_000_000 % 300


def series(hours=3.0, vol=7e-5, seed=5):
    rng = random.Random(seed)
    p, out = 70000.0, []
    for i in range(int(hours * 3600)):
        p *= math.exp(rng.gauss(0, vol))
        out.append((T0 - 600 + i, round(p, 2)))
    return out


def test_sim_efficient_market_gives_no_trades():
    eng = run_sim(series(), CFG, log=lambda _m: None, mm_lag_s=0, mm_noise=0.0, mm_spread=0.02)
    s = eng.summary()
    assert s["windows_seen"] >= 30
    assert s["windows_traded"] == 0 and s["total_pnl"] == 0


def test_sim_slow_market_maker_is_traded_and_accounts_add_up():
    eng = run_sim(series(), CFG, log=lambda _m: None, mm_lag_s=4, mm_noise=0.02)
    s = eng.summary()
    assert s["windows_traded"] > 5
    assert s["final_bankroll"] == pytest.approx(CFG.bankroll + s["total_pnl"], abs=0.02)
    for res in eng.results:
        assert res["spent"] <= CFG.max_window_usd + 1e-6
        assert res["buys"] <= CFG.max_trades_per_window
        assert res["pnl"] >= -res["spent"] - 1e-6            # can never lose more than staked
    assert all(f.shares == 0 or 0 < f.price < 1 for f in eng.fills)


def test_daily_loss_limit_stops_trading_in_sim():
    cfg = replace(CFG, daily_loss_limit=1.0, max_loss_streak=99)
    eng = run_sim(series(), cfg, log=lambda _m: None, mm_lag_s=4, mm_noise=0.02)
    running, tripped_at = 0.0, None
    for i, res in enumerate(eng.results):
        if tripped_at is not None and i > tripped_at + 1:
            assert res["buys"] == 0
        running += res["pnl"]
        if tripped_at is None and running <= -1.0:
            tripped_at = i


def test_calibration_on_gaussian_walk_is_sensible():
    rep = report(series(hours=48, seed=12), replace(CFG, student_nu=200.0))
    brier, loss = rep["current"]
    assert brier < 0.16 and loss < 0.45                       # far better than 50/50
    assert len(rep["buckets"]) == 10
    for n, sp, sy in rep["buckets"].values():
        assert n >= 200 and abs(sp / n - sy / n) < 0.06       # predicted ~ realised
    assert rep["best"][0][0] in (0.9, 1.0, 1.1)               # true vol is recovered


def test_recording_replays_to_identical_result(tmp_path):
    events = list(synth_events(series(hours=1.0), CFG, mm_lag_s=4, mm_noise=0.02))
    rec = Recorder(tmp_path / "rec")
    live = Engine(CFG, PaperBroker(CFG.paper_latency_s), Journal(tmp_path / "paper"),
                  log=lambda _m: None)
    for ev in events:
        rec.write(ev)
        live.on_event(ev)
    rec.close()
    files = sorted((tmp_path / "rec").glob("events-*.jsonl.gz"))
    assert files
    replay = Engine(CFG, PaperBroker(CFG.paper_latency_s), log=lambda _m: None)
    for ev in read_events(files):
        replay.on_event(ev)
    assert replay.summary() == live.summary()
    assert live.summary()["windows_traded"] > 0
    assert (tmp_path / "paper" / "windows.csv").exists()
    assert (tmp_path / "paper" / "trades.csv").read_text().count("\n") > 1


def test_series_from_recordings_fills_small_gaps_only(tmp_path):
    from btc5m.calibrate import series_from_recordings

    rec = Recorder(tmp_path)
    for pts, p in ((T0, 100.0), (T0 + 1, 101.0), (T0 + 4, 102.0), (T0 + 60, 103.0)):
        rec.write({"t": "px", "ts": pts + 0.3, "src": "chainlink", "p": p, "pts": float(pts)})
    rec.write({"t": "px", "ts": T0 + 2.0, "src": "spot", "p": 999.0})
    rec.close()
    got = series_from_recordings([str(f) for f in tmp_path.glob("*.jsonl.gz")])
    assert got == [(T0, 100.0), (T0 + 1, 101.0), (T0 + 2, 101.0), (T0 + 3, 101.0),
                   (T0 + 4, 102.0), (T0 + 60, 103.0)]


# ------------------------------------------------------------ SDK compatibility
polymarket = pytest.importorskip("polymarket")


def test_sdk_surface_the_bot_relies_on():
    from polymarket import AsyncPublicClient, AsyncSecureClient
    from polymarket.streams import CryptoPriceSpec, CryptoPricesSpec, MarketSpec

    assert "slug" in inspect.signature(AsyncPublicClient.get_event).parameters
    params = inspect.signature(AsyncSecureClient.place_market_order).parameters
    for name in ("token_id", "side", "amount", "max_spend", "max_price", "shares",
                 "min_price", "order_type"):
        assert name in params
    assert "order_id" in inspect.signature(AsyncSecureClient.get_order).parameters
    for name in ("private_key", "wallet"):
        assert name in inspect.signature(AsyncSecureClient.create).parameters
    assert MarketSpec(token_ids=["1", "2"], custom_feature_enabled=True).asset_ids == ("1", "2")
    assert CryptoPricesSpec(topic="prices.crypto.chainlink", symbols=["btc/usd"]).symbols == ("btc/usd",)
    assert CryptoPriceSpec(symbols=["btcusd"], provider="chainlink").symbols == ("btcusd",)


def test_market_stream_events_convert():
    from polymarket.streams import (
        MarketBookEvent, MarketPriceChangeEvent, MarketResolvedEvent,
    )

    from btc5m.polymarket_io import market_events

    cond = "0x" + "ab" * 32
    book = MarketBookEvent.model_validate({"type": "book", "payload": {
        "market": cond, "asset_id": "111",
        "bids": [{"price": "0.48", "size": "30"}, {"price": "0.49", "size": "20"}],
        "asks": [{"price": "0.52", "size": "25"}],
    }})
    assert market_events(book, 5.0) == [{
        "t": "book", "ts": 5.0, "tok": "111",
        "bids": [[0.48, 30.0], [0.49, 20.0]], "asks": [[0.52, 25.0]],
    }]
    change = MarketPriceChangeEvent.model_validate({"type": "price_change", "payload": {
        "market": cond,
        "price_changes": [{"asset_id": "111", "price": "0.5", "size": "0", "side": "SELL"}],
    }})
    assert market_events(change, 6.0) == [
        {"t": "delta", "ts": 6.0, "tok": "111", "side": "SELL", "p": 0.5, "s": 0.0}]
    done = MarketResolvedEvent.model_validate({"type": "market_resolved", "payload": {
        "id": "9", "market": cond, "winning_asset_id": "111", "winning_outcome": "Up",
    }})
    assert market_events(done, 7.0) == [{"t": "resolved", "ts": 7.0, "tok": "111"}]


def _fake_event(start, **over):
    trading = SimpleNamespace(
        fees_enabled=True, minimum_order_size=5,
        fee_schedule=SimpleNamespace(rate="0.07", exponent=1),
    )
    market = SimpleNamespace(
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(label="Up", token_id="111"),
            no=SimpleNamespace(label="Down", token_id="222"),
        ),
        trading=trading,
        state=SimpleNamespace(end_date=datetime.fromtimestamp(start + 300, timezone.utc)),
    )
    ev = SimpleNamespace(slug=f"btc-updown-5m-{start}", markets=(market,), metadata=None)
    for key, value in over.items():
        setattr(ev, key, value)
    return ev


def test_window_event_from_sdk_event():
    from btc5m.polymarket_io import window_event

    win = window_event(_fake_event(T0, metadata={"priceToBeat": 70123.5}), T0, CFG)
    assert (win["up"], win["down"], win["end"]) == ("111", "222", T0 + 300)
    assert (win["fee_rate"], win["fee_exp"], win["strike"]) == (0.07, 1.0, 70123.5)
    assert win["taker_delay_s"] == 0.0
    ev = _fake_event(T0)
    ev.markets[0].trading.taker_delay_ms = 250
    assert window_event(ev, T0, CFG)["taker_delay_s"] == 0.25
    ev.markets[0].trading.seconds_delay = 3
    assert window_event(ev, T0, CFG)["taker_delay_s"] == 3.0
    assert "strike" not in window_event(_fake_event(T0), T0, CFG)
    ev = _fake_event(T0)
    ev.markets[0].trading.fee_schedule = None        # not reported -> assume default, not zero
    assert window_event(ev, T0, CFG)["fee_rate"] == CFG.default_fee_rate
    ev.markets[0].trading.fees_enabled = False
    assert window_event(ev, T0, CFG)["fee_rate"] == 0.0
    with pytest.raises(ValueError):
        window_event(_fake_event(T0), T0 + 300, CFG)             # wrong window
    ev = _fake_event(T0)
    ev.markets[0].outcomes.yes.label = "Yes"
    with pytest.raises(ValueError):
        window_event(ev, T0, CFG)


# ------------------------------------------------------------------ live broker
class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def place_market_order(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def get_order(self, *, order_id):
        return SimpleNamespace(size_matched="7", status="matched")


def _live(result, tmp_path, order):
    from btc5m.polymarket_io import LiveBroker

    cfg = replace(CFG, kill_file=str(tmp_path / "STOP"))
    client = FakeClient(result)

    async def go():
        br = LiveBroker(client, cfg, lambda _m: None)
        br.fee_lookup = lambda _s: (0.07, 1.0)
        br.submit(order)
        assert br.busy
        while br.busy:
            await asyncio.sleep(0.01)
        return br.poll(0.0, None)

    return asyncio.run(go()), client, tmp_path / "STOP"


BUY = Order(T0, UP, "BUY", "111", 10.0, 17.0, 0.57, 100.0, 0.7, 0.68, 0.1)
SELL = Order(T0, UP, "SELL", "111", 0.0, 12.0, 0.55, 100.0, 0.4, 0.42, 0.1)


def test_live_broker_buy_and_sell_requests(tmp_path):
    ok = SimpleNamespace(ok=True, status="matched", order_id="o1",
                         making_amount="9.69", taking_amount="17")
    (fill,), client, stop = _live(ok, tmp_path, BUY)
    assert client.calls == [{"token_id": "111", "side": "BUY", "amount": "10.00",
                             "max_spend": "10.00", "max_price": "0.5700", "order_type": "FAK"}]
    assert fill.shares == 17.0 and fill.price == pytest.approx(0.57) and fill.fee > 0
    assert not stop.exists()

    ok = SimpleNamespace(ok=True, status="matched", order_id="o2",
                         making_amount="12", taking_amount="6.6")
    (fill,), client, _ = _live(ok, tmp_path, SELL)
    assert client.calls == [{"token_id": "111", "side": "SELL", "shares": "12.00",
                             "min_price": "0.5500", "order_type": "FAK"}]
    assert fill.shares == 12.0 and fill.price == pytest.approx(0.55)


def test_live_broker_rejection_and_delay(tmp_path):
    no = SimpleNamespace(ok=False, code="fak_not_filled", message="no match")
    (fill,), _, stop = _live(no, tmp_path, BUY)
    assert fill.shares == 0 and "fak_not_filled" in fill.note and not stop.exists()

    delayed = SimpleNamespace(ok=True, status="delayed", order_id="o3",
                              making_amount="0", taking_amount="0")
    (fill,), _, stop = _live(delayed, tmp_path, BUY)
    assert fill.shares == 7.0 and fill.price == pytest.approx(0.57) and not stop.exists()


def test_live_broker_halts_when_outcome_unknown(tmp_path):
    (fill,), _, stop = _live(ConnectionError("socket died"), tmp_path, BUY)
    assert fill.shares == 0 and "UNKNOWN" in fill.note
    assert stop.exists()                       # no new entries until a human checks

    from polymarket.errors import RateLimitError, RequestRejectedError

    for exc_type in (RequestRejectedError, RateLimitError):
        exc = exc_type.__new__(exc_type)           # definitely not placed: no halt
        sub = tmp_path / exc_type.__name__
        sub.mkdir()
        (fill,), _, stop_ok = _live(exc, sub, BUY)
        assert fill.shares == 0 and "rejected" in fill.note and not stop_ok.exists()

    broken = SimpleNamespace(ok=True, status="matched", order_id="o5",
                             making_amount="not a number", taking_amount="1")
    sub = tmp_path / "broken"
    sub.mkdir()
    (fill,), _, stop_b = _live(broken, sub, BUY)
    assert "UNKNOWN" in fill.note and stop_b.exists()

    bad = SimpleNamespace(ok=True, status="matched", order_id="o4",
                          making_amount="17", taking_amount="9.69")   # price > 1
    (fill,), _, stop2 = _live(bad, tmp_path, BUY)
    assert "IMPLAUSIBLE" in fill.note and stop2.exists()


def test_recorder_survives_truncation_and_prunes_old_files(tmp_path):
    import os
    import time as _time

    old = tmp_path / "events-20200101.jsonl.gz"
    old.write_bytes(b"x")
    os.utime(old, (_time.time() - 30 * 86400,) * 2)
    rec = Recorder(tmp_path, keep_days=14)
    for i in range(200):
        rec.write({"t": "px", "ts": T0 + i, "src": "chainlink", "p": 100.0 + i, "pts": T0 + i})
    rec.close()
    assert not old.exists()
    (path,) = tmp_path.glob("*.gz")
    assert len(list(read_events([path]))) == 200
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])               # killed mid-write
    assert 0 < len(list(read_events([path]))) < 200


def test_stream_silence_is_treated_as_a_failure():
    from btc5m.polymarket_io import FeedStalled, _events

    class Handle:
        def __init__(self, items):
            self.items = list(items)

        async def __anext__(self):
            if not self.items:
                await asyncio.sleep(60)
            item = self.items.pop(0)
            if item is StopAsyncIteration:
                raise StopAsyncIteration
            return item

    async def collect(items):
        return [x async for x in _events(Handle(items), 0.05)]

    assert asyncio.run(collect([1, 2, StopAsyncIteration])) == [1, 2]
    with pytest.raises(FeedStalled):
        asyncio.run(collect([1, 2]))


def test_report_and_dashboard_from_logs(tmp_path):
    from btc5m import analysis
    from btc5m.dashboard import page

    assert "No windows logged yet" in page(tmp_path / "empty", "paper")
    eng = Engine(CFG, PaperBroker(CFG.paper_latency_s), Journal(tmp_path / "paper"),
                 log=lambda _m: None)
    for ev in synth_events(series(hours=2.0), CFG, mm_lag_s=4, mm_noise=0.02):
        eng.on_event(ev)
    windows, trades = analysis.load(tmp_path / "paper")
    s = analysis.summary(windows)
    assert s["windows_traded"] == eng.summary()["windows_traded"] > 0
    assert s["total_pnl"] == pytest.approx(eng.summary()["total_pnl"], abs=0.05)
    d = analysis.detail(windows, trades)
    assert d["buys"] == sum(b["buys"] for b in d["by_price"]) == sum(
        b["buys"] for b in d["by_time_left"])
    assert d["buys"] > 0 and analysis.equity(windows)[-1] == pytest.approx(s["total_pnl"], abs=0.05)
    assert "by entry price" in analysis.render(tmp_path / "paper")

    with (tmp_path / "paper" / "windows.csv").open("a", encoding="utf-8") as fh:
        fh.write("2030-01-01T00:00:00.000Z,x,1,2,UP,0,0,0,0,0,200,<script>alert(1)</script>\n")
    html = page(tmp_path / "paper", "paper")
    assert "Profit / loss" in html and "<polyline" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
