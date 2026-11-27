#!/usr/bin/env bash
# [NOTE] Interactive lifecycle manager for AVE (--install, --upgrade, --backup, --uninstall).

set -euo pipefail

ACTION="${1:---install}"
DATA_DIR="${AVE_DATA_DIR:-$HOME/containers/ave}"
BACKUP_DIR="${AVE_BACKUP_DIR:-$HOME/backups/ave}"
QUADLET_FILE="$HOME/.config/containers/systemd/ave.container"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"

detect_runtime() {
    if command -v podman >/dev/null 2>&1; then
        echo "podman"
    elif command -v docker >/dev/null 2>&1; then
        echo "docker"
    else
        echo "[ERROR] Neither Podman nor Docker was found."
        echo "Install Podman (https://podman.io) or Docker first."
        exit 1
    fi
}

backup_config() {
    if [[ ! -d "$DATA_DIR/config" ]]; then
        echo "[AVE] No config directory found at $DATA_DIR/config — nothing to back up."
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    local stamp archive
    stamp="$(date +%Y-%m-%d_%H%M%S)"
    archive="$BACKUP_DIR/ave-config-$stamp.tar.gz"
    tar -czf "$archive" -C "$DATA_DIR" config
    echo "[AVE] Saved lightweight config backup to: $archive"
}

calc_shm() {
    if [[ -r /proc/meminfo ]]; then
        awk '/MemTotal/ {g=int($2/1024/1024/2); print (g>0?g:2)"g"}' /proc/meminfo
    else
        echo "4g"
    fi
}

case "$ACTION" in
    --backup)
        backup_config
        exit 0
        ;;

    --uninstall)
        RUNTIME="$(detect_runtime)"
        echo "=========================================="
        echo "  AVE — Uninstall Utility                 "
        echo "=========================================="
        read -r -p "Create a final config backup in $BACKUP_DIR before removing? (Y/n): " DO_BAK
        if [[ "${DO_BAK,,}" != "n" && "${DO_BAK,,}" != "no" ]]; then
            backup_config
        fi
        if [[ -f "$QUADLET_FILE" ]]; then
            systemctl --user stop ave.service 2>/dev/null || true
            systemctl --user disable ave.service 2>/dev/null || true
            rm -f "$QUADLET_FILE"
            systemctl --user daemon-reload 2>/dev/null || true
        fi
        "$RUNTIME" stop ave >/dev/null 2>&1 || true
        "$RUNTIME" rm ave >/dev/null 2>&1 || true
        "$RUNTIME" rmi localhost/ave:latest >/dev/null 2>&1 || true
        echo "[AVE] Container and service removed."
        echo "[AVE] Your media and config folders at $DATA_DIR were left untouched."
        exit 0
        ;;

    --upgrade)
        RUNTIME="$(detect_runtime)"
        echo "=========================================="
        echo "  AVE — Upgrade Utility                   "
        echo "=========================================="
        backup_config
        if [[ -d "$SCRIPT_DIR/.git" ]]; then
            echo "[AVE] Pulling latest code from Git..."
            git -C "$SCRIPT_DIR" pull --ff-only || true
        fi
        echo "[AVE] Rebuilding AVE container image..."
        "$RUNTIME" build -t localhost/ave:latest -f "$SCRIPT_DIR/Containerfile" "$SCRIPT_DIR"
        if [[ -f "$QUADLET_FILE" ]]; then
            systemctl --user restart ave.service
            echo "[AVE] Restarted systemd Quadlet service (ave.service)."
        else
            echo "[AVE] Image rebuilt. Re-run ./install.sh to recreate the container with your preferred options."
        fi
        exit 0
        ;;

    --install|*)
        RUNTIME="$(detect_runtime)"
        echo "=========================================="
        echo "  AVE — Automatic Video Encoder Installer "
        echo "=========================================="
        echo "[AVE] Runtime detected: $RUNTIME"

        read -r -p "Host data path [$DATA_DIR]: " INPUT_DATA_DIR
        DATA_DIR="${INPUT_DATA_DIR:-$DATA_DIR}"
        DATA_DIR="${DATA_DIR/#\~/$HOME}"
        mkdir -p "$DATA_DIR"/{watch,output,config}

        WEB_PORT="8488"
        read -r -p "Web GUI port [$WEB_PORT]: " INPUT_PORT
        WEB_PORT="${INPUT_PORT:-8488}"

        read -r -p "Allow access from other devices on your LAN? (y/N): " ALLOW_LAN
        BIND_IP="127.0.0.1"
        WEB_USER="ave"
        WEB_PASS=""
        if [[ "${ALLOW_LAN,,}" == "y" || "${ALLOW_LAN,,}" == "yes" ]]; then
            BIND_IP="0.0.0.0"
            read -r -p "Set a Web GUI username [ave]: " INPUT_USER
            WEB_USER="${INPUT_USER:-ave}"
            read -r -s -p "Set a Web GUI password (leave blank for none): " WEB_PASS
            echo ""
        fi

        GPU_ENABLED="false"
        if [[ -e /dev/dri/renderD128 ]]; then
            CPU_INFO="$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null || echo 'Unknown CPU')"
            echo ""
            echo "[AVE] GPU render device detected (/dev/dri/renderD128)."
            echo "[AVE] Detected CPU: ${CPU_INFO#*: }"
            echo "[AVE] Note: Intel 8th-Gen+ is recommended for 10-bit HEVC VAAPI; AMD quality varies."
            read -r -p "Enable optional GPU hardware acceleration pass-through? (y/N): " ENABLE_GPU
            if [[ "${ENABLE_GPU,,}" == "y" || "${ENABLE_GPU,,}" == "yes" ]]; then
                GPU_ENABLED="true"
            fi
        fi

        SHM_GB="$(calc_shm)"
        echo "[AVE] Building container image..."
        "$RUNTIME" build -t localhost/ave:latest -f "$SCRIPT_DIR/Containerfile" "$SCRIPT_DIR"

        "$RUNTIME" stop ave >/dev/null 2>&1 || true
        "$RUNTIME" rm ave >/dev/null 2>&1 || true

        if [[ "$RUNTIME" == "podman" && "$(uname -s)" == "Linux" ]]; then
            read -r -p "Start AVE automatically on system boot? (Y/n): " AUTO_BOOT
            mkdir -p "$(dirname "$QUADLET_FILE")"
            {
                echo "[Unit]"
                echo "Description=AVE — Automatic Video Encoder"
                echo "After=network-online.target"
                echo ""
                echo "[Container]"
                echo "Image=localhost/ave:latest"
                echo "ContainerName=ave"
                echo "AutoUpdate=registry"
                echo "PublishPort=$BIND_IP:$WEB_PORT:8488"
                echo "Volume=$DATA_DIR:/data:Z"
                echo "ShmSize=$SHM_GB"
                if [[ -n "$WEB_PASS" ]]; then
                    echo "Environment=AVE_WEB_USER=$WEB_USER"
                    echo "Environment=AVE_WEB_PASSWORD=$WEB_PASS"
                fi
                if [[ "$GPU_ENABLED" == "true" ]]; then
                    echo "AddDevice=/dev/dri:/dev/dri"
                    echo "GroupAdd=keep-groups"
                fi
                echo ""
                echo "[Service]"
                echo "Restart=always"
                echo "TimeoutStartSec=120"
                echo "MemoryHigh=75%"
                echo "MemoryMax=85%"
                if [[ "${AUTO_BOOT,,}" != "n" && "${AUTO_BOOT,,}" != "no" ]]; then
                    echo ""
                    echo "[Install]"
                    echo "WantedBy=default.target"
                fi
            } > "$QUADLET_FILE"

            systemctl --user daemon-reload
            systemctl --user restart ave.service
            if [[ "${AUTO_BOOT,,}" != "n" && "${AUTO_BOOT,,}" != "no" ]]; then
                loginctl enable-linger "$USER" 2>/dev/null || true
                echo "[AVE] Enabled and started ave.service (auto-starts on boot)."
            else
                echo "[AVE] Started ave.service (manual start only; will not auto-start on boot)."
            fi
        else
            RUN_ARGS=(-d --name ave --restart unless-stopped -p "$BIND_IP:$WEB_PORT:8488" -v "$DATA_DIR:/data" --shm-size="$SHM_GB")
            [[ -n "$WEB_PASS" ]] && RUN_ARGS+=(-e "AVE_WEB_USER=$WEB_USER" -e "AVE_WEB_PASSWORD=$WEB_PASS")
            [[ "$GPU_ENABLED" == "true" ]] && RUN_ARGS+=(--device /dev/dri:/dev/dri)
            "$RUNTIME" run "${RUN_ARGS[@]}" localhost/ave:latest >/dev/null
        fi

        echo ""
        echo "=========================================="
        echo "  AVE is ready!                           "
        echo "  Web GUI: http://127.0.0.1:$WEB_PORT     "
        echo "  Watch folder: $DATA_DIR/watch           "
        echo "=========================================="
        ;;
esac
