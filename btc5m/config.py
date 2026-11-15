"""All tunables in one place. Override with a TOML file (--config) or CLI flags."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

WINDOW_S = 300  # Polymarket "Bitcoin Up or Down - 5 Minutes" window length


@dataclass(frozen=True)
class Config:
    # --- capital & sizing -------------------------------------------------
    bankroll: float = 200.0          # capital the bot is allowed to treat as its own (USD)
    kelly_fraction: float = 0.25     # fraction of full Kelly to stake
    max_trade_usd: float = 15.0      # hard cap per order, fees included
    max_window_usd: float = 30.0     # hard cap on total buys in one 5-minute window
    max_trades_per_window: int = 3
    min_trade_usd: float = 1.0       # exchange minimum for marketable orders

    # --- when an edge is worth taking -------------------------------------
    min_edge: float = 0.03           # required edge per share after fees, in price units
    exit_edge: float = 0.04          # sell when bid (after fee) beats fair value by this much
    min_price: float = 0.08          # do not buy below / above these prices: the tails are
    max_price: float = 0.92          # where model error and adverse selection dominate
    p_cap: float = 0.97              # never believe a probability above this

    # --- model ------------------------------------------------------------
    vol_fast_halflife_s: float = 120.0
    vol_slow_halflife_s: float = 1800.0
    vol_fast_weight: float = 0.6
    vol_mult: float = 1.15           # widen estimated vol; set from `calibrate` (see README)
    vol_floor: float = 2.0e-5        # per-sqrt-second log-return vol floor
    vol_min_samples: int = 90        # ticks needed before the estimate is trusted
    student_nu: float = 4.0          # tail thickness; >= 200 means normal
    latency_s: float = 0.6           # assumed decision-to-fill delay
    adverse_sigmas: float = 1.0      # price is assumed to move this many sigmas against us
                                     # over `latency_s` before the edge is measured

    # --- timing gates -----------------------------------------------------
    no_trade_first_s: float = 8.0    # let the strike print and books settle
    no_trade_last_s: float = 6.0     # too little time to get filled safely
    trade_cooldown_s: float = 2.0    # wait for the book to refresh after a fill
    decide_interval_s: float = 0.1
    strike_grace_s: float = 2.0      # accept a strike tick at most this late
    settle_timeout_s: float = 45.0

    # --- staleness guards -------------------------------------------------
    max_price_age_s: float = 3.0
    max_book_age_s: float = 15.0
    max_clock_skew_s: float = 2.5    # |local clock - oracle clock| above this blocks trading

    # --- risk -------------------------------------------------------------
    daily_loss_limit: float = 40.0   # stop opening positions for the UTC day
    max_loss_streak: int = 4         # consecutive losing windows before a cool-down
    cooldown_windows: int = 6
    min_bankroll: float = 20.0
    kill_file: str = "STOP"          # create this file to block new entries

    # --- optional faster spot feed (Coinbase) ------------------------------
    spot_lead: bool = False
    basis_halflife_s: float = 60.0
    max_spot_age_s: float = 2.0

    # --- fees when the market does not report a schedule -------------------
    default_fee_rate: float = 0.07
    default_fee_exponent: float = 1.0

    # --- paper trading realism ---------------------------------------------
    paper_latency_s: float = 0.6

    # --- output -------------------------------------------------------------
    data_dir: str = "data"
    record: bool = True
    status_interval_s: float = 15.0


def load_config(path: str | None = None, **overrides: object) -> Config:
    cfg = Config()
    names = {f.name for f in fields(Config)}
    if path:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(raw) - names
        if unknown:
            raise ValueError(f"unknown config keys in {path}: {sorted(unknown)}")
        cfg = replace(cfg, **raw)
    clean = {k: v for k, v in overrides.items() if v is not None}
    unknown = set(clean) - names
    if unknown:
        raise ValueError(f"unknown config overrides: {sorted(unknown)}")
    cfg = replace(cfg, **clean)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    def need(ok: bool, msg: str) -> None:
        if not ok:
            raise ValueError(f"bad config: {msg}")

    need(cfg.bankroll > 0, "bankroll must be positive")
    need(0 < cfg.kelly_fraction <= 1, "kelly_fraction must be in (0, 1]")
    need(cfg.max_trade_usd <= cfg.max_window_usd, "max_trade_usd cannot exceed max_window_usd")
    need(cfg.max_window_usd <= cfg.bankroll, "max_window_usd cannot exceed bankroll")
    need(0 < cfg.min_price < cfg.max_price < 1, "need 0 < min_price < max_price < 1")
    need(cfg.min_edge > 0, "min_edge must be positive")
    need(cfg.no_trade_first_s + cfg.no_trade_last_s < WINDOW_S, "timing gates cover the whole window")
    need(cfg.daily_loss_limit > 0, "daily_loss_limit must be positive")
