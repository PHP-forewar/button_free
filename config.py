import os
from typing import Dict, List, Tuple

# ═══ TOKEN ADDRESSES ═══
TOKENS: Dict[str, str] = {
    "JUP":    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY":    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "PYTH":   "HZ1JovNiVvGrk6PJTS6xfDFkPHoiSJhGiYJMRNaHGNE6",
    "ORCA":   "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
    "SOL":    "So11111111111111111111111111111111111111112",
    "JTO":    "jtojtomepa8b6nS7nLVMwX8VKkYdJjdthqxPzZrMBg",
    "HNT":    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
}

# ═══ API ENDPOINTS ═══
JUPITER_PRICE_URL       = "https://price.jup.ag/v6/price"
BINANCE_TICKER_URL      = "https://api.binance.com/api/v3/ticker/price"
BINANCE_24H_URL         = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES_URL      = "https://api.binance.com/api/v3/klines"
BINANCE_FUNDING_URL     = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OI_URL          = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_OI_HIST_URL     = "https://fapi.binance.com/futures/data/openInterestHist"
COINGLASS_LIQ_URL       = "https://open-api.coinglass.com/public/v2/liquidation_ex_chart"
COINGECKO_BASE_URL      = "https://api.coingecko.com/api/v3"
HELIUS_RPC_URL          = "https://mainnet.helius-rpc.com/"
HELIUS_API_URL          = "https://api.helius.xyz/v0"

# ═══ API KEYS ═══
HELIUS_API_KEY  = os.environ.get("HELIUS_API_KEY", "demo")
COINGLASS_KEY   = os.environ.get("COINGLASS_KEY", "demo")

# ═══ CACHE DURATIONS (seconds) ═══
CACHE_PRICE_TTL       = 10
CACHE_OI_TTL          = 30
CACHE_WHALE_TTL       = 60
CACHE_LIQUIDATION_TTL = 300
CACHE_COINGECKO_TTL   = 60
CACHE_FUNDING_TTL     = 30

# ═══ SIGNAL WEIGHTS ═══
SIGNAL_WEIGHTS = {
    "momentum": 0.20,
    "cvd":      0.20,
    "oi":       0.20,
    "funding":  0.15,
    "whale":    0.25,
}

# ═══ ENTRY THRESHOLDS ═══
LONG_THRESHOLD  = 65
SHORT_THRESHOLD = -65

# ═══ EXIT PARAMETERS ═══
MOONBAG_TRIGGER_PCT   = 20.0
MOONBAG_SELL_FRACTION = 0.50
TRAILING_DRAWDOWN_PCT = 7.0
STOP_LOSS_PCT         = 10.0
TIME_KILL_HOURS       = 4

# ═══ FUNDING RATE THRESHOLDS (%) ═══
FUNDING_HIGH =  0.05   # longs paying → short bias
FUNDING_LOW  = -0.05   # shorts paying → long bias

# ═══ WHALE THRESHOLDS ═══
WHALE_USD = 50_000

# ═══ PAPER TRADING ═══
INITIAL_BALANCE = 10_000.0
MAX_POSITIONS   = 3

# Tiered bet sizing: (min_balance, max_balance, bet_usd)
BET_TIERS: List[Tuple[float, float, float]] = [
    (100,    500,             15.0),
    (500,    2_000,           80.0),
    (2_000,  10_000,         200.0),
    (10_000, 25_000,         500.0),
    (25_000, float("inf"),   800.0),
]

# ═══ INDICATOR SETTINGS ═══
RSI_PERIOD              = 14
MACD_FAST               = 12
MACD_SLOW               = 26
MACD_SIGNAL             = 9
PRICE_HISTORY_MAXLEN    = 60
MIN_HISTORY_FOR_SIGNALS = 30

# ═══ TOKEN MAPPINGS ═══
COINGECKO_IDS: Dict[str, str] = {
    "JUP":    "jupiter-exchange-solana",
    "RAY":    "raydium",
    "PYTH":   "pyth-network",
    "ORCA":   "orca",
    "RENDER": "render-token",
    "SOL":    "solana",
    "JTO":    "jito-governance-token",
    "HNT":    "helium",
}

BINANCE_FUTURES_SYMBOLS: Dict[str, str] = {
    "JUP":    "JUPUSDT",
    "RAY":    "RAYUSDT",
    "PYTH":   "PYTHUSDT",
    "ORCA":   "ORCAUSDT",
    "RENDER": "RENDERUSDT",
    "SOL":    "SOLUSDT",
    "JTO":    "JTOUSDT",
    "HNT":    "HNTUSDT",
}

BINANCE_SPOT_SYMBOLS: Dict[str, str] = {
    "JUP":    "JUPUSDT",
    "RAY":    "RAYUSDT",
    "PYTH":   "PYTHUSDT",
    "ORCA":   "ORCAUSDT",
    "RENDER": "RENDERUSDT",
    "SOL":    "SOLUSDT",
    "JTO":    "JTOUSDT",
    "HNT":    "HNTUSDT",
}

# ═══ DISPLAY ═══
REFRESH_INTERVAL = 10   # seconds between screen updates

# ═══ FILES ═══
TRADES_CSV = "trades.csv"
LOG_FILE   = "bot.log"
