# Configuration lifecycle shared by install.sh, uninstall.sh, and their tests.
# This file is sourced, never executed, and installs nothing on its own.

CONFIG_BASENAME=omen-fanctl.toml
BACKUP_GLOB="$CONFIG_BASENAME.bak.*"

config_log() {
    printf '==> %s\n' "$*"
}

config_backups() {
    # Print one path per line; a directory without backups prints nothing.
    local directory=$1 candidate

    # The directory is quoted, the pattern deliberately is not.
    for candidate in "$directory"/$BACKUP_GLOB; do
        [[ -e "$candidate" ]] || continue
        printf '%s\n' "$candidate"
    done
}

config_backup_path() {
    # A free backup name for this second. Replacing twice inside one second
    # must not overwrite the first backup, so collisions gain a suffix.
    local target=$1 base counter=1 candidate

    base="$target.bak.$(date +%Y%m%d%H%M%S)"
    candidate=$base
    while [[ -e "$candidate" ]]; do
        candidate="$base.$counter"
        counter=$((counter + 1))
    done
    printf '%s\n' "$candidate"
}

install_configuration() {
    # install_configuration <packaged> <target> <replace: true|false>
    local packaged=$1 target=$2 replace=$3 backup

    if [[ ! -e "$target" ]]; then
        config_log "Installing configuration -> $target"
        install -D -m 0644 -- "$packaged" "$target"
        return 0
    fi

    if [[ "$replace" != true ]]; then
        config_log "Preserving existing configuration: $target"
        config_log "Run with --replace-config to update it to the packaged defaults"
        return 0
    fi

    if cmp -s "$packaged" "$target"; then
        config_log "Configuration already matches the packaged defaults: $target"
        return 0
    fi

    backup=$(config_backup_path "$target")
    config_log "Saving replaced configuration: $backup"
    cp -a -- "$target" "$backup"
    config_log "Installing configuration -> $target"
    install -D -m 0644 -- "$packaged" "$target"
    config_log "Restart the service to apply it: systemctl restart omen-fanctl.service"
}

purge_configuration() {
    # purge_configuration <config_dir>. Backups hold configuration the user
    # wrote, so purging the active file deliberately keeps them.
    local directory=$1 target="$1/$CONFIG_BASENAME" backups

    if [[ -e "$target" ]]; then
        config_log "Removing $target"
        rm -f -- "$target"
    else
        config_log "Not installed: $target"
    fi

    mapfile -t backups < <(config_backups "$directory")
    if (( ${#backups[@]} > 0 )); then
        config_log \
            "Preserving ${#backups[@]} replaced-configuration backup(s) in $directory"
    fi
    rmdir --ignore-fail-on-non-empty "$directory" 2>/dev/null || true
}
