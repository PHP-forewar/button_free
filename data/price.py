"""Jupiter Price API + Binance ticker. Caches results to reduce API load."""

import time
import logging
import requests
from collections import deque, defaultdict
from threading import Lock
from typing import Dict, Optional

import config

logger = logging.getLogger(__name__)

# ─── generic TTL cache ────────────────────────────────────────────────────────

class _Cache:
    def __init__(self, ttl: float):
        self._ttl   = ttl
        self._store: Dict = {}
        self._ts:   Dict = {}
        self._lock  = Lock()

    def get(self, key):
        with self._lock:
            if key in self._store and (time.time() - self._ts[key]) < self._ttl:
                return self._store[key]
        return None

    def set(self, key, value):
        with self._lock:
            self._store[key] = value
            self._ts[key]    = time.time()


# ─── HTTP helper with exponential backoff ─────────────────────────────────────

def _get(url: str, params=None, headers=None, timeout=8) -> Optional[dict]:
    delay = 2
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=timeout)
            if r.status_code == 429:
                logger.warning("Rate limited %s – backing off %ss", url, delay)
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 3:
                logger.error("API error %s: %s", url, exc)
            else:
                time.sleep(delay)
                delay *= 2
    return None


# ─── Price data ───────────────────────────────────────────────────────────────

_price_cache = _Cache(config.CACHE_PRICE_TTL)

def get_jupiter_prices() -> Dict[str, float]:
    """Return {symbol: usd_price} for all configured tokens via Jupiter."""
    cached = _price_cache.get("jupiter")
    if cached is not None:
        return cached

    ids = ",".join(config.TOKENS.keys())
    data = _get(config.JUPITER_PRICE_URL, params={"ids": ids})
    result: Dict[str, float] = {}

    if data and "data" in data:
        for sym, info in data["data"].items():
            try:
                result[sym] = float(info["price"])
            except (KeyError, TypeError, ValueError):
                pass

    if result:
        _price_cache.set("jupiter", result)
    return result


def get_btc_price() -> Optional[float]:
    cached = _price_cache.get("btc")
    if cached is not None:
        return cached

    data = _get(config.BINANCE_TICKER_URL, params={"symbol": "BTCUSDT"})
    if data:
        try:
            price = float(data["price"])
            _price_cache.set("btc", price)
            return price
        except (KeyError, ValueError):
            pass
    return None


def get_binance_spot_ticker(symbol: str) -> Optional[dict]:
    """Return Binance 24hr ticker stats for one symbol."""
    cache_key = f"24h_{symbol}"
    cached = _price_cache.get(cache_key)
    if cached is not None:
        return cached

    data = _get(config.BINANCE_24H_URL, params={"symbol": symbol})
    if data:
        _price_cache.set(cache_key, data)
    return data


# ─── Price history (rolling deque per token for indicators) ──────────────────

class PriceHistory:
    """Thread-safe rolling price history used by SignalEngine."""

    def __init__(self, maxlen: int = config.PRICE_HISTORY_MAXLEN):
        self._maxlen = maxlen
        self._histories: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )
        self._lock = Lock()

    def push(self, token: str, price: float):
        with self._lock:
            self._histories[token].append(price)

    def get(self, token: str) -> list:
        with self._lock:
            return list(self._histories[token])

    def ready(self, token: str, min_len: int = config.MIN_HISTORY_FOR_SIGNALS) -> bool:
        with self._lock:
            return len(self._histories[token]) >= min_len


# Shared singleton
price_history = PriceHistory()
