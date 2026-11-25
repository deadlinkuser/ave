#!/usr/bin/env bash
# [NOTE] Interactive installer for AVE (Automatic Video Encoder) supporting Podman and Docker.

set -euo pipefail

echo "=========================================="
echo "  AVE — Automatic Video Encoder Installer "
echo "=========================================="
echo ""

# 1. Detect container runtime (prefer Podman, fallback to Docker)
RUNTIME=""
if command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
elif command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
else
    echo "[ERROR] Neither Podman nor Docker was found."
    echo "Install Podman (https://podman.io) or Docker first, then re-run this script."
    exit 1
fi
echo "[AVE] Detected container runtime: $RUNTIME"

# 2. Prompt for host data directory
DEFAULT_DATA_DIR="$HOME/containers/ave"
read -r -p "Host data path [$DEFAULT_DATA_DIR]: " INPUT_DATA_DIR
DATA_DIR="${INPUT_DATA_DIR:-$DEFAULT_DATA_DIR}"
DATA_DIR="${DATA_DIR/#\~/$HOME}"

mkdir -p "$DATA_DIR"/{watch,output,config}
echo "[AVE] Using data directory: $DATA_DIR"

# 3. Prompt for Web GUI port
read -r -p "Web GUI port: " INPUT_PORT
WEB_PORT="${INPUT_PORT:-8488}"

# 4. Prompt for local-only vs. LAN access with optional login password
read -r -p "Allow access from other devices on your LAN? (y/N): " ALLOW_LAN
BIND_IP="127.0.0.1"
AUTH_ARGS=()

if [[ "${ALLOW_LAN,,}" == "y" || "${ALLOW_LAN,,}" == "yes" ]]; then
    BIND_IP="0.0.0.0"
    read -r -p "Set a Web GUI username [ave]: " INPUT_USER
    WEB_USER="${INPUT_USER:-ave}"
    read -r -s -p "Set a Web GUI password (leave blank for no password): " INPUT_PASS
    echo ""
    if [[ -n "$INPUT_PASS" ]]; then
        AUTH_ARGS=(-e "AVE_WEB_USER=$WEB_USER" -e "AVE_WEB_PASSWORD=$INPUT_PASS")
        echo "[AVE] LAN session login enabled for user '$WEB_USER'."
    else
        echo "[AVE] Warning: LAN access enabled without a password."
    fi
fi

# 5. Calculate safe /dev/shm size (50% of host RAM, minimum 2g)
SHM_GB="4g"
if [[ -r /proc/meminfo ]]; then
    SHM_GB=$(awk '/MemTotal/ {g=int($2/1024/1024/2); print (g>0?g:2)"g"}' /proc/meminfo)
elif command -v sysctl >/dev/null 2>&1; then
    MEM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
    if [[ "$MEM_BYTES" -gt 0 ]]; then
        G=$((MEM_BYTES / 1024 / 1024 / 1024 / 2))
        SHM_GB="$((G > 0 ? G : 2))g"
    fi
fi

# 6. Build image from the script's repository directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[AVE] Building container image (this may take a minute on first run)..."
"$RUNTIME" build -t localhost/ave:latest -f "$SCRIPT_DIR/Containerfile" "$SCRIPT_DIR"

# 7. Replace any existing 'ave' container cleanly
"$RUNTIME" stop ave >/dev/null 2>&1 || true
"$RUNTIME" rm ave >/dev/null 2>&1 || true

# 8. Volume mount suffix (:Z for Podman SELinux/rootless, omitted on Docker Desktop)
VOL_OPT="$DATA_DIR:/data"
if [[ "$RUNTIME" == "podman" ]]; then
    VOL_OPT="$DATA_DIR:/data:Z"
fi

echo "[AVE] Starting container..."
"$RUNTIME" run -d --name ave \
    --restart unless-stopped \
    -p "$BIND_IP:$WEB_PORT:8488" \
    -v "$VOL_OPT" \
    --shm-size="$SHM_GB" \
    "${AUTH_ARGS[@]}" \
    localhost/ave:latest >/dev/null

# 9. Optional Systemd Quadlet setup on Linux when using Podman
if [[ "$RUNTIME" == "podman" && -d "$HOME/.config" && "$(uname -s)" == "Linux" ]]; then
    read -r -p "Enable automatic startup on boot via systemd user service? (y/N): " ENABLE_BOOT
    if [[ "${ENABLE_BOOT,,}" == "y" || "${ENABLE_BOOT,,}" == "yes" ]]; then
        QUADLET_DIR="$HOME/.config/containers/systemd"
        mkdir -p "$QUADLET_DIR"
        cat << QEOF > "$QUADLET_DIR/ave.container"
[Unit]
Description=AVE — Automatic Video Encoder
After=network-online.target

[Container]
Image=localhost/ave:latest
ContainerName=ave
AutoUpdate=registry
PublishPort=$BIND_IP:$WEB_PORT:8488
Volume=$DATA_DIR:/data:Z
ShmSize=$SHM_GB
MemoryHigh=75%
MemoryMax=85%

[Service]
Restart=always
TimeoutStartSec=120

[Install]
WantedBy=default.target
QEOF
        "$RUNTIME" stop ave >/dev/null 2>&1 || true
        "$RUNTIME" rm ave >/dev/null 2>&1 || true
        systemctl --user daemon-reload
        systemctl --user enable --now ave.service
        loginctl enable-linger "$USER" 2>/dev/null || true
        echo "[AVE] Installed and started systemd user service (ave.service)."
    fi
fi

echo ""
echo "=========================================="
echo "  AVE is ready!                           "
echo "  Open: http://127.0.0.1:$WEB_PORT        "
echo "  Drop videos into: $DATA_DIR/watch       "
echo "=========================================="
