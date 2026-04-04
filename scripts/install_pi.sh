#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="/opt/noise-warden"
VERSION_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$BASE_DIR/venv"

sudo mkdir -p "$BASE_DIR" "$BASE_DIR/shared" "$BASE_DIR/shared/snippets" "$BASE_DIR/shared/build" "$BASE_DIR/shared/playlist"
sudo cp "$VERSION_DIR/scripts/deploy_noise_warden.sh" "$BASE_DIR/deploy_noise_warden.sh"
sudo chmod +x "$BASE_DIR/deploy_noise_warden.sh"

if [[ "$VERSION_DIR" != "$BASE_DIR/"* ]]; then
  echo "ERROR: extract this package under $BASE_DIR"; exit 1
fi

sudo ln -sfn "$VERSION_DIR" "$BASE_DIR/current"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$VERSION_DIR/requirements.txt"

sudo cp "$VERSION_DIR/services/noise-warden.service" /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload

[[ -f "$BASE_DIR/shared/build/build_notes.txt" ]] || echo "" | sudo tee "$BASE_DIR/shared/build/build_notes.txt" >/dev/null
[[ -f "$BASE_DIR/shared/build/ordinance_excerpt.txt" ]] || echo "" | sudo tee "$BASE_DIR/shared/build/ordinance_excerpt.txt" >/dev/null

echo "Done. Start with: sudo systemctl enable --now noise-warden"
