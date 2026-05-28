"""
Signal engine – 5-layer scoring system.
Each layer returns a value in [-100, +100].
Combined score drives entry decisions.
"""

import logging
import numpy as np
from typing import Dict, List, Tuple

import config
from data.price     import price_history
from data.onchain   import get_whale_activity
from data.sentiment import (get_funding_signal, get_oi_signal,
                             get_cvd_signal, get_coingecko_data)
from data.liquidation import get_liquidation_levels

logger = logging.getLogger(__name__)


# ─── Technical indicator helpers (no ta-lib dependency) ──────────────────────

def _rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0

    arr    = np.array(prices, dtype=float)
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(prices: np.ndarray, period: int) -> np.ndarray:
    k      = 2.0 / (period + 1)
    result = np.zeros(len(prices))
    result[0] = prices[0]
    for i in range(1, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def _macd(prices: List[float],
          fast: int = 12, slow: int = 26,
          signal_period: int = 9) -> Tuple[float, float, float]:
    """Return (macd_line, signal_line, histogram) for the last point."""
    if len(prices) < slow + signal_period:
        return 0.0, 0.0, 0.0

    arr        = np.array(prices, dtype=float)
    ema_fast   = _ema(arr, fast)
    ema_slow   = _ema(arr, slow)
    macd_line  = ema_fast - ema_slow
    sig_line   = _ema(macd_line[slow - 1:], signal_period)
    histogram  = macd_line[slow - 1 + signal_period - 1:] - sig_line[signal_period - 1:]

    ml = float(macd_line[-1])
    sl = float(sig_line[-1])
    h  = float(histogram[-1]) if len(histogram) > 0 else 0.0
    return ml, sl, h


# ─── SignalEngine ─────────────────────────────────────────────────────────────

class SignalEngine:

    # cached signal results per token
    _last_signals: Dict[str, dict] = {}

    # ── Layer 1: Price momentum (RSI + MACD) ──────────────────────────────────

    def get_price_momentum(self, token: str) -> float:
        """
        Score [-100, +100].
        +100 = very bullish momentum  (RSI oversold + MACD bullish cross)
        -100 = very bearish momentum  (RSI overbought + MACD bearish cross)
        """
        prices = price_history.get(token)
        if len(prices) < config.MIN_HISTORY_FOR_SIGNALS:
            return 0.0

        rsi = _rsi(prices, config.RSI_PERIOD)
        ml, sl, hist = _macd(prices, config.MACD_FAST,
                              config.MACD_SLOW, config.MACD_SIGNAL)

        # RSI component: oversold=+50, overbought=-50, linear in between
        if rsi <= 30:
            rsi_score = 50.0
        elif rsi >= 70:
            rsi_score = -50.0
        else:
            rsi_score = (50 - rsi) * (50.0 / 20.0)  # linear -50..+50

        # MACD histogram: positive → bullish, scale to [-50, +50]
        macd_score = float(np.clip(hist / (abs(ml) + 1e-9) * 50, -50, 50))

        return float(np.clip(rsi_score + macd_score, -100, 100))

    # ── Layer 2: CVD divergence ───────────────────────────────────────────────

    def get_cvd_divergence(self, token: str) -> str:
        """Return 'CONFIRM' | 'DIVERGE' | 'NEUTRAL'."""
        return get_cvd_signal(token)

    def _cvd_score(self, cvd: str, direction: str) -> float:
        """Convert CVD string to numeric score given intended direction."""
        if cvd == "CONFIRM":
            return 60.0 if direction == "LONG" else -60.0
        if cvd == "DIVERGE":
            return -60.0 if direction == "LONG" else 60.0
        return 0.0

    # ── Layer 3: Open Interest ────────────────────────────────────────────────

    def get_oi_signal(self, token: str, price_change_pct: float = 0.0) -> str:
        """Return 'STRONG' | 'WEAK' | 'FAKE'."""
        return get_oi_signal(token, price_change_pct)

    def _oi_score(self, oi: str, direction: str) -> float:
        if oi == "STRONG":
            return 60.0 if direction == "LONG" else -60.0
        if oi == "FAKE":
            return 0.0
        return 20.0 if direction == "LONG" else -20.0  # WEAK

    # ── Layer 4: Funding rate ─────────────────────────────────────────────────

    def get_funding_signal(self, token: str) -> str:
        """Return 'LONG_BIAS' | 'SHORT_BIAS' | 'NEUTRAL'."""
        return get_funding_signal(token)

    def _funding_score(self, funding: str) -> float:
        if funding == "LONG_BIAS":
            return 50.0
        if funding == "SHORT_BIAS":
            return -50.0
        return 0.0

    # ── Layer 5: Whale activity ───────────────────────────────────────────────

    def get_whale_activity(self, token: str, price: float = 0.0) -> str:
        """Return 'BULLISH_WHALE' | 'BEARISH_WHALE' | 'QUIET'."""
        return get_whale_activity(token, price)

    def _whale_score(self, whale: str) -> float:
        if whale == "BULLISH_WHALE":
            return 100.0
        if whale == "BEARISH_WHALE":
            return -100.0
        return 0.0

    # ── Liquidation levels ────────────────────────────────────────────────────

    def get_liquidation_levels(self, token: str, price: float) -> dict:
        return get_liquidation_levels(token, price)

    # ── Combined score ────────────────────────────────────────────────────────

    def combined_score(self, token: str,
                       price: float = 0.0,
                       price_change_pct: float = 0.0) -> dict:
        """
        Compute weighted combined score.
        Returns:
          {
            score:       float [-100, 100],
            direction:   'LONG' | 'SHORT' | 'NEUTRAL',
            confidence:  float [0, 100] (abs(score)),
            momentum:    float,
            cvd:         str,
            oi:          str,
            funding:     str,
            whale:       str,
            liq_levels:  dict,
            block_reason: str | None,
          }
        """
        momentum_raw = self.get_price_momentum(token)
        cvd          = self.get_cvd_divergence(token)
        funding      = self.get_funding_signal(token)
        whale        = self.get_whale_activity(token, price)
        oi           = self.get_oi_signal(token, price_change_pct)
        liq          = self.get_liquidation_levels(token, price)

        # Determine raw direction from momentum for CVD/OI scoring
        direction_raw = "LONG" if momentum_raw > 0 else "SHORT"

        momentum_score = momentum_raw * config.SIGNAL_WEIGHTS["momentum"]
        cvd_score      = self._cvd_score(cvd, direction_raw) * config.SIGNAL_WEIGHTS["cvd"]
        oi_score       = self._oi_score(oi, direction_raw) * config.SIGNAL_WEIGHTS["oi"]
        funding_score  = self._funding_score(funding) * config.SIGNAL_WEIGHTS["funding"]
        whale_score    = self._whale_score(whale) * config.SIGNAL_WEIGHTS["whale"]

        total = momentum_score + cvd_score + oi_score + funding_score + whale_score
        total = float(np.clip(total, -100, 100))

        if total >= 10:
            direction = "LONG"
        elif total <= -10:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # Determine what (if anything) is blocking a trade
        block_reason = self._block_reason(total, direction, cvd, oi,
                                          funding, whale, price, liq)

        result = {
            "score":        total,
            "direction":    direction,
            "confidence":   round(abs(total), 1),
            "momentum":     round(momentum_raw, 1),
            "cvd":          cvd,
            "oi":           oi,
            "funding":      funding,
            "whale":        whale,
            "liq_levels":   liq,
            "block_reason": block_reason,
        }
        self._last_signals[token] = result
        return result

    def _block_reason(self, score: float, direction: str,
                      cvd: str, oi: str, funding: str, whale: str,
                      price: float, liq: dict) -> str | None:
        """Return human-readable reason why entry is blocked, or None."""
        from data.liquidation import price_near_cluster

        if direction == "LONG":
            if score < config.LONG_THRESHOLD:
                return f"Score {score:.0f} < {config.LONG_THRESHOLD}"
            if cvd == "DIVERGE":
                return "CVD divergence (sell into rally)"
            if oi == "FAKE":
                return "OI/price diverge (fake move)"
            if whale == "BEARISH_WHALE":
                return "Bearish whale detected"
            if price_near_cluster(price, liq.get("long_cluster"), 1.5):
                return "Near long liquidation cluster"
        elif direction == "SHORT":
            if score > config.SHORT_THRESHOLD:
                return f"Score {score:.0f} > {config.SHORT_THRESHOLD}"
            if funding != "SHORT_BIAS":
                return f"Funding not SHORT_BIAS ({funding})"
            if price_near_cluster(price, liq.get("short_cluster"), 1.5):
                return "Near short liquidation cluster"
        return None


# Shared singleton
signal_engine = SignalEngine()
