"""
Colorful terminal dashboard – refreshes every 10 seconds.
Uses ANSI escape codes (colorama) for full color support.
"""

import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from colorama import Fore, Back, Style, init as colorama_init

from strategy.position import Portfolio, Position
import config

colorama_init(autoreset=True)

# ─── Color shortcuts ──────────────────────────────────────────────────────────
G  = Fore.GREEN
R  = Fore.RED
Y  = Fore.YELLOW
C  = Fore.CYAN
M  = Fore.MAGENTA
W  = Fore.WHITE
B  = Fore.BLUE
DG = Fore.LIGHTBLACK_EX   # dark grey
BLD= Style.BRIGHT
RST= Style.RESET_ALL

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _clr(value: float, pos_clr=G, neg_clr=R, zero_clr=W) -> str:
    if value > 0:  return pos_clr
    if value < 0:  return neg_clr
    return zero_clr

def _bar(score: float, width: int = 10) -> str:
    """ASCII progress bar representing abs(score) / 100."""
    filled = int(abs(score) / 100 * width)
    filled = min(filled, width)
    clr = G if score >= 0 else R
    bar = clr + "█" * filled + DG + "░" * (width - filled) + RST
    return f"[{bar}]"

def _fmt_price(p: float) -> str:
    if p >= 1000:   return f"${p:,.2f}"
    if p >= 1:      return f"${p:.4f}"
    if p >= 0.001:  return f"${p:.6f}"
    return f"${p:.8f}"

def _fmt_pnl(pnl_pct: float) -> str:
    sign = "+" if pnl_pct >= 0 else ""
    clr  = _clr(pnl_pct)
    return f"{clr}{sign}{pnl_pct:.2f}%{RST}"

def _whale_icon(w: str) -> str:
    if w == "BULLISH_WHALE":  return G + "🐋BULL" + RST
    if w == "BEARISH_WHALE":  return R + "🐋BEAR" + RST
    return DG + "QUIET" + RST

def _funding_str(token: str) -> str:
    from data.sentiment import get_funding_rate
    rate = get_funding_rate(token)
    if rate is None:
        return DG + " N/A  " + RST
    clr = _clr(rate, R, G)   # high funding (longs paying) = red = danger for longs
    sign = "+" if rate >= 0 else ""
    return f"{clr}{sign}{rate:.3f}%{RST}"

def _signal_dir(direction: str) -> str:
    if direction == "LONG":    return G + BLD + "LONG " + RST
    if direction == "SHORT":   return R + BLD + "SHORT" + RST
    return DG + "  -  " + RST

def _clear():
    os.system("cls" if os.name == "nt" else "clear")

def _line(ch="─", width=86) -> str:
    return DG + ch * width + RST

def _header(text: str, width: int = 86) -> str:
    pad = width - len(text) - 4
    return DG + "╠══ " + RST + BLD + text + RST + DG + " " + "═" * pad + "╣" + RST


# ─── Main display class ───────────────────────────────────────────────────────

class TerminalDisplay:
    WIDTH = 86

    def __init__(self, portfolio: Portfolio):
        self.portfolio  = portfolio
        self._start_time = time.time()

    # ── Top banner ────────────────────────────────────────────────────────────

    def _render_banner(self, btc_price: Optional[float]) -> List[str]:
        now     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        btc_str = f"BTC ${btc_price:,.0f}" if btc_price else "BTC ---"
        title   = f" SOLANA PAPER TRADING BOT  │  {now}  │  {btc_str} "

        W2 = self.WIDTH
        lines = [
            DG + "╔" + "═" * W2 + "╗" + RST,
            DG + "║" + RST + C + BLD + title.center(W2) + RST + DG + "║" + RST,
            DG + "╠" + "═" * W2 + "╣" + RST,
        ]
        return lines

    # ── Portfolio summary ─────────────────────────────────────────────────────

    def _render_portfolio(self, prices: Dict[str, float]) -> List[str]:
        p    = self.portfolio
        bal  = p.cash_balance
        opn  = p.open_value(prices)
        tot  = bal + opn
        roi  = p.roi_pct(prices)
        n_tr = len(p.trade_log)
        wins = sum(1 for t in p.trade_log if t["pnl_usd"] > 0)
        wr   = f"{wins/n_tr*100:.0f}%" if n_tr > 0 else "--"

        roi_clr = _clr(roi)
        line = (
            f" Cash {W}{BLD}${bal:,.2f}{RST}"
            f"  Open {C}${opn:,.2f}{RST}"
            f"  Total {BLD}${tot:,.2f}{RST}"
            f"  ROI {roi_clr}{BLD}{'+' if roi>=0 else ''}{roi:.2f}%{RST}"
            f"  Trades {W}{n_tr}{RST}"
            f"  WR {G}{wr}{RST}"
        )
        W2 = self.WIDTH
        return [
            _header("PORTFOLIO"),
            DG + "║" + RST + line.ljust(W2 + 30) + DG + "║" + RST,
        ]

    # ── Token table ───────────────────────────────────────────────────────────

    def _render_token_table(self, prices: Dict[str, float],
                            signals: Dict[str, dict]) -> List[str]:
        W2  = self.WIDTH
        hdr = (f" {'TOKEN':<7}{'PRICE':>10}  "
               f"{'SCORE':>6}  {'SIGNAL STRENGTH':^15}  "
               f"{'WHALE':<10}  {'FUNDING':>8}  {'STATUS':<18}")

        lines = [
            _header("MARKET SIGNALS"),
            DG + "║" + RST + DG + hdr + RST + DG + "║" + RST,
            DG + "║" + DG + "─" * W2 + "║" + RST,
        ]

        for token in config.TOKENS:
            price = prices.get(token)
            sig   = signals.get(token, {})
            if price is None:
                row = f" {token:<7}" + DG + "  (no data)" + RST
                lines.append(DG + "║" + RST + row.ljust(W2 + 20) + DG + "║" + RST)
                continue

            score    = sig.get("score", 0.0)
            direction = sig.get("direction", "NEUTRAL")
            whale    = sig.get("whale", "QUIET")
            block    = sig.get("block_reason")
            conf     = sig.get("confidence", 0.0)

            price_str   = f"{_fmt_price(price):>10}"
            score_clr   = _clr(score)
            score_str   = f"{score_clr}{score:>+6.1f}{RST}"
            bar_str     = _bar(score, 10)
            conf_str    = f"{abs(conf):>3.0f}%"
            whale_str   = _whale_icon(whale)
            fund_str    = _funding_str(token)
            dir_str     = _signal_dir(direction)
            status_str  = (DG + f"{block[:18]}" + RST) if block else (dir_str)

            row = (f" {W}{token:<7}{RST}"
                   f"{price_str}  "
                   f"{score_str}  "
                   f"{bar_str} {conf_str}  "
                   f"{whale_str:<10}  "
                   f"{fund_str:>8}  "
                   f"{status_str:<18}")

            lines.append(DG + "║" + RST + row + DG + "║" + RST)

        return lines

    # ── Active positions ──────────────────────────────────────────────────────

    def _render_positions(self, prices: Dict[str, float]) -> List[str]:
        W2 = self.WIDTH
        lines = [_header("ACTIVE POSITIONS")]

        if not self.portfolio.positions:
            lines.append(DG + "║" + RST +
                         DG + "  No open positions".ljust(W2) + RST +
                         DG + "║" + RST)
            return lines

        hdr = (f" {'TOKEN':<7}{'DIR':<6}{'ENTRY':>10}  "
               f"{'CURRENT':>10}  {'PnL':>8}  "
               f"{'TRAIL STOP':>12}  {'TIME LEFT':>10}  {'STATUS'}")
        lines.append(DG + "║" + RST + DG + hdr + RST + DG + "║" + RST)
        lines.append(DG + "║" + DG + "─" * W2 + "║" + RST)

        for pos in self.portfolio.positions:
            price   = prices.get(pos.token, pos.entry_price)
            pnl_pct = pos.pnl_pct(price)
            pnl_usd = pos.pnl_usd(price)
            hrs     = pos.hours_remaining()
            trail   = f"${pos.trailing_stop:.4f}" if pos.trailing_stop else "  --    "

            pnl_clr  = _clr(pnl_pct)
            dir_str  = G + "LONG " + RST if pos.direction == "LONG" else R + "SHORT" + RST
            moon_tag = M + " 🌙" + RST if pos.moonbag_done else "   "

            row = (f" {W}{pos.token:<7}{RST}"
                   f"{dir_str} "
                   f"{_fmt_price(pos.entry_price):>10}  "
                   f"{_fmt_price(price):>10}  "
                   f"{pnl_clr}{'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%{RST} "
                   f"{pnl_clr}(${pnl_usd:+.2f}){RST}  "
                   f"{C}{trail:>12}{RST}  "
                   f"{Y}{hrs:.1f}h left{RST}"
                   f"{moon_tag}")
            lines.append(DG + "║" + RST + row + DG + "║" + RST)

        return lines

    # ── Last 10 trades ────────────────────────────────────────────────────────

    def _render_trades(self) -> List[str]:
        W2    = self.WIDTH
        trades = self.portfolio.trade_log[-10:]
        lines = [_header("LAST 10 TRADES")]

        if not trades:
            lines.append(DG + "║" + RST +
                         DG + "  No trades yet".ljust(W2) + RST +
                         DG + "║" + RST)
            return lines

        hdr = f" {'TIME':<20} {'TOKEN':<7}{'DIR':<6} {'ENTRY':>10}  {'EXIT':>10}  {'PnL%':>8}  {'PnL$':>9}  {'REASON'}"
        lines.append(DG + "║" + RST + DG + hdr + RST + DG + "║" + RST)

        for t in reversed(trades):
            pnl  = float(t["pnl_pct"])
            clr  = _clr(pnl)
            ts   = t["timestamp"][:19].replace("T", " ")
            row  = (f" {DG}{ts:<20}{RST}"
                    f" {W}{t['token']:<7}{RST}"
                    f"{G+'LONG ' if t['direction']=='LONG' else R+'SHORT'}{RST} "
                    f"{_fmt_price(float(t['entry_price'])):>10}  "
                    f"{_fmt_price(float(t['exit_price'])):>10}  "
                    f"{clr}{'+' if pnl>=0 else ''}{pnl:.2f}%{RST}  "
                    f"{clr}${float(t['pnl_usd']):>+8.2f}{RST}  "
                    f"{DG}{t['exit_reason']}{RST}")
            lines.append(DG + "║" + RST + row + DG + "║" + RST)

        return lines

    # ── Footer ────────────────────────────────────────────────────────────────

    def _render_footer(self) -> List[str]:
        W2      = self.WIDTH
        uptime  = int(time.time() - self._start_time)
        h, rem  = divmod(uptime, 3600)
        m, s    = divmod(rem, 60)
        bet     = self.portfolio.bet_size()
        footer  = (f" Uptime {C}{h:02d}:{m:02d}:{s:02d}{RST}"
                   f"  │  Next bet {Y}${bet:.0f}{RST}"
                   f"  │  Max positions {W}{config.MAX_POSITIONS}{RST}"
                   f"  │  {DG}PAPER TRADING – NO REAL FUNDS{RST}")
        return [
            DG + "╠" + "═" * W2 + "╣" + RST,
            DG + "║" + RST + footer.ljust(W2 + 30) + DG + "║" + RST,
            DG + "╚" + "═" * W2 + "╝" + RST,
        ]

    # ── Full render ───────────────────────────────────────────────────────────

    def render(self, prices: Dict[str, float],
               signals: Dict[str, dict],
               btc_price: Optional[float] = None):
        _clear()

        sections = (
            self._render_banner(btc_price)
            + self._render_portfolio(prices)
            + self._render_token_table(prices, signals)
            + self._render_positions(prices)
            + self._render_trades()
            + self._render_footer()
        )

        sys.stdout.write("\n".join(sections) + "\n")
        sys.stdout.flush()
