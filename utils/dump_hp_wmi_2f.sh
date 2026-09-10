#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TRACE_PROGRAM="$SCRIPT_DIR/trace_hp_wmi_2f.bt"
readonly BOARD_PATH=/sys/class/dmi/id/board_name
readonly PROFILE_PATH=/sys/firmware/acpi/platform_profile
readonly TEMPERATURE_LIMIT_MILLIC=70000
readonly VALIDATED_KERNEL=7.1.9-arch1-2

trace_pid=""
module_removed=0
original_profile=""

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

module_is_loaded() {
    awk '$1 == "hp_wmi" { found=1 } END { exit !found }' /proc/modules
}

find_hp_hwmon() {
    local directory name
    for directory in /sys/class/hwmon/hwmon*; do
        [[ -r "$directory/name" ]] || continue
        read -r name < "$directory/name"
        if [[ "$name" == hp ]]; then
            printf '%s\n' "$directory"
            return 0
        fi
    done
    return 1
}

stop_trace() {
    if [[ -n "$trace_pid" ]] && kill -0 "$trace_pid" 2>/dev/null; then
        kill -INT "$trace_pid" 2>/dev/null || true
        wait "$trace_pid" 2>/dev/null || true
    fi
    trace_pid=""
}

restore_system() {
    local hp_hwmon=""
    set +e
    stop_trace
    if (( module_removed )) || ! module_is_loaded; then
        modprobe hp_wmi
        module_removed=0
    fi
    hp_hwmon="$(find_hp_hwmon 2>/dev/null)"
    if [[ -n "$hp_hwmon" && -w "$hp_hwmon/pwm1_enable" ]]; then
        if [[ "$(<"$hp_hwmon/pwm1_enable")" != 2 ]]; then
            printf '2\n' > "$hp_hwmon/pwm1_enable"
        fi
    fi
    if [[ -n "$original_profile" && -w "$PROFILE_PATH" ]]; then
        if [[ "$(<"$PROFILE_PATH")" != "$original_profile" ]]; then
            printf '%s\n' "$original_profile" > "$PROFILE_PATH"
        fi
    fi
}

trap restore_system EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

(( EUID == 0 )) || die "run this script with sudo"
[[ "$(<"$BOARD_PATH")" == 8D87 ]] || die "this capture is allowlisted only for board 8D87"
[[ "$(uname -r)" == "$VALIDATED_KERNEL" ]] || \
    die "trace offset is validated only for kernel $VALIDATED_KERNEL (running: $(uname -r))"
[[ -r "$TRACE_PROGRAM" ]] || die "missing $TRACE_PROGRAM"
command -v bpftrace >/dev/null || die "bpftrace is required (Arch: sudo pacman -S bpftrace)"
command -v modprobe >/dev/null || die "modprobe was not found"
command -v perl >/dev/null || die "perl was not found"
command -v hexdump >/dev/null || die "hexdump was not found"
module_is_loaded || die "hp_wmi is not loaded"

if pgrep -f '[o]men_fanctl.py' >/dev/null; then
    die "omen_fanctl.py is running; stop it before capturing"
fi

hp_hwmon="$(find_hp_hwmon)" || die "HP hwmon device was not found"
[[ -r "$hp_hwmon/pwm1_enable" ]] || die "pwm1_enable is unavailable"
[[ "$(<"$hp_hwmon/pwm1_enable")" == 2 ]] || die "fans must be in firmware Auto (pwm1_enable=2)"

max_cpu_millic=0
for directory in /sys/class/hwmon/hwmon*; do
    [[ -r "$directory/name" ]] || continue
    [[ "$(<"$directory/name")" == k10temp ]] || continue
    for temperature_path in "$directory"/temp*_input; do
        [[ -r "$temperature_path" ]] || continue
        read -r temperature < "$temperature_path"
        if [[ "$temperature" =~ ^[0-9]+$ ]] && (( temperature > max_cpu_millic )); then
            max_cpu_millic=$temperature
        fi
    done
done
(( max_cpu_millic > 0 )) || die "no k10temp reading was found"
(( max_cpu_millic < TEMPERATURE_LIMIT_MILLIC )) || \
    die "CPU is too hot for module reload: $((max_cpu_millic / 1000)) C"

if [[ -r "$PROFILE_PATH" ]]; then
    read -r original_profile < "$PROFILE_PATH"
fi

stamp="$(date +%Y%m%d-%H%M%S)"
output_prefix="${1:-$PWD/hp-wmi-2f-$stamp}"
trace_log="${output_prefix}.trace.log"
binary_dump="${output_prefix}.bin"
hex_dump="${output_prefix}.hex.txt"

printf 'Board: 8D87; CPU: %.1f C; profile: %s; fans: Auto\n' \
    "$(awk -v value="$max_cpu_millic" 'BEGIN { print value / 1000 }')" \
    "${original_profile:-unknown}"
printf 'Starting WMI trace...\n'
BPFTRACE_MAX_STRLEN=256 bpftrace -q "$TRACE_PROGRAM" > "$trace_log" 2>&1 &
trace_pid=$!
sleep 2
kill -0 "$trace_pid" 2>/dev/null || {
    wait "$trace_pid" || true
    die "bpftrace failed to start; inspect $trace_log"
}

printf 'Reloading hp_wmi once to trigger its read-only 0x2f query...\n'
modprobe -r hp_wmi
module_removed=1
modprobe hp_wmi
module_removed=0
sleep 2
stop_trace

escaped_hex="$(sed -n 's/^HP2F_HEX //p' "$trace_log" | tail -n 1)"
[[ -n "$escaped_hex" ]] || die "0x2f response was not captured; inspect $trace_log"
plain_hex="${escaped_hex//\\x/}"
plain_hex="${plain_hex//[[:space:]]/}"
[[ "$plain_hex" =~ ^[0-9a-fA-F]+$ ]] || die "capture contains invalid hex"
(( ${#plain_hex} == 256 )) || \
    die "expected 128 bytes, captured $((${#plain_hex} / 2)); inspect $trace_log"

printf '%s' "$plain_hex" | \
    perl -e '$hex = do { local $/; <STDIN> }; print pack("H*", $hex)' \
    > "$binary_dump"
hexdump -C "$binary_dump" > "$hex_dump"
[[ "$(stat -c %s "$binary_dump")" == 128 ]] || die "binary dump size validation failed"

restore_system
trap - EXIT INT TERM

if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
    chown "$SUDO_UID:$SUDO_GID" "$trace_log" "$binary_dump" "$hex_dump"
fi

printf 'Captured and validated 128 bytes.\n'
printf 'Binary: %s\nHex:    %s\nTrace:  %s\n' \
    "$binary_dump" "$hex_dump" "$trace_log"
printf 'Final profile: %s; pwm1_enable: %s\n' \
    "$(<"$PROFILE_PATH")" "$(<"$(find_hp_hwmon)/pwm1_enable")"
