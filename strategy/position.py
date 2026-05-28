"""Position and portfolio management with CSV trade logging."""

import csv
import os
import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime

import config

logger = logging.getLogger(__name__)


@dataclass
class Position:
    token:       str
    direction:   str          # 'LONG' | 'SHORT'
    entry_price: float
    size_usd:    float        # notional USD (paper)
    size_units:  float        # token units
    open_time:   float = field(default_factory=time.time)

    # dynamic state
    max_pnl_pct:    float = 0.0
    trailing_stop:  Optional[float] = None   # price level
    moonbag_done:   bool = False
    moonbag_units:  float = 0.0              # units kept after moonbag sell
    signal_snapshot: dict = field(default_factory=dict)

    # ── PnL calculation ────────────────────────────────────────────────────────

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.direction == "LONG":
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100

    def pnl_usd(self, current_price: float) -> float:
        return self.pnl_pct(current_price) / 100 * self.size_usd

    # ── Trailing stop update ──────────────────────────────────────────────────

    def update_trailing(self, current_price: float):
        pct = self.pnl_pct(current_price)
        if pct > self.max_pnl_pct:
            self.max_pnl_pct = pct

        # trailing stop is set once we're at least 3% in profit
        if self.max_pnl_pct >= 3.0:
            trail_pct = self.max_pnl_pct - config.TRAILING_DRAWDOWN_PCT
            if self.direction == "LONG":
                stop = self.entry_price * (1 + trail_pct / 100)
                self.trailing_stop = max(self.trailing_stop or 0, stop)
            else:
                stop = self.entry_price * (1 - trail_pct / 100)
                if self.trailing_stop is None:
                    self.trailing_stop = stop
                else:
                    self.trailing_stop = min(self.trailing_stop, stop)

    # ── Exit checks ───────────────────────────────────────────────────────────

    def should_exit(self, current_price: float) -> Optional[str]:
        """Return exit reason string or None."""
        self.update_trailing(current_price)
        pct = self.pnl_pct(current_price)

        # stop loss
        if pct <= -config.STOP_LOSS_PCT:
            return "STOP_LOSS"

        # trailing stop hit
        if self.trailing_stop is not None:
            if self.direction == "LONG" and current_price <= self.trailing_stop:
                return "TRAILING_STOP"
            if self.direction == "SHORT" and current_price >= self.trailing_stop:
                return "TRAILING_STOP"

        # time kill
        hours_open = (time.time() - self.open_time) / 3600
        if hours_open >= config.TIME_KILL_HOURS:
            return "TIME_KILL"

        return None

    def should_moonbag(self, current_price: float) -> bool:
        """True when +20% profit and moonbag hasn't been taken yet."""
        return (not self.moonbag_done and
                self.pnl_pct(current_price) >= config.MOONBAG_TRIGGER_PCT)

    # ── Time remaining ────────────────────────────────────────────────────────

    def hours_remaining(self) -> float:
        elapsed = (time.time() - self.open_time) / 3600
        return max(0.0, config.TIME_KILL_HOURS - elapsed)


# ─── Portfolio ────────────────────────────────────────────────────────────────

class Portfolio:
    def __init__(self, initial_balance: float = config.INITIAL_BALANCE):
        self.cash_balance: float    = initial_balance
        self.positions: List[Position] = []
        self.trade_log: List[dict]  = []
        self._ensure_csv()

    # ── Bet sizing ────────────────────────────────────────────────────────────

    def bet_size(self) -> float:
        bal = self.total_value()
        for lo, hi, bet in config.BET_TIERS:
            if lo <= bal < hi:
                return bet
        return config.BET_TIERS[-1][2]

    # ── Entry ─────────────────────────────────────────────────────────────────

    def open_position(self, token: str, direction: str,
                      price: float, signals: dict) -> Optional[Position]:
        if len(self.positions) >= config.MAX_POSITIONS:
            logger.info("MAX_POSITIONS reached – skipping %s %s", direction, token)
            return None

        size_usd = self.bet_size()
        if size_usd > self.cash_balance:
            logger.info("Insufficient cash (%.2f) for bet %.2f", self.cash_balance, size_usd)
            return None

        size_units = size_usd / price if price > 0 else 0
        pos = Position(
            token=token,
            direction=direction,
            entry_price=price,
            size_usd=size_usd,
            size_units=size_units,
            signal_snapshot=signals,
        )
        self.positions.append(pos)
        self.cash_balance -= size_usd
        logger.info("OPEN %s %s @ %.6f  size=%.2f", direction, token, price, size_usd)
        return pos

    # ── Moonbag ───────────────────────────────────────────────────────────────

    def take_moonbag(self, pos: Position, current_price: float):
        """Sell 50% at +20%, keep rest running."""
        sell_fraction = config.MOONBAG_SELL_FRACTION
        sell_usd = pos.size_usd * sell_fraction
        pnl_pct  = pos.pnl_pct(current_price)
        pnl_usd  = sell_usd * pnl_pct / 100

        self.cash_balance += sell_usd + pnl_usd
        pos.size_usd  *= (1 - sell_fraction)
        pos.size_units *= (1 - sell_fraction)
        pos.moonbag_done = True

        logger.info("MOONBAG %s %s – sold 50%% @ %.6f  pnl=%.2f",
                    pos.direction, pos.token, current_price, pnl_usd)
        self._log_trade(pos, current_price, pnl_usd, pnl_pct, "MOONBAG_50PCT")

    # ── Exit ──────────────────────────────────────────────────────────────────

    def close_position(self, pos: Position, current_price: float,
                       reason: str):
        pnl_pct = pos.pnl_pct(current_price)
        pnl_usd = pos.pnl_usd(current_price)
        self.cash_balance += pos.size_usd + pnl_usd
        self.positions.remove(pos)
        self._log_trade(pos, current_price, pnl_usd, pnl_pct, reason)
        logger.info("CLOSE %s %s @ %.6f  reason=%s  pnl=%.2f (%.2f%%)",
                    pos.direction, pos.token, current_price,
                    reason, pnl_usd, pnl_pct)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def open_value(self, prices: Dict[str, float]) -> float:
        total = 0.0
        for pos in self.positions:
            price = prices.get(pos.token, pos.entry_price)
            total += pos.size_usd + pos.pnl_usd(price)
        return total

    def total_value(self, prices: Dict[str, float] = None) -> float:
        if prices:
            return self.cash_balance + self.open_value(prices)
        return self.cash_balance

    def roi_pct(self, prices: Dict[str, float] = None) -> float:
        total = self.total_value(prices)
        return (total - config.INITIAL_BALANCE) / config.INITIAL_BALANCE * 100

    # ── CSV logging ───────────────────────────────────────────────────────────

    def _ensure_csv(self):
        if not os.path.exists(config.TRADES_CSV):
            with open(config.TRADES_CSV, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "timestamp", "token", "direction",
                    "entry_price", "exit_price",
                    "pnl_usd", "pnl_pct",
                    "signal_scores", "exit_reason",
                ])

    def _log_trade(self, pos: Position, exit_price: float,
                   pnl_usd: float, pnl_pct: float, reason: str):
        row = {
            "timestamp":     datetime.utcnow().isoformat(),
            "token":         pos.token,
            "direction":     pos.direction,
            "entry_price":   round(pos.entry_price, 8),
            "exit_price":    round(exit_price, 8),
            "pnl_usd":       round(pnl_usd, 4),
            "pnl_pct":       round(pnl_pct, 4),
            "signal_scores": str(pos.signal_snapshot.get("score", "")),
            "exit_reason":   reason,
        }
        self.trade_log.append(row)

        try:
            with open(config.TRADES_CSV, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                w.writerow(row)
        except Exception as exc:
            logger.error("CSV write error: %s", exc)
