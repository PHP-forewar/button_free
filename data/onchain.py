"""Helius RPC – whale wallet activity tracker."""

import time
import logging
import requests
from typing import Dict, Optional

import config
from data.price import _Cache, _get

logger = logging.getLogger(__name__)

_whale_cache = _Cache(config.CACHE_WHALE_TTL)

# Known large Solana ecosystem wallets (public addresses)
_KNOWN_WHALES = [
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # large defi wallet
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "H6ARHf6YXhGYeQfUzQNGk6rDNnLBQKrenN712K4AQJEG",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
]

# ─── Helius RPC helpers ───────────────────────────────────────────────────────

def _helius_rpc(method: str, params: list) -> Optional[dict]:
    url = f"{config.HELIUS_RPC_URL}?api-key={config.HELIUS_API_KEY}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    delay = 2
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(delay); delay *= 2; continue
            if r.status_code == 401:
                logger.debug("Helius: auth failed (demo key)")
                return None
            r.raise_for_status()
            data = r.json()
            return data.get("result")
        except Exception as exc:
            if attempt == 2:
                logger.debug("Helius RPC %s error: %s", method, exc)
            else:
                time.sleep(delay); delay *= 2
    return None


def _helius_transactions(address: str, limit: int = 10) -> Optional[list]:
    """Get recent transactions for an address via Helius enhanced API."""
    url = f"{config.HELIUS_API_URL}/addresses/{address}/transactions"
    params = {"api-key": config.HELIUS_API_KEY, "limit": limit}
    delay = 2
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code in (401, 403):
                return None
            if r.status_code == 429:
                time.sleep(delay); delay *= 2; continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 2:
                logger.debug("Helius tx error: %s", exc)
            else:
                time.sleep(delay); delay *= 2
    return None


# ─── Token largest accounts ──────────────────────────────────────────────────

def _get_largest_holders(mint: str) -> list:
    """Returns list of {address, uiAmount} for top 20 holders."""
    result = _helius_rpc("getTokenLargestAccounts", [mint])
    if result and "value" in result:
        return result["value"]
    return []


# ─── Whale activity detection ─────────────────────────────────────────────────

class _WhaleState:
    """Stores previous holder balances to detect changes."""
    _prev: Dict[str, Dict[str, float]] = {}  # mint -> {address -> amount}

    @classmethod
    def update(cls, mint: str, holders: list) -> str:
        """Compare current holders with previous snapshot → BULLISH/BEARISH/QUIET."""
        current = {h["address"]: h.get("uiAmount", 0) for h in holders}

        if mint not in cls._prev:
            cls._prev[mint] = current
            return "QUIET"

        prev = cls._prev[mint]
        net_change_usd = 0.0

        for addr, amount in current.items():
            prev_amount = prev.get(addr, 0)
            delta = amount - prev_amount
            # approximate USD: we don't have price here, mark as units
            net_change_usd += delta

        cls._prev[mint] = current

        if net_change_usd > 0:
            return "BUY_PRESSURE"
        elif net_change_usd < 0:
            return "SELL_PRESSURE"
        return "QUIET"


def _volume_based_whale(token: str, current_price: float) -> str:
    """
    Fallback: use Binance 24h volume stats to approximate whale activity.
    A significant volume spike relative to average + price direction signals whales.
    """
    from data.price import get_binance_spot_ticker
    symbol = config.BINANCE_SPOT_SYMBOLS.get(token)
    if not symbol:
        return "QUIET"

    ticker = get_binance_spot_ticker(symbol)
    if not ticker:
        return "QUIET"

    try:
        price_change_pct = float(ticker.get("priceChangePercent", 0))
        quote_volume     = float(ticker.get("quoteVolume", 0))
        count            = int(ticker.get("count", 1))

        # average trade size proxy
        avg_trade_size = quote_volume / max(count, 1)

        # Volume > $500K and strong directional move → whale signal
        if quote_volume > 500_000 and price_change_pct > 3.0:
            return "BULLISH_WHALE"
        elif quote_volume > 500_000 and price_change_pct < -3.0:
            return "BEARISH_WHALE"
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    return "QUIET"


def get_whale_activity(token: str, current_price: float = 0.0) -> str:
    """
    Return 'BULLISH_WHALE' | 'BEARISH_WHALE' | 'QUIET'.
    Tries Helius first, falls back to volume-based detection.
    """
    cached = _whale_cache.get(token)
    if cached is not None:
        return cached

    mint = config.TOKENS.get(token)
    signal = "QUIET"

    if mint:
        holders = _get_largest_holders(mint)
        if holders:
            pressure = _WhaleState.update(mint, holders)
            # rough USD estimate using current price
            if pressure == "BUY_PRESSURE":
                signal = "BULLISH_WHALE"
            elif pressure == "SELL_PRESSURE":
                signal = "BEARISH_WHALE"
            else:
                signal = "QUIET"
        else:
            signal = _volume_based_whale(token, current_price)
    else:
        signal = _volume_based_whale(token, current_price)

    _whale_cache.set(token, signal)
    return signal
