# Standalone HP fan-control daemon

This is an experimental standalone automatic fan controller for HP system board
`8D87`. It uses the Linux `hp-wmi` hwmon interface for fan control and the
project's read-only WMI sensor probe for HP's IR temperature. It does not run,
link to, or depend on OmenCore.

Direct invocation defaults to read-only. Do not start with `--apply` or enable
the system service before checking the dry-run output.

## What it controls

- Reads CPU temperature from `k10temp`.
- Optionally reads the AMD GPU hwmon sensor and NVIDIA temperature through
  `nvidia-smi`.
- Optionally reads Gaming Hub's exact IR input (WMI group `0x20008`, query
  `0x23`, index `0`) from `/proc/hp_wmi_sensors` when the experimental probe is
  available.
- Evaluates CPU, GPU, and IR temperatures against separate curves and uses the
  highest resulting target. `acpitz` remains an opt-in diagnostic proxy only.
- Uses raw temperature for prompt ramp-up, and asymmetric EWMA plus the HP
  high/low thresholds for a slower ramp-down.
- Leaves BIOS Auto active while cool. Outside Performance, a controller that
  was already in Auto sleeps immediately; a Manual/Max controller first keeps
  cooling until the release thresholds are reached, selects Auto, and monitors
  the complete firmware fan-stop window before sleeping.
- While sleeping outside Performance, blocks on the kernel's `platform_profile`
  sysfs notification and does not query CPU/GPU/IR, invoke `nvidia-smi`, or run
  the control algorithm. A profile-change event wakes it immediately.
- Uses `pwm1_enable=1` and `pwm1` only when software control is needed.
- Uses `pwm1_enable=0` for immediate maximum fans at the raw critical threshold.
- Never switches a hot Manual/Max controller directly to firmware Auto.

The current kernel driver converts the standard hwmon PWM scale (`0..255`) to
the HP firmware fan-level scale and maps the second fan according to its fan
table. The daemon does not construct raw fan-control WMI packets itself.

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
probing is enabled by default but optional at runtime: a missing or invalid
probe produces one warning while CPU/GPU control continues, and IR joins
automatically if the probe appears. Neither available `acpitz` zone is the same
input; one can still be enabled explicitly with `--include-acpi-proxy` for
comparisons.
An IR reading below its first curve point does not hold the daemon in Manual
after a CPU/GPU-triggered cycle; IR release hysteresis is latched only after IR
itself reaches its activation point. Because WMI IR has whole-degree resolution,
its separate default release hysteresis is 1 C (activate at 42 C, release at or
below 41 C once both raw and EWMA readings cool sufficiently).
When NVIDIA telemetry is available, every sample also records
`nvidia_power_draw_w` and `nvidia_power_limit_w` from `nvidia-smi`. Empty values
mean that the dGPU was asleep or its driver did not expose the metric.

## 1. Run unit tests

```bash
cd fan-control-daemon-research/src
python3 -m unittest -v
```

## Install as a system service

The repository root contains explicit install and uninstall scripts. Installing
does not load the optional WMI IR probe. Existing configuration is never
overwritten. During an upgrade, `install.sh` stops an existing service before
replacing any files. For safety it refuses to stop an active controller until
that controller has completed its Auto guard, entered `state=sleeping`, and
`pwm1_enable=2`.

Install the files without starting fan control:

```bash
cd fan-control-daemon-research
sudo ./install.sh
sudoedit /etc/hp-fan-control/fan-control.toml
```

After reviewing the installed configuration, enable the daemon:

```bash
sudo systemctl enable --now hp-fan-control.service
systemctl --no-pager --full status hp-fan-control.service
journalctl -u hp-fan-control.service -f
```

Alternatively, a reviewed configuration can be installed and enabled in one
explicit step with `sudo ./install.sh --enable-now`. Timestamped full-resolution
CSV telemetry is written under `/var/log/hp-fan-control/` and rotated daily for
14 days. Periodic journal status is limited to once every 30 seconds, while
state transitions and warnings are logged immediately.

The systemd process remains resident so it can notice profile changes without
depending on desktop-specific hooks. Its fan controller is active only in
Performance. Under any other profile it blocks in `poll(POLLPRI)` only when
firmware Auto already owns the fans. If a profile change occurs during Manual
or Max, it enters `handoff`, continues reading CPU/GPU/IR and controlling the
fans, and selects Auto as soon as every active temperature reaches its release
threshold. Because the tested F.07 firmware can then stop both fans for roughly
90-120 seconds, the daemon enters `auto-guard` and continues sampling for
`auto_guard_s` (180 seconds by default). New heat immediately re-enters Manual;
after the next cooldown, Auto starts a fresh guard. Profile notifications remain
effective throughout Manual, `handoff`, and `auto-guard`; only a completed guard
outside Performance permits `state=sleeping`. Linux calls
`sysfs_notify` when the profile changes, so this does not poll the file. The
wait has a five-second timeout solely to send the systemd watchdog heartbeat;
the profile is re-read only after an actual notification. Event wake-up was
validated on the 8D87 with both `amd-pmf` and `hp-wmi` profile providers. The
notification originates in the upstream Linux
[`platform_profile` core](https://github.com/torvalds/linux/blob/v7.1/drivers/acpi/platform_profile.c#L383-L413).

The service sends `SIGTERM` on normal stop. If software still owns the fans,
the daemon selects maximum rather than triggering the firmware's fan-stop
window. `ExecStopPost` independently runs `--failsafe` after every stop: it
preserves a stable Auto state, but replaces Manual, Max, or a guarded Auto with
Max after an uncatchable `SIGKILL` and before any configured restart. The guard
is persisted under `/run/hp-fan-control/`, so it survives loss of the main
process. A restarted daemon can adopt Max and continue cooling. The recovery
command is restricted to board `8D87`
and does not depend on a readable configuration. The daemon also sends
systemd watchdog heartbeats; if its control loop stops making progress for 15
seconds, systemd kills it, runs the independent maximum-fan recovery, and
restarts it after five seconds.

Linux 7.1 `hp-wmi` provides the final firmware-backed layer. While Max or
Manual is selected, its kernel delayed work refreshes the HP user-defined fan
state every 90 seconds. If the kernel can no longer run that work, HP firmware
expires the state after 120 seconds and returns to its fallback fan state. This
firmware timeout is distinct from, and slower than, the normal systemd recovery
path. See the upstream `hp-wmi` implementation around
[`hp_wmi_apply_fan_settings`](https://github.com/torvalds/linux/blob/v7.1/drivers/platform/x86/hp/hp-wmi.c#L2248-L2290)
and its [keep-alive worker](https://github.com/torvalds/linux/blob/v7.1/drivers/platform/x86/hp/hp-wmi.c#L2423-L2440).

### Crash-recovery test

Test this only after the normal dry-run and actuator tests pass. Use a moderate
workload to make the installed service enter Manual, stop the workload, and
confirm `pwm1_enable=1`. Keep the independent hwmon watch visible, then kill
only the service's main process:

```bash
watch -n0.5 'grep . /sys/class/hwmon/hwmon*/{name,pwm1_enable,pwm1,fan1_input,fan2_input} 2>/dev/null'
```

```bash
sudo systemctl kill --kill-whom=main --signal=SIGKILL hp-fan-control.service
journalctl -u hp-fan-control.service --since=-1min --no-pager
systemctl --no-pager --full status hp-fan-control.service
```

The journal must show the killed main process, successful `maximum fail-safe
verified` recovery from `ExecStopPost`, and a fresh service start. The hwmon
view must change directly from Manual to Max (`pwm1_enable=0`) without passing
through Auto. The restarted daemon adopts Max, then follows its normal thermal
release and post-Auto guard rules.

To test a stuck loop, repeat under the same cooled, moderate conditions with
`SIGSTOP` instead. No further heartbeats can be sent, so the 15-second systemd
watchdog must kill the stopped process and execute the same Max/restart path:

```bash
sudo systemctl kill --kill-whom=main --signal=SIGSTOP hp-fan-control.service
```

Do not deliberately hang or crash the kernel to test the 120-second firmware
fallback.

Stop and remove the service while preserving configuration and telemetry:

```bash
sudo ./uninstall.sh
```

The uninstaller refuses to proceed during Auto guard or unless
`pwm1_enable=2`. Switch to Balanced and wait for the journal to report
`state=sleeping` first. This prevents an
uninstall from forcing the unsafe hot Manual-to-Auto transition. If a service
is intentionally stopped while Manual/Max is active, maximum fans remain as a
fail-safe; start the service again and let it complete a safe handoff.

Use `sudo ./uninstall.sh --purge-config` only when the installed configuration
should also be removed. Telemetry remains preserved in both modes.

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

To include the optional IR input, load and verify the read-only sensor probe:

```bash
cd fan-control-daemon-research/src
sudo ./probe_hp_ir_sensor.sh --load-only
```

Index 0 must appear as a valid row such as `0 IR 39`. Then run:

```bash
cd fan-control-daemon-research/src
python3 hp_fan_control.py --duration 60
```

Without the probe, the same command logs one warning and runs from CPU/GPU;
`ir` fields remain empty. In either case, this prints temperatures and decisions
without changing fan state. A timestamped CSV file is created in the current
directory.

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

Start the workload in another terminal. Stop the workload, switch to Balanced,
and wait for `state=sleeping` before stopping the controller. If Ctrl+C or the
duration limit stops it while Manual/Max is still active, it deliberately
leaves maximum fans selected instead of forcing an unsafe Auto transition.

Useful independent checks:

```bash
watch -n1 'cat /sys/firmware/acpi/platform_profile; grep . /sys/class/hwmon/hwmon*/{name,pwm1,pwm1_enable,fan1_input,fan2_input} 2>/dev/null'
```

Expected states:

| State | Meaning |
|---|---|
| `bios-auto` | No writes; firmware owns the curve |
| `manual` | Daemon owns the curve and writes intermediate PWM values |
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

- It is allowlisted only for board `8D87`.
- The extracted preset is confirmed only for the tested Vibrance/8D87 model;
  other boards must remain excluded until their matching Gaming Hub resource
  and fan-level behavior are verified.
- A daemon crash or `SIGKILL` cannot execute Python cleanup, so safe recovery
  depends on the service's `ExecStopPost`, which selects Max rather than Auto
  when userspace owned the fans or Auto was still inside its guarded fan-stop
  window. A stuck control loop is detected by
  the systemd watchdog. If userspace or the kernel cannot perform either path,
  the HP firmware's 120-second user-defined-state timeout is the last fallback;
  sudden power loss naturally cannot run any software cleanup.
- The WMI IR probe is an optional experimental extension. If it is absent,
  malformed, or lost later, the daemon logs the degraded state and continues
  safely from CPU/GPU; IR joins or rejoins automatically when available. Loss
  of the mandatory CPU source still invokes the maximum-fan fail-safe.
- The physical make/model of the IR sensor chip cannot be inferred from WMI.
- `acpitz` is not Gaming Hub's IR input and is disabled by default.
- Never run this daemon together with another fan-control program.
