#!/usr/bin/env python3
"""
Solana Paper Trading Bot
========================
Paper-only. No wallets. No real funds.

Usage:
    python main.py

Environment variables (optional):
    HELIUS_API_KEY  – Helius RPC key (default: demo)
    COINGLASS_KEY   – Coinglass API key (default: demo)
"""

import sys
import time
import logging
import traceback
from typing import Dict, Optional

import config
from data.price        import get_jupiter_prices, get_btc_price, price_history
from strategy.signals  import signal_engine
from strategy.engine   import TradingEngine
from strategy.position import Portfolio
from display.terminal  import TerminalDisplay


# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# suppress noisy libraries
for lib in ("urllib3", "requests"):
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger("main")


# ─── Warm-up ─────────────────────────────────────────────────────────────────

def _warm_up(n: int = config.MIN_HISTORY_FOR_SIGNALS):
    """Collect price history before enabling signal engine."""
    print(f"\n  Warming up price history ({n} samples @ "
          f"{config.REFRESH_INTERVAL}s intervals)…\n")
    for i in range(n):
        prices = get_jupiter_prices()
        for token, price in prices.items():
            price_history.push(token, price)
        pct = int((i + 1) / n * 30)
        bar = "█" * pct + "░" * (30 - pct)
        print(f"\r  [{bar}] {i+1}/{n}", end="", flush=True)
        if i < n - 1:
            time.sleep(config.REFRESH_INTERVAL)
    print("\n\n  Warm-up complete – starting live trading loop.\n")
    time.sleep(1)


# ─── Main loop ────────────────────────────────────────────────────────────────

def run():
    portfolio = Portfolio(config.INITIAL_BALANCE)
    engine    = TradingEngine(portfolio)
    display   = TerminalDisplay(portfolio)

    logger.info("Bot started. Initial balance: $%.2f", config.INITIAL_BALANCE)

    _warm_up()

    consecutive_errors = 0

    while True:
        loop_start = time.time()
        try:
            # ── Fetch data ────────────────────────────────────────────────────
            prices: Dict[str, float] = get_jupiter_prices()
            if not prices:
                logger.warning("Jupiter returned empty prices – skipping tick")
                time.sleep(config.REFRESH_INTERVAL)
                continue

            btc_price: Optional[float] = get_btc_price()

            # Push latest prices into history for indicators
            for token, price in prices.items():
                price_history.push(token, price)

            # ── Trade decisions ───────────────────────────────────────────────
            engine.tick(prices)

            # ── Build signal snapshot for display ─────────────────────────────
            signals = engine.get_signal_summary()

            # ── Render terminal ───────────────────────────────────────────────
            display.render(prices, signals, btc_price)

            consecutive_errors = 0

        except KeyboardInterrupt:
            _shutdown(portfolio, prices if "prices" in dir() else {})
            return

        except Exception as exc:
            consecutive_errors += 1
            logger.error("Loop error #%d: %s\n%s",
                         consecutive_errors, exc, traceback.format_exc())

            # back off if repeated failures
            if consecutive_errors >= 5:
                logger.critical("5 consecutive errors – sleeping 60s")
                time.sleep(60)
                consecutive_errors = 0

        # ── Maintain refresh interval ─────────────────────────────────────────
        elapsed = time.time() - loop_start
        sleep_for = max(0.0, config.REFRESH_INTERVAL - elapsed)
        time.sleep(sleep_for)


def _shutdown(portfolio: Portfolio, prices: Dict[str, float]):
    print("\n\n  Shutting down…")
    roi = portfolio.roi_pct(prices)
    bal = portfolio.total_value(prices)
    n   = len(portfolio.trade_log)
    wins = sum(1 for t in portfolio.trade_log if float(t["pnl_usd"]) > 0)
    wr  = f"{wins/n*100:.1f}%" if n else "--"

    print(f"\n  ─── SESSION SUMMARY ───────────────────────────")
    print(f"  Final balance : ${bal:,.2f}")
    print(f"  ROI           : {'+' if roi>=0 else ''}{roi:.2f}%")
    print(f"  Total trades  : {n}")
    print(f"  Win rate      : {wr}")
    print(f"  Log file      : {config.LOG_FILE}")
    print(f"  Trades CSV    : {config.TRADES_CSV}")
    print(f"  ───────────────────────────────────────────────\n")
    logger.info("Bot stopped. Final balance: $%.2f  ROI: %.2f%%  Trades: %d",
                bal, roi, n)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
