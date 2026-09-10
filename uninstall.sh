#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR=/usr/local/lib/omen-fanctl
DOC_DIR=/usr/local/share/doc/omen-fanctl
CONFIG_DIR=/etc/omen-fanctl
UNIT_PATH=/etc/systemd/system/omen-fanctl.service
TMPFILES_PATH=/etc/tmpfiles.d/omen-fanctl.conf
LOGROTATE_PATH=/etc/logrotate.d/omen-fanctl
PURGE_CONFIG=false

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=src/install/config.sh
source "$SCRIPT_DIR/src/install/config.sh"

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

log "Checking whether omen-fanctl can be removed safely"
if [[ -e /run/omen-fanctl/auto-guard ]]; then
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

if systemctl cat omen-fanctl.service >/dev/null 2>&1; then
    if systemctl is-active --quiet omen-fanctl.service; then
        log "Disabling and stopping omen-fanctl.service"
    else
        log "Disabling installed omen-fanctl.service"
    fi
    systemctl disable --now omen-fanctl.service || true
else
    log "omen-fanctl.service is not installed"
fi

if [[ "$(<"$HP_HWMON/pwm1_enable")" != 2 ]]; then
    echo "ERROR: service stop did not preserve BIOS Auto; files were not removed" >&2
    exit 1
fi

remove_file "$UNIT_PATH"
remove_file "$TMPFILES_PATH"
remove_file "$LOGROTATE_PATH"
remove_file "$INSTALL_DIR/omen_fanctl.py"
shopt -s nullglob
installed_modules=("$INSTALL_DIR/omen_fanctl/"*.py)
shopt -u nullglob
for module in "${installed_modules[@]}"; do
    remove_file "$module"
done
remove_cache "$INSTALL_DIR/omen_fanctl/__pycache__"
remove_cache "$INSTALL_DIR/__pycache__"
remove_file "$DOC_DIR/README.md"
rmdir --ignore-fail-on-non-empty \
    "$INSTALL_DIR/omen_fanctl" "$INSTALL_DIR" "$DOC_DIR" 2>/dev/null || true

if [[ "$PURGE_CONFIG" == true ]]; then
    purge_configuration "$CONFIG_DIR"
elif [[ -e "$CONFIG_DIR/omen-fanctl.toml" ]]; then
    log "Preserving configuration: $CONFIG_DIR/omen-fanctl.toml"
else
    log "No configuration file found"
fi

log "Reloading systemd units"
systemctl daemon-reload
log "Preserving telemetry directory: /var/log/omen-fanctl"
log "Uninstallation complete"
