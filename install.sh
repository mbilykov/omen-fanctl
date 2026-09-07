#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=/usr/local/lib/hp-fan-control
DOC_DIR=/usr/local/share/doc/hp-fan-control
CONFIG_DIR=/etc/hp-fan-control
UNIT_PATH=/etc/systemd/system/hp-fan-control.service
LOGROTATE_PATH=/etc/logrotate.d/hp-fan-control
ENABLE_NOW=false

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--enable-now]

Installs the daemon, default configuration, documentation, and systemd unit.
The optional WMI IR procfs provider is not installed. Existing configuration
is kept.
EOF
}

case "${1:-}" in
    "") ;;
    --enable-now) ENABLE_NOW=true ;;
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

for required in \
    "$SCRIPT_DIR/src/hp_fan_control.py" \
    "$SCRIPT_DIR/config/fan-control.toml" \
    "$SCRIPT_DIR/README.md" \
    "$SCRIPT_DIR/systemd/hp-fan-control.service" \
    "$SCRIPT_DIR/systemd/hp-fan-control.logrotate"; do
    if [[ ! -f "$required" ]]; then
        echo "ERROR: required source file is missing: $required" >&2
        exit 1
    fi
done

if systemctl cat hp-fan-control.service >/dev/null 2>&1; then
    if systemctl is-active --quiet hp-fan-control.service; then
        if [[ -e /run/hp-fan-control/auto-guard ]]; then
            echo "ERROR: refusing to stop the existing service during Auto guard" >&2
            echo "Wait for 'state=sleeping', then retry." >&2
            echo "Monitor with: journalctl -fu hp-fan-control.service" >&2
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
            echo "ERROR: cannot safely stop the existing service: HP hwmon was not found" >&2
            exit 1
        fi
        if [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
            echo "ERROR: refusing to stop the existing service outside BIOS Auto" >&2
            echo "Switch to Balanced, wait for 'state=sleeping', then retry." >&2
            exit 1
        fi
    fi
    echo "Stopping existing hp-fan-control.service before installation..."
    systemctl stop hp-fan-control.service
    if [[ -n "${HP_HWMON:-}" ]] && [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
        echo "ERROR: service stopped in maximum fail-safe; files were not replaced" >&2
        echo "Start the service again and wait for 'state=sleeping'." >&2
        exit 1
    fi
fi

install -D -m 0755 "$SCRIPT_DIR/src/hp_fan_control.py" \
    "$INSTALL_DIR/hp_fan_control.py"
install -D -m 0644 "$SCRIPT_DIR/README.md" "$DOC_DIR/README.md"
install -D -m 0644 "$SCRIPT_DIR/systemd/hp-fan-control.service" "$UNIT_PATH"
install -D -m 0644 "$SCRIPT_DIR/systemd/hp-fan-control.logrotate" \
    "$LOGROTATE_PATH"

if [[ -e "$CONFIG_DIR/fan-control.toml" ]]; then
    echo "Keeping existing configuration: $CONFIG_DIR/fan-control.toml"
else
    install -D -m 0644 "$SCRIPT_DIR/config/fan-control.toml" \
        "$CONFIG_DIR/fan-control.toml"
fi

systemctl daemon-reload

if [[ "$ENABLE_NOW" == true ]]; then
    systemctl enable --now hp-fan-control.service
    systemctl --no-pager --full status hp-fan-control.service || true
else
    echo "Installed but not running. Review $CONFIG_DIR/fan-control.toml, then run:"
    echo "  sudo systemctl enable --now hp-fan-control.service"
fi
