# HP fan-control prototype

This is an experimental standalone automatic fan controller for HP system board
`8D87`. It uses the Linux `hp-wmi` hwmon interface for fan control and the
project's read-only WMI sensor probe for HP's IR temperature. It does not run,
link to, or depend on OmenCore.

The default mode is read-only. Do not start with `--apply` before checking the
dry-run output.

## What it controls

- Reads CPU temperature from `k10temp`.
- Optionally reads the AMD GPU hwmon sensor and NVIDIA temperature through
  `nvidia-smi`.
- Reads Gaming Hub's exact IR input (WMI group `0x20008`, query `0x23`, index
  `0`) from `/proc/hp_wmi_sensors`.
- Evaluates CPU, GPU, and IR temperatures against separate curves and uses the
  highest resulting target. `acpitz` remains an opt-in diagnostic proxy only.
- Uses raw temperature for prompt ramp-up, and asymmetric EWMA plus the HP
  high/low thresholds for a slower ramp-down.
- Leaves BIOS Auto active while cool or outside the Performance profile.
- Uses `pwm1_enable=1` and `pwm1` only when software control is needed.
- Uses `pwm1_enable=0` for immediate maximum fans at the raw critical threshold.
- Restores `pwm1_enable=2` on normal exit, `SIGINT`, or `SIGTERM`.

The current kernel driver converts the standard hwmon PWM scale (`0..255`) to
the HP firmware fan-level scale and maps the second fan according to its fan
table. The prototype does not construct raw WMI packets itself.

## Included factory preset

The default config selects `hp-vibrance-stx-n22x9-performance`. These tables
were extracted from HP Gaming Hub's embedded resource
`PowerControl.JSON.Vibrance_STX_N22X9.json` (version `20250930`), selected for
the tested 8D87/Vibrance machine. Values below are HP fan levels on a `0..60`
scale; the Linux hwmon PWM command is computed as `level / 60 * 255`.

| Sensor | Rising temperatures (C) | Falling temperatures (C) | Fan levels |
|---|---|---|---|
| CPU | 60,64,68,71,74,76,78,80,82,83,84,85 | 56,60,64,67,70,72,74,76,78,79,80,81 | 19,20,21,22,23,25,28,31,34,37,43,47 |
| GPU | 57,60,63,66,69,71,73,75,77,78,79,80 | 53,55,58,61,64,67,69,71,73,75,76,77 | 19,20,21,22,23,25,28,31,34,37,43,47 |
| IR | 42,44,46,48,50,52,54,56,58,60,62,64 | not present in the resource | 19,20,21,22,23,25,28,31,34,37,43,47 |

The normal preset stops at level 47/60 (~78.3%). It does not weaken the
independent emergency rule: any raw monitored temperature at 92 C requests
level 60/60 (PWM 255) immediately. The precise physical sensor-chip model is
unknown; the WMI value itself is confirmed to be Gaming Hub's IR input.

The CSV log contains raw and filtered `ir` values, each sensor's target, and
`winning_sensor`, so a run can verify which curve controlled the fans. WMI IR
is enabled by default. Neither available `acpitz` zone is the same input; one
can still be enabled explicitly with `--include-acpi-proxy` for comparisons.
An IR reading below its first curve point does not hold the daemon in Manual
after a CPU/GPU-triggered cycle; IR release hysteresis is latched only after IR
itself reaches its activation point.
When NVIDIA telemetry is available, every sample also records
`nvidia_power_draw_w` and `nvidia_power_limit_w` from `nvidia-smi`. Empty values
mean that the dGPU was asleep or its driver did not expose the metric.

## 1. Run unit tests

```bash
cd fan-control-daemon-research/src
python3 -m unittest -v
```

## Capture the raw firmware fan table (`0x2f`)

`dump_hp_wmi_2f.sh` uses an eBPF probe on the WMI core and reloads `hp_wmi`
once to capture the read-only query performed during driver initialization. It
refuses to run unless board `8D87` is cool, firmware Auto is active, and the fan
controller is stopped. It restores the module, Auto mode, and the original
platform profile on exit. The script requires `bpftrace`, `perl`, and
`hexdump`; the latter two are already present on the tested installation.

```bash
sudo pacman -S bpftrace
sudo ./dump_hp_wmi_2f.sh
```

The output consists of a validated 128-byte `.bin`, an annotated `.hex.txt`,
and the original trace log. This diagnostic is x86_64-specific because it
captures the arguments of `wmi_evaluate_method` by calling convention.

## 2. Read-only dry run

Load and verify the read-only sensor probe with the project script, then select
the Performance profile:

```bash
cd fan-control-daemon-research/src
sudo ./probe_hp_ir_sensor.sh --load-only
```

Index 0 must appear as a valid row such as `0 IR 39`. Then run:

```bash
cd fan-control-daemon-research/src
python3 hp_fan_control.py --duration 60
```

This prints the temperatures and decisions but does not change fan state. A
timestamped CSV file is created in the current directory.

## 3. Short actuator test

Only after the dry run looks reasonable, verify that the kernel interface can
hold an intermediate level and return to Auto:

```bash
cd fan-control-daemon-research/src
sudo python3 hp_fan_control.py --apply --actuator-test 60 --duration 15
```

This requests 60% PWM for 15 seconds, prints actual RPM once per second, then
restores `pwm1_enable=2`. The test refuses values below 35%, durations above 60
seconds, non-allowlisted boards, or a starting state other than BIOS Auto.

On the tested machine, the EC took approximately ten seconds to ramp from
stopped fans to the 60% target. During that ramp, `pwm1` readback followed the
currently reached level rather than immediately displaying the requested one.

## 4. Short real idle test

With Performance selected, run the actual controller without a workload:

```bash
cd fan-control-daemon-research/src
sudo python3 hp_fan_control.py --apply --duration 60
```

At temperatures below 60 C this should remain in `bios-auto`. This verifies
profile gating and clean shutdown before a thermal test.

## 5. Controlled workload test

Keep the controller visible in one terminal:

```bash
cd fan-control-daemon-research/src
sudo python3 hp_fan_control.py --apply --duration 300
```

Start the workload in another terminal. Stop the workload before stopping the
controller. Press Ctrl+C if anything looks wrong. On Ctrl+C the controller
returns the fan interface to firmware Auto.

Useful independent checks:

```bash
watch -n1 'cat /sys/firmware/acpi/platform_profile; grep . /sys/class/hwmon/hwmon*/{name,pwm1,pwm1_enable,fan1_input,fan2_input} 2>/dev/null'
```

Expected states:

| State | Meaning |
|---|---|
| `bios-auto` | No writes; firmware owns the curve |
| `manual` | Prototype owns the curve and writes intermediate PWM values |
| `emergency` | Raw temperature reached 92 C; maximum fans requested |

### CPU-only control test

For a clean CPU-curve validation, disable GPU, WMI IR, and the ACPI proxy:

```bash
sudo python3 hp_fan_control.py --apply --cpu-only --duration 240 \
  --log-file factory-performance-cpu-only.csv
```

The normal command (without `--cpu-only`) evaluates CPU, GPU, and WMI IR
independently. Use that mode for the following combined workload test. The
ACPI proxy remains off unless `--include-acpi-proxy` is supplied.

### Combined CPU and NVIDIA GPU workload

The included CUDA stress helper provides a reproducible GPU workload when no
GPU benchmark is installed. Build it locally with:

```bash
/opt/cuda/bin/nvcc -O3 -o cuda_gpu_stress cuda_gpu_stress.cu
```

Run the controller for 240 seconds, then run `cuda_gpu_stress 150` and the
existing 150-second CPU stress command at approximately the same time in two
other terminals. Stop either workload immediately if the controller reports a
sensor failure or cannot retain fan control.

## Custom per-sensor curves

Replace the preset with one or more named tables. `pwm_percent` is accepted as
an alternative to `fan_level`; `stepped = false` produces linear interpolation.
Missing sensor tables fall back to the CPU table.

```toml
[curves.cpu]
high_temperature_c = [60, 70, 80]
low_temperature_c = [56, 66, 76]
fan_level = [19, 31, 47]
stepped = true

[curves.gpu]
high_temperature_c = [57, 67, 77]
low_temperature_c = [53, 63, 73]
fan_level = [19, 31, 47]
stepped = true

[curves.ir]
temperature_c = [42, 54, 64]
pwm_percent = [31.7, 46.7, 78.3]
stepped = true
```

## Important limitations

- This is not installed as a system service yet.
- It is allowlisted only for board `8D87`.
- The extracted preset is confirmed only for the tested Vibrance/8D87 model;
  other boards must remain excluded until their matching Gaming Hub resource
  and fan-level behavior are verified.
- `SIGKILL`, power loss, or a kernel crash cannot run cleanup. The current
  `hp-wmi` driver has its own firmware fallback/keepalive behavior, but this is
  not a substitute for testing failure modes.
- The probe must be loaded when WMI IR is enabled. A missing or malformed
  index-0 reading prevents startup. If IR is lost later, the daemon logs the
  degraded state and continues safely from CPU/GPU; IR rejoins automatically
  when it recovers. Loss of the mandatory CPU source still invokes the
  maximum-fan fail-safe.
- The physical make/model of the IR sensor chip cannot be inferred from WMI.
- `acpitz` is not Gaming Hub's IR input and is disabled by default.
- Never run this prototype together with another fan-control program.
