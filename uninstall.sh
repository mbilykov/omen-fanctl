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

HP_HWMON=
for candidate in /sys/class/hwmon/hwmon*; do
    if [[ -r "$candidate/name" ]] && [[ "$(<"$candidate/name")" == hp ]]; then
        HP_HWMON=$candidate
        break
    fi
done
if [[ -z "$HP_HWMON" ]] || [[ ! -r "$HP_HWMON/pwm1_enable" ]]; then
    echo "ERROR: HP fan-control hwmon interface was not found" >&2
    exit 1
fi
if [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
    echo "ERROR: refusing to uninstall while fan control is not in BIOS Auto" >&2
    echo "Switch to Balanced, wait for 'state=sleeping', then retry." >&2
    exit 1
fi

if systemctl cat hp-fan-control.service >/dev/null 2>&1; then
    systemctl disable --now hp-fan-control.service || true
fi

if [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
    echo "ERROR: service stop did not preserve BIOS Auto; files were not removed" >&2
    exit 1
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
