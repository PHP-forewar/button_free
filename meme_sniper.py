#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
5X MEME SNIPER  -  paper trading bot (REAL PUL YO'Q / NO REAL MONEY)
====================================================================

Realistik scalping simulyatori. Binance USD-M Futures matematikasini
simulyatsiya qiladi: 5x leverage, isolated margin, taker komissiya.
Hech qanday real Binance hisob ulanmaydi - faqat ochiq narx ma'lumotlari
o'qiladi (read-only public endpoints).

Strategiya:
  * Tez scalping (12s skanerlash), moonbag YO'Q.
  * IKKI TOMONLAMA: LONG (narx ko'tarilsa) VA SHORT (narx tushsa).
    Yo'nalish avtomatik tanlanadi (ALLOW_LONG / ALLOW_SHORT).
  * 2 bosqichli kapital (DEGEN SPRINT -> PRO MODE I/II/III).
  * ATR asosida 2 ta TP/SL rejimi (1% micro-scalp / 2% momentum).
  * Confluence engine: 4 ta filtr (BTC trend, volume, momentum, funding).
    Kirish uchun BARCHA 4 filtr yashil bo'lishi shart (yo'nalishga mos).
    SHORT uchun filtrlar teskari: BTC tushyapti, momentum manfiy, h.k.

Ishga tushirish (Windows PowerShell):
    pip install requests pandas numpy colorama
    python meme_sniper.py

Pure-funksiyalarni tekshirish (network kerak emas):
    python meme_sniper.py --selftest
"""

import os
import sys
import csv
import time
import math
import logging
from collections import deque, defaultdict
from datetime import datetime

import requests

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy spec talabi
    print("numpy kerak:  pip install numpy")
    raise

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
    _COLOR = True
except ImportError:  # pragma: no cover - rangsiz fallback
    class _Dummy:
        def __getattr__(self, _):
            return ""
    Fore = Style = _Dummy()
    _COLOR = False


# ═══════════════════════════════════════════════════════════════════
# ASOSIY PARAMETRLAR
# ═══════════════════════════════════════════════════════════════════
START_BALANCE = 200.0
LEVERAGE = 5                 # 5x (faqat MATEMATIK simulyatsiya)
MARGIN_TYPE = "isolated"     # zarar faqat stake bilan cheklangan
UPDATE_INTERVAL = 12         # soniya (tez skanerlash)
MAX_POSITIONS = 3
PAPER_TRADING = True         # real wallet YO'Q

TAKER_FEE = 0.0004           # 0.04% taker

# Klines (ATR/momentum) va kunlik (7d volume) qayta-yuklash kadensi.
# Bitta loop = UPDATE_INTERVAL soniya.
SCAN_EVERY_LOOPS = 5         # ~60s: ATR + 30m momentum klines yangilanadi
DAILY_REFRESH_LOOPS = 150    # ~30min: 7 kunlik o'rtacha volume yangilanadi
MAX_REQ_PER_MIN = 28         # rate limit (<30/daqiqa, xavfsiz chegara)

STATS_EVERY_TRADES = 50      # har 50 savdoda statistika chiqadi

# Binance USD-M Futures public endpointlar (read-only)
BASE = "https://fapi.binance.com"
EP_PRICE = BASE + "/fapi/v1/ticker/price"
EP_24HR = BASE + "/fapi/v1/ticker/24hr"
EP_KLINES = BASE + "/fapi/v1/klines"
EP_PREMIUM = BASE + "/fapi/v1/premiumIndex"

BTC_SYMBOL = "BTCUSDT"

# Tokenlar:  ko'rinadigan nom -> Binance Futures juftligi
TOKENS = {
    "WIF":   "WIFUSDT",
    "PEPE":  "1000PEPEUSDT",
    "BONK":  "1000BONKUSDT",
    "DOGE":  "DOGEUSDT",
    "FLOKI": "1000FLOKIUSDT",
    "SHIB":  "1000SHIBUSDT",
    "NEIRO": "NEIROUSDT",
}
PAIR_TO_NAME = {v: k for k, v in TOKENS.items()}

# Savdo yo'nalishlari (ikki tomonlama)
ALLOW_LONG = True            # narx ko'tarilishidan foyda
ALLOW_SHORT = True           # narx tushishidan foyda

# Confluence filtr chegaralari (YUMSHATILGAN — ko'proq signal)
BTC_24H_FLOOR = -3.0         # LONG:  BTC 24h > -3%  (faqat kuchli tushishda blok)
BTC_24H_CEIL = 3.0           # SHORT: BTC 24h < +3%  (faqat kuchli ko'tarilishda blok)
VOL_RATIO_MIN = 0.8          # 24h vol / 7d avg >= 0.8 (o'rtacha faollik yetarli)
MOM_MIN, MOM_MAX = 0.3, 8.0  # 30m o'zgarish kattaligi [0.3%, 8.0%] (keng oyna)
FOMO_LEVEL = 15.0            # |o'zgarish| 15% dan oshsa FOMO/panika
FUNDING_MAX = 0.003          # LONG:  funding < +0.3%
FUNDING_MIN = -0.003         # SHORT: funding > -0.3%
MIN_FILTERS = 3              # 3/4 filtr yashil bo'lsa kirish (4 shart emas)

# Fayllar
CSV_FILE = "trades_sniper.csv"
LOG_FILE = "bot_sniper.log"

CSV_HEADER = [
    "timestamp", "symbol", "side", "mode", "stake", "leverage",
    "entry_price", "exit_price", "price_change_pct",
    "pnl_on_stake_pct", "pnl_usd", "fee", "result",
    "balance_after", "stage", "btc_1h", "volume_ratio", "funding",
]


# ═══════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════
logger = logging.getLogger("meme_sniper")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)


# ═══════════════════════════════════════════════════════════════════
# KAPITAL BOSQICHLARI (AVTOPILOT)
# ═══════════════════════════════════════════════════════════════════
def get_stake(balance):
    """Bosqichga qarab keyingi savdo uchun jilov (margin)."""
    if balance < 1000:
        return balance * 0.50      # DEGEN SPRINT (50%, variance past)
    elif balance < 5000:
        return 150.0               # PRO MODE I
    elif balance < 10000:
        return 300.0               # PRO MODE II
    else:
        return 600.0               # PRO MODE III


def stage_name(balance):
    if balance < 1000:
        return "DEGEN SPRINT"
    elif balance < 5000:
        return "PRO MODE I"
    elif balance < 10000:
        return "PRO MODE II"
    else:
        return "PRO MODE III"


def stake_label(balance):
    """Dashboard uchun jilov foizi yorlig'i."""
    if balance < 1000:
        return "50%"
    stake = get_stake(balance)
    pct = (stake / balance * 100) if balance > 0 else 0.0
    return f"~{pct:.0f}%"


# ═══════════════════════════════════════════════════════════════════
# TP/SL REJIMI (ATR asosida avto-tanlash)
# ═══════════════════════════════════════════════════════════════════
def select_mode(volatility):
    """
    volatility = ATR(14) / current_price * 100

    REJIM A - "1% MICRO-SCALP" (flat bozor): volatility < 1.5%
        TP narx +1.0%  (stake +5%) ,  SL narx -1.5%  (stake -7.5%)
    REJIM B - "2% MOMENTUM" (faol bozor): volatility >= 1.5%
        TP narx +2.0%  (stake +10%),  SL narx -2.0%  (stake -10%)

    Default: B (volatility None bo'lsa).
    """
    if volatility is not None and volatility < 1.5:
        return {"mode": "1%", "label": "1% MICRO-SCALP",
                "tp": 1.0, "sl": -1.5}
    return {"mode": "2%", "label": "2% MOMENTUM",
            "tp": 2.0, "sl": -2.0}


# ═══════════════════════════════════════════════════════════════════
# INDIKATORLAR (numpy)
# ═══════════════════════════════════════════════════════════════════
def atr_from_klines(klines, period=14):
    """ATR(period) klines listidan. Klines yetarli bo'lmasa None."""
    if not klines or len(klines) < period + 1:
        return None
    highs = np.array([float(k[2]) for k in klines])
    lows = np.array([float(k[3]) for k in klines])
    closes = np.array([float(k[4]) for k in klines])
    prev_close = closes[:-1]
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - prev_close),
        np.abs(lows[1:] - prev_close),
    ])
    if len(tr) < period:
        return None
    return float(np.mean(tr[-period:]))


def pct_change_over(klines, periods):
    """Oxirgi 'periods' ta sham oldingi yopilishga nisbatan % o'zgarish."""
    if not klines or len(klines) < periods + 1:
        return None
    closes = [float(k[4]) for k in klines]
    past = closes[-(periods + 1)]
    now = closes[-1]
    if past == 0:
        return None
    return (now - past) / past * 100.0


def volatility_pct(klines, last_price, period=14):
    """ATR/price * 100."""
    atr = atr_from_klines(klines, period)
    if atr is None or not last_price:
        return None
    return atr / last_price * 100.0


def avg_quote_volume_7d(daily_klines):
    """Oxirgi 7 ta YAKUNLANGAN kunning o'rtacha quote volume'i."""
    if not daily_klines or len(daily_klines) < 2:
        return None
    # oxirgi sham hali yakunlanmagan -> tashlab yuboramiz
    completed = daily_klines[:-1][-7:]
    if not completed:
        return None
    vols = [float(k[7]) for k in completed]  # index 7 = quoteAssetVolume
    return sum(vols) / len(vols)


# ═══════════════════════════════════════════════════════════════════
# RATE-LIMITED API KLIENT
# ═══════════════════════════════════════════════════════════════════
class RateLimitedAPI:
    """max ~30 so'rov/daqiqa. Xato bo'lsa log yozadi, None qaytaradi."""

    def __init__(self, max_per_min=MAX_REQ_PER_MIN):
        self.max_per_min = max_per_min
        self._calls = deque()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "meme-sniper-paper/1.0"})

    def _throttle(self):
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self.max_per_min:
            sleep_for = 60 - (now - self._calls[0]) + 0.05
            if sleep_for > 0:
                logger.info("Rate limit: %.1fs kutilmoqda", sleep_for)
                time.sleep(sleep_for)
        self._calls.append(time.time())

    def get(self, url, params=None, timeout=10):
        self._throttle()
        try:
            r = self.session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # API xato -> log, davom et
            logger.error("API xato %s params=%s : %s", url, params, e)
            return None

    # --- batch endpointlar (bitta so'rovda hammasi) ---
    def all_prices(self):
        data = self.get(EP_PRICE)
        if not data:
            return None
        return {d["symbol"]: float(d["price"]) for d in data}

    def all_24hr(self):
        data = self.get(EP_24HR)
        if not data:
            return None
        out = {}
        for d in data:
            try:
                out[d["symbol"]] = {
                    "change_pct": float(d["priceChangePercent"]),
                    "quote_volume": float(d["quoteVolume"]),
                }
            except (KeyError, ValueError):
                continue
        return out

    def all_funding(self):
        data = self.get(EP_PREMIUM)
        if not data:
            return None
        out = {}
        for d in data:
            try:
                out[d["symbol"]] = float(d["lastFundingRate"])
            except (KeyError, ValueError):
                continue
        return out

    def klines(self, symbol, interval, limit):
        return self.get(EP_KLINES, params={
            "symbol": symbol, "interval": interval, "limit": limit})


# ═══════════════════════════════════════════════════════════════════
# POZITSIYA
# ═══════════════════════════════════════════════════════════════════
class Position:
    def __init__(self, name, pair, entry_price, stake, mode_info,
                 stage, btc_1h, volume_ratio, funding, side="LONG"):
        self.name = name
        self.pair = pair
        self.side = side                          # "LONG" / "SHORT"
        self.entry_price = entry_price
        self.stake = stake
        self.position_size = stake * LEVERAGE     # 5x notional
        self.mode = mode_info["mode"]             # "1%" / "2%"
        self.mode_label = mode_info["label"]
        # tp/sl - POZITSIYA FOYDASIGA nisbatan (LONG ham, SHORT ham bir xil)
        self.tp = mode_info["tp"]                 # foyda % (musbat)
        self.sl = mode_info["sl"]                 # foyda % (manfiy)
        self.entry_time = time.time()
        # kirishdagi snapshot (CSV uchun)
        self.stage = stage
        self.btc_1h = btc_1h
        self.volume_ratio = volume_ratio
        self.funding = funding
        # jonli holat
        self.current_price = entry_price

    def update(self, price):
        self.current_price = price

    def price_change_pct(self):
        """Narxning xom o'zgarishi (yo'nalishsiz)."""
        return (self.current_price - self.entry_price) / self.entry_price * 100.0

    def directional_change(self):
        """Narx o'zgarishi POZITSIYA FOYDASIGA nisbatan.
        LONG  -> +narx oshsa musbat;  SHORT -> +narx tushsa musbat."""
        raw = self.price_change_pct()
        return raw if self.side == "LONG" else -raw

    def pnl_on_stake_pct(self):
        # isolated margin: -100% dan past tushmaydi (likvidatsiya)
        return max(self.directional_change() * LEVERAGE, -100.0)

    def pnl_usd(self):
        return self.stake * (self.pnl_on_stake_pct() / 100.0)

    def round_trip_fee(self):
        # taker, kirish + chiqish
        return self.position_size * TAKER_FEE * 2

    def check_exit(self):
        """'WIN' / 'LOSS' / None - foydaga nisbatan TP/SL."""
        d = self.directional_change()
        if d >= self.tp:
            return "WIN"
        if d <= self.sl:
            return "LOSS"
        return None

    def hold_seconds(self):
        return time.time() - self.entry_time


# ═══════════════════════════════════════════════════════════════════
# CONFLUENCE ENGINE - 4 TA FILTR
# ═══════════════════════════════════════════════════════════════════
def btc_filter(btc_1h, btc_24h, side="LONG"):
    """FILTR 1 - Ota Trend (BTC).
    LONG : BTC 1h > -0.3%  VA  24h > -3%  (kuchli tushishda blok)
    SHORT: BTC 1h < +0.3%  VA  24h < +3%  (kuchli ko'tarilishda blok)
    """
    if btc_1h is None or btc_24h is None:
        return False
    if side == "SHORT":
        return btc_1h < 0.3 and btc_24h < BTC_24H_CEIL
    return btc_1h > -0.3 and btc_24h > BTC_24H_FLOOR


def volume_filter(volume_ratio):
    """FILTR 2 - Volume portlashi: 24h vol / 7d avg >= 1.5.
    Yo'nalishdan qat'i nazar - faollik/qiziqish belgisi."""
    if volume_ratio is None:
        return False
    return volume_ratio >= VOL_RATIO_MIN


def momentum_filter(change_30m, side="LONG"):
    """FILTR 3 - Mahalliy momentum (keng oyna, FOMO guard).
    LONG :  +0.3% .. +8.0%   (yuqoriga harakat)
    SHORT:  -8.0% .. -0.3%   (pastga harakat)
    """
    if change_30m is None:
        return False
    if side == "SHORT":
        return -MOM_MAX <= change_30m <= -MOM_MIN
    return MOM_MIN <= change_30m <= MOM_MAX


def funding_filter(funding, side="LONG"):
    """FILTR 4 - Funding rate (keng chegara).
    LONG : funding < +0.3%
    SHORT: funding > -0.3%
    """
    if funding is None:
        return False
    if side == "SHORT":
        return funding > FUNDING_MIN
    return funding < FUNDING_MAX


# ═══════════════════════════════════════════════════════════════════
# DASHBOARD YORDAMCHILARI
# ═══════════════════════════════════════════════════════════════════
INNER = 58  # quti ichki kengligi (uzun kichik-narxli tokenlar uchun ham)


def _strip_ansi(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\x1b":
            j = s.find("m", i)
            if j == -1:
                break
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def vlen(s):
    return len(_strip_ansi(s))


def row(content=""):
    """Quti qatori: | content<padding> |"""
    pad = INNER - vlen(content)
    if pad < 0:
        # ko'rinadigan uzunlikni kesamiz
        content = _strip_ansi(content)[:INNER]
        pad = INNER - len(content)
    return "| " + content + " " * pad + " |"


def sep():
    return "+" + "-" * (INNER + 2) + "+"


def green(s):
    return f"{Fore.GREEN}{s}{Style.RESET_ALL}" if _COLOR else s


def red(s):
    return f"{Fore.RED}{s}{Style.RESET_ALL}" if _COLOR else s


def yellow(s):
    return f"{Fore.YELLOW}{s}{Style.RESET_ALL}" if _COLOR else s


def cyan(s):
    return f"{Fore.CYAN}{s}{Style.RESET_ALL}" if _COLOR else s


def light(s, ok):
    return green("[YASHIL]") if ok else red("[QIZIL] ")


def fmt_price(p):
    if p is None:
        return "n/a"
    if p == 0:
        return "0"
    ap = abs(p)
    if ap >= 100:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:.4f}"
    if ap >= 0.01:
        return f"{p:.5f}"
    return f"{p:.7f}"


def signed(v, suffix="%", dp=1):
    if v is None:
        return "n/a"
    return f"{v:+.{dp}f}{suffix}"


def money(v, dp=2):
    """+$12.20 / -$11.80 ko'rinishi."""
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.{dp}f}"


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


# ═══════════════════════════════════════════════════════════════════
# SNIPER (asosiy holat + mantiq)
# ═══════════════════════════════════════════════════════════════════
class MemeSniper:
    def __init__(self):
        self.api = RateLimitedAPI()
        self.balance = START_BALANCE
        self.positions = {}          # pair -> Position
        self.trades = []             # yopilgan savdolar (dict)
        self.start_time = time.time()
        self.loop_count = 0

        # confluence cache:  pair -> dict(holatlar)
        self.signals = {}
        self.btc_1h = None
        self.btc_24h = None
        self.btc_price = None

        # sekin o'zgaradigan kesh
        self._daily_vol = {}         # pair -> 7d avg quote volume
        self._24hr = {}              # pair -> {change_pct, quote_volume}
        self._funding = {}           # pair -> funding rate

        # statistika
        self.filter_blocks = defaultdict(int)   # filtr -> bloklash soni
        self.last_stats_at = 0

        self._init_csv()
        logger.info("=== 5X MEME SNIPER START === balance=%.2f paper=%s",
                    self.balance, PAPER_TRADING)

    # ---- CSV ----
    def _init_csv(self):
        # eski sarlavha mos kelmasa (masalan 'side' ustunsiz) - zaxiraga ko'chiramiz
        if os.path.exists(CSV_FILE):
            try:
                with open(CSV_FILE, "r", encoding="utf-8") as f:
                    first = f.readline().strip()
                if first and first != ",".join(CSV_HEADER):
                    bak = CSV_FILE + ".bak"
                    os.replace(CSV_FILE, bak)
                    logger.info("Eski CSV sarlavhasi mos emas -> %s", bak)
            except Exception as e:
                logger.error("CSV migratsiya xato: %s", e)
        if not os.path.exists(CSV_FILE):
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADER)

    def _log_trade_csv(self, pos, result, exit_price, pnl_usd, fee):
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pos.name, pos.side, pos.mode, f"{pos.stake:.2f}", LEVERAGE,
                fmt_price(pos.entry_price), fmt_price(exit_price),
                f"{pos.price_change_pct():.3f}", f"{pos.pnl_on_stake_pct():.3f}",
                f"{pnl_usd:.4f}", f"{fee:.4f}", result,
                f"{self.balance:.2f}", stage_name(self.balance),
                signed(pos.btc_1h), f"{pos.volume_ratio:.2f}"
                if pos.volume_ratio is not None else "n/a",
                signed(pos.funding * 100, "%", 3)
                if pos.funding is not None else "n/a",
            ])

    # ---- equity / ROI ----
    def equity(self):
        eq = self.balance
        for p in self.positions.values():
            eq += p.stake + p.pnl_usd()   # bloklangan jilov + jonli pnl
        return eq

    def roi_pct(self):
        return (self.equity() - START_BALANCE) / START_BALANCE * 100.0

    # ---- jonli narxlar / pozitsiyalarni boshqarish ----
    def tick_prices(self):
        prices = self.api.all_prices()
        if not prices:
            return
        self.btc_price = prices.get(BTC_SYMBOL, self.btc_price)
        for pair, pos in list(self.positions.items()):
            if pair in prices:
                pos.update(prices[pair])
                result = pos.check_exit()
                if result:
                    self.close_position(pair, result)
        # ochiq bo'lmagan tokenlarning jonli narxini signal cache'ga yozamiz
        for pair in TOKENS.values():
            if pair in prices and pair in self.signals:
                self.signals[pair]["price"] = prices[pair]

    def close_position(self, pair, result):
        pos = self.positions.pop(pair)
        pnl = pos.pnl_usd()
        fee = pos.round_trip_fee()
        # isolated: jilov + pnl qaytadi, keyin komissiya yechiladi
        self.balance += pos.stake + pnl
        self.balance -= fee
        self.trades.append({
            "name": pos.name, "side": pos.side, "mode": pos.mode,
            "result": result,
            "price_change_pct": pos.price_change_pct(),
            "directional_change": pos.directional_change(),
            "pnl_usd": pnl, "fee": fee, "hold": pos.hold_seconds(),
            "mode_label": pos.mode_label,
        })
        self._log_trade_csv(pos, result, pos.current_price, pnl, fee)
        logger.info("CLOSE %s %s %s chg=%.2f%% pnl=%.2f fee=%.2f bal=%.2f",
                    pos.side, pos.name, result, pos.price_change_pct(), pnl, fee,
                    self.balance)

        if len(self.trades) % STATS_EVERY_TRADES == 0:
            self.print_stats()

    # ---- confluence skanerlash ----
    def refresh_batch(self):
        """Har loop: arzon batch endpointlar (24hr + funding)."""
        d24 = self.api.all_24hr()
        if d24:
            self._24hr = d24
            if BTC_SYMBOL in d24:
                self.btc_24h = d24[BTC_SYMBOL]["change_pct"]
        fund = self.api.all_funding()
        if fund:
            self._funding = fund

    def refresh_daily_volume(self):
        """Sekin: 7 kunlik o'rtacha volume (har ~30 daqiqa)."""
        for pair in TOKENS.values():
            kl = self.api.klines(pair, "1d", 8)
            avg = avg_quote_volume_7d(kl)
            if avg is not None:
                self._daily_vol[pair] = avg

    def refresh_klines_signals(self):
        """~60s: BTC 1h + har token uchun ATR(14) va 30m momentum."""
        # BTC 1h o'zgarish: 5m * 12 = 60 min
        btc_kl = self.api.klines(BTC_SYMBOL, "5m", 13)
        b1h = pct_change_over(btc_kl, 12) if btc_kl else None
        if b1h is not None:
            self.btc_1h = b1h

        for pair in TOKENS.values():
            name = PAIR_TO_NAME[pair]
            kl = self.api.klines(pair, "5m", 20)
            change_30m = pct_change_over(kl, 6) if kl else None    # 6*5m=30m
            price = self.signals.get(pair, {}).get("price")
            if price is None and kl:
                price = float(kl[-1][4])
            vol = volatility_pct(kl, price) if kl else None

            vol24 = self._24hr.get(pair, {}).get("quote_volume")
            avg7 = self._daily_vol.get(pair)
            ratio = (vol24 / avg7) if (vol24 and avg7) else None
            funding = self._funding.get(pair)

            self.signals[pair] = {
                "name": name,
                "price": price,
                "change_30m": change_30m,
                "volatility": vol,
                "volume_ratio": ratio,
                "funding": funding,
                "is_fomo": (change_30m is not None and abs(change_30m) > FOMO_LEVEL),
            }

    def evaluate_filters(self, sig, side):
        """Berilgan yo'nalish uchun 4 filtr holati (dict of bool)."""
        return {
            "btc": btc_filter(self.btc_1h, self.btc_24h, side),
            "volume": volume_filter(sig.get("volume_ratio")),
            "momentum": momentum_filter(sig.get("change_30m"), side),
            "funding": funding_filter(sig.get("funding"), side),
        }

    def evaluate_token(self, sig):
        """Token uchun eng yaxshi yo'nalishni tanlaydi -> (side, flags, score).
        LONG va SHORT bir vaqtda 4/4 bo'la olmaydi (BTC/momentum teskari)."""
        sides = []
        if ALLOW_LONG:
            sides.append("LONG")
        if ALLOW_SHORT:
            sides.append("SHORT")
        if not sides:
            sides = ["LONG"]
        best = None
        for side in sides:
            flags = self.evaluate_filters(sig, side)
            score = sum(flags.values())
            if best is None or score > best[2]:
                best = (side, flags, score)
        return best

    def try_enter(self):
        """Bo'sh slot bo'lsa, 4 filtr yashil tokenga kirish (LONG yoki SHORT)."""
        if len(self.positions) >= MAX_POSITIONS:
            return
        # eng kuchli (eng ko'p yashil) tokenni birinchi ko'ramiz
        ranked = self.ranked_signals()
        for pair, sig, side, flags, score in ranked:
            if pair in self.positions:
                continue
            # bloklagan filtrlarni statistikaga yozamiz
            for key, ok in flags.items():
                if not ok:
                    self.filter_blocks[key] += 1
            if score >= MIN_FILTERS:
                self.open_position(pair, sig, side)
                if len(self.positions) >= MAX_POSITIONS:
                    break

    def ranked_signals(self):
        """[(pair, sig, side, flags, score)] - yashil filtrlar soni bo'yicha."""
        out = []
        for pair, sig in self.signals.items():
            side, flags, score = self.evaluate_token(sig)
            out.append((pair, sig, side, flags, score))
        # ko'proq yashil -> oldinroq; teng bo'lsa harakat kattaligi yuqori bo'lgani
        out.sort(key=lambda x: (x[4], abs(x[1].get("change_30m") or 0)),
                 reverse=True)
        return out

    def open_position(self, pair, sig, side):
        price = sig.get("price")
        if not price:
            return
        stake = get_stake(self.balance)
        if stake <= 0 or stake > self.balance:
            logger.info("Jilov yetarli emas: stake=%.2f bal=%.2f",
                        stake, self.balance)
            return
        mode_info = select_mode(sig.get("volatility"))
        pos = Position(
            name=sig["name"], pair=pair, entry_price=price, stake=stake,
            mode_info=mode_info, side=side, stage=stage_name(self.balance),
            btc_1h=self.btc_1h, volume_ratio=sig.get("volume_ratio"),
            funding=sig.get("funding"),
        )
        self.balance -= stake          # jilov bloklanadi
        self.positions[pair] = pos
        logger.info("ENTER %s %s @ %s stake=%.2f size=%.2f mode=%s "
                    "vol=%.2f%% btc1h=%s",
                    side, pos.name, fmt_price(price), stake, pos.position_size,
                    mode_info["label"],
                    sig.get("volatility") or 0.0, signed(self.btc_1h))

    # ---- DASHBOARD ----
    def render(self):
        clear_screen()
        lines = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        btc_arrow = "^" if (self.btc_1h or 0) > 0 else "v"
        btc_p = fmt_price(self.btc_price) if self.btc_price else "n/a"

        lines.append(sep())
        title = (f"{cyan('5X MEME SNIPER')} | {now} | "
                 f"BTC ${btc_p} {green(btc_arrow) if btc_arrow=='^' else red(btc_arrow)}")
        lines.append(row(title))
        lines.append(sep())

        # kassa / bosqich
        wins = sum(1 for t in self.trades if t["result"] == "WIN")
        losses = sum(1 for t in self.trades if t["result"] == "LOSS")
        total = len(self.trades)
        wr = (wins / total * 100) if total else 0.0
        roi = self.roi_pct()
        roi_s = green(signed(roi)) if roi >= 0 else red(signed(roi))

        lines.append(row(f"KASSA: ${self.balance:,.2f}   |  "
                         f"BOSQICH: {yellow(stage_name(self.balance))}"))
        next_stake = get_stake(self.balance)
        # joriy rejim = eng yaqin signalning rejimi (default 2% MOMENTUM)
        cur_mode = self._display_mode()
        lines.append(row(f"Jilov: ${next_stake:,.2f} ({stake_label(self.balance)})"
                         f"   Rejim: {cur_mode}"))
        lines.append(row(f"Savdo: {total}  W:{wins} L:{losses}  "
                         f"WR: {wr:.1f}%  ROI: {roi_s}"))
        lines.append(sep())

        # confluence filtrlar
        lines.append(row(cyan("CONFLUENCE FILTRLAR")))
        lines.append(row(f"Bozor: BTC 1h{signed(self.btc_1h)} "
                         f"24h{signed(self.btc_24h)} -> {self._btc_regime()}"))

        nearest = self._nearest_signal()
        if nearest:
            pair, sig, side, flags, score = nearest
            side_tag = green(f"[{side}]") if side == "LONG" else red(f"[{side}]")
            dirword = "long" if side == "LONG" else "short"
            lines.append(row(f"Eng yaqin signal: {yellow(sig['name'])} {side_tag}"))
            lines.append(row(f"  BTC:      {light('', flags['btc'])} "
                             f"({dirword} uchun mos)"))
            ratio = sig.get("volume_ratio")
            lines.append(row(f"  Volume:   {light('', flags['volume'])} "
                             f"{(f'{ratio:.1f}x' if ratio else 'n/a')} "
                             f"(kerak {VOL_RATIO_MIN}x)"))
            ch = sig.get("change_30m")
            mom_extra = " FOMO!" if sig.get("is_fomo") else ""
            need = (f"+{MOM_MIN}..{MOM_MAX}%" if side == "LONG"
                    else f"-{MOM_MIN}..-{MOM_MAX}%")
            lines.append(row(f"  Momentum: {light('', flags['momentum'])} "
                             f"{signed(ch)}{mom_extra} (kerak {need})"))
            fr = sig.get("funding")
            lines.append(row(f"  Funding:  {light('', flags['funding'])} "
                             f"{(signed(fr*100,'%',3) if fr is not None else 'n/a')}"))
            if score >= MIN_FILTERS:
                tail = green(f"{score}/4 yashil, {side} KIRISH!")
            else:
                tail = yellow(f"{score}/4 yashil, kutilmoqda (kerak {MIN_FILTERS})")
            lines.append(row(f"  -> {tail}"))
        else:
            lines.append(row("  ma'lumot kutilmoqda..."))
        lines.append(sep())

        # aktiv pozitsiyalar
        lines.append(row(cyan(f"AKTIV POZITSIYALAR ({len(self.positions)}/{MAX_POSITIONS})")))
        if self.positions:
            for pos in self.positions.values():
                dchg = pos.directional_change()       # foydaga nisbatan
                pnl_pct = pos.pnl_on_stake_pct()
                col = green if dchg >= 0 else red
                scol = green if pos.side == "LONG" else red
                lines.append(row(
                    f"{scol(pos.side.ljust(5))} {pos.name:<5} "
                    f"entry:${fmt_price(pos.entry_price)} "
                    f"now:${fmt_price(pos.current_price)} "
                    f"{col(signed(dchg))} ({col(signed(pnl_pct))})"))
                state = green("foydada") if dchg >= 0 else red("zararda")
                lines.append(row(f"  TP:+{pos.tp:g}% SL:{pos.sl:g}%  "
                                 f"[{state}]  {pos.side} {pos.mode}"))
        else:
            lines.append(row("  ochiq pozitsiya yo'q"))
        lines.append(sep())

        # oxirgi savdolar
        lines.append(row(cyan("OXIRGI SAVDOLAR")))
        if self.trades:
            for t in self.trades[-4:][::-1]:
                col = green if t["result"] == "WIN" else red
                ml = "1% scalp" if t["mode"] == "1%" else "2% momentum"
                scol = green if t.get("side") == "LONG" else red
                side_lbl = scol((t.get("side") or "LONG").ljust(5))
                dchg = t.get("directional_change", t["price_change_pct"])
                lines.append(row(
                    f"{side_lbl} {t['name']:<5} {col(t['result'].ljust(4))} "
                    f"{col(signed(dchg))} ({col(money(t['pnl_usd']))})  {ml}"))
        else:
            lines.append(row("  hali savdo yo'q"))
        lines.append(sep())
        footer = "  PAPER TRADING - REAL PUL YO'Q"
        lines.append(yellow(footer) if _COLOR else footer)

        print("\n".join(lines))
        sys.stdout.flush()

    def _nearest_signal(self):
        ranked = self.ranked_signals()
        for item in ranked:
            if item[0] not in self.positions:   # item[0] = pair
                return item
        return ranked[0] if ranked else None

    def _btc_regime(self):
        """BTC qaysi yo'nalishga ruxsat berishini ko'rsatadi."""
        if btc_filter(self.btc_1h, self.btc_24h, "LONG"):
            return green("LONG bozor")
        if btc_filter(self.btc_1h, self.btc_24h, "SHORT"):
            return red("SHORT bozor")
        return yellow("neytral")

    def _display_mode(self):
        nearest = self._nearest_signal()
        if nearest:
            vol = nearest[1].get("volatility")
            mi = select_mode(vol)
            return mi["label"]
        return select_mode(None)["label"]

    # ---- STATISTIKA ----
    def print_stats(self):
        lines = []
        lines.append("")
        lines.append("=" * 56)
        lines.append("  STATISTIKA")
        lines.append("=" * 56)
        total = len(self.trades)
        if total == 0:
            lines.append("  Hali savdo yo'q.")
            print("\n".join(lines))
            logger.info("STATS: savdo yo'q")
            return

        def wr_of(mode):
            sub = [t for t in self.trades if t["mode"] == mode]
            if not sub:
                return None
            w = sum(1 for t in sub if t["result"] == "WIN")
            return len(sub), w / len(sub) * 100

        for mode, label in (("1%", "1% MICRO-SCALP"), ("2%", "2% MOMENTUM")):
            r = wr_of(mode)
            if r:
                n, wr = r
                lines.append(f"  {label:<16}: {n:>3} savdo, WR {wr:5.1f}%")
            else:
                lines.append(f"  {label:<16}: savdo yo'q")

        # LONG vs SHORT
        for side in ("LONG", "SHORT"):
            sub = [t for t in self.trades if t.get("side") == side]
            if sub:
                w = sum(1 for t in sub if t["result"] == "WIN")
                pnl = sum(t["pnl_usd"] for t in sub)
                lines.append(f"  {side:<16}: {len(sub):>3} savdo, "
                             f"WR {w/len(sub)*100:5.1f}%  ({money(pnl)})")

        avg_hold = sum(t["hold"] for t in self.trades) / total
        lines.append(f"  O'rtacha hold     : {avg_hold:5.1f}s "
                     f"({avg_hold/60:.1f} daqiqa)")

        # eng foydali token
        by_token = defaultdict(float)
        for t in self.trades:
            by_token[t["name"]] += t["pnl_usd"]
        best = max(by_token.items(), key=lambda x: x[1])
        lines.append(f"  Eng foydali token : {best[0]} ({money(best[1])})")

        # eng ko'p bloklagan filtr
        if self.filter_blocks:
            fb = max(self.filter_blocks.items(), key=lambda x: x[1])
            name_map = {"btc": "BTC Trend", "volume": "Volume",
                        "momentum": "Momentum", "funding": "Funding"}
            lines.append(f"  Eng ko'p bloklagan: {name_map.get(fb[0], fb[0])} "
                         f"({fb[1]} marta)")

        # soatlik ROI
        hours = (time.time() - self.start_time) / 3600
        roi = self.roi_pct()
        if hours > 0:
            lines.append(f"  ROI: {signed(roi)}  |  Soatlik ROI: "
                         f"{signed(roi/hours)}/soat  ({hours:.2f}h)")
        lines.append("=" * 56)
        out = "\n".join(lines)
        print(out)
        logger.info("STATS\n%s", out)

    # ---- ASOSIY LOOP ----
    def run(self):
        print(cyan("5X MEME SNIPER") + " ishga tushdi. "
              + yellow("PAPER TRADING - REAL PUL YO'Q"))
        print("Ma'lumotlar yuklanmoqda... (Ctrl+C - to'xtatish va statistika)")
        time.sleep(1)

        # boshlang'ich sekin ma'lumotlar
        self.refresh_batch()
        self.refresh_daily_volume()
        self.refresh_klines_signals()

        try:
            while True:
                self.loop_count += 1

                # 1-2. jonli narxlar + ochiq pozitsiyalarni TP/SL tekshirish
                self.tick_prices()

                # arzon batch (24hr + funding) har loop
                self.refresh_batch()

                # klines (ATR + momentum) ~60s da bir
                if self.loop_count % SCAN_EVERY_LOOPS == 0:
                    self.refresh_klines_signals()
                # 7d volume ~30 daqiqada bir
                if self.loop_count % DAILY_REFRESH_LOOPS == 0:
                    self.refresh_daily_volume()

                # 4-6. confluence -> kirish
                self.try_enter()

                # 7. dashboard
                self.render()

                # 9. kutish
                time.sleep(UPDATE_INTERVAL)

        except KeyboardInterrupt:
            print("\n" + yellow("To'xtatildi. Yakuniy statistika:"))
            self.print_stats()
            logger.info("=== STOP (KeyboardInterrupt) === bal=%.2f trades=%d",
                        self.balance, len(self.trades))


# ═══════════════════════════════════════════════════════════════════
# SELF-TEST (network kerak emas)
# ═══════════════════════════════════════════════════════════════════
def _selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        status = "OK " if cond else "XATO"
        if not cond:
            ok = False
        print(f"  [{status}] {name}")

    # bosqichlar
    check("get_stake DEGEN (200 -> 100)", abs(get_stake(200) - 100) < 1e-9)
    check("get_stake PRO I (2000 -> 150)", get_stake(2000) == 150)
    check("get_stake PRO II (6000 -> 300)", get_stake(6000) == 300)
    check("get_stake PRO III (20000 -> 600)", get_stake(20000) == 600)
    check("stage_name 200 = DEGEN SPRINT", stage_name(200) == "DEGEN SPRINT")
    check("stage_name 12000 = PRO MODE III",
          stage_name(12000) == "PRO MODE III")

    # rejim tanlash
    a = select_mode(0.8)
    b = select_mode(2.3)
    d = select_mode(None)
    check("select_mode flat -> 1% (tp=1, sl=-1.5)",
          a["mode"] == "1%" and a["tp"] == 1.0 and a["sl"] == -1.5)
    check("select_mode faol -> 2% (tp=2, sl=-2)",
          b["mode"] == "2%" and b["tp"] == 2.0 and b["sl"] == -2.0)
    check("select_mode default -> 2%", d["mode"] == "2%")

    # filtrlar - LONG (yumshatilgan chegaralar)
    check("btc_filter LONG green (1h flat)", btc_filter(0.0, 2.1) is True)
    check("btc_filter LONG green (1h -0.2)", btc_filter(-0.2, 2.1) is True)
    check("btc_filter LONG red (1h<-0.3)", btc_filter(-0.5, 2.1) is False)
    check("btc_filter LONG red (24h<=-3)", btc_filter(0.8, -3.5) is False)
    check("volume_filter 1.0x green", volume_filter(1.0) is True)
    check("volume_filter 0.5x red", volume_filter(0.5) is False)
    check("momentum LONG 2.0 green", momentum_filter(2.0) is True)
    check("momentum LONG 0.5 green (yumshat)", momentum_filter(0.5) is True)
    check("momentum LONG 0.2 red (past)", momentum_filter(0.2) is False)
    check("momentum LONG 9 red (FOMO)", momentum_filter(9.0) is False)
    check("funding LONG 0.0002 green", funding_filter(0.0002) is True)
    check("funding LONG 0.004 red", funding_filter(0.004) is False)

    # filtrlar - SHORT (teskari)
    check("btc_filter SHORT green (1h flat)",
          btc_filter(0.0, -0.3, "SHORT") is True)
    check("btc_filter SHORT red (1h>0.3)",
          btc_filter(0.5, -0.3, "SHORT") is False)
    check("btc_filter SHORT red (24h>=3)",
          btc_filter(-0.5, 3.5, "SHORT") is False)
    check("momentum SHORT -2.0 green", momentum_filter(-2.0, "SHORT") is True)
    check("momentum SHORT -0.5 green (yumshat)",
          momentum_filter(-0.5, "SHORT") is True)
    check("momentum SHORT +2.0 red", momentum_filter(2.0, "SHORT") is False)
    check("momentum SHORT -0.2 red (past)",
          momentum_filter(-0.2, "SHORT") is False)
    check("momentum SHORT -9 red (panika)",
          momentum_filter(-9.0, "SHORT") is False)
    check("funding SHORT -0.0002 green",
          funding_filter(-0.0002, "SHORT") is True)
    check("funding SHORT -0.004 red",
          funding_filter(-0.004, "SHORT") is False)

    # indikatorlar (sun'iy klines: [t,o,h,l,c,v,ct,qv,...])
    kl = []
    base = 100.0
    for i in range(20):
        o = base + i
        h = o + 1.0
        l = o - 1.0
        c = o + 0.5
        kl.append([i, o, h, l, c, 1000, i, 50000])
    atr = atr_from_klines(kl, 14)
    check("atr_from_klines musbat", atr is not None and atr > 0)
    ch30 = pct_change_over(kl, 6)
    check("pct_change_over hisob", ch30 is not None and ch30 > 0)
    vol = volatility_pct(kl, float(kl[-1][4]))
    check("volatility_pct hisob", vol is not None and vol > 0)
    avg7 = avg_quote_volume_7d(kl)
    check("avg_quote_volume_7d = 50000", avg7 is not None and abs(avg7 - 50000) < 1e-6)
    check("atr None (kam ma'lumot)", atr_from_klines(kl[:5], 14) is None)

    # pozitsiya matematikasi (5x)
    mi = select_mode(2.3)  # 2% rejim
    pos = Position("WIF", "WIFUSDT", 1.0, 100.0, mi, "DEGEN SPRINT",
                   0.8, 1.8, 0.0002)
    check("position_size = stake*5", pos.position_size == 500.0)
    pos.update(1.02)  # +2% narx
    check("price_change_pct = +2%", abs(pos.price_change_pct() - 2.0) < 1e-9)
    check("pnl_on_stake = +10%", abs(pos.pnl_on_stake_pct() - 10.0) < 1e-9)
    check("pnl_usd = +10", abs(pos.pnl_usd() - 10.0) < 1e-9)
    check("exit = WIN @ +2%", pos.check_exit() == "WIN")
    pos.update(0.98)  # -2%
    check("exit = LOSS @ -2%", pos.check_exit() == "LOSS")
    check("round_trip_fee = 500*0.0004*2 = 0.4",
          abs(pos.round_trip_fee() - 0.4) < 1e-9)
    # likvidatsiya kapi (isolated)
    pos.update(0.70)  # -30% narx -> -150% stake -> kap -100%
    check("isolated kap -100% (likvidatsiya)",
          abs(pos.pnl_on_stake_pct() + 100.0) < 1e-9)

    # SHORT pozitsiya matematikasi (narx tushsa foyda)
    ps = Position("WIF", "WIFUSDT", 1.0, 100.0, mi, "DEGEN SPRINT",
                  -0.8, 1.8, -0.0002, side="SHORT")
    ps.update(0.98)  # narx -2% -> SHORT uchun +2% foyda
    check("SHORT directional +2% @ narx -2%",
          abs(ps.directional_change() - 2.0) < 1e-9)
    check("SHORT pnl_on_stake = +10%",
          abs(ps.pnl_on_stake_pct() - 10.0) < 1e-9)
    check("SHORT pnl_usd = +10", abs(ps.pnl_usd() - 10.0) < 1e-9)
    check("SHORT exit WIN @ narx -2%", ps.check_exit() == "WIN")
    ps.update(1.02)  # narx +2% -> SHORT uchun -2% zarar
    check("SHORT exit LOSS @ narx +2%", ps.check_exit() == "LOSS")

    # yo'nalish tanlash (evaluate_token)
    ev = MemeSniper.__new__(MemeSniper)
    ev.btc_1h, ev.btc_24h = -0.5, -0.3                  # SHORT bozor
    sig_s = {"name": "WIF", "price": 1.0, "change_30m": -2.0,
             "volatility": 2.3, "volume_ratio": 1.0, "funding": -0.0002}
    side_s, flags_s, score_s = ev.evaluate_token(sig_s)
    check("evaluate_token -> SHORT 4/4", side_s == "SHORT" and score_s == 4)
    ev.btc_1h, ev.btc_24h = 0.8, 2.1                    # LONG bozor
    sig_l = {"name": "WIF", "price": 1.0, "change_30m": 2.0,
             "volatility": 2.3, "volume_ratio": 1.0, "funding": 0.0002}
    side_l, flags_l, score_l = ev.evaluate_token(sig_l)
    check("evaluate_token -> LONG 4/4", side_l == "LONG" and score_l == 4)

    # close_position balans matematikasi
    sn = MemeSniper.__new__(MemeSniper)   # __init__ siz (CSV/log yo'q)
    sn.balance = 100.0
    sn.positions = {}
    sn.trades = []
    sn.start_time = time.time()
    sn.filter_blocks = defaultdict(int)
    mi2 = select_mode(2.3)
    p2 = Position("DOGE", "DOGEUSDT", 1.0, 100.0, mi2, "DEGEN SPRINT",
                  0.5, 1.7, 0.0001)
    sn.positions["DOGEUSDT"] = p2
    p2.update(1.02)  # +2% -> +10 usd, fee 0.4
    # close uchun CSV/logni vaqtincha o'chiramiz
    sn._log_trade_csv = lambda *a, **k: None
    sn.close_position("DOGEUSDT", "WIN")
    # 100 (bo'sh) + stake 100 + pnl 10 - fee 0.4 = 209.6
    check("close WIN balans = 209.6", abs(sn.balance - 209.6) < 1e-6)
    check("trade qayd etildi", len(sn.trades) == 1)

    print("\n" + ("HAMMASI OK" if ok else "BA'ZI TESTLAR XATO"))
    return 0 if ok else 1


# ═══════════════════════════════════════════════════════════════════
def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if not PAPER_TRADING:
        print("XAVF: PAPER_TRADING=False. Bu bot faqat simulyatsiya uchun.")
        sys.exit(1)
    MemeSniper().run()


if __name__ == "__main__":
    main()
