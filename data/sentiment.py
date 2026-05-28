"""Funding rates, Open Interest, and CoinGecko market data."""

import logging
from typing import Optional, Dict

import config
from data.price import _Cache, _get

logger = logging.getLogger(__name__)

_funding_cache     = _Cache(config.CACHE_FUNDING_TTL)
_oi_cache          = _Cache(config.CACHE_OI_TTL)
_coingecko_cache   = _Cache(config.CACHE_COINGECKO_TTL)

# ─── Funding rate ─────────────────────────────────────────────────────────────

def get_funding_rate(token: str) -> Optional[float]:
    """
    Return current funding rate (%) for a token's perpetual contract.
    Positive = longs paying.  Negative = shorts paying.
    Returns None if data unavailable.
    """
    cached = _funding_cache.get(token)
    if cached is not None:
        return cached

    symbol = config.BINANCE_FUTURES_SYMBOLS.get(token)
    if not symbol:
        return None

    data = _get(config.BINANCE_FUNDING_URL, params={"symbol": symbol})
    if data:
        try:
            rate = float(data["lastFundingRate"]) * 100  # as percentage
            _funding_cache.set(token, rate)
            return rate
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Funding rate parse error %s: %s", token, exc)
    return None


def get_funding_signal(token: str) -> str:
    """
    Return 'LONG_BIAS' | 'SHORT_BIAS' | 'NEUTRAL'.
    LONG_BIAS  → shorts paying → good time to be long.
    SHORT_BIAS → longs paying  → good time to be short.
    """
    rate = get_funding_rate(token)
    if rate is None:
        return "NEUTRAL"
    if rate > config.FUNDING_HIGH:
        return "SHORT_BIAS"
    if rate < config.FUNDING_LOW:
        return "LONG_BIAS"
    return "NEUTRAL"


# ─── Open Interest ────────────────────────────────────────────────────────────

def get_open_interest(token: str) -> Optional[float]:
    """Return current OI in USD for token's perp."""
    cached = _oi_cache.get(f"oi_{token}")
    if cached is not None:
        return cached

    symbol = config.BINANCE_FUTURES_SYMBOLS.get(token)
    if not symbol:
        return None

    data = _get(config.BINANCE_OI_URL, params={"symbol": symbol})
    if data:
        try:
            oi = float(data["openInterest"])
            _oi_cache.set(f"oi_{token}", oi)
            return oi
        except (KeyError, TypeError, ValueError):
            pass
    return None


def get_oi_change(token: str) -> Optional[float]:
    """
    Return OI % change over the last two 5-minute intervals.
    Positive = OI growing (more conviction).
    """
    cached = _oi_cache.get(f"oi_chg_{token}")
    if cached is not None:
        return cached

    symbol = config.BINANCE_FUTURES_SYMBOLS.get(token)
    if not symbol:
        return None

    data = _get(config.BINANCE_OI_HIST_URL,
                params={"symbol": symbol, "period": "5m", "limit": 3})
    if data and len(data) >= 2:
        try:
            old_oi  = float(data[0]["sumOpenInterest"])
            new_oi  = float(data[-1]["sumOpenInterest"])
            if old_oi == 0:
                return None
            change = (new_oi - old_oi) / old_oi * 100
            _oi_cache.set(f"oi_chg_{token}", change)
            return change
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    return None


def get_oi_signal(token: str, price_change_pct: float) -> str:
    """
    Return 'STRONG' | 'WEAK' | 'FAKE'.
    OI + price in same direction → STRONG (real move).
    OI + price diverge          → FAKE  (liquidity hunt).
    """
    oi_change = get_oi_change(token)
    if oi_change is None:
        return "WEAK"

    same_dir = (oi_change > 0 and price_change_pct > 0) or \
               (oi_change < 0 and price_change_pct < 0)

    abs_oi = abs(oi_change)

    if abs_oi > 1.0 and same_dir:
        return "STRONG"
    if abs_oi > 0.3 and not same_dir:
        return "FAKE"
    return "WEAK"


# ─── CoinGecko market data ────────────────────────────────────────────────────

def get_coingecko_data(token: str) -> Optional[dict]:
    """
    Return CoinGecko market data dict for token.
    Keys: price_change_24h, volume_24h, market_cap, volume_change_24h
    """
    cached = _coingecko_cache.get(token)
    if cached is not None:
        return cached

    coin_id = config.COINGECKO_IDS.get(token)
    if not coin_id:
        return None

    url = f"{config.COINGECKO_BASE_URL}/coins/{coin_id}"
    params = {
        "localization":   "false",
        "tickers":        "false",
        "market_data":    "true",
        "community_data": "false",
        "developer_data": "false",
    }
    data = _get(url, params=params, timeout=12)
    if not data:
        return None

    try:
        md = data["market_data"]
        result = {
            "price_change_24h":  md["price_change_percentage_24h"],
            "volume_24h":        md["total_volume"]["usd"],
            "market_cap":        md["market_cap"]["usd"],
            "price_change_1h":   md.get("price_change_percentage_1h_in_currency",
                                        {}).get("usd", 0.0),
        }
        _coingecko_cache.set(token, result)
        return result
    except (KeyError, TypeError):
        return None


def get_cvd_signal(token: str) -> str:
    """
    Proxy CVD using CoinGecko + Binance volume data.

    Logic:
      - If price going up BUT volume declining → divergence (DIVERGE)
      - If price going up AND volume growing   → confirmation (CONFIRM)
      - Otherwise NEUTRAL
    Returns 'CONFIRM' | 'DIVERGE' | 'NEUTRAL'.
    """
    cg = get_coingecko_data(token)
    if not cg:
        return "NEUTRAL"

    symbol = config.BINANCE_SPOT_SYMBOLS.get(token)
    if not symbol:
        return "NEUTRAL"

    from data.price import get_binance_spot_ticker
    ticker = get_binance_spot_ticker(symbol)
    if not ticker:
        return "NEUTRAL"

    try:
        price_chg   = float(ticker["priceChangePercent"])   # 24h price %
        volume      = float(ticker["quoteVolume"])
        prev_volume = cg.get("volume_24h", 0)

        if prev_volume == 0:
            return "NEUTRAL"

        volume_chg_pct = (volume - prev_volume) / prev_volume * 100

        if price_chg > 0 and volume_chg_pct > 5:
            return "CONFIRM"
        if price_chg > 0 and volume_chg_pct < -5:
            return "DIVERGE"
        if price_chg < 0 and volume_chg_pct > 5:
            return "CONFIRM"   # volume confirms the sell
        if price_chg < 0 and volume_chg_pct < -5:
            return "DIVERGE"   # price falling but no volume = fake sell

    except (TypeError, ValueError, ZeroDivisionError):
        pass

    return "NEUTRAL"
