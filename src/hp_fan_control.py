#!/usr/bin/env python3
"""Experimental automatic fan controller for HP 8D87.

This prototype uses the Linux hp-wmi hwmon/sysfs ABI for fan control and the
read-only hp_wmi_sensor_probe procfs ABI for HP's IR temperature. It has no
OmenCore runtime dependency. Writes are disabled unless --apply is explicitly
supplied.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import logging
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable


LOG = logging.getLogger("hp-fan-control")
PWM_MAX = 255
AUTO_MODE = 2
MANUAL_MODE = 1
MAX_MODE = 0
HP_FAN_LEVEL_MAX = 60.0
AUTO_GUARD_PATH = Path("/run/hp-fan-control/auto-guard")


class ConfigurationError(ValueError):
    pass


class HardwareError(RuntimeError):
    pass


class SystemdNotifier:
    """Minimal sd_notify client; inert outside a systemd notify service."""

    def __init__(self, address: str | None):
        self.address = address
        self.failed = False

    @classmethod
    def from_environment(cls) -> "SystemdNotifier":
        address = os.environ.get("NOTIFY_SOCKET")
        watchdog_pid = os.environ.get("WATCHDOG_PID")
        if watchdog_pid:
            try:
                if int(watchdog_pid) != os.getpid():
                    address = None
            except ValueError:
                address = None
        if address and address.startswith("@"):
            address = "\0" + address[1:]
        return cls(address)

    def notify(self, message: str) -> None:
        if self.address is None or self.failed:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
                connection.sendto(message.encode("utf-8"), self.address)
        except OSError as exc:
            self.failed = True
            LOG.warning("cannot notify systemd watchdog: %s", exc)

    def ready(self) -> None:
        self.notify("READY=1")

    def watchdog(self) -> None:
        self.notify("WATCHDOG=1")

    def stopping(self) -> None:
        self.notify("STOPPING=1")


class PlatformProfileMonitor:
    """Keep a sysfs fd open and consume platform-profile notifications."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.handle = path.open("r", encoding="ascii")
            self.poller = select.poll()
            self.poller.register(
                self.handle.fileno(), select.POLLPRI | select.POLLERR
            )
            self.current = self._read()
        except OSError as exc:
            raise HardwareError(f"cannot monitor platform profile: {exc}") from exc

    def _read(self) -> str:
        try:
            self.handle.seek(0)
            return self.handle.read().strip()
        except OSError as exc:
            raise HardwareError(f"cannot read platform profile: {exc}") from exc

    def wait_for_change(self, timeout_s: float) -> bool:
        try:
            events = self.poller.poll(round(timeout_s * 1000))
        except OSError as exc:
            raise HardwareError(f"cannot wait for platform profile change: {exc}") from exc
        if not events:
            return False
        self.current = self._read()
        return True

    def close(self) -> None:
        self.handle.close()


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def percent_to_pwm(percent: float) -> int:
    return round(clamp(percent, 0.0, 100.0) * PWM_MAX / 100.0)


def pwm_to_percent(pwm: int) -> float:
    return pwm * 100.0 / PWM_MAX


@dataclass(frozen=True)
class Curve:
    temperatures: tuple[float, ...]
    pwm_percent: tuple[float, ...]
    fall_temperatures: tuple[float, ...] | None = None
    stepped: bool = False

    def __post_init__(self) -> None:
        if len(self.temperatures) != len(self.pwm_percent):
            raise ConfigurationError("curve temperature and PWM lists differ in length")
        if len(self.temperatures) < 2:
            raise ConfigurationError("curve needs at least two points")
        if any(b <= a for a, b in zip(self.temperatures, self.temperatures[1:])):
            raise ConfigurationError("curve temperatures must be strictly increasing")
        if any(b < a for a, b in zip(self.pwm_percent, self.pwm_percent[1:])):
            raise ConfigurationError("curve PWM values must be non-decreasing")
        if any(value < 0 or value > 100 for value in self.pwm_percent):
            raise ConfigurationError("curve PWM values must be between 0 and 100")
        if self.fall_temperatures is not None:
            if len(self.fall_temperatures) != len(self.temperatures):
                raise ConfigurationError(
                    "curve falling-temperature and PWM lists differ in length"
                )
            if any(
                b <= a
                for a, b in zip(
                    self.fall_temperatures, self.fall_temperatures[1:]
                )
            ):
                raise ConfigurationError(
                    "curve falling temperatures must be strictly increasing"
                )
            if any(
                low >= high
                for low, high in zip(self.fall_temperatures, self.temperatures)
            ):
                raise ConfigurationError(
                    "each falling temperature must be below its rising temperature"
                )

    def evaluate_percent(self, temperature: float) -> float:
        if self.stepped:
            index = 0
            for candidate, threshold in enumerate(self.temperatures):
                if temperature >= threshold:
                    index = candidate
                else:
                    break
            return self.pwm_percent[index]
        if temperature <= self.temperatures[0]:
            return self.pwm_percent[0]
        if temperature >= self.temperatures[-1]:
            return self.pwm_percent[-1]

        for left in range(len(self.temperatures) - 1):
            t0, t1 = self.temperatures[left], self.temperatures[left + 1]
            if t0 <= temperature <= t1:
                p0, p1 = self.pwm_percent[left], self.pwm_percent[left + 1]
                ratio = (temperature - t0) / (t1 - t0)
                return p0 + ratio * (p1 - p0)
        raise AssertionError("unreachable curve interval")

    def evaluate_pwm(self, temperature: float) -> int:
        return percent_to_pwm(self.evaluate_percent(temperature))

    def target_percent(
        self, temperature: float, previous_percent: float | None = None
    ) -> float:
        """Evaluate a curve, retaining a stepped level until its low threshold."""
        rising_target = self.evaluate_percent(temperature)
        if (
            previous_percent is None
            or not self.stepped
            or self.fall_temperatures is None
            or rising_target >= previous_percent
        ):
            return rising_target

        # Rate limiting can leave the global PWM between factory levels. Start
        # at the first factory level not lower than the prior sensor target.
        index = len(self.pwm_percent) - 1
        for candidate, level in enumerate(self.pwm_percent):
            if level >= previous_percent:
                index = candidate
                break
        while index > 0 and temperature < self.fall_temperatures[index]:
            index -= 1
        return self.pwm_percent[index]


def hp_level_percent(level: int) -> float:
    return level * 100.0 / HP_FAN_LEVEL_MAX


def hp_factory_performance_curves() -> dict[str, Curve]:
    """Factory OGH Vibrance_STX_N22X9 Performance tables (version 20250930)."""
    levels = tuple(
        hp_level_percent(value)
        for value in (19, 20, 21, 22, 23, 25, 28, 31, 34, 37, 43, 47)
    )
    return {
        "cpu": Curve(
            (60, 64, 68, 71, 74, 76, 78, 80, 82, 83, 84, 85),
            levels,
            (56, 60, 64, 67, 70, 72, 74, 76, 78, 79, 80, 81),
            stepped=True,
        ),
        "gpu": Curve(
            (57, 60, 63, 66, 69, 71, 73, 75, 77, 78, 79, 80),
            levels,
            (53, 55, 58, 61, 64, 67, 69, 71, 73, 75, 76, 77),
            stepped=True,
        ),
        "ir": Curve(
            (42, 44, 46, 48, 50, 52, 54, 56, 58, 60, 62, 64),
            levels,
            stepped=True,
        ),
    }


@dataclass
class Ewma:
    rise_alpha: float
    fall_alpha: float
    value: float | None = None

    def update(self, sample: float) -> float:
        if self.value is None:
            self.value = sample
        else:
            alpha = self.rise_alpha if sample >= self.value else self.fall_alpha
            self.value = alpha * sample + (1.0 - alpha) * self.value
        return self.value


@dataclass(frozen=True)
class Settings:
    allowed_boards: tuple[str, ...]
    required_profile: str
    sample_interval_s: float
    control_interval_s: float
    activation_temp_c: float
    release_temp_c: float
    critical_temp_c: float
    critical_release_temp_c: float
    emergency_hold_s: float
    decrease_hysteresis_c: float
    max_rise_percent_per_update: float
    max_fall_percent_per_update: float
    minimum_manual_percent: float
    ewma_rise_alpha: float
    ewma_fall_alpha: float
    include_acpi: bool
    include_amd_gpu: bool
    include_nvidia_gpu: bool
    curve: Curve
    ir_release_hysteresis_c: float = 1.0
    auto_guard_s: float = 180.0
    include_hp_wmi_ir: bool = True
    hp_wmi_sensors_path: Path = Path("/proc/hp_wmi_sensors")
    curves: dict[str, Curve] | None = None
    curve_source: str = "legacy-shared"

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"cannot load {path}: {exc}") from exc

        daemon = raw.get("daemon", {})
        ewma = raw.get("ewma", {})
        sensors = raw.get("sensors", {})
        curve_data = raw.get("curve", {})
        curves_data = raw.get("curves", {})
        try:
            preset = str(curves_data.get("preset", "")).strip()
            if preset:
                if preset != "hp-vibrance-stx-n22x9-performance":
                    raise ConfigurationError(f"unknown curve preset: {preset}")
                curves = hp_factory_performance_curves()
                curve = curves["cpu"]
                curve_source = preset
            else:
                named = {
                    name: cls._load_curve(curves_data[name])
                    for name in ("cpu", "gpu", "ir", "acpi")
                    if name in curves_data
                }
                if named:
                    curve = named.get("cpu", next(iter(named.values())))
                    curves = named
                    curve_source = "custom-per-sensor"
                else:
                    curve = cls._load_curve(curve_data)
                    curves = None
                    curve_source = "legacy-shared"
            settings = cls(
                allowed_boards=tuple(str(v) for v in daemon["allowed_boards"]),
                required_profile=str(daemon.get("required_profile", "performance")),
                sample_interval_s=float(daemon.get("sample_interval_s", 1.0)),
                control_interval_s=float(daemon.get("control_interval_s", 5.0)),
                activation_temp_c=float(daemon.get("activation_temp_c", 65.0)),
                release_temp_c=float(daemon.get("release_temp_c", 55.0)),
                critical_temp_c=float(daemon.get("critical_temp_c", 92.0)),
                critical_release_temp_c=float(
                    daemon.get("critical_release_temp_c", 82.0)
                ),
                emergency_hold_s=float(daemon.get("emergency_hold_s", 10.0)),
                decrease_hysteresis_c=float(
                    daemon.get("decrease_hysteresis_c", 3.0)
                ),
                max_rise_percent_per_update=float(
                    daemon.get("max_rise_percent_per_update", 20.0)
                ),
                max_fall_percent_per_update=float(
                    daemon.get("max_fall_percent_per_update", 8.0)
                ),
                minimum_manual_percent=float(
                    daemon.get("minimum_manual_percent", 35.0)
                ),
                ewma_rise_alpha=float(ewma.get("rise_alpha", 0.25)),
                ewma_fall_alpha=float(ewma.get("fall_alpha", 0.10)),
                include_acpi=bool(sensors.get("include_acpi", True)),
                include_amd_gpu=bool(sensors.get("include_amd_gpu", True)),
                include_nvidia_gpu=bool(sensors.get("include_nvidia_gpu", True)),
                curve=curve,
                ir_release_hysteresis_c=float(
                    daemon.get("ir_release_hysteresis_c", 1.0)
                ),
                auto_guard_s=float(
                    daemon.get("auto_guard_s", 180.0)
                ),
                include_hp_wmi_ir=bool(sensors.get("include_hp_wmi_ir", True)),
                hp_wmi_sensors_path=Path(
                    str(sensors.get("hp_wmi_sensors_path", "/proc/hp_wmi_sensors"))
                ),
                curves=curves,
                curve_source=curve_source,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid configuration: {exc}") from exc
        settings.validate()
        return settings

    @staticmethod
    def _load_curve(data: dict[str, object]) -> Curve:
        temperatures = data.get("high_temperature_c", data.get("temperature_c"))
        pwm_values = data.get("pwm_percent")
        levels = data.get("fan_level")
        if pwm_values is None and levels is not None:
            pwm_values = [hp_level_percent(int(v)) for v in levels]
        if temperatures is None or pwm_values is None:
            raise ConfigurationError("curve needs temperature_c and pwm_percent")
        low = data.get("low_temperature_c")
        return Curve(
            tuple(float(v) for v in temperatures),
            tuple(float(v) for v in pwm_values),
            None if low is None else tuple(float(v) for v in low),
            bool(data.get("stepped", False)),
        )

    def curve_for(self, sensor: str) -> Curve:
        if self.curves is None:
            return self.curve
        # acpitz is retained only as an opt-in diagnostic proxy for the IR
        # input and therefore uses the IR table unless explicitly configured.
        if sensor == "acpi":
            return self.curves.get("acpi", self.curves.get("ir", self.curve))
        return self.curves.get(sensor, self.curve)

    def validate(self) -> None:
        if not self.allowed_boards:
            raise ConfigurationError("allowed_boards must not be empty")
        if self.sample_interval_s < 0.25:
            raise ConfigurationError("sample_interval_s must be at least 0.25")
        if self.control_interval_s < self.sample_interval_s:
            raise ConfigurationError("control_interval_s must be >= sample_interval_s")
        if self.release_temp_c >= self.activation_temp_c:
            raise ConfigurationError("release_temp_c must be below activation_temp_c")
        if self.critical_release_temp_c >= self.critical_temp_c:
            raise ConfigurationError(
                "critical_release_temp_c must be below critical_temp_c"
            )
        if self.activation_temp_c >= self.critical_temp_c:
            raise ConfigurationError("activation_temp_c must be below critical_temp_c")
        if not 0 < self.ir_release_hysteresis_c < self.activation_temp_c:
            raise ConfigurationError(
                "ir_release_hysteresis_c must be positive and below activation_temp_c"
            )
        if self.auto_guard_s < 120:
            raise ConfigurationError("auto_guard_s must be at least 120 seconds")
        for name, value in (
            ("ewma.rise_alpha", self.ewma_rise_alpha),
            ("ewma.fall_alpha", self.ewma_fall_alpha),
        ):
            if not 0 < value <= 1:
                raise ConfigurationError(f"{name} must be in (0, 1]")
        for name, value in (
            ("minimum_manual_percent", self.minimum_manual_percent),
            ("max_rise_percent_per_update", self.max_rise_percent_per_update),
            ("max_fall_percent_per_update", self.max_fall_percent_per_update),
        ):
            if not 0 < value <= 100:
                raise ConfigurationError(f"{name} must be in (0, 100]")


def read_text(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def read_int(path: Path) -> int:
    try:
        return int(read_text(path))
    except (OSError, ValueError) as exc:
        raise HardwareError(f"cannot read integer from {path}: {exc}") from exc


def write_int(path: Path, value: int) -> None:
    try:
        path.write_text(f"{value}\n", encoding="ascii")
    except OSError as exc:
        raise HardwareError(f"cannot write {value} to {path}: {exc}") from exc


def find_hwmon(name: str, root: Path = Path("/sys/class/hwmon")) -> list[Path]:
    matches: list[Path] = []
    for directory in sorted(root.glob("hwmon*")):
        try:
            if read_text(directory / "name") == name:
                matches.append(directory)
        except OSError:
            continue
    return matches


def read_hwmon_temperatures(directory: Path) -> list[float]:
    values: list[float] = []
    for path in sorted(directory.glob("temp*_input")):
        try:
            raw = read_int(path)
        except HardwareError:
            continue
        value = raw / 1000.0
        if 1.0 <= value <= 125.0:
            values.append(value)
    return values


def read_hp_wmi_ir_temperature(path: Path) -> float:
    """Read index 0 (IR) from hp_wmi_sensor_probe's text procfs ABI."""
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise HardwareError(
            f"cannot read HP WMI IR sensor from {path}: {exc}"
        ) from exc

    if not lines or lines[0].split() != ["index", "name", "temp_c"]:
        raise HardwareError(f"invalid HP WMI sensor header in {path}")

    ir_values: list[float] = []
    for line in lines[1:]:
        fields = line.split()
        if not fields or fields[0] != "0":
            continue
        if len(fields) != 3 or fields[1] != "IR":
            raise HardwareError(f"invalid HP WMI IR row in {path}: {line!r}")
        try:
            value = float(fields[2])
        except ValueError as exc:
            raise HardwareError(
                f"HP WMI IR query failed in {path}: {fields[2]!r}"
            ) from exc
        if not 1.0 <= value <= 125.0:
            raise HardwareError(f"HP WMI IR temperature is out of range: {value}")
        ir_values.append(value)

    if len(ir_values) != 1:
        raise HardwareError(
            f"expected exactly one HP WMI IR row in {path}, found {len(ir_values)}"
        )
    return ir_values[0]


@dataclass(frozen=True)
class TemperatureSnapshot:
    cpu: float
    gpu: float | None
    acpi: float | None
    ir: float | None = None
    nvidia_power_draw_w: float | None = None
    nvidia_power_limit_w: float | None = None

    @property
    def raw_hottest(self) -> float:
        return max(
            v for v in (self.cpu, self.gpu, self.ir, self.acpi) if v is not None
        )


class Sensors:
    def __init__(self, settings: Settings, hwmon_root: Path = Path("/sys/class/hwmon")):
        self.settings = settings
        self.hwmon_root = hwmon_root
        cpu_matches = find_hwmon("k10temp", hwmon_root)
        if not cpu_matches:
            raise HardwareError("k10temp hwmon sensor was not found")
        self.cpu_hwmon = cpu_matches[0]
        self.amd_gpu_hwmons = (
            find_hwmon("amdgpu", hwmon_root) if settings.include_amd_gpu else []
        )
        self.nvidia_smi = (
            shutil.which("nvidia-smi") if settings.include_nvidia_gpu else None
        )
        self.hp_wmi_sensors_path = settings.hp_wmi_sensors_path
        self.hp_wmi_ir_failed = False

    def _cpu_temperature(self) -> float:
        values = read_hwmon_temperatures(self.cpu_hwmon)
        if not values:
            raise HardwareError("no valid k10temp temperature is available")
        return max(values)

    def _amd_gpu_temperature(self) -> float | None:
        values: list[float] = []
        for directory in self.amd_gpu_hwmons:
            values.extend(read_hwmon_temperatures(directory))
        return max(values) if values else None

    def _nvidia_metrics(
        self,
    ) -> tuple[float | None, float | None, float | None]:
        if not self.nvidia_smi:
            return None, None, None
        try:
            result = subprocess.run(
                [
                    self.nvidia_smi,
                    "--query-gpu=temperature.gpu,power.draw,power.limit",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None, None
        if result.returncode != 0:
            return None, None, None
        temperatures: list[float] = []
        power_draws: list[float] = []
        power_limits: list[float] = []
        for line in result.stdout.splitlines():
            fields = line.split(",")
            if len(fields) != 3:
                continue
            parsed: list[float | None] = []
            for field in fields:
                try:
                    parsed.append(float(field.strip()))
                except ValueError:
                    parsed.append(None)
            temperature, power_draw, power_limit = parsed
            if temperature is not None and 1.0 <= temperature <= 125.0:
                temperatures.append(temperature)
            if power_draw is not None and 0.0 <= power_draw <= 1000.0:
                power_draws.append(power_draw)
            if power_limit is not None and 1.0 <= power_limit <= 1000.0:
                power_limits.append(power_limit)
        return (
            max(temperatures) if temperatures else None,
            sum(power_draws) if power_draws else None,
            sum(power_limits) if power_limits else None,
        )

    def _acpi_temperature(self) -> float | None:
        if not self.settings.include_acpi:
            return None
        values: list[float] = []
        for directory in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                if read_text(directory / "type") != "acpitz":
                    continue
                value = read_int(directory / "temp") / 1000.0
            except (OSError, HardwareError):
                continue
            if 1.0 <= value <= 125.0:
                values.append(value)
        return max(values) if values else None

    def _hp_wmi_ir_temperature(self) -> float | None:
        if not self.settings.include_hp_wmi_ir:
            return None
        try:
            value = read_hp_wmi_ir_temperature(self.hp_wmi_sensors_path)
        except HardwareError as exc:
            if not self.hp_wmi_ir_failed:
                LOG.warning(
                    "optional HP WMI IR sensor unavailable; continuing with CPU/GPU: %s",
                    exc,
                )
            self.hp_wmi_ir_failed = True
            return None
        if self.hp_wmi_ir_failed:
            LOG.info("HP WMI IR sensor recovered")
        self.hp_wmi_ir_failed = False
        return value

    def read(self) -> TemperatureSnapshot:
        nvidia_temperature, power_draw, power_limit = self._nvidia_metrics()
        gpu_values = [self._amd_gpu_temperature(), nvidia_temperature]
        valid_gpu = [value for value in gpu_values if value is not None]
        return TemperatureSnapshot(
            cpu=self._cpu_temperature(),
            gpu=max(valid_gpu) if valid_gpu else None,
            acpi=self._acpi_temperature(),
            ir=self._hp_wmi_ir_temperature(),
            nvidia_power_draw_w=power_draw,
            nvidia_power_limit_w=power_limit,
        )


class HpFanHwmon:
    def __init__(self, root: Path = Path("/sys/class/hwmon")):
        matches = find_hwmon("hp", root)
        if len(matches) != 1:
            raise HardwareError(f"expected exactly one hp hwmon device, found {len(matches)}")
        self.path = matches[0]
        self.pwm = self.path / "pwm1"
        self.enable = self.path / "pwm1_enable"
        self.fan1 = self.path / "fan1_input"
        self.fan2 = self.path / "fan2_input"
        for required in (self.pwm, self.enable, self.fan1, self.fan2):
            if not required.exists():
                raise HardwareError(f"required hp-wmi attribute is missing: {required}")

    def status(self) -> tuple[int, int, int, int]:
        return (
            read_int(self.enable),
            read_int(self.pwm),
            read_int(self.fan1),
            read_int(self.fan2),
        )

    def set_manual(self, pwm: int) -> None:
        pwm = int(clamp(pwm, 1, PWM_MAX))
        # Linux 7.1 hp-wmi deliberately captures the current physical RPM when
        # switching Auto -> Manual, producing a smooth and non-zero transition.
        # pwm1 rejects writes outside Manual mode, so mode must be changed first.
        write_int(self.enable, MANUAL_MODE)
        try:
            write_int(self.pwm, pwm)
        except HardwareError:
            # The mode write may have succeeded even if the first PWM write
            # failed. Roll back immediately instead of leaving an unowned
            # Manual mode behind.
            try:
                self.restore_auto()
            except HardwareError as restore_error:
                LOG.critical(
                    "initial manual PWM write failed and Auto rollback also failed: %s",
                    restore_error,
                )
            raise

    def update_manual(self, pwm: int) -> None:
        pwm = int(clamp(pwm, 1, PWM_MAX))
        if read_int(self.enable) != MANUAL_MODE:
            raise HardwareError("manual fan mode was lost unexpectedly")
        write_int(self.pwm, pwm)

    def set_maximum(self) -> None:
        write_int(self.enable, MAX_MODE)

    def restore_auto(self) -> None:
        write_int(self.enable, AUTO_MODE)


class CsvLog:
    FIELDS = (
        "timestamp",
        "elapsed_s",
        "profile",
        "state",
        "cpu_raw_c",
        "gpu_raw_c",
        "nvidia_power_draw_w",
        "nvidia_power_limit_w",
        "ir_raw_c",
        "acpi_raw_c",
        "cpu_ewma_c",
        "gpu_ewma_c",
        "ir_ewma_c",
        "acpi_ewma_c",
        "hottest_control_c",
        "curve_source",
        "winning_sensor",
        "cpu_target_percent",
        "gpu_target_percent",
        "ir_target_percent",
        "acpi_target_percent",
        "requested_pwm",
        "requested_percent",
        "actual_mode",
        "actual_pwm",
        "fan1_rpm",
        "fan2_rpm",
        "note",
    )

    def __init__(self, path: Path | None):
        self.handle = None
        self.writer = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = path.open("w", encoding="utf-8", newline="")
            self.writer = csv.DictWriter(self.handle, fieldnames=self.FIELDS)
            self.writer.writeheader()
            self.handle.flush()

    def write(self, row: dict[str, object]) -> None:
        if self.writer is None:
            return
        self.writer.writerow(row)
        assert self.handle is not None
        self.handle.flush()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()


class Controller:
    def __init__(
        self,
        settings: Settings,
        fan: HpFanHwmon,
        sensors: Sensors,
        apply: bool,
        duration_s: float | None,
        csv_log: CsvLog,
        profile_path: Path = Path("/sys/firmware/acpi/platform_profile"),
        status_interval_s: float = 1.0,
        notifier: SystemdNotifier | None = None,
        inactive_event_wait_s: float = 5.0,
        auto_guard_path: Path | None = None,
    ):
        self.settings = settings
        self.fan = fan
        self.sensors = sensors
        self.apply = apply
        self.duration_s = duration_s
        self.csv_log = csv_log
        self.profile_path = profile_path
        self.status_interval_s = status_interval_s
        self.notifier = notifier or SystemdNotifier(None)
        self.profile_monitor: PlatformProfileMonitor | None = None
        self.inactive_event_wait_s = inactive_event_wait_s
        self.auto_guard_path = auto_guard_path
        self.next_status_log = 0.0
        self.last_status_state = ""
        self.last_status_note = ""
        self.stop_requested = False
        self.manual_active = False
        self.emergency = False
        self.emergency_since: float | None = None
        self.commanded_pwm: int | None = None
        self.auto_guard_until: float | None = None
        self.filters = {
            "cpu": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
            "gpu": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
            "ir": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
            "acpi": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
        }
        self.sensor_targets: dict[str, float | None] = {
            "cpu": None,
            "gpu": None,
            "ir": None,
            "acpi": None,
        }
        self.activated_sensors: set[str] = set()
        self.winning_sensor = ""

    def request_stop(self, signum: int, _frame: object) -> None:
        LOG.info("received signal %s", signum)
        self.stop_requested = True

    def _profile(self) -> str:
        if self.profile_monitor is None:
            raise HardwareError("platform profile monitor is not initialized")
        return self.profile_monitor.current

    def _filtered(self, snapshot: TemperatureSnapshot) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for name, value in (
            ("cpu", snapshot.cpu),
            ("gpu", snapshot.gpu),
            ("ir", snapshot.ir),
            ("acpi", snapshot.acpi),
        ):
            result[name] = None if value is None else self.filters[name].update(value)
        return result

    def _cool_enough_for_auto(
        self,
        snapshot: TemperatureSnapshot,
        filtered: dict[str, float | None],
    ) -> bool:
        raw = {
            "cpu": snapshot.cpu,
            "gpu": snapshot.gpu,
            "ir": snapshot.ir,
            "acpi": snapshot.acpi,
        }
        for name, filtered_value in filtered.items():
            raw_value = raw[name]
            if raw_value is None or filtered_value is None:
                continue
            # IR/acpitz use much lower curve temperatures than CPU/GPU. Do not
            # let a cool sensor that never activated control prevent a return
            # to firmware Auto after another sensor caused the Manual cycle.
            if name in ("ir", "acpi") and name not in self.activated_sensors:
                continue
            release = self.settings.release_temp_c
            if name in ("ir", "acpi"):
                curve = self.settings.curve_for(name)
                activation = min(
                    self.settings.activation_temp_c, curve.temperatures[0]
                )
                release = min(
                    release,
                    activation - self.settings.ir_release_hysteresis_c,
                )
            if raw_value > release or filtered_value > release:
                return False
        return True

    def _activation_sources(self, snapshot: TemperatureSnapshot) -> set[str]:
        raw = {
            "cpu": snapshot.cpu,
            "gpu": snapshot.gpu,
            "ir": snapshot.ir,
            "acpi": snapshot.acpi,
        }
        return {
            name
            for name, value in raw.items()
            if value is not None
            and value >= (
                min(
                    self.settings.activation_temp_c,
                    self.settings.curve_for(name).temperatures[0],
                )
                if name in ("ir", "acpi")
                else self.settings.activation_temp_c
            )
        }

    def _should_activate(self, snapshot: TemperatureSnapshot) -> bool:
        return bool(self._activation_sources(snapshot))

    def _desired_pwm(
        self,
        filtered: dict[str, float | None],
        raw_hottest: float | None = None,
        raw_temperatures: dict[str, float | None] | None = None,
    ) -> tuple[int, float]:
        temperatures = [value for value in filtered.values() if value is not None]
        # Raw temperature gives prompt fan ramp-up. The filtered value remains
        # higher during cooldown and therefore controls the slower ramp-down.
        hottest = max(temperatures)
        if raw_hottest is not None:
            hottest = max(hottest, raw_hottest)
        if raw_hottest is not None and raw_temperatures is None:
            raw_temperatures = {
                max(
                    (name for name, value in filtered.items() if value is not None),
                    key=lambda name: filtered[name],
                ): raw_hottest
            }

        targets: dict[str, float] = {}
        for name, filtered_temperature in filtered.items():
            if filtered_temperature is None:
                self.sensor_targets[name] = None
                continue
            raw_temperature = (
                None if raw_temperatures is None else raw_temperatures.get(name)
            )
            evaluating = max(
                filtered_temperature,
                filtered_temperature if raw_temperature is None else raw_temperature,
            )
            curve = self.settings.curve_for(name)
            previous = self.sensor_targets[name]
            target = curve.target_percent(evaluating, previous)

            # Custom/legacy linear curves use the original generic decrease
            # hysteresis. Factory stepped CPU/GPU tables use their own lows.
            if (
                previous is not None
                and target < previous
                and curve.fall_temperatures is None
            ):
                target = curve.target_percent(
                    filtered_temperature + self.settings.decrease_hysteresis_c,
                    previous,
                )
            self.sensor_targets[name] = target
            targets[name] = target

        if not targets:
            raise HardwareError("no valid temperature is available for fan control")
        self.winning_sensor = max(targets, key=targets.get)
        candidate = percent_to_pwm(targets[self.winning_sensor])

        minimum = percent_to_pwm(self.settings.minimum_manual_percent)
        candidate = max(candidate, minimum)

        if self.commanded_pwm is not None:
            rise = percent_to_pwm(self.settings.max_rise_percent_per_update)
            fall = percent_to_pwm(self.settings.max_fall_percent_per_update)
            candidate = min(candidate, self.commanded_pwm + rise)
            candidate = max(candidate, self.commanded_pwm - fall)
        return int(clamp(candidate, 1, PWM_MAX)), hottest

    def _apply_manual(self, pwm: int) -> int:
        if not self.apply:
            self.manual_active = True
            self.commanded_pwm = pwm
            self._clear_auto_guard()
            return pwm
        if not self.manual_active:
            # Never reduce airflow at the Auto -> Manual boundary. hp-wmi's
            # pwm1 read reflects the current CPU fan level even in Auto mode.
            _, current_pwm, _, _ = self.fan.status()
            pwm = max(pwm, current_pwm)
            self.fan.set_manual(pwm)
            self.manual_active = True
            self._clear_auto_guard()
        elif pwm != self.commanded_pwm:
            self.fan.update_manual(pwm)
        self.commanded_pwm = pwm
        return pwm

    def _maximum(self) -> None:
        if self.apply:
            mode, _, _, _ = self.fan.status()
            if mode != MAX_MODE:
                self.fan.set_maximum()
        self.manual_active = True
        self.commanded_pwm = PWM_MAX
        self._clear_auto_guard()

    def _start_auto_guard(self, now: float) -> None:
        self.auto_guard_until = now + self.settings.auto_guard_s
        if self.auto_guard_path is not None:
            try:
                self.auto_guard_path.write_text(
                    f"{self.auto_guard_until:.6f}\n", encoding="ascii"
                )
            except OSError as exc:
                raise HardwareError(
                    f"cannot persist firmware Auto guard: {exc}"
                ) from exc

    def _clear_auto_guard(self) -> None:
        self.auto_guard_until = None
        if self.auto_guard_path is not None:
            try:
                self.auto_guard_path.unlink(missing_ok=True)
            except OSError as exc:
                raise HardwareError(
                    f"cannot clear firmware Auto guard: {exc}"
                ) from exc

    def _auto_guard_active(self, now: float) -> bool:
        if self.auto_guard_until is None:
            return False
        if now < self.auto_guard_until:
            return True
        self._clear_auto_guard()
        return False

    def _restore_auto(self, reason: str, now: float) -> None:
        if not (self.manual_active or self.emergency):
            return
        LOG.info("restoring firmware Auto: %s", reason)
        if self.apply:
            self.fan.restore_auto()
        self.manual_active = False
        self.emergency = False
        self.emergency_since = None
        self.commanded_pwm = None
        self.sensor_targets = {"cpu": None, "gpu": None, "ir": None, "acpi": None}
        self.activated_sensors.clear()
        self.winning_sensor = ""
        self._start_auto_guard(now)

    def _log_sample(
        self,
        started: float,
        profile: str,
        state: str,
        snapshot: TemperatureSnapshot,
        filtered: dict[str, float | None],
        hottest: float,
        requested: int | None,
        note: str = "",
    ) -> None:
        try:
            mode, actual_pwm, fan1, fan2 = self.fan.status()
        except HardwareError:
            mode = actual_pwm = fan1 = fan2 = -1

        def fmt(value: float | None) -> str:
            return "" if value is None else f"{value:.1f}"

        now = time.monotonic()
        if (
            state != self.last_status_state
            or note != self.last_status_note
            or now >= self.next_status_log
        ):
            LOG.info(
                "state=%-9s profile=%-11s CPU=%5.1f GPU=%5s GPUW=%6s IR=%5s ACPI=%5s "
                "control=%5.1f winner=%-4s request=%3s (%5s%%) "
                "actual=%d/%d fans=%d/%d%s",
                state,
                profile,
                snapshot.cpu,
                fmt(snapshot.gpu) or "n/a",
                fmt(snapshot.nvidia_power_draw_w) or "n/a",
                fmt(snapshot.ir) or "n/a",
                fmt(snapshot.acpi) or "n/a",
                hottest,
                self.winning_sensor or "-",
                "-" if requested is None else requested,
                "-" if requested is None else f"{pwm_to_percent(requested):.1f}",
                mode,
                actual_pwm,
                fan1,
                fan2,
                f" note={note}" if note else "",
            )
            self.last_status_state = state
            self.last_status_note = note
            self.next_status_log = now + self.status_interval_s
        self.csv_log.write(
            {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "elapsed_s": f"{time.monotonic() - started:.1f}",
                "profile": profile,
                "state": state,
                "cpu_raw_c": fmt(snapshot.cpu),
                "gpu_raw_c": fmt(snapshot.gpu),
                "nvidia_power_draw_w": fmt(snapshot.nvidia_power_draw_w),
                "nvidia_power_limit_w": fmt(snapshot.nvidia_power_limit_w),
                "ir_raw_c": fmt(snapshot.ir),
                "acpi_raw_c": fmt(snapshot.acpi),
                "cpu_ewma_c": fmt(filtered["cpu"]),
                "gpu_ewma_c": fmt(filtered["gpu"]),
                "ir_ewma_c": fmt(filtered["ir"]),
                "acpi_ewma_c": fmt(filtered["acpi"]),
                "hottest_control_c": f"{hottest:.1f}",
                "curve_source": self.settings.curve_source,
                "winning_sensor": self.winning_sensor,
                "cpu_target_percent": fmt(self.sensor_targets["cpu"]),
                "gpu_target_percent": fmt(self.sensor_targets["gpu"]),
                "ir_target_percent": fmt(self.sensor_targets["ir"]),
                "acpi_target_percent": fmt(self.sensor_targets["acpi"]),
                "requested_pwm": "" if requested is None else requested,
                "requested_percent": (
                    "" if requested is None else f"{pwm_to_percent(requested):.1f}"
                ),
                "actual_mode": mode,
                "actual_pwm": actual_pwm,
                "fan1_rpm": fan1,
                "fan2_rpm": fan2,
                "note": note,
            }
        )

    def run(self) -> None:
        started = time.monotonic()
        next_control = started
        if self.apply:
            initial_mode, _, _, _ = self.fan.status()
            if initial_mode == MAX_MODE:
                self.manual_active = True
                self.emergency = True
                self.emergency_since = started
                self.commanded_pwm = PWM_MAX
                LOG.warning("adopting maximum-fan fail-safe from previous service run")
            elif initial_mode != AUTO_MODE:
                raise HardwareError(
                    f"expected firmware Auto or fail-safe Max, found {initial_mode}; "
                    "another controller may be active"
                )

        self.profile_monitor = PlatformProfileMonitor(self.profile_path)
        self.notifier.ready()

        try:
            while not self.stop_requested:
                self.notifier.watchdog()
                now = time.monotonic()
                if self.duration_s is not None and now - started >= self.duration_s:
                    LOG.info("configured duration completed")
                    break

                profile = self._profile()
                outside_required_profile = profile != self.settings.required_profile
                auto_guard_active = self._auto_guard_active(now)
                if outside_required_profile and not (
                    self.manual_active or self.emergency or auto_guard_active
                ):
                    for temperature_filter in self.filters.values():
                        temperature_filter.value = None
                    if self.last_status_state != "sleeping":
                        LOG.info(
                            "state=sleeping profile=%s; waiting for %s",
                            profile,
                            self.settings.required_profile,
                        )
                        self.last_status_state = "sleeping"
                    self.profile_monitor.wait_for_change(
                        self.inactive_event_wait_s
                    )
                    continue

                try:
                    snapshot = self.sensors.read()
                except HardwareError as exc:
                    if self.manual_active or self.emergency or auto_guard_active:
                        LOG.error("sensor failure during control; selecting maximum: %s", exc)
                        self._maximum()
                        self.emergency = True
                        self.emergency_since = self.emergency_since or now
                    else:
                        LOG.error("sensor failure while BIOS Auto is active: %s", exc)
                    self.profile_monitor.wait_for_change(
                        self.settings.sample_interval_s
                    )
                    continue

                filtered = self._filtered(snapshot)
                hottest = max(v for v in filtered.values() if v is not None)
                note = ""

                self.activated_sensors.update(
                    self._activation_sources(snapshot)
                )

                if snapshot.raw_hottest >= self.settings.critical_temp_c:
                    if not self.emergency:
                        LOG.warning(
                            "critical raw temperature %.1f C; selecting maximum fans",
                            snapshot.raw_hottest,
                        )
                    self._maximum()
                    self.emergency = True
                    self.emergency_since = self.emergency_since or now
                    state = "emergency"
                    requested = PWM_MAX
                    note = "raw critical threshold"
                elif self.emergency:
                    held_long_enough = (
                        self.emergency_since is not None
                        and now - self.emergency_since >= self.settings.emergency_hold_s
                    )
                    if hottest <= self.settings.critical_release_temp_c and held_long_enough:
                        self.emergency = False
                        self.manual_active = False
                        self.commanded_pwm = None
                        self._clear_auto_guard()
                        self.sensor_targets = {
                            "cpu": None,
                            "gpu": None,
                            "ir": None,
                            "acpi": None,
                        }
                        self.activated_sensors = self._activation_sources(snapshot)
                        requested, hottest = self._desired_pwm(filtered)
                        requested = self._apply_manual(requested)
                        state = "manual"
                        note = "left emergency state"
                    else:
                        self._maximum()
                        state = "emergency"
                        requested = PWM_MAX
                        note = "waiting for critical release"
                elif (
                    not self.manual_active
                    and not self._should_activate(snapshot)
                ):
                    state = "auto-guard" if auto_guard_active else "bios-auto"
                    requested = None
                    if auto_guard_active:
                        note = "monitoring firmware fan-stop window"
                elif self.manual_active and self._cool_enough_for_auto(
                    snapshot, filtered
                ):
                    reason = "temperatures returned below release thresholds"
                    if outside_required_profile:
                        reason += f" for profile {profile}"
                    self._restore_auto(reason, now)
                    auto_guard_active = True
                    state = "auto-guard"
                    requested = None
                    note = "monitoring firmware fan-stop window"
                else:
                    candidate, hottest = self._desired_pwm(
                        filtered,
                        snapshot.raw_hottest,
                        {
                            "cpu": snapshot.cpu,
                            "gpu": snapshot.gpu,
                            "ir": snapshot.ir,
                            "acpi": snapshot.acpi,
                        },
                    )
                    # Heating may raise the target on every sample. Fan-speed
                    # reductions remain limited to the normal control cadence.
                    should_update = (
                        self.commanded_pwm is None
                        or candidate > self.commanded_pwm
                        or now >= next_control
                    )
                    if should_update:
                        requested = self._apply_manual(candidate)
                        next_control = now + self.settings.control_interval_s
                    else:
                        requested = self.commanded_pwm
                    state = "handoff" if outside_required_profile else "manual"
                    if outside_required_profile:
                        note = "cooling before firmware Auto"

                self._log_sample(
                    started, profile, state, snapshot, filtered, hottest, requested, note
                )
                self.profile_monitor.wait_for_change(
                    self.settings.sample_interval_s
                )
        finally:
            try:
                guard_active_at_stop = self._auto_guard_active(time.monotonic())
                if self.apply and (
                    self.manual_active or self.emergency or guard_active_at_stop
                ):
                    LOG.critical(
                        "controller stopped while software cooling or Auto guard "
                        "was active; "
                        "selecting maximum fans"
                    )
                    self.fan.set_maximum()
                    self._clear_auto_guard()
            except HardwareError as exc:
                LOG.critical("FAILED TO SELECT MAXIMUM FANS: %s", exc)
            self.notifier.stopping()
            self.profile_monitor.close()


def acquire_lock(path: Path) -> object:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="ascii")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise HardwareError(f"another controller holds {path}") from exc
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    source_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Experimental standalone automatic fan controller for HP 8D87"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=source_dir / "fan-control.toml",
        help="configuration file",
    )
    operation_group = parser.add_mutually_exclusive_group()
    operation_group.add_argument(
        "--apply",
        action="store_true",
        help="actually write fan controls (default is dry-run)",
    )
    operation_group.add_argument(
        "--restore-auto",
        action="store_true",
        help="force firmware Auto and exit (unsafe while the machine is hot)",
    )
    operation_group.add_argument(
        "--failsafe",
        action="store_true",
        help="preserve Auto or replace a userspace-owned mode with maximum fans",
    )
    parser.add_argument(
        "--actuator-test",
        type=float,
        metavar="PERCENT",
        help="hold a fixed PWM for a short hardware test, then restore Auto",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many seconds; hot Manual/Max exits fail safe to Max",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="CSV output path (default: timestamped file in the current directory)",
    )
    parser.add_argument(
        "--no-log-file", action="store_true", help="disable CSV output"
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=1.0,
        help="seconds between periodic console/journal status lines",
    )
    sensor_group = parser.add_mutually_exclusive_group()
    sensor_group.add_argument(
        "--cpu-only",
        action="store_true",
        help="ignore GPU, WMI IR, and ACPI sensors (controlled CPU test mode)",
    )
    sensor_group.add_argument(
        "--include-acpi-proxy",
        action="store_true",
        help="also evaluate acpitz as an experimental proxy for HP IR",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def run_actuator_test(
    fan: HpFanHwmon, sensors: Sensors, percent: float, duration_s: float
) -> None:
    if not 35.0 <= percent <= 100.0:
        raise ConfigurationError("--actuator-test must be between 35 and 100 percent")
    if not 1.0 <= duration_s <= 60.0:
        raise ConfigurationError("actuator-test duration must be between 1 and 60 seconds")
    mode, current_pwm, fan1, fan2 = fan.status()
    if mode != AUTO_MODE:
        raise HardwareError(
            f"actuator test requires firmware Auto (pwm1_enable=2), found {mode}"
        )

    target = percent_to_pwm(percent)
    # Do not reduce airflow when entering the test.
    target = max(target, current_pwm)
    stop = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop
        LOG.info("received signal %s", signum)
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    LOG.warning(
        "actuator test: current=%d (%.1f%%), requested=%d (%.1f%%), fans=%d/%d",
        current_pwm,
        pwm_to_percent(current_pwm),
        target,
        pwm_to_percent(target),
        fan1,
        fan2,
    )
    started = time.monotonic()
    try:
        fan.set_manual(target)
        while not stop and time.monotonic() - started < duration_s:
            snapshot = sensors.read()
            mode, actual_pwm, fan1, fan2 = fan.status()
            LOG.info(
                "test mode=%d pwm=%d (%.1f%%) fans=%d/%d CPU=%.1f GPU=%s IR=%s ACPI=%s",
                mode,
                actual_pwm,
                pwm_to_percent(actual_pwm),
                fan1,
                fan2,
                snapshot.cpu,
                "n/a" if snapshot.gpu is None else f"{snapshot.gpu:.1f}",
                "n/a" if snapshot.ir is None else f"{snapshot.ir:.1f}",
                "n/a" if snapshot.acpi is None else f"{snapshot.acpi:.1f}",
            )
            time.sleep(1.0)
    finally:
        LOG.info("actuator test finished; restoring firmware Auto")
        fan.restore_auto()
        restored_mode, _, restored_fan1, restored_fan2 = fan.status()
        if restored_mode != AUTO_MODE:
            raise HardwareError(
                f"failed to verify firmware Auto after test: mode={restored_mode}"
            )
        LOG.info("firmware Auto restored; fans=%d/%d", restored_fan1, restored_fan2)


def restore_firmware_auto(fan: HpFanHwmon) -> None:
    mode, _, _, _ = fan.status()
    if mode != AUTO_MODE:
        fan.restore_auto()
    restored_mode, _, fan1, fan2 = fan.status()
    if restored_mode != AUTO_MODE:
        raise HardwareError(
            f"failed to verify firmware Auto: mode={restored_mode}"
        )
    LOG.info("firmware Auto verified; fans=%d/%d", fan1, fan2)


def ensure_failsafe_fan_state(
    fan: HpFanHwmon,
    auto_guard_path: Path | None = None,
) -> None:
    """Preserve stable Auto; turn owned or guarded fan states into Max."""
    mode, _, _, _ = fan.status()
    guarded_auto = auto_guard_path is not None and auto_guard_path.exists()
    if mode != AUTO_MODE or guarded_auto:
        fan.set_maximum()
        if auto_guard_path is not None:
            try:
                auto_guard_path.unlink(missing_ok=True)
            except OSError as exc:
                raise HardwareError(
                    f"cannot clear firmware Auto guard during recovery: {exc}"
                ) from exc
    safe_mode, _, fan1, fan2 = fan.status()
    if safe_mode not in (AUTO_MODE, MAX_MODE):
        raise HardwareError(
            f"failed to establish Auto or maximum fail-safe: mode={safe_mode}"
        )
    state = "firmware Auto" if safe_mode == AUTO_MODE else "maximum fail-safe"
    LOG.info("%s verified; fans=%d/%d", state, fan1, fan2)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if args.restore_auto or args.failsafe:
            board = read_text(Path("/sys/class/dmi/id/board_name"))
            if board != "8D87":
                raise HardwareError(
                    f"fan recovery is only allowed on board 8D87, found {board!r}"
                )
            if os.geteuid() != 0:
                raise HardwareError("fan-control writes must be run as root (use sudo)")
            lock_handle = acquire_lock(Path("/run/hp-fan-control/control.lock"))
            fan = HpFanHwmon()
            if args.failsafe:
                ensure_failsafe_fan_state(fan, AUTO_GUARD_PATH)
            else:
                restore_firmware_auto(fan)
            lock_handle.close()
            return 0

        settings = Settings.load(args.config)
        if args.cpu_only:
            settings = replace(
                settings,
                include_acpi=False,
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                include_hp_wmi_ir=False,
            )
        elif args.include_acpi_proxy:
            settings = replace(settings, include_acpi=True)
        board = read_text(Path("/sys/class/dmi/id/board_name"))
        if board not in settings.allowed_boards:
            raise HardwareError(
                f"board {board!r} is not allowlisted: {settings.allowed_boards}"
            )
        if args.apply and os.geteuid() != 0:
            raise HardwareError("fan-control writes must be run as root (use sudo)")
        if args.duration is not None and args.duration <= 0:
            raise ConfigurationError("--duration must be positive")
        if args.status_interval < settings.sample_interval_s:
            raise ConfigurationError(
                "--status-interval must be >= daemon sample_interval_s"
            )

        lock_path = Path("/run/hp-fan-control/control.lock")
        if not args.apply:
            lock_path = Path("/tmp/hp-fan-control-dry-run.lock")
        # Keep the file object alive for the lifetime of main; closing it releases
        # the advisory lock.
        lock_handle = acquire_lock(lock_path)

        fan = HpFanHwmon()
        sensors = Sensors(settings)
        if args.actuator_test is not None:
            if not args.apply:
                raise ConfigurationError("--actuator-test also requires --apply")
            test_duration = 15.0 if args.duration is None else args.duration
            run_actuator_test(fan, sensors, args.actuator_test, test_duration)
            lock_handle.close()
            return 0
        if args.no_log_file:
            log_path = None
        elif args.log_file:
            log_path = args.log_file
        else:
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            log_path = Path.cwd() / f"hp-fan-control-{stamp}.csv"
        csv_log = CsvLog(log_path)

        LOG.warning(
            "%s mode; board=%s curves=%s hp_hwmon=%s log=%s",
            "APPLY" if args.apply else "DRY-RUN",
            board,
            settings.curve_source,
            fan.path,
            log_path or "disabled",
        )
        controller = Controller(
            settings=settings,
            fan=fan,
            sensors=sensors,
            apply=args.apply,
            duration_s=args.duration,
            csv_log=csv_log,
            status_interval_s=args.status_interval,
            notifier=SystemdNotifier.from_environment(),
            auto_guard_path=AUTO_GUARD_PATH if args.apply else None,
        )
        signal.signal(signal.SIGINT, controller.request_stop)
        signal.signal(signal.SIGTERM, controller.request_stop)
        try:
            controller.run()
        finally:
            csv_log.close()
        lock_handle.close()
        return 0
    except (ConfigurationError, HardwareError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
