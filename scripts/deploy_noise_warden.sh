#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="/opt/noise-warden"
TARGET_VERSION="${1:-}"
VENV_DIR="$BASE_DIR/venv"

if [[ -z "$TARGET_VERSION" ]]; then
  echo "Usage: $0 noise-warden-vX_Y"; exit 1
fi

TARGET_DIR="$BASE_DIR/$TARGET_VERSION"
if [[ ! -d "$TARGET_DIR" ]]; then
  echo "Target version directory not found: $TARGET_DIR"; exit 1
fi

sudo systemctl stop noise-warden || true
sudo ln -sfn "$TARGET_DIR" "$BASE_DIR/current"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$TARGET_DIR/requirements.txt"

sudo cp "$TARGET_DIR/services/noise-warden.service" /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload
sudo cp "$TARGET_DIR/scripts/deploy_noise_warden.sh" "$BASE_DIR/deploy_noise_warden.sh"
sudo chmod +x "$BASE_DIR/deploy_noise_warden.sh"

sudo systemctl enable --now noise-warden
sudo systemctl --no-pager --full status noise-warden || true

echo "Upgrade complete. Active: $TARGET_DIR"
