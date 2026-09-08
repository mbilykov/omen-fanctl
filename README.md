# HP fan-control daemon for performance profile

Linux fan-control service for HP systems where firmware Auto mode does not
provide sufficient cooling under the Performance platform profile.

## Navigation

- [Problem](#problem)
- [Requirements and compatibility](#requirements-and-compatibility)
- [Tested configuration](#tested-configuration)
- [Installation](#installation)
- [Uninstallation](#uninstallation)
- [Logging](#logging)
- [Control logic](#control-logic)
- [Unit tests](#unit-tests)
- [Hardware validation](#hardware-validation)
- [License](#license)

## Problem

On the tested HP OMEN MAX 16-ak0xxx, firmware Auto mode limits fan speed to
approximately 3,400/3,600 RPM under a sustained Performance-profile workload.
CPU temperature reached 92-99 C even though the fans support approximately
6,000 RPM.

The daemon applies the factory CPU, GPU, and optional IR curves through the
Linux `hp-wmi` hwmon interface. It controls the fans only while the
`performance` platform profile is selected and software cooling is required.

The daemon also handles a firmware transition issue observed on BIOS F.07.
After switching from Manual to Auto, both fans can remain stopped for roughly
90-120 seconds. During this interval the workload may return before firmware
cooling resumes. The daemon therefore keeps monitoring temperatures for 180
seconds after every Manual-to-Auto transition and immediately reclaims Manual
control if the system heats up.

Hardware measurements, extracted fan tables, and validation results are
documented in [the hardware research notes](docs/hardware-research.md).

## Requirements and compatibility

Runtime requirements:

- Python 3.11 or newer; no third-party Python packages are required.
- `systemd` with watchdog and notification support.
- `/sys/firmware/acpi/platform_profile` with a `performance` profile.
- Linux `hp-wmi` hwmon fan control exposing `pwm1`, `pwm1_enable`,
  `fan1_input`, and `fan2_input`.
- `k10temp` for the mandatory CPU temperature source.
- Root access for installation and fan-control writes.
- `nvidia-smi` is optional and supplies NVIDIA GPU temperature and power data.
- `/proc/hp_wmi_sensors` is optional. If supplied by an external kernel
  component, sensor index 0 is used as the confirmed HP IR input.

The implementation and bundled curve are primarily designed and validated for
system board `8D87`. The default configuration rejects every other board.

Other HP Victus/OMEN-family systems using the same kernel `hp-wmi` fan-control
implementation may expose compatible interfaces. This does not guarantee that
their WMI capabilities, fan mapping, curves, or firmware transition behavior
are identical. A new board must complete the [hardware validation](#hardware-validation)
procedure before it is added to `allowed_boards` or used with `--apply`.

## Tested configuration

| Component | Tested value |
|---|---|
| Computer | HP OMEN MAX Gaming Laptop 16-ak0xxx |
| System board | `8D87` |
| BIOS | `F.07` |
| Distribution | Arch Linux / Omarchy |
| Kernel | `7.1.9-arch1-2` |
| Fan interface | Linux `hp-wmi` hwmon |
| CPU sensor | `k10temp` |
| GPU telemetry | AMD hwmon and NVIDIA `nvidia-smi` |
| Optional IR source | WMI group `0x20008`, query `0x23`, index `0` |

The following workload tools were used during validation:

- `stress-ng` for sustained CPU workloads;
- the CUDA workload in `utils/cuda_gpu_stress.cu`, built with `nvcc`, for
  sustained NVIDIA GPU workloads;
- combined `stress-ng` and CUDA workloads for shared thermal-load testing.

## Installation

Clone the repository:

```bash
git clone https://github.com/<owner>/hp-fan-control.git
cd hp-fan-control
```

Display the installer options:

```bash
./install.sh --help
```

Install without starting the service:

```bash
sudo ./install.sh
```

Install and start the service for the current boot without changing its boot
enablement:

```bash
sudo ./install.sh --start-now
```

Install and enable the service immediately:

```bash
sudo ./install.sh --enable-now
```

The configuration is installed at `/etc/hp-fan-control/fan-control.toml`.
Existing configuration is preserved during upgrades.

### Service management

Start or stop the installed service:

```bash
sudo systemctl start hp-fan-control.service
sudo systemctl stop hp-fan-control.service
```

Enable or disable automatic startup at boot. These commands do not change the
current running state:

```bash
sudo systemctl enable hp-fan-control.service
sudo systemctl disable hp-fan-control.service
```

Show the current service status:

```bash
systemctl status hp-fan-control.service
```

## Uninstallation

Remove the service while preserving its configuration and telemetry:

```bash
sudo ./uninstall.sh
```

Also remove `/etc/hp-fan-control/fan-control.toml`:

```bash
sudo ./uninstall.sh --purge-config
```

The uninstaller refuses to stop the service during `auto-guard` or while the
fan interface is outside firmware Auto mode. Wait for `state=sleeping` before
retrying. Telemetry under `/var/log/hp-fan-control/` is always preserved.

## Logging

Follow service events and state transitions:

```bash
journalctl -fu hp-fan-control.service
```

The journal records state changes immediately and rate-limits unchanged status
messages to one entry every 30 seconds.

Full-resolution telemetry is written once per second to timestamped CSV files
under `/var/log/hp-fan-control/`. Records include:

- platform profile and controller state;
- raw and EWMA-filtered CPU, GPU, IR, and optional ACPI temperatures;
- NVIDIA power draw and power limit when available;
- the target from each sensor curve and the winning sensor;
- requested PWM, actual fan mode, and both fan RPM values.

`--include-acpi-proxy` adds `acpitz` readings and their hypothetical IR-curve
target to this telemetry for comparison. The proxy cannot activate Manual
mode, change the requested PWM, trigger emergency cooling, or delay the return
to firmware Auto.

Each daemon start creates a separate timestamped CSV file. The supplied
systemd-tmpfiles policy removes inactive telemetry files after 14 days; it does
not truncate or rename the file currently held open by the daemon.

## Control logic

The systemd service remains resident, but temperature polling and fan-control
work run only when required.

1. Outside the `performance` profile, the daemon enters `sleeping` after a safe
   handoff. It blocks on the kernel `platform_profile` notification instead of
   polling the profile file.
2. In Performance, firmware Auto remains active while temperatures are below
   the activation thresholds. CPU and GPU control activate at 60 C by default.
   Optional IR control activates at the first curve step above the minimum
   Manual PWM, 44 C with the supplied factory curve.
3. Raw temperatures permit immediate fan-speed increases. Asymmetric EWMA,
   curve hysteresis, and PWM rate limits prevent rapid decreases or oscillation.
4. CPU, GPU, and IR are evaluated against independent curves. The highest fan
   request wins. Linux `hp-wmi` converts the standard `0..255` PWM value to the
   firmware fan-level mapping.
5. Software control uses Manual mode (`pwm1_enable=1`). Any raw monitored
   temperature reaching 92 C immediately selects maximum mode
   (`pwm1_enable=0`, PWM 255).
6. Manual control returns to firmware Auto when raw CPU and GPU temperatures
   are at or below `fan_stop_temp_c` (45 C by default). If IR activated the
   cycle, it must also fall to its release threshold, 43 C by default.
7. After selecting Auto, `auto-guard` monitors the complete observed firmware
   fan-stop window for 180 seconds. New heat immediately restores Manual
   control. A profile change during this interval does not cancel protection.
8. Once the guard expires outside Performance, the daemon enters `sleeping`.

Controller states:

| State | Fan ownership and behavior |
|---|---|
| `bios-auto` | Firmware owns the fans; Performance remains monitored |
| `manual` | The daemon writes intermediate PWM levels |
| `emergency` | Maximum fans remain selected until critical release |
| `handoff` | Cooling continues before a safe return to Auto |
| `auto-guard` | Firmware owns the fans while temperatures remain monitored |
| `sleeping` | Firmware owns the fans; the daemon waits for a profile event |

IR is optional. If `/proc/hp_wmi_sensors` is absent, invalid, or disappears,
the daemon logs the degraded state and continues with CPU/GPU. Loss of the
mandatory CPU source, or loss of any previously available GPU source, while
software control or `auto-guard` is active selects maximum fans. CPU and AMD
GPU hwmon paths are rediscovered after driver reset or device re-probe;
`nvidia-smi` discovery is retried every 30 seconds, while NVIDIA query failures
and all sensor recoveries are logged once per transition. The last valid NVIDIA
metrics bridge up to two consecutive query failures; the third failure selects
the maximum-fan fail-safe.

Crash recovery is independent of Python cleanup. The systemd unit uses a
15-second watchdog and `ExecStopPost=... --failsafe`. If the process exits,
hangs, or receives `SIGKILL` during Manual, Max, or guarded Auto, the recovery
command selects maximum fans before systemd restarts the service. A guard
marker in `/run/hp-fan-control/` makes this decision survive loss of the main
process. The daemon derives its heartbeat interval from systemd's
`WATCHDOG_USEC`, so long sensor sampling intervals do not starve the watchdog.

The executable `src/daemon/hp_fan_control.py` is a compatibility entry point.
Implementation is split by responsibility under `src/daemon/hp_fan_control/`:
configuration and curves, hardware adapters, the control state machine, and
the command-line lifecycle. The installer preserves the same executable path
used by the systemd unit.

## Unit tests

Run the complete test suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The tests use temporary files and fake fan devices; they do not write to the
machine's hwmon interface.

## Hardware validation

Fan-control writes can produce inadequate cooling on incompatible hardware.
Do not use `--apply` until read-only output and the detected interfaces have
been reviewed. Never run this daemon together with another fan-control tool.

### 1. Verify platform interfaces

```bash
cat /sys/class/dmi/id/board_name
cat /sys/firmware/acpi/platform_profile
grep . /sys/class/hwmon/hwmon*/{name,pwm1,pwm1_enable,fan1_input,fan2_input} 2>/dev/null
```

On `8D87`, the HP fan interface must report `name=hp` and firmware Auto must be
`pwm1_enable=2` before testing.

If the optional IR interface exists, verify that index 0 is valid:

```bash
cat /proc/hp_wmi_sensors
```

Expected format:

```text
index name temp_c
0 IR 39
```

### 2. Run read-only mode

```bash
python3 src/daemon/hp_fan_control.py \
  --config src/config/fan-control.toml --duration 60
```

Review sensor selection, temperatures, requested PWM, and warnings. This mode
does not write fan controls.

### 3. Run the bounded actuator test

Only on an allowlisted and reviewed board:

```bash
sudo python3 src/daemon/hp_fan_control.py \
  --config src/config/fan-control.toml \
  --apply --actuator-test 60 --duration 15
```

The test requests 60% PWM for 15 seconds and must restore
`pwm1_enable=2`. Stop testing if the fans do not ramp or Auto is not restored.

### 4. Validate CPU and GPU control

Follow the controller in one terminal:

```bash
journalctl -fu hp-fan-control.service
```

Use a bounded CPU workload in another terminal:

```bash
stress-ng --cpu 4 --timeout 60s --metrics-brief
```

Build and run the included CUDA workload when the CUDA toolkit is available:

```bash
/opt/cuda/bin/nvcc -O3 \
  -o utils/cuda_gpu_stress utils/cuda_gpu_stress.cu
utils/cuda_gpu_stress 60
```

Verify that raw temperature increases raise PWM promptly, the hottest sensor
wins, RPM follows the requested level, and the critical threshold selects
maximum fans. Stop the workloads before thermal or power limits are exceeded.

### 5. Validate profile handoff and Auto guard

While Manual control is active, stop the workload and select a profile other
than Performance. Verify this sequence in the journal:

```text
manual -> handoff -> auto-guard -> sleeping
```

During `auto-guard`, start a short workload. The daemon must immediately return
to `manual` or `handoff`. Stop the workload and confirm that a new 180-second
guard begins after the next return to Auto.

### 6. Validate process-failure recovery

Perform this test only after normal Manual control, cooldown, and Auto handoff
have been verified:

```bash
sudo systemctl kill --kill-whom=main --signal=SIGKILL \
  hp-fan-control.service
journalctl -u hp-fan-control.service --since=-1min --no-pager
```

The journal must show the killed process, `maximum fail-safe verified`, and a
new service process. The fan interface must transition directly to maximum
without an intermediate unsafe Auto handoff.

Results for any additional board should include DMI and BIOS identifiers,
kernel version, hwmon channels, dry-run logs, actuator behavior, fan mapping,
CPU/GPU workload telemetry, profile handoff, Auto-guard behavior, and crash
recovery. Do not add the board to `allowed_boards` based only on a matching
product family.

## License

This project is distributed under the [MIT License](LICENSE).
