# ForexBot — Martingale Trading Bot

A professional Python-based Forex trading bot implementing the **Martingale strategy** for XAUUSD (Gold).

---

## 📋 Strategy Rules (from original notes)

| Parameter | Value |
|-----------|-------|
| Starting Lot | 0.01 |
| Take Profit | +7 points from 1st entry |
| Grid Step | 6 points adverse move → open next level |
| Lot Multiplier | × 2 on each level |
| On Grid Win | Reset all → restart at 0.01 |
| Max Levels | 8 (safety cap) |
| Max Drawdown | $500 (safety stop) |

**Lot Progression:**
```
Level 1: 0.01
Level 2: 0.02
Level 3: 0.04
Level 4: 0.08
Level 5: 0.16
Level 6: 0.32
Level 7: 0.64
Level 8: 1.28
```

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
cd d:\AntiGravity\ForexBot
pip install -r requirements.txt
```

### 2. Run the bot (Simulator mode — no real money)
```bash
python server.py
```

### 3. Open Dashboard
```
http://localhost:5000
```

---

## 📁 Project Structure

```
ForexBot/
├── bot/
│   ├── engine.py          # Core martingale logic
│   ├── mt5_connector.py   # MetaTrader5 API
│   ├── simulator.py       # Paper trading simulator
│   └── __init__.py
├── dashboard/
│   ├── index.html         # Web dashboard
│   ├── style.css          # Dark premium UI
│   └── app.js             # Real-time charts & controls
├── server.py              # Flask + WebSocket server
├── config.json            # Bot configuration
├── requirements.txt
└── README.md
```

---

## ⚙️ Configuration (`config.json`)

```json
{
  "symbol": "XAUUSD",
  "starting_lot": 0.01,
  "take_profit_points": 7,
  "grid_step_points": 6,
  "max_grid_levels": 8,
  "max_drawdown_usd": 500,
  "trade_direction": "BUY"
}
```

---

## 🔴 Live MT5 Trading

1. Open MetaTrader 5 terminal
2. Edit `config.json`:
   ```json
   "mt5": {
     "login": YOUR_ACCOUNT_NUMBER,
     "password": "YOUR_PASSWORD",
     "server": "YOUR_BROKER_SERVER"
   }
   ```
3. In dashboard, select **Live MT5** mode → Start

---

## ⚠️ Risk Warning

Martingale strategy carries **high risk**. Each level doubles the position size. A long losing streak can cause significant losses. Always test in **simulator mode** first. Never risk money you cannot afford to lose.
