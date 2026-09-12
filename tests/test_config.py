"""Configuration loading, validation, and preset tests."""

import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

from omen_fanctl.cli import (
    _csv_log_path,
    parse_args,
)
from omen_fanctl.config import (
    CURVE_PRESETS,
    HP_8D87_CPU_GPU_LEVEL_TABLE,
    ConfigurationError,
    Settings,
    extended_performance_curves,
    hp_factory_performance_curves,
    hp_gpu_level_for_cpu_level,
    hp_level_percent,
    percent_to_hp_level,
    single_channel_performance_curves,
)
from omen_fanctl.hardware import (
    HardwareError,
    validate_required_profile,
)

from tests import CONFIG_PATH
from tests.helpers import (
    fixed_policy_settings,
)


class SettingsTests(unittest.TestCase):
    def test_default_config_is_independent_of_source_tree_layout(self):
        self.assertEqual(
            parse_args([]).config,
            Path("/etc/omen-fanctl/omen-fanctl.toml"),
        )

    def test_missing_manual_minimum_uses_factory_level_19(self):
        source = CONFIG_PATH.read_text(encoding="utf-8")
        configured_minimum = "minimum_manual_percent = 31.6667\n"
        self.assertIn(configured_minimum, source)
        source = source.replace(configured_minimum, "")
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(source, encoding="utf-8")
            settings = Settings.load(config)

        self.assertAlmostEqual(
            settings.minimum_manual_percent,
            hp_level_percent(19),
        )

    def test_omitted_behavior_defaults_match_the_packaged_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves]
preset = "performance-single-channel"
""",
                encoding="utf-8",
            )
            defaults = Settings.load(config)

        packaged = Settings.load(CONFIG_PATH)
        intentionally_different = {
            # An omitted handoff threshold must remain disabled so an upgrade
            # cannot silently adopt newly packaged shutdown behavior.
            "stop_handoff_max_temp_c",
            # The exact factory fraction and its rounded TOML spelling both
            # map to PWM 81 / HP fan level 19.
            "minimum_manual_percent",
        }
        for field in fields(Settings):
            if field.name in intentionally_different:
                continue
            with self.subTest(field=field.name):
                self.assertEqual(
                    getattr(defaults, field.name),
                    getattr(packaged, field.name),
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
        with patch("omen_fanctl.cli.Path.cwd", return_value=Path("/logs")):
            self.assertEqual(
                _csv_log_path(parse_args([])),
                Path("/logs/omen-fanctl.csv"),
            )
        self.assertEqual(
            _csv_log_path(parse_args(["--log-file", "/tmp/custom.csv"])),
            Path("/tmp/custom.csv"),
        )
        self.assertIsNone(_csv_log_path(parse_args(["--no-log-file"])))

    def test_shipped_configuration_enables_the_temperature_trigger(self):
        settings = Settings.load(CONFIG_PATH)
        self.assertEqual(settings.critical_temp_c, 92.0)
        self.assertEqual(settings.critical_release_temp_c, 82.0)

    def test_shipped_configuration_hands_cool_fans_back_on_stop(self):
        settings = Settings.load(CONFIG_PATH)
        self.assertEqual(settings.stop_handoff_max_temp_c, 70.0)

    def test_upgraded_configuration_without_the_key_keeps_maximum_on_stop(self):
        # Upgrades preserve the installed file, so a missing key must not
        # change how a validated release answers a stop.
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves]
preset = "performance-extended"
""",
                encoding="utf-8",
            )

            self.assertIsNone(Settings.load(config).stop_handoff_max_temp_c)

    def test_stop_handoff_temperature_must_be_a_temperature_or_false(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]
stop_handoff_max_temp_c = true

[curves]
preset = "performance-extended"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "stop_handoff_max_temp_c must be a temperature or false",
            ):
                Settings.load(config)

    def test_stop_handoff_temperature_must_stay_below_critical_release(self):
        settings = replace(
            fixed_policy_settings(),
            stop_handoff_max_temp_c=82.0,
        )
        with self.assertRaisesRegex(
            ConfigurationError,
            "stop_handoff_max_temp_c must be below critical_release_temp_c",
        ):
            settings.validate()

    def test_stop_handoff_temperature_may_be_disabled(self):
        replace(fixed_policy_settings(), stop_handoff_max_temp_c=None).validate()

    def test_critical_temp_must_be_a_temperature_or_false(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]
critical_temp_c = true

[curves]
preset = "performance-extended"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "critical_temp_c must be a temperature or false to disable it",
            ):
                Settings.load(config)

    def test_disabled_trigger_drops_the_thresholds_that_depend_on_it(self):
        extended = extended_performance_curves()
        settings = replace(
            fixed_policy_settings(),
            critical_temp_c=None,
            activation_temp_c=95.0,
            release_temp_c=90.0,
            critical_release_temp_c=99.0,
            curve=extended["cpu"],
            curves=tuple(extended.items()),
        )
        # Ordering rules against critical_temp_c no longer apply, but the
        # remaining thresholds are still validated.
        settings.validate()

        with self.assertRaisesRegex(
            ConfigurationError, r"critical_release_temp_c must be in \(0, 125]"
        ):
            replace(settings, critical_release_temp_c=130.0).validate()

    def test_disabled_trigger_requires_curves_that_reach_full_speed(self):
        # The factory curve stops at ~78.3%, so with no temperature override
        # nothing could ever ask for full speed.
        factory = hp_factory_performance_curves()
        settings = replace(
            fixed_policy_settings(),
            critical_temp_c=None,
            curve=factory["cpu"],
            curves=tuple(factory.items()),
        )

        with self.assertRaises(ConfigurationError) as caught:
            settings.validate()

        self.assertEqual(
            str(caught.exception),
            "the cpu curve must reach 100% when critical_temp_c is disabled; "
            "select a 100% curve only when the detected fan interface safely "
            "supports full-speed Manual, otherwise set critical_temp_c to a "
            "temperature",
        )

    def test_disabled_trigger_checks_every_active_control_curve(self):
        curves = dict(extended_performance_curves())
        curves["gpu"] = hp_factory_performance_curves()["gpu"]
        settings = replace(
            fixed_policy_settings(),
            critical_temp_c=None,
            curve=curves["cpu"],
            curves=tuple(curves.items()),
        )

        with self.assertRaisesRegex(
            ConfigurationError, "the gpu curve must reach 100%"
        ):
            settings.validate()

        # A sensor that cannot drive control is not required to reach full speed.
        replace(settings, include_amd_gpu=False, include_nvidia_gpu=False).validate()

    def test_factory_preset_still_loads_with_a_temperature_trigger(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]
critical_temp_c = 92.0

[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
                encoding="utf-8",
            )
            settings = Settings.load(config)

        self.assertEqual(settings.critical_temp_c, 92.0)
        self.assertAlmostEqual(
            settings.curve_for("cpu").pwm_percent[-1], hp_level_percent(47)
        )

    def test_factory_preset_with_the_trigger_disabled_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]
critical_temp_c = false

[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "the cpu curve must reach 100% when critical_temp_c is disabled",
            ):
                Settings.load(config)

    def test_shipped_configuration_selects_the_single_channel_preset(self):
        settings = Settings.load(CONFIG_PATH)
        self.assertEqual(settings.curve_source, "performance-single-channel")
        self.assertEqual(
            set(dict(settings.curves or ())),
            {"cpu", "gpu", "ir"},
        )
        for sensor in ("cpu", "gpu", "ir"):
            with self.subTest(sensor=sensor):
                self.assertAlmostEqual(
                    settings.curve_for(sensor).pwm_percent[-1], hp_level_percent(56)
                )

    def test_loads_factory_preset(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves]
preset = "hp-vibrance-stx-n22x9-performance"
""",
                encoding="utf-8",
            )
            settings = Settings.load(config)

        self.assertEqual(settings.curve_source, "hp-vibrance-stx-n22x9-performance")
        self.assertEqual(
            set(dict(settings.curves or ())),
            {"cpu", "gpu", "ir"},
        )
        self.assertAlmostEqual(
            settings.curve_for("gpu").pwm_percent[-1], hp_level_percent(47)
        )

    def test_extended_preset_keeps_every_factory_step_below_its_headroom(self):
        factory = hp_factory_performance_curves()
        presets = {
            "single-channel": single_channel_performance_curves(),
            "dual-channel": extended_performance_curves(),
        }
        headroom_start = {"cpu": 86.0, "gpu": 82.0, "ir": 66.0}
        expected_top = {"single-channel": 56, "dual-channel": 60}
        for preset, curves in presets.items():
            for sensor, curve in curves.items():
                with self.subTest(preset=preset, sensor=sensor):
                    base = factory[sensor]
                    self.assertEqual(
                        curve.temperatures[: len(base.temperatures)], base.temperatures
                    )
                    self.assertEqual(
                        curve.pwm_percent[: len(base.pwm_percent)], base.pwm_percent
                    )
                    self.assertEqual(
                        len(curve.temperatures), len(base.temperatures) + 3
                    )
                    self.assertEqual(curve.temperatures[-3], headroom_start[sensor])
                    # Below the added steps the tables must be indistinguishable.
                    probe = headroom_start[sensor] - 0.5
                    self.assertAlmostEqual(
                        curve.evaluate_percent(probe), base.evaluate_percent(probe)
                    )
                    self.assertAlmostEqual(
                        curve.pwm_percent[-1], hp_level_percent(expected_top[preset])
                    )

    def test_single_channel_curve_uses_the_firmware_observed_cpu_ceiling(self):
        cpu = single_channel_performance_curves()["cpu"]
        self.assertAlmostEqual(cpu.evaluate_percent(89.9), hp_level_percent(55))
        self.assertAlmostEqual(cpu.evaluate_percent(90.0), hp_level_percent(56))

    def test_gpu_levels_follow_the_captured_firmware_table(self):
        expected = {
            19: 21,
            22: 23,
            31: 33,
            47: 49,
            51: 53,
            55: 57,
            56: 58,
            58: 58,
            60: 58,
        }
        for cpu_level, gpu_level in expected.items():
            with self.subTest(cpu_level=cpu_level):
                self.assertEqual(
                    hp_gpu_level_for_cpu_level(
                        cpu_level,
                        HP_8D87_CPU_GPU_LEVEL_TABLE,
                    ),
                    gpu_level,
                )

    def test_single_channel_preset_is_safe_with_one_pwm_channel(self):
        curves = single_channel_performance_curves()
        settings = replace(
            fixed_policy_settings(),
            curve=curves["cpu"],
            curves=tuple(curves.items()),
            curve_source="performance-single-channel",
        )
        settings.validate_fan_interface(independent_pwm_channels=False)

    def test_every_registered_preset_has_explicit_single_channel_compatibility(self):
        compatibility = {
            "hp-vibrance-stx-n22x9-performance": True,
            "performance-single-channel": True,
            "performance-extended": False,
        }
        self.assertEqual(set(CURVE_PRESETS), set(compatibility))

        for preset, curve_factory in CURVE_PRESETS.items():
            with self.subTest(preset=preset):
                curves = curve_factory()
                settings = replace(
                    fixed_policy_settings(),
                    curve=curves["cpu"],
                    curves=tuple(curves.items()),
                    curve_source=preset,
                )
                top_levels = {
                    percent_to_hp_level(settings.curve_for(sensor).pwm_percent[-1])
                    for sensor in settings.active_control_sensors()
                }
                if compatibility[preset]:
                    self.assertLessEqual(max(top_levels), 56)
                    settings.validate_fan_interface(independent_pwm_channels=False)
                else:
                    self.assertGreater(max(top_levels), 56)
                    with self.assertRaises(ConfigurationError):
                        settings.validate_fan_interface(independent_pwm_channels=False)

    def test_extended_cpu_curve_reaches_full_speed_at_ninety(self):
        cpu = extended_performance_curves()["cpu"]
        full_speed_at = min(
            temperature
            for temperature, percent in zip(cpu.temperatures, cpu.pwm_percent)
            if percent >= 100.0
        )
        self.assertEqual(full_speed_at, 90.0)
        self.assertAlmostEqual(cpu.evaluate_percent(89.9), hp_level_percent(55))
        self.assertAlmostEqual(cpu.evaluate_percent(90.0), 100.0)

    def test_extended_preset_requires_independent_pwm_channels(self):
        curves = extended_performance_curves()
        settings = replace(
            fixed_policy_settings(),
            curve=curves["cpu"],
            curves=tuple(curves.items()),
            curve_source="performance-extended",
        )

        with self.assertRaisesRegex(
            ConfigurationError,
            r"cpu curve maps to HP fan level 60.*only pwm1.*pwm2 is required",
        ):
            settings.validate_fan_interface(independent_pwm_channels=False)

        settings.validate_fan_interface(independent_pwm_channels=True)

    def test_factory_preset_is_safe_with_a_single_pwm_channel(self):
        settings = Settings.load(CONFIG_PATH)
        curves = hp_factory_performance_curves()
        settings = replace(
            settings,
            curve=curves["cpu"],
            curves=tuple(curves.items()),
            curve_source="hp-vibrance-stx-n22x9-performance",
        )
        settings.validate_fan_interface(independent_pwm_channels=False)

    def test_single_pwm_channel_rejects_an_unsafe_manual_floor(self):
        settings = replace(
            fixed_policy_settings(),
            minimum_manual_percent=hp_level_percent(57),
        )

        with self.assertRaisesRegex(
            ConfigurationError,
            r"minimum_manual_percent maps to HP fan level 57.*only pwm1",
        ):
            settings.validate_fan_interface(independent_pwm_channels=False)

    def test_unknown_preset_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves]
preset = "performance-unleashed"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError, "unknown curve preset: performance-unleashed"
            ):
                Settings.load(config)

    def test_rejects_preset_with_explicit_sensor_curve(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves]
preset = "performance-extended"

[curves.cpu]
temperature_c = [10, 5]
pwm_percent = [500, "oops"]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                r"curves\.preset and explicit curves\.<sensor> sections are "
                "mutually exclusive",
            ):
                Settings.load(config)

    def test_rejects_legacy_curve_with_per_sensor_curves(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curve]
temperature_c = [50, 60]
pwm_percent = [30, 40]

[curves.cpu]
temperature_c = [50, 60]
pwm_percent = [30, 40]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "configuration sections curve and curves are mutually exclusive",
            ):
                Settings.load(config)

    def test_rejects_empty_curves_section_with_its_own_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
            config.write_text(
                """
[daemon]
allowed_boards = ["8D87"]

[curves]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "curves must define either preset or curves.cpu",
            ):
                Settings.load(config)

    def test_rejects_both_names_for_a_curve_temperature_axis(self):
        cases = {
            "curve": """
[curve]
temperature_c = [50, 60]
high_temperature_c = [50, 60]
pwm_percent = [30, 40]
""",
            "curves.cpu": """
[curves.cpu]
temperature_c = [50, 60]
high_temperature_c = [50, 60]
pwm_percent = [30, 40]
""",
        }
        for path, curve in cases.items():
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary) / "omen-fanctl.toml"
                config.write_text(
                    f"""
[daemon]
allowed_boards = ["8D87"]
{curve}
""",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ConfigurationError,
                    f"{path} cannot define both high_temperature_c and temperature_c",
                ):
                    Settings.load(config)

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
                config = Path(temporary) / "omen-fanctl.toml"
                config.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(
                    ConfigurationError,
                    f"unknown configuration key: {unknown_key}",
                ):
                    Settings.load(config)

    def test_per_sensor_curves_require_cpu_curve(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "omen-fanctl.toml"
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
                    config = Path(temporary) / "omen-fanctl.toml"
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
            config = Path(temporary) / "omen-fanctl.toml"
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
                    "omen_fanctl.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "omen_fanctl.hardware.time.sleep",
                    side_effect=publish_profile,
                ) as sleep,
                patch("omen_fanctl.hardware.LOG.warning") as warning,
                patch("omen_fanctl.hardware.LOG.info") as info,
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


if __name__ == "__main__":
    unittest.main()
