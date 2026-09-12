"""Control policy, loop, telemetry, and shutdown tests."""

import csv
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from omen_fanctl.config import (
    PWM_MAX,
    ConfigurationError,
    Curve,
    Settings,
    hp_level_percent,
    percent_to_pwm,
    pwm_to_percent,
)
from omen_fanctl.controller import (
    OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES,
    STOP_HANDOFF_MAX_TEMPERATURE_AGE_S,
    Controller,
    ControlPolicy,
    CsvLog,
    Ewma,
    SystemdNotifier,
    boottime,
)
from omen_fanctl.hardware import (
    AUTO_MODE,
    MAX_MODE,
    HardwareError,
    TemperatureSnapshot,
)

from tests import CONFIG_PATH
from tests.helpers import (
    FakeFan,
    FakeSensors,
    fixed_policy_settings,
    initialized_sensors,
    initialized_sensors_with_amd_gpu,
    settings_with,
)


class _ControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "platform_profile"
        self.profile.write_text("performance\n")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, timeout_s):
        self.now += timeout_s


def controller_with_fake_time(**kwargs):
    clock = FakeClock()
    return Controller(
        clock=clock,
        freshness_clock=clock,
        wait=clock.wait,
        **kwargs,
    )


def stop_after_first_sample(controller):
    """Deliver a stop signal the way systemd does: between two samples."""
    original = controller.wait

    def wait(timeout_s):
        controller.stop_requested = True
        return original(timeout_s)

    controller.wait = wait


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
        self.assertEqual(row["pwm_abi"], "single")
        self.assertEqual(row["manual_max_level"], 56)

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
            patch("omen_fanctl.controller.LOG.info") as log_info,
            patch("omen_fanctl.controller.time.monotonic", return_value=1.0),
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
        with patch("omen_fanctl.controller.LOG.info") as info:
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

        with patch("omen_fanctl.controller.LOG.error") as error:
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

        with patch("omen_fanctl.controller.LOG.error") as error:
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
        fan.manual_pwm_max = PWM_MAX
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

        with patch("omen_fanctl.controller.LOG.info") as info:
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

        with patch("omen_fanctl.controller.LOG.info") as info:
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


class ControllerLoopTests(_ControllerTestCase):
    def test_single_channel_max_to_manual_logs_the_required_reduction(self):
        fan = FakeFan()
        fan.mode = MAX_MODE
        fan.pwm = PWM_MAX
        fan.manual_pwm_max = percent_to_pwm(hp_level_percent(56))
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=fan,
            sensors=FakeSensors(70),
            apply=True,
            duration_s=1.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        with self.assertLogs("omen-fanctl", level="WARNING") as logs:
            applied = controller._apply_manual(percent_to_pwm(hp_level_percent(47)))

        self.assertEqual(applied, fan.manual_pwm_max)
        self.assertEqual(fan.actions, [("manual", fan.manual_pwm_max)])
        self.assertEqual(controller.policy.commanded_pwm, fan.manual_pwm_max)
        self.assertIn(
            "entering Manual lowers firmware PWM from 255 to safe maximum 238 "
            "(mode=0 abi=single level=56)",
            "\n".join(logs.output),
        )

    def test_single_channel_auto_to_manual_logs_the_required_reduction(self):
        fan = FakeFan()
        fan.mode = AUTO_MODE
        fan.pwm = 250
        controller = controller_with_fake_time(
            settings=Settings.load(CONFIG_PATH),
            fan=fan,
            sensors=FakeSensors(70),
            apply=True,
            duration_s=1.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        with self.assertLogs("omen-fanctl", level="WARNING") as logs:
            applied = controller._apply_manual(percent_to_pwm(hp_level_percent(47)))

        self.assertEqual(applied, fan.manual_pwm_max)
        self.assertEqual(fan.actions, [("manual", fan.manual_pwm_max)])
        self.assertIn(
            "entering Manual lowers firmware PWM from 250 to safe maximum 238 "
            "(mode=2 abi=single level=56)",
            "\n".join(logs.output),
        )

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

    def test_shipped_trigger_selects_maximum_above_the_factory_curve(self):
        settings = Settings.load(CONFIG_PATH)
        sensors = Mock()
        sensors.read.return_value = TemperatureSnapshot(
            cpu=95.0, gpu=50.0, acpi=None, ir=None
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
        self.assertNotIn("manual", [action for action, _ in fan.actions])
        self.assertEqual(fan.actions[0], ("maximum", 255))

    def test_sensor_loss_still_selects_maximum_with_the_shipped_trigger(self):
        settings = Settings.load(CONFIG_PATH)
        sensors = Mock()
        sensors.read.side_effect = [
            TemperatureSnapshot(cpu=80.0, gpu=50.0, acpi=None, ir=None),
            HardwareError("cpu temperature unreadable"),
            HardwareError("cpu temperature unreadable"),
        ]
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

        with self.assertLogs("omen-fanctl", level="ERROR") as logs:
            controller.run()

        self.assertTrue(controller.emergency)
        self.assertIn("sensor failure during control", "\n".join(logs.output))
        self.assertEqual(fan.mode, MAX_MODE)

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

        with patch("omen_fanctl.controller.LOG.info") as info:
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
            settings=settings_with(critical_temp_c=92.0),
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
            critical_temp_c=92.0,
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
        with patch("omen_fanctl.controller.LOG.error") as error:
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

    def _stopping_controller(self, **changes):
        settings = replace(fixed_policy_settings(), **changes)
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=settings,
            fan=fan,
            sensors=FakeSensors(50),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
            auto_guard_path=self.root / "auto-guard",
        )
        fan.set_manual(81)
        fan.actions.clear()
        controller.manual_active = True
        controller.stop_requested = True
        controller.last_snapshot = TemperatureSnapshot(cpu=55.0, gpu=50.0, acpi=None)
        controller.last_snapshot_at = controller.freshness_clock()
        controller.auto_guard_until = controller.clock() + 60.0
        return controller, fan

    def test_requested_stop_returns_cool_fans_to_firmware_auto(self):
        guard = self.root / "auto-guard"
        guard.write_text("active\n")
        controller, fan = self._stopping_controller()

        controller._failsafe_on_stop()

        self.assertEqual(fan.actions, [("auto", None)])
        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertFalse(guard.exists())
        self.assertIsNone(controller.auto_guard_until)
        self.assertFalse(controller.manual_active)

    def test_requested_stop_above_the_handoff_limit_selects_maximum(self):
        controller, fan = self._stopping_controller()
        controller.last_snapshot = TemperatureSnapshot(cpu=70.1, gpu=50.0, acpi=None)

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)

    def test_requested_stop_without_a_temperature_selects_maximum(self):
        controller, fan = self._stopping_controller()
        controller.last_snapshot = None

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)

    def test_requested_stop_holding_maximum_fans_keeps_them(self):
        controller, fan = self._stopping_controller()
        controller.emergency = True

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)

    def test_requested_stop_keeps_an_externally_asserted_maximum(self):
        # Manual control preserves a maximum asserted by the EC or by the user
        # without entering the emergency state, so the mode has to be read.
        controller, fan = self._stopping_controller()
        fan.mode = MAX_MODE

        controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)

    def test_requested_stop_waits_for_a_missing_activated_sensor(self):
        # IR drove this Manual cycle and then disappeared. CPU and GPU alone
        # look cool, but the sensor that caused the cycle has not been read.
        controller, fan = self._stopping_controller()
        controller.policy.observe_activations(
            TemperatureSnapshot(cpu=50.0, gpu=50.0, acpi=None, ir=80.0)
        )
        controller.last_snapshot = TemperatureSnapshot(
            cpu=50.0, gpu=50.0, acpi=None, ir=None
        )

        with patch("omen_fanctl.controller.LOG.warning") as warning:
            controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)
        warning.assert_any_call(
            "stop requested while activated control sensors %s are missing; "
            "keeping the maximum-fan fail-safe",
            "ir",
        )

    def test_stop_hands_off_once_a_missing_sensor_has_aged_out(self):
        controller, fan = self._stopping_controller()
        missing = TemperatureSnapshot(cpu=50.0, gpu=50.0, acpi=None, ir=None)
        controller.policy.observe_activations(
            TemperatureSnapshot(cpu=50.0, gpu=50.0, acpi=None, ir=80.0)
        )
        for _ in range(OPTIONAL_SENSOR_MISSING_RELEASE_SAMPLES):
            controller.policy.observe_activations(missing)
        controller.last_snapshot = missing

        controller._failsafe_on_stop()

        self.assertEqual(fan.actions, [("auto", None)])
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_requested_stop_refuses_a_degraded_source_without_a_cache(self):
        # An NVIDIA query that fails after runtime suspend has no cached value
        # to mark stale, and an AMD GPU can still fill the aggregated reading.
        controller, fan = self._stopping_controller()
        controller.last_snapshot = TemperatureSnapshot(
            cpu=55.0,
            gpu=50.0,
            acpi=None,
            nvidia_runtime_suspended=False,
            degraded_sources=("NVIDIA GPU temperature source",),
        )

        with patch("omen_fanctl.controller.LOG.warning") as warning:
            controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)
        warning.assert_any_call(
            "stop requested while %s degraded; keeping the maximum-fan fail-safe",
            "NVIDIA GPU temperature source",
        )

    def test_a_failed_query_after_wake_blocks_the_stop_handoff(self):
        # End to end over the real sensor layer: the sample that a first failed
        # NVIDIA query after runtime suspend produces must not be handed off.
        with patch(
            "omen_fanctl.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        runtime_status = sensors.nvidia_runtime_status_files[0]
        fresh = SimpleNamespace(returncode=0, stdout="65, 80.0, 120.0\n", stderr="")

        with patch(
            "omen_fanctl.hardware.subprocess.run",
            side_effect=[fresh, subprocess.TimeoutExpired("nvidia-smi", 2.0)],
        ):
            sensors.read()
            runtime_status.write_text("suspended\n")
            sensors.read()
            runtime_status.write_text("active\n")
            after_wake = sensors.read()

        controller, fan = self._stopping_controller()
        controller.last_snapshot = after_wake

        controller._failsafe_on_stop()

        # Nothing else about this sample would have refused the handoff.
        self.assertLess(after_wake.raw_control_hottest, 70.0)
        self.assertFalse(after_wake.has_cached_readings)
        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)

    def test_requested_stop_refuses_reused_sensor_readings(self):
        controller, fan = self._stopping_controller()
        controller.last_snapshot = TemperatureSnapshot(
            cpu=55.0,
            gpu=50.0,
            acpi=None,
            nvidia_metrics_stale=True,
        )

        with patch("omen_fanctl.controller.LOG.warning") as warning:
            controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)
        warning.assert_any_call(
            "stop requested on reused sensor readings; "
            "keeping the maximum-fan fail-safe"
        )

    def test_requested_stop_refuses_a_reused_amd_reading(self):
        controller, fan = self._stopping_controller()
        controller.last_snapshot = TemperatureSnapshot(
            cpu=55.0,
            gpu=50.0,
            acpi=None,
            amd_gpu_temperature_stale=True,
        )

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)

    def test_a_powered_down_gpu_does_not_block_the_stop_handoff(self):
        # An RTD3-suspended NVIDIA GPU is expected to have no temperature,
        # which is the same exception the normal Auto handoff makes.
        controller, fan = self._stopping_controller()
        controller.policy.observe_activations(
            TemperatureSnapshot(cpu=50.0, gpu=80.0, acpi=None)
        )
        controller.last_snapshot = TemperatureSnapshot(
            cpu=55.0,
            gpu=None,
            acpi=None,
            nvidia_runtime_suspended=True,
        )

        controller._failsafe_on_stop()

        self.assertEqual(fan.actions, [("auto", None)])
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_requested_stop_selects_maximum_from_an_unknown_mode(self):
        controller, fan = self._stopping_controller()
        fan.mode = 7

        with patch("omen_fanctl.controller.LOG.error") as error:
            controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)
        error.assert_any_call(
            "unexpected fan mode %s on stop; keeping the maximum-fan fail-safe",
            7,
        )

    def test_stop_during_the_guard_clears_it_without_rewriting_auto(self):
        # hp-wmi re-applies the fan settings on every mode write, so a
        # redundant Auto write could restart the firmware fan-stop window.
        guard = self.root / "auto-guard"
        guard.write_text("active\n")
        controller, fan = self._stopping_controller()
        controller.manual_active = False
        fan.mode = AUTO_MODE

        controller._failsafe_on_stop()

        self.assertEqual(fan.actions, [])
        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertFalse(guard.exists())
        self.assertIsNone(controller.auto_guard_until)

    def test_requested_stop_selects_maximum_when_the_mode_is_unreadable(self):
        controller, fan = self._stopping_controller()
        fan.status = Mock(side_effect=HardwareError("read failed"))

        with patch("omen_fanctl.controller.LOG.error") as error:
            controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)
        error.assert_any_call(
            "cannot read the fan mode before a stop handoff: %s",
            ANY,
        )

    def test_reading_from_before_a_suspend_is_not_fresh(self):
        # CLOCK_MONOTONIC stops while the system is suspended, so scheduling
        # time can look unchanged across hours of sleep.
        controller, fan = self._stopping_controller()
        monotonic = controller.clock
        controller.freshness_clock = lambda: monotonic() + 3600.0

        controller._failsafe_on_stop()

        self.assertLess(
            controller.clock() - controller.last_snapshot_at,
            STOP_HANDOFF_MAX_TEMPERATURE_AGE_S,
        )
        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)

    def test_freshness_is_measured_on_a_suspend_aware_clock(self):
        controller = Controller(
            settings=fixed_policy_settings(),
            fan=FakeFan(),
            sensors=FakeSensors(50),
            apply=False,
            duration_s=None,
            csv_log=CsvLog(None),
        )

        self.assertIs(controller.freshness_clock, boottime)
        # CLOCK_BOOTTIME is CLOCK_MONOTONIC plus the time spent suspended, so
        # it cannot read behind a CLOCK_MONOTONIC value sampled before it.
        monotonic = time.monotonic()
        self.assertGreaterEqual(boottime(), monotonic)

    def test_requested_stop_selects_maximum_from_a_stale_reading(self):
        controller, fan = self._stopping_controller()
        controller.last_snapshot_at = (
            controller.freshness_clock() - STOP_HANDOFF_MAX_TEMPERATURE_AGE_S - 0.1
        )

        with patch("omen_fanctl.controller.LOG.warning") as warning:
            controller._failsafe_on_stop()

        self.assertNotIn(("auto", None), fan.actions)
        self.assertEqual(fan.mode, MAX_MODE)
        warning.assert_any_call(
            "stop requested with no sample newer than %g seconds; "
            "keeping the maximum-fan fail-safe",
            STOP_HANDOFF_MAX_TEMPERATURE_AGE_S,
        )

    def test_reading_at_the_freshness_limit_still_hands_off(self):
        controller, fan = self._stopping_controller()
        controller.last_snapshot_at = (
            controller.freshness_clock() - STOP_HANDOFF_MAX_TEMPERATURE_AGE_S
        )

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, AUTO_MODE)

    def test_sampling_slower_than_the_freshness_limit_is_reported(self):
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=replace(
                fixed_policy_settings(),
                sample_interval_s=STOP_HANDOFF_MAX_TEMPERATURE_AGE_S + 1.0,
                control_interval_s=STOP_HANDOFF_MAX_TEMPERATURE_AGE_S + 1.0,
            ),
            fan=fan,
            sensors=FakeSensors(50),
            apply=True,
            duration_s=0.0,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )

        with patch("omen_fanctl.controller.LOG.warning") as warning:
            controller.run()

        warning.assert_any_call(
            "sample_interval_s=%g is above the %g-second freshness limit for "
            "the stop handoff; a stop will select maximum fans unless it "
            "arrives within that limit of a sample",
            STOP_HANDOFF_MAX_TEMPERATURE_AGE_S + 1.0,
            STOP_HANDOFF_MAX_TEMPERATURE_AGE_S,
        )

    def test_unrequested_exit_selects_maximum_even_when_cool(self):
        controller, fan = self._stopping_controller()
        controller.stop_requested = False

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)

    def test_disabled_stop_handoff_selects_maximum_on_every_exit(self):
        controller, fan = self._stopping_controller(stop_handoff_max_temp_c=None)

        controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)

    def test_stop_handoff_falls_back_to_maximum_when_auto_is_refused(self):
        controller, fan = self._stopping_controller()
        fan.restore_auto = Mock(side_effect=HardwareError("write failed"))

        with patch("omen_fanctl.controller.LOG.critical") as critical:
            controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)
        critical.assert_any_call("failed to restore firmware Auto on stop: %s", ANY)

    def test_stop_handoff_selects_maximum_when_the_guard_survives(self):
        guard = self.root / "auto-guard"
        guard.mkdir()
        controller, fan = self._stopping_controller()

        with patch("omen_fanctl.controller.LOG.error") as error:
            controller._failsafe_on_stop()

        self.assertEqual(fan.mode, MAX_MODE)
        error.assert_any_call(
            "cannot clear the Auto guard after a stop handoff: %s",
            ANY,
        )

    def test_signalled_run_leaves_cool_fans_in_firmware_auto(self):
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=fixed_policy_settings(),
            fan=fan,
            sensors=FakeSensors(65),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )
        stop_after_first_sample(controller)

        controller.run()

        self.assertEqual(fan.actions[0][0], "manual")
        self.assertEqual(fan.actions[-1], ("auto", None))
        self.assertEqual(fan.mode, AUTO_MODE)

    def test_signalled_run_under_load_still_selects_maximum(self):
        fan = FakeFan()
        controller = controller_with_fake_time(
            settings=fixed_policy_settings(),
            fan=fan,
            sensors=FakeSensors(75),
            apply=True,
            duration_s=None,
            csv_log=CsvLog(None),
            profile_path=self.profile,
        )
        stop_after_first_sample(controller)

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
            patch("omen_fanctl.controller.LOG.critical") as critical,
            patch("omen_fanctl.controller.LOG.error") as error,
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
            patch("omen_fanctl.controller.LOG.critical") as critical,
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
            patch("omen_fanctl.controller.LOG.error") as error,
        ):
            controller._failsafe_on_stop()

        fan.set_maximum.assert_not_called()
        clear_guard.assert_called_once_with()
        error.assert_called_once_with(
            "failed to clear Auto guard: %s",
            ANY,
        )


if __name__ == "__main__":
    unittest.main()
