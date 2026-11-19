import math
import random
from dataclasses import replace

import pytest

from btc5m.book import Book, fee_per_share
from btc5m.broker import PaperBroker, WindowView
from btc5m.config import Config, load_config
from btc5m.engine import Engine
from btc5m.model import VolEstimator, norm_cdf, prob_up, t_cdf, unit_cdf
from btc5m.risk import Risk
from btc5m.strategy import DOWN, UP, Fair, Order, decide, fair_value, plan_buy

# Most tests use the simpler instant-price settlement; the averaged rule has its own.
CFG = Config(status_interval_s=0.0, kill_file="", twap_s=0.0)
START = 1_800_000_000 - 1_800_000_000 % 300


def quiet(_msg):
    pass


# ----------------------------------------------------------------------- model
def test_t_cdf_known_values():
    assert t_cdf(1.0, 1.0) == pytest.approx(0.75, abs=1e-9)          # Cauchy
    assert t_cdf(2.0, 2.0) == pytest.approx(0.908248290, abs=1e-7)
    assert t_cdf(-1.5, 5.0) == pytest.approx(1 - t_cdf(1.5, 5.0), abs=1e-12)
    assert t_cdf(2.015048373, 5.0) == pytest.approx(0.95, abs=1e-6)


def test_unit_cdf_limits():
    for z in (-3, -1, 0, 0.5, 2.5):
        assert unit_cdf(z, 200) == norm_cdf(z)
        assert unit_cdf(z, 1e6) == norm_cdf(z)
    # fat tails: less certain than the normal far from the strike
    assert unit_cdf(4.0, 4.0) < norm_cdf(4.0)
    assert unit_cdf(-4.0, 4.0) > norm_cdf(-4.0)


def test_prob_up_behaviour():
    sig = 7e-5
    assert prob_up(100, 100, 120, sig) == pytest.approx(0.5)
    assert prob_up(100, 100, 0, sig) == 1.0           # a tie resolves Up
    assert prob_up(99.99, 100, 0, sig) == 0.0
    ps = [prob_up(s, 70000, 120, sig) for s in (69900, 69980, 70000, 70020, 70100)]
    assert ps == sorted(ps) and ps[0] < 0.1 and ps[-1] > 0.9
    # same distance from the strike matters more with less time left
    assert prob_up(70030, 70000, 20, sig) > prob_up(70030, 70000, 240, sig)


def test_vol_estimator_recovers_known_vol():
    rng = random.Random(1)
    true = 8e-5
    vol = VolEstimator(fast_halflife_s=600, slow_halflife_s=3600)
    p = 70000.0
    for t in range(20000):
        p *= math.exp(rng.gauss(0, true))
        vol.update(t, p)
    assert vol.ready
    assert vol.sigma == pytest.approx(true, rel=0.12)


def test_vol_estimator_ignores_gaps_and_duplicates():
    vol = VolEstimator(min_samples=1)
    vol.update(0, 100.0)
    vol.update(0, 105.0)       # duplicate timestamp
    vol.update(500, 150.0)     # reconnect gap: must not become one huge return
    assert vol.samples == 0
    vol.update(501, 150.0)
    assert vol.samples == 1


# ------------------------------------------------------------------ book & fee
def test_fee_curve():
    assert fee_per_share(0.5, 0.07, 1) == pytest.approx(0.0175)
    assert fee_per_share(0.9, 0.07, 1) == pytest.approx(0.0063)
    assert fee_per_share(0.5, 0.25, 2) == pytest.approx(0.015625)
    assert fee_per_share(0.5, 0.0, 1) == 0.0
    assert fee_per_share(1.0, 0.07, 1) == 0.0


def test_book_updates():
    b = Book()
    b.set_snapshot([[0.48, 100], [0.47, 50]], [[0.52, 80], [0.50, 20], [0.55, 0]], ts=1)
    assert b.best_bid() == 0.48 and b.best_ask() == 0.50
    b.apply("SELL", 0.50, 0, ts=2)
    assert b.best_ask() == 0.52 and b.ts == 2
    b.apply("BUY", 0.49, 10, ts=3)
    assert b.bids_sorted()[0] == (0.49, 10.0)
    b.consume("BUY", 0.49, 4)
    assert b.bids[0.49] == pytest.approx(6.0) and b.ts == 3
    b.consume("BUY", 0.49, 6)
    assert 0.49 not in b.bids


# -------------------------------------------------------------------- strategy
def _books(up_bid, up_ask, size=200.0):
    up, down = Book(), Book()
    up.set_snapshot([[up_bid, size]], [[up_ask, size]], ts=0)
    down.set_snapshot([[round(1 - up_ask, 2), size]], [[round(1 - up_bid, 2), size]], ts=0)
    return {UP: up, DOWN: down}


def _decide(fair, books, held=None, allow=True, cfg=CFG, budget=30.0):
    return decide(
        cfg, window=START, now=10.0, fair=fair, books=books,
        tokens={UP: "u", DOWN: "d"}, held=held or {UP: 0.0, DOWN: 0.0},
        fee_rate=0.07, fee_exp=1.0, min_size=5.0, bankroll=200.0,
        window_budget_left=budget, allow_entry=allow,
    )


def test_no_trade_without_edge():
    fair = Fair(p_up=0.50, p_up_lo=0.49, p_up_hi=0.51, sigma=7e-5)
    assert _decide(fair, _books(0.49, 0.51)) is None
    # fair is above the ask, but not by enough to pay the fee and the edge buffer
    fair = Fair(p_up=0.55, p_up_lo=0.54, p_up_hi=0.56, sigma=7e-5)
    assert _decide(fair, _books(0.49, 0.51)) is None


def test_buys_cheap_side_and_respects_caps():
    fair = Fair(p_up=0.70, p_up_lo=0.68, p_up_hi=0.72, sigma=7e-5)
    o = _decide(fair, _books(0.55, 0.57))
    assert o is not None and (o.side, o.action) == (UP, "BUY")
    assert o.usd <= CFG.max_trade_usd + 1e-9 and o.limit == 0.57
    assert o.edge == pytest.approx(0.68 - 0.57 - fee_per_share(0.57, 0.07, 1))
    # the Down side is bought when Up is overpriced
    fair = Fair(p_up=0.30, p_up_lo=0.28, p_up_hi=0.32, sigma=7e-5)
    o = _decide(fair, _books(0.43, 0.45))
    assert o is not None and o.side == DOWN and o.token == "d"
    # entries blocked
    assert _decide(fair, _books(0.43, 0.45), allow=False) is None
    # window budget nearly used up
    assert _decide(fair, _books(0.43, 0.45), budget=0.5) is None


def test_never_holds_both_sides_and_skips_tails():
    fair = Fair(p_up=0.30, p_up_lo=0.28, p_up_hi=0.32, sigma=7e-5)
    books = _books(0.43, 0.45)                 # Down is cheap here...
    books[UP].set_snapshot([[0.26, 200]], [[0.29, 200]], ts=0)   # ...and Up is fairly priced
    assert _decide(fair, books, held={UP: 10.0, DOWN: 0.0}) is None
    assert _decide(fair, books).side == DOWN   # flat, the same book is a buy
    fair = Fair(p_up=0.99, p_up_lo=0.985, p_up_hi=0.995, sigma=7e-5)
    assert _decide(fair, _books(0.93, 0.94)) is None      # above max_price


def test_sells_when_market_overpays():
    fair = Fair(p_up=0.40, p_up_lo=0.38, p_up_hi=0.42, sigma=7e-5)
    o = _decide(fair, _books(0.55, 0.57), held={UP: 20.0, DOWN: 0.0}, allow=False)
    assert o is not None and (o.side, o.action, o.shares, o.limit) == (UP, "SELL", 20.0, 0.55)
    # not rich enough to be worth the fee
    assert _decide(fair, _books(0.44, 0.46), held={UP: 20.0, DOWN: 0.0}, allow=False) is None


def test_plan_buy_walks_only_levels_with_edge():
    b = Book()
    b.set_snapshot([], [[0.50, 10], [0.55, 10], [0.62, 10]], ts=0)
    plan = plan_buy(b, 0.62, 1000.0, CFG, 0.07, 1.0)
    assert plan.shares == 20 and plan.limit == 0.55          # 0.62 has no edge
    plan = plan_buy(b, 0.62, 6.0, CFG, 0.07, 1.0)
    assert plan.limit == 0.55 and plan.cost + plan.fees <= 6.0 + 1e-9


def test_fair_value_pessimistic_bounds():
    f = fair_value(CFG, 70010.0, 70000.0, 30.0, 7e-5)
    assert f.p_up_lo < f.p_up < f.p_up_hi
    assert f.pessimistic(DOWN) == pytest.approx(1 - f.p_up_hi)
    assert f.pessimistic(UP) < f.fair(UP) and f.pessimistic(DOWN) < f.fair(DOWN)
    old = fair_value(CFG, 70010.0, 70000.0, 30.0, 7e-5, price_age_s=2.0)
    assert old.p_up == f.p_up and old.p_up_lo < f.p_up_lo and old.p_up_hi > f.p_up_hi


# ---------------------------------------------------------------- paper broker
def _order(action="BUY", usd=10.0, shares=0.0, limit=0.57, ts=100.0, side=UP):
    return Order(START, side, action, "u", usd, shares, limit, ts, 0.7, 0.68, 0.1)


def test_paper_broker_latency_limit_and_fees():
    books = _books(0.55, 0.57, size=8.0)
    books[UP].apply("SELL", 0.60, 100, ts=0)
    view = lambda _s: WindowView(START + 300, books, 0.07, 1.0)  # noqa: E731
    br = PaperBroker(latency_s=0.5)
    br.submit(_order(), view(START))
    assert br.busy and br.poll(100.4, view) == []
    (f,) = br.poll(100.5, view)
    assert not br.busy
    assert f.shares == 8.0 and f.price == pytest.approx(0.57)     # 0.60 is past the limit
    assert f.fee == pytest.approx(8 * fee_per_share(0.57, 0.07, 1))
    assert 0.57 not in books[UP].asks                              # liquidity is gone
    br.submit(_order(ts=101.0), view(START))
    (f,) = br.poll(102.0, view)
    assert f.shares == 0 and f.note == "quotes gone before fill"


def test_paper_broker_only_fills_size_that_stayed_put():
    def setup(delay=0.0):
        books = _books(0.55, 0.57, size=50.0)
        view = lambda _s: WindowView(START + 300, books, 0.07, 1.0, delay)  # noqa: E731
        br = PaperBroker(latency_s=0.5)
        br.submit(_order(usd=100.0, limit=0.58), view(START))
        return books, view, br

    books, view, br = setup()                 # quote pulled, then put back: gone for us
    books[UP].apply("SELL", 0.57, 0, ts=100.1)
    assert br.poll(100.2, view) == []
    books[UP].apply("SELL", 0.57, 50, ts=100.3)
    (f,) = br.poll(100.5, view)
    assert f.shares == 0

    books, view, br = setup()                 # quote shrinks: only the remainder fills
    books[UP].apply("SELL", 0.57, 12, ts=100.1)
    br.poll(100.2, view)
    books[UP].apply("SELL", 0.57, 40, ts=100.3)
    (f,) = br.poll(100.5, view)
    assert f.shares == 12.0

    books, view, br = setup()                 # size that appears later does not count
    books[UP].apply("SELL", 0.56, 500, ts=100.1)
    (f,) = br.poll(100.5, view)
    assert f.shares == 50.0 and f.price == pytest.approx(0.57)

    books, view, br = setup(delay=2.0)        # exchange taker delay is added to the wait
    assert br.poll(102.4, view) == []
    (f,) = br.poll(102.5, view)
    assert f.shares == 50.0


def test_paper_broker_budget_and_sell_and_expiry():
    books = _books(0.55, 0.57, size=1000.0)
    view = lambda _s: WindowView(START + 300, books, 0.07, 1.0)  # noqa: E731
    br = PaperBroker(latency_s=0.0)
    br.submit(_order(usd=10.0), view(START))
    (f,) = br.poll(100.0, view)
    assert f.notional + f.fee <= 10.0 and f.notional + f.fee > 9.9
    br.submit(_order("SELL", usd=0.0, shares=12.0, limit=0.55), view(START))
    (f,) = br.poll(100.0, view)
    assert f.shares == 12.0 and f.price == pytest.approx(0.55)
    br.submit(_order(ts=START + 299.9), view(START))
    (f,) = br.poll(START + 300.0, lambda _s: WindowView(START + 300, books, 0.07, 1.0))
    assert f.shares == 0 and "closed" in f.note


# ---------------------------------------------------------------------- engine
def _warm(engine, until, price=70000.0, n=200, lag=0.05):
    """Feed quiet oracle ticks up to (not including) `until`."""
    rng = random.Random(3)
    for t in range(until - n, until):
        price *= math.exp(rng.gauss(0, 5e-5))
        engine.on_event({"t": "px", "ts": t + lag, "src": "chainlink", "p": price, "pts": float(t)})
    return price


def _win(start, ts):
    return {"t": "win", "ts": ts, "start": start, "end": start + 300, "slug": "x",
            "up": "u", "down": "d", "fee_rate": 0.07, "fee_exp": 1.0, "min_size": 5.0}


def _book_events(ts, up_bid, up_ask, size=200.0):
    return [
        {"t": "book", "ts": ts, "tok": "u", "bids": [[up_bid, size]], "asks": [[up_ask, size]]},
        {"t": "book", "ts": ts, "tok": "d", "bids": [[round(1 - up_ask, 2), size]],
         "asks": [[round(1 - up_bid, 2), size]]},
    ]


def _run_window(engine, final_price, up_bid=0.50, up_ask=0.52, jump=40.0):
    price = _warm(engine, START)
    engine.on_event(_win(START, START - 20))
    engine.on_event({"t": "px", "ts": START + 0.05, "src": "chainlink", "p": price, "pts": float(START)})
    strike = price
    for t in range(START + 1, START + 300):
        px = strike + jump if t >= START + 60 else strike
        engine.on_event({"t": "px", "ts": t + 0.05, "src": "chainlink", "p": px, "pts": float(t)})
        for ev in _book_events(t + 0.2, up_bid, up_ask):
            engine.on_event(ev)
    end = START + 300
    engine.on_event({"t": "px", "ts": end + 0.05, "src": "chainlink",
                     "p": strike + final_price, "pts": float(end)})
    engine.on_event({"t": "clock", "ts": end + 2})
    return strike


def test_engine_trades_and_settles_a_win():
    eng = Engine(CFG, PaperBroker(0.5), log=quiet)
    strike = _run_window(eng, final_price=+25.0)
    (res,) = eng.results
    assert res["strike"] == strike and res["winner"] == UP and res["buys"] >= 1
    buys = [f for f in eng.fills if f.shares > 0]
    assert all(f.order.side == UP and f.order.action == "BUY" for f in buys)
    shares = sum(f.shares for f in buys)
    cost = sum(f.notional + f.fee for f in buys)
    assert res["pnl"] == pytest.approx(shares - cost, abs=1e-3)
    assert res["spent"] <= CFG.max_window_usd + 1e-9
    assert eng.risk.bankroll == pytest.approx(CFG.bankroll + res["pnl"], abs=1e-3)
    assert res["buys"] <= CFG.max_trades_per_window


def test_engine_settles_a_loss():
    eng = Engine(CFG, PaperBroker(0.5), log=quiet)
    _run_window(eng, final_price=-5.0)
    (res,) = eng.results
    assert res["winner"] == DOWN and res["buys"] >= 1
    assert res["pnl"] == pytest.approx(-res["spent"], abs=1e-3)
    assert eng.risk.loss_streak == 1


def test_engine_exchange_resolution_overrides_and_flags_mismatch():
    eng = Engine(CFG, PaperBroker(0.5), log=quiet)
    price = _warm(eng, START)
    eng.on_event(_win(START, START - 20))
    eng.on_event({"t": "px", "ts": START + 0.05, "src": "chainlink", "p": price, "pts": float(START)})
    eng.on_event({"t": "resolved", "ts": START + 299, "tok": "d"})
    eng.on_event({"t": "px", "ts": START + 300.05, "src": "chainlink", "p": price + 10,
                  "pts": float(START + 300)})
    (res,) = eng.results
    assert res["winner"] == DOWN and "MISMATCH" in res["note"]


def test_engine_will_not_trade_without_a_strike():
    eng = Engine(CFG, PaperBroker(0.5), log=quiet)
    price = _warm(eng, START + 100)                 # joined 100s into the window
    eng.on_event(_win(START, START + 100))
    for t in range(START + 100, START + 300):
        eng.on_event({"t": "px", "ts": t + 0.05, "src": "chainlink", "p": price + 60, "pts": float(t)})
        for ev in _book_events(t + 0.2, 0.50, 0.52):
            eng.on_event(ev)
    assert eng.fills == [] and "no strike" in eng.idle_reason


@pytest.mark.parametrize("what", ["stale_price", "stale_book", "skew", "cold_vol"])
def test_engine_data_quality_gates(what):
    eng = Engine(replace(CFG, max_book_age_s=3.0), PaperBroker(0.5), log=quiet)
    lag = 5.0 if what == "skew" else 0.05   # local clock 5s ahead of the oracle's
    if what == "cold_vol":
        price = 70000.0
    else:
        price = _warm(eng, START, lag=lag)
    eng.on_event(_win(START, START - 20))
    eng.on_event({"t": "px", "ts": START + lag, "src": "chainlink", "p": price, "pts": float(START)})
    for t in range(START + 1, START + 80):
        if not (what == "stale_price" and t > START + 20):
            eng.on_event({"t": "px", "ts": t + lag, "src": "chainlink", "p": price + 50, "pts": float(t)})
        if not (what == "stale_book" and t > START + 2):
            for ev in _book_events(t + 0.2, 0.50, 0.52):
                eng.on_event(ev)
        eng.on_event({"t": "clock", "ts": t + 0.9})
    if what == "stale_price":
        assert all(f.order.ts <= START + 20 + CFG.max_price_age_s + 1 for f in eng.fills)
        assert eng.idle_reason == "price feed stale"
    else:
        assert eng.fills == []
        assert {"stale_book": "order book stale", "skew": "clock skew",
                "cold_vol": "warming up"}[what] in eng.idle_reason


# ------------------------------------------------------------------------ risk
def test_risk_limits(tmp_path):
    kill = tmp_path / "STOP"
    cfg = replace(CFG, daily_loss_limit=20.0, max_loss_streak=2, cooldown_windows=3,
                  kill_file=str(kill))
    r = Risk(cfg)
    now = 1_800_000_000.0
    assert r.blocked(now) is None
    r.on_settle(now, -5.0, True)
    r.on_settle(now, -5.0, True)
    assert "cool-down" in r.blocked(now)
    for _ in range(3):
        r.on_window_start(now)
    assert r.blocked(now) is None
    r.on_settle(now, -11.0, True)
    assert "daily loss" in r.blocked(now)
    assert r.blocked(now + 86400) is None           # new UTC day
    kill.write_text("x")
    assert "kill file" in r.blocked(now + 86400)
    assert r.bankroll == pytest.approx(cfg.bankroll - 21.0)


def test_config_validation(tmp_path):
    with pytest.raises(ValueError):
        load_config(None, bankroll=10.0)                       # window cap above bankroll
    with pytest.raises(ValueError):
        load_config(None, nonsense=1)
    p = tmp_path / "c.toml"
    p.write_text("bankroll = 500\nmin_edge = 0.05\n")
    cfg = load_config(str(p), min_edge=None)
    assert cfg.bankroll == 500 and cfg.min_edge == 0.05


def test_flat_staking_does_not_reinvest_wins():
    eng = Engine(CFG, PaperBroker(0.5), log=quiet)
    eng.risk.bankroll = 900.0
    assert eng.sizing_bankroll() == CFG.bankroll           # wins are banked
    eng.risk.bankroll = 120.0
    assert eng.sizing_bankroll() == 120.0                  # losses still shrink bets
    eng = Engine(replace(CFG, compound=True), PaperBroker(0.5), log=quiet)
    eng.risk.bankroll = 900.0
    assert eng.sizing_bankroll() == 900.0


def test_book_snapshots_are_compact_and_replayable():
    eng = Engine(CFG, PaperBroker(0.5), log=quiet)
    eng.on_event(_win(START, START - 20))
    asks = [[round(0.50 + i / 100, 2), 10.0] for i in range(12)]
    bids = [[round(0.49 - i / 100, 2), 10.0] for i in range(12)]
    eng.on_event({"t": "book", "ts": START + 1.0, "tok": "u", "bids": bids, "asks": asks})
    (snap,) = eng.book_snapshots(3)
    assert snap["tok"] == "u" and snap["t"] == "book"
    assert snap["bids"] == [[0.49, 10.0], [0.48, 10.0], [0.47, 10.0]]
    assert snap["asks"] == [[0.5, 10.0], [0.51, 10.0], [0.52, 10.0]]
    eng.on_event({"t": "clock", "ts": START + 301.0})
    assert eng.book_snapshots(3) == []                     # nothing for a finished window


def _tick(eng, pts, p, lag=0.0):
    eng.on_event({"t": "px", "ts": pts + lag, "src": "chainlink", "p": p, "pts": float(pts)})


def test_averaged_settlement_strike_and_final():
    cfg = replace(CFG, twap_s=60.0)
    eng = Engine(cfg, PaperBroker(0.5), log=quiet)
    eng.on_event(_win(START, START - 100))
    for t in range(START - 90, START + 301):
        _tick(eng, t, 70000.0 + (t - START))            # rises $1 a second
    w = eng.windows.get(START) or next(iter(eng.windows.values()))
    res = eng.results[0] if eng.results else None
    # mean of the 60 ticks ending at the boundary: boundary price minus 29.5
    strike = res["strike"] if res else w.strike
    final = res["final"] if res else w.final
    assert strike == pytest.approx(70000.0 - 29.5)
    assert final == pytest.approx(70300.0 - 29.5)

    late = Engine(cfg, PaperBroker(0.5), log=quiet)     # joined 20 s before the start:
    late.on_event(_win(START, START - 30))              # most of the average was not seen
    for t in range(START - 20, START + 5):
        _tick(late, t, 70000.0)
    assert late.windows[START].strike is None and late.windows[START].strike_note == "missed"


def test_averaged_settlement_pricing_terms():
    cfg = replace(CFG, twap_s=60.0)
    eng = Engine(cfg, PaperBroker(0.5), log=quiet)
    eng.on_event(_win(START, START - 100))
    for t in range(START - 90, START + 101):
        _tick(eng, t, 70000.0)
    w = eng.windows[START]
    assert w.strike == pytest.approx(70000.0)
    # 200 s left: the average is as uncertain as a price 160 s ahead
    assert eng.settle_terms(w, 70000.0) == (w.strike, pytest.approx(160.0))
    # Now jump $50 above the strike and hold it through the first half of the last minute.
    for t in range(START + 101, START + 271):
        _tick(eng, t, 70050.0)
    need, horizon = eng.settle_terms(w, 70050.0)
    assert horizon == pytest.approx(10.0)               # 30 s left, averaged
    # half the average is locked $50 high, so the rest may average $50 low and still win
    assert need == pytest.approx(69950.0, abs=2.0)
    p_avg = fair_value(cfg, 70050.0, need, horizon, 5e-5).p_up
    p_point = fair_value(cfg, 70050.0, w.strike, 30.0, 5e-5).p_up
    assert p_avg > p_point > 0.5
    # Instant-price rule is untouched.
    plain = Engine(CFG, PaperBroker(0.5), log=quiet)
    plain.on_event(_win(START, START - 100))
    _tick(plain, START, 70000.0)
    _tick(plain, START + 100, 70010.0)
    assert plain.settle_terms(plain.windows[START], 70010.0) == (70000.0, 200.0)


# ------------------------------------------------------------- resting bids
REST = replace(CFG, strategy="rest")


def _rest_engine(fav_bid=0.85, fav_ask=0.86):
    eng = Engine(REST, PaperBroker(0.5), log=quiet)
    eng.on_event(_win(START, START - 100))
    for t in range(START - 90, START + 1):
        _tick(eng, t, 70000.0)
    up_bids = [[fav_bid, 200.0], [round(fav_bid - 0.01, 2), 200.0]]
    up_asks = [[fav_ask, 200.0]]
    dn = lambda lv: [[round(1 - p, 2), s] for p, s in lv]  # noqa: E731
    eng.on_event({"t": "book", "ts": START + 20.0, "tok": "u", "bids": up_bids, "asks": up_asks})
    eng.on_event({"t": "book", "ts": START + 20.0, "tok": "d", "bids": dn(up_asks), "asks": dn(up_bids)})
    eng.on_event({"t": "clock", "ts": START + 20.15})     # decisions run at most every 0.1 s
    return eng


def _trade(eng, ts, tok, p, s=50.0):
    eng.on_event({"t": "trade", "ts": ts, "tok": tok, "p": p, "s": s})


def test_rest_quotes_the_favourite_and_fills_only_on_a_sweep():
    eng = _rest_engine()
    q = eng.quoter.state[START].quotes
    assert [x.price for x in q[UP]] == [0.85] and q[DOWN] == []      # only the favourite
    _trade(eng, START + 20.2, "u", 0.84)            # before the order is live: nothing
    assert eng.windows[START].buys == 0
    _trade(eng, START + 21.0, "u", 0.85)            # at our price: queue unknown, not booked
    assert eng.windows[START].buys == 0 and eng.quoter.state[START].touch
    _trade(eng, START + 21.5, "d", 0.16, s=5.0)     # Down at 0.16 = Up at 0.84: swept, 5 shares
    _trade(eng, START + 21.6, "u", 0.84, s=50.0)    # the rest of the order fills too
    w = eng.windows[START]
    assert w.buys == 2 and w.held[UP] == pytest.approx(round(REST.rest_usd / 0.85, 2))
    assert w.fees == 0                                               # makers pay no fee
    _trade(eng, START + 22.0, "u", 0.80)            # budget of one order used up
    assert eng.windows[START].buys == 2
    # the favourite flips: the bot must not now bid on Down as well
    eng.on_event({"t": "book", "ts": START + 23.0, "tok": "u", "bids": [[0.14, 99.0]], "asks": [[0.15, 99.0]]})
    eng.on_event({"t": "book", "ts": START + 23.0, "tok": "d", "bids": [[0.85, 99.0]], "asks": [[0.86, 99.0]]})
    eng.on_event({"t": "clock", "ts": START + 23.2})
    assert eng.quoter.state[START].quotes[DOWN] == []
    _trade(eng, START + 24.0, "d", 0.80)
    assert eng.windows[START].buys == 2 and eng.windows[START].held[DOWN] == 0


def test_rest_several_orders_per_market_same_side_only():
    eng = Engine(replace(REST, rest_max_fills=3, rest_refill_s=2.0), PaperBroker(0.5), log=quiet)
    eng.on_event(_win(START, START - 100))
    for t in range(START - 90, START + 1):
        _tick(eng, t, 70000.0)
    filled = []
    for k in range(6):                              # a fresh book and a sweep every 4 s
        ts = START + 20.0 + 4 * k
        eng.on_event({"t": "book", "ts": ts, "tok": "u", "bids": [[0.85, 99.0]], "asks": [[0.86, 99.0]]})
        eng.on_event({"t": "book", "ts": ts, "tok": "d", "bids": [[0.14, 99.0]], "asks": [[0.15, 99.0]]})
        eng.on_event({"t": "clock", "ts": ts + 0.15})
        _trade(eng, ts + 1.0, "u", 0.84, s=100.0)
        filled.append(eng.windows[START].held[UP])
    w = eng.windows[START]
    assert w.buys == 3 and w.held[DOWN] == 0
    assert w.spent == pytest.approx(30.0, abs=0.1)   # three $10 orders, then the budget is used
    assert filled[-1] == filled[-2]


def test_rest_cancels_late_and_settles_with_touch_note():
    eng = _rest_engine()
    for t in range(START + 21, START + 241):        # quote stays up until 240 s
        _tick(eng, t, 70000.0)
    eng.on_event({"t": "book", "ts": START + 240.5, "tok": "u", "bids": [[0.85, 9.0]], "asks": [[0.86, 9.0]]})
    eng.on_event({"t": "book", "ts": START + 240.5, "tok": "d", "bids": [[0.14, 9.0]], "asks": [[0.15, 9.0]]})
    _trade(eng, START + 241.5, "u", 0.80)           # after cancel took effect: no fill
    assert eng.windows[START].buys == 0
    eng2 = _rest_engine()
    _trade(eng2, START + 21.0, "u", 0.85)           # a touch only
    for t in range(START + 1, START + 330):
        _tick(eng2, t, 69990.0 if t > START + 200 else 70000.0)
    res = eng2.results[0]
    assert res["buys"] == 0 and "touch-only 1 pnl" in res["note"]


def test_rest_ignores_sides_outside_the_band_and_bad_config():
    eng = _rest_engine(fav_bid=0.70, fav_ask=0.71)
    q = eng.quoter.state[START].quotes
    assert q[UP] == [] and q[DOWN] == []
    with pytest.raises(ValueError):
        load_config(None, strategy="maker")
    with pytest.raises(ValueError):
        load_config(None, strategy="rest", rest_from_s=250.0, rest_until_s=240.0)


def test_rest_queue_fills_only_after_the_queue_ahead_has_traded():
    eng = Engine(replace(REST, rest_fill="queue"), PaperBroker(0.5), log=quiet)
    eng.on_event(_win(START, START - 100))
    for t in range(START - 90, START + 1):
        _tick(eng, t, 70000.0)
    eng.on_event({"t": "book", "ts": START + 20.0, "tok": "u", "bids": [[0.85, 30.0]], "asks": [[0.86, 99.0]]})
    eng.on_event({"t": "book", "ts": START + 20.0, "tok": "d", "bids": [[0.14, 99.0]], "asks": [[0.15, 30.0]]})
    eng.on_event({"t": "clock", "ts": START + 20.15})
    (q,) = eng.quoter.state[START].quotes[UP]
    assert q.ahead == 30.0
    _trade(eng, START + 21.0, "u", 0.85, s=20.0)          # 10 shares still ahead of us
    assert eng.windows[START].buys == 0 and q.ahead == pytest.approx(10.0)
    _trade(eng, START + 21.5, "d", 0.15, s=14.0)          # same level seen from Down: 4 reach us
    w = eng.windows[START]
    assert w.buys == 1 and w.held[UP] == pytest.approx(4.0)
    _trade(eng, START + 22.0, "u", 0.84, s=50.0)          # a sweep through fills the rest
    assert w.held[UP] == pytest.approx(round(REST.rest_usd / 0.85, 2))
    strict = _rest_engine()                               # the strict rule never fills at the level
    _trade(strict, START + 21.0, "u", 0.85, s=500.0)
    assert strict.windows[START].buys == 0
