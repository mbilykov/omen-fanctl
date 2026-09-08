#!/usr/bin/env python3

import os
import select
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "src" / "config" / "fan-control.toml"
SERVICE_PATH = PROJECT_ROOT / "src" / "systemd" / "hp-fan-control.service"
ENTRY_POINT_PATH = PROJECT_ROOT / "src" / "daemon" / "hp_fan_control.py"
DAEMON_PATH = PROJECT_ROOT / "src" / "daemon"
sys.path.insert(0, str(PROJECT_ROOT / "src" / "daemon"))

import hp_fan_control as hp_fan_control_package  # noqa: E402
from hp_fan_control.cli import (  # noqa: E402
    acquire_lock,
    dry_run_lock_path,
    ensure_failsafe_fan_state,
    main,
    parse_args,
    restore_firmware_auto,
    run_actuator_test,
)
from hp_fan_control.config import (  # noqa: E402
    ConfigurationError,
    Curve,
    Settings,
    hp_factory_performance_curves,
    hp_level_percent,
    percent_to_pwm,
    pwm_to_percent,
)
from hp_fan_control.controller import (  # noqa: E402
    ControlPolicy,
    Controller,
    CsvLog,
    Ewma,
    SystemdNotifier,
)
from hp_fan_control.hardware import (  # noqa: E402
    AUTO_MODE,
    MANUAL_MODE,
    MAX_MODE,
    HardwareError,
    HardwareNotReadyError,
    HpFanHwmon,
    PlatformProfileMonitor,
    Sensors,
    TemperatureSnapshot,
    read_hp_wmi_ir_temperature,
    validate_required_profile,
    wait_for_hp_fan_hwmon,
)


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
        include_hp_wmi_ir=True,
        curves=tuple(curves.items()),
        curve_source="fixed-test-factory",
    )
    settings.validate()
    return settings


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, timeout_s):
        self.now += timeout_s


def controller_with_fake_time(**kwargs):
    clock = FakeClock()
    return Controller(clock=clock, wait=clock.wait, **kwargs)


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
    return Sensors(settings, root)


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
    settings = settings_with(
        include_acpi=False,
        include_amd_gpu=True,
        include_nvidia_gpu=False,
        include_hp_wmi_ir=False,
    )
    return Sensors(settings, root), root, gpu


def initialized_fan(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    hp = root / "hwmon0"
    hp.mkdir()
    (hp / "name").write_text("hp\n")
    for name, value in (
        ("pwm1", "100\n"),
        ("pwm1_enable", f"{AUTO_MODE}\n"),
        ("fan1_input", "2400\n"),
        ("fan2_input", "2600\n"),
    ):
        (hp / name).write_text(value)
    return HpFanHwmon(root)


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.curve = Curve((50.0, 60.0, 70.0, 90.0), (25.0, 35.0, 55.0, 95.0))

    def test_clamps_below_and_above_curve(self):
        self.assertAlmostEqual(self.curve.evaluate_percent(20), 25)
        self.assertAlmostEqual(self.curve.evaluate_percent(100), 95)

    def test_rejects_fall_temperatures_for_linear_curve(self):
        with self.assertRaisesRegex(
            ConfigurationError,
            "low_temperature_c requires stepped = true",
        ):
            Curve(
                (50.0, 60.0),
                (30.0, 40.0),
                fall_temperatures=(45.0, 55.0),
            )

    def test_interpolates(self):
        self.assertAlmostEqual(self.curve.evaluate_percent(55), 30)
        self.assertAlmostEqual(self.curve.evaluate_percent(65), 45)
        self.assertAlmostEqual(self.curve.evaluate_percent(80), 75)

    def test_rejects_decreasing_temperature(self):
        with self.assertRaises(ValueError):
            Curve((50.0, 40.0), (20.0, 30.0))

    def test_rejects_decreasing_pwm(self):
        with self.assertRaises(ValueError):
            Curve((40.0, 50.0), (30.0, 20.0))

    def test_factory_step_uses_low_threshold_when_cooling(self):
        curve = hp_factory_performance_curves()["cpu"]
        at_83 = curve.target_percent(83.0)
        self.assertAlmostEqual(at_83, hp_level_percent(37))
        self.assertAlmostEqual(curve.target_percent(79.0, at_83), at_83)
        self.assertAlmostEqual(
            curve.target_percent(78.9, at_83), hp_level_percent(34)
        )

    def test_factory_tables_keep_separate_sensor_thresholds(self):
        curves = hp_factory_performance_curves()
        self.assertAlmostEqual(curves["cpu"].target_percent(60), hp_level_percent(19))
        self.assertAlmostEqual(curves["gpu"].target_percent(60), hp_level_percent(20))
        self.assertAlmostEqual(curves["ir"].target_percent(60), hp_level_percent(37))


class ConversionTests(unittest.TestCase):
    def test_percent_pwm_endpoints(self):
        self.assertEqual(percent_to_pwm(0), 0)
        self.assertEqual(percent_to_pwm(100), 255)

    def test_round_trip(self):
        for percent in (25, 35, 50, 75, 95):
            self.assertAlmostEqual(pwm_to_percent(percent_to_pwm(percent)), percent, delta=0.2)


class LockTests(unittest.TestCase):
    def test_wraps_lock_directory_creation_failure(self):
        lock = Path("/unavailable/control.lock")

        with (
            patch.object(
                Path,
                "mkdir",
                side_effect=PermissionError("permission denied"),
            ),
            self.assertRaisesRegex(HardwareError, "cannot open lock"),
        ):
            acquire_lock(lock)

    def test_refuses_symlink_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            victim = directory / "victim"
            victim.write_text("unchanged\n")
            lock = directory / "control.lock"
            lock.symlink_to(victim)

            with self.assertRaisesRegex(HardwareError, "cannot open lock"):
                acquire_lock(lock)

            self.assertEqual(victim.read_text(), "unchanged\n")

    def test_contended_lock_does_not_truncate_owner_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "control.lock"
            owner = acquire_lock(lock)
            expected = f"{os.getpid()}\n"
            try:
                self.assertEqual(lock.read_text(), expected)
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(
                    HardwareError, "another controller holds"
                ):
                    acquire_lock(lock)
                self.assertEqual(lock.read_text(), expected)
            finally:
                owner.close()

    def test_closes_handle_when_flock_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "control.lock"
            real_fdopen = os.fdopen
            handles = []

            def capture_handle(*args, **kwargs):
                handle = real_fdopen(*args, **kwargs)
                handles.append(handle)
                return handle

            with (
                patch("hp_fan_control.cli.os.fdopen", side_effect=capture_handle),
                patch(
                    "hp_fan_control.cli.fcntl.flock",
                    side_effect=OSError("filesystem failure"),
                ),
                self.assertRaisesRegex(HardwareError, "cannot acquire lock"),
            ):
                acquire_lock(lock)

            self.assertEqual(len(handles), 1)
            self.assertTrue(handles[0].closed)

    def test_closes_handle_when_lock_initialization_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "control.lock"
            handle = Mock()
            handle.write.side_effect = OSError("filesystem failure")

            with (
                patch("hp_fan_control.cli.os.open", return_value=123),
                patch(
                    "hp_fan_control.cli.os.fstat",
                    return_value=SimpleNamespace(
                        st_mode=0o100600,
                        st_uid=os.geteuid(),
                    ),
                ),
                patch("hp_fan_control.cli.os.fchmod"),
                patch("hp_fan_control.cli.os.fdopen", return_value=handle),
                patch("hp_fan_control.cli.fcntl.flock"),
                self.assertRaisesRegex(OSError, "filesystem failure"),
            ):
                acquire_lock(lock)

            handle.close.assert_called_once_with()

    def test_rejects_invalid_lock_metadata_and_closes_descriptor(self):
        invalid_metadata = (
            SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_uid=os.geteuid() + 1,
            ),
            SimpleNamespace(
                st_mode=stat.S_IFIFO | 0o600,
                st_uid=os.geteuid(),
            ),
        )

        for metadata in invalid_metadata:
            with self.subTest(mode=metadata.st_mode, uid=metadata.st_uid):
                with tempfile.TemporaryDirectory() as temporary:
                    lock = Path(temporary) / "control.lock"
                    with (
                        patch("hp_fan_control.cli.os.open", return_value=123),
                        patch("hp_fan_control.cli.os.fstat", return_value=metadata),
                        patch("hp_fan_control.cli.os.close") as close,
                        self.assertRaisesRegex(
                            HardwareError,
                            "lock must be a regular file owned by uid",
                        ),
                    ):
                        acquire_lock(lock)

                    close.assert_called_once_with(123)

    def test_dry_run_lock_is_scoped_to_effective_uid(self):
        with patch("hp_fan_control.cli.os.geteuid", return_value=1234):
            self.assertEqual(
                dry_run_lock_path(),
                Path("/tmp/hp-fan-control-dry-run-1234.lock"),
            )


class EwmaTests(unittest.TestCase):
    def test_asymmetric_update(self):
        ewma = Ewma(rise_alpha=0.5, fall_alpha=0.1)
        self.assertEqual(ewma.update(50), 50)
        self.assertEqual(ewma.update(70), 60)
        self.assertEqual(ewma.update(50), 59)


class ControlDecisionTests(unittest.TestCase):
    def setUp(self):
        self.policy = ControlPolicy(fixed_policy_settings())

    def test_policy_state_views_are_read_only(self):
        with self.assertRaises(AttributeError):
            self.policy.commanded_pwm = 120
        with self.assertRaises(TypeError):
            self.policy.sensor_targets["cpu"] = 50.0

    def test_uses_hottest_sensor(self):
        pwm, hottest = self.policy.desired_pwm(
            {"cpu": 65.0, "gpu": 70.0, "acpi": 60.0}
        )
        self.assertEqual(hottest, 70)
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(23), delta=0.3)
        self.assertEqual(self.policy.winning_sensor, "gpu")

    def test_limits_fan_speed_decrease(self):
        self.policy.set_commanded_pwm(percent_to_pwm(80))
        pwm, _ = self.policy.desired_pwm(
            {"cpu": 60.0, "gpu": 50.0, "acpi": 50.0}
        )
        self.assertAlmostEqual(pwm_to_percent(pwm), 72, delta=0.4)

    def test_rechecks_manual_mode_when_pwm_is_unchanged(self):
        settings = fixed_policy_settings()
        fan = Mock()
        controller = Controller(
            settings=settings,
            fan=fan,
            sensors=None,
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
        )
        controller.manual_active = True
        controller.policy.set_commanded_pwm(120)

        self.assertEqual(controller._apply_manual(120), 120)

        fan.update_manual.assert_called_once_with(120, write_pwm=False)

    def test_raw_temperature_bypasses_ewma_lag_on_rise(self):
        pwm, hottest = self.policy.desired_pwm(
            {"cpu": 55.0, "gpu": 50.0, "acpi": 50.0},
            raw_temperatures={"cpu": 80.0, "gpu": 50.0, "acpi": 50.0},
        )
        self.assertEqual(hottest, 80)
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(31), delta=0.3)

    def test_auto_guard_expires_at_configured_deadline(self):
        controller = Controller(
            fixed_policy_settings(), None, None, False, None, CsvLog(None)
        )
        controller._start_auto_guard(10.0)
        self.assertTrue(controller._auto_guard_active(189.9))
        self.assertFalse(controller._auto_guard_active(190.0))

    def test_auto_guard_marker_lives_until_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard = Path(temporary) / "auto-guard"
            controller = Controller(
                fixed_policy_settings(),
                None,
                None,
                False,
                None,
                CsvLog(None),
                auto_guard_path=guard,
            )
            controller._start_auto_guard(10.0)
            self.assertTrue(guard.exists())
            self.assertTrue(controller._auto_guard_active(100.0))
            self.assertTrue(guard.exists())
            self.assertFalse(controller._auto_guard_active(190.0))
            self.assertFalse(guard.exists())

    def test_controller_rejects_auto_guard_shorter_than_firmware_window(self):
        settings = replace(fixed_policy_settings(), auto_guard_s=119.0)
        with self.assertRaisesRegex(
            ConfigurationError, "auto_guard_s must be at least 120 seconds"
        ):
            Controller(settings, None, None, False, None, CsvLog(None))

    def test_independent_gpu_curve_can_win(self):
        pwm, _ = self.policy.desired_pwm(
            {"cpu": 65.0, "gpu": 75.0, "acpi": 45.0},
            raw_temperatures={"cpu": 65.0, "gpu": 75.0, "acpi": 45.0},
        )
        self.assertEqual(self.policy.winning_sensor, "gpu")
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(31), delta=0.3)

    def test_confirmed_wmi_ir_curve_can_win(self):
        pwm, _ = self.policy.desired_pwm(
            {"cpu": 55.0, "gpu": 50.0, "ir": 54.0, "acpi": None},
            raw_temperatures={"cpu": 55.0, "gpu": 50.0, "ir": 54.0, "acpi": None},
        )
        self.assertEqual(self.policy.winning_sensor, "ir")
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(28), delta=0.3)

    def test_ir_activates_at_first_curve_step_above_manual_floor(self):
        self.assertEqual(self.policy.activation_threshold("ir"), 44.0)
        self.assertFalse(
            self.policy.should_activate(
                TemperatureSnapshot(cpu=55.0, gpu=40.0, acpi=None, ir=43.0)
            )
        )
        self.assertTrue(
            self.policy.should_activate(
                TemperatureSnapshot(cpu=55.0, gpu=40.0, acpi=None, ir=44.0)
            )
        )

    def test_ir_release_threshold_prevents_auto_manual_oscillation(self):
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=40.0, gpu=40.0, acpi=None, ir=44.0)
        )
        self.assertFalse(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(cpu=40.0, gpu=40.0, acpi=None, ir=44.0),
            )
        )
        self.assertTrue(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(cpu=41.0, gpu=41.0, acpi=None, ir=43.0),
            )
        )

    def test_ir_below_activation_does_not_block_cpu_triggered_auto_release(self):
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=60.0, gpu=40.0, acpi=None, ir=43.0)
        )
        self.assertTrue(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(cpu=44.0, gpu=44.0, acpi=None, ir=43.0),
            )
        )

    def test_acpi_proxy_is_telemetry_only(self):
        snapshot = TemperatureSnapshot(
            cpu=44.0, gpu=44.0, acpi=95.0, ir=None
        )

        self.assertEqual(self.policy.activation_sources(snapshot), set())
        self.assertTrue(self.policy.cool_enough_for_auto(snapshot))
        self.assertEqual(snapshot.raw_control_hottest, 44.0)

        pwm, hottest = self.policy.desired_pwm(
            {"cpu": 44.0, "gpu": 44.0, "ir": None, "acpi": 95.0},
            {"cpu": 44.0, "gpu": 44.0, "ir": None, "acpi": 95.0},
        )
        self.assertEqual(hottest, 44.0)
        self.assertEqual(self.policy.winning_sensor, "cpu")
        self.assertAlmostEqual(
            pwm_to_percent(pwm), hp_level_percent(19), delta=0.3
        )
        self.assertAlmostEqual(
            self.policy.sensor_targets["acpi"], hp_level_percent(47)
        )

    def test_acpi_only_input_raises_hardware_error(self):
        with self.assertRaisesRegex(
            HardwareError, "no valid temperature is available for fan control"
        ):
            self.policy.desired_pwm(
                {"cpu": None, "gpu": None, "ir": None, "acpi": 45.0}
            )

    def test_raw_fan_stop_threshold_controls_auto_handoff(self):
        self.assertFalse(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(46.0, 44.0, None)
            )
        )
        self.assertTrue(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(45.0, 45.0, None)
            )
        )


class SettingsTests(unittest.TestCase):
    def test_default_config_is_independent_of_source_tree_layout(self):
        self.assertEqual(
            parse_args([]).config,
            Path("/etc/hp-fan-control/fan-control.toml"),
        )

    def test_loads_factory_preset(self):
        config = CONFIG_PATH
        settings = Settings.load(config)
        self.assertEqual(
            settings.curve_source, "hp-vibrance-stx-n22x9-performance"
        )
        self.assertEqual(
            set(dict(settings.curves or ())),
            {"cpu", "gpu", "ir"},
        )
        self.assertAlmostEqual(
            settings.curve_for("gpu").pwm_percent[-1], hp_level_percent(47)
        )

    def test_per_sensor_curves_require_cpu_curve(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "fan-control.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves.gpu]
temperature_c = [50, 60]
pwm_percent = [30, 40]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "curves.cpu is required",
            ):
                Settings.load(config)

    def test_settings_curves_are_immutable_and_hashable(self):
        settings = Settings.load(CONFIG_PATH)
        self.assertIsInstance(settings.curves, tuple)
        with self.assertRaises(TypeError):
            settings.curves[0] = ("cpu", "clobbered")
        self.assertIsInstance(hash(settings), int)

    def test_required_profile_must_be_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            choices = Path(temporary) / "platform_profile_choices"
            choices.write_text("low-power balanced performance\n")

            validate_required_profile("performance", choices)
            with self.assertRaisesRegex(
                ConfigurationError,
                "required_profile 'performnce' is unavailable",
            ):
                validate_required_profile("performnce", choices)


class SensorMetricTests(unittest.TestCase):
    def test_rediscovers_cpu_after_hwmon_index_changes(self):
        sensors = initialized_sensors(self)
        self.assertEqual(sensors.read().cpu, 50.0)
        new_cpu = sensors.hwmon_root / "hwmon12"
        sensors.cpu_hwmon.rename(new_cpu)

        self.assertEqual(sensors.read().cpu, 50.0)
        self.assertEqual(sensors.cpu_hwmon, new_cpu)

    def test_cpu_loss_fails_safe_and_recovers(self):
        sensors = initialized_sensors(self)
        self.assertEqual(sensors.read().cpu, 50.0)
        offline = sensors.hwmon_root / "offline-k10temp"
        sensors.cpu_hwmon.rename(offline)

        with (
            patch("hp_fan_control.hardware.LOG.warning") as warning,
            patch("hp_fan_control.hardware.LOG.info") as info,
        ):
            with self.assertRaisesRegex(HardwareError, "CPU temperature"):
                sensors.read()
            with self.assertRaises(HardwareError):
                sensors.read()

            recovered = sensors.hwmon_root / "hwmon12"
            offline.rename(recovered)
            self.assertEqual(sensors.read().cpu, 50.0)

        warning.assert_called_once()
        info.assert_called_once_with("%s recovered", "CPU temperature source")

    def test_rediscovers_amd_gpu_after_hwmon_index_changes(self):
        sensors, root, old_gpu = initialized_sensors_with_amd_gpu(self)

        self.assertEqual(sensors.read().gpu, 85.0)
        new_gpu = root / "hwmon14"
        old_gpu.rename(new_gpu)

        self.assertEqual(sensors.read().gpu, 85.0)
        self.assertEqual(sensors.amd_gpu_hwmons, [new_gpu])

    def test_runtime_gpu_loss_fails_safe_until_sensor_recovers(self):
        sensors, root, gpu = initialized_sensors_with_amd_gpu(self)
        self.assertEqual(sensors.read().gpu, 85.0)
        offline = root / "offline-amdgpu"
        gpu.rename(offline)

        with (
            patch("hp_fan_control.hardware.LOG.warning") as warning,
            patch("hp_fan_control.hardware.LOG.info") as info,
        ):
            with self.assertRaisesRegex(
                HardwareError, "AMD GPU temperature source unavailable"
            ):
                sensors.read()
            with self.assertRaises(HardwareError):
                sensors.read()

            recovered = root / "hwmon14"
            offline.rename(recovered)
            self.assertEqual(sensors.read().gpu, 85.0)

        warning.assert_called_once_with(
            "%s unavailable: %s",
            "AMD GPU temperature source",
            "no valid amdgpu temperature was found during rediscovery",
        )
        info.assert_called_once_with(
            "%s recovered", "AMD GPU temperature source"
        )

    def test_runtime_nvidia_loss_fails_safe_and_logs_once(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        available = SimpleNamespace(returncode=0, stdout="61, 80.0, 120.0\n")

        with (
            patch(
                "hp_fan_control.hardware.subprocess.run",
                side_effect=[
                    available,
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    available,
                ],
            ),
            patch("hp_fan_control.hardware.LOG.warning") as warning,
            patch("hp_fan_control.hardware.LOG.info") as info,
        ):
            self.assertEqual(sensors.read().gpu, 61.0)
            self.assertEqual(sensors.read().gpu, 61.0)
            self.assertEqual(sensors.read().gpu, 61.0)
            with self.assertRaisesRegex(HardwareError, "NVIDIA GPU"):
                sensors.read()
            with self.assertRaises(HardwareError):
                sensors.read()
            self.assertEqual(sensors.read().gpu, 61.0)

        warning.assert_called_once()
        info.assert_called_once_with(
            "%s recovered", "NVIDIA GPU temperature source"
        )

    def test_nvidia_failure_includes_stderr(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        available = SimpleNamespace(
            returncode=0,
            stdout="61, 80.0, 120.0\n",
            stderr="",
        )
        failed = SimpleNamespace(
            returncode=9,
            stdout="",
            stderr="Failed to initialize NVML:\nDriver/library version mismatch\n",
        )

        with patch(
            "hp_fan_control.hardware.subprocess.run",
            side_effect=[available, failed, failed, failed],
        ):
            self.assertEqual(sensors.read().gpu, 61.0)
            self.assertEqual(sensors.read().gpu, 61.0)
            self.assertEqual(sensors.read().gpu, 61.0)
            with self.assertRaisesRegex(
                HardwareError,
                "status 9: Failed to initialize NVML: "
                "Driver/library version mismatch",
            ):
                sensors.read()

    def test_retries_nvidia_tool_discovery(self):
        result = SimpleNamespace(returncode=0, stdout="61, 80.0, 120.0\n")
        with (
            patch(
                "hp_fan_control.hardware.shutil.which",
                side_effect=[None, "/usr/bin/nvidia-smi"],
            ) as which,
            patch(
                "hp_fan_control.hardware.subprocess.run",
                return_value=result,
            ),
            patch(
                "hp_fan_control.hardware.time.monotonic",
                side_effect=[100.0, 100.0, 129.9, 130.0],
            ),
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
            self.assertIsNone(sensors.read().gpu)
            self.assertIsNone(sensors.read().gpu)
            self.assertEqual(which.call_count, 1)
            self.assertEqual(sensors.read().gpu, 61.0)

        self.assertEqual(which.call_count, 2)

    def test_missing_optional_ir_interface_does_not_block_sensor_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cpu = root / "hwmon0"
            cpu.mkdir()
            (cpu / "name").write_text("k10temp\n")
            (cpu / "temp1_input").write_text("50000\n")
            settings = Settings.load(CONFIG_PATH)
            settings = settings_with(
                settings,
                include_acpi=False,
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                hp_wmi_sensors_path=root / "missing-interface",
            )
            sensors = Sensors(settings, root)
            snapshot = sensors.read()
            self.assertEqual(snapshot.cpu, 50.0)
            self.assertIsNone(snapshot.ir)
            self.assertTrue(sensors.ir_health.failed)

    def test_reads_index_zero_hp_wmi_ir_temperature(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hp_wmi_sensors"
            path.write_text(
                "index name temp_c\n0 IR 39\n1 Ambient 48\n2 PCH 55\n3 VR 51\n"
            )
            self.assertEqual(read_hp_wmi_ir_temperature(path), 39.0)

    def test_rejects_failed_hp_wmi_ir_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hp_wmi_sensors"
            path.write_text("index name temp_c\n0 IR error:-121\n")
            with self.assertRaises(HardwareError):
                read_hp_wmi_ir_temperature(path)

    def test_runtime_ir_loss_falls_back_to_cpu_gpu_and_recovers(self):
        sensors = initialized_sensors(
            self,
            include_hp_wmi_ir=True,
            hp_wmi_sensors_path=Path("/proc/hp_wmi_sensors"),
        )
        with patch(
            "hp_fan_control.hardware.read_hp_wmi_ir_temperature",
            side_effect=[HardwareError("missing"), 41.0],
        ):
            first = sensors.read()
            self.assertTrue(sensors.ir_health.failed)
            second = sensors.read()
            self.assertFalse(sensors.ir_health.failed)
        self.assertIsNone(first.ir)
        self.assertEqual(second.ir, 41.0)

    def test_reads_nvidia_temperature_draw_and_limit(self):
        sensors = initialized_sensors(self)
        sensors.nvidia_smi = "/usr/bin/nvidia-smi"
        result = SimpleNamespace(returncode=0, stdout="72, 174.5, 175.0\n")
        with patch("hp_fan_control.hardware.subprocess.run", return_value=result):
            snapshot = sensors.read()
        self.assertEqual(snapshot.gpu, 72.0)
        self.assertEqual(snapshot.nvidia_power_draw_w, 174.5)
        self.assertEqual(snapshot.nvidia_power_limit_w, 175.0)

    def test_keeps_temperature_when_power_is_unavailable(self):
        sensors = initialized_sensors(self)
        sensors.nvidia_smi = "/usr/bin/nvidia-smi"
        result = SimpleNamespace(returncode=0, stdout="61, [N/A], [N/A]\n")
        with patch("hp_fan_control.hardware.subprocess.run", return_value=result):
            snapshot = sensors.read()
        self.assertEqual(snapshot.gpu, 61.0)
        self.assertIsNone(snapshot.nvidia_power_draw_w)
        self.assertIsNone(snapshot.nvidia_power_limit_w)


class FakeFan:
    def __init__(self):
        self.mode = AUTO_MODE
        self.pwm = 100
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


class FailingAfterFirstSample:
    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.reads == 1:
            return TemperatureSnapshot(cpu=70.0, gpu=50.0, acpi=None, ir=45.0)
        raise HardwareError("mandatory CPU source disappeared")


class SequenceSensors:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.last = None

    def read(self):
        try:
            self.last = next(self.snapshots)
        except StopIteration:
            pass
        return self.last


class ControllerLoopTests(unittest.TestCase):
    def test_csv_fields_match_log_sample_row(self):
        csv_log = Mock(spec=CsvLog)
        controller = Controller(
            settings=Settings.load(CONFIG_PATH),
            fan=FakeFan(),
            sensors=FakeSensors(70),
            apply=False,
            duration_s=None,
            csv_log=csv_log,
        )
        snapshot = TemperatureSnapshot(
            cpu=70.0,
            gpu=60.0,
            acpi=50.0,
            ir=55.0,
            nvidia_power_draw_w=100.0,
            nvidia_power_limit_w=150.0,
        )
        filtered = {"cpu": 69.0, "gpu": 59.0, "ir": 54.0, "acpi": 49.0}

        controller.log_sample(
            0.0,
            "performance",
            "manual",
            snapshot,
            filtered,
            70.0,
            180,
            "contract check",
        )

        csv_log.write.assert_called_once()
        row = csv_log.write.call_args.args[0]
        self.assertCountEqual(row, CsvLog.FIELDS)

    def test_ir_manual_floor_bucket_stays_in_firmware_auto(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(CONFIG_PATH)
            sensors = Mock()
            sensors.read.return_value = TemperatureSnapshot(
                cpu=44.0, gpu=44.0, acpi=None, ir=43.0
            )
            fan = FakeFan()
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=sensors,
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
            )

            controller.run()

        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertEqual(fan.actions, [])

    def test_acpi_proxy_above_critical_cannot_leave_firmware_auto(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(CONFIG_PATH)
            settings = settings_with(settings, include_acpi=True)
            sensors = Mock()
            sensors.read.return_value = TemperatureSnapshot(
                cpu=44.0, gpu=44.0, acpi=95.0, ir=None
            )
            fan = FakeFan()
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=sensors,
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
            )

            controller.run()

        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertFalse(controller.emergency)
        self.assertEqual(fan.actions, [])

    def test_raw_cpu_or_gpu_at_critical_threshold_selects_maximum(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(CONFIG_PATH)

            for sensor_name in ("cpu", "gpu"):
                with self.subTest(sensor=sensor_name):
                    temperatures = {"cpu": 50.0, "gpu": 50.0}
                    temperatures[sensor_name] = settings.critical_temp_c
                    sensors = Mock()
                    sensors.read.return_value = TemperatureSnapshot(
                        cpu=temperatures["cpu"],
                        gpu=temperatures["gpu"],
                        acpi=None,
                        ir=None,
                    )
                    fan = FakeFan()
                    controller = controller_with_fake_time(
                        settings=settings,
                        fan=fan,
                        sensors=sensors,
                        apply=True,
                        duration_s=2.5,
                        csv_log=CsvLog(None),
                        profile_path=profile,
                    )

                    controller.run()

                    self.assertTrue(controller.emergency)
                    self.assertEqual(fan.actions[0], ("maximum", 255))
                    self.assertNotIn("manual", (action for action, _ in fan.actions))
                    self.assertEqual(fan.mode, MAX_MODE)

    def test_repeated_status_note_is_rate_limited(self):
        settings = Settings.load(CONFIG_PATH)
        controller = Controller(
            settings=settings,
            fan=FakeFan(),
            sensors=FakeSensors(70),
            apply=False,
            duration_s=None,
            csv_log=CsvLog(None),
            status_interval_s=30,
        )
        snapshot = TemperatureSnapshot(70, 50, None, None)
        filtered = {"cpu": 70.0, "gpu": 50.0, "ir": None, "acpi": None}
        with (
            patch("hp_fan_control.controller.LOG.info") as log_info,
            patch("hp_fan_control.controller.time.monotonic", return_value=1.0),
        ):
            controller.log_sample(
                0, "balanced", "handoff", snapshot, filtered, 70, 100,
                "cooling before firmware Auto",
            )
            controller.log_sample(
                0, "balanced", "handoff", snapshot, filtered, 70, 100,
                "cooling before firmware Auto",
            )
            controller.log_sample(
                0, "balanced", "handoff", snapshot, filtered, 70, 100,
                "new handoff detail",
            )
        self.assertEqual(log_info.call_count, 2)

    def test_non_performance_profile_sleeps_without_reading_sensors(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            settings = Settings.load(CONFIG_PATH)
            sensors = Mock()
            notifier = Mock(spec=SystemdNotifier)
            controller = controller_with_fake_time(
                settings=settings,
                fan=FakeFan(),
                sensors=sensors,
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
                notifier=notifier,
                inactive_event_wait_s=1.0,
            )
            controller.run()
        sensors.read.assert_not_called()
        notifier.ready.assert_called_once_with()
        self.assertGreaterEqual(notifier.watchdog.call_count, 1)

    def test_injected_wait_refreshes_profile_during_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            clock = FakeClock()
            sensors = Mock()
            sensors.read.return_value = TemperatureSnapshot(50, 50, None, None)

            def switch_to_performance(timeout_s):
                profile.write_text("performance\n")
                clock.wait(timeout_s)

            controller = Controller(
                settings=Settings.load(CONFIG_PATH),
                fan=FakeFan(),
                sensors=sensors,
                apply=True,
                duration_s=2.0,
                csv_log=CsvLog(None),
                profile_path=profile,
                inactive_event_wait_s=1.0,
                clock=clock,
                wait=switch_to_performance,
            )

            controller.run()

        sensors.read.assert_called_once_with()

    def test_new_heat_during_auto_guard_reclaims_manual_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            settings = Settings.load(CONFIG_PATH)
            fan = FakeFan()
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=FakeSensors(70),
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
                inactive_event_wait_s=1.0,
            )
            controller.auto_guard_until = float("inf")
            controller.run()
        self.assertEqual(fan.actions[0][0], "manual")
        self.assertNotIn(("auto", None), fan.actions)

    def test_new_heat_after_auto_handoff_starts_a_new_manual_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            settings = Settings.load(CONFIG_PATH)
            settings = settings_with(settings, emergency_hold_s=0.0)
            cool = TemperatureSnapshot(44, 44, None, None)
            hot = TemperatureSnapshot(70, 50, None, None)
            fan = FakeFan()
            fan.mode = MAX_MODE
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=SequenceSensors([cool, cool, hot]),
                apply=True,
                duration_s=3.0,
                csv_log=CsvLog(None),
                profile_path=profile,
                inactive_event_wait_s=1.0,
            )
            controller.run()
        auto_index = fan.actions.index(("auto", None))
        later_actions = fan.actions[auto_index + 1 :]
        self.assertTrue(any(action == "manual" for action, _ in later_actions))

    def test_cpu_sensor_loss_during_auto_guard_selects_maximum(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            settings = Settings.load(CONFIG_PATH)
            fan = FakeFan()
            sensors = Mock()
            sensors.read.side_effect = HardwareError("CPU unavailable")
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=sensors,
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
                inactive_event_wait_s=1.0,
            )
            controller.auto_guard_until = float("inf")
            controller.run()
        self.assertIn(("maximum", 255), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)

    def test_leaving_performance_keeps_hot_manual_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            settings = Settings.load(CONFIG_PATH)
            fan = FakeFan()
            fan.mode = MAX_MODE
            sensors = FakeSensors(70)
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=sensors,
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
                inactive_event_wait_s=1.0,
            )
            controller.run()
        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.actions[-1], ("maximum", 255))
        self.assertEqual(fan.mode, MAX_MODE)

    def test_cool_handoff_monitors_auto_before_sleeping(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("balanced\n")
            settings = Settings.load(CONFIG_PATH)
            settings = settings_with(
                settings,
                emergency_hold_s=0.0,
                auto_guard_s=120.0,
            )
            fan = FakeFan()
            fan.mode = MAX_MODE
            sensors = Mock()
            sensors.read.return_value = TemperatureSnapshot(44, 44, None, None)
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=sensors,
                apply=True,
                duration_s=122.0,
                csv_log=CsvLog(None),
                profile_path=profile,
                status_interval_s=1000.0,
                inactive_event_wait_s=1.0,
            )
            controller.run()
        self.assertIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_hot_timed_exit_selects_maximum(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(CONFIG_PATH)
            fan = FakeFan()
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=FakeSensors(70),
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
            )
            controller.run()
            self.assertEqual(fan.actions[0][0], "manual")
            self.assertEqual(fan.actions[-1][0], "maximum")
            self.assertEqual(fan.mode, MAX_MODE)

    def test_systemd_watchdog_tracks_controller_progress_and_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(CONFIG_PATH)
            notifier = Mock(spec=SystemdNotifier)
            controller = controller_with_fake_time(
                settings=settings,
                fan=FakeFan(),
                sensors=FakeSensors(50),
                apply=True,
                duration_s=2.5,
                csv_log=CsvLog(None),
                profile_path=profile,
                notifier=notifier,
            )
            controller.run()
        notifier.ready.assert_called_once_with()
        self.assertGreaterEqual(notifier.watchdog.call_count, 1)
        notifier.stopping.assert_called_once_with()

    def test_mandatory_sensor_loss_and_exit_preserve_maximum(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(CONFIG_PATH)
            fan = FakeFan()
            controller = controller_with_fake_time(
                settings=settings,
                fan=fan,
                sensors=FailingAfterFirstSample(),
                apply=True,
                duration_s=3.5,
                csv_log=CsvLog(None),
                profile_path=profile,
            )
            controller.run()
            self.assertEqual(fan.actions[0][0], "manual")
            self.assertIn(("maximum", 255), fan.actions)
            self.assertEqual(fan.actions[-1][0], "maximum")
            self.assertEqual(fan.mode, MAX_MODE)

    def test_actuator_test_restores_auto(self):
        fan = FakeFan()
        with (
            patch("hp_fan_control.cli.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
            patch("hp_fan_control.cli.time.sleep"),
            patch("hp_fan_control.cli.signal.signal"),
        ):
            run_actuator_test(fan, FakeSensors(50), 60, 1)
        self.assertEqual(fan.actions[0][0], "manual")
        self.assertEqual(fan.actions[-1][0], "auto")
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_restore_auto_recovery_command(self):
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        restore_firmware_auto(fan)
        self.assertEqual(fan.actions, [("auto", None)])
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_failsafe_recovery_replaces_manual_with_maximum(self):
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        ensure_failsafe_fan_state(fan)
        self.assertEqual(fan.actions, [("maximum", 255)])
        self.assertEqual(fan.mode, MAX_MODE)

    def test_failsafe_recovery_preserves_existing_auto(self):
        fan = FakeFan()
        ensure_failsafe_fan_state(fan)
        self.assertEqual(fan.actions, [])
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_failsafe_recovery_replaces_guarded_auto_with_maximum(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard = Path(temporary) / "auto-guard"
            guard.write_text("123\n")
            fan = FakeFan()
            ensure_failsafe_fan_state(fan, guard)
        self.assertEqual(fan.actions, [("maximum", 255)])
        self.assertEqual(fan.mode, MAX_MODE)
        self.assertFalse(guard.exists())

    def test_restore_auto_does_not_depend_on_configuration(self):
        lock = Mock()
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        with (
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=lock),
            patch("hp_fan_control.cli.HpFanHwmon", return_value=fan),
            patch("hp_fan_control.cli.Settings.load") as load_settings,
        ):
            self.assertEqual(main(["--restore-auto"]), 0)
        load_settings.assert_not_called()
        lock.close.assert_called_once_with()
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_failsafe_command_does_not_depend_on_configuration(self):
        lock = Mock()
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        with tempfile.TemporaryDirectory() as temporary:
            guard = Path(temporary) / "missing-auto-guard"
            with (
                patch("hp_fan_control.cli.read_text", return_value="8D87"),
                patch("hp_fan_control.cli.os.geteuid", return_value=0),
                patch("hp_fan_control.cli.acquire_lock", return_value=lock),
                patch("hp_fan_control.cli.HpFanHwmon", return_value=fan),
                patch("hp_fan_control.cli.wait_for_hp_fan_hwmon") as wait_for_hwmon,
                patch("hp_fan_control.cli.AUTO_GUARD_PATH", guard),
                patch("hp_fan_control.cli.Settings.load") as load_settings,
            ):
                self.assertEqual(main(["--failsafe"]), 0)
        load_settings.assert_not_called()
        wait_for_hwmon.assert_not_called()
        lock.close.assert_called_once_with()
        self.assertEqual(fan.mode, MAX_MODE)

    def test_main_closes_lock_when_hwmon_startup_times_out(self):
        lock = Mock()

        def fake_read_text(path):
            if path.name == "board_name":
                return "8D87"
            if path.name == "platform_profile_choices":
                return "balanced performance"
            raise AssertionError(f"unexpected read: {path}")

        with (
            patch("hp_fan_control.cli.read_text", side_effect=fake_read_text),
            patch("hp_fan_control.cli.acquire_lock", return_value=lock),
            patch(
                "hp_fan_control.cli.wait_for_hp_fan_hwmon",
                side_effect=HardwareError("hp hwmon startup timeout"),
            ),
        ):
            self.assertEqual(
                main(["--config", str(CONFIG_PATH), "--no-log-file"]),
                1,
            )

        lock.close.assert_called_once_with()

    def test_main_rejects_unavailable_required_profile_before_locking(self):
        settings = Settings.load(CONFIG_PATH)
        settings = settings_with(settings, required_profile="performnce")
        acquire = Mock()

        def fake_read_text(path):
            if path.name == "board_name":
                return "8D87"
            if path.name == "platform_profile_choices":
                return "low-power balanced performance"
            raise AssertionError(f"unexpected read: {path}")

        with (
            patch("hp_fan_control.cli.Settings.load", return_value=settings),
            patch("hp_fan_control.cli.read_text", side_effect=fake_read_text),
            patch("hp_fan_control.cli.acquire_lock", acquire),
        ):
            self.assertEqual(main(["--no-log-file"]), 1)

        acquire.assert_not_called()

    def test_failsafe_closes_lock_when_hwmon_initialization_fails(self):
        lock = Mock()
        with (
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=lock),
            patch(
                "hp_fan_control.cli.HpFanHwmon",
                side_effect=HardwareError("hp hwmon unavailable"),
            ),
        ):
            self.assertEqual(main(["--failsafe"]), 1)

        lock.close.assert_called_once_with()


class SystemdNotifierTests(unittest.TestCase):
    def test_abstract_notify_socket_is_supported(self):
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        with (
            patch.dict(
                "hp_fan_control.controller.os.environ",
                {"NOTIFY_SOCKET": "@notify", "WATCHDOG_PID": str(os.getpid())},
                clear=True,
            ),
            patch("hp_fan_control.controller.socket.socket", return_value=context),
        ):
            notifier = SystemdNotifier.from_environment()
            notifier.watchdog()
        connection.sendto.assert_called_once_with(b"WATCHDOG=1", "\0notify")

    def test_watchdog_for_another_pid_does_not_suppress_ready(self):
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        with (
            patch.dict(
                "hp_fan_control.controller.os.environ",
                {"NOTIFY_SOCKET": "/run/notify", "WATCHDOG_PID": "999999"},
                clear=True,
            ),
            patch("hp_fan_control.controller.socket.socket", return_value=context),
        ):
            notifier = SystemdNotifier.from_environment()
            notifier.ready()
            notifier.watchdog()

        self.assertEqual(notifier.address, "/run/notify")
        self.assertFalse(notifier.watchdog_enabled)
        connection.sendto.assert_called_once_with(b"READY=1", "/run/notify")

    def test_transient_notification_failure_is_retried(self):
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        notifier = SystemdNotifier("/run/notify")

        with (
            patch(
                "hp_fan_control.controller.socket.socket",
                side_effect=[OSError("temporary failure"), context],
            ) as socket_factory,
            patch("hp_fan_control.controller.LOG.warning") as warning,
            patch("hp_fan_control.controller.LOG.info") as info,
        ):
            notifier.ready()
            notifier.watchdog()

        self.assertEqual(socket_factory.call_count, 2)
        warning.assert_called_once()
        info.assert_called_once_with("systemd notification channel recovered")
        connection.sendto.assert_called_once_with(b"WATCHDOG=1", "/run/notify")
        self.assertFalse(notifier.failed)


class PlatformProfileMonitorTests(unittest.TestCase):
    def test_sysfs_notification_refreshes_cached_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform_profile"
            path.write_text("balanced\n")
            poller = Mock()
            poller.poll.return_value = [(7, select.POLLPRI)]
            with patch("hp_fan_control.hardware.select.poll", return_value=poller):
                monitor = PlatformProfileMonitor(path)
                self.assertEqual(monitor.current, "balanced")
                path.write_text("performance\n")
                self.assertTrue(monitor.wait_for_change(5.0))
                self.assertEqual(monitor.current, "performance")
                monitor.close()
        poller.poll.assert_called_once_with(5000)


class FakeHwmonTests(unittest.TestCase):
    def test_missing_hp_hwmon_is_temporarily_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(HardwareNotReadyError, "was not found"):
                HpFanHwmon(Path(temporary))

    def test_incomplete_hp_hwmon_is_temporarily_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            hp = Path(temporary) / "hwmon7"
            hp.mkdir()
            (hp / "name").write_text("hp\n")

            with self.assertRaisesRegex(
                HardwareNotReadyError,
                "required hp-wmi attribute is missing",
            ):
                HpFanHwmon(Path(temporary))

    def test_multiple_hp_hwmon_devices_fail_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("hwmon7", "hwmon8"):
                directory = root / name
                directory.mkdir()
                (directory / "name").write_text("hp\n")

            with self.assertRaisesRegex(HardwareError, "found 2") as caught:
                HpFanHwmon(root)

            self.assertNotIsInstance(caught.exception, HardwareNotReadyError)

    def test_safe_manual_transition_and_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hp = root / "hwmon8"
            hp.mkdir()
            (hp / "name").write_text("hp\n")
            (hp / "pwm1").write_text("0\n")
            (hp / "pwm1_enable").write_text(f"{AUTO_MODE}\n")
            (hp / "fan1_input").write_text("3400\n")
            (hp / "fan2_input").write_text("3600\n")

            fan = HpFanHwmon(root)
            fan.set_manual(178)
            self.assertEqual(int((hp / "pwm1").read_text()), 178)
            self.assertEqual(int((hp / "pwm1_enable").read_text()), MANUAL_MODE)
            fan.restore_auto()
            self.assertEqual(int((hp / "pwm1_enable").read_text()), AUTO_MODE)

    def test_failed_initial_pwm_write_rolls_manual_mode_back_to_auto(self):
        fan = initialized_fan(self)
        failure = HardwareError("PWM write failed")
        with patch(
            "hp_fan_control.hardware.write_int",
            side_effect=[None, failure, None],
        ) as write:
            with self.assertRaisesRegex(HardwareError, "PWM write failed"):
                fan.set_manual(100)
        self.assertEqual(
            write.call_args_list,
            [
                call(fan.enable, MANUAL_MODE),
                call(fan.pwm, 100),
                call(fan.enable, AUTO_MODE),
            ],
        )

    def test_update_attempts_single_manual_mode_recovery(self):
        fan = initialized_fan(self)

        with (
            patch("hp_fan_control.hardware.read_int", return_value=AUTO_MODE),
            patch("hp_fan_control.hardware.write_int") as write,
        ):
            fan.update_manual(120)

        self.assertEqual(
            write.call_args_list,
            [
                call(fan.enable, MANUAL_MODE),
                call(fan.pwm, 120),
            ],
        )

    def test_update_fails_if_manual_mode_is_lost_again_after_recovery(self):
        fan = initialized_fan(self)

        with (
            patch("hp_fan_control.hardware.read_int", return_value=AUTO_MODE),
            patch("hp_fan_control.hardware.write_int") as write,
        ):
            fan.update_manual(120)
            with self.assertRaisesRegex(
                HardwareError,
                "lost again immediately after recovery",
            ):
                fan.update_manual(120, write_pwm=False)

        self.assertEqual(
            write.call_args_list,
            [
                call(fan.enable, MANUAL_MODE),
                call(fan.pwm, 120),
            ],
        )

    def test_update_does_not_rewrite_unchanged_manual_pwm(self):
        fan = initialized_fan(self)

        with (
            patch("hp_fan_control.hardware.read_int", return_value=MANUAL_MODE),
            patch("hp_fan_control.hardware.write_int") as write,
        ):
            fan.update_manual(120, write_pwm=False)

        write.assert_not_called()

    def test_update_preserves_externally_asserted_maximum_mode(self):
        fan = initialized_fan(self)

        with (
            patch("hp_fan_control.hardware.read_int", return_value=MAX_MODE),
            patch("hp_fan_control.hardware.write_int") as write,
        ):
            fan.update_manual(100)

        write.assert_not_called()

    def test_update_rejects_unknown_mode_without_writing(self):
        fan = initialized_fan(self)

        with (
            patch("hp_fan_control.hardware.read_int", return_value=3),
            patch("hp_fan_control.hardware.write_int") as write,
            self.assertRaisesRegex(HardwareError, "unexpected fan mode"),
        ):
            fan.update_manual(100)

        write.assert_not_called()


class HwmonStartupTests(unittest.TestCase):
    def test_waits_for_hwmon_attributes_on_real_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hp = root / "hwmon7"
            hp.mkdir()
            (hp / "name").write_text("hp\n")

            def publish_attributes(_delay):
                (hp / "pwm1").write_text("0\n")
                (hp / "pwm1_enable").write_text(f"{AUTO_MODE}\n")
                (hp / "fan1_input").write_text("0\n")
                (hp / "fan2_input").write_text("0\n")

            with (
                patch(
                    "hp_fan_control.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "hp_fan_control.hardware.time.sleep",
                    side_effect=publish_attributes,
                ) as sleep,
                patch("hp_fan_control.hardware.LOG.info") as log_info,
            ):
                fan = wait_for_hp_fan_hwmon(root=root)

            self.assertEqual(fan.path, hp)
            sleep.assert_called_once_with(1.0)
            log_info.assert_called_once_with("hp hwmon interface became ready")

    def test_retries_transient_hp_hwmon_absence(self):
        fan = Mock(spec=HpFanHwmon)
        with (
            patch(
                "hp_fan_control.hardware.HpFanHwmon",
                side_effect=[HardwareNotReadyError("not ready"), fan],
            ) as constructor,
            patch("hp_fan_control.hardware.time.monotonic", side_effect=[100.0, 100.0]),
            patch("hp_fan_control.hardware.time.sleep") as sleep,
        ):
            self.assertIs(wait_for_hp_fan_hwmon(), fan)

        self.assertEqual(constructor.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_fails_after_hp_hwmon_startup_timeout(self):
        with (
            patch(
                "hp_fan_control.hardware.HpFanHwmon",
                side_effect=HardwareNotReadyError("not ready"),
            ),
            patch("hp_fan_control.hardware.time.monotonic", side_effect=[100.0, 120.0]),
            patch("hp_fan_control.hardware.time.sleep") as sleep,
            self.assertRaisesRegex(
                HardwareError,
                "did not become ready within 20 seconds",
            ),
        ):
            wait_for_hp_fan_hwmon()

        sleep.assert_not_called()

    def test_does_not_retry_non_transient_hwmon_error(self):
        with (
            patch(
                "hp_fan_control.hardware.HpFanHwmon",
                side_effect=HardwareError("multiple hp devices"),
            ),
            patch("hp_fan_control.hardware.time.sleep") as sleep,
            self.assertRaisesRegex(HardwareError, "multiple hp devices"),
        ):
            wait_for_hp_fan_hwmon()

        sleep.assert_not_called()


class EntryPointTests(unittest.TestCase):
    def test_package_exports_every_name_declared_in_all(self):
        exports = hp_fan_control_package.__all__

        self.assertEqual(len(exports), len(set(exports)))
        for name in exports:
            with self.subTest(name=name):
                self.assertTrue(hasattr(hp_fan_control_package, name))

    def test_script_entry_point_displays_help(self):
        result = subprocess.run(
            [sys.executable, str(ENTRY_POINT_PATH), "--help"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_module_entry_point_displays_help(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(DAEMON_PATH)
        result = subprocess.run(
            [sys.executable, "-m", "hp_fan_control", "--help"],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)


class SystemdUnitTests(unittest.TestCase):
    def test_restart_policy_avoids_start_limit(self):
        service = SERVICE_PATH.read_text(encoding="utf-8")
        self.assertIn("StartLimitIntervalSec=60\n", service)
        self.assertIn("StartLimitBurst=10\n", service)
        self.assertIn("Restart=always\n", service)
        self.assertIn("RestartSec=10\n", service)


if __name__ == "__main__":
    unittest.main()
