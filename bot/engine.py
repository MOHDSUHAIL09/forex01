"""
Martingale Forex Trading Bot — Core Engine
Strategy:
 - Start 0.01 lot
 - Level 1 TP  : +3 pts from Level-1 entry  → close Level-1 trade, reset grid
 - Level 2+ TP : +8 pts from LAST entry      → close ALL trades, reset grid
 - Grid Step   : open next level after 6 pts adverse move from last entry
 - Lot doubling: 0.01 → 0.02 → 0.04 → 0.08 → 0.16 → 0.32 → 0.64 → 1.28 → 2.56
 - Max Levels  : 9
 - Safety stop : $500 max drawdown
"""

import threading
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Callable


@dataclass
class Trade:
    trade_id:    int
    level:       int
    entry_price: float
    lot_size:    float
    direction:   str
    take_profit: float
    status:      str           = "OPEN"
    close_price: Optional[float] = None
    pnl_points:  float         = 0.0
    pnl_usd:     float         = 0.0
    opened_at:   str           = ""
    closed_at:   str           = ""
    mt5_ticket:  Optional[int] = None

    def to_dict(self):
        return asdict(self)


class MartingaleEngine:
    """
    Dual-mode Martingale engine — simulator or live MT5.

    TP Logic:
      • If only Level 1 is open and price moves +3 pts → TP → reset grid
      • If Level 2+ exists and price reaches +8 pts from LAST entry → TP → close all → reset
    """

    def __init__(self, config: dict,
                 on_event: Optional[Callable] = None,
                 connector=None):
        self.cfg        = config
        self.on_event   = on_event
        self.connector  = connector   # MT5Connector or None

        # Strategy params
        self.symbol            = config["symbol"]
        self.starting_lot      = float(config["starting_lot"])
        self.tp_points         = float(config["take_profit_points"])        # multi-level TP (+8)
        self.first_level_tp    = float(config.get("first_level_tp_points",  # level-1 TP (+3)
                                                   config["take_profit_points"]))
        self.grid_step         = float(config["grid_step_points"])          # 6 pts
        self.max_levels        = int(config["max_grid_levels"])             # 9
        self.max_drawdown      = float(config["max_drawdown_usd"])          # $500
        self.direction         = config["trade_direction"]                  # BUY or SELL

        # State
        self.grid:         List[Trade] = []
        self.history:      List[Trade] = []
        self.trade_counter = 1
        self.is_running    = False
        self.current_price = 0.0
        self.balance       = float(config["simulator"]["initial_balance"])
        self.equity        = self.balance
        self.total_profit  = 0.0
        self.total_loss    = 0.0
        self.win_count     = 0
        self.loss_count    = 0

        # Live mode: sync balance from MT5
        self._live_mode = connector is not None
        if self._live_mode:
            info = connector.get_account_info()
            if info:
                self.balance = info["balance"]
                self.equity  = info["equity"]

        # XAUUSD: $10 per point per lot
        self.point_value_per_lot = 10.0
        self._lock = threading.Lock()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def start(self, initial_price: float):
        with self._lock:
            if self.is_running:
                return
            self.is_running    = True
            self.current_price = initial_price
            self._open_first_trade(initial_price)
            self._emit("bot_started", {
                "price": initial_price,
                "mode": "live" if self._live_mode else "simulator",
            })

    def stop(self):
        with self._lock:
            if not self.is_running:
                return
            self.is_running = False
            if self._live_mode and self.connector:
                # Positions are closed by server.py via close_all_with_pnl()
                pass
            else:
                for t in self.grid:
                    if t.status == "OPEN":
                        self._close_trade(t, self.current_price, "STOPPED")
            self._emit("bot_stopped", self._get_state())

    def reset(self):
        with self._lock:
            self.grid.clear()
            self.history.clear()
            self.trade_counter = 1
            self.is_running    = False
            self._emit("bot_reset", {})

    def tick(self, price: float):
        """Called on every price tick (simulator or MT5 feed)."""
        with self._lock:
            if not self.is_running:
                return

            self.current_price = price
            open_trades = [t for t in self.grid if t.status == "OPEN"]

            # If no open trade in grid, open fresh Level-1 trade
            if not open_trades:
                self._open_first_trade(price)
                open_trades = [t for t in self.grid if t.status == "OPEN"]
                if not open_trades:
                    # Order might be in flight or broker error; still emit tick so chart & header price stay live
                    self.equity = self.balance + self._calc_unrealized(price)
                    self._emit("tick", self._get_state())
                    return

            # ── Sync equity from MT5 ──
            if self._live_mode and self.connector:
                info = self.connector.get_account_info()
                if info:
                    self.equity = info["equity"]
                    self.balance = info["balance"]

            # ── Take Profit Logic ──────────────────────────────────────────
            # Rule A — Level 1 only: TP if price moves +3 pts from L1 entry
            if len(open_trades) == 1:
                l1 = open_trades[0]
                pts_from_l1 = (price - l1.entry_price if self.direction == "BUY"
                               else l1.entry_price - price)
                if pts_from_l1 >= self.first_level_tp:
                    self._emit("log", {
                        "msg": f"Level-1 TP hit at +{pts_from_l1:.1f} pts (target +{self.first_level_tp})",
                        "type": "win"
                    })
                    self._close_grid_win(price, tp_rule="L1")
                    return

            # Rule B — Level 2+: TP if price moves +8 pts from LAST entry
            else:
                last = open_trades[-1]
                pts_from_last = (price - last.entry_price if self.direction == "BUY"
                                 else last.entry_price - price)
                if pts_from_last >= self.tp_points:
                    self._emit("log", {
                        "msg": (f"Grid TP hit: +{pts_from_last:.1f} pts from last entry "
                                f"(target +{self.tp_points})"),
                        "type": "win"
                    })
                    self._close_grid_win(price, tp_rule="GRID")
                    return

            # ── Max Drawdown check ─────────────────────────────────────────
            unrealized = self._calc_unrealized(price)
            if unrealized <= -self.max_drawdown:
                self._emit("max_drawdown_hit", {"drawdown": round(unrealized, 2)})
                self.is_running = False
                if self._live_mode and self.connector:
                    self.connector.close_all_by_magic()
                return

            # ── Grid Step check ────────────────────────────────────────────
            last = open_trades[-1]
            dist = (last.entry_price - price if self.direction == "BUY"
                    else price - last.entry_price)
            if dist >= self.grid_step:
                if len(open_trades) < self.max_levels:
                    next_lot = round(last.lot_size * 2, 2)
                    self._open_level_trade(price, len(self.grid) + 1, next_lot)
                else:
                    self._emit("max_levels_reached", {"levels": self.max_levels})

            # ── Emit tick ─────────────────────────────────────────────────
            self.equity = self.balance + self._calc_unrealized(price)
            self._emit("tick", self._get_state())

    # ─────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────

    def _open_first_trade(self, price: float):
        self._open_level_trade(price, 1, self.starting_lot)

    def _open_level_trade(self, price: float, level: int, lot: float):
        """Open one grid level trade (simulator or MT5)."""
        # TP displayed per-trade (informational only; actual check is in tick())
        if level == 1:
            tp_display = (price + self.first_level_tp if self.direction == "BUY"
                          else price - self.first_level_tp)
        else:
            tp_display = (price + self.tp_points if self.direction == "BUY"
                          else price - self.tp_points)

        trade = Trade(
            trade_id    = self.trade_counter,
            level       = level,
            entry_price = price,
            lot_size    = lot,
            direction   = self.direction,
            take_profit = tp_display,
            opened_at   = datetime.now().strftime("%H:%M:%S"),
        )

        # Live MT5: place real order
        if self._live_mode and self.connector:
            ok, msg = self.connector.open_order(
                trade_id  = self.trade_counter,
                direction = self.direction,
                lot       = lot,
                comment   = f"MG_L{level}",
            )
            if ok:
                self._emit("log", {"msg": f"MT5 Order OK: {msg}", "type": "open"})
            else:
                self._emit("log", {"msg": f"MT5 Order FAILED: {msg}", "type": "loss"})
                return   # don't add to grid if MT5 rejected

        self.grid.append(trade)
        self.trade_counter += 1

        # Descriptive log
        rule = (f"TP: +{self.first_level_tp} pts (L1 rule)"
                if level == 1
                else f"TP: +{self.tp_points} pts from last entry")
        self._emit("log", {
            "msg": (f"Level {level} OPEN | {self.direction} {lot} lots "
                    f"@ {price:.2f} | {rule}"),
            "type": "open"
        })
        self._emit("trade_opened", trade.to_dict())

    def _close_grid_win(self, price: float, tp_rule: str = "GRID"):
        """Close all open trades on TP hit, then restart."""
        total_pnl = 0.0
        levels_closed = len([t for t in self.grid if t.status == "OPEN"])

        if self._live_mode and self.connector:
            report = self.connector.close_all_with_pnl("GridTP")
            total_pnl = report.get("total_pnl", 0.0)
            for rec in report.get("closed", []):
                sign = "+" if rec["pnl"] >= 0 else ""
                self._emit("log", {
                    "msg": (f"  Closed ticket {rec['ticket']} | "
                            f"{rec['direction']} {rec['lot']} lots | "
                            f"P/L: {sign}${abs(rec['pnl']):.2f}"),
                    "type": "win" if rec["pnl"] >= 0 else "loss"
                })
            info = self.connector.get_account_info()
            if info:
                self.balance = info["balance"]
                self.equity  = info["equity"]
        else:
            for trade in self.grid:
                if trade.status == "OPEN":
                    total_pnl += self._close_trade(trade, price, "GRID_WIN")
            self.balance += total_pnl
            self.equity   = self.balance

        self.win_count    += 1
        self.total_profit += max(total_pnl, 0)
        self.total_loss   += abs(min(total_pnl, 0))

        self.history.extend(self.grid)
        self.grid.clear()

        sign = "+" if total_pnl >= 0 else ""
        self._emit("log", {
            "msg": (f"GRID WIN [{tp_rule}] | {levels_closed} level(s) closed | "
                    f"P/L: {sign}${abs(total_pnl):.2f} | "
                    f"Balance: ${self.balance:.2f}"),
            "type": "win"
        })
        self._emit("grid_win", {
            "pnl":        round(total_pnl, 2),
            "balance":    round(self.balance, 2),
            "tp_rule":    tp_rule,
            "levels":     levels_closed,
        })

        # Restart: open fresh Level-1 trade
        self._open_first_trade(price)

    def _close_trade(self, trade: Trade, price: float, reason: str) -> float:
        trade.status      = f"CLOSED_{reason}"
        trade.close_price = price
        trade.closed_at   = datetime.now().strftime("%H:%M:%S")
        trade.pnl_points  = round(
            (price - trade.entry_price if self.direction == "BUY"
             else trade.entry_price - price), 2)
        trade.pnl_usd     = round(
            trade.pnl_points * self.point_value_per_lot * trade.lot_size, 2)
        return trade.pnl_usd

    def _calc_unrealized(self, price: float) -> float:
        total = 0.0
        for t in self.grid:
            if t.status == "OPEN":
                pts    = (price - t.entry_price if self.direction == "BUY"
                          else t.entry_price - price)
                total += pts * self.point_value_per_lot * t.lot_size
        return round(total, 2)

    def _get_state(self) -> dict:
        open_trades = [t.to_dict() for t in self.grid if t.status == "OPEN"]
        unrealized  = self._calc_unrealized(self.current_price)
        equity      = (self.balance + unrealized
                       if not self._live_mode else self.equity)

        # Compute TP target for dashboard display
        levels = len(open_trades)
        if levels == 1:
            first_entry = open_trades[0]["entry_price"]
            tp_target   = (first_entry + self.first_level_tp if self.direction == "BUY"
                           else first_entry - self.first_level_tp)
            tp_rule_lbl = f"+{self.first_level_tp} pts (L1)"
        elif levels > 1:
            last_entry = open_trades[-1]["entry_price"]
            tp_target  = (last_entry + self.tp_points if self.direction == "BUY"
                          else last_entry - self.tp_points)
            tp_rule_lbl = f"+{self.tp_points} pts (last entry)"
        else:
            tp_target   = 0.0
            tp_rule_lbl = "—"

        return {
            "is_running":     self.is_running,
            "mode":           "live" if self._live_mode else "simulator",
            "current_price":  self.current_price,
            "balance":        round(self.balance, 2),
            "equity":         round(equity, 2),
            "unrealized_pnl": unrealized,
            "open_trades":    open_trades,
            "grid_levels":    levels,
            "max_levels":     self.max_levels,
            "total_profit":   round(self.total_profit, 2),
            "total_loss":     round(self.total_loss, 2),
            "win_count":      self.win_count,
            "loss_count":     self.loss_count,
            "history_count":  len(self.history),
            "tp_target":      round(tp_target, 2),
            "tp_rule":        tp_rule_lbl,
            "timestamp":      datetime.now().strftime("%H:%M:%S"),
        }

    def _emit(self, event: str, data: dict):
        if self.on_event:
            try:
                self.on_event(event, data)
            except Exception:
                pass

    def get_state(self) -> dict:
        with self._lock:
            return self._get_state()

    def get_history(self) -> list:
        with self._lock:
            return [t.to_dict() for t in self.history]
