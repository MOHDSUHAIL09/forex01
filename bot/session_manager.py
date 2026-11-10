"""
Session Manager — manages per-user bot sessions.
Each user gets an isolated engine + MT5 connector instance.
"""

import uuid
import threading
from datetime import datetime
from typing import Dict, Optional


class BotSession:
    """One isolated trading session per user."""

    def __init__(self, session_id: str, user_name: str, config: dict):
        self.session_id  = session_id
        self.user_name   = user_name
        self.config      = config
        self.engine      = None
        self.connector   = None
        self.simulator   = None
        self.mode        = "simulator"
        self.created_at  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.started_at  = None
        self.socket_id   = None   # current socket.io sid

    def is_running(self) -> bool:
        return self.engine is not None and self.engine.is_running

    def to_summary(self) -> dict:
        state = {}
        if self.engine:
            state = self.engine.get_state()
        return {
            "session_id":  self.session_id,
            "user_name":   self.user_name,
            "mode":        self.mode,
            "is_running":  self.is_running(),
            "created_at":  self.created_at,
            "started_at":  self.started_at,
            "balance":     state.get("balance", 0),
            "equity":      state.get("equity", 0),
            "total_profit":state.get("total_profit", 0),
            "grid_levels": state.get("grid_levels", 0),
            "win_count":   state.get("win_count", 0),
            "loss_count":  state.get("loss_count", 0),
            "mt5_login":   self.config.get("mt5", {}).get("login", ""),
            "mt5_server":  self.config.get("mt5", {}).get("server", ""),
            "symbol":      self.config.get("symbol", "XAUUSD"),
        }


class SessionManager:
    """Thread-safe registry of all active user sessions."""

    def __init__(self):
        self._sessions: Dict[str, BotSession] = {}
        self._lock = threading.Lock()

    def create_session(self, user_name: str, base_config: dict) -> BotSession:
        """Create a new session. Returns the new BotSession."""
        import copy
        session_id = str(uuid.uuid4())
        cfg = copy.deepcopy(base_config)
        session = BotSession(session_id, user_name, cfg)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[BotSession]:
        return self._sessions.get(session_id)

    def remove(self, session_id: str):
        session = self._sessions.pop(session_id, None)
        if session:
            self._cleanup(session)

    def all_sessions(self) -> list:
        with self._lock:
            return [s.to_summary() for s in self._sessions.values()]

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.is_running())

    def _cleanup(self, session: BotSession):
        try:
            if session.engine and session.engine.is_running:
                session.engine.stop()
            if session.simulator:
                session.simulator.stop()
            if session.connector and session.connector.connected:
                session.connector.stop_tick_feed()
                session.connector.disconnect()
        except Exception as e:
            print(f"[SessionManager] Cleanup error for {session.session_id}: {e}")
