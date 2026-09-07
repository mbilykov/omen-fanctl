#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=/usr/local/lib/hp-fan-control
DOC_DIR=/usr/local/share/doc/hp-fan-control
CONFIG_DIR=/etc/hp-fan-control
UNIT_PATH=/etc/systemd/system/hp-fan-control.service
TMPFILES_PATH=/etc/tmpfiles.d/hp-fan-control.conf
LEGACY_LOGROTATE_PATH=/etc/logrotate.d/hp-fan-control
START_NOW=false
ENABLE_NOW=false

log() {
    printf '==> %s\n' "$*"
}

install_file() {
    local mode=$1
    local source=$2
    local destination=$3
    local displayed_source=${source#"$SCRIPT_DIR"/}

    log "Installing $displayed_source -> $destination"
    install -D -m "$mode" "$source" "$destination"
}

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--start-now | --enable-now]

Installs the daemon, default configuration, documentation, and systemd unit.
The optional WMI IR procfs provider is not installed. Existing configuration
is kept.

Options:
  --start-now   Start the service after installation without enabling it
  --enable-now  Enable the service at boot and start it after installation
  -h, --help    Show this help and exit
EOF
}

if (( $# > 1 )); then
    usage >&2
    exit 2
fi

case "${1:-}" in
    "") ;;
    --start-now) START_NOW=true ;;
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

log "Verifying source files"
for required in \
    "$SCRIPT_DIR/src/daemon/hp_fan_control.py" \
    "$SCRIPT_DIR/src/config/fan-control.toml" \
    "$SCRIPT_DIR/README.md" \
    "$SCRIPT_DIR/src/systemd/hp-fan-control.service" \
    "$SCRIPT_DIR/src/tmpfiles/hp-fan-control.conf"; do
    if [[ ! -f "$required" ]]; then
        echo "ERROR: required source file is missing: $required" >&2
        exit 1
    fi
done

if systemctl cat hp-fan-control.service >/dev/null 2>&1; then
    log "Existing hp-fan-control installation detected"
    if systemctl is-active --quiet hp-fan-control.service; then
        log "Service is active; checking whether it can be stopped safely"
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
        log "Fan control is in BIOS Auto; stopping hp-fan-control.service"
        systemctl stop hp-fan-control.service
        log "Service stopped"
        if [[ -n "${HP_HWMON:-}" ]] && [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
            echo "ERROR: service stopped in maximum fail-safe; files were not replaced" >&2
            echo "Start the service again and wait for 'state=sleeping'." >&2
            exit 1
        fi
    else
        log "Service is already stopped"
    fi
else
    log "No existing hp-fan-control installation detected"
fi

install_file 0755 "$SCRIPT_DIR/src/daemon/hp_fan_control.py" \
    "$INSTALL_DIR/hp_fan_control.py"
install_file 0644 "$SCRIPT_DIR/README.md" "$DOC_DIR/README.md"
install_file 0644 "$SCRIPT_DIR/src/systemd/hp-fan-control.service" "$UNIT_PATH"
install_file 0644 "$SCRIPT_DIR/src/tmpfiles/hp-fan-control.conf" \
    "$TMPFILES_PATH"

if [[ -e "$LEGACY_LOGROTATE_PATH" ]]; then
    log "Removing obsolete logrotate policy: $LEGACY_LOGROTATE_PATH"
    rm -f -- "$LEGACY_LOGROTATE_PATH"
fi

log "Creating telemetry directory and applying retention policy"
systemd-tmpfiles --create "$TMPFILES_PATH"

if [[ -e "$CONFIG_DIR/fan-control.toml" ]]; then
    log "Preserving existing configuration: $CONFIG_DIR/fan-control.toml"
else
    install_file 0644 "$SCRIPT_DIR/src/config/fan-control.toml" \
        "$CONFIG_DIR/fan-control.toml"
fi

log "Reloading systemd units"
systemctl daemon-reload

if [[ "$ENABLE_NOW" == true ]]; then
    log "Enabling and starting hp-fan-control.service"
    systemctl enable --now hp-fan-control.service
    log "Installation complete; service is enabled and running"
    systemctl --no-pager --full status hp-fan-control.service || true
elif [[ "$START_NOW" == true ]]; then
    log "Starting hp-fan-control.service without changing its boot enablement"
    systemctl start hp-fan-control.service
    log "Installation complete; service is running"
    systemctl --no-pager --full status hp-fan-control.service || true
else
    log "Installation complete; service was not started"
    echo "Review $CONFIG_DIR/fan-control.toml, then run one of:"
    echo "  sudo systemctl start hp-fan-control.service"
    echo "  sudo systemctl enable --now hp-fan-control.service"
    echo "Run './install.sh --help' to see all installation options."
fi
