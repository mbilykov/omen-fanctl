"""Hardware adapters for temperature sensors, hp-wmi, and profiles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
import select
import shutil
import subprocess
import time

from .config import ConfigurationError, PWM_MAX, Settings, clamp


LOG = logging.getLogger("hp-fan-control")
AUTO_MODE = 2
MANUAL_MODE = 1
MAX_MODE = 0
PLATFORM_PROFILE_CHOICES_PATH = Path(
    "/sys/firmware/acpi/platform_profile_choices"
)
CONTROL_SENSORS = ("cpu", "gpu", "ir")
HP_HWMON_STARTUP_TIMEOUT_S = 20.0
HP_HWMON_STARTUP_RETRY_S = 1.0


class HardwareError(RuntimeError):
    pass


class HardwareNotReadyError(HardwareError):
    """Required hardware is still being initialized."""


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
            raise HardwareError(
                f"cannot wait for platform profile change: {exc}"
            ) from exc
        if not events:
            return False
        return self.refresh()

    def refresh(self) -> bool:
        current = self._read()
        changed = current != self.current
        self.current = current
        return changed

    def close(self) -> None:
        self.handle.close()

def read_text(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def read_int(path: Path) -> int:
    try:
        return int(read_text(path))
    except (OSError, ValueError) as exc:
        raise HardwareError(f"cannot read integer from {path}: {exc}") from exc


def validate_required_profile(
    required_profile: str,
    choices_path: Path = PLATFORM_PROFILE_CHOICES_PATH,
) -> None:
    choices = tuple(read_text(choices_path).split())
    if required_profile not in choices:
        available = ", ".join(choices) if choices else "none"
        raise ConfigurationError(
            f"required_profile {required_profile!r} is unavailable; "
            f"platform choices: {available}"
        )


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
    """Read index 0 (IR) from the optional text procfs ABI."""
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
    """Raw readings from mandatory and optional temperature sources."""

    cpu: float
    gpu: float | None
    acpi: float | None
    ir: float | None = None
    nvidia_power_draw_w: float | None = None
    nvidia_power_limit_w: float | None = None

    def control_temperatures(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in CONTROL_SENSORS}

    @property
    def raw_control_hottest(self) -> float:
        """Return the hottest sensor that is allowed to control the fans."""
        return max(
            value
            for value in self.control_temperatures().values()
            if value is not None
        )


class Sensors:
    """Aggregate a mandatory CPU source and best-effort auxiliary sensors."""

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
                    "optional HP WMI IR sensor unavailable; "
                    "continuing with CPU/GPU: %s",
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
    """Validated access to the hp-wmi fan-control hwmon attributes."""

    def __init__(self, root: Path = Path("/sys/class/hwmon")):
        matches = find_hwmon("hp", root)
        if not matches:
            raise HardwareNotReadyError("hp hwmon device was not found")
        if len(matches) > 1:
            raise HardwareError(
                f"expected exactly one hp hwmon device, found {len(matches)}"
            )
        self.path = matches[0]
        self.pwm = self.path / "pwm1"
        self.enable = self.path / "pwm1_enable"
        self.fan1 = self.path / "fan1_input"
        self.fan2 = self.path / "fan2_input"
        self._manual_recovery_pending = False
        for required in (self.pwm, self.enable, self.fan1, self.fan2):
            if not required.exists():
                raise HardwareNotReadyError(
                    f"required hp-wmi attribute is missing: {required}"
                )

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
        self._manual_recovery_pending = False

    def update_manual(self, pwm: int, *, write_pwm: bool = True) -> None:
        pwm = int(clamp(pwm, 1, PWM_MAX))
        mode = read_int(self.enable)
        if mode == MAX_MODE:
            # Max may have been asserted by the EC or by the user. Never
            # reduce that independently requested safety state.
            self._manual_recovery_pending = False
            LOG.warning("maximum fan mode asserted externally; preserving it")
            return
        if mode == AUTO_MODE:
            if getattr(self, "_manual_recovery_pending", False):
                raise HardwareError(
                    "manual fan mode was lost again immediately after recovery"
                )
            LOG.warning(
                "manual fan mode was lost (mode=%d); attempting one recovery",
                mode,
            )
            self.set_manual(pwm)
            self._manual_recovery_pending = True
            return
        if mode != MANUAL_MODE:
            raise HardwareError(f"unexpected fan mode during manual control: {mode}")
        self._manual_recovery_pending = False
        if write_pwm:
            write_int(self.pwm, pwm)

    def set_maximum(self) -> None:
        write_int(self.enable, MAX_MODE)
        self._manual_recovery_pending = False

    def restore_auto(self) -> None:
        write_int(self.enable, AUTO_MODE)
        self._manual_recovery_pending = False


def wait_for_hp_fan_hwmon(
    root: Path = Path("/sys/class/hwmon"),
    timeout_s: float = HP_HWMON_STARTUP_TIMEOUT_S,
    retry_s: float = HP_HWMON_STARTUP_RETRY_S,
) -> HpFanHwmon:
    """Wait briefly for hp-wmi to finish publishing its hwmon interface."""
    deadline = time.monotonic() + timeout_s
    waiting_logged = False
    while True:
        try:
            fan = HpFanHwmon(root=root)
            if waiting_logged:
                LOG.info("hp hwmon interface became ready")
            return fan
        except HardwareNotReadyError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HardwareError(
                    f"hp hwmon did not become ready within {timeout_s:g} seconds: {exc}"
                ) from exc
            if not waiting_logged:
                LOG.warning(
                    "hp hwmon is not ready; waiting up to %g seconds: %s",
                    timeout_s,
                    exc,
                )
                waiting_logged = True
            time.sleep(min(retry_s, remaining))
