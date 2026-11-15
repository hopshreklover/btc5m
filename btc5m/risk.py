"""Account-level limits. Everything here can only ever block new entries."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .config import Config


class Risk:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bankroll = cfg.bankroll
        self.day: str | None = None
        self.day_pnl = 0.0
        self.loss_streak = 0
        self.cooldown_left = 0
        self.total_pnl = 0.0
        self.windows_traded = 0
        self.windows_won = 0

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if day != self.day:
            self.day = day
            self.day_pnl = 0.0

    def on_window_start(self, now: float) -> None:
        self._roll_day(now)
        if self.cooldown_left > 0:
            self.cooldown_left -= 1

    def blocked(self, now: float) -> str | None:
        """Reason new entries are blocked, or None."""
        self._roll_day(now)
        if self.cfg.kill_file and os.path.exists(self.cfg.kill_file):
            return f"kill file '{self.cfg.kill_file}' present"
        if self.day_pnl <= -self.cfg.daily_loss_limit:
            return f"daily loss limit hit ({self.day_pnl:+.2f})"
        if self.bankroll < self.cfg.min_bankroll:
            return f"bankroll {self.bankroll:.2f} below minimum"
        if self.cooldown_left > 0:
            return f"cool-down after loss streak ({self.cooldown_left} windows left)"
        return None

    def on_settle(self, now: float, pnl: float, traded: bool) -> None:
        self._roll_day(now)
        self.bankroll += pnl
        self.day_pnl += pnl
        self.total_pnl += pnl
        if not traded:
            return
        self.windows_traded += 1
        if pnl > 0:
            self.windows_won += 1
            self.loss_streak = 0
        elif pnl < 0:
            self.loss_streak += 1
            if self.loss_streak >= self.cfg.max_loss_streak:
                self.cooldown_left = self.cfg.cooldown_windows
                self.loss_streak = 0
