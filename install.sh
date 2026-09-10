#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=/usr/local/lib/omen-fanctl
DAEMON_SOURCE_DIR=$SCRIPT_DIR/src/daemon/omen_fanctl
DOC_DIR=/usr/local/share/doc/omen-fanctl
CONFIG_DIR=/etc/omen-fanctl
UNIT_PATH=/etc/systemd/system/omen-fanctl.service
TMPFILES_PATH=/etc/tmpfiles.d/omen-fanctl.conf
LOGROTATE_PATH=/etc/logrotate.d/omen-fanctl
START_NOW=false
ENABLE_NOW=false
REPLACE_CONFIG=false
CONFIG_PATH=$CONFIG_DIR/omen-fanctl.toml
PACKAGED_CONFIG=$SCRIPT_DIR/src/config/omen-fanctl.toml

# shellcheck source=src/install/config.sh
source "$SCRIPT_DIR/src/install/config.sh"

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
Usage: sudo ./install.sh [--start-now | --enable-now] [--replace-config]

Installs the daemon, default configuration, documentation, and systemd unit.
The optional WMI IR procfs provider is not installed. Existing configuration
is kept unless --replace-config is explicitly supplied.

Requirements: Python 3.11 or newer, systemd, and logrotate.
On Arch Linux: sudo pacman -S --needed logrotate

Options:
  --start-now       Start the service after installation without enabling it
  --enable-now      Enable the service at boot and start it after installation
  --replace-config  Replace an existing configuration with the packaged
                    defaults, keeping the previous file as a timestamped
                    .bak copy beside it
  -h, --help        Show this help and exit
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --start-now) START_NOW=true ;;
        --enable-now) ENABLE_NOW=true ;;
        --replace-config) REPLACE_CONFIG=true ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$START_NOW" == true && "$ENABLE_NOW" == true ]]; then
    echo "ERROR: --start-now and --enable-now are mutually exclusive" >&2
    exit 2
fi

if (( EUID != 0 )); then
    echo "ERROR: run this script with sudo" >&2
    exit 1
fi

log "Verifying source files"
for required in \
    "$SCRIPT_DIR/src/daemon/omen_fanctl.py" \
    "$SCRIPT_DIR/src/config/omen-fanctl.toml" \
    "$SCRIPT_DIR/README.md" \
    "$SCRIPT_DIR/src/systemd/omen-fanctl.service" \
    "$SCRIPT_DIR/src/logrotate/omen-fanctl" \
    "$SCRIPT_DIR/src/tmpfiles/omen-fanctl.conf"; do
    if [[ ! -f "$required" ]]; then
        echo "ERROR: required source file is missing: $required" >&2
        exit 1
    fi
done
if ! command -v logrotate >/dev/null 2>&1; then
    echo "ERROR: the logrotate package is required for telemetry retention." >&2
    echo "Please install it before running this installer." >&2
    echo "On Arch Linux: sudo pacman -S --needed logrotate" >&2
    exit 1
fi
shopt -s nullglob
DAEMON_SOURCES=("$DAEMON_SOURCE_DIR"/*.py)
shopt -u nullglob
if (( ${#DAEMON_SOURCES[@]} == 0 )); then
    echo "ERROR: no Python modules found in $DAEMON_SOURCE_DIR" >&2
    exit 1
fi
for source in "${DAEMON_SOURCES[@]}"; do
    if [[ ! -f "$source" ]]; then
        echo "ERROR: invalid daemon source: $source" >&2
        exit 1
    fi
done

if systemctl cat omen-fanctl.service >/dev/null 2>&1; then
    log "Existing omen-fanctl installation detected"
    if systemctl is-active --quiet omen-fanctl.service; then
        log "Service is active; checking whether it can be stopped safely"
        if [[ -e /run/omen-fanctl/auto-guard ]]; then
            echo "ERROR: refusing to stop the existing service during Auto guard" >&2
            echo "Wait for 'state=sleeping', then retry." >&2
            echo "Monitor with: journalctl -fu omen-fanctl.service" >&2
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
        log "Fan control is in BIOS Auto; stopping omen-fanctl.service"
        systemctl stop omen-fanctl.service
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
    log "No existing omen-fanctl installation detected"
fi

for source in "${DAEMON_SOURCES[@]}"; do
    install_file 0644 "$source" \
        "$INSTALL_DIR/omen_fanctl/${source##*/}"
done
install_file 0755 "$SCRIPT_DIR/src/daemon/omen_fanctl.py" \
    "$INSTALL_DIR/omen_fanctl.py"
install_file 0644 "$SCRIPT_DIR/README.md" "$DOC_DIR/README.md"
install_file 0644 "$SCRIPT_DIR/src/systemd/omen-fanctl.service" "$UNIT_PATH"
install_file 0644 "$SCRIPT_DIR/src/logrotate/omen-fanctl" "$LOGROTATE_PATH"
install_file 0644 "$SCRIPT_DIR/src/tmpfiles/omen-fanctl.conf" \
    "$TMPFILES_PATH"

log "Creating telemetry directory and applying retention policy"
systemd-tmpfiles --create "$TMPFILES_PATH"

install_configuration "$PACKAGED_CONFIG" "$CONFIG_PATH" "$REPLACE_CONFIG"

log "Reloading systemd units"
systemctl daemon-reload

if [[ "$ENABLE_NOW" == true ]]; then
    log "Enabling and starting omen-fanctl.service"
    systemctl enable --now omen-fanctl.service
    log "Installation complete; service is enabled and running"
    systemctl --no-pager --full status omen-fanctl.service || true
elif [[ "$START_NOW" == true ]]; then
    log "Starting omen-fanctl.service without changing its boot enablement"
    systemctl start omen-fanctl.service
    log "Installation complete; service is running"
    systemctl --no-pager --full status omen-fanctl.service || true
else
    log "Installation complete; service was not started"
    echo "Review $CONFIG_DIR/omen-fanctl.toml, then run one of:"
    echo "  sudo systemctl start omen-fanctl.service"
    echo "  sudo systemctl enable --now omen-fanctl.service"
    echo "Run './install.sh --help' to see all installation options."
fi
