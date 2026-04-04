#!/usr/bin/env bash
set -euo pipefail
BASE=/opt/noise-warden
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "Usage: $0 <version-dir-name>"
  exit 1
fi

if [ ! -d "$BASE/$TARGET" ]; then
  echo "Missing directory: $BASE/$TARGET"
  exit 1
fi

sudo systemctl stop noise-warden || true
ln -sfn "$BASE/$TARGET" "$BASE/current"

python3 -m venv "$BASE/venv"
source "$BASE/venv/bin/activate"
pip install --upgrade pip
pip install -r "$BASE/current/requirements.txt"

sudo cp "$BASE/current/deploy/noise-warden.service" /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload
sudo systemctl start noise-warden
sudo systemctl status noise-warden --no-pager || true
