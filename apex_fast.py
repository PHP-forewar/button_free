#!/usr/bin/env python3
"""
APEX FAST Paper Trading Bot
============================
V12 + V16 best features combined.
Binance-only. Paper trading. No real funds.

Usage:  python apex_fast.py
"""

import os, sys, csv, time, logging, traceback
from datetime import timezone
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional, Tuple

import requests
import numpy as np
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ══════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════

START_BALANCE    = 10_000.0
UPDATE_INTERVAL  = 60          # seconds between ticks
WARM_UP_SAMPLES  = 5           # fast start
MAX_POSITIONS    = 5
PAPER_TRADING    = True        # NEVER set to False

TOKENS: Dict[str, str] = {
    "BTC":    "BTCUSDT",   "ETH":    "ETHUSDT",
    "SOL":    "SOLUSDT",   "BNB":    "BNBUSDT",
    "DOGE":   "DOGEUSDT",  "AVAX":   "AVAXUSDT",
    "LINK":   "LINKUSDT",  "UNI":    "UNIUSDT",
    "AAVE":   "AAVEUSDT",  "ARB":    "ARBUSDT",
    "OP":     "OPUSDT",    "NEAR":   "NEARUSDT",
    "WIF":    "WIFUSDT",   "BONK":   "BONKUSDT",
    "PEPE":   "PEPEUSDT",  "SHIB":   "SHIBUSDT",
    "RENDER": "RENDERUSDT","FIL":    "FILUSDT",
    "DOT":    "DOTUSDT",   "ATOM":   "ATOMUSDT",
}

BET_TIERS: List[Tuple[float, float, float]] = [
    (0,      2_000,    100.0),
    (2_000,  5_000,    300.0),
    (5_000,  15_000,   600.0),
    (15_000, 30_000,  1_000.0),
    (30_000, float("inf"), 2_000.0),
]

# ── Signal thresholds ──────────────────────────────────────────
LONG_THRESHOLD   = 32.0   # was 45 – reached in normal market
SHORT_THRESHOLD  = -32.0  # was -45

# ── Entry filters ──────────────────────────────────────────────
PRICE_ABOVE_AVG  = 0.2    # LONG:  price > vwap + 0.2%  (was 0.5)
PRICE_BELOW_AVG  = 0.2    # SHORT: price < vwap - 0.2%  (was 0.5)
VOL_SPIKE_RATIO  = 1.5    # current-hour vs 24h avg  (was 3.0)

# ── Funding ─────────────────────────────────────────────────────
FUNDING_HIGH =  0.05      # % – longs paying → SHORT bias
FUNDING_LOW  = -0.05      # % – shorts paying → LONG bias

# ── Exit params ──────────────────────────────────────────────────
PROFIT_LOCK_TRIGGER = 1.5  # % gain to enable profit lock
PROFIT_LOCK_FLOOR   = 0.5  # SL moves to entry + 0.5% (LONG)
MOONBAG_TRIGGER     = 15.0 # % to close 50%
MOONBAG_FRACTION    = 0.50
ATR_TRAIL_MULT      = 1.0  # trail distance = ATR × 1.0
HARD_STOP_PCT       = 2.0  # hard stop loss %
TIME_KILL_HOURS     = 4.0

# ── Antifragile ──────────────────────────────────────────────────
RATCHET_PCT        = 15.0  # % of new portfolio ATH gain → locked
DRAWDOWN_HALF      = 10.0  # % drawdown → bet × 0.5
DRAWDOWN_CLOSE     = 20.0  # % drawdown → close all + pause
PAUSE_SECONDS      = 30 * 60
COOLDOWN_SECONDS   = 30 * 60  # min gap between trades on same symbol

# ── Indicators ───────────────────────────────────────────────────
RSI_PERIOD         = 14
ATR_PERIOD         = 14
PRICE_HIST_LEN     = 30

# ── Binance API ──────────────────────────────────────────────────
BINANCE_PRICE   = "https://api.binance.com/api/v3/ticker/price"
BINANCE_24H     = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES  = "https://api.binance.com/api/v3/klines"
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/premiumIndex"

# ── Files ────────────────────────────────────────────────────────
TRADES_CSV = "apex_trades.csv"
LOG_FILE   = "apex_fast.log"

# ── Display ──────────────────────────────────────────────────────
DASH_W = 49   # dashboard inner width

# ══════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _lib in ("urllib3", "requests"):
    logging.getLogger(_lib).setLevel(logging.WARNING)
logger = logging.getLogger("apex")

# ══════════════════════════════════════════════════════════════════
#  HTTP + CACHE
# ══════════════════════════════════════════════════════════════════

_rl_lock   = Lock()
_rl_count  = 0
_rl_window = time.time()


def _get(url: str, params=None, timeout=8) -> Optional[dict]:
    """HTTP GET with Binance rate-limit guard (30 req / 60s) and 2 retries."""
    global _rl_count, _rl_window
    with _rl_lock:
        now = time.time()
        if now - _rl_window >= 60:
            _rl_count = 0
            _rl_window = now
        if _rl_count >= 28:
            wait = 60 - (now - _rl_window)
            if wait > 0:
                time.sleep(wait)
            _rl_count = 0
            _rl_window = time.time()
        _rl_count += 1

    delay = 2
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(delay); delay *= 2; continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 2:
                logger.error("API %s: %s", url.split("/")[-1], exc)
            else:
                time.sleep(delay); delay *= 2
    return None


class _Cache:
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._d: dict = {}
        self._t: dict = {}

    def get(self, k):
        if k in self._d and time.time() - self._t[k] < self._ttl:
            return self._d[k]
        return None

    def set(self, k, v):
        self._d[k] = v; self._t[k] = time.time()


_c_price   = _Cache(10)
_c_ticker  = _Cache(60)
_c_funding = _Cache(30)
_c_klines  = _Cache(120)

# ══════════════════════════════════════════════════════════════════
#  BINANCE DATA LAYER
# ══════════════════════════════════════════════════════════════════

def fetch_all_prices() -> Dict[str, float]:
    c = _c_price.get("all")
    if c: return c
    data = _get(BINANCE_PRICE)
    if not isinstance(data, list):
        return {}
    lookup = {}
    for item in data:
        try: lookup[item["symbol"]] = float(item["price"])
        except (KeyError, TypeError, ValueError): pass
    result = {sym: lookup[bsym] for sym, bsym in TOKENS.items() if bsym in lookup}
    if result: _c_price.set("all", result)
    return result


def fetch_all_tickers() -> Dict[str, dict]:
    """One call → 24h stats for all tokens."""
    c = _c_ticker.get("all")
    if c: return c
    data = _get(BINANCE_24H)
    if not isinstance(data, list):
        return {}
    lookup = {item["symbol"]: item for item in data if isinstance(item, dict)}
    result = {sym: lookup[bsym] for sym, bsym in TOKENS.items() if bsym in lookup}
    if result: _c_ticker.set("all", result)
    return result


def fetch_funding(symbol: str) -> Optional[float]:
    """Return funding rate % (positive = longs paying)."""
    c = _c_funding.get(symbol)
    if c is not None: return c
    bsym = TOKENS.get(symbol)
    if not bsym: return None
    data = _get(BINANCE_FUNDING, params={"symbol": bsym})
    if data:
        try:
            rate = float(data["lastFundingRate"]) * 100
            _c_funding.set(symbol, rate)
            return rate
        except (KeyError, TypeError, ValueError):
            pass
    return None


def fetch_klines(symbol: str, interval="1h", limit=26) -> Optional[list]:
    key = f"{symbol}_{interval}_{limit}"
    c = _c_klines.get(key)
    if c: return c
    bsym = TOKENS.get(symbol)
    if not bsym: return None
    data = _get(BINANCE_KLINES,
                params={"symbol": bsym, "interval": interval, "limit": limit})
    if isinstance(data, list) and data:
        _c_klines.set(key, data)
        return data
    return None

# ══════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════

class PriceHistory:
    def __init__(self):
        self._h: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=PRICE_HIST_LEN))

    def push(self, sym: str, price: float):
        self._h[sym].append(price)

    def get(self, sym: str) -> list:
        return list(self._h[sym])

    def ready(self, sym: str, n: int = RSI_PERIOD + 2) -> bool:
        return len(self._h[sym]) >= n


price_history = PriceHistory()


def calc_rsi(prices: list, period: int = RSI_PERIOD) -> float:
    if len(prices) < period + 1:
        return 50.0
    arr = np.array(prices, dtype=float)
    d   = np.diff(arr)
    g   = np.where(d > 0, d, 0.0)
    lo  = np.where(d < 0, -d, 0.0)
    ag  = float(np.mean(g[:period]))
    al  = float(np.mean(lo[:period]))
    for i in range(period, len(g)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + lo[i]) / period
    if al == 0: return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def calc_atr(symbol: str) -> Optional[float]:
    """ATR(14) using Wilder's smoothing on 1h klines."""
    klines = fetch_klines(symbol, "1h", ATR_PERIOD + 5)
    if not klines or len(klines) < ATR_PERIOD + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        try:
            h  = float(klines[i][2])
            lo = float(klines[i][3])
            pc = float(klines[i - 1][4])
            trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
        except (IndexError, TypeError, ValueError):
            pass
    if len(trs) < ATR_PERIOD:
        return None
    atr = float(np.mean(trs[:ATR_PERIOD]))
    for i in range(ATR_PERIOD, len(trs)):
        atr = (atr * (ATR_PERIOD - 1) + trs[i]) / ATR_PERIOD
    return atr


def calc_volume_spike(symbol: str) -> bool:
    """
    Compare estimated current-hour volume against 24h average hourly.
    Uses 1h klines: last 25 candles.
    """
    klines = fetch_klines(symbol, "1h", 26)
    if not klines or len(klines) < 3:
        return False
    try:
        # Volumes of last 24 completed candles
        vols = [float(k[5]) for k in klines[:-1][-24:]]
        if not vols: return False
        avg_vol = float(np.mean(vols))
        if avg_vol == 0: return False
        # Current (incomplete) candle — scale to full hour
        cur_k     = klines[-1]
        open_ts   = int(cur_k[0])
        now_ms    = int(time.time() * 1000)
        pct_done  = min((now_ms - open_ts) / (3_600_000), 1.0)
        cur_vol   = float(cur_k[5])
        est_vol   = cur_vol / pct_done if pct_done > 0.05 else cur_vol
        return est_vol > avg_vol * VOL_SPIKE_RATIO
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return False

# ══════════════════════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════

class SignalEngine:

    def momentum_score(self, symbol: str, ticker: dict) -> float:
        """
        score = price_change×0.4 + volume_ratio×0.3 + rsi_signal×0.3
        Returns [-100, +100].
        """
        try:
            price_chg = float(ticker.get("priceChangePercent", 0))
        except (TypeError, ValueError):
            price_chg = 0.0

        # Normalise price change: ±4% → ±100 pts  (was ÷15, too insensitive)
        pc_norm = float(np.clip(price_chg / 4.0 * 100.0, -100, 100))

        # Placeholder; vol_norm is injected in combined_score (needs all tickers)
        vol_norm = 0.0

        # RSI signal
        prices = price_history.get(symbol)
        if price_history.ready(symbol):
            rsi = calc_rsi(prices)
            rsi_sig = 1.0 if rsi > 60 else (-1.0 if rsi < 40 else 0.0)
        else:
            rsi_sig = 0.0
        rsi_norm = rsi_sig * 100.0

        score = pc_norm * 0.4 + vol_norm * 0.3 + rsi_norm * 0.3
        return float(np.clip(score, -100, 100))

    def funding_signal(self, symbol: str) -> Tuple[str, Optional[float]]:
        rate = fetch_funding(symbol)
        if rate is None:
            return "NEUTRAL", None
        if rate > FUNDING_HIGH:
            return "SHORT_BIAS", rate
        if rate < FUNDING_LOW:
            return "LONG_BIAS", rate
        return "NEUTRAL", rate

    def combined_score(self, symbol: str,
                       ticker: dict,
                       all_tickers: Dict[str, dict]) -> dict:
        # Momentum (partial — no vol_norm yet)
        mom = self.momentum_score(symbol, ticker)

        # Volume ratio vs median of all tokens
        vols = []
        for t in all_tickers.values():
            try: vols.append(float(t.get("quoteVolume", 0)))
            except (TypeError, ValueError): pass
        median_vol = float(np.median(vols)) if vols else 1.0

        try:
            own_vol = float(ticker.get("quoteVolume", 0))
        except (TypeError, ValueError):
            own_vol = 0.0

        vol_ratio = own_vol / median_vol if median_vol > 0 else 1.0
        # vol_ratio 2x → +100 pts contribution, 0.5x → -50 pts
        vol_norm  = float(np.clip((vol_ratio - 1.0) * 100.0, -100, 100))
        mom += vol_norm * 0.3
        mom  = float(np.clip(mom, -100, 100))

        fund_sig, rate = self.funding_signal(symbol)
        spike          = calc_volume_spike(symbol)

        final = mom
        if fund_sig == "LONG_BIAS":  final += 15
        if fund_sig == "SHORT_BIAS": final -= 15
        if spike: final *= 1.2

        final = float(np.clip(final, -100, 100))

        direction = ("LONG" if final >= 10
                     else "SHORT" if final <= -10
                     else "NEUTRAL")

        return {
            "score":     round(final, 1),
            "direction": direction,
            "funding":   fund_sig,
            "funding_rate": rate,
            "spike":     spike,
            "momentum":  round(mom, 1),
        }


signal_engine = SignalEngine()

# ══════════════════════════════════════════════════════════════════
#  POSITION
# ══════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol:       str
    direction:    str          # LONG | SHORT
    entry_price:  float
    size_usd:     float
    open_time:    float = field(default_factory=time.time)

    # Dynamic state
    max_pnl_pct:    float = 0.0
    trailing_stop:  Optional[float] = None
    profit_locked:  bool = False
    moonbag_done:   bool = False

    # Metadata
    signal_score:   float = 0.0
    funding_rate:   Optional[float] = None
    volume_spike:   bool = False
    atr_entry:      Optional[float] = None

    # ── PnL ───────────────────────────────────────────────────────

    def pnl_pct(self, price: float) -> float:
        if self.entry_price == 0: return 0.0
        return ((price - self.entry_price) / self.entry_price * 100
                if self.direction == "LONG"
                else (self.entry_price - price) / self.entry_price * 100)

    def pnl_usd(self, price: float) -> float:
        return self.pnl_pct(price) / 100 * self.size_usd

    def hours_open(self) -> float:
        return (time.time() - self.open_time) / 3600

    # ── Trailing stop ─────────────────────────────────────────────

    def update_trailing(self, price: float):
        pct = self.pnl_pct(price)
        if pct > self.max_pnl_pct:
            self.max_pnl_pct = pct

        # ATR trailing activates after moonbag is taken
        if not self.moonbag_done or not self.atr_entry:
            return

        dist = self.atr_entry * ATR_TRAIL_MULT
        if self.direction == "LONG":
            stop = price - dist
            self.trailing_stop = max(self.trailing_stop or 0.0, stop)
        else:
            stop = price + dist
            self.trailing_stop = (stop if self.trailing_stop is None
                                  else min(self.trailing_stop, stop))

    # ── Exit check ────────────────────────────────────────────────

    def should_exit(self, price: float) -> Optional[str]:
        self.update_trailing(price)
        pct = self.pnl_pct(price)

        if pct <= -HARD_STOP_PCT:
            return "STOP_LOSS"

        # Profit lock: once +1.5%, SL shifts to +0.5%
        if pct >= PROFIT_LOCK_TRIGGER:
            self.profit_locked = True
        if self.profit_locked and pct <= PROFIT_LOCK_FLOOR:
            return "PROFIT_LOCK"

        if self.trailing_stop is not None:
            if self.direction == "LONG"  and price <= self.trailing_stop: return "TRAIL_STOP"
            if self.direction == "SHORT" and price >= self.trailing_stop: return "TRAIL_STOP"

        if self.hours_open() >= TIME_KILL_HOURS:
            return "TIME_KILL"

        return None

    def should_moonbag(self, price: float) -> bool:
        return not self.moonbag_done and self.pnl_pct(price) >= MOONBAG_TRIGGER

# ══════════════════════════════════════════════════════════════════
#  PORTFOLIO
# ══════════════════════════════════════════════════════════════════

class Portfolio:
    def __init__(self):
        self.cash          = START_BALANCE
        self.locked        = 0.0          # profit ratchet: never traded again
        self.positions:    List[Position] = []
        self.trade_log:    List[dict]     = []
        self.ath_value     = START_BALANCE
        self.paused_until  = 0.0
        self.cooldowns:    Dict[str, float] = {}
        self.today_pnl     = 0.0
        self._ensure_csv()

    # ── Helpers ───────────────────────────────────────────────────

    def total_value(self, prices: Dict[str, float] = None) -> float:
        open_val = sum(
            p.size_usd + p.pnl_usd(prices.get(p.symbol, p.entry_price))
            for p in self.positions
        ) if prices else 0.0
        return self.cash + open_val

    def drawdown_pct(self, prices: Dict[str, float] = None) -> float:
        tv = self.total_value(prices)
        return max(0.0, (self.ath_value - tv) / self.ath_value * 100) if self.ath_value else 0.0

    def roi_pct(self, prices: Dict[str, float] = None) -> float:
        return (self.total_value(prices) - START_BALANCE) / START_BALANCE * 100

    def win_rate(self) -> float:
        if not self.trade_log: return 0.0
        return sum(1 for t in self.trade_log if float(t["pnl_usd"]) > 0) / len(self.trade_log) * 100

    def _dd_mult(self, prices: Dict[str, float]) -> float:
        return 0.5 if self.drawdown_pct(prices) >= DRAWDOWN_HALF else 1.0

    def bet_size(self, mult: float = 1.0) -> float:
        total = self.total_value()
        for lo, hi, bet in BET_TIERS:
            if lo <= total < hi:
                return round(bet * mult, 2)
        return round(BET_TIERS[-1][2] * mult, 2)

    def is_paused(self) -> bool:
        return time.time() < self.paused_until

    def cooling_down(self, sym: str) -> bool:
        return (time.time() - self.cooldowns.get(sym, 0)) < COOLDOWN_SECONDS

    # ── Profit ratchet ────────────────────────────────────────────

    def update_ath(self, prices: Dict[str, float]):
        tv = self.total_value(prices)
        if tv > self.ath_value:
            gain   = tv - self.ath_value
            lock   = gain * (RATCHET_PCT / 100)
            self.locked    += lock
            self.cash      -= lock
            self.ath_value  = tv
            logger.info("ATH $%.2f → locked $%.2f (total $%.2f)",
                        tv, lock, self.locked)

    # ── Drawdown protection ───────────────────────────────────────

    def check_drawdown(self, prices: Dict[str, float]) -> bool:
        dd = self.drawdown_pct(prices)
        if dd >= DRAWDOWN_CLOSE:
            logger.warning("Drawdown %.1f%% – closing all, pausing %ds",
                           dd, PAUSE_SECONDS)
            for pos in list(self.positions):
                self.close(pos, prices.get(pos.symbol, pos.entry_price),
                           "DD_PROTECT")
            self.paused_until = time.time() + PAUSE_SECONDS
            return True
        return False

    # ── Entry / Exit ──────────────────────────────────────────────

    def open(self, symbol: str, direction: str, price: float,
             sig: dict, atr: Optional[float]) -> Optional[Position]:
        if len(self.positions) >= MAX_POSITIONS: return None
        if self.cooling_down(symbol): return None
        if self.is_paused(): return None

        mult = self._dd_mult({symbol: price})
        size = self.bet_size(mult)
        if size > self.cash: return None

        pos = Position(
            symbol=symbol, direction=direction,
            entry_price=price, size_usd=size,
            signal_score=sig.get("score", 0),
            funding_rate=sig.get("funding_rate"),
            volume_spike=sig.get("spike", False),
            atr_entry=atr,
        )
        self.positions.append(pos)
        self.cash -= size
        logger.info("OPEN %s %s @ %.6f  $%.2f  score=%.1f",
                    direction, symbol, price, size, sig.get("score", 0))
        return pos

    def moonbag(self, pos: Position, price: float):
        pnl_pct = pos.pnl_pct(price)
        sell    = pos.size_usd * MOONBAG_FRACTION
        pnl_usd = sell * pnl_pct / 100
        self.cash   += sell + pnl_usd
        pos.size_usd *= (1 - MOONBAG_FRACTION)
        pos.moonbag_done = True
        self.today_pnl  += pnl_usd
        logger.info("MOONBAG %s %s @ %.6f  $%.2f",
                    pos.direction, pos.symbol, price, pnl_usd)
        self._log(pos, price, pnl_usd, pnl_pct, "MOONBAG_50PCT")

    def close(self, pos: Position, price: float, reason: str):
        pnl_pct = pos.pnl_pct(price)
        pnl_usd = pos.pnl_usd(price)
        self.cash          += pos.size_usd + pnl_usd
        self.today_pnl     += pnl_usd
        self.cooldowns[pos.symbol] = time.time()
        self.positions.remove(pos)
        logger.info("CLOSE %s %s @ %.6f  %s  $%.2f (%.2f%%)",
                    pos.direction, pos.symbol, price, reason, pnl_usd, pnl_pct)
        self._log(pos, price, pnl_usd, pnl_pct, reason)

    # ── CSV ───────────────────────────────────────────────────────

    def _ensure_csv(self):
        if not os.path.exists(TRADES_CSV):
            with open(TRADES_CSV, "w", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp", "symbol", "direction",
                    "entry_price", "exit_price",
                    "pnl_usd", "pnl_pct", "exit_reason",
                    "signal_score", "funding_rate",
                    "volume_spike", "holding_time_minutes",
                ])

    def _log(self, pos: Position, exit_price: float,
             pnl_usd: float, pnl_pct: float, reason: str):
        row = {
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "symbol":               pos.symbol,
            "direction":            pos.direction,
            "entry_price":          round(pos.entry_price, 8),
            "exit_price":           round(exit_price, 8),
            "pnl_usd":              round(pnl_usd, 4),
            "pnl_pct":              round(pnl_pct, 4),
            "exit_reason":          reason,
            "signal_score":         round(pos.signal_score, 2),
            "funding_rate":         round(pos.funding_rate or 0, 4),
            "volume_spike":         pos.volume_spike,
            "holding_time_minutes": round(pos.hours_open() * 60, 1),
        }
        self.trade_log.append(row)
        try:
            with open(TRADES_CSV, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=list(row.keys())).writerow(row)
        except Exception as exc:
            logger.error("CSV: %s", exc)

# ══════════════════════════════════════════════════════════════════
#  TRADING ENGINE
# ══════════════════════════════════════════════════════════════════

class TradingEngine:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        self.last_sigs: Dict[str, dict] = {}

    def tick(self, prices: Dict[str, float],
             tickers: Dict[str, dict]):
        port = self.portfolio

        port.update_ath(prices)
        if port.check_drawdown(prices):
            return
        if port.is_paused():
            return

        # Exits
        for pos in list(port.positions):
            p = prices.get(pos.symbol, pos.entry_price)
            if pos.should_moonbag(p):
                port.moonbag(pos, p)
            reason = pos.should_exit(p)
            if reason:
                port.close(pos, p, reason)

        # Signals for all tokens
        for sym, ticker in tickers.items():
            sig = signal_engine.combined_score(sym, ticker, tickers)
            self.last_sigs[sym] = sig

        # Entries
        for sym, sig in sorted(self.last_sigs.items(),
                                key=lambda x: abs(x[1]["score"]),
                                reverse=True):
            if len(port.positions) >= MAX_POSITIONS:
                break
            if any(p.symbol == sym for p in port.positions):
                continue

            price  = prices.get(sym)
            ticker = tickers.get(sym, {})
            if price is None:
                continue

            score  = sig["score"]
            direc  = sig["direction"]
            spike  = sig["spike"]
            fund   = sig["funding"]
            atr    = calc_atr(sym)

            if direc == "LONG" and self._ok_long(score, spike, price, ticker):
                port.open(sym, "LONG", price, sig, atr)

            elif direc == "SHORT" and self._ok_short(score, spike, fund, price, ticker):
                port.open(sym, "SHORT", price, sig, atr)

    def _vwap_diff(self, price: float, ticker: dict) -> float:
        try:
            vwap = float(ticker["weightedAvgPrice"])
            return (price - vwap) / vwap * 100
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _ok_long(self, score, spike, price, ticker) -> bool:
        # spike already boosts score ×1.2 in combined_score
        return (score >= LONG_THRESHOLD and
                self._vwap_diff(price, ticker) >= PRICE_ABOVE_AVG)

    def _ok_short(self, score, spike, funding, price, ticker) -> bool:
        # Block SHORT only when funding is LONG_BIAS (shorts already pay premium)
        return (score <= SHORT_THRESHOLD and
                funding != "LONG_BIAS" and
                self._vwap_diff(price, ticker) <= -PRICE_BELOW_AVG)

# ══════════════════════════════════════════════════════════════════
#  TERMINAL DISPLAY
# ══════════════════════════════════════════════════════════════════

G   = Fore.GREEN;   R  = Fore.RED;    Y = Fore.YELLOW
C   = Fore.CYAN;    M  = Fore.MAGENTA; W = Fore.WHITE
DG  = Fore.LIGHTBLACK_EX
BLD = Style.BRIGHT; RST = Style.RESET_ALL


def _c(v: float):
    return G if v > 0 else (R if v < 0 else W)


def _pct(v: float) -> str:
    return f"{_c(v)}{'+' if v >= 0 else ''}{v:.2f}%{RST}"


def _fp(p: float) -> str:
    if p >= 10000: return f"${p:,.0f}"
    if p >= 100:   return f"${p:,.2f}"
    if p >= 1:     return f"${p:.4f}"
    if p >= 0.01:  return f"${p:.5f}"
    return         f"${p:.7f}"


def _bar(score: float, threshold: float, w: int = 8) -> str:
    if threshold == 0: return DG + "░" * w + RST
    pct    = min(abs(score) / abs(threshold), 1.0)
    filled = int(pct * w)
    clr    = G if score > 0 else R
    return clr + "█" * filled + DG + "░" * (w - filled) + RST


def _row(txt: str) -> str:
    inner = (" " + txt)
    # strip ANSI for length calc so we can pad correctly
    import re
    plain = re.sub(r'\x1b\[[0-9;]*m', '', inner)
    pad   = max(0, DASH_W - 2 - len(plain))
    return DG + "|" + RST + inner + " " * pad + DG + "|" + RST


def _sep():
    return DG + "+" + "-" * (DASH_W - 2) + "+" + RST


def render(port: Portfolio, sigs: Dict[str, dict],
           prices: Dict[str, float],
           btc_p: Optional[float], t0: float):
    os.system("cls" if os.name == "nt" else "clear")

    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    btcs  = f"BTC {_fp(btc_p)}" if btc_p else "BTC ---"
    up    = int(time.time() - t0)
    h, r  = divmod(up, 3600); m, s = divmod(r, 60)

    tv    = port.total_value(prices)
    roi   = port.roi_pct(prices)
    dd    = port.drawdown_pct(prices)
    wr    = port.win_rate()
    n_tr  = len(port.trade_log)
    mult  = port._dd_mult(prices) if prices else 1.0
    bet   = port.bet_size(mult)
    paused = port.is_paused()

    print(DG + "+" + "-" * (DASH_W - 2) + "+" + RST)
    hdr = f" APEX FAST  |  {now}  |  {btcs}"
    print(_row(C + BLD + hdr + RST))
    print(_sep())

    # Portfolio
    print(_row(BLD + "PORTFOLIO" + RST))
    print(_row(
        f"Cash:{W} ${port.cash:,.2f}{RST}  "
        f"Open:{C} ${tv - port.cash:,.2f}{RST}  "
        f"Total:{BLD} ${tv:,.2f}{RST}"
    ))
    print(_row(
        f"ROI: {_pct(roi)}  "
        f"DD: {(R if dd > 5 else DG)}-{dd:.1f}%{RST}  "
        f"Locked: {M}${port.locked:,.2f}{RST}"
    ))
    print(_row(
        f"Trades:{W} {n_tr}{RST}  "
        f"WR: {G}{wr:.0f}%{RST}  "
        f"Today: {_pct(port.today_pnl / START_BALANCE * 100)}  "
        f"Bet:{Y} ${bet:.0f}{RST}"
    ))
    if paused:
        left = int(port.paused_until - time.time())
        print(_row(R + BLD + f"! PAUSED – drawdown guard, resumes in {left}s" + RST))
    print(_sep())

    # Top signals
    top = sorted(
        [(s, d) for s, d in sigs.items() if abs(d.get("score", 0)) > 10],
        key=lambda x: abs(x[1]["score"]), reverse=True
    )[:6]
    print(_row(BLD + f"TOP SIGNALS  (entry>±{LONG_THRESHOLD:.0f})" + RST))
    if not top:
        print(_row(DG + "  Signals below threshold" + RST))
    else:
        for sym, sig in top:
            sc    = sig["score"]
            dir_s = (G + "LONG " + RST) if sig["direction"] == "LONG" else (R + "SHORT" + RST)
            spk_s = G + "SPK" + RST if sig["spike"] else DG + "   " + RST
            rate  = sig.get("funding_rate")
            fs    = f"{rate:>+.3f}%" if rate is not None else "  N/A "
            bar   = _bar(sc, LONG_THRESHOLD if sc >= 0 else SHORT_THRESHOLD)
            print(_row(
                f"{W}{sym:<6}{RST}{_c(sc)}{sc:>+6.1f}{RST} "
                f"{dir_s} {bar} {spk_s} {DG}{fs}{RST}"
            ))
    print(_sep())

    # Active positions
    n_pos = len(port.positions)
    print(_row(BLD + f"ACTIVE POSITIONS  ({n_pos}/{MAX_POSITIONS})" + RST))
    if not port.positions:
        print(_row(DG + "  No open positions" + RST))
    else:
        for pos in port.positions:
            p      = prices.get(pos.symbol, pos.entry_price)
            pnl_p  = pos.pnl_pct(p)
            pnl_u  = pos.pnl_usd(p)
            dir_s  = (G + "LONG " + RST) if pos.direction == "LONG" else (R + "SHORT" + RST)
            tag    = ""
            if pos.moonbag_done:  tag += M + "[M]" + RST
            if pos.profit_locked: tag += Y + "[PL]" + RST
            if pos.trailing_stop: tag += C + "[T]" + RST
            print(_row(
                f"{dir_s} {W}{pos.symbol:<6}{RST}"
                f"entry:{_fp(pos.entry_price)} "
                f"PnL:{_pct(pnl_p)}(${pnl_u:+.1f}) "
                f"{tag}"
            ))
    print(_sep())

    # Last 10 trades
    print(_row(BLD + "LAST 10 TRADES" + RST))
    trades = port.trade_log[-10:]
    if not trades:
        print(_row(DG + "  No trades yet" + RST))
    else:
        for t in reversed(trades):
            pnl  = float(t["pnl_usd"])
            dir_s = (G + "LONG " + RST) if t["direction"] == "LONG" else (R + "SHORT" + RST)
            print(_row(
                f"{dir_s}{W}{t['symbol']:<6}{RST}"
                f"{_c(pnl)}${pnl:>+8.2f}{RST}  "
                f"{DG}{t['exit_reason']:<14}{RST}"
                f"{DG}{float(t['pnl_pct']):>+.1f}%{RST}"
            ))

    print(DG + "+" + "-" * (DASH_W - 2) + "+" + RST)
    print(DG + f"  Uptime {h:02d}:{m:02d}:{s:02d}"
          f"  |  PAPER TRADING – NO REAL FUNDS" + RST)

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def warm_up():
    print(f"\n  APEX FAST  –  warming up ({WARM_UP_SAMPLES} samples)...\n")
    for i in range(WARM_UP_SAMPLES):
        prices = fetch_all_prices()
        for sym, p in prices.items():
            price_history.push(sym, p)
        bar = ("█" * (i + 1)).ljust(WARM_UP_SAMPLES, "░")
        print(f"\r  [{bar}] {i+1}/{WARM_UP_SAMPLES}", end="", flush=True)
        if i < WARM_UP_SAMPLES - 1:
            time.sleep(UPDATE_INTERVAL)
    print("\n\n  Ready – starting trading loop.\n")
    time.sleep(1)


def main():
    logger.info("APEX FAST started. Balance: $%.2f", START_BALANCE)
    port   = Portfolio()
    engine = TradingEngine(port)
    t0     = time.time()

    warm_up()

    errors = 0
    while True:
        loop_start = time.time()
        try:
            prices  = fetch_all_prices()
            tickers = fetch_all_tickers()
            btc_p   = prices.get("BTC")

            if not prices:
                logger.warning("No prices – skipping")
                time.sleep(UPDATE_INTERVAL)
                continue

            for sym, p in prices.items():
                price_history.push(sym, p)

            engine.tick(prices, tickers)
            render(port, engine.last_sigs, prices, btc_p, t0)
            errors = 0

        except KeyboardInterrupt:
            _shutdown(port, locals().get("prices", {}))
            return
        except Exception as exc:
            errors += 1
            logger.error("Loop #%d: %s\n%s", errors, exc, traceback.format_exc())
            if errors >= 5:
                time.sleep(60); errors = 0

        time.sleep(max(0.0, UPDATE_INTERVAL - (time.time() - loop_start)))


def _shutdown(port: Portfolio, prices: dict):
    tv  = port.total_value(prices)
    roi = port.roi_pct(prices)
    n   = len(port.trade_log)
    print("\n\n  ─── APEX FAST SESSION SUMMARY ───────────────────")
    print(f"  Final balance  : ${tv:,.2f}")
    print(f"  ROI            : {'+' if roi >= 0 else ''}{roi:.2f}%")
    print(f"  Locked profit  : ${port.locked:,.2f}")
    print(f"  Total trades   : {n}")
    print(f"  Win rate       : {port.win_rate():.1f}%")
    print(f"  Log → {LOG_FILE}     Trades → {TRADES_CSV}")
    print(f"  ──────────────────────────────────────────────────\n")
    logger.info("Stopped. Final $%.2f ROI %.2f%% Trades %d", tv, roi, n)


if __name__ == "__main__":
    main()
