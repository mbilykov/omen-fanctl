#!/usr/bin/env python3

import csv
import inspect
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "src" / "config" / "fan-control.toml"
SERVICE_PATH = PROJECT_ROOT / "src" / "systemd" / "hp-fan-control.service"
LOGROTATE_PATH = PROJECT_ROOT / "src" / "logrotate" / "hp-fan-control"
ENTRY_POINT_PATH = PROJECT_ROOT / "src" / "daemon" / "hp_fan_control.py"
DAEMON_PATH = PROJECT_ROOT / "src" / "daemon"
sys.path.insert(0, str(PROJECT_ROOT / "src" / "daemon"))

import hp_fan_control as hp_fan_control_package  # noqa: E402
from hp_fan_control.cli import (  # noqa: E402
    AUTO_GUARD_PATH,
    CONFIGURATION_ERROR_EXIT_STATUS,
    clear_confirmed_board,
    clear_confirmed_board_if_safe,
    systemd_owns_runtime_directory,
    record_confirmed_board,
    recovery_allowed_boards,
    acquire_lock,
    _csv_log_path,
    dry_run_lock_path,
    ensure_failsafe_fan_state,
    main,
    parse_args,
    restore_firmware_auto,
    run_actuator_test,
)
from hp_fan_control.config import (  # noqa: E402
    DEFAULT_ALLOWED_BOARDS,
    ConfigurationError,
    Curve,
    Settings,
    hp_factory_performance_curves,
    hp_level_percent,
    load_allowed_boards,
    percent_to_pwm,
    pwm_to_percent,
)
from hp_fan_control.controller import (  # noqa: E402
    ControlPolicy,
    Controller,
    CsvLog,
    Ewma,
    OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES,
    SystemdNotifier,
)
from hp_fan_control.hardware import (  # noqa: E402
    AUTO_MODE,
    FailurePolicy,
    HP_HWMON_STARTUP_TIMEOUT_S,
    K10TEMP_STARTUP_TIMEOUT_S,
    MANUAL_MODE,
    MAX_MODE,
    PLATFORM_PROFILE_STARTUP_TIMEOUT_S,
    HardwareError,
    HardwareNotReadyError,
    HpFanHwmon,
    PlatformProfileMonitor,
    Sensors,
    SourceHealth,
    TemperatureSnapshot,
    read_hp_wmi_ir_temperature,
    validate_required_profile,
    wait_for_hp_fan_hwmon,
    wait_for_temperature_sensors,
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


def parse_systemd_settings(unit: str) -> dict[str, str]:
    settings = {}
    for raw_line in unit.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key] = value
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

    def test_rejects_duplicate_pwm_levels_with_falling_thresholds(self):
        with self.assertRaisesRegex(
            ConfigurationError,
            "PWM values must be strictly increasing when low_temperature_c is set",
        ):
            Curve(
                (50.0, 60.0),
                (30.0, 30.0),
                fall_temperatures=(45.0, 55.0),
                stepped=True,
            )

    def test_rejects_non_finite_curve_values(self):
        cases = (
            (
                "temperatures",
                ((50.0, float("nan")), (30.0, 40.0), None, False),
            ),
            (
                "PWM values",
                ((50.0, 60.0), (30.0, float("inf")), None, False),
            ),
            (
                "falling temperatures",
                (
                    (50.0, 60.0),
                    (30.0, 40.0),
                    (45.0, float("nan")),
                    True,
                ),
            ),
        )

        for message, arguments in cases:
            with (
                self.subTest(values=message),
                self.assertRaisesRegex(
                    ConfigurationError, f"curve {message} must be finite"
                ),
            ):
                Curve(*arguments)

    def test_factory_step_uses_low_threshold_when_cooling(self):
        curve = hp_factory_performance_curves()["cpu"]
        at_83 = curve.target_percent(83.0)
        self.assertAlmostEqual(at_83, hp_level_percent(37))
        self.assertAlmostEqual(curve.target_percent(79.0, at_83), at_83)
        self.assertAlmostEqual(curve.target_percent(78.9, at_83), hp_level_percent(34))

    def test_factory_step_rejects_previous_value_between_levels(self):
        curve = hp_factory_performance_curves()["cpu"]

        for temperature in (77.3, 84.0):
            with (
                self.subTest(temperature=temperature),
                self.assertRaisesRegex(
                    ValueError,
                    "previous stepped target is not a curve level",
                ),
            ):
                curve.target_percent(temperature, 66.9)

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
            self.assertAlmostEqual(
                pwm_to_percent(percent_to_pwm(percent)), percent, delta=0.2
            )

    def test_half_pwm_values_round_up(self):
        self.assertEqual(percent_to_pwm(hp_level_percent(22)), 94)
        self.assertEqual(percent_to_pwm(hp_level_percent(34)), 145)


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
                with self.assertRaisesRegex(HardwareError, "another controller holds"):
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

    def test_missing_optional_sensor_resets_its_filter(self):
        controller = Controller(
            settings=fixed_policy_settings(),
            fan=FakeFan(),
            sensors=None,
            apply=False,
            duration_s=None,
            csv_log=CsvLog(None),
        )
        hot = TemperatureSnapshot(cpu=50.0, gpu=50.0, acpi=None, ir=64.0)
        missing = TemperatureSnapshot(cpu=50.0, gpu=50.0, acpi=None, ir=None)
        recovered = TemperatureSnapshot(cpu=50.0, gpu=50.0, acpi=None, ir=40.0)

        for _ in range(40):
            controller._filtered(hot)
        for _ in range(20):
            filtered = controller._filtered(missing)

        self.assertIsNone(filtered["ir"])
        self.assertIsNone(controller.filters["ir"].value)
        self.assertEqual(controller._filtered(recovered)["ir"], 40.0)


class CsvLogTests(unittest.TestCase):
    def test_restart_appends_without_duplicate_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hp-fan-control.csv"

            first = CsvLog(path)
            first.write({})
            first.close()
            second = CsvLog(path)
            second.write({})
            second.close()

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines.count(",".join(CsvLog.FIELDS)), 1)
        self.assertEqual(len(lines), 3)

    def test_archives_existing_csv_with_incompatible_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hp-fan-control.csv"
            previous = path.with_name(f"{path.name}.previous")
            old_fields = tuple(
                field
                for field in CsvLog.FIELDS
                if field
                not in {
                    "amd_gpu_temperature_stale",
                    "nvidia_metrics_stale",
                }
            )
            old_contents = f"{','.join(old_fields)}\nlegacy-row\n"
            path.write_text(old_contents, encoding="utf-8")

            with self.assertLogs("hp-fan-control", level="WARNING") as captured:
                log = CsvLog(path)
            log.write({})
            log.close()

            current_lines = path.read_text(encoding="utf-8").splitlines()
            archived_contents = previous.read_text(encoding="utf-8")

        self.assertEqual(archived_contents, old_contents)
        self.assertEqual(current_lines[0], ",".join(CsvLog.FIELDS))
        self.assertEqual(len(current_lines), 2)
        self.assertTrue(
            any(
                "archived CSV with incompatible schema" in line
                for line in captured.output
            )
        )

    def test_rewrites_header_after_external_copytruncate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hp-fan-control.csv"
            log = CsvLog(path)
            log.write({})

            path.write_text("", encoding="utf-8")
            log.write({})
            log.close()
            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines[0], ",".join(CsvLog.FIELDS))
        self.assertEqual(len(lines), 2)

    def test_io_failure_is_deduplicated_and_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hp-fan-control.csv"
            log = CsvLog(path)
            with patch(
                "hp_fan_control.controller.os.fstat",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertLogs("hp-fan-control", level="WARNING") as captured:
                    log.write({})
                    log.write({})

            self.assertEqual(
                sum("CSV telemetry unavailable" in line for line in captured.output),
                1,
            )
            with self.assertLogs("hp-fan-control", level="INFO") as captured:
                log.write({})
            log.close()

        self.assertTrue(
            any("CSV telemetry recovered" in line for line in captured.output)
        )


class _ControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "platform_profile"
        self.profile.write_text("performance\n")


class ControllerTelemetryTests(_ControllerTestCase):
    def test_csv_fields_match_log_sample_row(self):
        csv_log = Mock(spec=CsvLog)
        settings = replace(Settings.load(CONFIG_PATH), include_acpi=True)
        controller = Controller(
            settings=settings,
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
            nvidia_metrics_stale=True,
            amd_gpu_temperature_stale=True,
        )
        filtered = {"cpu": 69.0, "gpu": 59.0, "ir": 54.0, "acpi": 49.0}
        # log_sample serializes existing targets; desired_pwm primes them for
        # this telemetry contract without applying a fan command.
        controller.policy.desired_pwm(
            filtered,
            {"cpu": 70.0, "gpu": 60.0, "ir": 55.0, "acpi": 50.0},
        )

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
        self.assertEqual(
            row["acpi_target_percent"],
            f"{hp_level_percent(23):.1f}",
        )
        self.assertEqual(row["nvidia_metrics_stale"], "true")
        self.assertEqual(row["amd_gpu_temperature_stale"], "true")

        csv_log.reset_mock()
        controller.log_sample(
            0.0,
            "performance",
            "manual",
            replace(
                snapshot,
                nvidia_power_draw_w=None,
                nvidia_power_limit_w=None,
                nvidia_metrics_stale=None,
                amd_gpu_temperature_stale=None,
            ),
            filtered,
            70.0,
            180,
            "contract check",
        )
        self.assertEqual(
            csv_log.write.call_args.args[0]["nvidia_metrics_stale"],
            "",
        )
        self.assertEqual(
            csv_log.write.call_args.args[0]["amd_gpu_temperature_stale"],
            "",
        )

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
        # Freeze time before next_status_log: an identical note is suppressed,
        # while a changed note must still be emitted immediately.
        with (
            patch("hp_fan_control.controller.LOG.info") as log_info,
            patch("hp_fan_control.controller.time.monotonic", return_value=1.0),
        ):
            controller.log_sample(
                0,
                "balanced",
                "handoff",
                snapshot,
                filtered,
                70,
                100,
                "cooling before firmware Auto",
            )
            controller.log_sample(
                0,
                "balanced",
                "handoff",
                snapshot,
                filtered,
                70,
                100,
                "cooling before firmware Auto",
            )
            controller.log_sample(
                0,
                "balanced",
                "handoff",
                snapshot,
                filtered,
                70,
                100,
                "new handoff detail",
            )
        self.assertEqual(log_info.call_count, 2)

    def test_controller_holds_pwm_for_stale_amd_sample_without_nvidia(self):
        sensors, _, gpu = initialized_sensors_with_amd_gpu(self)
        clock = FakeClock()
        sample_interval_s = sensors.settings.sample_interval_s

        def make_next_amd_read_fail(timeout_s):
            clock.wait(timeout_s)
            if clock.now == sample_interval_s:
                (gpu / "temp1_input").write_text("unreadable\n")

        root = self.root
        csv_path = root / "telemetry.csv"
        csv_log = CsvLog(csv_path)
        controller = Controller(
            settings=sensors.settings,
            fan=FakeFan(),
            sensors=sensors,
            apply=True,
            duration_s=2.0 * sample_interval_s,
            csv_log=csv_log,
            profile_path=self.profile,
            clock=clock,
            wait=make_next_amd_read_fail,
        )

        controller.run()
        csv_log.close()

        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["gpu_raw_c"] for row in rows], ["85.0", "85.0"])
        self.assertEqual(
            [row["amd_gpu_temperature_stale"] for row in rows],
            ["false", "true"],
        )
        self.assertEqual(rows[1]["requested_pwm"], rows[0]["requested_pwm"])
        self.assertEqual(rows[1]["gpu_ewma_c"], rows[0]["gpu_ewma_c"])
        self.assertEqual(rows[1]["nvidia_metrics_stale"], "")

    def test_persistent_sensor_failure_emits_periodic_status_and_csv(self):
        root = self.root
        csv_path = root / "telemetry.csv"
        settings = settings_with(
            Settings.load(CONFIG_PATH),
            sample_interval_s=1.0,
        )
        fan = FakeFan()
        csv_log = CsvLog(csv_path)
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=FailingAfterFirstSample(),
            apply=True,
            duration_s=120.0,
            csv_log=csv_log,
            profile_path=self.profile,
            status_interval_s=30.0,
        )
        with patch("hp_fan_control.controller.LOG.info") as info:
            controller.run()
        csv_log.close()

        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        failure_rows = [row for row in rows if row["state"] == "sensor-failure"]
        self.assertEqual(
            [row["elapsed_s"] for row in failure_rows],
            ["1.0", "31.0", "61.0", "91.0"],
        )
        self.assertTrue(all(row["cpu_raw_c"] == "" for row in failure_rows))
        self.assertTrue(all(row["nvidia_metrics_stale"] == "" for row in failure_rows))
        self.assertTrue(
            all(row["amd_gpu_temperature_stale"] == "" for row in failure_rows)
        )
        self.assertTrue(all(row["requested_pwm"] == "255" for row in failure_rows))
        failure_statuses = [
            logged
            for logged in info.call_args_list
            if logged.args
            and logged.args[0].startswith("state=%-14s")
            and logged.args[1] == "sensor-failure"
        ]
        self.assertEqual(len(failure_statuses), 4)

    def test_sensor_failure_in_bios_auto_reports_without_requesting_pwm(self):
        root = self.root
        csv_path = root / "telemetry.csv"
        sensors = Mock()
        sensors.read.side_effect = HardwareError("mandatory CPU source disappeared")
        fan = FakeFan()
        csv_log = CsvLog(csv_path)
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=fan,
            sensors=sensors,
            apply=True,
            duration_s=61.0,
            csv_log=csv_log,
            profile_path=self.profile,
            status_interval_s=30.0,
        )

        with patch("hp_fan_control.controller.LOG.error") as error:
            controller.run()
        csv_log.close()

        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertFalse(controller.emergency)
        self.assertFalse(controller.manual_active)
        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertEqual(fan.actions, [])
        self.assertEqual([row["elapsed_s"] for row in rows], ["0.0", "30.0", "60.0"])
        self.assertTrue(all(row["state"] == "sensor-failure" for row in rows))
        self.assertTrue(all(row["requested_pwm"] == "" for row in rows))
        self.assertTrue(all(row["requested_percent"] == "" for row in rows))
        error.assert_called_once_with(
            "sensor failure while BIOS Auto is active: %s",
            ANY,
        )

    def test_changing_sensor_error_text_does_not_bypass_status_interval(self):
        root = self.root
        csv_path = root / "telemetry.csv"
        attempts = 0

        def fail_with_changing_text():
            nonlocal attempts
            attempts += 1
            raise HardwareError(f"sensor read failed at attempt {attempts}")

        sensors = Mock()
        sensors.read.side_effect = fail_with_changing_text
        csv_log = CsvLog(csv_path)
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=FakeFan(),
            sensors=sensors,
            apply=True,
            duration_s=61.0,
            csv_log=csv_log,
            profile_path=self.profile,
            status_interval_s=30.0,
        )

        with patch("hp_fan_control.controller.LOG.error") as error:
            controller.run()
        csv_log.close()

        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual([row["elapsed_s"] for row in rows], ["0.0", "30.0", "60.0"])
        self.assertEqual(
            [row["note"] for row in rows],
            [
                "sensor read failed at attempt 1",
                "sensor read failed at attempt 31",
                "sensor read failed at attempt 61",
            ],
        )
        error.assert_called_once()


class ControlDecisionTests(unittest.TestCase):
    def setUp(self):
        self.policy = ControlPolicy(fixed_policy_settings())

    def test_policy_state_views_are_read_only(self):
        with self.assertRaises(AttributeError):
            self.policy.commanded_pwm = 120
        with self.assertRaises(TypeError):
            self.policy.sensor_targets["cpu"] = 50.0

    def test_uses_hottest_sensor(self):
        pwm, hottest = self.policy.desired_pwm({"cpu": 65.0, "gpu": 70.0, "acpi": 60.0})
        self.assertEqual(hottest, 70)
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(23), delta=0.3)
        self.assertEqual(self.policy.winning_sensor, "gpu")

    def test_limits_fan_speed_decrease(self):
        self.policy.set_commanded_pwm(percent_to_pwm(80))
        pwm, _ = self.policy.desired_pwm({"cpu": 60.0, "gpu": 50.0, "acpi": 50.0})
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

    def test_raw_temperature_is_retained_when_linear_curve_decreases(self):
        curve = Curve((50.0, 100.0), (30.0, 100.0))
        settings = replace(
            fixed_policy_settings(),
            curve=curve,
            curves=None,
            decrease_hysteresis_c=3.0,
        )
        policy = ControlPolicy(settings)
        policy.desired_pwm({"cpu": 90.0}, {"cpu": 90.0})

        policy.desired_pwm({"cpu": 60.0}, {"cpu": 80.0})

        self.assertAlmostEqual(
            policy.sensor_targets["cpu"],
            curve.evaluate_percent(83.0),
        )

    def test_linear_curve_hysteresis_never_raises_a_falling_target(self):
        curve = Curve((50.0, 100.0), (30.0, 100.0))
        settings = replace(
            fixed_policy_settings(),
            curve=curve,
            curves=None,
            decrease_hysteresis_c=3.0,
        )
        policy = ControlPolicy(settings)
        policy.desired_pwm({"cpu": 80.0}, {"cpu": 80.0})
        previous = policy.sensor_targets["cpu"]

        policy.desired_pwm({"cpu": 79.5}, {"cpu": 79.5})

        self.assertEqual(policy.sensor_targets["cpu"], previous)

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

    def test_auto_guard_write_failure_is_a_hardware_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            guard = Path(temporary) / "auto-guard"
            guard.mkdir()
            controller = Controller(
                fixed_policy_settings(),
                None,
                None,
                False,
                None,
                CsvLog(None),
                auto_guard_path=guard,
            )

            with self.assertRaisesRegex(
                HardwareError,
                "cannot persist firmware Auto guard",
            ):
                controller._start_auto_guard(10.0)

        self.assertEqual(controller.auto_guard_until, 190.0)

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
        # IR may delay release only after IR itself activated the Manual cycle.
        # Its first factory step above the Manual floor is 44 C, so IR at 43 C
        # is inactive while CPU/GPU at 44 C are below fan_stop_temp_c=45 C.
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=60.0, gpu=40.0, acpi=None, ir=43.0)
        )
        self.assertTrue(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(cpu=44.0, gpu=44.0, acpi=None, ir=43.0),
            )
        )

    def test_missing_activated_gpu_blocks_auto_release(self):
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=45.0, gpu=75.0, acpi=None)
        )
        missing = TemperatureSnapshot(cpu=45.0, gpu=None, acpi=None)

        for _ in range(OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES + 1):
            self.policy.observe_activations(missing)
            self.assertFalse(self.policy.cool_enough_for_auto(missing))

    def test_runtime_suspended_activated_gpu_allows_auto_release(self):
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=40.0, gpu=65.0, acpi=None)
        )
        suspended = TemperatureSnapshot(
            cpu=40.0,
            gpu=None,
            acpi=None,
            nvidia_runtime_suspended=True,
        )

        self.policy.observe_activations(suspended)

        self.assertTrue(self.policy.cool_enough_for_auto(suspended))

    def test_missing_activated_ir_stops_blocking_auto_after_bounded_outage(self):
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=40.0, gpu=40.0, acpi=None, ir=44.0)
        )
        missing = TemperatureSnapshot(
            cpu=40.0,
            gpu=40.0,
            acpi=None,
            ir=None,
        )

        with patch("hp_fan_control.controller.LOG.info") as info:
            for _ in range(OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES - 1):
                self.policy.observe_activations(missing)
                self.assertFalse(self.policy.cool_enough_for_auto(missing))
            self.policy.observe_activations(missing)

        self.assertTrue(self.policy.cool_enough_for_auto(missing))
        info.assert_called_once_with(
            "optional control sensor %s unavailable for %d consecutive "
            "samples; no longer blocking firmware Auto",
            "ir",
            OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES,
        )

    def test_available_ir_resets_missing_sample_streak(self):
        active = TemperatureSnapshot(
            cpu=40.0,
            gpu=40.0,
            acpi=None,
            ir=44.0,
        )
        missing = replace(active, ir=None)
        self.policy.observe_activations(active)
        self.policy.observe_activations(missing)
        self.policy.observe_activations(active)

        with patch("hp_fan_control.controller.LOG.info") as info:
            for _ in range(OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES - 1):
                self.policy.observe_activations(missing)
                self.assertFalse(self.policy.cool_enough_for_auto(missing))

        info.assert_not_called()

    def test_missing_never_activated_gpu_does_not_block_auto_release(self):
        self.policy.observe_activations(
            TemperatureSnapshot(cpu=60.0, gpu=None, acpi=None)
        )

        self.assertTrue(
            self.policy.cool_enough_for_auto(
                TemperatureSnapshot(cpu=45.0, gpu=None, acpi=None)
            )
        )

    def test_acpi_proxy_is_telemetry_only(self):
        snapshot = TemperatureSnapshot(cpu=44.0, gpu=44.0, acpi=95.0, ir=None)

        self.assertEqual(self.policy.activation_sources(snapshot), set())
        self.assertTrue(self.policy.cool_enough_for_auto(snapshot))
        self.assertEqual(snapshot.raw_control_hottest, 44.0)

        pwm, hottest = self.policy.desired_pwm(
            {"cpu": 44.0, "gpu": 44.0, "ir": None, "acpi": 95.0},
            {"cpu": 44.0, "gpu": 44.0, "ir": None, "acpi": 95.0},
        )
        self.assertEqual(hottest, 44.0)
        self.assertEqual(self.policy.winning_sensor, "cpu")
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(19), delta=0.3)
        self.assertAlmostEqual(self.policy.sensor_targets["acpi"], hp_level_percent(47))

    def test_acpi_only_input_raises_hardware_error(self):
        with self.assertRaisesRegex(
            HardwareError, "no valid temperature is available for fan control"
        ):
            self.policy.desired_pwm(
                {"cpu": None, "gpu": None, "ir": None, "acpi": 45.0}
            )

    def test_raw_fan_stop_threshold_controls_auto_handoff(self):
        self.assertFalse(
            self.policy.cool_enough_for_auto(TemperatureSnapshot(46.0, 44.0, None))
        )
        self.assertTrue(
            self.policy.cool_enough_for_auto(TemperatureSnapshot(45.0, 45.0, None))
        )


class SettingsTests(unittest.TestCase):
    def test_default_config_is_independent_of_source_tree_layout(self):
        self.assertEqual(
            parse_args([]).config,
            Path("/etc/hp-fan-control/fan-control.toml"),
        )

    def test_missing_manual_minimum_uses_factory_level_19(self):
        source = CONFIG_PATH.read_text(encoding="utf-8")
        configured_minimum = "minimum_manual_percent = 31.6667\n"
        self.assertIn(configured_minimum, source)
        source = source.replace(configured_minimum, "")
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "fan-control.toml"
            config.write_text(source, encoding="utf-8")
            settings = Settings.load(config)

        self.assertAlmostEqual(
            settings.minimum_manual_percent,
            hp_level_percent(19),
        )

    def test_rejects_conflicting_operation_and_log_arguments(self):
        cases = (
            ["--failsafe", "--actuator-test", "50"],
            ["--restore-auto", "--actuator-test", "50"],
            ["--log-file", "telemetry.csv", "--no-log-file"],
        )
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                patch("sys.stderr"),
                self.assertRaises(SystemExit) as caught,
            ):
                parse_args(arguments)
            self.assertEqual(caught.exception.code, 2)

    def test_selects_csv_log_path(self):
        with patch("hp_fan_control.cli.Path.cwd", return_value=Path("/logs")):
            self.assertEqual(
                _csv_log_path(parse_args([])),
                Path("/logs/hp-fan-control.csv"),
            )
        self.assertEqual(
            _csv_log_path(parse_args(["--log-file", "/tmp/custom.csv"])),
            Path("/tmp/custom.csv"),
        )
        self.assertIsNone(_csv_log_path(parse_args(["--no-log-file"])))

    def test_loads_factory_preset(self):
        config = CONFIG_PATH
        settings = Settings.load(config)
        self.assertEqual(settings.curve_source, "hp-vibrance-stx-n22x9-performance")
        self.assertEqual(
            set(dict(settings.curves or ())),
            {"cpu", "gpu", "ir"},
        )
        self.assertAlmostEqual(
            settings.curve_for("gpu").pwm_percent[-1], hp_level_percent(47)
        )

    def test_rejects_unknown_configuration_keys(self):
        cases = {
            "top-level": (
                "mystery",
                """
[daemon]
allowed_boards = ["8D87"]
[curves]
preset = "hp-vibrance-stx-n22x9-performance"
[mystery]
enabled = true
""",
            ),
            "daemon": (
                "daemon.activaton_temp_c",
                """
[daemon]
allowed_boards = ["8D87"]
activaton_temp_c = 60.0
[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
            ),
            "ewma": (
                "ewma.raise_alpha",
                """
[daemon]
allowed_boards = ["8D87"]
[ewma]
raise_alpha = 0.25
[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
            ),
            "sensors": (
                "sensors.include_nvida_gpu",
                """
[daemon]
allowed_boards = ["8D87"]
[sensors]
include_nvida_gpu = true
[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
            ),
            "legacy curve": (
                "curve.steped",
                """
[daemon]
allowed_boards = ["8D87"]
[curve]
temperature_c = [50, 60]
pwm_percent = [30, 40]
steped = true
""",
            ),
            "curves": (
                "curves.presett",
                """
[daemon]
allowed_boards = ["8D87"]
[curves]
presett = "hp-vibrance-stx-n22x9-performance"
""",
            ),
            "named curve": (
                "curves.cpu.steped",
                """
[daemon]
allowed_boards = ["8D87"]
[curves.cpu]
temperature_c = [50, 60]
pwm_percent = [30, 40]
steped = true
""",
            ),
        }

        for name, (unknown_key, contents) in cases.items():
            with self.subTest(section=name), tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary) / "fan-control.toml"
                config.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(
                    ConfigurationError,
                    f"unknown configuration key: {unknown_key}",
                ):
                    Settings.load(config)

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

    def test_allowed_boards_must_be_a_list(self):
        cases = {
            "bare string": 'allowed_boards = "8C99"',
            "integer": "allowed_boards = 8887",
            "non-string entries": "allowed_boards = [8887]",
            "nested list": 'allowed_boards = [["8C99"]]',
        }
        for name, line in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as temporary:
                    config = Path(temporary) / "fan-control.toml"
                    config.write_text(
                        f"""
[daemon]
{line}

[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        ConfigurationError,
                        "allowed_boards must be a list of board names",
                    ):
                        Settings.load(config)

    def test_allowed_boards_entries_are_trimmed(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "fan-control.toml"
            config.write_text(
                """
[daemon]
allowed_boards = [" 8D87 ", "", "8C99"]

[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
                encoding="utf-8",
            )

            self.assertEqual(Settings.load(config).allowed_boards, ("8D87", "8C99"))

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
                HardwareError,
                "required_profile 'performnce' is unavailable",
            ):
                validate_required_profile("performnce", choices, timeout_s=0)

    def test_waits_for_required_profile_during_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            choices = Path(temporary) / "platform_profile_choices"

            def publish_profile(_delay):
                choices.write_text("low-power balanced performance\n")

            with (
                patch(
                    "hp_fan_control.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "hp_fan_control.hardware.time.sleep",
                    side_effect=publish_profile,
                ) as sleep,
                patch("hp_fan_control.hardware.LOG.warning") as warning,
                patch("hp_fan_control.hardware.LOG.info") as info,
            ):
                validate_required_profile("performance", choices)

        sleep.assert_called_once_with(1.0)
        warning.assert_called_once()
        info.assert_called_once_with("required platform profile became available")

    def test_rejects_unsafe_scalar_settings(self):
        cases = (
            (
                "decrease_hysteresis_c",
                -50.0,
                "decrease_hysteresis_c must be between 0 and 20",
            ),
            (
                "decrease_hysteresis_c",
                100.0,
                "decrease_hysteresis_c must be between 0 and 20",
            ),
            (
                "emergency_hold_s",
                -1.0,
                "emergency_hold_s must be non-negative",
            ),
            (
                "critical_temp_c",
                500.0,
                r"critical_temp_c must be in \(0, 125]",
            ),
            (
                "activation_temp_c",
                0.0,
                r"activation_temp_c must be in \(0, 125]",
            ),
            (
                "critical_release_temp_c",
                130.0,
                r"critical_release_temp_c must be in \(0, 125]",
            ),
            (
                "activation_temp_c",
                float("nan"),
                "activation_temp_c must be finite",
            ),
            (
                "control_interval_s",
                float("inf"),
                "control_interval_s must be finite",
            ),
            (
                "allowed_boards",
                (),
                "allowed_boards must not be empty",
            ),
            (
                "sample_interval_s",
                0.1,
                "sample_interval_s must be at least 0.25",
            ),
            (
                "control_interval_s",
                0.5,
                "control_interval_s must be >= sample_interval_s",
            ),
            (
                "release_temp_c",
                60.0,
                "release_temp_c must be below activation_temp_c",
            ),
            (
                "fan_stop_temp_c",
                60.0,
                "fan_stop_temp_c must be below activation_temp_c",
            ),
            (
                "critical_release_temp_c",
                92.0,
                "critical_release_temp_c must be below critical_temp_c",
            ),
            (
                "activation_temp_c",
                92.0,
                "activation_temp_c must be below critical_temp_c",
            ),
            (
                "ir_release_hysteresis_c",
                0.0,
                "ir_release_hysteresis_c must be positive",
            ),
            (
                "auto_guard_s",
                119.0,
                "auto_guard_s must be at least 120 seconds",
            ),
            (
                "ewma_rise_alpha",
                0.0,
                r"ewma.rise_alpha must be in \(0, 1]",
            ),
            (
                "minimum_manual_percent",
                0.0,
                r"minimum_manual_percent must be in \(0, 100]",
            ),
            (
                "max_fall_percent_per_update",
                101.0,
                r"max_fall_percent_per_update must be in \(0, 100]",
            ),
        )

        for field, value, message in cases:
            with self.subTest(field=field):
                settings = replace(fixed_policy_settings(), **{field: value})
                with self.assertRaisesRegex(ConfigurationError, message):
                    settings.validate()


class SourceHealthTests(unittest.TestCase):
    def test_fail_fast_cause_uses_existing_debounced_failure_streak(self):
        health = SourceHealth(
            "AMD GPU temperature source",
            FailurePolicy.REQUIRED_AFTER_AVAILABLE,
        )
        health.available()

        health.unavailable("temperature unreadable", failure_threshold=3)
        self.assertEqual(health.consecutive_failures, 1)

        with self.assertRaisesRegex(
            HardwareError,
            "AMD GPU temperature source unavailable: device disappeared",
        ):
            health.unavailable("device disappeared", failure_threshold=1)

        self.assertEqual(health.consecutive_failures, 2)


class SensorMetricTests(unittest.TestCase):
    def test_rediscovers_cpu_after_hwmon_index_changes(self):
        sensors = initialized_sensors(self)
        self.assertEqual(sensors.read().cpu, 50.0)
        empty_cpu = sensors.hwmon_root / "hwmon1"
        empty_cpu.mkdir()
        (empty_cpu / "name").write_text("k10temp\n")
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

    def test_cpu_loss_remains_hardware_error_if_health_policy_is_relaxed(self):
        sensors = initialized_sensors(self)
        sensors.cpu_health.policy = FailurePolicy.OPTIONAL
        sensors.cpu_hwmon.rename(sensors.hwmon_root / "offline-k10temp")

        with self.assertRaisesRegex(
            HardwareError,
            "CPU temperature source unavailable",
        ):
            sensors.read()

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
            self.assertIsNone(sensors.last_amd_gpu_temperature)
            self.assertIsNone(sensors.amd_gpu_temperature_stale)
            with self.assertRaises(HardwareError):
                sensors.read()

            recovered = root / "hwmon14"
            offline.rename(recovered)
            self.assertEqual(sensors.read().gpu, 85.0)

        warning.assert_called_once_with(
            "%s unavailable: %s",
            "AMD GPU temperature source",
            "no amdgpu hwmon device was found during rediscovery",
        )
        info.assert_called_once_with("%s recovered", "AMD GPU temperature source")

    def test_single_unreadable_amd_gpu_sample_does_not_fail_safe(self):
        sensors, _, _ = initialized_sensors_with_amd_gpu(self)
        self.assertEqual(sensors.read().gpu, 85.0)

        with (
            patch.object(sensors, "_cpu_temperature", return_value=50.0),
            patch(
                "hp_fan_control.hardware.read_hwmon_temperatures",
                return_value=[],
            ),
        ):
            stale = sensors.read()

        self.assertEqual(stale.gpu, 85.0)
        self.assertTrue(stale.amd_gpu_temperature_stale)
        self.assertEqual(sensors.amd_gpu_health.consecutive_failures, 1)
        self.assertEqual(sensors._amd_gpu_temperature(), 85.0)
        self.assertEqual(sensors.amd_gpu_health.consecutive_failures, 0)
        self.assertFalse(sensors.amd_gpu_temperature_stale)

    def test_three_unreadable_amd_gpu_samples_fail_safe(self):
        sensors, _, _ = initialized_sensors_with_amd_gpu(self)
        self.assertEqual(sensors._amd_gpu_temperature(), 85.0)

        with patch(
            "hp_fan_control.hardware.read_hwmon_temperatures",
            return_value=[],
        ):
            self.assertEqual(sensors._amd_gpu_temperature(), 85.0)
            self.assertEqual(sensors._amd_gpu_temperature(), 85.0)
            with self.assertRaisesRegex(
                HardwareError,
                "AMD GPU temperature source unavailable",
            ):
                sensors._amd_gpu_temperature()

    def test_unreadable_amd_gpu_then_disappearance_fails_safe(self):
        sensors, root, gpu = initialized_sensors_with_amd_gpu(self)
        self.assertEqual(sensors.read().gpu, 85.0)

        with (
            patch.object(sensors, "_cpu_temperature", return_value=50.0),
            patch(
                "hp_fan_control.hardware.read_hwmon_temperatures",
                return_value=[],
            ),
        ):
            stale = sensors.read()

        gpu.rename(root / "offline-amdgpu")
        with self.assertRaisesRegex(
            HardwareError,
            "no amdgpu hwmon device was found during rediscovery",
        ):
            sensors.read()

        self.assertEqual(stale.gpu, 85.0)
        self.assertTrue(stale.amd_gpu_temperature_stale)
        self.assertEqual(sensors.amd_gpu_health.consecutive_failures, 2)

    def test_amd_failure_preserves_ewma_and_target_pwm_without_nvidia(self):
        sensors, _, gpu = initialized_sensors_with_amd_gpu(self)
        controller = Controller(
            settings=sensors.settings,
            fan=FakeFan(),
            sensors=sensors,
            apply=False,
            duration_s=None,
            csv_log=CsvLog(None),
        )
        hot = sensors.read()
        hot_filtered = controller._filtered(hot)
        hot_pwm, _ = controller.policy.desired_pwm(
            hot_filtered,
            {**hot.control_temperatures(), "acpi": hot.acpi},
        )

        with (
            patch.object(sensors, "_cpu_temperature", return_value=50.0),
            patch(
                "hp_fan_control.hardware.read_hwmon_temperatures",
                return_value=[],
            ),
        ):
            stale = sensors.read()
        stale_filtered = controller._filtered(stale)
        stale_pwm, _ = controller.policy.desired_pwm(
            stale_filtered,
            {**stale.control_temperatures(), "acpi": stale.acpi},
        )

        (gpu / "temp1_input").write_text("40000\n")
        recovered = controller._filtered(sensors.read())
        expected = (
            85.0 * (1.0 - sensors.settings.ewma_fall_alpha)
            + 40.0 * sensors.settings.ewma_fall_alpha
        )

        self.assertTrue(stale.amd_gpu_temperature_stale)
        self.assertIsNone(stale.nvidia_metrics_stale)
        self.assertEqual(stale_filtered["gpu"], 85.0)
        self.assertEqual(stale_pwm, hot_pwm)
        self.assertAlmostEqual(recovered["gpu"], expected)
        self.assertGreater(recovered["gpu"], 40.0)

    def test_unreadable_amd_gpu_keeps_current_nvidia_sample(self):
        sensors, _, _ = initialized_sensors_with_amd_gpu(self)
        self.assertEqual(sensors._amd_gpu_temperature(), 85.0)
        sensors.settings = settings_with(
            sensors.settings,
            include_nvidia_gpu=True,
        )
        sensors.nvidia_smi = "/usr/bin/nvidia-smi"
        nvidia = SimpleNamespace(
            returncode=0,
            stdout="61, 80.0, 120.0\n",
            stderr="",
        )

        with (
            patch.object(sensors, "_cpu_temperature", return_value=50.0),
            patch(
                "hp_fan_control.hardware.subprocess.run",
                return_value=nvidia,
            ),
            patch(
                "hp_fan_control.hardware.read_hwmon_temperatures",
                return_value=[],
            ),
        ):
            snapshot = sensors.read()

        self.assertEqual(snapshot.gpu, 85.0)
        self.assertTrue(snapshot.amd_gpu_temperature_stale)
        self.assertEqual(snapshot.nvidia_power_draw_w, 80.0)
        self.assertFalse(snapshot.nvidia_metrics_stale)

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
        info.assert_called_once_with("%s recovered", "NVIDIA GPU temperature source")

    def test_suspended_nvidia_gpu_skips_query_and_clears_cached_metrics(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        sensors.last_nvidia_metrics = (61.0, 80.0, 120.0)
        sensors.nvidia_gpu_health.available()
        self.assertEqual(len(sensors.nvidia_runtime_status_files), 1)
        sensors.nvidia_runtime_status_files[0].write_text("suspended\n")

        with patch("hp_fan_control.hardware.subprocess.run") as run:
            snapshot = sensors.read()

        run.assert_not_called()
        self.assertIsNone(snapshot.gpu)
        self.assertIsNone(snapshot.nvidia_power_draw_w)
        self.assertIsNone(snapshot.nvidia_power_limit_w)
        self.assertIsNone(snapshot.nvidia_metrics_stale)
        self.assertTrue(snapshot.nvidia_runtime_suspended)
        self.assertEqual(sensors.last_nvidia_metrics, (None, None, None))
        self.assertFalse(sensors.nvidia_gpu_health.failed)
        self.assertEqual(sensors.nvidia_gpu_health.consecutive_failures, 0)

    def test_nvidia_failure_after_runtime_suspend_does_not_reuse_old_metrics(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        runtime_status = sensors.nvidia_runtime_status_files[0]
        fresh_result = SimpleNamespace(
            returncode=0,
            stdout="65, 80.0, 120.0\n",
            stderr="",
        )

        with patch(
            "hp_fan_control.hardware.subprocess.run",
            side_effect=[
                fresh_result,
                subprocess.TimeoutExpired("nvidia-smi", 2.0),
            ],
        ):
            fresh = sensors.read()
            runtime_status.write_text("suspended\n")
            suspended = sensors.read()
            runtime_status.write_text("active\n")
            failed_after_wake = sensors.read()

        self.assertEqual(fresh.gpu, 65.0)
        self.assertIsNone(suspended.gpu)
        self.assertIsNone(failed_after_wake.gpu)
        self.assertIsNone(failed_after_wake.nvidia_power_draw_w)
        self.assertIsNone(failed_after_wake.nvidia_power_limit_w)
        self.assertIsNone(failed_after_wake.nvidia_metrics_stale)

    def test_nvidia_query_resumes_when_runtime_status_becomes_active(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        runtime_status = sensors.nvidia_runtime_status_files[0]
        runtime_status.write_text("suspended\n")
        result = SimpleNamespace(
            returncode=0,
            stdout="61, 80.0, 120.0\n",
            stderr="",
        )

        with patch(
            "hp_fan_control.hardware.subprocess.run",
            return_value=result,
        ) as run:
            suspended = sensors.read()
            runtime_status.write_text("active\n")
            active = sensors.read()

        self.assertIsNone(suspended.gpu)
        self.assertEqual(active.gpu, 61.0)
        self.assertFalse(active.nvidia_metrics_stale)
        self.assertFalse(active.nvidia_runtime_suspended)
        run.assert_called_once()

    def test_periodically_discovers_late_nvidia_pci_device(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        runtime_status = sensors.nvidia_runtime_status_files[0]
        runtime_status.write_text("suspended\n")
        sensors.nvidia_runtime_status_files = ()
        sensors.next_nvidia_pci_discovery = 130.0

        with (
            patch("hp_fan_control.hardware.time.monotonic", return_value=130.0),
            patch("hp_fan_control.hardware.subprocess.run") as run,
        ):
            snapshot = sensors.read()

        run.assert_not_called()
        self.assertTrue(snapshot.nvidia_runtime_suspended)
        self.assertEqual(
            sensors.nvidia_runtime_status_files,
            (runtime_status,),
        )
        self.assertEqual(sensors.next_nvidia_pci_discovery, 160.0)

    def test_stale_nvidia_pci_path_is_rediscovered_immediately(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        old_status = sensors.nvidia_runtime_status_files[0]
        new_device = sensors.pci_root / "0000:c4:00.0"
        old_status.parents[1].rename(new_device)
        new_status = new_device / "power" / "runtime_status"
        new_status.write_text("suspended\n")
        sensors.next_nvidia_pci_discovery = 200.0

        with (
            patch("hp_fan_control.hardware.time.monotonic", return_value=100.0),
            patch("hp_fan_control.hardware.subprocess.run") as run,
        ):
            snapshot = sensors.read()

        run.assert_not_called()
        self.assertTrue(snapshot.nvidia_runtime_suspended)
        self.assertEqual(
            sensors.nvidia_runtime_status_files,
            (new_status,),
        )
        self.assertEqual(sensors.next_nvidia_pci_discovery, 130.0)

    def test_unreadable_nvidia_runtime_status_falls_back_to_query(self):
        with patch(
            "hp_fan_control.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        sensors.nvidia_runtime_status_files[0].unlink()
        result = SimpleNamespace(
            returncode=0,
            stdout="61, 80.0, 120.0\n",
            stderr="",
        )

        with patch(
            "hp_fan_control.hardware.subprocess.run",
            return_value=result,
        ) as run:
            snapshot = sensors.read()

        self.assertEqual(snapshot.gpu, 61.0)
        self.assertFalse(snapshot.nvidia_metrics_stale)
        self.assertIsNone(snapshot.nvidia_runtime_suspended)
        run.assert_called_once()

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
                "status 9: Failed to initialize NVML: Driver/library version mismatch",
            ):
                sensors.read()

    def test_nvidia_os_error_retains_last_metrics_and_forgets_executable(self):
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
        with (
            patch(
                "hp_fan_control.hardware.subprocess.run",
                side_effect=[available, OSError("driver disappeared")],
            ),
            patch("hp_fan_control.hardware.time.monotonic", return_value=100.0),
        ):
            fresh = sensors.read()
            stale = sensors.read()

        self.assertEqual(fresh.gpu, 61.0)
        self.assertFalse(fresh.nvidia_metrics_stale)
        self.assertEqual(stale.gpu, 61.0)
        self.assertTrue(stale.nvidia_metrics_stale)
        self.assertIsNone(sensors.nvidia_smi)
        self.assertEqual(sensors.next_nvidia_discovery, 130.0)

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
            first_missing = sensors.read()
            second_missing = sensors.read()
            self.assertIsNone(first_missing.gpu)
            self.assertIsNone(first_missing.nvidia_metrics_stale)
            self.assertIsNone(second_missing.gpu)
            self.assertIsNone(second_missing.nvidia_metrics_stale)
            self.assertEqual(which.call_count, 1)
            recovered = sensors.read()
            self.assertEqual(recovered.gpu, 61.0)
            self.assertFalse(recovered.nvidia_metrics_stale)

        self.assertEqual(which.call_count, 2)

    def test_disabled_nvidia_source_has_no_staleness_status(self):
        with patch(
            "hp_fan_control.hardware.find_nvidia_runtime_status_files"
        ) as find_runtime_status:
            sensors = initialized_sensors(self, include_nvidia_gpu=False)

        snapshot = sensors.read()

        find_runtime_status.assert_not_called()
        self.assertIsNone(snapshot.nvidia_power_draw_w)
        self.assertIsNone(snapshot.nvidia_power_limit_w)
        self.assertIsNone(snapshot.nvidia_metrics_stale)

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

    def test_reads_acpi_temperature_from_injected_thermal_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hwmon_root = root / "hwmon"
            thermal_root = root / "thermal"
            cpu = hwmon_root / "hwmon0"
            cpu.mkdir(parents=True)
            (cpu / "name").write_text("k10temp\n")
            (cpu / "temp1_input").write_text("50000\n")
            acpi = thermal_root / "thermal_zone0"
            acpi.mkdir(parents=True)
            (acpi / "type").write_text("acpitz\n")
            (acpi / "temp").write_text("55000\n")
            ignored = thermal_root / "thermal_zone1"
            ignored.mkdir()
            (ignored / "type").write_text("x86_pkg_temp\n")
            (ignored / "temp").write_text("99000\n")
            settings = settings_with(
                include_acpi=True,
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                include_hp_wmi_ir=False,
            )
            sensors = Sensors(settings, hwmon_root, thermal_root)

            snapshot = sensors.read()

        self.assertEqual(snapshot.acpi, 55.0)

    def test_missing_acpi_temperature_is_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hwmon_root = root / "hwmon"
            thermal_root = root / "thermal"
            cpu = hwmon_root / "hwmon0"
            cpu.mkdir(parents=True)
            thermal_root.mkdir()
            (cpu / "name").write_text("k10temp\n")
            (cpu / "temp1_input").write_text("50000\n")
            settings = settings_with(
                include_acpi=True,
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                include_hp_wmi_ir=False,
            )
            sensors = Sensors(settings, hwmon_root, thermal_root)

            with patch("hp_fan_control.hardware.LOG.warning") as warning:
                snapshot = sensors.read()

        self.assertIsNone(snapshot.acpi)
        self.assertTrue(sensors.acpi_health.failed)
        warning.assert_called_once()


class FakeFan:
    """Controller state stub; HpFanHwmon tests own hardware-mode semantics."""

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


class FakeFanContractTests(unittest.TestCase):
    def test_controller_stub_matches_hp_fan_public_method_signatures(self):
        for name in (
            "status",
            "set_manual",
            "update_manual",
            "set_maximum",
            "restore_auto",
        ):
            with self.subTest(method=name):
                real = inspect.signature(getattr(HpFanHwmon, name))
                fake = inspect.signature(getattr(FakeFan, name))
                real_parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in real.parameters.values()
                )
                fake_parameters = tuple(
                    (parameter.name, parameter.kind, parameter.default)
                    for parameter in fake.parameters.values()
                )
                self.assertEqual(fake_parameters, real_parameters)


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


class ControllerLoopTests(_ControllerTestCase):
    def test_ir_manual_floor_bucket_stays_in_firmware_auto(self):
        settings = Settings.load(CONFIG_PATH)
        sensors = Mock()
        # The first factory IR step above the Manual floor is 44 C, so 43 C is
        # inactive while CPU/GPU at 44 C are below fan_stop_temp_c=45 C.
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
            profile_path=self.profile,
        )

        controller.run()

        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertEqual(fan.actions, [])

    def test_acpi_proxy_above_critical_cannot_leave_firmware_auto(self):
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
            profile_path=self.profile,
        )

        controller.run()

        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertFalse(controller.emergency)
        self.assertEqual(fan.actions, [])

    def test_raw_cpu_or_gpu_at_critical_threshold_selects_maximum(self):
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
                    profile_path=self.profile,
                )

                controller.run()

                self.assertTrue(controller.emergency)
                self.assertEqual(fan.actions[0], ("maximum", 255))
                self.assertNotIn("manual", [action for action, _ in fan.actions])
                self.assertEqual(fan.mode, MAX_MODE)

    def test_non_performance_profile_sleeps_without_reading_sensors(self):
        self.profile.write_text("balanced\n")
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
            profile_path=self.profile,
            notifier=notifier,
            inactive_event_wait_s=1.0,
        )
        controller.run()
        sensors.read.assert_not_called()
        notifier.ready.assert_called_once_with()
        self.assertGreaterEqual(notifier.watchdog.call_count, 1)

    def test_injected_wait_refreshes_profile_during_run(self):
        self.profile.write_text("balanced\n")
        clock = FakeClock()
        sensors = Mock()
        sensors.read.return_value = TemperatureSnapshot(50, 50, None, None)

        def switch_to_performance(timeout_s):
            self.profile.write_text("performance\n")
            clock.wait(timeout_s)

        controller = Controller(
            settings=Settings.load(CONFIG_PATH),
            fan=FakeFan(),
            sensors=sensors,
            apply=True,
            duration_s=2.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
            inactive_event_wait_s=1.0,
            clock=clock,
            wait=switch_to_performance,
        )

        controller.run()

        sensors.read.assert_called_once_with()

    def test_new_heat_during_auto_guard_reclaims_manual_control(self):
        self.profile.write_text("balanced\n")
        settings = Settings.load(CONFIG_PATH)
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=FakeSensors(70),
            apply=True,
            duration_s=2.5,
            csv_log=CsvLog(None),
            profile_path=self.profile,
            inactive_event_wait_s=1.0,
        )
        controller.auto_guard_until = float("inf")
        controller.run()
        self.assertEqual(fan.actions[0][0], "manual")
        self.assertNotIn(("auto", None), fan.actions)

    def test_new_heat_after_auto_handoff_starts_a_new_manual_cycle(self):
        self.profile.write_text("balanced\n")
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
            profile_path=self.profile,
            inactive_event_wait_s=1.0,
        )
        controller.run()
        auto_index = fan.actions.index(("auto", None))
        later_actions = fan.actions[auto_index + 1 :]
        self.assertTrue(any(action == "manual" for action, _ in later_actions))

    def test_cpu_sensor_loss_during_auto_guard_selects_maximum(self):
        self.profile.write_text("balanced\n")
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
            profile_path=self.profile,
            inactive_event_wait_s=1.0,
        )
        controller.auto_guard_until = float("inf")
        controller.run()
        self.assertIn(("maximum", 255), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)

    def test_leaving_performance_keeps_hot_manual_control(self):
        self.profile.write_text("balanced\n")
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
            profile_path=self.profile,
            inactive_event_wait_s=1.0,
        )
        controller.run()
        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.actions[-1], ("maximum", 255))
        self.assertEqual(fan.mode, MAX_MODE)

    def test_missing_hot_gpu_sample_does_not_restore_bios_auto(self):
        fan = FakeFan()
        sensors = SequenceSensors(
            [
                TemperatureSnapshot(45.0, 75.0, None),
                TemperatureSnapshot(45.0, None, None),
                TemperatureSnapshot(45.0, 75.0, None),
            ]
        )
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=fan,
            sensors=sensors,
            apply=True,
            duration_s=3.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        controller.run()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.actions[-1], ("maximum", 255))
        self.assertEqual(fan.mode, MAX_MODE)

    def test_runtime_suspended_gpu_releases_manual_to_bios_auto(self):
        settings = Settings.load(CONFIG_PATH)
        hot = TemperatureSnapshot(cpu=40.0, gpu=65.0, acpi=None)
        suspended = TemperatureSnapshot(
            cpu=40.0,
            gpu=None,
            acpi=None,
            nvidia_runtime_suspended=True,
        )
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=SequenceSensors([hot, suspended]),
            apply=True,
            duration_s=2.0 * settings.sample_interval_s,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        controller.run()

        self.assertIn(("auto", None), fan.actions)
        self.assertFalse(controller.manual_active)

    def test_missing_optional_ir_eventually_allows_bios_auto(self):
        settings = Settings.load(CONFIG_PATH)
        hot_ir = TemperatureSnapshot(40.0, 40.0, None, 44.0)
        missing_ir = TemperatureSnapshot(40.0, 40.0, None, None)
        missing_sample_count = OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES + 5
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=SequenceSensors([hot_ir] + [missing_ir] * missing_sample_count),
            apply=True,
            duration_s=((missing_sample_count + 1) * settings.sample_interval_s),
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        with patch("hp_fan_control.controller.LOG.info") as info:
            controller.run()

        self.assertIn(("auto", None), fan.actions)
        self.assertEqual(fan.actions.count(("auto", None)), 1)
        self.assertFalse(controller.manual_active)
        info.assert_any_call(
            "optional control sensor %s unavailable for %d consecutive "
            "samples; no longer blocking firmware Auto",
            "ir",
            OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES,
        )

    def test_cool_handoff_monitors_auto_before_sleeping(self):
        self.profile.write_text("balanced\n")
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
            profile_path=self.profile,
            status_interval_s=1000.0,
            inactive_event_wait_s=1.0,
        )
        controller.run()
        self.assertIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_emergency_start_at_zero_is_not_replaced_on_next_sample(self):
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=FakeFan(),
            sensors=FakeSensors(92.0),
            apply=True,
            duration_s=2.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        controller.run()

        self.assertEqual(controller.emergency_since, 0.0)

    def test_new_emergency_gets_a_fresh_hold_period_after_recovery(self):
        settings = settings_with(
            emergency_hold_s=2.0,
            ewma_fall_alpha=1.0,
        )
        hot = TemperatureSnapshot(92.0, 50.0, None)
        cool = TemperatureSnapshot(35.0, 35.0, None)
        controller = controller_with_fake_time(
            settings=settings,
            fan=FakeFan(),
            sensors=SequenceSensors([hot, cool, cool, hot, cool]),
            apply=True,
            duration_s=5.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        controller.run()

        self.assertTrue(controller.emergency)
        self.assertEqual(controller.emergency_since, 3.0)

    def test_systemd_watchdog_tracks_controller_progress_and_stop(self):
        settings = Settings.load(CONFIG_PATH)
        notifier = Mock(spec=SystemdNotifier)
        controller = controller_with_fake_time(
            settings=settings,
            fan=FakeFan(),
            sensors=FakeSensors(50),
            apply=True,
            duration_s=2.5,
            csv_log=CsvLog(None),
            profile_path=self.profile,
            notifier=notifier,
        )
        controller.run()
        notifier.ready.assert_called_once_with()
        self.assertGreaterEqual(notifier.watchdog.call_count, 1)
        notifier.stopping.assert_called_once_with()

    def test_long_sample_wait_keeps_systemd_watchdog_alive(self):
        settings = settings_with(
            Settings.load(CONFIG_PATH),
            sample_interval_s=30.0,
            control_interval_s=30.0,
        )
        notifier = Mock(spec=SystemdNotifier)
        notifier.watchdog_interval_s = 7.5
        controller = controller_with_fake_time(
            settings=settings,
            fan=FakeFan(),
            sensors=FakeSensors(50),
            apply=True,
            duration_s=30.5,
            csv_log=CsvLog(None),
            profile_path=self.profile,
            notifier=notifier,
        )

        controller.run()

        self.assertGreaterEqual(notifier.watchdog.call_count, 5)
        notifier.stopping.assert_called_once_with()

    def test_stop_request_ends_long_sample_wait_after_one_quantum(self):
        settings = settings_with(
            Settings.load(CONFIG_PATH),
            sample_interval_s=30.0,
            control_interval_s=30.0,
        )
        notifier = Mock(spec=SystemdNotifier)
        notifier.watchdog_interval_s = 7.5
        clock = FakeClock()
        waits = []

        def request_stop_during_wait(timeout_s):
            waits.append(timeout_s)
            clock.wait(timeout_s)
            controller.request_stop(15, None)

        controller = Controller(
            settings=settings,
            fan=FakeFan(),
            sensors=FakeSensors(50),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
            profile_path=self.profile,
            notifier=notifier,
            clock=clock,
            wait=request_stop_during_wait,
        )

        controller.run()

        self.assertEqual(waits, [7.5])
        notifier.stopping.assert_called_once_with()

    def test_mandatory_sensor_loss_and_exit_preserve_maximum(self):
        settings = Settings.load(CONFIG_PATH)
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=FailingAfterFirstSample(),
            apply=True,
            duration_s=3.5,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )
        with patch("hp_fan_control.controller.LOG.error") as error:
            controller.run()
        self.assertEqual(fan.actions[0][0], "manual")
        self.assertIn(("maximum", 255), fan.actions)
        self.assertEqual(fan.actions[-1][0], "maximum")
        self.assertEqual(fan.mode, MAX_MODE)
        error.assert_called_once_with(
            "sensor failure during control; selecting maximum: %s",
            ANY,
        )

    def test_sensor_failure_resets_ewma_before_recovery(self):
        sensors = Mock()
        sensors.read.side_effect = [
            TemperatureSnapshot(75.0, 75.0, None, None),
            HardwareError("mandatory GPU source disappeared"),
            TemperatureSnapshot(35.0, 35.0, None, None),
        ]
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=FakeFan(),
            sensors=sensors,
            apply=True,
            duration_s=3.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        controller.run()

        self.assertEqual(controller.filters["cpu"].value, 35.0)
        self.assertEqual(controller.filters["gpu"].value, 35.0)
        self.assertIsNone(controller.filters["ir"].value)
        self.assertIsNone(controller.filters["acpi"].value)


class ControllerShutdownTests(_ControllerTestCase):
    def test_hot_timed_exit_selects_maximum(self):
        settings = Settings.load(CONFIG_PATH)
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=FakeSensors(70),
            apply=True,
            duration_s=2.5,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )
        controller.run()
        self.assertEqual(fan.actions[0][0], "manual")
        self.assertEqual(fan.actions[-1][0], "maximum")
        self.assertEqual(fan.mode, MAX_MODE)

    def test_stop_reports_guard_cleanup_failure_without_questioning_maximum(self):
        guard = self.root / "auto-guard"
        guard.mkdir()
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=fixed_policy_settings(),
            fan=fan,
            sensors=FakeSensors(50),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
            auto_guard_path=guard,
        )
        controller.manual_active = True
        controller.auto_guard_until = controller.clock() + 60.0

        with (
            patch("hp_fan_control.controller.LOG.critical") as critical,
            patch("hp_fan_control.controller.LOG.error") as error,
        ):
            controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)
        self.assertFalse(
            any(
                call_args.args[0].startswith("FAILED TO SELECT MAXIMUM FANS")
                for call_args in critical.call_args_list
            )
        )
        error.assert_called_once_with(
            "maximum fans selected but failed to clear Auto guard: %s",
            ANY,
        )

    def test_stop_retains_guard_when_selecting_maximum_fails(self):
        fan = FakeFan()
        fan.set_maximum = Mock(side_effect=HardwareError("write failed"))
        controller = controller_with_fake_time(
            settings=fixed_policy_settings(),
            fan=fan,
            sensors=FakeSensors(50),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
        )
        controller.manual_active = True
        controller.auto_guard_until = controller.clock() + 60.0

        with (
            patch.object(controller, "_clear_auto_guard") as clear_guard,
            patch("hp_fan_control.controller.LOG.critical") as critical,
        ):
            controller._failsafe_on_stop()

        clear_guard.assert_not_called()
        self.assertIsNotNone(controller.auto_guard_until)
        critical.assert_any_call("FAILED TO SELECT MAXIMUM FANS: %s", ANY)

    def test_stop_clears_expired_guard_without_selecting_maximum(self):
        guard = self.root / "auto-guard"
        guard.write_text("expired\n")
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=fixed_policy_settings(),
            fan=fan,
            sensors=FakeSensors(50),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
            auto_guard_path=guard,
        )
        controller.auto_guard_until = controller.clock() - 10.0

        controller._failsafe_on_stop()

        self.assertFalse(guard.exists())
        self.assertEqual(fan.actions, [])
        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertIsNone(controller.auto_guard_until)

    def test_stop_reports_failure_to_clear_guard_without_selecting_maximum(self):
        fan = FakeFan()
        fan.set_maximum = Mock(wraps=fan.set_maximum)
        controller = controller_with_fake_time(
            settings=fixed_policy_settings(),
            fan=fan,
            sensors=FakeSensors(50),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
        )
        controller.auto_guard_until = controller.clock() - 10.0

        with (
            patch.object(
                controller,
                "_clear_auto_guard",
                side_effect=HardwareError("unlink failed"),
            ) as clear_guard,
            patch("hp_fan_control.controller.LOG.error") as error,
        ):
            controller._failsafe_on_stop()

        fan.set_maximum.assert_not_called()
        clear_guard.assert_called_once_with()
        error.assert_called_once_with(
            "failed to clear Auto guard: %s",
            ANY,
        )


class RuntimeMarkerIsolation(unittest.TestCase):
    """Keeps tests away from the runtime marker of a live system service.

    The recovery paths delete the confirmed-board marker once the fans are
    safe. Running the suite as root while the service is active would otherwise
    remove the marker that its ExecStopPost depends on.
    """

    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.confirmed_board_path = Path(directory) / "board"
        patcher = patch(
            "hp_fan_control.cli.CONFIRMED_BOARD_PATH", self.confirmed_board_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class AllowedBoardRecoveryTests(RuntimeMarkerIsolation):
    def _safe_fan(self, mode=AUTO_MODE):
        fan = Mock(spec=HpFanHwmon)
        fan.path = Path("/sys/class/hwmon/hwmon7")
        fan.status.return_value = (mode, 0, 3000, 3000)
        return fan

    def _write_config(self, body):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = Path(directory) / "fan-control.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_load_allowed_boards_reads_configured_list(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8D87", "8C99"]\n')
        self.assertEqual(load_allowed_boards(path), ("8D87", "8C99"))

    def test_load_allowed_boards_falls_back_when_file_is_missing(self):
        missing = Path(tempfile.mkdtemp()) / "absent.toml"
        self.assertEqual(load_allowed_boards(missing), DEFAULT_ALLOWED_BOARDS)

    def test_load_allowed_boards_falls_back_on_unrelated_syntax_error(self):
        path = self._write_config('[daemon\nallowed_boards = ["8C99"]\n')
        self.assertEqual(load_allowed_boards(path), DEFAULT_ALLOWED_BOARDS)

    def test_load_allowed_boards_falls_back_on_empty_list(self):
        path = self._write_config("[daemon]\nallowed_boards = []\n")
        self.assertEqual(load_allowed_boards(path), DEFAULT_ALLOWED_BOARDS)

    def test_load_allowed_boards_falls_back_on_a_bare_string(self):
        # A string is iterable: accepting one would expand "8C99" into its
        # characters and then reject the board it names.
        path = self._write_config('[daemon]\nallowed_boards = "8C99"\n')
        self.assertEqual(load_allowed_boards(path), DEFAULT_ALLOWED_BOARDS)

    def test_load_allowed_boards_falls_back_on_non_string_entries(self):
        path = self._write_config("[daemon]\nallowed_boards = [8887]\n")
        self.assertEqual(load_allowed_boards(path), DEFAULT_ALLOWED_BOARDS)

    def test_load_allowed_boards_trims_entries(self):
        path = self._write_config('[daemon]\nallowed_boards = [" 8C99 ", ""]\n')
        self.assertEqual(load_allowed_boards(path), ("8C99",))

    def test_load_allowed_boards_falls_back_on_invalid_utf8(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = Path(directory) / "fan-control.toml"
        path.write_bytes(b'\xff\xfe[daemon]\nallowed_boards = ["8C99"]\n')
        self.assertEqual(load_allowed_boards(path), DEFAULT_ALLOWED_BOARDS)

    def test_recovery_allowlist_keeps_a_confirmed_board_after_config_damage(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        damaged = directory / "fan-control.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)

        self.assertEqual(recovery_allowed_boards(damaged, confirmed), ("8D87", "8C99"))

    def test_recovery_allowlist_does_not_duplicate_a_configured_board(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        confirmed = path.parent / "board"
        record_confirmed_board("8C99", confirmed)

        self.assertEqual(recovery_allowed_boards(path, confirmed), ("8C99",))

    def test_recovery_allowlist_ignores_a_missing_or_unreadable_marker(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        absent = path.parent / "board"
        self.assertEqual(recovery_allowed_boards(path, absent), ("8C99",))

        absent.write_bytes(b"\xff\xfe")
        self.assertEqual(recovery_allowed_boards(path, absent), ("8C99",))

    def test_failsafe_accepts_a_confirmed_board_when_the_config_is_damaged(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        damaged = directory / "fan-control.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        fan = self._safe_fan()
        with (
            patch("hp_fan_control.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("hp_fan_control.cli.read_text", return_value="8C99"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=Mock()),
            patch("hp_fan_control.cli.HpFanHwmon", return_value=fan),
            patch("hp_fan_control.cli.ensure_failsafe_fan_state") as failsafe,
        ):
            result = main(["--config", str(damaged), "--failsafe"])

        self.assertEqual(result, 0)
        failsafe.assert_called_once_with(fan, AUTO_GUARD_PATH)

    def test_apply_records_the_confirmed_board_before_touching_the_fans(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        observed = []
        with (
            patch("hp_fan_control.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("hp_fan_control.cli.Settings.load", return_value=settings),
            patch("hp_fan_control.cli.read_text", return_value="8C99"),
            patch("hp_fan_control.cli.validate_required_profile"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=Mock()),
            patch("hp_fan_control.cli.wait_for_hp_fan_hwmon") as wait_fan,
            patch(
                "hp_fan_control.cli.wait_for_temperature_sensors",
                return_value=Mock(spec=Sensors),
            ),
            patch("hp_fan_control.cli.CsvLog", return_value=Mock(spec=CsvLog)),
            patch(
                "hp_fan_control.cli.SystemdNotifier.from_environment",
                return_value=Mock(spec=SystemdNotifier),
            ),
            patch("hp_fan_control.cli.Controller", return_value=Mock(spec=Controller)),
            patch("hp_fan_control.cli.signal.signal"),
        ):
            fan = Mock(spec=HpFanHwmon)
            fan.path = Path("/sys/class/hwmon/hwmon7")

            def discover_fan():
                observed.append(confirmed.read_text(encoding="ascii"))
                return fan

            wait_fan.side_effect = discover_fan
            with patch.dict(os.environ, {"RUNTIME_DIRECTORY": str(confirmed.parent)}):
                result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertEqual(observed, ["8C99\n"])

    def _apply_run_patches(self, confirmed, settings, extra=()):
        """Patch a full apply-mode startup down to a Mock controller."""
        wait_fan = patch("hp_fan_control.cli.wait_for_hp_fan_hwmon")
        fan = self._safe_fan()
        started = wait_fan.start()
        started.return_value = fan
        self.fan = fan
        self.addCleanup(wait_fan.stop)
        for target in (
            patch("hp_fan_control.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("hp_fan_control.cli.Settings.load", return_value=settings),
            patch("hp_fan_control.cli.read_text", return_value="8C99"),
            patch("hp_fan_control.cli.validate_required_profile"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=Mock()),
            patch(
                "hp_fan_control.cli.wait_for_temperature_sensors",
                return_value=Mock(spec=Sensors),
            ),
            patch("hp_fan_control.cli.CsvLog", return_value=Mock(spec=CsvLog)),
            patch(
                "hp_fan_control.cli.SystemdNotifier.from_environment",
                return_value=Mock(spec=SystemdNotifier),
            ),
            patch("hp_fan_control.cli.signal.signal"),
            *extra,
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_manual_apply_clears_its_marker_when_the_run_ends(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        controller = Mock(spec=Controller)
        recorded = []
        controller.run.side_effect = lambda: recorded.append(confirmed.exists())
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(patch("hp_fan_control.cli.Controller", return_value=controller),),
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertEqual(recorded, [True])
        self.assertFalse(confirmed.exists())

    def test_manual_apply_clears_its_marker_after_a_failed_run(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        controller = Mock(spec=Controller)
        controller.run.side_effect = HardwareError("sensor lost")
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(patch("hp_fan_control.cli.Controller", return_value=controller),),
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            result = main(["--apply"])

        self.assertEqual(result, 1)
        self.assertFalse(confirmed.exists())

    def test_manual_actuator_test_clears_its_marker(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(patch("hp_fan_control.cli.run_actuator_test"),),
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            result = main(["--apply", "--actuator-test", "60"])

        self.assertEqual(result, 0)
        self.assertFalse(confirmed.exists())

    def test_systemd_managed_apply_leaves_the_marker_for_exec_stop_post(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(
                patch(
                    "hp_fan_control.cli.Controller",
                    return_value=Mock(spec=Controller),
                ),
            ),
        )
        with patch.dict(os.environ, {"RUNTIME_DIRECTORY": str(confirmed.parent)}):
            result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertEqual(confirmed.read_text(encoding="ascii"), "8C99\n")

    def test_completed_recovery_drops_the_marker(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        damaged = directory / "fan-control.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        with (
            patch("hp_fan_control.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("hp_fan_control.cli.read_text", return_value="8C99"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=Mock()),
            patch("hp_fan_control.cli.HpFanHwmon", return_value=self._safe_fan()),
            patch("hp_fan_control.cli.ensure_failsafe_fan_state"),
        ):
            result = main(["--config", str(damaged), "--failsafe"])

        self.assertEqual(result, 0)
        self.assertFalse(confirmed.exists())

    def test_rejected_recovery_keeps_the_marker(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        damaged = directory / "fan-control.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        with (
            patch("hp_fan_control.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("hp_fan_control.cli.read_text", return_value="8DFF"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.ensure_failsafe_fan_state") as failsafe,
            self.assertLogs("hp-fan-control", level="ERROR"),
        ):
            result = main(["--config", str(damaged), "--failsafe"])

        self.assertEqual(result, 1)
        failsafe.assert_not_called()
        self.assertTrue(confirmed.exists())

    def test_marker_survives_a_run_that_left_software_control_active(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        controller = Mock(spec=Controller)
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(patch("hp_fan_control.cli.Controller", return_value=controller),),
        )
        # The controller stop path only logs when it cannot select maximum fans.
        self.fan.status.return_value = (MANUAL_MODE, 128, 4200, 4400)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            with self.assertLogs("hp-fan-control", level="ERROR") as logs:
                result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertTrue(confirmed.exists())
        self.assertIn("fan mode 1 is not safe", "\n".join(logs.output))

    def test_marker_survives_an_actuator_test_that_did_not_restore_auto(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(
                patch(
                    "hp_fan_control.cli.run_actuator_test",
                    side_effect=HardwareError("failed to verify firmware Auto"),
                ),
            ),
        )
        self.fan.status.return_value = (MANUAL_MODE, 153, 5000, 5100)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            result = main(["--apply", "--actuator-test", "60"])

        self.assertEqual(result, 1)
        self.assertTrue(confirmed.exists())

    def test_marker_is_dropped_after_a_run_that_ended_in_maximum_fans(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        self._apply_run_patches(
            confirmed,
            settings,
            extra=(
                patch(
                    "hp_fan_control.cli.Controller",
                    return_value=Mock(spec=Controller),
                ),
            ),
        )
        self.fan.status.return_value = (MAX_MODE, 255, 6000, 6100)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertFalse(confirmed.exists())

    def test_marker_survives_an_unreadable_fan_state(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        fan = Mock(spec=HpFanHwmon)
        fan.status.side_effect = HardwareError("cannot read integer from pwm1_enable")
        with self.assertLogs("hp-fan-control", level="ERROR") as logs:
            clear_confirmed_board_if_safe(confirmed, fan)

        self.assertTrue(confirmed.exists())
        self.assertIn("keeping the confirmed board marker", "\n".join(logs.output))

    def test_cleanup_never_raises_out_of_the_shutdown_path(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        fan = Mock(spec=HpFanHwmon)
        fan.status.side_effect = RuntimeError("unexpected")
        with self.assertLogs("hp-fan-control", level="ERROR"):
            clear_confirmed_board_if_safe(confirmed, fan)

        self.assertTrue(confirmed.exists())

    def test_runtime_directory_identifies_only_the_managed_directory(self):
        marker = Path("/run/hp-fan-control/board")
        cases = {
            "": False,
            "/run/other-unit": False,
            "/run/hp-fan-control-backup": False,
            "/run/hp-fan-control": True,
            "/run/other-unit:/run/hp-fan-control": True,
        }
        for value, expected in cases.items():
            with self.subTest(runtime_directory=value):
                with patch.dict(os.environ, {"RUNTIME_DIRECTORY": value}):
                    self.assertEqual(systemd_owns_runtime_directory(marker), expected)

    def test_inherited_invocation_id_does_not_claim_ownership(self):
        marker = Path("/run/hp-fan-control/board")
        with patch.dict(os.environ, {"INVOCATION_ID": "b3f0"}):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            self.assertFalse(systemd_owns_runtime_directory(marker))

    def test_clear_confirmed_board_reports_rather_than_raises(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        clear_confirmed_board(directory / "absent")

        with (
            patch("pathlib.Path.unlink", side_effect=OSError("read-only file system")),
            self.assertLogs("hp-fan-control", level="ERROR") as logs,
        ):
            clear_confirmed_board(directory / "board")

        self.assertIn("cannot clear the confirmed board marker", "\n".join(logs.output))

    def test_dry_run_does_not_record_a_confirmed_board(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        with (
            patch("hp_fan_control.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("hp_fan_control.cli.Settings.load", return_value=settings),
            patch("hp_fan_control.cli.read_text", return_value="8C99"),
            patch("hp_fan_control.cli.validate_required_profile"),
            patch("hp_fan_control.cli.acquire_lock", return_value=Mock()),
            patch("hp_fan_control.cli.wait_for_hp_fan_hwmon") as wait_fan,
            patch(
                "hp_fan_control.cli.wait_for_temperature_sensors",
                return_value=Mock(spec=Sensors),
            ),
            patch("hp_fan_control.cli.CsvLog", return_value=Mock(spec=CsvLog)),
            patch(
                "hp_fan_control.cli.SystemdNotifier.from_environment",
                return_value=Mock(spec=SystemdNotifier),
            ),
            patch("hp_fan_control.cli.Controller", return_value=Mock(spec=Controller)),
            patch("hp_fan_control.cli.signal.signal"),
        ):
            wait_fan.return_value = Mock(spec=HpFanHwmon)
            wait_fan.return_value.path = Path("/sys/class/hwmon/hwmon7")
            result = main([])

        self.assertEqual(result, 0)
        self.assertFalse(confirmed.exists())

    def test_failsafe_accepts_an_allowlisted_non_default_board(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        fan = self._safe_fan()
        with (
            patch("hp_fan_control.cli.read_text", return_value="8C99"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=Mock()),
            patch("hp_fan_control.cli.HpFanHwmon", return_value=fan),
            patch("hp_fan_control.cli.ensure_failsafe_fan_state") as failsafe,
        ):
            result = main(["--config", str(path), "--failsafe"])

        self.assertEqual(result, 0)
        failsafe.assert_called_once_with(fan, AUTO_GUARD_PATH)

    def test_failsafe_rejects_a_board_outside_the_allowlist(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        with (
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.ensure_failsafe_fan_state") as failsafe,
            self.assertLogs("hp-fan-control", level="ERROR") as logs,
        ):
            result = main(["--config", str(path), "--failsafe"])

        self.assertEqual(result, 1)
        self.assertIn("fan recovery is only allowed", "\n".join(logs.output))
        failsafe.assert_not_called()

    def test_restore_auto_rejects_a_board_outside_the_allowlist(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        with (
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.restore_firmware_auto") as restore,
            self.assertLogs("hp-fan-control", level="ERROR") as logs,
        ):
            result = main(["--config", str(path), "--restore-auto"])

        self.assertEqual(result, 1)
        self.assertIn("fan recovery is only allowed", "\n".join(logs.output))
        restore.assert_not_called()


class MainStartupTests(RuntimeMarkerIsolation):
    def _confirmed_board_path(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        return Path(directory) / "board"

    def _safe_fan(self):
        fan = Mock(spec=HpFanHwmon)
        fan.path = Path("/sys/class/hwmon/hwmon7")
        fan.status.return_value = (AUTO_MODE, 0, 3000, 3000)
        return fan

    def test_main_constructs_and_runs_controller_with_requested_log(self):
        settings = fixed_policy_settings()
        lock = Mock()
        fan = self._safe_fan()
        sensors = Mock(spec=Sensors)
        csv_log = Mock(spec=CsvLog)
        controller = Mock(spec=Controller)
        notifier = Mock(spec=SystemdNotifier)
        log_path = Path("/tmp/requested-telemetry.csv")
        with (
            patch("hp_fan_control.cli.Settings.load", return_value=settings),
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.validate_required_profile"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=lock),
            patch(
                "hp_fan_control.cli.CONFIRMED_BOARD_PATH",
                self._confirmed_board_path(),
            ),
            patch("hp_fan_control.cli.wait_for_hp_fan_hwmon", return_value=fan),
            patch(
                "hp_fan_control.cli.wait_for_temperature_sensors",
                return_value=sensors,
            ),
            patch("hp_fan_control.cli.CsvLog", return_value=csv_log) as csv_type,
            patch(
                "hp_fan_control.cli.SystemdNotifier.from_environment",
                return_value=notifier,
            ),
            patch("hp_fan_control.cli.Controller", return_value=controller) as factory,
            patch("hp_fan_control.cli.signal.signal") as install_signal,
        ):
            result = main(
                [
                    "--config",
                    str(CONFIG_PATH),
                    "--apply",
                    "--duration",
                    "10",
                    "--status-interval",
                    "5",
                    "--log-file",
                    str(log_path),
                ]
            )

        self.assertEqual(result, 0)
        csv_type.assert_called_once_with(log_path)
        factory.assert_called_once_with(
            settings=settings,
            fan=fan,
            sensors=sensors,
            apply=True,
            duration_s=10.0,
            csv_log=csv_log,
            status_interval_s=5.0,
            notifier=notifier,
            auto_guard_path=AUTO_GUARD_PATH,
        )
        install_signal.assert_has_calls(
            [
                call(signal.SIGINT, controller.request_stop),
                call(signal.SIGTERM, controller.request_stop),
            ]
        )
        controller.run.assert_called_once_with()
        csv_log.close.assert_called_once_with()
        lock.close.assert_called_once_with()

    def test_main_dispatches_actuator_test_without_constructing_controller(self):
        settings = fixed_policy_settings()
        lock = Mock()
        fan = self._safe_fan()
        sensors = Mock(spec=Sensors)
        with (
            patch("hp_fan_control.cli.Settings.load", return_value=settings),
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.validate_required_profile"),
            patch("hp_fan_control.cli.os.geteuid", return_value=0),
            patch("hp_fan_control.cli.acquire_lock", return_value=lock),
            patch(
                "hp_fan_control.cli.CONFIRMED_BOARD_PATH",
                self._confirmed_board_path(),
            ),
            patch("hp_fan_control.cli.wait_for_hp_fan_hwmon", return_value=fan),
            patch(
                "hp_fan_control.cli.wait_for_temperature_sensors",
                return_value=sensors,
            ),
            patch("hp_fan_control.cli.run_actuator_test") as actuator_test,
            patch("hp_fan_control.cli.Controller") as controller_type,
            patch("hp_fan_control.cli.CsvLog") as csv_type,
        ):
            result = main(["--apply", "--actuator-test", "60", "--duration", "12"])

        self.assertEqual(result, 0)
        actuator_test.assert_called_once_with(
            fan,
            sensors,
            60.0,
            12.0,
            settings.minimum_manual_percent,
        )
        controller_type.assert_not_called()
        csv_type.assert_not_called()
        lock.close.assert_called_once_with()

    def test_main_applies_sensor_selection_overrides(self):
        base = fixed_policy_settings()
        cases = (
            (
                ["--cpu-only"],
                {
                    "include_acpi": False,
                    "include_amd_gpu": False,
                    "include_nvidia_gpu": False,
                    "include_hp_wmi_ir": False,
                },
            ),
            (
                ["--include-acpi-proxy"],
                {
                    "include_acpi": True,
                    "include_amd_gpu": True,
                    "include_nvidia_gpu": True,
                    "include_hp_wmi_ir": True,
                },
            ),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                lock = Mock()
                wait_for_sensors = Mock(
                    side_effect=HardwareError("stop after settings capture")
                )
                with (
                    patch("hp_fan_control.cli.Settings.load", return_value=base),
                    patch("hp_fan_control.cli.read_text", return_value="8D87"),
                    patch("hp_fan_control.cli.validate_required_profile"),
                    patch("hp_fan_control.cli.dry_run_lock_path"),
                    patch("hp_fan_control.cli.acquire_lock", return_value=lock),
                    patch("hp_fan_control.cli.wait_for_hp_fan_hwmon"),
                    patch(
                        "hp_fan_control.cli.wait_for_temperature_sensors",
                        wait_for_sensors,
                    ),
                    patch("hp_fan_control.cli.LOG.error"),
                ):
                    result = main([*arguments, "--no-log-file"])

                self.assertEqual(result, 1)
                selected = wait_for_sensors.call_args.args[0]
                for name, value in expected.items():
                    self.assertEqual(getattr(selected, name), value)
                lock.close.assert_called_once_with()

    def test_main_rejects_invalid_runtime_options_before_locking(self):
        settings = fixed_policy_settings()
        cases = (
            (["--apply"], 1000, 1),
            (["--duration", "0"], 0, CONFIGURATION_ERROR_EXIT_STATUS),
            (
                ["--status-interval", "0.5"],
                0,
                CONFIGURATION_ERROR_EXIT_STATUS,
            ),
        )
        for arguments, effective_uid, expected_status in cases:
            with self.subTest(arguments=arguments):
                acquire = Mock()
                with (
                    patch(
                        "hp_fan_control.cli.Settings.load",
                        return_value=settings,
                    ),
                    patch("hp_fan_control.cli.read_text", return_value="8D87"),
                    patch("hp_fan_control.cli.validate_required_profile"),
                    patch(
                        "hp_fan_control.cli.os.geteuid",
                        return_value=effective_uid,
                    ),
                    patch("hp_fan_control.cli.acquire_lock", acquire),
                    patch("hp_fan_control.cli.LOG.error"),
                ):
                    result = main(arguments)

                self.assertEqual(result, expected_status)
                acquire.assert_not_called()

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

    def test_main_closes_lock_when_k10temp_startup_times_out(self):
        lock = Mock()
        with (
            patch("hp_fan_control.cli.read_text", return_value="8D87"),
            patch("hp_fan_control.cli.validate_required_profile"),
            patch("hp_fan_control.cli.acquire_lock", return_value=lock),
            patch("hp_fan_control.cli.wait_for_hp_fan_hwmon"),
            patch(
                "hp_fan_control.cli.wait_for_temperature_sensors",
                side_effect=HardwareError("k10temp startup timeout"),
            ),
        ):
            self.assertEqual(
                main(["--config", str(CONFIG_PATH), "--no-log-file"]),
                1,
            )

        lock.close.assert_called_once_with()

    def test_main_preserves_successful_system_exit_without_explicit_code(self):
        with patch("hp_fan_control.cli.parse_args", side_effect=SystemExit(None)):
            self.assertEqual(main([]), 0)

    def test_main_returns_retryable_exit_code_for_unavailable_required_profile(self):
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
            patch(
                "hp_fan_control.cli.validate_required_profile",
                side_effect=HardwareError("required platform profile startup timeout"),
            ),
            patch("hp_fan_control.cli.acquire_lock", acquire),
        ):
            self.assertEqual(
                main(["--no-log-file"]),
                1,
            )

        acquire.assert_not_called()


class RecoveryCommandTests(RuntimeMarkerIsolation):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

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

    def test_actuator_test_respects_configured_manual_minimum(self):
        with self.assertRaisesRegex(
            ConfigurationError,
            "must be between 40 and 100 percent",
        ):
            run_actuator_test(FakeFan(), FakeSensors(50), 39.9, 1, 40.0)

    def test_actuator_test_rejects_invalid_ranges_and_non_auto_mode(self):
        cases = (
            (101.0, 15.0, 40.0, "between 40 and 100 percent"),
            (60.0, 0.9, 40.0, "duration must be between 1 and 60 seconds"),
            (60.0, 60.1, 40.0, "duration must be between 1 and 60 seconds"),
        )
        for percent, duration, minimum, message in cases:
            with (
                self.subTest(percent=percent, duration=duration),
                self.assertRaisesRegex(ConfigurationError, message),
            ):
                run_actuator_test(
                    FakeFan(), FakeSensors(50), percent, duration, minimum
                )

        fan = FakeFan()
        fan.mode = MANUAL_MODE
        with self.assertRaisesRegex(HardwareError, "requires firmware Auto"):
            run_actuator_test(fan, FakeSensors(50), 60.0, 15.0, 40.0)

    def test_restore_auto_recovery_command(self):
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        restore_firmware_auto(fan)
        self.assertEqual(fan.actions, [("auto", None)])
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_restore_auto_rejects_failed_mode_verification(self):
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        fan.restore_auto = Mock()

        with self.assertRaisesRegex(
            HardwareError,
            "failed to verify firmware Auto: mode=1",
        ):
            restore_firmware_auto(fan)

        fan.restore_auto.assert_called_once_with()

    def test_failsafe_recovery_replaces_manual_with_maximum(self):
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        ensure_failsafe_fan_state(fan)
        self.assertEqual(fan.actions, [("maximum", 255)])
        self.assertEqual(fan.mode, MAX_MODE)

    def test_failsafe_recovery_rejects_failed_mode_verification(self):
        fan = FakeFan()
        fan.mode = MANUAL_MODE
        fan.set_maximum = Mock()

        with self.assertRaisesRegex(
            HardwareError,
            "failed to establish Auto or maximum fail-safe: mode=1",
        ):
            ensure_failsafe_fan_state(fan)

        fan.set_maximum.assert_called_once_with()

    def test_failsafe_recovery_preserves_existing_auto(self):
        fan = FakeFan()
        ensure_failsafe_fan_state(fan)
        self.assertEqual(fan.actions, [])
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_failsafe_recovery_replaces_guarded_auto_with_maximum(self):
        guard = self.root / "auto-guard"
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
        guard = self.root / "missing-auto-guard"
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
                {
                    "NOTIFY_SOCKET": "@notify",
                    "WATCHDOG_PID": str(os.getpid()),
                    "WATCHDOG_USEC": "15000000",
                },
                clear=True,
            ),
            patch("hp_fan_control.controller.socket.socket", return_value=context),
        ):
            notifier = SystemdNotifier.from_environment()
            notifier.watchdog()
        self.assertEqual(notifier.watchdog_interval_s, 7.5)
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

    def test_initial_read_failure_closes_profile_handle(self):
        path = Mock()
        handle = Mock()
        path.open.return_value = handle
        with (
            patch("hp_fan_control.hardware.select.poll"),
            patch.object(
                PlatformProfileMonitor,
                "_read",
                side_effect=HardwareError("read failed"),
            ),
            self.assertRaisesRegex(HardwareError, "read failed"),
        ):
            PlatformProfileMonitor(path)

        handle.close.assert_called_once_with()


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

    def test_failed_initial_pwm_write_reports_failed_auto_rollback(self):
        fan = initialized_fan(self)
        pwm_failure = HardwareError("PWM write failed")
        rollback_failure = HardwareError("Auto rollback failed")
        with (
            patch(
                "hp_fan_control.hardware.write_int",
                side_effect=[None, pwm_failure, rollback_failure],
            ) as write,
            patch("hp_fan_control.hardware.LOG.critical") as critical,
            self.assertRaisesRegex(HardwareError, "PWM write failed"),
        ):
            fan.set_manual(100)

        self.assertEqual(
            write.call_args_list,
            [
                call(fan.enable, MANUAL_MODE),
                call(fan.pwm, 100),
                call(fan.enable, AUTO_MODE),
            ],
        )
        critical.assert_called_once_with(
            "initial manual PWM write failed and Auto rollback also failed: %s",
            rollback_failure,
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
    def test_waits_for_k10temp_directory_on_real_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cpu = root / "hwmon0"
            settings = settings_with(
                fixed_policy_settings(),
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                include_hp_wmi_ir=False,
            )

            def publish_sensor(_delay):
                cpu.mkdir()
                (cpu / "name").write_text("k10temp\n")
                (cpu / "temp1_input").write_text("50000\n")

            with (
                patch(
                    "hp_fan_control.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "hp_fan_control.hardware.time.sleep",
                    side_effect=publish_sensor,
                ) as sleep,
                patch("hp_fan_control.hardware.LOG.warning") as log_warning,
                patch("hp_fan_control.hardware.LOG.info") as log_info,
            ):
                sensors = wait_for_temperature_sensors(settings, root=root)

            self.assertEqual(sensors.cpu_hwmon, cpu)
            self.assertEqual(sensors.read().cpu, 50.0)
            sleep.assert_called_once_with(1.0)
            log_warning.assert_called_once()
            log_info.assert_called_once_with("k10temp temperature source became ready")

    def test_waits_for_valid_k10temp_input_on_real_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cpu = root / "hwmon0"
            cpu.mkdir()
            (cpu / "name").write_text("k10temp\n")
            settings = settings_with(
                fixed_policy_settings(),
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                include_hp_wmi_ir=False,
            )

            def publish_temperature(_delay):
                (cpu / "temp1_input").write_text("50000\n")

            with (
                patch(
                    "hp_fan_control.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "hp_fan_control.hardware.time.sleep",
                    side_effect=publish_temperature,
                ) as sleep,
                patch("hp_fan_control.hardware.LOG.warning") as log_warning,
                patch("hp_fan_control.hardware.LOG.info") as log_info,
            ):
                sensors = wait_for_temperature_sensors(settings, root=root)

            self.assertEqual(sensors.cpu_hwmon, cpu)
            self.assertEqual(sensors.read().cpu, 50.0)
            sleep.assert_called_once_with(1.0)
            log_warning.assert_called_once()
            log_info.assert_called_once_with("k10temp temperature source became ready")

    def test_fails_after_k10temp_startup_timeout(self):
        settings = settings_with(
            fixed_policy_settings(),
            include_amd_gpu=False,
            include_nvidia_gpu=False,
            include_hp_wmi_ir=False,
        )
        with (
            patch(
                "hp_fan_control.hardware.Sensors",
                side_effect=HardwareNotReadyError("not ready"),
            ),
            patch(
                "hp_fan_control.hardware.time.monotonic",
                side_effect=[100.0, 120.0],
            ),
            patch("hp_fan_control.hardware.time.sleep") as sleep,
            self.assertRaisesRegex(
                HardwareError,
                "k10temp temperature source did not become ready within 20 seconds",
            ),
        ):
            wait_for_temperature_sensors(settings)

        sleep.assert_not_called()

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

    def test_script_entry_point_uses_configuration_status_for_bad_arguments(self):
        result = subprocess.run(
            [sys.executable, str(ENTRY_POINT_PATH), "--unknown-option"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, CONFIGURATION_ERROR_EXIT_STATUS)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_script_entry_point_uses_configuration_status_for_invalid_toml(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "invalid.toml"
            config.write_text("[daemon\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ENTRY_POINT_PATH),
                    "--config",
                    str(config),
                    "--no-log-file",
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, CONFIGURATION_ERROR_EXIT_STATUS)
        self.assertIn("cannot load", result.stderr)


class SystemdUnitTests(unittest.TestCase):
    def test_unit_setting_parser_ignores_comments_with_equals(self):
        unit = "# Note: TimeoutStartSec=1s\nTimeoutStartSec=90s\n"

        self.assertEqual(
            parse_systemd_settings(unit),
            {"TimeoutStartSec": "90s"},
        )

    def test_start_timeout_covers_sequential_hwmon_readiness_windows(self):
        service = SERVICE_PATH.read_text(encoding="utf-8")
        settings = parse_systemd_settings(service)
        timeout_start_s = float(settings["TimeoutStartSec"].removesuffix("s"))
        sequential_readiness_s = sum(
            (
                PLATFORM_PROFILE_STARTUP_TIMEOUT_S,
                HP_HWMON_STARTUP_TIMEOUT_S,
                K10TEMP_STARTUP_TIMEOUT_S,
            )
        )

        self.assertGreater(timeout_start_s, sequential_readiness_s)

    def test_restart_policy_retries_runtime_but_not_configuration_failures(self):
        service = SERVICE_PATH.read_text(encoding="utf-8")
        settings = parse_systemd_settings(service)

        self.assertIn("Restart=always\n", service)
        self.assertIn(
            f"RestartPreventExitStatus={CONFIGURATION_ERROR_EXIT_STATUS}\n",
            service,
        )
        restart_s = float(settings["RestartSec"].removesuffix("s"))
        burst = int(settings["StartLimitBurst"])
        interval_s = float(settings["StartLimitIntervalSec"].removesuffix("s"))
        self.assertGreater(restart_s * burst, interval_s)

    def test_csv_rotation_targets_only_the_stable_log(self):
        policy = LOGROTATE_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "/var/log/hp-fan-control/hp-fan-control.csv {\n",
            policy,
        )
        self.assertNotIn("*.csv", policy)
        for directive in (
            "daily",
            "rotate 14",
            "compress",
            "delaycompress",
            "copytruncate",
            "missingok",
            "notifempty",
        ):
            with self.subTest(directive=directive):
                self.assertIn(f"    {directive}\n", policy)


if __name__ == "__main__":
    unittest.main()
