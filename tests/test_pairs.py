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
