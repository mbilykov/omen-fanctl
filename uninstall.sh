#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR=/usr/local/lib/hp-fan-control
DOC_DIR=/usr/local/share/doc/hp-fan-control
CONFIG_DIR=/etc/hp-fan-control
UNIT_PATH=/etc/systemd/system/hp-fan-control.service
TMPFILES_PATH=/etc/tmpfiles.d/hp-fan-control.conf
LEGACY_LOGROTATE_PATH=/etc/logrotate.d/hp-fan-control
PURGE_CONFIG=false

log() {
    printf '==> %s\n' "$*"
}

remove_file() {
    local path=$1

    if [[ -e "$path" ]]; then
        log "Removing $path"
        rm -f -- "$path"
    else
        log "Not installed: $path"
    fi
}

remove_cache() {
    local path=$1

    if [[ -d "$path" ]]; then
        log "Removing generated Python cache: $path"
        rm -rf -- "$path"
    fi
}

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

log "Checking whether hp-fan-control can be removed safely"
if [[ -e /run/hp-fan-control/auto-guard ]]; then
    echo "ERROR: refusing to uninstall during Auto guard" >&2
    echo "Wait for 'state=sleeping', then retry." >&2
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
log "Fan control is in BIOS Auto"

if systemctl cat hp-fan-control.service >/dev/null 2>&1; then
    if systemctl is-active --quiet hp-fan-control.service; then
        log "Disabling and stopping hp-fan-control.service"
    else
        log "Disabling installed hp-fan-control.service"
    fi
    systemctl disable --now hp-fan-control.service || true
else
    log "hp-fan-control.service is not installed"
fi

if [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
    echo "ERROR: service stop did not preserve BIOS Auto; files were not removed" >&2
    exit 1
fi

remove_file "$UNIT_PATH"
remove_file "$TMPFILES_PATH"
remove_file "$LEGACY_LOGROTATE_PATH"
remove_file "$INSTALL_DIR/hp_fan_control.py"
shopt -s nullglob
installed_modules=("$INSTALL_DIR/hp_fan_control/"*.py)
shopt -u nullglob
for module in "${installed_modules[@]}"; do
    remove_file "$module"
done
remove_cache "$INSTALL_DIR/hp_fan_control/__pycache__"
remove_cache "$INSTALL_DIR/__pycache__"
remove_file "$DOC_DIR/README.md"
rmdir --ignore-fail-on-non-empty \
    "$INSTALL_DIR/hp_fan_control" "$INSTALL_DIR" "$DOC_DIR" 2>/dev/null || true

if [[ "$PURGE_CONFIG" == true ]]; then
    remove_file "$CONFIG_DIR/fan-control.toml"
    rmdir --ignore-fail-on-non-empty "$CONFIG_DIR" 2>/dev/null || true
elif [[ -e "$CONFIG_DIR/fan-control.toml" ]]; then
    log "Preserving configuration: $CONFIG_DIR/fan-control.toml"
else
    log "No configuration file found"
fi

log "Reloading systemd units"
systemctl daemon-reload
log "Preserving telemetry directory: /var/log/hp-fan-control"
log "Uninstallation complete"
