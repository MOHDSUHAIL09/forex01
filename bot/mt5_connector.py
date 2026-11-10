"""
MT5 Live Trading Connector — Multi-User Edition
- Auto-launches MT5 terminal on the SERVER (users need no MT5 on their device)
- Per-session connection with thread-safe locking
- close_all_with_pnl(): closes all positions and returns detailed P/L report
"""

import sys
import os
import time
import threading
import subprocess
from datetime import datetime
from typing import Callable, Optional, Dict, List

# Force UTF-8 output on Windows
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[WARNING] MetaTrader5 not installed. Run: pip install MetaTrader5")

# Global lock — MT5 Python API is NOT thread-safe; serialize all API calls
_mt5_lock = threading.Lock()


class MT5Connector:
    def __init__(self, config: dict,
                 on_tick: Optional[Callable] = None,
                 on_event: Optional[Callable] = None):
        self.cfg       = config
        self.on_tick   = on_tick
        self.on_event  = on_event
        self.connected = False
        self._running  = False
        self._thread: Optional[threading.Thread] = None
        self.symbol    = config["symbol"]
        self.magic     = config["magic_number"]
        self._tickets: Dict[int, int] = {}   # trade_id -> MT5 ticket
        self._mt5_proc: Optional[subprocess.Popen] = None   # launched process

    # ─────────────────────────────────────────
    # Connection  (server-side, auto-launch)
    # ─────────────────────────────────────────

    # Common MT5 installation paths to auto-detect
    COMMON_PATHS = [
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
        r"C:\Program Files\YumePrime MT5 Terminal\terminal64.exe",
        r"C:\Program Files\YumePrime Trade\terminal64.exe",
        r"C:\Program Files\YumePrime\terminal64.exe",
        r"C:\Program Files\ICMarkets - MetaTrader 5\terminal64.exe",
        r"C:\Program Files\XM MT5\terminal64.exe",
        r"C:\Program Files\Exness MT5\terminal64.exe",
        r"C:\Program Files\FxPro - MetaTrader 5\terminal64.exe",
        r"C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe",
    ]

    @classmethod
    def find_terminal(cls) -> str:
        """Auto-detect MT5 terminal64.exe from common install locations."""
        for p in cls.COMMON_PATHS:
            if os.path.exists(p):
                return p
        # Search AppData roaming (broker-branded installs)
        appdata = os.environ.get("APPDATA", "")
        mq_path = os.path.join(appdata, "MetaQuotes", "Terminal")
        if os.path.exists(mq_path):
            for root, dirs, files in os.walk(mq_path):
                if "terminal64.exe" in files:
                    return os.path.join(root, "terminal64.exe")
        return ""

    def connect(self) -> tuple:
        """
        Connect to MT5 terminal.
        - Attaches to already running MT5 terminal first (fast, reliable, no IPC timeout).
        - If MT5 is closed, launches via detected path.
        - Automatically checks active account; switches if requested with credentials.
        Returns (success, message).
        """
        if not MT5_AVAILABLE:
            return False, "MetaTrader5 library not installed on server. Run: pip install MetaTrader5"

        mt5_cfg  = self.cfg.get("mt5", {})
        path     = mt5_cfg.get("terminal_path", mt5_cfg.get("path", "")).strip()
        login    = int(mt5_cfg.get("login", 0)) if mt5_cfg.get("login") else 0
        password = str(mt5_cfg.get("password", "")).strip()
        server   = str(mt5_cfg.get("server", "")).strip()

        with _mt5_lock:
            # 1. Attach to running terminal first
            ok = mt5.initialize()
            if not ok:
                # Terminal not running; try launching via path
                if not path or not os.path.exists(path):
                    path = self.find_terminal()
                if path and os.path.exists(path):
                    print(f"[MT5] Terminal not running, launching from {path}...")
                    ok = mt5.initialize(path=path)

            if not ok:
                err = mt5.last_error()
                return False, f"MT5 initialize failed: {err}. Please make sure MetaTrader 5 is running on the computer."

            # 2. Check active account in terminal
            acc = mt5.account_info()
            if acc is None:
                return False, "MT5 connected, but no account is logged in inside the terminal."

            print(f"[MT5] Terminal currently logged into: {acc.login} ({acc.server})")

            # 3. Check requested account
            if login and login > 0:
                is_same_login = (acc.login == login)
                is_same_server = (not server or acc.server.strip().lower() == server.strip().lower())

                if is_same_login and is_same_server:
                    print(f"[MT5] Already logged into requested account {acc.login} ({acc.server})")
                else:
                    # Need to switch account
                    if not password:
                        return False, (
                            f"MT5 is currently logged into account {acc.login} ({acc.server}). "
                            f"To switch to account {login} ({server}), please enter the password in the form, "
                            f"or double-click account {login} directly inside your MetaTrader 5 terminal."
                        )

                    print(f"[MT5] Switching account to {login} @ {server}...")
                    login_kwargs = {"login": login, "timeout": 10000}
                    if password:
                        login_kwargs["password"] = password
                    if server:
                        login_kwargs["server"] = server

                    auth = mt5.login(**login_kwargs)
                    if not auth:
                        err = mt5.last_error()
                        return False, (
                            f"MT5 login failed for account {login} on {server}: {err}. "
                            f"Please verify account number, password, and exact broker server name."
                        )

                    acc = mt5.account_info()
                    if not acc:
                        return False, "Login succeeded but failed to retrieve account info."

            # 4. Ensure symbol is visible
            sym = mt5.symbol_info(self.symbol)
            if sym is None or not sym.visible:
                mt5.symbol_select(self.symbol, True)

        self.connected = True
        msg = (
            f"Connected OK | Account: {acc.login} | "
            f"Balance: {acc.balance:.2f} {acc.currency} | "
            f"Leverage: 1:{acc.leverage} | Broker: {acc.company}"
        )
        print(f"[MT5] {msg}")
        return True, msg

    def disconnect(self):
        self._running = False
        self.connected = False
        print("[MT5] Disconnected.")

    # ─────────────────────────────────────────
    # Tick Feed
    # ─────────────────────────────────────────

    def start_tick_feed(self, interval_ms: int = 250):
        if not self.connected:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._tick_loop, args=(interval_ms,),
            daemon=True, name="MT5TickFeed"
        )
        self._thread.start()
        print(f"[MT5] Tick feed started ({interval_ms}ms)")

    def stop_tick_feed(self):
        self._running = False

    def _tick_loop(self, interval_ms: int):
        while self._running and self.connected:
            tick = self.get_tick()
            if tick and self.on_tick:
                try:
                    self.on_tick(tick["ask"])
                except Exception as e:
                    print(f"[MT5] Tick callback error: {e}")
            time.sleep(interval_ms / 1000)

    def get_tick(self) -> Optional[dict]:
        if not self.connected:
            return None
        with _mt5_lock:
            t = mt5.symbol_info_tick(self.symbol)
        if t is None:
            return None
        return {"bid": t.bid, "ask": t.ask, "spread": round(t.ask - t.bid, 5)}

    # ─────────────────────────────────────────
    # Order Execution
    # ─────────────────────────────────────────

    def _get_filling_mode(self, sym_info):
        filling = getattr(sym_info, "filling_mode", 0)
        if filling & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        elif filling & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def open_order(self, trade_id: int, direction: str, lot: float,
                   sl: float = 0.0, comment: str = "MartingaleBot") -> tuple:
        if not self.connected:
            return False, "Not connected to MT5"

        with _mt5_lock:
            sym_info = mt5.symbol_info(self.symbol)
            if sym_info is None:
                return False, f"Symbol {self.symbol} not found"
            if not sym_info.visible:
                mt5.symbol_select(self.symbol, True)

            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                return False, "Cannot get current price tick"

            order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
            price      = tick.ask if direction == "BUY" else tick.bid

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       self.symbol,
                "volume":       float(lot),
                "type":         order_type,
                "price":        price,
                "deviation":    20,
                "magic":        self.magic,
                "comment":      comment[:31],
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling_mode(sym_info),
            }
            if sl > 0:
                request["sl"] = sl

            result = mt5.order_send(request)

        if result is None:
            with _mt5_lock:
                err = mt5.last_error()
            return False, f"order_send None: {err}"

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self._tickets[trade_id] = result.order
            msg = f"Order OK | Ticket:{result.order} | {direction} {lot} @ {result.price:.2f}"
            print(f"[MT5] {msg}")
            return True, msg
        return False, f"Order failed: retcode={result.retcode} | {result.comment}"

    def close_order(self, trade_id: int, direction: str, lot: float) -> tuple:
        if not self.connected:
            return False, "Not connected"

        ticket = self._tickets.get(trade_id)
        if ticket is None:
            return False, f"No ticket for trade_id={trade_id}"

        with _mt5_lock:
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                return False, "Cannot get price"

            close_type = mt5.ORDER_TYPE_SELL if direction == "BUY" else mt5.ORDER_TYPE_BUY
            price      = tick.bid if direction == "BUY" else tick.ask
            sym_info   = mt5.symbol_info(self.symbol)
            fill_mode  = self._get_filling_mode(sym_info) if sym_info else mt5.ORDER_FILLING_IOC

            result = mt5.order_send({
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       self.symbol,
                "volume":       float(lot),
                "type":         close_type,
                "position":     ticket,
                "price":        price,
                "deviation":    20,
                "magic":        self.magic,
                "comment":      "BotClose",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": fill_mode,
            })

        if result is None:
            with _mt5_lock:
                err = mt5.last_error()
            return False, f"close None: {err}"

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self._tickets.pop(trade_id, None)
            msg = f"Closed OK | Ticket:{ticket} @ {result.price:.2f}"
            print(f"[MT5] {msg}")
            return True, msg
        return False, f"Close failed: {result.retcode} | {result.comment}"

    # ─────────────────────────────────────────
    # Close All — with detailed P/L report
    # ─────────────────────────────────────────

    def close_all_with_pnl(self, reason: str = "BotStop") -> dict:
        """
        Close ALL open positions with this magic number.
        Returns a detailed P/L report:
        {
          "closed": [ { ticket, direction, lot, open_price, close_price, pnl, pnl_pct } ],
          "total_pnl": float,
          "total_trades": int,
          "success_count": int,
          "failed_count": int,
          "reason": str,
          "timestamp": str
        }
        """
        report = {
            "closed":        [],
            "total_pnl":     0.0,
            "total_trades":  0,
            "success_count": 0,
            "failed_count":  0,
            "reason":        reason,
            "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        if not self.connected:
            report["error"] = "Not connected to MT5"
            return report

        with _mt5_lock:
            positions = mt5.positions_get(symbol=self.symbol) or []

        my_positions = [p for p in positions if p.magic == self.magic]
        report["total_trades"] = len(my_positions)

        if not my_positions:
            return report

        for pos in my_positions:
            direction  = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            close_type = mt5.ORDER_TYPE_SELL if direction == "BUY" else mt5.ORDER_TYPE_BUY

            with _mt5_lock:
                tick = mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    report["failed_count"] += 1
                    continue
                close_price = tick.bid if direction == "BUY" else tick.ask
                sym_info    = mt5.symbol_info(self.symbol)
                fill_mode   = self._get_filling_mode(sym_info) if sym_info else mt5.ORDER_FILLING_IOC

                result = mt5.order_send({
                    "action":       mt5.TRADE_ACTION_DEAL,
                    "symbol":       self.symbol,
                    "volume":       pos.volume,
                    "type":         close_type,
                    "position":     pos.ticket,
                    "price":        close_price,
                    "deviation":    30,
                    "magic":        self.magic,
                    "comment":      reason[:20],
                    "type_time":    mt5.ORDER_TIME_GTC,
                    "type_filling": fill_mode,
                })

            # Calculate real P/L from MT5 position profit field
            realized_pnl = round(pos.profit, 2)

            trade_record = {
                "ticket":      pos.ticket,
                "direction":   direction,
                "lot":         pos.volume,
                "open_price":  pos.price_open,
                "close_price": round(close_price, 2),
                "pnl":         realized_pnl,
                "open_time":   datetime.fromtimestamp(pos.time).strftime("%H:%M:%S"),
            }

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                trade_record["status"] = "closed"
                report["success_count"] += 1
                report["total_pnl"]    += realized_pnl
                self._tickets.pop(pos.ticket, None)
                print(f"[MT5] Closed ticket {pos.ticket} | P/L: {realized_pnl:+.2f}")
            else:
                trade_record["status"] = "failed"
                trade_record["error"]  = result.comment if result else "unknown"
                report["failed_count"] += 1
                print(f"[MT5] Failed to close ticket {pos.ticket}")

            report["closed"].append(trade_record)

        report["total_pnl"] = round(report["total_pnl"], 2)
        return report

    # Keep backward compat
    def close_all_by_magic(self) -> list:
        result = self.close_all_with_pnl("EmergencyClose")
        msgs = []
        for t in result["closed"]:
            msgs.append(
                f"Ticket {t['ticket']} {t['direction']} {t['lot']} | "
                f"P/L: {t['pnl']:+.2f} | {t['status'].upper()}"
            )
        if not msgs:
            msgs.append("No open positions found")
        return msgs

    # ─────────────────────────────────────────
    # Account & Symbol Info
    # ─────────────────────────────────────────

    def get_account_info(self) -> Optional[dict]:
        if not self.connected:
            return None
        with _mt5_lock:
            info = mt5.account_info()
        if info is None:
            return None
        return {
            "login":       info.login,
            "balance":     info.balance,
            "equity":      info.equity,
            "margin":      info.margin,
            "free_margin": info.margin_free,
            "profit":      info.profit,
            "leverage":    info.leverage,
            "currency":    info.currency,
            "company":     info.company,
            "server":      info.server,
        }

    def get_symbol_info(self) -> Optional[dict]:
        if not self.connected:
            return None
        with _mt5_lock:
            s = mt5.symbol_info(self.symbol)
        if s is None:
            return None
        return {
            "symbol":           s.name,
            "digits":           s.digits,
            "point":            s.point,
            "trade_tick_value": s.trade_tick_value,
            "volume_min":       s.volume_min,
            "volume_max":       s.volume_max,
            "volume_step":      s.volume_step,
        }

    def get_open_positions(self) -> list:
        if not self.connected:
            return []
        with _mt5_lock:
            positions = mt5.positions_get(symbol=self.symbol) or []
        return [
            {
                "ticket":     p.ticket,
                "type":       "BUY" if p.type == 0 else "SELL",
                "volume":     p.volume,
                "open_price": p.price_open,
                "current":    p.price_current,
                "profit":     p.profit,
                "sl":         p.sl,
                "tp":         p.tp,
                "comment":    p.comment,
            }
            for p in positions if p.magic == self.magic
        ]
