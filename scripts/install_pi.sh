#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
sudo apt update
sudo apt install -y python3-venv python3-pip portaudio19-dev libsndfile1 ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
mkdir -p data/snippets data/uploads media/playlist logs
cp deploy/noise-warden.service /tmp/noise-warden.service
sudo sed "s|__ROOT__|$ROOT|g" /tmp/noise-warden.service > /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload
echo "Run: sudo systemctl enable --now noise-warden"
