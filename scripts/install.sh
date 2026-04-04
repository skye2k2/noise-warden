#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/noise-warden-v2"
sudo mkdir -p "$APP_DIR"
sudo rsync -a ./ "$APP_DIR"/
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
if [ ! -f config.yaml ]; then cp config.example.yaml config.yaml; fi
sudo cp deploy/noise-warden.service /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload
echo "Install complete. Run: sudo systemctl enable --now noise-warden"
