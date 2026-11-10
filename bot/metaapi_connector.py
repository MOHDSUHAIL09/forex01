"""
MetaApi Cloud Connector — Direct MT5 Cloud REST API Integration
Allows connecting to any MT5 account (demo/live) in the cloud without
having MetaTrader 5 installed or running locally.
"""

import time
import threading
import urllib3
import requests
from typing import Callable, Optional, Dict, List

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class MetaApiConnector:
    """
    Cloud MT5 connector using MetaApi REST API.
    Interchangeable with local MT5Connector.
    """

    PROVISIONING_URL = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
    CLIENT_URL       = "https://mt-client-api-v1.london.agiliumtrade.ai"

    def __init__(self, config: dict,
                 on_tick: Optional[Callable] = None,
                 on_event: Optional[Callable] = None):
        self.cfg        = config
        self.on_tick    = on_tick
        self.on_event   = on_event
        self.connected  = False
        self._running   = False
        self._thread: Optional[threading.Thread] = None
        self.symbol     = config.get("symbol", "XAUUSD")
        self.magic      = config.get("magic_number", 202611)

        # MetaApi token from config
        cloud_cfg   = config.get("metaapi", {})
        self.token  = cloud_cfg.get("token", "").strip()

        # MT5 account details
        mt5_cfg     = config.get("mt5", {})
        self.login  = str(mt5_cfg.get("login", "")).strip()
        self.password = str(mt5_cfg.get("password", "")).strip()
        self.server = str(mt5_cfg.get("server", "")).strip()

        self.account_id: Optional[str] = None
        self.region: Optional[str] = None
        self.client_url: str = self.CLIENT_URL
        # trade_id -> positionId
        self._position_ids: Dict[int, str] = {}

        # Reusable HTTP session with SSL verification bypassed for custom broker certificates
        self.session = requests.Session()
        self.session.verify = False

    def _resolve_client_url(self, region: Optional[str] = None) -> str:
        """Resolve regional client API URL via MetaApi provisioning service."""
        r = region or "london"
        url = f"https://mt-client-api-v1.{r}.agiliumtrade.ai"
        print(f"[MetaApi] Using regional client URL: {url}", flush=True)
        return url

    @property
    def headers(self) -> dict:
        return {
            "auth-token": self.token,
            "Content-Type": "application/json"
        }

    # ─────────────────────────────────────────
    # Connection / Account Provisioning
    # ─────────────────────────────────────────

    def connect(self) -> tuple[bool, str]:
        """
        Connect to MT5 in the Cloud via MetaApi.
        1. Checks MetaApi token.
        2. Finds or creates MT5 account in MetaApi.
        3. Deploys and waits for connection.
        """
        if not self.token:
            return False, "MetaApi token missing! Please configure 'metaapi.token' in config.json or enter in dashboard."

        if not self.login or not self.password or not self.server:
            return False, "MT5 Login, Password, and Server are required for Cloud connection."

        try:
            print(f"[MetaApi] Searching existing cloud accounts for {self.login} @ {self.server}...", flush=True)
            # 1. Search existing account
            res = self.session.get(f"{self.PROVISIONING_URL}/users/current/accounts",
                                   headers=self.headers, timeout=25)
            if res.status_code != 200:
                err_msg = res.json().get("message", res.text)
                return False, f"MetaApi Auth Error: {err_msg}"

            accounts = res.json()
            target_acc = None
            for acc in accounts:
                if str(acc.get("login")) == self.login and acc.get("server", "").lower() == self.server.lower():
                    target_acc = acc
                    break

            # 2. Create if not found
            if not target_acc:
                print(f"[MetaApi] Creating new cloud MT5 account for {self.login} on {self.server}...", flush=True)
                create_payload = {
                    "name": f"ForexBot_{self.login}",
                    "type": "cloud",
                    "login": self.login,
                    "password": self.password,
                    "server": self.server,
                    "platform": "mt5",
                    "application": "ForexBot",
                    "magic": self.magic,
                    "reliability": "regular"
                }
                c_res = self.session.post(f"{self.PROVISIONING_URL}/users/current/accounts",
                                          headers=self.headers, json=create_payload, timeout=25)
                if c_res.status_code not in (200, 201):
                    err_msg = c_res.json().get("message", c_res.text)
                    return False, f"Failed to register MT5 account on MetaApi: {err_msg}"
                target_acc = c_res.json()

            self.account_id = target_acc.get("_id") or target_acc.get("id")
            if not self.account_id:
                return False, f"Could not determine MetaApi account ID from response: {target_acc}"

            self.region = target_acc.get("region")
            self.client_url = self._resolve_client_url(self.region)
            print(f"[MetaApi] Cloud Account ID: {self.account_id} | Region: {self.region} | Client URL: {self.client_url}", flush=True)

            # Ensure latest password is set on existing account
            try:
                self.session.put(
                    f"{self.PROVISIONING_URL}/users/current/accounts/{self.account_id}",
                    headers=self.headers,
                    json={"password": self.password},
                    timeout=15
                )
            except Exception as e:
                print(f"[MetaApi] Password update note: {e}", flush=True)

            # 3. Deploy if not deployed
            state = target_acc.get("state")
            if state != "DEPLOYED":
                print(f"[MetaApi] Deploying cloud account {self.account_id} (current state: {state})...", flush=True)
                d_res = self.session.post(
                    f"{self.PROVISIONING_URL}/users/current/accounts/{self.account_id}/deploy",
                    headers={"auth-token": self.token}, timeout=20
                )
                print(f"[MetaApi] Deploy response: {d_res.status_code} - {d_res.text}", flush=True)

                if d_res.status_code not in (200, 201, 202):
                    print(f"[MetaApi] Deploy returned {d_res.status_code}, trying redeploy...", flush=True)
                    r_res = self.session.post(
                        f"{self.PROVISIONING_URL}/users/current/accounts/{self.account_id}/redeploy",
                        headers={"auth-token": self.token}, timeout=20
                    )
                    print(f"[MetaApi] Redeploy response: {r_res.status_code} - {r_res.text}", flush=True)
                    if r_res.status_code not in (200, 201, 202):
                        err_text = r_res.text or d_res.text
                        try:
                            err_msg = (r_res.json() if r_res.text.startswith("{") else d_res.json()).get("message", err_text)
                        except Exception:
                            err_msg = err_text
                        return False, f"MetaApi Deploy Failed ({d_res.status_code}): {err_msg}"

            # 4. Wait for connection (poll up to 90s)
            print("[MetaApi] Waiting for cloud connection to broker...", flush=True)
            start_time = time.time()
            connected_ok = False
            last_status = None
            last_err_reason = None
            undeployed_count = 0

            while time.time() - start_time < 90:
                try:
                    status_res = self.session.get(
                        f"{self.PROVISIONING_URL}/users/current/accounts/{self.account_id}",
                        headers={"auth-token": self.token}, timeout=25
                    )
                    if status_res.status_code == 200:
                        data = status_res.json()
                        conn_status = data.get("connectionStatus")
                        state = data.get("state")
                        last_status = f"State: {state}, Connection: {conn_status}"
                        last_err_reason = data.get("connectionError")

                        print(f"[MetaApi] Polling status: {last_status}", flush=True)

                        if conn_status == "CONNECTED" and state == "DEPLOYED":
                            connected_ok = True
                            if data.get("region") and data.get("region") != self.region:
                                self.region = data.get("region")
                                self.client_url = self._resolve_client_url(self.region)
                            time.sleep(3.0)
                            break
                        elif state == "DEPLOY_FAILED":
                            return False, f"MetaApi Deployment Failed: {last_err_reason or 'Internal cloud error'}"
                        elif state == "UNDEPLOYED":
                            undeployed_count += 1
                            if undeployed_count >= 5:
                                return False, f"MetaApi account remained UNDEPLOYED. Reason: {last_err_reason or 'Account not deployed'}. Please check https://app.metaapi.cloud."
                        elif conn_status == "DISCONNECTED" and state == "DEPLOYED":
                            if last_err_reason:
                                # Immediate return on known fatal auth errors
                                if any(x in str(last_err_reason).upper() for x in ("INVALID", "PASSWORD", "ACCOUNT", "NOT_FOUND")):
                                    return False, f"Broker login failed: {last_err_reason}. Please check your MT5 login, password, and server name."
                except Exception as re:
                    print(f"[MetaApi] Polling network retry: {re}", flush=True)

                time.sleep(3.0)

            if not connected_ok:
                err_detail = f" Last status: [{last_status}]" if last_status else ""
                if last_err_reason:
                    err_detail += f" Error: {last_err_reason}"
                return False, (
                    f"MetaApi cloud connection timed out while establishing connection with broker.{err_detail}. "
                    f"Please verify your MT5 account password and server in https://app.metaapi.cloud."
                )

            # 5. Fetch account info
            info = self.get_account_info()
            if not info:
                return False, "Connected to cloud, but failed to fetch account information."

            self.connected = True
            msg = (
                f"Connected OK (Cloud) | Account: {info['login']} | "
                f"Balance: {info['balance']:.2f} {info['currency']} | "
                f"Leverage: 1:{info['leverage']} | Broker: {info['company']}"
            )
            print(f"[MetaApi] {msg}")
            return True, msg

        except Exception as e:
            return False, f"MetaApi Connection Exception: {str(e)}"

    def disconnect(self):
        self._running = False
        self.connected = False
        print("[MetaApi] Disconnected.")

    # ─────────────────────────────────────────
    # Account & Symbol Info
    # ─────────────────────────────────────────

    def get_account_info(self) -> Optional[dict]:
        if not self.account_id:
            return None

        r = self.region or "london"
        urls = [
            f"https://mt-client-api-v1.{r}.agiliumtrade.ai/users/current/accounts/{self.account_id}/account-information",
            f"{self.client_url}/users/current/accounts/{self.account_id}/account-information"
        ]

        for attempt in range(5):
            for url in urls:
                try:
                    res = self.session.get(url, headers=self.headers, timeout=12)
                    if res.status_code == 200:
                        data = res.json()
                        return {
                            "login":       data.get("login", self.login),
                            "balance":     data.get("balance", 0.0),
                            "equity":      data.get("equity", 0.0),
                            "margin":      data.get("margin", 0.0),
                            "free_margin": data.get("freeMargin", 0.0),
                            "profit":      data.get("profit", 0.0),
                            "leverage":    data.get("leverage", 100),
                            "currency":    data.get("currency", "USD"),
                            "company":     data.get("broker", self.server),
                            "server":      self.server,
                        }
                    else:
                        print(f"[MetaApi] get_account_info (attempt {attempt+1}) {res.status_code}: {res.text[:120]}", flush=True)
                except Exception as e:
                    print(f"[MetaApi] get_account_info error: {e}", flush=True)

            if attempt < 4:
                time.sleep(2.0)

        return None

    def get_symbol_info(self) -> Optional[dict]:
        if not self.account_id:
            return None
        try:
            url = f"{self.client_url}/users/current/accounts/{self.account_id}/symbols/{self.symbol}/specification"
            res = self.session.get(url, headers=self.headers, timeout=10)
            if res.status_code == 200:
                s = res.json()
                return {
                    "symbol":           s.get("symbol", self.symbol),
                    "digits":           s.get("digits", 2),
                    "point":            s.get("point", 0.01),
                    "trade_tick_value": s.get("tickValue", 0.1),
                    "volume_min":       s.get("minVolume", 0.01),
                    "volume_max":       s.get("maxVolume", 100.0),
                    "volume_step":      s.get("volumeStep", 0.01),
                }
        except Exception:
            pass
        return {
            "symbol": self.symbol,
            "digits": 2,
            "point": 0.01,
            "trade_tick_value": 0.1,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }

    # ─────────────────────────────────────────
    # Price / Tick Feed
    # ─────────────────────────────────────────

    def get_tick(self) -> Optional[dict]:
        if not self.account_id:
            return None
        try:
            url = f"{self.client_url}/users/current/accounts/{self.account_id}/symbols/{self.symbol}/current-price"
            res = self.session.get(url, headers=self.headers, timeout=6)
            if res.status_code == 200:
                data = res.json()
                bid = float(data.get("bid", 0.0))
                ask = float(data.get("ask", 0.0))
                return {"bid": bid, "ask": ask, "spread": round(ask - bid, 5)}
            elif res.status_code == 404:
                # Try fallback aliases
                for alias in ["GOLD", "XAUUSD.m", "XAUUSDm", "XAUUSD.pro", "XAUUSD.cent", "XAUUSD.y"]:
                    if alias == self.symbol:
                        continue
                    a_url = f"{self.client_url}/users/current/accounts/{self.account_id}/symbols/{alias}/current-price"
                    a_res = self.session.get(a_url, headers=self.headers, timeout=5)
                    if a_res.status_code == 200:
                        data = a_res.json()
                        self.symbol = alias
                        print(f"[MetaApi] Auto-resolved gold symbol to: {alias}", flush=True)
                        bid = float(data.get("bid", 0.0))
                        ask = float(data.get("ask", 0.0))
                        return {"bid": bid, "ask": ask, "spread": round(ask - bid, 5)}
        except Exception as e:
            pass
        return None

    def start_tick_feed(self, interval_ms: int = 500):
        if not self.connected:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._tick_loop, args=(interval_ms,),
            daemon=True, name="MetaApiTickFeed"
        )
        self._thread.start()
        print(f"[MetaApi] Cloud tick feed started ({interval_ms}ms)")

    def stop_tick_feed(self):
        self._running = False

    def _tick_loop(self, interval_ms: int):
        while self._running and self.connected:
            tick = self.get_tick()
            if tick and self.on_tick:
                try:
                    self.on_tick(tick["ask"])
                except Exception as e:
                    print(f"[MetaApi] Tick callback error: {e}")
            time.sleep(max(interval_ms, 300) / 1000)

    # ─────────────────────────────────────────
    # Order Execution
    # ─────────────────────────────────────────

    def open_order(self, trade_id: int, direction: str, lot: float,
                   sl: float = 0.0, comment: str = "MartingaleBot") -> tuple[bool, str]:
        if not self.connected or not self.account_id:
            return False, "Not connected to MetaApi cloud"

        action_type = "ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL"
        payload = {
            "actionType": action_type,
            "symbol": self.symbol,
            "volume": float(lot),
            "comment": comment[:31],
        }
        if sl > 0:
            payload["stopLoss"] = sl

        try:
            url = f"{self.client_url}/users/current/accounts/{self.account_id}/trade"
            res = self.session.post(url, headers=self.headers, json=payload, timeout=12)
            data = res.json()

            if res.status_code == 200 and data.get("numericCode") in (10009, 10008, 0):
                pos_id = str(data.get("positionId") or data.get("orderId"))
                self._position_ids[trade_id] = pos_id
                price = data.get("openPrice", 0.0)
                if price <= 0:
                    t = self.get_tick()
                    price = (t["ask"] if direction == "BUY" else t["bid"]) if t else 0.0
                msg = f"Cloud Order OK | Position:{pos_id} | {direction} {lot} @ {price:.2f}"
                print(f"[MetaApi] {msg}", flush=True)
                return True, msg
            else:
                err_msg = data.get("stringCode") or data.get("message") or res.text
                print(f"[MetaApi] Cloud order failed ({res.status_code}): {err_msg}", flush=True)
                return False, f"Cloud order failed: {err_msg}"
        except Exception as e:
            print(f"[MetaApi] Trade exception: {str(e)}", flush=True)
            return False, f"Trade exception: {str(e)}"

    def close_order(self, trade_id: int, direction: str, lot: float) -> tuple[bool, str]:
        if not self.connected or not self.account_id:
            return False, "Not connected"

        pos_id = self._position_ids.get(trade_id)
        if not pos_id:
            return False, f"No position ID recorded for trade_id={trade_id}"

        payload = {
            "actionType": "POSITION_CLOSE_ID",
            "positionId": pos_id
        }
        try:
            url = f"{self.client_url}/users/current/accounts/{self.account_id}/trade"
            res = self.session.post(url, headers=self.headers, json=payload, timeout=12)
            data = res.json()
            if res.status_code == 200 and data.get("numericCode") in (10009, 10008, 0):
                self._position_ids.pop(trade_id, None)
                msg = f"Closed OK | Position:{pos_id}"
                print(f"[MetaApi] {msg}")
                return True, msg
            else:
                return False, f"Close failed: {data.get('stringCode', res.text)}"
        except Exception as e:
            return False, f"Close exception: {str(e)}"

    def close_all_with_pnl(self, reason: str = "BotStop") -> dict:
        """Close all open positions on the cloud account and return detailed PnL report."""
        report = {
            "closed": [],
            "total_pnl": 0.0,
            "total_trades": 0,
            "success_count": 0,
            "failed_count": 0,
            "reason": reason,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        if not self.connected or not self.account_id:
            report["error"] = "Not connected"
            return report

        try:
            # 1. Fetch current open positions from cloud
            url = f"{self.client_url}/users/current/accounts/{self.account_id}/positions"
            res = self.session.get(url, headers=self.headers, timeout=10)
            if res.status_code != 200:
                report["error"] = "Failed to fetch open positions"
                return report

            positions = res.json()
            # Filter symbol (accept current symbol and any gold aliases)
            gold_keys = (self.symbol.upper(), "XAU", "GOLD")
            my_pos = [
                p for p in positions
                if any(k in str(p.get("symbol", "")).upper() for k in gold_keys)
            ]
            report["total_trades"] = len(my_pos)

            for pos in my_pos:
                pos_id = str(pos.get("id") or pos.get("positionId") or "")
                profit = float(pos.get("unrealizedProfit", pos.get("profit", 0.0)))
                direction = "BUY" if pos.get("type") in (0, "POSITION_TYPE_BUY") else "SELL"

                close_payload = {
                    "actionType": "POSITION_CLOSE_ID",
                    "positionId": pos_id
                }
                c_res = self.session.post(
                    f"{self.client_url}/users/current/accounts/{self.account_id}/trade",
                    headers=self.headers, json=close_payload, timeout=10
                )
                c_data = c_res.json()
                is_closed = (c_res.status_code == 200 and c_data.get("numericCode") in (10009, 10008, 0))

                trade_record = {
                    "ticket": pos_id,
                    "direction": direction,
                    "lot": pos.get("volume", 0.01),
                    "open_price": pos.get("openPrice", 0.0),
                    "close_price": pos.get("currentPrice", 0.0),
                    "pnl": round(profit, 2),
                    "status": "closed" if is_closed else "failed"
                }

                if is_closed:
                    report["success_count"] += 1
                    report["total_pnl"] += round(profit, 2)
                    print(f"[MetaApi] Position {pos_id} closed | P/L: ${profit:+.2f}", flush=True)
                else:
                    report["failed_count"] += 1
                    print(f"[MetaApi] Failed to close position {pos_id}: {c_data}", flush=True)

                report["closed"].append(trade_record)

            # Also check locally tracked positions
            for trade_id, tracked_id in list(self._position_ids.items()):
                if any(str(p.get("id") or p.get("positionId")) == str(tracked_id) for p in my_pos):
                    continue
                try:
                    c_res = self.session.post(
                        f"{self.client_url}/users/current/accounts/{self.account_id}/trade",
                        headers=self.headers, json={"actionType": "POSITION_CLOSE_ID", "positionId": str(tracked_id)}, timeout=10
                    )
                    c_data = c_res.json()
                    if c_res.status_code == 200 and c_data.get("numericCode") in (10009, 10008, 0):
                        report["success_count"] += 1
                        report["closed"].append({"ticket": str(tracked_id), "status": "closed", "pnl": 0.0})
                except Exception:
                    pass
            self._position_ids.clear()

            report["total_pnl"] = round(report["total_pnl"], 2)
            print(f"[MetaApi] close_all_with_pnl ({reason}): {report['success_count']} closed | Total P/L: ${report['total_pnl']:+.2f}", flush=True)
        except Exception as e:
            report["error"] = str(e)

        return report

    def close_all_by_magic(self) -> list:
        res = self.close_all_with_pnl("Emergency")
        msgs = []
        for t in res.get("closed", []):
            msgs.append(f"Position {t['ticket']} {t['direction']} {t['lot']} | P/L: {t['pnl']:+.2f}")
        return msgs if msgs else ["No open positions"]

    def get_open_positions(self) -> list:
        if not self.account_id:
            return []
        try:
            url = f"{self.client_url}/users/current/accounts/{self.account_id}/positions"
            res = self.session.get(url, headers=self.headers, timeout=8)
            if res.status_code == 200:
                pos = res.json()
                return [
                    {
                        "ticket": p.get("id"),
                        "type": "BUY" if p.get("type") in (0, "POSITION_TYPE_BUY") else "SELL",
                        "volume": p.get("volume"),
                        "open_price": p.get("openPrice"),
                        "current": p.get("currentPrice"),
                        "profit": p.get("unrealizedProfit", p.get("profit", 0.0)),
                        "sl": p.get("stopLoss", 0.0),
                        "tp": p.get("takeProfit", 0.0),
                        "comment": p.get("comment", "")
                    }
                    for p in pos if p.get("symbol") == self.symbol
                ]
        except Exception:
            pass
        return []
