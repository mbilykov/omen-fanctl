"""Configuration loading and fan-curve evaluation."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path


PWM_MAX = 255
HP_FAN_LEVEL_MAX = 60.0
DEFAULT_ALLOWED_BOARDS = ("8D87",)
MAX_DECREASE_HYSTERESIS_C = 20.0
TOP_LEVEL_KEYS = frozenset({"daemon", "ewma", "sensors", "curve", "curves"})
DAEMON_KEYS = frozenset(
    {
        "allowed_boards",
        "required_profile",
        "sample_interval_s",
        "control_interval_s",
        "activation_temp_c",
        "release_temp_c",
        "fan_stop_temp_c",
        "critical_temp_c",
        "critical_release_temp_c",
        "emergency_hold_s",
        "decrease_hysteresis_c",
        "ir_release_hysteresis_c",
        "auto_guard_s",
        "max_rise_percent_per_update",
        "max_fall_percent_per_update",
        "minimum_manual_percent",
    }
)
EWMA_KEYS = frozenset({"rise_alpha", "fall_alpha"})
SENSOR_KEYS = frozenset(
    {
        "include_acpi",
        "include_amd_gpu",
        "include_nvidia_gpu",
        "include_hp_wmi_ir",
        "hp_wmi_sensors_path",
    }
)
CURVE_KEYS = frozenset(
    {
        "temperature_c",
        "high_temperature_c",
        "low_temperature_c",
        "pwm_percent",
        "fan_level",
        "stepped",
    }
)
NAMED_CURVE_KEYS = frozenset({"preset", "cpu", "gpu", "ir", "acpi"})


class ConfigurationError(ValueError):
    pass


def configuration_table(
    value: object,
    path: str,
    allowed_keys: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"configuration section {path} must be a table")
    unknown = sorted(set(value) - allowed_keys)
    if unknown:
        names = ", ".join(f"{path}.{key}" if path else key for key in unknown)
        raise ConfigurationError(f"unknown configuration key: {names}")
    return value


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def percent_to_pwm(percent: float) -> int:
    scaled = clamp(percent, 0.0, 100.0) * PWM_MAX / 100.0
    return int(scaled + 0.5)


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
        if any(not math.isfinite(value) for value in self.temperatures):
            raise ConfigurationError("curve temperatures must be finite")
        if any(not math.isfinite(value) for value in self.pwm_percent):
            raise ConfigurationError("curve PWM values must be finite")
        if any(b <= a for a, b in zip(self.temperatures, self.temperatures[1:])):
            raise ConfigurationError("curve temperatures must be strictly increasing")
        if any(b < a for a, b in zip(self.pwm_percent, self.pwm_percent[1:])):
            raise ConfigurationError("curve PWM values must be non-decreasing")
        if any(value < 0 or value > 100 for value in self.pwm_percent):
            raise ConfigurationError("curve PWM values must be between 0 and 100")
        if self.fall_temperatures is not None:
            if not self.stepped:
                raise ConfigurationError(
                    "curve low_temperature_c requires stepped = true"
                )
            if any(
                b <= a for a, b in zip(self.pwm_percent, self.pwm_percent[1:])
            ):
                raise ConfigurationError(
                    "curve PWM values must be strictly increasing when "
                    "low_temperature_c is set"
                )
            if len(self.fall_temperatures) != len(self.temperatures):
                raise ConfigurationError(
                    "curve falling-temperature and PWM lists differ in length"
                )
            if any(
                not math.isfinite(value) for value in self.fall_temperatures
            ):
                raise ConfigurationError(
                    "curve falling temperatures must be finite"
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

    def target_percent(
        self, temperature: float, previous_percent: float | None = None
    ) -> float:
        """Evaluate a curve, retaining a stepped level until its low threshold.

        Raises:
            ValueError: If a stepped curve's previous target is not one of its
                configured PWM levels.
        """
        if (
            previous_percent is not None
            and self.stepped
            and self.fall_temperatures is not None
            and previous_percent not in self.pwm_percent
        ):
            raise ValueError("previous stepped target is not a curve level")

        rising_target = self.evaluate_percent(temperature)
        if (
            previous_percent is None
            or not self.stepped
            or self.fall_temperatures is None
            or rising_target >= previous_percent
        ):
            return rising_target

        index = self.pwm_percent.index(previous_percent)
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
    fan_stop_temp_c: float = 45.0
    ir_release_hysteresis_c: float = 1.0
    auto_guard_s: float = 180.0
    include_hp_wmi_ir: bool = True
    hp_wmi_sensors_path: Path = Path("/proc/hp_wmi_sensors")
    curves: tuple[tuple[str, Curve], ...] | None = None
    curve_source: str = "legacy-shared"

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"cannot load {path}: {exc}") from exc

        configuration_table(raw, "", TOP_LEVEL_KEYS)
        daemon = configuration_table(raw.get("daemon", {}), "daemon", DAEMON_KEYS)
        ewma = configuration_table(raw.get("ewma", {}), "ewma", EWMA_KEYS)
        sensors = configuration_table(
            raw.get("sensors", {}), "sensors", SENSOR_KEYS
        )
        curve_data = configuration_table(
            raw.get("curve", {}), "curve", CURVE_KEYS
        )
        curves_data = configuration_table(
            raw.get("curves", {}), "curves", NAMED_CURVE_KEYS
        )
        named_curve_data = {
            name: configuration_table(
                curves_data[name], f"curves.{name}", CURVE_KEYS
            )
            for name in ("cpu", "gpu", "ir", "acpi")
            if name in curves_data
        }
        try:
            preset = str(curves_data.get("preset", "")).strip()
            if preset:
                if preset != "hp-vibrance-stx-n22x9-performance":
                    raise ConfigurationError(f"unknown curve preset: {preset}")
                named = hp_factory_performance_curves()
                curve = named["cpu"]
                curves = tuple(named.items())
                curve_source = preset
            else:
                named = {
                    name: cls._load_curve(data)
                    for name, data in named_curve_data.items()
                }
                if named:
                    if "cpu" not in named:
                        raise ConfigurationError(
                            "curves.cpu is required when using per-sensor curves"
                        )
                    curve = named["cpu"]
                    curves = tuple(named.items())
                    curve_source = "custom-per-sensor"
                else:
                    curve = cls._load_curve(curve_data)
                    curves = None
                    curve_source = "legacy-shared"
            allowed_boards = board_list(daemon["allowed_boards"])
            if allowed_boards is None:
                raise ConfigurationError(
                    "allowed_boards must be a list of board names, "
                    'for example allowed_boards = ["8D87"]'
                )
            settings = cls(
                allowed_boards=allowed_boards,
                required_profile=str(daemon.get("required_profile", "performance")),
                sample_interval_s=float(daemon.get("sample_interval_s", 1.0)),
                control_interval_s=float(daemon.get("control_interval_s", 5.0)),
                activation_temp_c=float(daemon.get("activation_temp_c", 65.0)),
                release_temp_c=float(daemon.get("release_temp_c", 55.0)),
                fan_stop_temp_c=float(daemon.get("fan_stop_temp_c", 45.0)),
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
                    daemon.get("minimum_manual_percent", hp_level_percent(19))
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
        curves = dict(self.curves)
        # acpitz is retained only as an opt-in diagnostic proxy for the IR
        # input and therefore uses the IR table unless explicitly configured.
        if sensor == "acpi":
            return curves.get("acpi", curves.get("ir", self.curve))
        return curves.get(sensor, self.curve)

    def validate(self) -> None:
        if not self.allowed_boards:
            raise ConfigurationError("allowed_boards must not be empty")
        numeric_values = (
            ("sample_interval_s", self.sample_interval_s),
            ("control_interval_s", self.control_interval_s),
            ("activation_temp_c", self.activation_temp_c),
            ("release_temp_c", self.release_temp_c),
            ("fan_stop_temp_c", self.fan_stop_temp_c),
            ("critical_temp_c", self.critical_temp_c),
            ("critical_release_temp_c", self.critical_release_temp_c),
            ("emergency_hold_s", self.emergency_hold_s),
            ("decrease_hysteresis_c", self.decrease_hysteresis_c),
            ("ir_release_hysteresis_c", self.ir_release_hysteresis_c),
            ("auto_guard_s", self.auto_guard_s),
            ("ewma.rise_alpha", self.ewma_rise_alpha),
            ("ewma.fall_alpha", self.ewma_fall_alpha),
            ("minimum_manual_percent", self.minimum_manual_percent),
            ("max_rise_percent_per_update", self.max_rise_percent_per_update),
            ("max_fall_percent_per_update", self.max_fall_percent_per_update),
        )
        for name, value in numeric_values:
            if not math.isfinite(value):
                raise ConfigurationError(f"{name} must be finite")
        if self.sample_interval_s < 0.25:
            raise ConfigurationError("sample_interval_s must be at least 0.25")
        if self.control_interval_s < self.sample_interval_s:
            raise ConfigurationError("control_interval_s must be >= sample_interval_s")
        for name, value in (
            ("activation_temp_c", self.activation_temp_c),
            ("release_temp_c", self.release_temp_c),
            ("fan_stop_temp_c", self.fan_stop_temp_c),
            ("critical_temp_c", self.critical_temp_c),
            ("critical_release_temp_c", self.critical_release_temp_c),
        ):
            if not 0 < value <= 125:
                raise ConfigurationError(f"{name} must be in (0, 125]")
        if self.release_temp_c >= self.activation_temp_c:
            raise ConfigurationError("release_temp_c must be below activation_temp_c")
        if self.fan_stop_temp_c >= self.activation_temp_c:
            raise ConfigurationError(
                "fan_stop_temp_c must be below activation_temp_c"
            )
        if self.critical_release_temp_c >= self.critical_temp_c:
            raise ConfigurationError(
                "critical_release_temp_c must be below critical_temp_c"
            )
        if self.activation_temp_c >= self.critical_temp_c:
            raise ConfigurationError("activation_temp_c must be below critical_temp_c")
        if self.emergency_hold_s < 0:
            raise ConfigurationError("emergency_hold_s must be non-negative")
        if not 0 <= self.decrease_hysteresis_c <= MAX_DECREASE_HYSTERESIS_C:
            raise ConfigurationError(
                "decrease_hysteresis_c must be between 0 and 20"
            )
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


def board_list(value: object) -> tuple[str, ...] | None:
    """Normalise an ``allowed_boards`` value, or ``None`` if it is not a list.

    A bare ``allowed_boards = "8C99"`` is iterable, so accepting any iterable
    would silently expand it into its characters and reject the very board it
    names. Only a list of strings is a board list.
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return tuple(item.strip() for item in value if item.strip())


def load_allowed_boards(path: Path) -> tuple[str, ...]:
    """Read ``allowed_boards`` without validating the rest of the file.

    Recovery commands run from ``ExecStopPost`` and must never be blocked by an
    unrelated configuration error, so any failure falls back to the built-in
    board list rather than raising.
    """
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        boards = board_list(raw["daemon"]["allowed_boards"])
    except (
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
    ):
        return DEFAULT_ALLOWED_BOARDS
    return boards or DEFAULT_ALLOWED_BOARDS
