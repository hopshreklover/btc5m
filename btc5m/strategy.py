"""Decide whether the book is offering a price worth taking.

Pure functions: given model inputs, the two books and the current position,
return at most one Order. No I/O, no clocks, so the same code runs live, on
paper and in replay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .book import Book, fee_per_share
from .config import Config
from .model import prob_up

UP, DOWN = "UP", "DOWN"
SIDES = (UP, DOWN)


@dataclass(frozen=True)
class Order:
    window: int        # window start (epoch seconds)
    side: str          # UP or DOWN
    action: str        # BUY or SELL
    token: str
    usd: float         # BUY: maximum total spend, fees included
    shares: float      # SELL: shares to sell (BUY: expected shares, informational)
    limit: float       # worst acceptable price
    ts: float
    p_fair: float      # model probability for this side
    p_used: float      # conservative probability the decision was based on
    edge: float        # expected edge per share at decision time


@dataclass(frozen=True)
class Fair:
    p_up: float
    p_up_lo: float     # if the price moves against Up before we are filled
    p_up_hi: float     # if it moves against Down
    sigma: float

    def fair(self, side: str) -> float:
        return self.p_up if side == UP else 1.0 - self.p_up

    def pessimistic(self, side: str) -> float:
        return self.p_up_lo if side == UP else 1.0 - self.p_up_hi

    def optimistic(self, side: str) -> float:
        return self.p_up_hi if side == UP else 1.0 - self.p_up_lo


def fair_value(
    cfg: Config, spot: float, strike: float, tau_s: float, sigma: float,
    price_age_s: float = 0.0,
) -> Fair:
    """`price_age_s` is how old the price already is when we see it; the adverse
    move is assumed over that plus the order latency."""
    sigma = sigma * cfg.vol_mult
    blind = max(cfg.latency_s, 0.0) + max(price_age_s, 0.0)
    shift = math.exp(cfg.adverse_sigmas * sigma * math.sqrt(blind))
    return Fair(
        p_up=prob_up(spot, strike, tau_s, sigma, cfg.student_nu),
        p_up_lo=prob_up(spot / shift, strike, tau_s, sigma, cfg.student_nu),
        p_up_hi=prob_up(spot * shift, strike, tau_s, sigma, cfg.student_nu),
        sigma=sigma,
    )


def _floor2(x: float) -> float:
    return math.floor(x * 100.0 + 1e-9) / 100.0


def _ceil2(x: float, cap: float) -> float:
    """Round a spend up to the cent, but never past the budget it was planned under."""
    return min(math.ceil(x * 100.0 - 1e-9) / 100.0, _floor2(cap))


@dataclass(frozen=True)
class _Plan:
    shares: float
    cost: float     # price * shares
    fees: float
    limit: float
    profit: float   # expected profit at the conservative probability


def plan_buy(
    book: Book, p: float, budget: float, cfg: Config, fee_rate: float, fee_exp: float
) -> _Plan | None:
    """Walk the asks while each level still clears `min_edge` after fees."""
    shares = cost = fees = profit = 0.0
    limit = 0.0
    for price, size in book.asks_sorted():
        if price < cfg.min_price or price > cfg.max_price:
            break
        f = fee_per_share(price, fee_rate, fee_exp)
        edge = p - price - f
        if edge < cfg.min_edge:
            break
        take = _floor2(min(size, (budget - cost - fees) / (price + f)))
        if take <= 0:
            break
        shares += take
        cost += take * price
        fees += take * f
        profit += take * edge
        limit = price
    if shares <= 0:
        return None
    return _Plan(shares, cost, fees, limit, profit)


def plan_sell(
    book: Book, hold_value: float, held: float, cfg: Config, fee_rate: float, fee_exp: float
) -> _Plan | None:
    """Walk the bids while selling beats holding by `exit_edge` after fees."""
    shares = cost = fees = profit = 0.0
    limit = 0.0
    for price, size in book.bids_sorted():
        f = fee_per_share(price, fee_rate, fee_exp)
        gain = price - f - hold_value
        if gain < cfg.exit_edge:
            break
        take = _floor2(min(size, held - shares))
        if take <= 0:
            break
        shares += take
        cost += take * price
        fees += take * f
        profit += take * gain
        limit = price
    if shares <= 0:
        return None
    return _Plan(shares, cost, fees, limit, profit)


def decide(
    cfg: Config,
    *,
    window: int,
    now: float,
    fair: Fair,
    books: dict[str, Book],
    tokens: dict[str, str],
    held: dict[str, float],
    fee_rate: float,
    fee_exp: float,
    min_size: float,
    bankroll: float,
    window_budget_left: float,
    allow_entry: bool,
) -> Order | None:
    # 1) Exits: the market is paying more for our shares than they are worth.
    for side in SIDES:
        h = held.get(side, 0.0)
        if h <= 0:
            continue
        plan = plan_sell(books[side], fair.optimistic(side), h, cfg, fee_rate, fee_exp)
        if plan and (plan.shares >= min_size or plan.shares >= h - 1e-9):
            return Order(
                window, side, "SELL", tokens[side], 0.0, plan.shares, plan.limit, now,
                fair.fair(side), fair.optimistic(side), plan.profit / plan.shares,
            )

    if not allow_entry:
        return None

    # 2) Entries: buy the side whose ask is below a conservative fair value.
    best: tuple[float, str, _Plan, float, float] | None = None
    for side in SIDES:
        other = DOWN if side == UP else UP
        if held.get(other, 0.0) > 0:
            continue  # never hold both sides of the same window
        book = books[side]
        ask = book.best_ask()
        if ask is None or book.crossed():
            continue
        p = min(fair.pessimistic(side), cfg.p_cap)
        all_in = ask + fee_per_share(ask, fee_rate, fee_exp)
        if all_in >= 1.0 or p - all_in < cfg.min_edge:
            continue
        kelly = (p - all_in) / (1.0 - all_in)
        budget = min(
            cfg.kelly_fraction * kelly * bankroll,
            cfg.max_trade_usd,
            window_budget_left,
            bankroll,
        )
        if budget < cfg.min_trade_usd:
            continue
        plan = plan_buy(book, p, budget, cfg, fee_rate, fee_exp)
        if plan is None or plan.shares < min_size or plan.cost + plan.fees < cfg.min_trade_usd:
            continue
        if best is None or plan.profit > best[0]:
            best = (plan.profit, side, plan, p, budget)
    if best is None:
        return None
    _, side, plan, p, budget_cap = best
    return Order(
        window, side, "BUY", tokens[side], _ceil2(plan.cost + plan.fees, budget_cap), plan.shares,
        plan.limit, now, fair.fair(side), p, plan.profit / plan.shares,
    )
