#!/usr/bin/env bash
# Deploy a new version of noise-warden by swinging the "current" symlink.
#
# This script lives at /opt/noise-warden/deploy_noise_warden.sh (outside any
# version directory) and is copied there during first-time install_pi.sh.
#
# Usage:
#   cd /opt/noise-warden
#   ./deploy_noise_warden.sh noise-warden-v12
#
# The target directory must already exist inside /opt/noise-warden/.
# If upgrading from a source outside /opt (e.g., ~/Desktop), use install_pi.sh
# instead — it handles the copy step.

set -euo pipefail
BASE=/opt/noise-warden
TARGET="${1:-}"

normalize_permissions() {
    sudo chmod -R g+rwX "$BASE"
    sudo find "$BASE" -type d -exec chmod g+s {} +
}

if [ -z "$TARGET" ]; then
    echo "Usage: $0 <version-dir-name>"
    echo ""
    echo "Available versions:"
    # List directories that look like version dirs (contain noise_warden/)
    for d in "$BASE"/*/; do
        dirname="$(basename "$d")"
        if [ -d "$d/noise_warden" ]; then
            if [ "$(readlink -f "$BASE/current")" = "$(readlink -f "$d")" ]; then
                echo "  $dirname  (current)"
            else
                echo "  $dirname"
            fi
        fi
    done
    exit 1
fi

if [ ! -d "$BASE/$TARGET" ]; then
    echo "ERROR: Missing directory: $BASE/$TARGET"
    echo ""
    echo "If the version is on your Desktop or home directory, use install_pi.sh"
    echo "to copy it into /opt/noise-warden/ first:"
    echo "  cd ~/path/to/$TARGET && bash scripts/install_pi.sh"
    exit 1
fi

if [ ! -f "$BASE/$TARGET/noise_warden/main.py" ]; then
    echo "ERROR: $BASE/$TARGET doesn't look like a noise-warden project"
    echo "       (missing noise_warden/main.py)"
    exit 1
fi

echo "=== Deploying $TARGET ==="

sudo systemctl stop noise-warden || true
sudo ln -sfn "$BASE/$TARGET" "$BASE/current"

echo "Updating Python venv ..."
if [ -d "$BASE/venv" ]; then
    sudo rm -rf "$BASE/venv"
fi
sudo python3 -m venv "$BASE/venv"
sudo "$BASE/venv/bin/pip" install --upgrade pip
sudo "$BASE/venv/bin/pip" install -r "$BASE/current/requirements.txt"

# Update service file in case it changed
sudo cp "$BASE/current/deploy/noise-warden.service" /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload

# Ensure ownership is correct after any file changes
sudo chown -R noisewarden:noisewarden "$BASE"
normalize_permissions

echo "Starting service ..."
sudo systemctl start noise-warden

# Give it a moment to either start or fail
sleep 2

echo ""
sudo systemctl status noise-warden --no-pager || true

echo ""
echo "=== Deploy complete: $TARGET ==="
