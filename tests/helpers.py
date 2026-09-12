"""Reusable fixtures and doubles shared by multiple test components."""

import tempfile
from dataclasses import replace
from pathlib import Path

from omen_fanctl.config import (
    Settings,
    hp_factory_performance_curves,
    hp_level_percent,
    hp_level_to_pwm,
)
from omen_fanctl.hardware import (
    AUTO_MODE,
    MANUAL_MODE,
    MAX_MODE,
    Sensors,
    TemperatureSnapshot,
)

from tests import CONFIG_PATH


def settings_with(settings=None, **changes):
    updated = replace(settings or Settings.load(CONFIG_PATH), **changes)
    updated.validate()
    return updated


def fixed_policy_settings():
    """Return stable decision-test inputs independent of the shipped TOML."""
    curves = hp_factory_performance_curves()
    settings = Settings(
        allowed_boards=("8D87",),
        required_profile="performance",
        sample_interval_s=1.0,
        control_interval_s=5.0,
        activation_temp_c=60.0,
        release_temp_c=52.0,
        fan_stop_temp_c=45.0,
        critical_temp_c=92.0,
        critical_release_temp_c=82.0,
        emergency_hold_s=10.0,
        decrease_hysteresis_c=3.0,
        max_rise_percent_per_update=20.0,
        max_fall_percent_per_update=8.0,
        minimum_manual_percent=hp_level_percent(19),
        ewma_rise_alpha=0.10,
        ewma_fall_alpha=0.05,
        include_acpi=False,
        include_amd_gpu=True,
        include_nvidia_gpu=True,
        curve=curves["cpu"],
        ir_release_hysteresis_c=1.0,
        auto_guard_s=180.0,
        stop_handoff_max_temp_c=70.0,
        include_hp_wmi_ir=True,
        curves=tuple(curves.items()),
        curve_source="fixed-test-factory",
    )
    settings.validate()
    return settings


def initialized_sensors(test, **changes):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    cpu = root / "hwmon0"
    cpu.mkdir()
    (cpu / "name").write_text("k10temp\n")
    (cpu / "temp1_input").write_text("50000\n")
    defaults = {
        "include_acpi": False,
        "include_amd_gpu": False,
        "include_nvidia_gpu": False,
        "include_hp_wmi_ir": False,
    }
    defaults.update(changes)
    settings = settings_with(**defaults)
    pci_root = root / "pci"
    pci_root.mkdir()
    if settings.include_nvidia_gpu:
        nvidia = pci_root / "0000:c3:00.0"
        power = nvidia / "power"
        power.mkdir(parents=True)
        (nvidia / "vendor").write_text("0x10de\n")
        (nvidia / "class").write_text("0x030000\n")
        (power / "runtime_status").write_text("active\n")
        nvidia_audio = pci_root / "0000:c3:00.1"
        audio_power = nvidia_audio / "power"
        audio_power.mkdir(parents=True)
        (nvidia_audio / "vendor").write_text("0x10de\n")
        (nvidia_audio / "class").write_text("0x040300\n")
        (audio_power / "runtime_status").write_text("active\n")
    return Sensors(settings, root, pci_root=pci_root)


def initialized_sensors_with_amd_gpu(test, temperature_c=85.0):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    cpu = root / "hwmon0"
    cpu.mkdir()
    (cpu / "name").write_text("k10temp\n")
    (cpu / "temp1_input").write_text("50000\n")
    gpu = root / "hwmon5"
    gpu.mkdir()
    (gpu / "name").write_text("amdgpu\n")
    (gpu / "temp1_input").write_text(f"{temperature_c * 1000:.0f}\n")
    pci_root = root / "pci"
    pci_root.mkdir()
    settings = settings_with(
        include_acpi=False,
        include_amd_gpu=True,
        include_nvidia_gpu=False,
        include_hp_wmi_ir=False,
    )
    return Sensors(settings, root, pci_root=pci_root), root, gpu


class FakeFan:
    """Controller state stub; HpFanHwmon tests own hardware-mode semantics."""

    def __init__(self):
        self.mode = AUTO_MODE
        self.pwm = 100
        self.supports_independent_pwm = False
        self.pwm_abi = "single"
        self.manual_max_level = 56
        self.manual_pwm_max = hp_level_to_pwm(self.manual_max_level)
        self.actions = []

    def status(self):
        return self.mode, self.pwm, 2400, 2600

    def set_manual(self, pwm):
        self.actions.append(("manual", pwm))
        self.mode = MANUAL_MODE
        self.pwm = pwm

    def update_manual(self, pwm, *, write_pwm=True):
        if write_pwm:
            self.actions.append(("update", pwm))
            self.pwm = pwm

    def set_maximum(self):
        self.actions.append(("maximum", 255))
        self.mode = MAX_MODE
        self.pwm = 255

    def restore_auto(self):
        self.actions.append(("auto", None))
        self.mode = AUTO_MODE


class FakeSensors:
    def __init__(self, temperature):
        self.temperature = temperature

    def read(self):
        return TemperatureSnapshot(self.temperature, 50.0, 50.0)
