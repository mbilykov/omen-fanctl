"""Fan-curve evaluation and PWM conversion tests."""

import unittest

from omen_fanctl.config import (
    ConfigurationError,
    Curve,
    hp_factory_performance_curves,
    hp_level_percent,
    percent_to_pwm,
    pwm_to_percent,
)


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


if __name__ == "__main__":
    unittest.main()
