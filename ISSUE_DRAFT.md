# Feature request: automatic software fan curves when BIOS Auto under-cools in Performance mode

## TL;DR

- On an HP OMEN MAX 16-ak0xxx (`8D87`, BIOS `F.07`), Linux Performance mode
  applies the expected power limits, including approximately 175 W dGPU power,
  but firmware Auto stops near 3,400/3,600 RPM and allows the CPU to reach
  92–99 C.
- HP OMEN Gaming Hub does more than select the Performance profile: while the
  machine is active, its Auto mode runs a userspace EWMA controller, evaluates
  separate CPU/GPU/IR curves, selects the highest request, maps it through the
  firmware fan table, and writes discrete fan levels through HP WMI.
- The exact factory Performance curves and the complete 128-byte `0x2f` fan
  mapping response have been extracted for this machine.
- Gaming Hub's IR input was identified as HP WMI `0x23`, sensor index `0`;
  neither Linux `acpitz` zone exposes the same reading on `8D87`.
- A standalone Linux prototype reproduced the factory curve through `hp-wmi`:
  fan speed increased to about 4,600/4,800 RPM and sustained CPU temperature
  stabilized around 85.5 C instead of the firmware-Auto result.
- Proposal: add an opt-in, safety-gated automatic curve controller to OmenCore,
  initially for explicitly validated systems.

## System and Linux reproduction

| Item | Value |
|---|---|
| Product | HP OMEN MAX Gaming Laptop 16-ak0xxx |
| Board / BIOS | `8D87` / `F.07` |
| Distribution / kernel | Arch Linux (Omarchy) / `7.1.9-arch1-2` |
| Platform profile | `performance` |
| Firmware fan mode | `pwm1_enable=2` (Auto) |
| Observed Auto ceiling | 3,400/3,600 RPM |
| Hardware maximum | approximately 6,000 RPM, verified with manual control |

Reproduction:

1. Select `performance` and verify `/sys/firmware/acpi/platform_profile`.
2. Leave `pwm1_enable=2`.
3. Apply a sustained CPU workload and record temperatures, PWM state, and RPM.

Representative stable samples:

```csv
elapsed_s,profile,cpu_c,gpu_c,acpi_c,fan1_rpm,fan2_rpm,pwm_enable,pwm_percent
0,performance,91.1,49,90.0,3400,3600,2,56.5
4,performance,91.5,50,90.0,3400,3600,2,56.5
8,performance,91.8,50,90.0,3400,3600,2,56.5
11,performance,92.0,50,90.0,3400,3600,2,56.5
```

The logger stopped at its 92 C safety threshold while the workload continued.
All 11 samples in the 90–94 C bin were exactly 3,400/3,600 RPM; another
60-second capture showed the same ceiling. The profile was active, and a
separate power-instrumented GPU test later recorded 174.6 W. The problem is
therefore not profile application or inability to command the fans: it is the
missing automatic software policy used by HP on Windows.

## What Gaming Hub does

The installed Windows package was
`AD2F1837.OMENCommandCenter 1101.2608.1.0 x64`. Its managed assemblies were
decompiled with ILSpyCmd 9.1; relevant code is in `PerformanceControl.dll`,
`HP.Omen.PerformanceControlModule.dll`,
`HP.Omen.Background.PerformanceControl.dll`, and
`HP.Omen.Core.Model.Device.dll`.

### Auto uses a userspace controller

`FanHandler.GetFanSetScheme` selects `EWMA_ALGO` even for Auto while the system
is not idle:

```csharp
case ThermalControl.Auto:
    if (!isIdle)
        return FanSetScheme.EWMA_ALGO;
    return FanSetScheme.IDLE_AUTO;
```

It evaluates separate inputs and takes their maximum:

```csharp
int cpu = GetFanTuning(cpuAdvanced, TuningDevice.Cpu);
int gpu = GetFanTuning(gpuAdvanced, TuningDevice.Gpu);
int ir  = GetFanTuning(irAdvanced,  TuningDevice.Ir);
target = Enumerable.Max(new int[3] { ir, cpu, gpu });
```

The inputs are EWMA-smoothed CPU, GPU, and IR/chassis temperatures. Rise and
fall coefficients are independent. CPU/GPU curves have separate high/low
thresholds; older tables without low thresholds use midpoints when decreasing.
The background loop samples every second, recalculates on an approximately
five-second cadence, and avoids redundant writes.

The selected CPU level is mapped to the other physical fans rather than copied
unchanged:

```csharp
List<int> sameLevelFans = SwFanControlTable.GetSameLevelFans(cpuFanSpeed);
if (sameLevelFans.Count > 1 && sameLevelFans[1] >= 0)
    gpuFanSpeed = sameLevelFans[1];
```

Gaming Hub's logs confirm the relevant state on this machine:

```text
8D87::ThisSystemID
IsSwFanControlSupport = True
GetThermalPolicyVersion = V1
enter GetSwFanControlTable()
leave GetSwFanControlTable(), rawTable is not null
FanType = Cpu
FanType = Gpu
Is3FanNb = False
ThermalMode = Auto
```

### IR sensor source on `8D87`

The device library reads four temperatures through HP WMI command group
`0x20008`, query `0x23`; the first input byte selects the sensor:

```csharp
public static int GetIRSensorValue() {
    return ChangeIrSensorToBoard ? GetSensorValue(1) : GetSensorValue(0);
}

private static int GetSensorValue(byte index) {
    byte[] input = { index, 0, 0, 0 };
    return _omenHsaClient.BiosWmiCmd_GetSync(131080, 35,
        input, input.Length, 4)[0];
}
```

The indices are `0 = IR`, `1 = Ambient`, `2 = PCH`, and `3 = VR`. Gaming Hub's
log for this machine explicitly says `ChangeIrSensorToBoard = False`, so its IR
curve uses index `0`.

A read-only Linux probe sampled the same WMI query alongside both ACPI thermal
zones during a two-minute 50% CPU load. WMI IR changed only `37 -> 39 C`, while
`thermal_zone0` changed `44 -> 66 C` and `thermal_zone1` changed `41 -> 49 C`.
Neither Linux `acpitz` zone is the IR value exposed to Gaming Hub. Zone 1 tracked
the WMI Ambient reading more closely, but this run does not establish that they
are the same firmware source.

## Exact factory Performance curve

Gaming Hub repeatedly selected this embedded configuration:

```text
LoadInitialConfig: Postfix = Vibrance_STX_N22X9
Load resource settings: HP.Omen.Core.Common.PowerControl.JSON.Vibrance_STX_N22X9.json
LoadResourceSettings: Resource version: 20250930
LoadInitialConfig: latestConfigName = Resource
NvGpuModel = NVIDIA GeForce RTX 5080 Laptop GPU
```

No Local or External configuration was selected. The matching resource was
extracted from `HP.Omen.Core.Common.dll`; decompiled mode-selection code assigns
its `SwFanControlCustomPerformance.FanTable` and coefficients in Performance.

| Sensor | Rising temperatures (C) | Falling temperatures (C) | Fan levels |
|---|---|---|---|
| CPU | 60,64,68,71,74,76,78,80,82,83,84,85 | 56,60,64,67,70,72,74,76,78,79,80,81 | 19,20,21,22,23,25,28,31,34,37,43,47 |
| GPU | 57,60,63,66,69,71,73,75,77,78,79,80 | 53,55,58,61,64,67,69,71,73,75,76,77 | 19,20,21,22,23,25,28,31,34,37,43,47 |
| IR | 42,44,46,48,50,52,54,56,58,60,62,64 | not provided | 19,20,21,22,23,25,28,31,34,37,43,47 |

`Lamda_Increase = 0.1`; `Lamda_Decrease = 0.05`. Normal Performance tops out
at level `47`; the separate Unleashed table reaches `60`. A logged local
`CustomFanCurve` (`50..90 C`, levels `20..36`) had `IsEnabled=false` and was not
the source of this behavior.

## HP WMI interface and captured fan mapping

Gaming Hub uses group `131080` / `0x20008` (`HPWMI_GM`):

| Purpose | Command | Direction |
|---|---:|---|
| Fan types/capabilities | `0x2c` | Get |
| Current levels | `0x2d` | Get |
| Software fan levels | `0x2e` | Set |
| Fan mapping table | `0x2f` | Get |
| Thermal/fan mode | `0x1a` | Set |

For `0x2e`, bytes 0 and 1 contain CPU and mapped GPU levels; byte 2 is the
optional third fan and the remainder is zero-filled:

```csharp
data[0] = Convert.ToByte(cpuFanLevel);
data[1] = Convert.ToByte(gpuFanLevel);
if (Is3FanNb)
    data[2] = Convert.ToByte(fan3);
```

`0x2d` returns current non-zero levels. `0x2c` identifies fan types in the low
and high nibbles of its first four bytes and capability flags in byte 8. For
thermal-policy V1, Gaming Hub maps Default/Eco to L2, Performance to L7, and
Cool to L4, then sends `0x1a`; observed L7 is `0x31`. Setting this mode alone
does not start the EWMA loop.

### Exact `0x2f` response from this BIOS

Both Gaming Hub and Linux parse byte 0 as fan count, leave byte 1 unspecified,
then read records containing one level per fan plus a noise/dB index until an
all-zero record. Linux 7.1 independently defines the same two-fan layout.

An eBPF probe captured the complete response while `hp_wmi` initialized. The
ACPI object was 136 bytes: an eight-byte HP response header followed by the
requested 128-byte payload.

```text
00000000  02 3c 13 15 17 14 16 19 16 17 1c 18 1a 1f 1c 1e
00000010  23 1e 20 25 22 24 28 24 26 2a 25 27 2b 2b 2d 2e
00000020  2f 31 30 32 34 32 35 37 33 38 3a 34 3c 3a 35 00
00000030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00000040  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00000050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00000060  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00000070  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

SHA-256:
`c9770ef1ffdaa25b7ef8aaa8c42757dbdff1aee07e22227e035e6d1c901a0e2f`.
Header byte 0 is `2`; byte 1 is `0x3c` (60), but neither parser assigns it a
meaning. The 15 records beginning at byte 2 are:

| CPU | GPU | Noise/dB | CPU | GPU | Noise/dB |
|---:|---:|---:|---:|---:|---:|
| 19 | 21 | 23 | 37 | 39 | 43 |
| 20 | 22 | 25 | 43 | 45 | 46 |
| 22 | 23 | 28 | 47 | 49 | 48 |
| 24 | 26 | 31 | 50 | 52 | 50 |
| 28 | 30 | 35 | 53 | 55 | 51 |
| 30 | 32 | 37 | 56 | 58 | 52 |
| 34 | 36 | 40 | 60 | 58 | 53 |
| 36 | 38 | 42 |  |  |  |

The terminator begins at offset `0x2f`; bytes through 127 are zero. The table
proves that CPU and GPU levels are not interchangeable—for example, CPU `47`
maps to GPU `49`, while CPU `60` maps to GPU `58`.

## Standalone Linux prototype

An independent Python prototype uses only the `hp-wmi` hwmon ABI; it neither
calls nor links to OmenCore. It implements:

- the extracted CPU/GPU/IR factory tables and asymmetric coefficients;
- maximum-of-available-sensors selection;
- raw temperature for immediate increases and EWMA/high-low hysteresis for
  decreases;
- configurable TOML curves, rate limits, profile gating, CSV telemetry, and
  NVIDIA power sampling;
- a raw 92 C emergency override to 100%;
- restoration of firmware Auto on exit, signal, profile change, or sensor
  failure.

The Linux `acpitz` sensors remain disabled by default: a direct WMI comparison
confirmed that neither is Gaming Hub's IR input on `8D87`.

The prototype can consume the confirmed index-0 value directly from the
read-only probe's `/proc/hp_wmi_sensors` interface. The probe is an optional
experimental extension: absent or invalid data removes IR from the maximum-of-
sensors decision while CPU/GPU control continues, including at startup. IR
joins or rejoins automatically when available; loss of the mandatory CPU
source still invokes the existing maximum-fan fail-safe.

### Actuator and controller-development tests

A bounded 60% test confirmed intermediate control and physical ramp time:

```text
elapsed   PWM feedback       fan 1 / fan 2
0 s         0 (0.0%)             0 /    0 RPM
3 s        42 (16.5%)         1000 / 1000 RPM
6 s        97 (38.0%)         2300 / 2400 RPM
9 s       136 (53.3%)         3200 / 3400 RPM
10 s      153 (60.0%)         3600 / 3700 RPM
11–15 s   153 (60.0%)         3600 / 3800 RPM
```

Auto was restored successfully. Five early CPU-only runs then identified two
timing requirements: EWMA alone lagged a raw rise from 74.0 to 93.8 C by about
two seconds, and a 65 C activation threshold started the fans too late. The
final logic uses the higher of raw/EWMA for increases, allows rising updates
every second, retains EWMA for decreases, and activates at 60 C.

| Run | Condition | Peak CPU | Sustained CPU | Result |
|---|---|---:|---:|---|
| 1 | EWMA-only rise | 99.4 C | 90–91 C | Exposed filtered-rise latency. |
| 2 | Profile toggled during load | 100.0 C | 90–92 C | Not comparable; fans began at 0 RPM. |
| 3 | Raw rise, activation at 65 C | 99.8 C | 90–91 C | Motivated earlier pre-spin. |
| 4 | Activation at 60 C | 97.5 C | 89–90 C | Removed 99 C samples. |
| 5 | Repeat without reapplying Performance | 97.8 C | about 89 C | Confirmed profile persistence after Auto restoration. |

Comparable runs used intermediate speeds, reached about
6,000/5,800–6,000 RPM when emergency was required, and restored Auto.

### Factory-curve validation

The final 175-second CPU workload used the exact factory Performance tables.
The ACPI proxy selected level 47 first, but CPU independently reached it at
85 C:

```text
elapsed   raw CPU   controller   request          fan 1 / fan 2
11.0 s     60.1 C   ACPI proxy    85 (33.3%)          0 /    0 RPM
14.3 s     79.0 C   CPU          119 (46.7%)       1000 / 1200 RPM
16.5 s     83.0 C   CPU          157 (61.6%)       2000 / 2000 RPM
18.7 s     83.9 C   ACPI proxy   200 (78.4%)       2700 / 2800 RPM
28.5 s     85.0 C   CPU          200 (78.4%)       4600 / 4800 RPM
```

PWM `200` represents factory level `47/60`; steady readback was `195`, likely
EC/driver quantization. CPU remained at 85.5–85.6 C, versus 92–99 C and
3,400/3,600 RPM in firmware Auto. Emergency was not needed, every sample
remained in `performance`, cooldown used intermediate levels, and Auto was
restored.

A clean CPU-only repeat excluded GPU/ACPI, reached 86.4 C and 4,600/4,800 RPM,
and selected CPU for all 202 controlled samples. A normal CPU+GPU-sensor repeat
reached 87.2/55 C and selected CPU for all 148 controlled samples. Neither run
entered emergency; both restored Auto.

### GPU and combined validation

In a simultaneous CPU/CUDA test with ACPI disabled, GPU initially won at 61 C.
CPU then crossed the independent safety threshold:

```text
elapsed   state       CPU/GPU       request       fan 1 / fan 2
47.9 s    manual      60.5/61 C      33.3%             0 /    0 RPM
63.1 s    manual      91.5/57 C      78.4%          3400 / 3600 RPM
64.3 s    emergency   92.4/57 C     100.0%          3800 / 4000 RPM
74.5 s    emergency   95.8/65 C     100.0%          6000 / 5800 RPM
88.0 s    emergency   98.2/68 C     100.0%          6000 / 5800 RPM
117.6 s   emergency   96.8/72 C     100.0%          6000 / 5800 RPM
```

At full fan speed, CPU remaining around 95–97 C reflects the machine's limit
under an unusually heavy synthetic load, not a failure to command fans.

A GPU-only run advanced the GPU target from level 19 to 28 at 74 C. CPU still
became the final controller because CUDA host overhead and shared cooling
raised it to 87.5 C. This exposed a brief Auto-return race while raw CPU was hot
but EWMA was cool; cleanup now requires both temperatures below release.

A power-instrumented repeat measured 174.6 W maximum dGPU draw, with 111
samples at or above 170 W and 119 at or above 160 W. `power.limit` was not
reported, so only actual draw is claimed. GPU peaked at 74 C. CPU reached
93.8 C through shared heat/host overhead, correctly invoking 6,000/6,000 RPM.

Across these tests, measurable RPM began about three seconds after the first
manual request and maximum speed could take roughly 20 seconds from pre-spin.
A temperature-only controller cannot eliminate an instantaneous synthetic
transient, but it removes the sustained firmware-Auto ceiling.

## Proposed OmenCore feature

Add an opt-in automatic controller with this control path:

1. Probe board identity, `0x2c` capabilities, and `0x2f`; reject unknown
   formats by default.
2. Sample CPU, GPU, and a proven chassis/IR sensor once per second.
3. Apply configurable asymmetric smoothing and per-sensor rise/fall curves.
4. Select the highest request and map physical fans through `0x2f`.
5. Write only changed levels at a conservative cadence; allow immediate rises.
6. Enable only for selected profiles and restore Auto on exit, profile change,
   suspend, service failure, reboot, or shutdown.

Daemon-enforced safety invariants should include strict curve validation,
model allowlisting, a raw critical-temperature override, sensor-loss handling,
a watchdog/fail-open path, rate limiting, and clear telemetry. Curve files must
not disable them, and userspace control does not replace hardware throttling or
shutdown.

The same backend can support custom curves through a small privileged daemon
and unprivileged CLI/TUI. Human-readable configuration should support
per-profile CPU/GPU/IR curves, import/export, live raw/filtered temperatures,
the winning curve, mapped levels/RPM, and offline CSV replay. Invalid changes
must be rejected atomically while the last known-good configuration remains
active. The standalone prototype already supports separate curves and the
factory preset in TOML; it is evidence, not proposed OmenCore code.

## Applicability beyond `8D87`

Linux 7.1 includes the same implementation for these Victus/OMEN-family IDs:

```text
8902  8A44  8A4D  8BAB  8BBE  8BC2  8BCA  8BCD  8BD4
8BD5  8C76  8C77  8C78  8C99  8C9C  8D41  8D87
```

This suggests broader applicability, not identical defects or mappings.
Unknown systems should start in read-only onboarding that records DMI/kernel,
hwmon channels/modes, `0x2c`, `0x2f`, and a bounded actuator/Auto-restore test.
Writes should require an explicit supported-device entry or successful
validation; board ID alone is insufficient.

Kernel references:

- Linux 7.1 `hp-wmi` implementation and quirks:
  <https://github.com/torvalds/linux/blob/v7.1/drivers/platform/x86/hp/hp-wmi.c#L2248-L2410>
- Current upstream PWM implementation:
  <https://github.com/torvalds/linux/blob/master/drivers/platform/x86/hp/hp-wmi.c#L2594-L2654>

## Remaining questions

- What `0x2f` variants exist on other supported generations?
- Can OmenCore already expose the full per-fan mapping, or is a backend API
  needed?
- Should curves be profile-indexed or run only in Performance?
- Does firmware require periodic refresh when the requested level is unchanged?
- What is the strongest practical recovery path for `SIGKILL` or a crash?
- Actuator/restoration behavior and mappings remain unvalidated outside `8D87`.

Everything central to this report is directly confirmed on `8D87`: active
Performance power limits, the firmware-Auto ceiling, Gaming Hub's userspace
algorithm and selected factory resource, WMI commands/payloads, exact `0x2f`,
intermediate and maximum actuation, factory-curve behavior, CPU/GPU/combined
loads, NVIDIA power draw, profile persistence, and clean Auto restoration.
