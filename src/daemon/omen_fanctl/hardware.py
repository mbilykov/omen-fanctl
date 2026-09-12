"""Hardware adapters for temperature sensors, hp-wmi, and profiles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
import select
import shutil
import subprocess
import time

from .config import (
    ConfigurationError,
    HP_CPU_GPU_LEVEL_TABLES,
    HP_FAN_LEVEL_MAX,
    HP_SINGLE_PWM_MAX_LEVEL,
    PWM_MAX,
    Settings,
    clamp,
    hp_gpu_level_for_cpu_level,
    hp_level_to_pwm,
    pwm_to_hp_level,
)


LOG = logging.getLogger("omen-fanctl")
AUTO_MODE = 2
MANUAL_MODE = 1
MAX_MODE = 0
PLATFORM_PROFILE_CHOICES_PATH = Path("/sys/firmware/acpi/platform_profile_choices")
CONTROL_SENSORS = ("cpu", "gpu", "ir")
AMD_GPU_READ_FAILURE_THRESHOLD = 3
PLATFORM_PROFILE_STARTUP_TIMEOUT_S = 20.0
PLATFORM_PROFILE_STARTUP_RETRY_S = 1.0
HP_HWMON_STARTUP_TIMEOUT_S = 20.0
HP_HWMON_STARTUP_RETRY_S = 1.0
K10TEMP_STARTUP_TIMEOUT_S = 20.0
K10TEMP_STARTUP_RETRY_S = 1.0
NVIDIA_DISCOVERY_INTERVAL_S = 30.0
NVIDIA_FAILURE_THRESHOLD = 3
NVIDIA_PCI_VENDOR = "0x10de"
NVIDIA_GPU_PCI_CLASSES = frozenset({"0x030000", "0x030200"})


class HardwareError(RuntimeError):
    pass


class HardwareNotReadyError(HardwareError):
    """Required hardware is still being initialized."""


class FailurePolicy(Enum):
    REQUIRED = auto()
    REQUIRED_AFTER_AVAILABLE = auto()
    OPTIONAL = auto()


@dataclass
class SourceHealth:
    """Track one sensor's loss and recovery without repeating state logic."""

    name: str
    policy: FailurePolicy
    failure_threshold: int = 1
    failed: bool = False
    ever_available: bool = False
    consecutive_failures: int = 0

    @property
    def degraded(self) -> bool:
        """True while a source this daemon depends on is failing.

        A source that never became available was never depended on, and an
        OPTIONAL one is expected to come and go. Both are silent by design.
        """
        if not self.failed:
            return False
        if self.policy is FailurePolicy.REQUIRED:
            return True
        return (
            self.policy is FailurePolicy.REQUIRED_AFTER_AVAILABLE
            and self.ever_available
        )

    def available(self) -> None:
        if self.failed:
            LOG.info("%s recovered", self.name)
        self.failed = False
        self.ever_available = True
        self.consecutive_failures = 0

    def unavailable(
        self,
        reason: object,
        failure_threshold: int | None = None,
    ) -> None:
        self.consecutive_failures += 1
        if not self.failed:
            LOG.warning("%s unavailable: %s", self.name, reason)
        self.failed = True
        required = self.policy is FailurePolicy.REQUIRED
        required_after_loss = (
            self.policy is FailurePolicy.REQUIRED_AFTER_AVAILABLE
            and self.ever_available
        )
        threshold = (
            self.failure_threshold if failure_threshold is None else failure_threshold
        )
        # An override changes the severity of this failure, not the identity of
        # the failure streak. A fail-fast cause must therefore take effect even
        # if earlier failures in the streak used a debounce threshold.
        if (required or required_after_loss) and self.consecutive_failures >= threshold:
            raise HardwareError(f"{self.name} unavailable: {reason}")


class PlatformProfileMonitor:
    """Keep a sysfs fd open and consume platform-profile notifications."""

    def __init__(self, path: Path):
        self.path = path
        handle = None
        try:
            handle = path.open("r", encoding="ascii")
            self.handle = handle
            self.poller = select.poll()
            self.poller.register(self.handle.fileno(), select.POLLPRI | select.POLLERR)
            self.current = self._read()
        except (OSError, HardwareError) as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if isinstance(exc, OSError):
                raise HardwareError(f"cannot monitor platform profile: {exc}") from exc
            raise

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
    timeout_s: float = PLATFORM_PROFILE_STARTUP_TIMEOUT_S,
    retry_s: float = PLATFORM_PROFILE_STARTUP_RETRY_S,
) -> None:
    deadline = time.monotonic() + timeout_s
    waiting_logged = False
    while True:
        try:
            choices = tuple(read_text(choices_path).split())
        except OSError as exc:
            not_ready = HardwareNotReadyError(
                f"cannot read platform profile choices from {choices_path}: {exc}"
            )
        else:
            if required_profile in choices:
                if waiting_logged:
                    LOG.info("required platform profile became available")
                return
            available = ", ".join(choices) if choices else "none"
            not_ready = HardwareNotReadyError(
                f"required_profile {required_profile!r} is unavailable; "
                f"platform choices: {available}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HardwareError(
                "required platform profile did not become available within "
                f"{timeout_s:g} seconds: {not_ready}"
            ) from not_ready
        if not waiting_logged:
            LOG.warning(
                "required platform profile is not available; "
                "waiting up to %g seconds: %s",
                timeout_s,
                not_ready,
            )
            waiting_logged = True
        time.sleep(min(retry_s, remaining))


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


def find_nvidia_runtime_status_files(
    root: Path = Path("/sys/bus/pci/devices"),
) -> tuple[Path, ...]:
    """Find runtime-PM status files without accessing the NVIDIA driver."""
    try:
        devices = tuple(root.iterdir())
    except OSError:
        return ()
    matches: list[Path] = []
    for device in devices:
        try:
            vendor = read_text(device / "vendor")
            device_class = read_text(device / "class")
        except OSError:
            continue
        if vendor == NVIDIA_PCI_VENDOR and device_class in NVIDIA_GPU_PCI_CLASSES:
            matches.append(device / "power" / "runtime_status")
    return tuple(sorted(matches))


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


def _find_hwmon_temperature_source(
    name: str,
    root: Path = Path("/sys/class/hwmon"),
) -> tuple[Path, list[float]] | None:
    """Return the first matching hwmon directory with valid temperatures."""
    for directory in find_hwmon(name, root):
        values = read_hwmon_temperatures(directory)
        if values:
            return directory, values
    return None


def read_hp_wmi_ir_temperature(path: Path) -> float:
    """Read index 0 (IR) from the optional text procfs ABI."""
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise HardwareError(f"cannot read HP WMI IR sensor from {path}: {exc}") from exc

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
    nvidia_metrics_stale: bool | None = None
    amd_gpu_temperature_stale: bool | None = None
    nvidia_runtime_suspended: bool | None = None
    degraded_sources: tuple[str, ...] = ()

    def control_temperatures(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in CONTROL_SENSORS}

    @property
    def raw_control_hottest(self) -> float:
        """Return the hottest sensor that is allowed to control the fans."""
        return max(
            value for value in self.control_temperatures().values() if value is not None
        )

    @property
    def has_cached_readings(self) -> bool:
        """True when a reading was reused after its source failed to answer.

        Such a sample still describes the machine well enough to keep cooling
        it, which is why the control loop accepts it, but it is not a
        measurement of the present moment.
        """
        return bool(self.nvidia_metrics_stale or self.amd_gpu_temperature_stale)


class Sensors:
    """Aggregate a mandatory CPU source and best-effort auxiliary sensors."""

    def __init__(
        self,
        settings: Settings,
        hwmon_root: Path = Path("/sys/class/hwmon"),
        thermal_root: Path = Path("/sys/class/thermal"),
        pci_root: Path = Path("/sys/bus/pci/devices"),
    ):
        self.settings = settings
        self.hwmon_root = hwmon_root
        self.thermal_root = thermal_root
        cpu_source = _find_hwmon_temperature_source("k10temp", hwmon_root)
        if cpu_source is None:
            raise HardwareNotReadyError(
                "no k10temp hwmon sensor with a valid temperature input was found"
            )
        self.cpu_hwmon, _ = cpu_source
        self.amd_gpu_hwmons = (
            find_hwmon("amdgpu", hwmon_root) if settings.include_amd_gpu else []
        )
        self.pci_root = pci_root
        self.nvidia_smi = None
        self.next_nvidia_discovery = 0.0
        self.next_nvidia_pci_discovery = 0.0
        if settings.include_nvidia_gpu:
            now = time.monotonic()
            self.nvidia_smi = shutil.which("nvidia-smi")
            if not self.nvidia_smi:
                self.next_nvidia_discovery = now + NVIDIA_DISCOVERY_INTERVAL_S
            self.nvidia_runtime_status_files = find_nvidia_runtime_status_files(
                pci_root
            )
            self.next_nvidia_pci_discovery = now + NVIDIA_DISCOVERY_INTERVAL_S
        else:
            self.nvidia_runtime_status_files = ()
        self.nvidia_runtime_suspended: bool | None = None
        self.cpu_health = SourceHealth("CPU temperature source", FailurePolicy.REQUIRED)
        self.amd_gpu_health = SourceHealth(
            "AMD GPU temperature source",
            FailurePolicy.REQUIRED_AFTER_AVAILABLE,
        )
        self.last_amd_gpu_temperature: float | None = None
        self.amd_gpu_temperature_stale: bool | None = None
        self.nvidia_gpu_health = SourceHealth(
            "NVIDIA GPU temperature source",
            FailurePolicy.REQUIRED_AFTER_AVAILABLE,
            failure_threshold=NVIDIA_FAILURE_THRESHOLD,
        )
        self.last_nvidia_metrics: tuple[float | None, float | None, float | None] = (
            None,
            None,
            None,
        )
        self.ir_health = SourceHealth(
            "optional HP WMI IR sensor", FailurePolicy.OPTIONAL
        )
        self.acpi_health = SourceHealth(
            "ACPI temperature proxy", FailurePolicy.OPTIONAL
        )
        self.hp_wmi_sensors_path = settings.hp_wmi_sensors_path

    def _cpu_temperature(self) -> float:
        values = read_hwmon_temperatures(self.cpu_hwmon)
        if not values:
            source = _find_hwmon_temperature_source("k10temp", self.hwmon_root)
            if source is not None:
                self.cpu_hwmon, values = source
        if not values:
            reason = "no valid k10temp temperature was found during rediscovery"
            self.cpu_health.unavailable(reason)
            # CPU is mandatory at this call site even if its health policy is
            # accidentally relaxed; never continue into max(values) empty.
            raise HardwareError(f"CPU temperature source unavailable: {reason}")
        self.cpu_health.available()
        return max(values)

    def _amd_gpu_temperature(self) -> float | None:
        if not self.settings.include_amd_gpu:
            self.amd_gpu_temperature_stale = None
            return None
        values: list[float] = []
        for directory in self.amd_gpu_hwmons:
            values.extend(read_hwmon_temperatures(directory))
        if not values:
            # hwmon indices change after a GPU reset or driver re-probe. Look
            # up the current registration instead of retaining a dead path.
            self.amd_gpu_hwmons = find_hwmon("amdgpu", self.hwmon_root)
            for directory in self.amd_gpu_hwmons:
                values.extend(read_hwmon_temperatures(directory))
        if not values:
            if self.amd_gpu_hwmons:
                self.amd_gpu_health.unavailable(
                    "amdgpu hwmon is present but its temperature is unreadable",
                    failure_threshold=AMD_GPU_READ_FAILURE_THRESHOLD,
                )
                self.amd_gpu_temperature_stale = (
                    True if self.last_amd_gpu_temperature is not None else None
                )
                return self.last_amd_gpu_temperature
            else:
                self.amd_gpu_temperature_stale = None
                self.last_amd_gpu_temperature = None
                self.amd_gpu_health.unavailable(
                    "no amdgpu hwmon device was found during rediscovery"
                )
            return None
        self.amd_gpu_health.available()
        self.last_amd_gpu_temperature = max(values)
        self.amd_gpu_temperature_stale = False
        return self.last_amd_gpu_temperature

    def _nvidia_failure(
        self, reason: object
    ) -> tuple[float | None, float | None, float | None, bool | None]:
        self.nvidia_gpu_health.unavailable(reason)
        stale = (
            True
            if any(value is not None for value in self.last_nvidia_metrics)
            else None
        )
        return (*self.last_nvidia_metrics, stale)

    def _nvidia_metrics(
        self,
    ) -> tuple[float | None, float | None, float | None, bool | None]:
        self.nvidia_runtime_suspended = None
        if not self.nvidia_smi and not self.settings.include_nvidia_gpu:
            return None, None, None, None
        now = time.monotonic()
        if not self.nvidia_smi:
            if now >= self.next_nvidia_discovery:
                self.nvidia_smi = shutil.which("nvidia-smi")
                self.next_nvidia_discovery = now + NVIDIA_DISCOVERY_INTERVAL_S
            if not self.nvidia_smi:
                return self._nvidia_failure("nvidia-smi was not found")
        if now >= self.next_nvidia_pci_discovery:
            self.nvidia_runtime_status_files = find_nvidia_runtime_status_files(
                self.pci_root
            )
            self.next_nvidia_pci_discovery = now + NVIDIA_DISCOVERY_INTERVAL_S
        if self.nvidia_runtime_status_files:
            try:
                runtime_statuses = tuple(
                    read_text(path) for path in self.nvidia_runtime_status_files
                )
            except OSError:
                # A reset or hotplug can invalidate the saved PCI path between
                # periodic scans. Rediscover once and retry in this sample.
                self.nvidia_runtime_status_files = find_nvidia_runtime_status_files(
                    self.pci_root
                )
                self.next_nvidia_pci_discovery = now + NVIDIA_DISCOVERY_INTERVAL_S
                try:
                    runtime_statuses = tuple(
                        read_text(path) for path in self.nvidia_runtime_status_files
                    )
                except OSError:
                    runtime_statuses = ()
            if runtime_statuses:
                self.nvidia_runtime_suspended = all(
                    status == "suspended" for status in runtime_statuses
                )
                if self.nvidia_runtime_suspended:
                    # Querying NVML wakes an RTD3-suspended dGPU. It has no
                    # current cooling demand. Its metrics also cannot safely
                    # bridge a later wake-up, so end their cache lifetime here.
                    self.last_nvidia_metrics = (None, None, None)
                    return None, None, None, None
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
        except OSError as exc:
            self.nvidia_smi = None
            self.next_nvidia_discovery = time.monotonic() + NVIDIA_DISCOVERY_INTERVAL_S
            return self._nvidia_failure(exc)
        except subprocess.TimeoutExpired as exc:
            return self._nvidia_failure(exc)
        if result.returncode != 0:
            reason = f"nvidia-smi exited with status {result.returncode}"
            stderr = " ".join(result.stderr.split())
            if stderr:
                reason = f"{reason}: {stderr}"
            return self._nvidia_failure(reason)
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
        if not temperatures:
            return self._nvidia_failure("nvidia-smi returned no valid temperature")
        self.nvidia_gpu_health.available()
        self.last_nvidia_metrics = (
            max(temperatures),
            sum(power_draws) if power_draws else None,
            sum(power_limits) if power_limits else None,
        )
        return (*self.last_nvidia_metrics, False)

    def _acpi_temperature(self) -> float | None:
        if not self.settings.include_acpi:
            return None
        values: list[float] = []
        for directory in sorted(self.thermal_root.glob("thermal_zone*")):
            try:
                if read_text(directory / "type") != "acpitz":
                    continue
                value = read_int(directory / "temp") / 1000.0
            except (OSError, HardwareError):
                continue
            if 1.0 <= value <= 125.0:
                values.append(value)
        if not values:
            self.acpi_health.unavailable("no valid acpitz temperature was found")
            return None
        self.acpi_health.available()
        return max(values)

    def _hp_wmi_ir_temperature(self) -> float | None:
        if not self.settings.include_hp_wmi_ir:
            return None
        try:
            value = read_hp_wmi_ir_temperature(self.hp_wmi_sensors_path)
        except HardwareError as exc:
            self.ir_health.unavailable(f"{exc}; continuing with CPU/GPU")
            return None
        self.ir_health.available()
        return value

    def _degraded_sources(self) -> tuple[str, ...]:
        """Name the depended-on sources that are currently failing.

        A reused reading is one symptom of this, but not the only one: a query
        that fails with no cache to fall back on reports neither a temperature
        nor staleness, which is what an NVIDIA GPU does on the first failed
        query after leaving runtime suspend. Callers that need a complete
        description of the machine, such as the stop handoff, ask here instead
        of inferring health from the values.

        An RTD3-suspended NVIDIA GPU is excluded: it is powered down, not
        failing, which is the same exception the Auto handoff makes.
        """
        suspended = self.nvidia_gpu_health if self.nvidia_runtime_suspended else None
        return tuple(
            health.name
            for health in (
                self.cpu_health,
                self.amd_gpu_health,
                self.nvidia_gpu_health,
                self.ir_health,
                self.acpi_health,
            )
            if health is not suspended and health.degraded
        )

    def read(self) -> TemperatureSnapshot:
        cpu_temperature = self._cpu_temperature()
        (
            nvidia_temperature,
            power_draw,
            power_limit,
            nvidia_metrics_stale,
        ) = self._nvidia_metrics()
        gpu_values = [self._amd_gpu_temperature(), nvidia_temperature]
        valid_gpu = [value for value in gpu_values if value is not None]
        gpu_temperature = max(valid_gpu) if valid_gpu else None
        # Every source has reported by now, so their health describes this
        # sample rather than the previous one.
        acpi_temperature = self._acpi_temperature()
        ir_temperature = self._hp_wmi_ir_temperature()
        return TemperatureSnapshot(
            cpu=cpu_temperature,
            gpu=gpu_temperature,
            acpi=acpi_temperature,
            ir=ir_temperature,
            nvidia_power_draw_w=power_draw,
            nvidia_power_limit_w=power_limit,
            nvidia_metrics_stale=nvidia_metrics_stale,
            amd_gpu_temperature_stale=self.amd_gpu_temperature_stale,
            nvidia_runtime_suspended=self.nvidia_runtime_suspended,
            degraded_sources=self._degraded_sources(),
        )


def wait_for_temperature_sensors(
    settings: Settings,
    root: Path = Path("/sys/class/hwmon"),
    timeout_s: float = K10TEMP_STARTUP_TIMEOUT_S,
    retry_s: float = K10TEMP_STARTUP_RETRY_S,
) -> Sensors:
    """Wait briefly for the mandatory CPU temperature source to become ready."""
    deadline = time.monotonic() + timeout_s
    waiting_logged = False
    while True:
        try:
            sensors = Sensors(settings, hwmon_root=root)
            if waiting_logged:
                LOG.info("k10temp temperature source became ready")
            return sensors
        except HardwareNotReadyError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HardwareError(
                    "k10temp temperature source did not become ready within "
                    f"{timeout_s:g} seconds: {exc}"
                ) from exc
            if not waiting_logged:
                LOG.warning(
                    "k10temp temperature source is not ready; "
                    "waiting up to %g seconds: %s",
                    timeout_s,
                    exc,
                )
                waiting_logged = True
            time.sleep(min(retry_s, remaining))


class HpFanHwmon:
    """Validated access to the hp-wmi fan-control hwmon attributes."""

    def __init__(
        self,
        root: Path = Path("/sys/class/hwmon"),
        *,
        board_name: str | None = None,
    ):
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
        pwm2 = self.path / "pwm2"
        self.pwm2 = pwm2 if pwm2.exists() else None
        self.board_name = board_name
        self.cpu_gpu_level_table = HP_CPU_GPU_LEVEL_TABLES.get(board_name)

    @property
    def supports_independent_pwm(self) -> bool:
        return self.pwm2 is not None

    @property
    def pwm_abi(self) -> str:
        return "dual" if self.supports_independent_pwm else "single"

    @property
    def manual_max_level(self) -> int:
        if self.supports_independent_pwm:
            return int(HP_FAN_LEVEL_MAX)
        return HP_SINGLE_PWM_MAX_LEVEL

    @property
    def manual_pwm_max(self) -> int:
        return hp_level_to_pwm(self.manual_max_level)

    def _require_manual_mapping(self) -> tuple[tuple[int, int], ...]:
        table = self.cpu_gpu_level_table
        if table is None:
            raise ConfigurationError(
                "dual-channel Manual control has no captured CPU/GPU mapping "
                f"for board {self.board_name!r}"
            )
        return table

    def validate_manual_mapping(self) -> None:
        if self.supports_independent_pwm:
            self._require_manual_mapping()

    def _pwm_targets(self, cpu_pwm: int) -> tuple[tuple[Path, int], ...]:
        if self.pwm2 is None:
            return ((self.pwm, cpu_pwm),)
        table = self._require_manual_mapping()
        cpu_level = pwm_to_hp_level(cpu_pwm)
        gpu_level = hp_gpu_level_for_cpu_level(cpu_level, table)
        return (
            (self.pwm, cpu_pwm),
            (self.pwm2, hp_level_to_pwm(gpu_level)),
        )

    def _validate_manual_pwm(self, pwm: int) -> None:
        if pwm > self.manual_pwm_max:
            raise HardwareError(
                f"Manual PWM {pwm} exceeds the single-channel safe maximum "
                f"{self.manual_pwm_max} (HP fan level {self.manual_max_level})"
            )

    def status(self) -> tuple[int, int, int, int]:
        return (
            read_int(self.enable),
            read_int(self.pwm),
            read_int(self.fan1),
            read_int(self.fan2),
        )

    def _write_pwm_targets_or_restore_auto(self, pwm: int, context: str) -> None:
        try:
            for channel, target in self._pwm_targets(pwm):
                write_int(channel, target)
        except HardwareError:
            # A channel write may have succeeded before a later one failed.
            # Leave neither an unowned Manual mode nor a partially updated pair.
            try:
                self.restore_auto()
            except HardwareError as restore_error:
                LOG.critical(
                    "%s failed and Auto rollback also failed: %s",
                    context,
                    restore_error,
                )
            raise

    def set_manual(self, pwm: int) -> None:
        pwm = int(clamp(pwm, 1, PWM_MAX))
        self._validate_manual_pwm(pwm)
        # Linux 7.1 hp-wmi deliberately captures the current physical RPM when
        # switching Auto -> Manual, producing a smooth and non-zero transition.
        # pwm1 rejects writes outside Manual mode, so mode must be changed first.
        write_int(self.enable, MANUAL_MODE)
        self._write_pwm_targets_or_restore_auto(pwm, "initial manual PWM write")
        self._manual_recovery_pending = False

    def update_manual(self, pwm: int, *, write_pwm: bool = True) -> None:
        pwm = int(clamp(pwm, 1, PWM_MAX))
        self._validate_manual_pwm(pwm)
        mode = read_int(self.enable)
        if mode == MAX_MODE:
            # Max may have been asserted by the EC or by the user. Never
            # reduce that independently requested safety state.
            self._manual_recovery_pending = False
            LOG.warning("maximum fan mode asserted externally; preserving it")
            return
        if mode == AUTO_MODE:
            if self._manual_recovery_pending:
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
            self._write_pwm_targets_or_restore_auto(pwm, "manual PWM update")

    def set_maximum(self) -> None:
        write_int(self.enable, MAX_MODE)
        self._manual_recovery_pending = False

    def restore_auto(self) -> None:
        write_int(self.enable, AUTO_MODE)
        self._manual_recovery_pending = False


def wait_for_hp_fan_hwmon(
    board_name: str,
    root: Path = Path("/sys/class/hwmon"),
    timeout_s: float = HP_HWMON_STARTUP_TIMEOUT_S,
    retry_s: float = HP_HWMON_STARTUP_RETRY_S,
) -> HpFanHwmon:
    """Wait briefly for hp-wmi to finish publishing its hwmon interface."""
    deadline = time.monotonic() + timeout_s
    waiting_logged = False
    unconfirmed_single_path: Path | None = None
    while True:
        confirming_single = False
        try:
            fan = HpFanHwmon(root=root, board_name=board_name)
            fan.validate_manual_mapping()
            if fan.supports_independent_pwm or fan.path == unconfirmed_single_path:
                if waiting_logged:
                    LOG.info("hp hwmon interface became ready")
                return fan
            unconfirmed_single_path = fan.path
            confirming_single = True
            not_ready = HardwareNotReadyError(
                "waiting to confirm that the optional pwm2 attribute is absent"
            )
        except HardwareNotReadyError as exc:
            unconfirmed_single_path = None
            not_ready = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HardwareError(
                f"hp hwmon did not become ready within {timeout_s:g} seconds: "
                f"{not_ready}"
            ) from not_ready
        if not confirming_single:
            if not waiting_logged:
                LOG.warning(
                    "hp hwmon is not ready; waiting up to %g seconds: %s",
                    timeout_s,
                    not_ready,
                )
                waiting_logged = True
        time.sleep(min(retry_s, remaining))
