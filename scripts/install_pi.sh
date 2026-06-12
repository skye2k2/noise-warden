#!/usr/bin/env bash
# First-time installation of noise-warden on a Raspberry Pi.
#
# This script can be run from ANYWHERE — a Desktop clone, a USB stick, a home
# directory checkout, etc. It copies the project into /opt/noise-warden/<version>/
# so the systemd service user can always reach the files, regardless of where
# the source directory lives or who owns it.
#
# Usage:
#   cd /path/to/noise-warden-v12   # wherever you extracted the archive
#   bash scripts/install_pi.sh
#
# What it does:
#   1. Creates /opt/noise-warden/ base structure + shared data directories
#   2. Copies the project into /opt/noise-warden/<version-dir-name>/
#   3. Points the "current" symlink at the copied version
#   4. Creates a Python venv and installs pip dependencies
#   5. Creates the noisewarden system user (audio + gpio groups)
#   6. Installs the systemd service unit
#   7. Runs a pre-flight check to verify the service can start

set -euo pipefail

BASE=/opt/noise-warden
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_NAME="$(basename "$SOURCE_DIR")"
DEST_DIR="$BASE/$VERSION_NAME"
CURRENT="$BASE/current"
VENV="$BASE/venv"
SHARED="$BASE/shared"

normalize_permissions() {
    sudo chmod -R g+rwX "$BASE"
    sudo find "$BASE" -type d -exec chmod g+s {} +
}

echo ""
echo "=== noise-warden install ==="
echo "  Source:      $SOURCE_DIR"
echo "  Destination: $DEST_DIR"
echo "  Symlink:     $CURRENT -> $DEST_DIR"
echo ""

# --- Create base directory structure ---
sudo mkdir -p "$BASE" "$SHARED" "$SHARED/snippets" "$SHARED/playlist" "$SHARED/build"
# --- Set ownership for the service user ---
sudo chown -R noisewarden:noisewarden "$BASE"
normalize_permissions

# --- Add the installing user to the noisewarden group ---
# Allows SSH/VS Code file browsing without sudo. Only needed on the Pi —
# the service creates WAV snippets owned by noisewarden:noisewarden.
if [ "$USER" != "noisewarden" ]; then
    sudo usermod -a -G noisewarden "$USER" 2>/dev/null || true
    echo "  Added $USER to noisewarden group (re-login for effect)."
fi

# --- Copy project files into /opt (skip .venv, .git, __pycache__, .pytest_cache) ---
# If the source IS already inside /opt/noise-warden, skip the copy
if [ "$SOURCE_DIR" != "$DEST_DIR" ]; then
    echo "Copying project files to $DEST_DIR ..."
    # Remove stale version dir if it exists (clean re-install)
    if [ -d "$DEST_DIR" ]; then
        echo "  (removing existing $DEST_DIR)"
        sudo rm -rf "$DEST_DIR"
    fi
    sudo mkdir -p "$DEST_DIR"
    sudo rsync -a \
        --exclude '.venv' \
        --exclude '.git' \
        --exclude '__pycache__' \
        --exclude '.pytest_cache' \
        --exclude 'node_modules' \
        --exclude '.TMP_*' \
        "$SOURCE_DIR/" "$DEST_DIR/"
    echo "  Done."
else
    echo "Source is already at $DEST_DIR — skipping copy."
fi

# --- Copy deploy script to base (outside any version) ---
sudo cp "$DEST_DIR/scripts/deploy_noise_warden.sh" "$BASE/deploy_noise_warden.sh"
sudo chmod +x "$BASE/deploy_noise_warden.sh"

# --- Point "current" symlink at the copied version ---
sudo ln -sfn "$DEST_DIR" "$CURRENT"

# --- Create venv and install dependencies ---
echo "Setting up Python venv at $VENV ..."
sudo python3 -m venv "$VENV"
sudo "$VENV/bin/pip" install --upgrade pip
sudo "$VENV/bin/pip" install -r "$CURRENT/requirements.txt"
echo "  Dependencies installed."

# --- Ensure adequate swap space ---
# 200 MB default swap on Pi OS is too small for 8 GB RAM — memory pressure
# escalates from "fine" to OOM-kill with almost no buffer. 1 GB is enough to
# absorb transient spikes without meaningful SD card wear at noise-warden's
# write volume.
MIN_SWAP_MB=1024
SWAP_FILE="/var/swap"
CURRENT_SWAP_MB=0
if [ -f "$SWAP_FILE" ]; then
    CURRENT_SWAP_MB=$(( $(stat -f%z "$SWAP_FILE" 2>/dev/null || stat -c%s "$SWAP_FILE" 2>/dev/null || echo 0) / 1024 / 1024 ))
fi
if [ "$CURRENT_SWAP_MB" -lt "$MIN_SWAP_MB" ]; then
    echo "Swap is ${CURRENT_SWAP_MB} MB (need ${MIN_SWAP_MB} MB). Resizing ..."
    sudo swapoff "$SWAP_FILE" 2>/dev/null || true
    sudo dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$MIN_SWAP_MB" status=progress
    sudo chmod 600 "$SWAP_FILE"
    sudo mkswap "$SWAP_FILE"
    sudo swapon "$SWAP_FILE"
    echo "  Swap resized to ${MIN_SWAP_MB} MB."
else
    echo "Swap is already ${CURRENT_SWAP_MB} MB (>= ${MIN_SWAP_MB} MB) — skipping."
fi

# --- Create system user ---
sudo useradd -r -s /usr/sbin/nologin noisewarden 2>/dev/null || true
# Audio group is required for microphone access; warn loudly if it fails
if sudo usermod -a -G audio noisewarden 2>/dev/null; then
    echo "  Added noisewarden to audio group."
else
    echo "  WARNING: Could not add noisewarden to audio group."
    echo "           The service will not be able to access the microphone."
    echo "           Fix manually: sudo usermod -a -G audio noisewarden"
fi
# gpio group only exists on Raspberry Pi OS
if getent group gpio >/dev/null 2>&1; then
    sudo usermod -a -G gpio noisewarden 2>/dev/null || true
fi

# --- Set ownership for the service user ---
sudo chown -R noisewarden:noisewarden "$BASE"
normalize_permissions

# --- Generate self-signed TLS certificate (required for Service Worker) ---
# Service Workers only register on secure contexts (HTTPS or localhost).
# Without TLS, the browser silently refuses to register the SW, and all
# offline caching (page navigation, snippet pre-loading) is disabled.
# The cert is self-signed — users accept the browser warning once per device.
TLS_DIR="$BASE/tls"
if [ ! -f "$TLS_DIR/cert.pem" ]; then
    echo "Generating self-signed TLS certificate ..."
    sudo mkdir -p "$TLS_DIR"
    HOSTNAME_VAL=$(hostname -f 2>/dev/null || hostname)
    sudo openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$TLS_DIR/key.pem" \
        -out "$TLS_DIR/cert.pem" \
        -days 3650 \
        -subj "/CN=$HOSTNAME_VAL" 2>/dev/null
    sudo chown -R noisewarden:noisewarden "$TLS_DIR"
    echo "  TLS certificate generated at $TLS_DIR/"
    echo "  On first visit, accept the browser's certificate warning."
else
    echo "TLS certificate already exists at $TLS_DIR/ — skipping generation."
fi

# --- Install systemd service ---
sudo cp "$CURRENT/deploy/noise-warden.service" /etc/systemd/system/noise-warden.service
sudo systemctl daemon-reload

# --- Pre-flight validation ---
echo ""
echo "=== Pre-flight checks ==="

ERRORS=0

# Check WorkingDirectory exists and is accessible
if [ ! -d "$CURRENT" ]; then
    echo "  FAIL: $CURRENT does not exist"
    ERRORS=$((ERRORS + 1))
else
    echo "  OK: WorkingDirectory $CURRENT exists"
fi

# Check the symlink resolves to a real directory
RESOLVED="$(readlink -f "$CURRENT" 2>/dev/null || echo "")"
if [ -z "$RESOLVED" ] || [ ! -d "$RESOLVED" ]; then
    echo "  FAIL: $CURRENT -> symlink does not resolve to a directory"
    ERRORS=$((ERRORS + 1))
else
    echo "  OK: Symlink resolves to $RESOLVED"
fi

# Check noisewarden can read the working directory
if sudo -u noisewarden test -r "$RESOLVED/noise_warden/main.py" 2>/dev/null; then
    echo "  OK: noisewarden user can read project files"
else
    echo "  FAIL: noisewarden user cannot read $RESOLVED/noise_warden/main.py"
    ERRORS=$((ERRORS + 1))
fi

# Check uvicorn is installed in the venv
if [ -x "$VENV/bin/uvicorn" ]; then
    echo "  OK: uvicorn found at $VENV/bin/uvicorn"
else
    echo "  FAIL: uvicorn not found at $VENV/bin/uvicorn"
    ERRORS=$((ERRORS + 1))
fi

# Check config file exists
if [ -f "$CURRENT/config/noise_warden.yaml" ]; then
    echo "  OK: Config file found"
else
    echo "  WARN: No config file at $CURRENT/config/noise_warden.yaml"
fi

# Check noisewarden is in the audio group
if id -nG noisewarden 2>/dev/null | grep -qw audio; then
    echo "  OK: noisewarden user is in the audio group"
else
    echo "  FAIL: noisewarden user is NOT in the audio group"
    echo "        Fix: sudo usermod -a -G audio noisewarden"
    ERRORS=$((ERRORS + 1))
fi

# Check for audio capture devices (warn-only — mic may not be plugged in yet)
WARNINGS=0
if command -v arecord >/dev/null 2>&1; then
    if arecord -l 2>/dev/null | grep -q "card"; then
        echo "  OK: Audio capture device detected"
    else
        echo "  WARN: No audio capture device found — plug in your USB microphone"
        echo "        before starting the service. Verify with: arecord -l"
        WARNINGS=$((WARNINGS + 1))
    fi
else
    echo "  WARN: arecord not found — cannot verify audio devices"
    echo "        Install with: sudo apt install -y alsa-utils"
    WARNINGS=$((WARNINGS + 1))
fi

echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo "=== $ERRORS pre-flight check(s) FAILED ==="
    echo "Fix the issues above before starting the service."
    exit 1
fi

if [ "$WARNINGS" -gt 0 ]; then
    echo "=== Install complete with $WARNINGS warning(s): $VERSION_NAME ==="
else
    echo "=== Install complete: $VERSION_NAME ==="
fi
echo ""
echo "Next steps:"
echo "  1. Plug in your USB microphone (if not already connected)"
echo "  2. Verify it's detected:  arecord -l"
echo "  3. Edit config:  sudo nano $CURRENT/config/noise_warden.yaml"
echo "  4. Start:        sudo systemctl enable --now noise-warden"
echo "  5. Check:        sudo systemctl status noise-warden"
echo "  6. Open:         https://<pi-ip>:8787/"
echo "     (Accept the self-signed certificate warning on first visit)"
