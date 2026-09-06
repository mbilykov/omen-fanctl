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
The optional WMI IR probe is not installed. Existing configuration is kept.
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
    "$SCRIPT_DIR/src/fan-control.toml" \
    "$SCRIPT_DIR/src/README.md" \
    "$SCRIPT_DIR/systemd/hp-fan-control.service" \
    "$SCRIPT_DIR/systemd/hp-fan-control.logrotate"; do
    if [[ ! -f "$required" ]]; then
        echo "ERROR: required source file is missing: $required" >&2
        exit 1
    fi
done

install -D -m 0755 "$SCRIPT_DIR/src/hp_fan_control.py" \
    "$INSTALL_DIR/hp_fan_control.py"
install -D -m 0644 "$SCRIPT_DIR/src/README.md" "$DOC_DIR/README.md"
install -D -m 0644 "$SCRIPT_DIR/systemd/hp-fan-control.service" "$UNIT_PATH"
install -D -m 0644 "$SCRIPT_DIR/systemd/hp-fan-control.logrotate" \
    "$LOGROTATE_PATH"

if [[ -e "$CONFIG_DIR/fan-control.toml" ]]; then
    echo "Keeping existing configuration: $CONFIG_DIR/fan-control.toml"
else
    install -D -m 0644 "$SCRIPT_DIR/src/fan-control.toml" \
        "$CONFIG_DIR/fan-control.toml"
fi

systemctl daemon-reload

if [[ "$ENABLE_NOW" == true ]]; then
    systemctl enable --now hp-fan-control.service
    systemctl --no-pager --full status hp-fan-control.service || true
else
    echo "Installed but not enabled. Review $CONFIG_DIR/fan-control.toml, then run:"
    echo "  sudo systemctl enable --now hp-fan-control.service"
fi
