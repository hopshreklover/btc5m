"""Nested-market pair strategy: pure logic, no network."""

import pytest

from btc5m.pairs import PairPaper, TickBuffer, choose_pair, fee, pair_quote, twap


def asks(u5=None, d5=None, u15=None, d15=None, size=100.0):
    def lv(p):
        return (p, size) if p is not None else (None, 0.0)
    return {("5m", "up"): lv(u5), ("5m", "down"): lv(d5),
            ("15m", "up"): lv(u15), ("15m", "down"): lv(d15)}


def test_twap_needs_coverage_and_averages_the_last_minute():
    ticks = [(t, 100.0 + t) for t in range(0, 121)]
    assert twap(ticks, 120) == pytest.approx(100.0 + sum(range(61, 121)) / 60)
    assert twap([(t, 1.0) for t in range(100, 121)], 120) is None      # only 20 s seen
    buf = TickBuffer(keep_s=200)
    for t in range(0, 500):
        buf.add(t, 5.0)
    assert buf.ticks[0][0] >= 299 and buf.twap(400) == 5.0
    assert buf.twap(600) is None                                       # not reached yet


def test_choose_pair_is_the_one_that_cannot_pay_zero():
    assert choose_pair(64000.0, 64010.0, 3.0) == "a"      # 5m strike below: 5m Up + 15m Down
    assert choose_pair(64010.0, 64000.0, 3.0) == "b"
    assert choose_pair(64000.0, 64001.0, 3.0) is None     # too close to call
    assert choose_pair(None, 64000.0, 3.0) is None


def test_pair_quote_adds_both_fees():
    cost, size, p5, p15 = pair_quote(asks(u5=0.40, d15=0.55, size=30.0), "a")
    assert cost == pytest.approx(0.95 + fee(0.40) + fee(0.55))
    assert (size, p5, p15) == (30.0, 0.40, 0.55)
    assert pair_quote(asks(u5=0.40), "a")[0] == 9.0                     # one leg missing


def test_paper_buy_needs_two_cheap_snapshots_and_pays_by_official_winners():
    pp = PairPaper(max_cost=0.99, usd=20.0)
    cheap = asks(u5=0.40, d15=0.50, d5=0.62, u15=0.52)
    assert pp.on_snapshot(1.0, 900, cheap, 64000.0, 64010.0) is None    # first sighting
    t = pp.on_snapshot(2.0, 900, cheap, 64000.0, 64010.0)
    assert t is not None and t.pair == "a" and t.cost < 0.99
    assert t.shares == pytest.approx(round(20.0 / t.cost, 2))
    assert pp.on_snapshot(3.0, 900, cheap, 64000.0, 64010.0) is None    # one trade per market
    # final between the strikes: both legs pay
    (s,) = pp.settle(900, "UP", "DOWN")
    assert s.payoff == 2.0 and s.pnl == pytest.approx(s.shares * (2.0 - s.cost))
    pp2 = PairPaper()
    pp2.on_snapshot(1.0, 1800, cheap, 64000.0, 64010.0)
    t2 = pp2.on_snapshot(2.0, 1800, cheap, 64000.0, 64010.0)
    (s2,) = pp2.settle(1800, "DOWN", "DOWN")                            # only the 15m leg
    assert s2.payoff == 1.0 and t2.pnl > 0


def test_vanishing_or_expensive_quotes_are_not_bought():
    pp = PairPaper(max_cost=0.99)
    cheap = asks(u5=0.40, d15=0.50)
    dear = asks(u5=0.48, d15=0.55)
    pp.on_snapshot(1.0, 900, cheap, 64000.0, 64010.0)
    assert pp.on_snapshot(2.0, 900, dear, 64000.0, 64010.0) is None     # gone on the next look
    assert pp.on_snapshot(3.0, 900, cheap, 64000.0, 64010.0) is None    # starts over
    thin = asks(u5=0.40, d15=0.50, size=2.0)
    pp3 = PairPaper()
    pp3.on_snapshot(1.0, 900, thin, 64000.0, 64010.0)
    assert pp3.on_snapshot(2.0, 900, thin, 64000.0, 64010.0) is None    # under 5 shares
    pp4 = PairPaper()
    pp4.on_snapshot(1.0, 900, cheap, 64000.0, 64001.0)
    assert pp4.on_snapshot(2.0, 900, cheap, 64000.0, 64001.0) is None   # strikes too close


def test_resume_restores_open_trades_and_unsettled_markets(tmp_path):
    from btc5m.pairs_live import resume

    (tmp_path / "trades.csv").write_text(
        "ts,start15,pair,shares,p5,p15,cost,k5_est,k15_est\n"
        "1000.0,900,a,20.0,0.4,0.5,0.9325,100.0,101.0\n"
        "2000.0,1800,b,10.0,0.45,0.5,0.9849,101.0,100.0\n", encoding="utf-8")
    (tmp_path / "results.csv").write_text(
        "start15,pair,official_pair,shares,cost,payoff,pnl\n900,a,a,20.0,0.9325,1.0,1.35\n",
        encoding="utf-8")
    (tmp_path / "settled.csv").write_text(
        "start15,strike5,strike15,winner5,winner15,final5,final15,k5_est,k15_est\n"
        "900,100,101,UP,UP,1,1,100,101\n", encoding="utf-8")
    (tmp_path / "books-20270101.csv").write_text(
        "ts,start15,sec5,cost_a,size_a,cost_b,size_b,k5_est,k15_est\n"
        "2700.5,2700,10,0.98,9,1.3,9,99.0,98.0\n"
        "9999.0,9900,10,0.98,9,1.3,9,99.0,98.0\n", encoding="utf-8")
    pp = PairPaper()
    todo = resume(tmp_path, pp, now=4000.0)
    assert sorted(todo) == [1800, 2700]              # 900 settled; 9900 not finished yet
    assert [t.start15 for t in pp.trades] == [1800] and todo[2700] == {"k5": 99.0, "k15": 98.0}
    assert pp.on_snapshot(1.0, 1800, asks(u5=0.4, d15=0.5), 64000.0, 64010.0) is None  # already traded
    (s,) = pp.settle(1800, "DOWN", "UP")
    assert s.payoff == 2.0
