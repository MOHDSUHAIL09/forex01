#!/usr/bin/env bash
# ForexBot Automated 1-Click Server Deployment Script (Ubuntu/Debian)

set -e

echo "=== [1/4] Updating system packages ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl ufw

echo "=== [2/4] Setting up Python virtual environment ==="
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== [3/4] Configuring Firewall (Port 5000) ==="
sudo ufw allow 5000/tcp || true

echo "=== [4/4] Setting up systemd background service ==="
CURRENT_DIR=$(pwd)
SERVICE_FILE="/etc/systemd/system/forexbot.service"

sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=ForexBot Automated Multi-User Trading Server
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$CURRENT_DIR
Environment="PATH=$CURRENT_DIR/venv/bin"
Environment="PORT=5000"
Environment="HOST=0.0.0.0"
ExecStart=$CURRENT_DIR/venv/bin/python server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable forexbot
sudo systemctl restart forexbot

echo ""
echo "==========================================================="
echo "✅ ForexBot is LIVE in background on your server!"
echo "Status check: sudo systemctl status forexbot"
echo "Logs view   : sudo journalctl -u forexbot -f"
echo "URL         : http://$(curl -s ifconfig.me):5000"
echo "==========================================================="
