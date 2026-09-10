# omen-fanctl

Linux fan-control daemon for the HP OMEN MAX 16 (board `8D87`), where firmware
Auto mode under-cools the Performance platform profile. Other OMEN/Victus
boards using the same `hp-wmi` interface may work, but require
[hardware validation](#hardware-validation) first.

## Navigation

- [Problem](#problem)
- [Requirements and compatibility](#requirements-and-compatibility)
- [Tested configuration](#tested-configuration)
- [Installation](#installation)
- [Uninstallation](#uninstallation)
- [Curve presets](#curve-presets)
- [Custom per-sensor curves](#custom-per-sensor-curves)
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

The daemon applies independent CPU, GPU, and optional IR curves through the
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
- `logrotate` for bounded CSV telemetry retention.
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
procedure before it is used with `--apply` or added to the `allowed_boards`
list of a system that runs the daemon as a service.

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

### Install requirements

On Arch Linux, install `logrotate` before running the installer:

```bash
sudo pacman -S --needed logrotate
```

### Install the daemon

Clone the repository:

```bash
git clone https://github.com/mbilykov/omen-fanctl.git
cd omen-fanctl
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

The configuration is installed at `/etc/omen-fanctl/omen-fanctl.toml`.
Existing configuration is preserved during upgrades, so a release that changes
the packaged defaults does not alter a working installation. To adopt the new
defaults:

```bash
sudo ./install.sh --replace-config
sudo systemctl restart omen-fanctl.service
```

The replaced file is kept beside it as `omen-fanctl.toml.bak.<timestamp>`, and
those backups survive `./uninstall.sh --purge-config`. Replacing again when the
installed file already matches the packaged defaults writes no backup.

Invalid configuration exits with status 78 and leaves the systemd unit in the
failed state instead of retrying forever. Runtime hardware failures continue
to retry every 10 seconds. After correcting the configuration, restart the
service normally.

### Service management

Start or stop the installed service:

```bash
sudo systemctl start omen-fanctl.service
sudo systemctl stop omen-fanctl.service
```

Enable or disable automatic startup at boot. These commands do not change the
current running state:

```bash
sudo systemctl enable omen-fanctl.service
sudo systemctl disable omen-fanctl.service
```

Show the current service status:

```bash
systemctl status omen-fanctl.service
```

## Uninstallation

Remove the service while preserving its configuration and telemetry:

```bash
sudo ./uninstall.sh
```

Also remove `/etc/omen-fanctl/omen-fanctl.toml`:

```bash
sudo ./uninstall.sh --purge-config
```

The uninstaller refuses to stop the service during `auto-guard` or while the
fan interface is outside firmware Auto mode. Wait for `state=sleeping` before
retrying. Telemetry under `/var/log/omen-fanctl/` is always preserved.

## Curve presets

`curves.preset` selects a built-in set of tables.

| Preset | Top step | Behaviour |
|---|---|---|
| `performance-extended` (default) | 60/60 (100%) at 90 C | The factory table plus three steps above its ceiling |
| `hp-vibrance-stx-n22x9-performance` | 47/60 (~78.3%) at 85 C | The extracted factory table, unmodified; requires `critical_temp_c` |

HP's Performance table stops at fan level 47 of 60, around 4,700 RPM, which
settles this machine near 85 C. Because the fans never go higher under that
table, the only path to full speed was the emergency threshold, which a
sustained workload reaches. The default preset keeps every factory step below
86 C unchanged and adds levels 51, 55, and 60 above it, so the curve itself
reaches full speed at 90 C. A normal workload therefore sounds as it did
before, and because the curve now covers the whole range, the temperature
override is disabled by default; see `critical_temp_c` in the configuration.

The extended CPU steps are 86 C -> 85%, 88 C -> 91.7%, and 90 C -> 100%, with
falling thresholds 82, 84, and 86 C. GPU adds 82, 85, and 88 C; optional IR
adds 66, 68, and 70 C.

## Custom per-sensor curves

To define custom tables, remove `preset` and add a
`[curves.cpu]` table. CPU is the required base curve; omitted `gpu` and `ir`
tables fall back to it. An omitted `acpi` table uses the `ir` curve when
present, otherwise it also falls back to CPU.

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
high_temperature_c = [42, 54, 64]
pwm_percent = [31.7, 46.7, 78.3]
stepped = true
```

`high_temperature_c` contains the rising thresholds; the legacy name
`temperature_c` is accepted as an alias. Fan output may be specified as HP
`fan_level` values or as `pwm_percent`. Each curve needs at least two points,
all arrays for that curve must have the same length, temperature thresholds
must be strictly increasing, and output values must be non-decreasing.

Set `stepped = false` (the default) for linear interpolation between points.
Set `stepped = true` for discrete output levels. Optional
`low_temperature_c` thresholds add per-step falling hysteresis and are valid
only for a stepped curve; they must be strictly increasing, each must be below
its corresponding rising threshold, and the associated fan output values must
be strictly increasing rather than merely non-decreasing. This last rule keeps
each retained hysteresis level unambiguous.

## Logging

Follow service events and state transitions:

```bash
journalctl -fu omen-fanctl.service
```

The journal records state changes immediately and rate-limits unchanged status
messages to one entry every 30 seconds.

Full-resolution telemetry is appended once per second to
`/var/log/omen-fanctl/omen-fanctl.csv`. Records include:

- platform profile and controller state;
- raw and EWMA-filtered CPU, GPU, IR, and optional ACPI temperatures, including
  a stale-data marker when a transient AMD read uses its cached temperature;
- NVIDIA power draw and power limit when available, plus a stale-data marker;
- the target from each sensor curve and the winning sensor;
- requested PWM, actual fan mode, and both fan RPM values.

`--include-acpi-proxy` adds `acpitz` readings and their hypothetical IR-curve
target to this telemetry for comparison. The proxy cannot activate Manual
mode, change the requested PWM, trigger emergency cooling, or delay the return
to firmware Auto.

Service restarts continue the same CSV file without duplicating its header. If
an upgrade changes the CSV schema, the daemon preserves the incompatible file
with a `.previous[.N]` suffix and starts a new active file with the current
header. The supplied logrotate policy rotates the active file daily, retains 14
archives, compresses old files, and uses `copytruncate` so the daemon does not
need to reopen it. `copytruncate` has a narrow race in which one telemetry row
can be lost; fan control is unaffected. The systemd-tmpfiles policy also removes
inactive telemetry files after 14 days, including timestamped and `.previous`
files left by older daemon versions or schema upgrades.

## Control logic

The systemd service remains resident, but temperature polling and fan-control
work run only when required.

1. Outside the `performance` profile, the daemon enters `sleeping` after a safe
   handoff. It blocks on the kernel `platform_profile` notification instead of
   polling the profile file.
2. In Performance, firmware Auto remains active while temperatures are below
   the activation thresholds. CPU and GPU control activate at 60 C by default.
   Optional IR control activates at the first curve step above the minimum
   Manual PWM, 44 C with the supplied curves.
3. Raw temperatures permit immediate fan-speed increases. Asymmetric EWMA,
   curve hysteresis, and PWM rate limits prevent rapid decreases or oscillation.
4. CPU, GPU, and IR are evaluated against independent curves. The highest fan
   request wins. Linux `hp-wmi` converts the standard `0..255` PWM value to the
   firmware fan-level mapping.
5. Software control stays in Manual mode (`pwm1_enable=1`) all the way to full
   speed, because the default curve reaches 100% at 90 C. Maximum mode
   (`pwm1_enable=0`, PWM 255) is reserved for a sensor lost during control and
   for a crashed run whose fans were left at maximum. Setting
   `critical_temp_c` restores the temperature override, which any raw monitored
   temperature at or above it then triggers; it is off by default because
   overriding a curve that already commands 100% changes no fan speed. The two
   settings are coupled: with the override off, every active control curve must
   reach 100%, otherwise full speed would be unreachable and the daemon refuses
   to start.
6. Manual control returns to firmware Auto when raw CPU and GPU temperatures
   are at or below `fan_stop_temp_c` (45 C by default). If IR activated the
   cycle, it must also fall to its release threshold, 43 C by default. A lost
   optional IR source blocks this handoff for two missing samples, then stops
   participating on the third so it cannot trap the daemon in Manual mode.
7. After selecting Auto, `auto-guard` monitors the complete observed firmware
   fan-stop window for 180 seconds. New heat immediately restores Manual
   control. A profile change during this interval does not cancel protection.
8. Once the guard expires outside Performance, the daemon enters `sleeping`.
9. A stop signal is answered by returning the fans to firmware Auto when the
   hottest raw control temperature is at or below `stop_handoff_max_temp_c`
   (70 C by default, `false` to disable). Reboot and poweroff arrive as
   `SIGTERM` while software control is usually still active, and the
   maximum-fan fail-safe would otherwise run the fans at full speed for the
   rest of the shutdown and into the next power-on. The fail-safe still applies
   above that temperature, when maximum mode is already selected or was
   asserted externally, and when the fan mode is unreadable or unrecognised.
   It also applies unless the last sample describes the machine being handed
   over: temperatures unreadable, older than five seconds, holding a reading
   reused after a failed query, taken while a depended-on source is failing, or
   missing a control sensor that activated the current Manual cycle and has not
   yet aged out all block the handoff, the last of those by the same rule the
   normal Auto handoff uses. Source health is reported separately from the
   readings because a query that fails with no cache to reuse yields neither a
   temperature nor a staleness flag, and another GPU can fill the aggregated
   value in its place. The firmware
   fan-stop window is unattended once the daemon is gone. That age limit is
   measured on `CLOCK_BOOTTIME`, so a sample from before a system suspend is
   never recent, and a `sample_interval_s` above the limit hands off only when
   a stop arrives within five seconds of a sample; such a cadence is reported
   once at startup. A stop taken during `auto-guard` retires the guard without
   writing Auto again: `hp-wmi` re-applies the fan settings on every mode
   write, which could restart the firmware fan-stop window just as the daemon
   exits.
   An installed configuration that predates this setting omits the key, which
   means the same as `false`: upgrades keep the unconditional fail-safe until
   the key is added by hand or by `install.sh --replace-config`.

Controller states:

| State | Fan ownership and behavior |
|---|---|
| `bios-auto` | Firmware owns the fans; Performance remains monitored |
| `manual` | The daemon writes intermediate PWM levels |
| `emergency` | Maximum fans remain selected until critical release; entered on sensor loss, on adopting a crashed run's maximum, or through `critical_temp_c` when configured |
| `handoff` | Cooling continues before a safe return to Auto |
| `auto-guard` | Firmware owns the fans while temperatures remain monitored |
| `sleeping` | Firmware owns the fans; the daemon waits for a profile event |

IR is optional. If `/proc/hp_wmi_sensors` is absent, invalid, or disappears,
the daemon logs the degraded state and continues with CPU/GPU. Loss of the
mandatory CPU source, or loss of any previously available GPU source, while
software control or `auto-guard` is active selects maximum fans. CPU and AMD
GPU hwmon paths are rediscovered after driver reset or device re-probe;
`nvidia-smi` and NVIDIA PCI runtime-status paths are rediscovered every 30
seconds. An NVIDIA GPU already in runtime suspend is not queried, avoiding a
telemetry-induced wake-up; polling resumes when its PCI runtime status becomes
active. Its expected lack of a temperature while powered down does not block a
Manual-to-Auto handoff. NVIDIA query failures and all sensor recoveries are
logged once per transition. The last valid NVIDIA metrics bridge up to two
consecutive query failures; the third failure selects the maximum-fan
fail-safe. Runtime suspend clears that cache so readings from an earlier
powered-on session cannot bridge a later wake-up.

Crash recovery is independent of Python cleanup. The systemd unit uses a
15-second watchdog and `ExecStopPost=... --failsafe`. If the process exits,
hangs, or receives `SIGKILL` during Manual, Max, or guarded Auto, the recovery
command selects maximum fans before systemd restarts the service. A requested
stop that completed a cool handoff leaves firmware Auto and no guard behind, so
the same command verifies Auto instead of escalating. A guard
marker in `/run/omen-fanctl/` makes this decision survive loss of the main
process. The service records its allowlisted board in the same directory once
it takes fan ownership, so recovery stays available on any validated board even
if the configuration file is damaged or removed while the fans are owned.

Neither marker outlives fan ownership. systemd discards the runtime directory
when the service stops, which a run detects through `RUNTIME_DIRECTORY`. A
manual `--apply` run owns no such directory and removes its own board marker
when it exits, as does a completed `--restore-auto` or `--failsafe`. That
removal happens only after the fan interface is confirmed to be in firmware
Auto or maximum mode: a run that ends with software control still active keeps
its marker, so the recovery that has to clean up is never locked out. The
daemon derives its heartbeat interval from systemd's
`WATCHDOG_USEC`, so long sensor sampling intervals do not starve the watchdog.

The executable `src/daemon/omen_fanctl.py` is a compatibility entry point.
Implementation is split by responsibility under `src/daemon/omen_fanctl/`:
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

### 1. Allowlist the board for testing

Every mode of the daemon refuses to start on an unlisted board, including
read-only runs. Read the board identifier:

```bash
cat /sys/class/dmi/id/board_name
```

Add that value to `allowed_boards` in the configuration used for testing. Work
on the repository copy, `src/config/omen-fanctl.toml`, rather than an installed
`/etc/omen-fanctl/omen-fanctl.toml`, so a partially validated board cannot
reach the systemd service:

```toml
[daemon]
allowed_boards = ["8D87", "8C99"]
```

The entry only permits the remaining steps to run. It does not assert that the
board is supported. Steps 3 to 6 run from the repository and need nothing else;
step 7 exercises the installed service, so add the board to
`/etc/omen-fanctl/omen-fanctl.toml` only once those earlier steps have
passed.

The recovery commands `--restore-auto` and `--failsafe` read the same list,
extended by the board that the running service recorded at startup. When their
configuration file cannot be parsed and no service has started, they fall back
to `8D87` alone.

### 2. Verify platform interfaces

```bash
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

### 3. Run read-only mode

```bash
python3 src/daemon/omen_fanctl.py \
  --config src/config/omen-fanctl.toml --duration 60
```

Review sensor selection, temperatures, requested PWM, and warnings. This mode
does not write fan controls.

### 4. Run the bounded actuator test

Only after the read-only output of the previous step has been reviewed:

```bash
sudo python3 src/daemon/omen_fanctl.py \
  --config src/config/omen-fanctl.toml \
  --apply --actuator-test 60 --duration 15
```

The test requests 60% PWM for 15 seconds and must restore
`pwm1_enable=2`. Stop testing if the fans do not ramp or Auto is not restored.

### 5. Validate CPU and GPU control

Follow the controller in one terminal:

```bash
journalctl -fu omen-fanctl.service
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

### 6. Validate profile handoff and Auto guard

While Manual control is active, stop the workload and select a profile other
than Performance. Verify this sequence in the journal:

```text
manual -> handoff -> auto-guard -> sleeping
```

During `auto-guard`, start a short workload. The daemon must immediately return
to `manual` or `handoff`. Stop the workload and confirm that a new 180-second
guard begins after the next return to Auto.

### 7. Validate process-failure recovery

Perform this test only after normal Manual control, cooldown, and Auto handoff
have been verified:

```bash
sudo systemctl kill --kill-whom=main --signal=SIGKILL \
  omen-fanctl.service
journalctl -u omen-fanctl.service --since=-1min --no-pager
```

The journal must show the killed process, `maximum fail-safe verified`, and a
new service process. The fan interface must transition directly to maximum
without an intermediate unsafe Auto handoff.

### 8. Validate the stop handoff

With the machine idle and Manual control active, stop the service:

```bash
sudo systemctl stop omen-fanctl.service
journalctl -u omen-fanctl.service --since=-1min --no-pager
cat /sys/class/hwmon/hwmon*/pwm1_enable
```

The journal must show `stop requested at ... C; returning the fans to firmware
Auto` followed by `firmware Auto verified`, and `pwm1_enable` must read `2`.
Repeat the same stop under a sustained workload above
`stop_handoff_max_temp_c`: that run must instead log
`selecting maximum fans` and `maximum fail-safe verified`.

Results for any additional board should include DMI and BIOS identifiers,
kernel version, hwmon channels, dry-run logs, actuator behavior, fan mapping,
CPU/GPU workload telemetry, profile handoff, Auto-guard behavior, and crash
recovery. Do not treat a board as supported, or leave it in the installed
`/etc/omen-fanctl/omen-fanctl.toml`, until every step above has passed, and
never add a board based only on a matching product family.

## License

This project is distributed under the [MIT License](LICENSE).
