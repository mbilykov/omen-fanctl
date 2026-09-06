#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hp_fan_control import (
    AUTO_MODE,
    MANUAL_MODE,
    Controller,
    Curve,
    CsvLog,
    Ewma,
    HardwareError,
    HpFanHwmon,
    Settings,
    Sensors,
    TemperatureSnapshot,
    hp_factory_performance_curves,
    hp_level_percent,
    percent_to_pwm,
    pwm_to_percent,
    read_hp_wmi_ir_temperature,
    run_actuator_test,
)


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.curve = Curve((50.0, 60.0, 70.0, 90.0), (25.0, 35.0, 55.0, 95.0))

    def test_clamps_below_and_above_curve(self):
        self.assertAlmostEqual(self.curve.evaluate_percent(20), 25)
        self.assertAlmostEqual(self.curve.evaluate_percent(100), 95)

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


class EwmaTests(unittest.TestCase):
    def test_asymmetric_update(self):
        ewma = Ewma(rise_alpha=0.5, fall_alpha=0.1)
        self.assertEqual(ewma.update(50), 50)
        self.assertEqual(ewma.update(70), 60)
        self.assertEqual(ewma.update(50), 59)


class ControlDecisionTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(
            allowed_boards=("8D87",),
            required_profile="performance",
            sample_interval_s=1,
            control_interval_s=5,
            activation_temp_c=65,
            release_temp_c=55,
            critical_temp_c=92,
            critical_release_temp_c=82,
            emergency_hold_s=10,
            decrease_hysteresis_c=3,
            max_rise_percent_per_update=20,
            max_fall_percent_per_update=8,
            minimum_manual_percent=35,
            ewma_rise_alpha=0.25,
            ewma_fall_alpha=0.10,
            include_acpi=True,
            include_amd_gpu=True,
            include_nvidia_gpu=True,
            curve=Curve((50, 60, 70, 80, 90), (25, 35, 55, 75, 95)),
        )
        self.controller = Controller(
            settings=settings,
            fan=None,
            sensors=None,
            apply=False,
            duration_s=None,
            csv_log=CsvLog(None),
        )

    def test_uses_hottest_sensor(self):
        pwm, hottest = self.controller._desired_pwm(
            {"cpu": 65.0, "gpu": 70.0, "acpi": 60.0}
        )
        self.assertEqual(hottest, 70)
        self.assertAlmostEqual(pwm_to_percent(pwm), 55, delta=0.2)

    def test_limits_fan_speed_decrease(self):
        self.controller.commanded_pwm = percent_to_pwm(80)
        pwm, _ = self.controller._desired_pwm(
            {"cpu": 60.0, "gpu": 50.0, "acpi": 50.0}
        )
        self.assertAlmostEqual(pwm_to_percent(pwm), 72, delta=0.4)

    def test_raw_temperature_bypasses_ewma_lag_on_rise(self):
        pwm, hottest = self.controller._desired_pwm(
            {"cpu": 55.0, "gpu": 50.0, "acpi": 50.0}, raw_hottest=80.0
        )
        self.assertEqual(hottest, 80)
        self.assertAlmostEqual(pwm_to_percent(pwm), 75, delta=0.2)

    def test_independent_gpu_curve_can_win(self):
        self.controller.settings = Settings(
            **{
                **self.controller.settings.__dict__,
                "curves": hp_factory_performance_curves(),
                "curve_source": "test-factory",
                "minimum_manual_percent": hp_level_percent(19),
            }
        )
        pwm, _ = self.controller._desired_pwm(
            {"cpu": 65.0, "gpu": 75.0, "acpi": 45.0},
            raw_temperatures={"cpu": 65.0, "gpu": 75.0, "acpi": 45.0},
        )
        self.assertEqual(self.controller.winning_sensor, "gpu")
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(31), delta=0.3)

    def test_confirmed_wmi_ir_curve_can_win(self):
        self.controller.settings = Settings(
            **{
                **self.controller.settings.__dict__,
                "curves": hp_factory_performance_curves(),
                "curve_source": "test-factory",
                "minimum_manual_percent": hp_level_percent(19),
            }
        )
        pwm, _ = self.controller._desired_pwm(
            {"cpu": 55.0, "gpu": 50.0, "ir": 54.0, "acpi": None},
            raw_temperatures={"cpu": 55.0, "gpu": 50.0, "ir": 54.0, "acpi": None},
        )
        self.assertEqual(self.controller.winning_sensor, "ir")
        self.assertAlmostEqual(pwm_to_percent(pwm), hp_level_percent(28), delta=0.3)

    def test_ir_uses_its_first_curve_point_for_activation(self):
        self.controller.settings = Settings(
            **{
                **self.controller.settings.__dict__,
                "curves": hp_factory_performance_curves(),
                "curve_source": "test-factory",
            }
        )
        self.assertFalse(
            self.controller._should_activate(
                TemperatureSnapshot(cpu=55.0, gpu=40.0, acpi=None, ir=41.0)
            )
        )
        self.assertTrue(
            self.controller._should_activate(
                TemperatureSnapshot(cpu=55.0, gpu=40.0, acpi=None, ir=42.0)
            )
        )

    def test_ir_release_threshold_prevents_auto_manual_oscillation(self):
        self.controller.settings = Settings(
            **{
                **self.controller.settings.__dict__,
                "curves": hp_factory_performance_curves(),
                "curve_source": "test-factory",
            }
        )
        self.assertFalse(
            self.controller._cool_enough_for_auto(
                TemperatureSnapshot(cpu=40.0, gpu=40.0, acpi=None, ir=40.0),
                {"cpu": 40.0, "gpu": 40.0, "ir": 40.0, "acpi": None},
            )
        )
        self.assertTrue(
            self.controller._cool_enough_for_auto(
                TemperatureSnapshot(cpu=39.0, gpu=39.0, acpi=None, ir=39.0),
                {"cpu": 39.0, "gpu": 39.0, "ir": 39.0, "acpi": None},
            )
        )

    def test_does_not_restore_auto_while_raw_temperature_is_hot(self):
        filtered = {"cpu": 51.0, "gpu": 50.0, "acpi": None}
        self.assertFalse(
            self.controller._cool_enough_for_auto(
                TemperatureSnapshot(70.0, 50.0, None), filtered
            )
        )
        self.assertTrue(
            self.controller._cool_enough_for_auto(
                TemperatureSnapshot(51.0, 50.0, None), filtered
            )
        )


class SettingsTests(unittest.TestCase):
    def test_loads_factory_preset(self):
        config = Path(__file__).with_name("fan-control.toml")
        settings = Settings.load(config)
        self.assertEqual(
            settings.curve_source, "hp-vibrance-stx-n22x9-performance"
        )
        self.assertEqual(set(settings.curves or {}), {"cpu", "gpu", "ir"})
        self.assertAlmostEqual(
            settings.curve_for("gpu").pwm_percent[-1], hp_level_percent(47)
        )


class SensorMetricTests(unittest.TestCase):
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
        sensors = object.__new__(Sensors)
        sensors.settings = SimpleNamespace(include_hp_wmi_ir=True)
        sensors.hp_wmi_sensors_path = Path("/proc/hp_wmi_sensors")
        sensors.hp_wmi_ir_failed = False
        with patch(
            "hp_fan_control.read_hp_wmi_ir_temperature",
            side_effect=[HardwareError("missing"), 41.0],
        ):
            self.assertIsNone(sensors._hp_wmi_ir_temperature())
            self.assertTrue(sensors.hp_wmi_ir_failed)
            self.assertEqual(sensors._hp_wmi_ir_temperature(), 41.0)
            self.assertFalse(sensors.hp_wmi_ir_failed)

    def test_reads_nvidia_temperature_draw_and_limit(self):
        sensors = object.__new__(Sensors)
        sensors.nvidia_smi = "/usr/bin/nvidia-smi"
        result = SimpleNamespace(returncode=0, stdout="72, 174.5, 175.0\n")
        with patch("hp_fan_control.subprocess.run", return_value=result):
            self.assertEqual(sensors._nvidia_metrics(), (72.0, 174.5, 175.0))

    def test_keeps_temperature_when_power_is_unavailable(self):
        sensors = object.__new__(Sensors)
        sensors.nvidia_smi = "/usr/bin/nvidia-smi"
        result = SimpleNamespace(returncode=0, stdout="61, [N/A], [N/A]\n")
        with patch("hp_fan_control.subprocess.run", return_value=result):
            self.assertEqual(sensors._nvidia_metrics(), (61.0, None, None))


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

    def update_manual(self, pwm):
        self.actions.append(("update", pwm))
        self.pwm = pwm

    def set_maximum(self):
        self.actions.append(("maximum", 255))
        self.mode = 0
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


class ControllerLoopTests(unittest.TestCase):
    def test_enters_manual_and_restores_auto_at_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings(
                allowed_boards=("8D87",),
                required_profile="performance",
                sample_interval_s=0.01,
                control_interval_s=0.01,
                activation_temp_c=65,
                release_temp_c=55,
                critical_temp_c=92,
                critical_release_temp_c=82,
                emergency_hold_s=0,
                decrease_hysteresis_c=3,
                max_rise_percent_per_update=20,
                max_fall_percent_per_update=8,
                minimum_manual_percent=35,
                ewma_rise_alpha=0.25,
                ewma_fall_alpha=0.1,
                include_acpi=True,
                include_amd_gpu=True,
                include_nvidia_gpu=True,
                curve=Curve((50, 60, 70, 80, 90), (25, 35, 55, 75, 100)),
            )
            fan = FakeFan()
            controller = Controller(
                settings=settings,
                fan=fan,
                sensors=FakeSensors(70),
                apply=True,
                duration_s=0.04,
                csv_log=CsvLog(None),
                profile_path=profile,
            )
            controller.run()
            self.assertEqual(fan.actions[0][0], "manual")
            self.assertEqual(fan.actions[-1][0], "auto")
            self.assertEqual(fan.mode, AUTO_MODE)

    def test_mandatory_sensor_loss_selects_maximum_then_restores_auto(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "platform_profile"
            profile.write_text("performance\n")
            settings = Settings.load(Path(__file__).with_name("fan-control.toml"))
            settings = Settings(
                **{
                    **settings.__dict__,
                    "sample_interval_s": 0.01,
                    "control_interval_s": 0.01,
                }
            )
            fan = FakeFan()
            controller = Controller(
                settings=settings,
                fan=fan,
                sensors=FailingAfterFirstSample(),
                apply=True,
                duration_s=0.04,
                csv_log=CsvLog(None),
                profile_path=profile,
            )
            controller.run()
            self.assertEqual(fan.actions[0][0], "manual")
            self.assertIn(("maximum", 255), fan.actions)
            self.assertEqual(fan.actions[-1][0], "auto")
            self.assertEqual(fan.mode, AUTO_MODE)

    def test_actuator_test_restores_auto(self):
        fan = FakeFan()
        with (
            patch("hp_fan_control.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
            patch("hp_fan_control.time.sleep"),
            patch("hp_fan_control.signal.signal"),
        ):
            run_actuator_test(fan, FakeSensors(50), 60, 1)
        self.assertEqual(fan.actions[0][0], "manual")
        self.assertEqual(fan.actions[-1][0], "auto")
        self.assertEqual(fan.mode, AUTO_MODE)


class FakeHwmonTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
