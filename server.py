"""
ForexBot — Multi-User Flask + Socket.IO Server
Each user gets an isolated bot session with their own MT5 credentials.
"""

import os
import sys
import time
import json
import copy
import threading
import requests
from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS

from bot.engine          import MartingaleEngine
from bot.simulator       import PriceSimulator
from bot.mt5_connector   import MT5Connector
from bot.metaapi_connector import MetaApiConnector
from bot.session_manager import SessionManager, BotSession

# ── Base config ──────────────────────────────────────────
def load_base_config() -> dict:
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    env_token = os.environ.get("METAAPI_TOKEN")
    if env_token:
        cfg.setdefault("metaapi", {})["token"] = env_token.strip()
    return cfg

BASE_CONFIG = load_base_config()

# ── Flask App ─────────────────────────────────────────────
app = Flask(__name__, static_folder="dashboard", static_url_path="")
app.secret_key = "forexbot-multi-user-secret-2024"
CORS(app)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)

@app.route("/api/debug/metaapi")
def debug_metaapi():
    sessions = session_mgr.all_sessions()
    details = []
    with session_mgr._lock:
        for sid, s in session_mgr._sessions.items():
            info = {
                "session_id": sid,
                "user_name": s.user_name,
                "mode": s.mode,
                "is_running": s.is_running(),
                "connector_type": type(s.connector).__name__ if s.connector else None,
                "connector_connected": s.connector.connected if s.connector else False,
                "connector_running": getattr(s.connector, "_running", False) if s.connector else False,
                "connector_account_id": getattr(s.connector, "account_id", None) if s.connector else None,
                "engine_price": s.engine.current_price if s.engine else None,
                "grid_len": len(s.engine.grid) if s.engine else 0,
            }
            if s.connector:
                try:
                    info["live_tick"] = s.connector.get_tick()
                except Exception as ex:
                    info["live_tick_err"] = str(ex)
            details.append(info)
    return jsonify({"sessions": sessions, "details": details})

# ── Global session manager ────────────────────────────────
session_mgr = SessionManager()

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def get_session_id() -> str:
    """Return current user's session_id from Flask session cookie."""
    return session.get("session_id", "")

def get_bot_session() -> BotSession:
    return session_mgr.get(get_session_id())

def make_event_handler(bot_session: BotSession):
    """Return an on_event callback that emits only to this user's room."""
    def handler(event: str, data: dict):
        socketio.emit(event, data, room=bot_session.session_id)
        if event == "log":
            socketio.emit("server_log", data, room=bot_session.session_id)
    return handler


# ─────────────────────────────────────────────────────────
# Static Pages
# ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not get_session_id() or not session_mgr.get(get_session_id()):
        return send_from_directory("dashboard", "login.html")
    return send_from_directory("dashboard", "index.html")

@app.route("/dashboard")
def dashboard():
    return send_from_directory("dashboard", "index.html")

@app.route("/login")
def login_page():
    return send_from_directory("dashboard", "login.html")

@app.route("/admin")
def admin_page():
    return send_from_directory("dashboard", "admin.html")


# ─────────────────────────────────────────────────────────
# Auth — Create / Destroy Session
# ─────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def register():
    """
    Create a new user session.
    Users provide: name, login, password, server
    Connection type: 'cloud' (MetaApi, zero local terminal needed) or 'local' (Desktop MT5)
    """
    data = request.json or {}
    name = data.get("name", "").strip() or f"User-{data.get('login','?')}"

    cfg = copy.deepcopy(BASE_CONFIG)

    # User credentials
    cfg["mt5"]["login"]    = int(data.get("login", 0)) if data.get("login") else 0
    cfg["mt5"]["password"] = data.get("password", "")
    cfg["mt5"]["server"]   = data.get("server", "")

    # Connection type (cloud vs local)
    conn_type = data.get("connection_type", cfg.get("connection_type", "cloud"))
    cfg["connection_type"] = conn_type

    # MetaApi token override if passed
    if "metaapi_token" in data and data["metaapi_token"].strip():
        if "metaapi" not in cfg:
            cfg["metaapi"] = {}
        cfg["metaapi"]["token"] = data["metaapi_token"].strip()

    # Optional strategy overrides
    if "symbol"       in data: cfg["symbol"]             = data["symbol"]
    if "starting_lot" in data: cfg["starting_lot"]       = float(data["starting_lot"])
    if "tp_points"    in data: cfg["take_profit_points"] = int(data["tp_points"])
    if "grid_step"    in data: cfg["grid_step_points"]   = int(data["grid_step"])
    if "max_levels"   in data: cfg["max_grid_levels"]    = int(data["max_levels"])
    if "max_drawdown" in data: cfg["max_drawdown_usd"]   = float(data["max_drawdown"])

    bot_session = session_mgr.create_session(name, cfg)
    session["session_id"] = bot_session.session_id

    return jsonify({
        "status":          "created",
        "session_id":      bot_session.session_id,
        "user_name":       name,
        "connection_type": conn_type
    })

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    sid = get_session_id()
    if sid:
        session_mgr.remove(sid)
    session.clear()
    return jsonify({"status": "logged_out"})

@app.route("/api/auth/me")
def me():
    bs = get_bot_session()
    if not bs:
        return jsonify({"error": "No session"}), 401
    return jsonify(bs.to_summary())


# ─────────────────────────────────────────────────────────
# MT5 Test (before creating session)
# ─────────────────────────────────────────────────────────

@app.route("/api/mt5/test", methods=["POST"])
def test_mt5():
    data = request.json or {}
    cfg  = copy.deepcopy(BASE_CONFIG)
    
    # Try session config first if user is logged in
    bs = get_bot_session()
    if bs and bs.config:
        cfg = copy.deepcopy(bs.config)

    if data.get("login"):
        cfg["mt5"]["login"] = int(data.get("login", 0))
    if data.get("password"):
        cfg["mt5"]["password"] = data.get("password", "")
    if data.get("server"):
        cfg["mt5"]["server"] = data.get("server", "")
    if data.get("path"):
        cfg["mt5"]["path"] = data.get("path", "").strip()

    conn_type = data.get("connection_type", cfg.get("connection_type", "cloud"))
    cfg["connection_type"] = conn_type

    if "metaapi_token" in data and data["metaapi_token"].strip():
        if "metaapi" not in cfg:
            cfg["metaapi"] = {}
        cfg["metaapi"]["token"] = data["metaapi_token"].strip()

    if conn_type == "cloud":
        c = MetaApiConnector(cfg)
    else:
        c = MT5Connector(cfg)

    ok, msg = c.connect()
    if ok:
        info = c.get_account_info()
        sym  = c.get_symbol_info()
        tick = c.get_tick()
        c.disconnect()
        return jsonify({"status": "connected", "message": msg,
                        "account": info, "symbol": sym, "type": conn_type,
                        "price": tick["ask"] if tick else None})
    return jsonify({"status": "error", "message": msg, "type": conn_type}), 400


# ─────────────────────────────────────────────────────────
# Bot State
# ─────────────────────────────────────────────────────────

@app.route("/api/state")
def get_state():
    bs = get_bot_session()
    if not bs or not bs.engine:
        return jsonify({"error": "No active session"})
    return jsonify(bs.engine.get_state())

@app.route("/api/history")
def get_history():
    bs = get_bot_session()
    if not bs or not bs.engine:
        return jsonify([])
    return jsonify(bs.engine.get_history())

@app.route("/api/mt5/account")
def get_mt5_account():
    bs = get_bot_session()
    if bs and bs.connector and bs.connector.connected:
        return jsonify({
            "account":   bs.connector.get_account_info(),
            "symbol":    bs.connector.get_symbol_info(),
            "positions": bs.connector.get_open_positions(),
        })
    return jsonify({"error": "Not connected to MT5"}), 400


# ─────────────────────────────────────────────────────────
# Bot Control
# ─────────────────────────────────────────────────────────

@app.route("/api/start", methods=["POST"])
def start_bot():
    bs = get_bot_session()
    if not bs:
        return jsonify({"status": "error", "message": "No session. Please login first."}), 401

    data     = request.json or {}
    mode     = data.get("mode", "simulator")
    on_event = make_event_handler(bs)

    # Stop existing if running
    if bs.engine and bs.engine.is_running:
        bs.engine.stop()
    if bs.simulator:
        bs.simulator.stop()
        bs.simulator = None
    if bs.connector and bs.connector.connected:
        bs.connector.stop_tick_feed()
        bs.connector.disconnect()
        bs.connector = None

    bs.mode = mode

    # ── SIMULATOR ──
    if mode == "simulator":
        start_price = float(data.get("start_price", 2650.0))
        scenario    = data.get("scenario", "ranging")
        speed_ms    = int(data.get("speed_ms", 400))

        bs.engine    = MartingaleEngine(bs.config, on_event=on_event, connector=None)
        bs.simulator = PriceSimulator(bs.config, on_tick=lambda p: bs.engine.tick(p))
        bs.simulator.set_starting_price(start_price)
        bs.simulator.set_scenario(scenario)
        bs.engine.start(start_price)
        bs.simulator.start(interval_ms=speed_ms)
        bs.started_at = __import__("datetime").datetime.now().strftime("%H:%M:%S")

        return jsonify({"status": "started", "mode": "simulator",
                        "price": start_price, "scenario": scenario})

    # ── LIVE TRADING (Cloud or Local MT5) ──
    elif mode in ("live", "cloud"):
        # Update credentials from request if explicitly supplied
        if data.get("login"):
            bs.config["mt5"]["login"] = int(data["login"])
        if data.get("password"):
            bs.config["mt5"]["password"] = data["password"]
        if data.get("server"):
            bs.config["mt5"]["server"] = data["server"]

        conn_type = bs.config.get("connection_type", "cloud")
        if conn_type == "cloud" or mode == "cloud":
            bs.connector = MetaApiConnector(bs.config)
        else:
            bs.connector = MT5Connector(bs.config)

        ok, msg = bs.connector.connect()
        if not ok:
            bs.connector = None
            return jsonify({"status": "error", "message": msg}), 500

        # Poll real market tick
        tick = None
        for _ in range(5):
            tick = bs.connector.get_tick()
            if tick and tick.get("ask", 0) > 0:
                break
            time.sleep(0.4)

        start_price = tick["ask"] if tick else 0.0
        info = bs.connector.get_account_info()

        bs.engine = MartingaleEngine(bs.config, on_event=on_event, connector=bs.connector)
        bs.connector.on_tick = lambda p: bs.engine.tick(p)
        if start_price > 0:
            bs.engine.start(start_price)
        else:
            bs.engine.is_running = True

        tick_interval = 500 if conn_type == "cloud" else 250
        bs.connector.start_tick_feed(interval_ms=int(data.get("tick_ms", tick_interval)))
        bs.started_at = __import__("datetime").datetime.now().strftime("%H:%M:%S")

        # Notify this user's room
        socketio.emit("mt5_account", info, room=bs.session_id)

        return jsonify({
            "status":          "started",
            "mode":            mode,
            "connection_type": conn_type,
            "price":           start_price,
            "account":         info,
            "message":         msg,
        })

    return jsonify({"status": "error", "message": f"Unknown mode: {mode}"}), 400


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    """
    Stop the bot.
    - If Live MT5: close ALL open positions first, then stop engine.
    - Returns detailed P/L report for each closed trade.
    """
    bs = get_bot_session()
    if not bs:
        return jsonify({"status": "error", "message": "No session"}), 401

    pnl_report = None

    # 1. Close all MT5 positions with P/L
    if bs.connector and bs.connector.connected:
        bs.connector.stop_tick_feed()
        pnl_report = bs.connector.close_all_with_pnl("BotStop")
        # Emit P/L report to user's dashboard
        socketio.emit("positions_closed", pnl_report, room=bs.session_id)

    # 2. Stop engine & simulator
    if bs.engine:
        bs.engine.stop()
    if bs.simulator:
        bs.simulator.stop()
        bs.simulator = None

    socketio.emit("bot_stopped", {"reason": "user_stop", "pnl_report": pnl_report}, room=bs.session_id)
    return jsonify({"status": "stopped", "pnl_report": pnl_report})


@app.route("/api/reset", methods=["POST"])
def reset_bot():
    """Stop bot, close all positions, disconnect MT5, clear history."""
    bs = get_bot_session()
    if not bs:
        return jsonify({"status": "error", "message": "No session"}), 401

    pnl_report = None

    if bs.connector and bs.connector.connected:
        bs.connector.stop_tick_feed()
        pnl_report = bs.connector.close_all_with_pnl("BotReset")
        bs.connector.disconnect()
        bs.connector = None

    if bs.engine:
        bs.engine.stop()
        bs.engine.reset()
    if bs.simulator:
        bs.simulator.stop()
        bs.simulator = None

    socketio.emit("bot_reset", {"pnl_report": pnl_report}, room=bs.session_id)
    return jsonify({"status": "reset", "pnl_report": pnl_report})


@app.route("/api/emergency_close", methods=["POST"])
def emergency_close():
    """
    EMERGENCY: immediately close ALL open positions.
    Returns detailed P/L for each closed trade.
    """
    bs = get_bot_session()
    if not bs:
        return jsonify({"status": "error", "message": "No session"}), 401

    pnl_report = None
    if bs.connector and bs.connector.connected:
        bs.connector.stop_tick_feed()
        pnl_report = bs.connector.close_all_with_pnl("EmergencyClose")
        socketio.emit("positions_closed", pnl_report, room=bs.session_id)

    # Stop bot & clear grid
    if bs.engine:
        bs.engine.stop()
    if bs.simulator:
        bs.simulator.stop()
        bs.simulator = None

    socketio.emit("bot_stopped", {"reason": "emergency", "pnl_report": pnl_report}, room=bs.session_id)
    return jsonify({"status": "closed", "pnl_report": pnl_report or {}})


# ─────────────────────────────────────────────────────────
# Admin API — view all sessions
# ─────────────────────────────────────────────────────────

@app.route("/api/admin/sessions")
def admin_sessions():
    return jsonify({
        "total":   len(session_mgr._sessions),
        "active":  session_mgr.active_count(),
        "sessions": session_mgr.all_sessions(),
    })

@app.route("/api/admin/kick/<sid>", methods=["POST"])
def admin_kick(sid):
    session_mgr.remove(sid)
    socketio.emit("kicked", {"reason": "Admin stopped your session"}, room=sid)
    return jsonify({"status": "kicked", "session_id": sid})


# ─────────────────────────────────────────────────────────
# Socket.IO
# ─────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    sid_cookie = request.cookies.get("session") or ""
    # Try to find session from Flask cookie (parsed by flask session)
    bot_session = get_bot_session()
    if bot_session:
        join_room(bot_session.session_id)
        if bot_session.engine:
            emit("state_sync", bot_session.engine.get_state())
        if bot_session.connector and bot_session.connector.connected:
            info = bot_session.connector.get_account_info()
            if info:
                emit("mt5_account", info)

@socketio.on("join_session")
def on_join_session(data):
    """Client sends their session_id to join the correct room."""
    sid = data.get("session_id", "")
    bs  = session_mgr.get(sid)
    if bs:
        join_room(sid)
        emit("joined", {"session_id": sid, "user_name": bs.user_name})
        if bs.engine:
            emit("state_sync", bs.engine.get_state())

@socketio.on("disconnect")
def on_disconnect():
    pass


# ─────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("HOST", BASE_CONFIG.get("dashboard", {}).get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT", BASE_CONFIG.get("dashboard", {}).get("port", 5000)))
    print(f"[ForexBot] Multi-User Server starting...")
    print(f"[ForexBot] Dashboard : http://{host}:{port}")
    print(f"[ForexBot] Admin     : http://{host}:{port}/admin")
    print(f"[ForexBot] Symbol    : {BASE_CONFIG.get('symbol', 'XAUUSD')}")
    socketio.run(app, host=host, port=port, debug=False,
                 allow_unsafe_werkzeug=True)
