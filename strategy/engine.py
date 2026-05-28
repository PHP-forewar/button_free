"""
Trading engine – evaluates signals and manages position lifecycle.
Paper trading only. No wallets, no real funds.
"""

import logging
from typing import Dict, Optional

import config
from data.liquidation   import price_near_cluster
from strategy.signals   import signal_engine
from strategy.position  import Portfolio, Position

logger = logging.getLogger(__name__)


class TradingEngine:

    def __init__(self, portfolio: Portfolio):
        self.portfolio    = portfolio
        self._prev_prices: Dict[str, float] = {}

    # ─── Main tick ────────────────────────────────────────────────────────────

    def tick(self, prices: Dict[str, float]):
        """Called every refresh cycle. Updates signals, checks entries/exits."""

        for token, price in prices.items():
            prev  = self._prev_prices.get(token, price)
            chg   = (price - prev) / prev * 100 if prev else 0.0
            sigs  = signal_engine.combined_score(token, price, chg)
            self._prev_prices[token] = price

            # Check exits for open positions in this token
            for pos in list(self.portfolio.positions):
                if pos.token != token:
                    continue
                self._process_exit(pos, price)

        # Check entries (only after exits to respect MAX_POSITIONS)
        for token, price in prices.items():
            sigs = signal_engine._last_signals.get(token)
            if sigs is None:
                continue
            self._process_entry(token, price, sigs)

    # ─── Entry logic ──────────────────────────────────────────────────────────

    def _already_open(self, token: str) -> bool:
        return any(p.token == token for p in self.portfolio.positions)

    def _process_entry(self, token: str, price: float, sigs: dict):
        if self._already_open(token):
            return
        if len(self.portfolio.positions) >= config.MAX_POSITIONS:
            return

        score    = sigs["score"]
        direction = sigs["direction"]

        if direction == "LONG" and self._check_long_entry(score, sigs, price):
            logger.info("ENTRY LONG %s score=%.1f", token, score)
            self.portfolio.open_position(token, "LONG", price, sigs)

        elif direction == "SHORT" and self._check_short_entry(score, sigs, price):
            logger.info("ENTRY SHORT %s score=%.1f", token, score)
            self.portfolio.open_position(token, "SHORT", price, sigs)

    def _check_long_entry(self, score: float, sigs: dict,
                          price: float) -> bool:
        if score < config.LONG_THRESHOLD:
            return False
        if sigs["cvd"] == "DIVERGE":
            return False
        if sigs["oi"] == "FAKE":
            return False
        if sigs["whale"] == "BEARISH_WHALE":
            return False

        liq = sigs.get("liq_levels", {})
        if price_near_cluster(price, liq.get("long_cluster"), 2.0):
            return False    # entering near a liquidation magnet is dangerous

        return True

    def _check_short_entry(self, score: float, sigs: dict,
                           price: float) -> bool:
        if score > config.SHORT_THRESHOLD:
            return False
        if sigs["cvd"] != "DIVERGE":
            return False    # shorts need price/volume divergence
        if sigs["funding"] != "SHORT_BIAS":
            return False    # longs must be paying for short entry

        liq = sigs.get("liq_levels", {})
        if price_near_cluster(price, liq.get("short_cluster"), 2.0):
            return False    # entering near a short cluster is risky

        return True

    # ─── Exit logic ───────────────────────────────────────────────────────────

    def _process_exit(self, pos: Position, current_price: float):
        # moonbag check first
        if pos.should_moonbag(current_price):
            self.portfolio.take_moonbag(pos, current_price)
            # position stays open with reduced size

        reason = pos.should_exit(current_price)
        if reason:
            self.portfolio.close_position(pos, current_price, reason)

    # ─── Status helpers ──────────────────────────────────────────────────────

    def get_signal_summary(self) -> Dict[str, dict]:
        return dict(signal_engine._last_signals)
