"""Coinglass liquidation heatmap – nearest long/short clusters."""

import logging
from typing import Dict, Optional, Tuple

import config
from data.price import _Cache, _get

logger = logging.getLogger(__name__)

_liq_cache = _Cache(config.CACHE_LIQUIDATION_TTL)

# Coinglass uses different symbol formats
_COINGLASS_PAIRS: Dict[str, str] = {
    "SOL":    "SOL/USDT",
    "JUP":    "JUP/USDT",
    "RAY":    "RAY/USDT",
    "PYTH":   "PYTH/USDT",
    "ORCA":   "ORCA/USDT",
    "RENDER": "RENDER/USDT",
    "JTO":    "JTO/USDT",
    "HNT":    "HNT/USDT",
}


def _fetch_coinglass(token: str) -> Optional[dict]:
    pair = _COINGLASS_PAIRS.get(token)
    if not pair:
        return None

    headers = {"coinglassSecret": config.COINGLASS_KEY}
    params  = {"ex": "Binance", "pair": pair, "interval": "0"}
    data = _get(config.COINGLASS_LIQ_URL, params=params,
                headers=headers, timeout=10)
    return data


def _parse_clusters(data: dict, current_price: float) -> Tuple[Optional[float], Optional[float]]:
    """
    Extract nearest long and short liquidation clusters.
    Returns (long_cluster_price, short_cluster_price).
    Long clusters are below current price (trapped longs).
    Short clusters are above current price (trapped shorts).
    """
    long_cluster  = None
    short_cluster = None

    try:
        # Coinglass v2 structure: data.chart → list of [price, liq_amount]
        chart = data.get("data", {}).get("chart", [])
        if not chart:
            return None, None

        # find the biggest cluster below and above current price
        long_max_liq  = 0.0
        short_max_liq = 0.0

        for entry in chart:
            price = float(entry[0])
            liq   = float(entry[1]) if len(entry) > 1 else 0.0

            if price < current_price and liq > long_max_liq:
                long_max_liq  = liq
                long_cluster  = price
            elif price > current_price and liq > short_max_liq:
                short_max_liq = liq
                short_cluster = price

    except (KeyError, TypeError, IndexError, ValueError):
        pass

    return long_cluster, short_cluster


def get_liquidation_levels(token: str, current_price: float) -> Dict[str, Optional[float]]:
    """
    Return {'long_cluster': price_or_None, 'short_cluster': price_or_None}.
    long_cluster  – price where trapped longs would get liquidated (below market).
    short_cluster – price where trapped shorts would get liquidated (above market).
    """
    cache_key = f"{token}_{int(current_price)}"
    cached = _liq_cache.get(cache_key)
    if cached is not None:
        return cached

    result = {"long_cluster": None, "short_cluster": None}

    data = _fetch_coinglass(token)
    if data and data.get("code") == "0":
        long_cl, short_cl = _parse_clusters(data, current_price)
        result["long_cluster"]  = long_cl
        result["short_cluster"] = short_cl
    else:
        # Fallback: estimate clusters using ±8% / ±12% common liquidation zones
        if current_price > 0:
            result["long_cluster"]  = round(current_price * 0.92, 6)
            result["short_cluster"] = round(current_price * 1.08, 6)
        logger.debug("Coinglass unavailable for %s – using estimated levels", token)

    _liq_cache.set(cache_key, result)
    return result


def price_near_cluster(current_price: float,
                       cluster_price: Optional[float],
                       pct_threshold: float = 2.0) -> bool:
    """Return True if current price is within pct_threshold% of the cluster."""
    if cluster_price is None or current_price == 0:
        return False
    diff_pct = abs(current_price - cluster_price) / current_price * 100
    return diff_pct <= pct_threshold
