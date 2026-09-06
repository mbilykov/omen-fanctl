#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR=/usr/local/lib/hp-fan-control
DOC_DIR=/usr/local/share/doc/hp-fan-control
CONFIG_DIR=/etc/hp-fan-control
UNIT_PATH=/etc/systemd/system/hp-fan-control.service
LOGROTATE_PATH=/etc/logrotate.d/hp-fan-control
PURGE_CONFIG=false

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge-config]

Stops and removes the daemon and systemd unit. Telemetry logs are preserved.
Configuration is preserved unless --purge-config is explicitly supplied.
EOF
}

case "${1:-}" in
    "") ;;
    --purge-config) PURGE_CONFIG=true ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if (( EUID != 0 )); then
    echo "ERROR: run this script with sudo" >&2
    exit 1
fi

if systemctl cat hp-fan-control.service >/dev/null 2>&1; then
    systemctl disable --now hp-fan-control.service || true
fi

# ExecStopPost normally performs this recovery. Repeat it before removing the
# executable so an inactive or previously failed unit also returns to Auto.
if [[ -x "$INSTALL_DIR/hp_fan_control.py" ]]; then
    "$INSTALL_DIR/hp_fan_control.py" --restore-auto || \
        echo "WARNING: could not verify firmware Auto" >&2
fi

rm -f -- "$UNIT_PATH" "$LOGROTATE_PATH" \
    "$INSTALL_DIR/hp_fan_control.py" "$DOC_DIR/README.md"
rmdir --ignore-fail-on-non-empty "$INSTALL_DIR" "$DOC_DIR" 2>/dev/null || true

if [[ "$PURGE_CONFIG" == true ]]; then
    rm -f -- "$CONFIG_DIR/fan-control.toml"
    rmdir --ignore-fail-on-non-empty "$CONFIG_DIR" 2>/dev/null || true
else
    echo "Preserved configuration: $CONFIG_DIR/fan-control.toml"
fi

systemctl daemon-reload
echo "Preserved telemetry directory: /var/log/hp-fan-control"
