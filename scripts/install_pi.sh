#!/usr/bin/env bash
set -euo pipefail

BASE=/opt/noise-warden
VERSION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_NAME="$(basename "$VERSION_DIR")"
CURRENT="$BASE/current"
VENV="$BASE/venv"
SHARED="$BASE/shared"

sudo mkdir -p "$BASE" "$SHARED" "$SHARED/snippets" "$SHARED/playlist" "$SHARED/build"
sudo chown -R "$USER:$USER" "$BASE"

if [ ! -f "$BASE/deploy_noise_warden.sh" ]; then
  cp "$VERSION_DIR/scripts/deploy_noise_warden.sh" "$BASE/deploy_noise_warden.sh"
  chmod +x "$BASE/deploy_noise_warden.sh"
fi

ln -sfn "$VERSION_DIR" "$CURRENT"

python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip
pip install -r "$CURRENT/requirements.txt"

sudo useradd -r -s /usr/sbin/nologin noisewarden 2>/dev/null || true
sudo usermod -a -G audio,gpio noisewarden 2>/dev/null || true
sudo chown -R noisewarden:noisewarden "$BASE"

sudo cp "$CURRENT/deploy/noise-warden.service" /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload

echo "Installed $VERSION_NAME at $BASE"
echo "Start with: sudo systemctl enable --now noise-warden"
