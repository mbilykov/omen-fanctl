#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
MODULE_DIR="$SCRIPT_DIR/hp-wmi-sensor-probe"
MODULE_NAME=hp_wmi_sensor_probe
PROC_FILE=/proc/hp_wmi_sensors
KERNEL_BUILD="/usr/lib/modules/$(uname -r)/build"
DURATION=${DURATION:-180}
INTERVAL=${INTERVAL:-5}
OUTPUT=${OUTPUT:-"$PROJECT_DIR/logs/hp-ir-comparison-$(date +%Y%m%d-%H%M%S).csv"}
MODE=compare

usage() {
    cat <<'EOF'
Usage:
  sudo ./probe_hp_ir_sensor.sh              Compare WMI and acpitz
  sudo ./probe_hp_ir_sensor.sh --load-only  Load and verify the daemon IR source
EOF
}

case "${1:-}" in
    "") ;;
    --load-only) MODE=load-only ;;
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

board=$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)
if [[ "$board" != "8D87" ]]; then
    echo "WARNING: designed for board 8D87; detected: ${board:-unknown}" >&2
fi

loaded_by_script=false
keep_loaded=false
cleanup() {
    if [[ "$loaded_by_script" == true && "$keep_loaded" == false && -e "$PROC_FILE" ]]; then
        rmmod "$MODULE_NAME" || true
    fi
}
trap cleanup EXIT INT TERM

validate_ir() {
    local readings=$1
    awk '
        NR == 1 {
            header = (NF == 3 && $1 == "index" && $2 == "name" && $3 == "temp_c")
        }
        $1 == 0 && $2 == "IR" && $3 ~ /^[0-9]+$/ && $3 >= 1 && $3 <= 125 {
            valid++
        }
        END { exit(header && valid == 1 ? 0 : 1) }
    ' <<<"$readings"
}

if [[ ! -e "$PROC_FILE" ]]; then
    if [[ ! -d "$KERNEL_BUILD" ]]; then
        echo "ERROR: matching kernel headers are missing: $KERNEL_BUILD" >&2
        echo "Install them with: sudo pacman -S linux-headers" >&2
        exit 1
    fi
    make -s -C "$MODULE_DIR"
    insmod "$MODULE_DIR/$MODULE_NAME.ko"
    loaded_by_script=true
fi

echo "Gaming Hub mapping on 8D87: IR = WMI sensor index 0"
echo "Initial HP WMI readings:"
initial_readings=$(cat "$PROC_FILE")
printf '%s\n' "$initial_readings"
if ! validate_ir "$initial_readings"; then
    echo "ERROR: $PROC_FILE does not contain exactly one valid '0 IR <temp>' row" >&2
    exit 1
fi

if [[ "$MODE" == load-only ]]; then
    keep_loaded=true
    echo "Probe is ready and will remain loaded for hp_fan_control.py."
    exit 0
fi

echo "Logging HP WMI and Linux ACPI readings every ${INTERVAL}s for ${DURATION}s..."
echo "Output: $OUTPUT"

mkdir -p "$(dirname -- "$OUTPUT")"
printf '%s\n' 'timestamp,ir_c,ambient_c,pch_c,vr_c,zone0_type,zone0_c,zone1_type,zone1_c' >"$OUTPUT"

deadline=$((SECONDS + DURATION))
while (( SECONDS < deadline )); do
    readings=$(cat "$PROC_FILE")
    ir=$(awk '$1 == 0 { print $3 }' <<<"$readings")
    ambient=$(awk '$1 == 1 { print $3 }' <<<"$readings")
    pch=$(awk '$1 == 2 { print $3 }' <<<"$readings")
    vr=$(awk '$1 == 3 { print $3 }' <<<"$readings")
    z0_type=$(cat /sys/class/thermal/thermal_zone0/type 2>/dev/null || printf absent)
    z0_raw=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || printf 0)
    z1_type=$(cat /sys/class/thermal/thermal_zone1/type 2>/dev/null || printf absent)
    z1_raw=$(cat /sys/class/thermal/thermal_zone1/temp 2>/dev/null || printf 0)
    z0=$(awk -v value="$z0_raw" 'BEGIN { printf "%.1f", value / 1000 }')
    z1=$(awk -v value="$z1_raw" 'BEGIN { printf "%.1f", value / 1000 }')

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$(date --iso-8601=seconds)" "$ir" "$ambient" "$pch" "$vr" \
        "$z0_type" "$z0" "$z1_type" "$z1" | tee -a "$OUTPUT"
    sleep "$INTERVAL"
done

echo "Finished: $OUTPUT"
