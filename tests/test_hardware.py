"""Sensor discovery, platform-profile, and fan-hardware tests."""

import select
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from omen_fanctl.config import (
    PWM_MAX,
    ConfigurationError,
    Settings,
    hp_level_to_pwm,
)
from omen_fanctl.controller import (
    Controller,
    CsvLog,
)
from omen_fanctl.hardware import (
    AUTO_MODE,
    MANUAL_MODE,
    MAX_MODE,
    FailurePolicy,
    HardwareError,
    HardwareNotReadyError,
    HpFanHwmon,
    PlatformProfileMonitor,
    Sensors,
    SourceHealth,
    read_hp_wmi_ir_temperature,
    wait_for_hp_fan_hwmon,
    wait_for_temperature_sensors,
)

from tests import CONFIG_PATH
from tests.helpers import (
    FakeFan,
    fixed_policy_settings,
    initialized_sensors,
    initialized_sensors_with_amd_gpu,
    settings_with,
)


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
    return HpFanHwmon(root, board_name="8D87")


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
            patch("omen_fanctl.hardware.LOG.warning") as warning,
            patch("omen_fanctl.hardware.LOG.info") as info,
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
            patch("omen_fanctl.hardware.LOG.warning") as warning,
            patch("omen_fanctl.hardware.LOG.info") as info,
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
                "omen_fanctl.hardware.read_hwmon_temperatures",
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
            "omen_fanctl.hardware.read_hwmon_temperatures",
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
                "omen_fanctl.hardware.read_hwmon_temperatures",
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
                "omen_fanctl.hardware.read_hwmon_temperatures",
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
                "omen_fanctl.hardware.subprocess.run",
                return_value=nvidia,
            ),
            patch(
                "omen_fanctl.hardware.read_hwmon_temperatures",
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
            "omen_fanctl.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        available = SimpleNamespace(returncode=0, stdout="61, 80.0, 120.0\n")

        with (
            patch(
                "omen_fanctl.hardware.subprocess.run",
                side_effect=[
                    available,
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    subprocess.TimeoutExpired("nvidia-smi", 2.0),
                    available,
                ],
            ),
            patch("omen_fanctl.hardware.LOG.warning") as warning,
            patch("omen_fanctl.hardware.LOG.info") as info,
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
            "omen_fanctl.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        sensors.last_nvidia_metrics = (61.0, 80.0, 120.0)
        sensors.nvidia_gpu_health.available()
        self.assertEqual(len(sensors.nvidia_runtime_status_files), 1)
        sensors.nvidia_runtime_status_files[0].write_text("suspended\n")

        with patch("omen_fanctl.hardware.subprocess.run") as run:
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
            "omen_fanctl.hardware.shutil.which",
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
            "omen_fanctl.hardware.subprocess.run",
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
        # No reading and no cache to reuse, so staleness cannot report this.
        self.assertIsNone(failed_after_wake.nvidia_metrics_stale)
        self.assertFalse(failed_after_wake.has_cached_readings)
        self.assertEqual(
            failed_after_wake.degraded_sources,
            ("NVIDIA GPU temperature source",),
        )
        # The suspended sample is a powered-down GPU, not a failing one.
        self.assertEqual(suspended.degraded_sources, ())

    def test_nvidia_query_resumes_when_runtime_status_becomes_active(self):
        with patch(
            "omen_fanctl.hardware.shutil.which",
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
            "omen_fanctl.hardware.subprocess.run",
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
            "omen_fanctl.hardware.shutil.which",
            return_value="/usr/bin/nvidia-smi",
        ):
            sensors = initialized_sensors(self, include_nvidia_gpu=True)
        runtime_status = sensors.nvidia_runtime_status_files[0]
        runtime_status.write_text("suspended\n")
        sensors.nvidia_runtime_status_files = ()
        sensors.next_nvidia_pci_discovery = 130.0

        with (
            patch("omen_fanctl.hardware.time.monotonic", return_value=130.0),
            patch("omen_fanctl.hardware.subprocess.run") as run,
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
            "omen_fanctl.hardware.shutil.which",
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
            patch("omen_fanctl.hardware.time.monotonic", return_value=100.0),
            patch("omen_fanctl.hardware.subprocess.run") as run,
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
            "omen_fanctl.hardware.shutil.which",
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
            "omen_fanctl.hardware.subprocess.run",
            return_value=result,
        ) as run:
            snapshot = sensors.read()

        self.assertEqual(snapshot.gpu, 61.0)
        self.assertFalse(snapshot.nvidia_metrics_stale)
        self.assertIsNone(snapshot.nvidia_runtime_suspended)
        run.assert_called_once()

    def test_nvidia_failure_includes_stderr(self):
        with patch(
            "omen_fanctl.hardware.shutil.which",
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
            "omen_fanctl.hardware.subprocess.run",
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
            "omen_fanctl.hardware.shutil.which",
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
                "omen_fanctl.hardware.subprocess.run",
                side_effect=[available, OSError("driver disappeared")],
            ),
            patch("omen_fanctl.hardware.time.monotonic", return_value=100.0),
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
                "omen_fanctl.hardware.shutil.which",
                side_effect=[None, "/usr/bin/nvidia-smi"],
            ) as which,
            patch(
                "omen_fanctl.hardware.subprocess.run",
                return_value=result,
            ),
            patch(
                "omen_fanctl.hardware.time.monotonic",
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
            "omen_fanctl.hardware.find_nvidia_runtime_status_files"
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
            "omen_fanctl.hardware.read_hp_wmi_ir_temperature",
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
        with patch("omen_fanctl.hardware.subprocess.run", return_value=result):
            snapshot = sensors.read()
        self.assertEqual(snapshot.gpu, 72.0)
        self.assertEqual(snapshot.nvidia_power_draw_w, 174.5)
        self.assertEqual(snapshot.nvidia_power_limit_w, 175.0)

    def test_keeps_temperature_when_power_is_unavailable(self):
        sensors = initialized_sensors(self)
        sensors.nvidia_smi = "/usr/bin/nvidia-smi"
        result = SimpleNamespace(returncode=0, stdout="61, [N/A], [N/A]\n")
        with patch("omen_fanctl.hardware.subprocess.run", return_value=result):
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

            with patch("omen_fanctl.hardware.LOG.warning") as warning:
                snapshot = sensors.read()

        self.assertIsNone(snapshot.acpi)
        self.assertTrue(sensors.acpi_health.failed)
        warning.assert_called_once()


class PlatformProfileMonitorTests(unittest.TestCase):
    def test_sysfs_notification_refreshes_cached_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform_profile"
            path.write_text("balanced\n")
            poller = Mock()
            poller.poll.return_value = [(7, select.POLLPRI)]
            with patch("omen_fanctl.hardware.select.poll", return_value=poller):
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
            patch("omen_fanctl.hardware.select.poll"),
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

            fan = HpFanHwmon(root, board_name="8D87")
            fan.set_manual(178)
            self.assertEqual(int((hp / "pwm1").read_text()), 178)
            self.assertEqual(int((hp / "pwm1_enable").read_text()), MANUAL_MODE)
            fan.restore_auto()
            self.assertEqual(int((hp / "pwm1_enable").read_text()), AUTO_MODE)

    def test_dual_channel_manual_writes_both_pwm_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hp = root / "hwmon8"
            hp.mkdir()
            for name, value in (
                ("name", "hp\n"),
                ("pwm1", "100\n"),
                ("pwm2", "100\n"),
                ("pwm1_enable", f"{AUTO_MODE}\n"),
                ("fan1_input", "3400\n"),
                ("fan2_input", "3600\n"),
            ):
                (hp / name).write_text(value)

            fan = HpFanHwmon(root, board_name="8D87")
            self.assertTrue(fan.supports_independent_pwm)
            self.assertEqual(fan.pwm_abi, "dual")
            self.assertEqual(fan.manual_max_level, 60)
            fan.set_manual(hp_level_to_pwm(47))
            self.assertEqual(int((hp / "pwm1").read_text()), hp_level_to_pwm(47))
            self.assertEqual(int((hp / "pwm2").read_text()), hp_level_to_pwm(49))

            fan.update_manual(hp_level_to_pwm(60))
            self.assertEqual(int((hp / "pwm1").read_text()), hp_level_to_pwm(60))
            self.assertEqual(int((hp / "pwm2").read_text()), hp_level_to_pwm(58))

    def test_dual_channel_rejects_board_without_a_captured_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hp = root / "hwmon8"
            hp.mkdir()
            for name, value in (
                ("name", "hp\n"),
                ("pwm1", "100\n"),
                ("pwm2", "100\n"),
                ("pwm1_enable", f"{AUTO_MODE}\n"),
                ("fan1_input", "3400\n"),
                ("fan2_input", "3600\n"),
            ):
                (hp / name).write_text(value)

            with self.assertRaisesRegex(
                ConfigurationError,
                "no captured CPU/GPU mapping for board '8C99'",
            ):
                wait_for_hp_fan_hwmon("8C99", root=root)

    def test_unmapped_dual_channel_is_rejected_before_entering_manual_mode(self):
        fan = initialized_fan(self)
        (fan.path / "pwm2").write_text("100\n")
        fan = HpFanHwmon(fan.path.parent, board_name="8C99")

        with (
            patch("omen_fanctl.hardware.write_int") as write,
            self.assertRaisesRegex(
                ConfigurationError,
                "no captured CPU/GPU mapping for board '8C99'",
            ),
        ):
            fan.set_manual(100)

        write.assert_not_called()
        self.assertEqual(int(fan.enable.read_text()), AUTO_MODE)

    def test_single_channel_rejects_pwm_above_firmware_observed_cpu_maximum(self):
        fan = initialized_fan(self)
        self.assertEqual(fan.pwm_abi, "single")
        self.assertEqual(fan.manual_max_level, 56)

        with self.assertRaisesRegex(HardwareError, "safe maximum"):
            fan.set_manual(PWM_MAX)

        self.assertEqual(int(fan.enable.read_text()), AUTO_MODE)

    def test_failed_initial_pwm_write_rolls_manual_mode_back_to_auto(self):
        fan = initialized_fan(self)
        failure = HardwareError("PWM write failed")
        with patch(
            "omen_fanctl.hardware.write_int",
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

    def test_failed_second_pwm_write_rolls_dual_channel_back_to_auto(self):
        fan = initialized_fan(self)
        fan.pwm2 = fan.path / "pwm2"
        failure = HardwareError("pwm2 write failed")
        with patch(
            "omen_fanctl.hardware.write_int",
            side_effect=[None, None, failure, None],
        ) as write:
            with self.assertRaisesRegex(HardwareError, "pwm2 write failed"):
                fan.set_manual(hp_level_to_pwm(47))

        self.assertEqual(
            write.call_args_list,
            [
                call(fan.enable, MANUAL_MODE),
                call(fan.pwm, hp_level_to_pwm(47)),
                call(fan.pwm2, hp_level_to_pwm(49)),
                call(fan.enable, AUTO_MODE),
            ],
        )

    def test_failed_initial_pwm_write_reports_failed_auto_rollback(self):
        fan = initialized_fan(self)
        pwm_failure = HardwareError("PWM write failed")
        rollback_failure = HardwareError("Auto rollback failed")
        with (
            patch(
                "omen_fanctl.hardware.write_int",
                side_effect=[None, pwm_failure, rollback_failure],
            ) as write,
            patch("omen_fanctl.hardware.LOG.critical") as critical,
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
            "%s failed and Auto rollback also failed: %s",
            "initial manual PWM write",
            rollback_failure,
        )

    def test_failed_second_update_write_rolls_dual_channel_back_to_auto(self):
        fan = initialized_fan(self)
        fan.pwm2 = fan.path / "pwm2"
        failure = HardwareError("pwm2 update failed")
        with (
            patch("omen_fanctl.hardware.read_int", return_value=MANUAL_MODE),
            patch(
                "omen_fanctl.hardware.write_int",
                side_effect=[None, failure, None],
            ) as write,
            self.assertRaisesRegex(HardwareError, "pwm2 update failed"),
        ):
            fan.update_manual(hp_level_to_pwm(47))

        self.assertEqual(
            write.call_args_list,
            [
                call(fan.pwm, hp_level_to_pwm(47)),
                call(fan.pwm2, hp_level_to_pwm(49)),
                call(fan.enable, AUTO_MODE),
            ],
        )

    def test_failed_update_reports_failed_auto_rollback(self):
        fan = initialized_fan(self)
        fan.pwm2 = fan.path / "pwm2"
        pwm_failure = HardwareError("pwm2 update failed")
        rollback_failure = HardwareError("Auto rollback failed")
        with (
            patch("omen_fanctl.hardware.read_int", return_value=MANUAL_MODE),
            patch(
                "omen_fanctl.hardware.write_int",
                side_effect=[None, pwm_failure, rollback_failure],
            ),
            patch("omen_fanctl.hardware.LOG.critical") as critical,
            self.assertRaisesRegex(HardwareError, "pwm2 update failed"),
        ):
            fan.update_manual(hp_level_to_pwm(47))

        critical.assert_called_once_with(
            "%s failed and Auto rollback also failed: %s",
            "manual PWM update",
            rollback_failure,
        )

    def test_update_attempts_single_manual_mode_recovery(self):
        fan = initialized_fan(self)

        with (
            patch("omen_fanctl.hardware.read_int", return_value=AUTO_MODE),
            patch("omen_fanctl.hardware.write_int") as write,
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
            patch("omen_fanctl.hardware.read_int", return_value=AUTO_MODE),
            patch("omen_fanctl.hardware.write_int") as write,
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
            patch("omen_fanctl.hardware.read_int", return_value=MANUAL_MODE),
            patch("omen_fanctl.hardware.write_int") as write,
        ):
            fan.update_manual(120, write_pwm=False)

        write.assert_not_called()

    def test_update_preserves_externally_asserted_maximum_mode(self):
        fan = initialized_fan(self)

        with (
            patch("omen_fanctl.hardware.read_int", return_value=MAX_MODE),
            patch("omen_fanctl.hardware.write_int") as write,
        ):
            fan.update_manual(100)

        write.assert_not_called()

    def test_update_rejects_unknown_mode_without_writing(self):
        fan = initialized_fan(self)

        with (
            patch("omen_fanctl.hardware.read_int", return_value=3),
            patch("omen_fanctl.hardware.write_int") as write,
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
                    "omen_fanctl.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "omen_fanctl.hardware.time.sleep",
                    side_effect=publish_sensor,
                ) as sleep,
                patch("omen_fanctl.hardware.LOG.warning") as log_warning,
                patch("omen_fanctl.hardware.LOG.info") as log_info,
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
                    "omen_fanctl.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "omen_fanctl.hardware.time.sleep",
                    side_effect=publish_temperature,
                ) as sleep,
                patch("omen_fanctl.hardware.LOG.warning") as log_warning,
                patch("omen_fanctl.hardware.LOG.info") as log_info,
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
                "omen_fanctl.hardware.Sensors",
                side_effect=HardwareNotReadyError("not ready"),
            ),
            patch(
                "omen_fanctl.hardware.time.monotonic",
                side_effect=[100.0, 120.0],
            ),
            patch("omen_fanctl.hardware.time.sleep") as sleep,
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
                    "omen_fanctl.hardware.time.monotonic",
                    side_effect=[100.0, 100.0, 101.0],
                ),
                patch(
                    "omen_fanctl.hardware.time.sleep",
                    side_effect=publish_attributes,
                ) as sleep,
                patch("omen_fanctl.hardware.LOG.info") as log_info,
            ):
                fan = wait_for_hp_fan_hwmon("8D87", root=root)

            self.assertEqual(fan.path, hp)
            self.assertEqual(sleep.call_args_list, [call(1.0), call(1.0)])
            log_info.assert_called_once_with("hp hwmon interface became ready")

    def test_wait_detects_pwm2_published_after_required_attributes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hp = root / "hwmon7"
            hp.mkdir()
            for name, value in (
                ("name", "hp\n"),
                ("pwm1", "0\n"),
                ("pwm1_enable", f"{AUTO_MODE}\n"),
                ("fan1_input", "0\n"),
                ("fan2_input", "0\n"),
            ):
                (hp / name).write_text(value)

            def publish_pwm2(_delay):
                (hp / "pwm2").write_text("0\n")

            with (
                patch(
                    "omen_fanctl.hardware.time.monotonic",
                    side_effect=[100.0, 100.0],
                ),
                patch(
                    "omen_fanctl.hardware.time.sleep",
                    side_effect=publish_pwm2,
                ) as sleep,
            ):
                fan = wait_for_hp_fan_hwmon("8D87", root=root)

            self.assertTrue(fan.supports_independent_pwm)
            sleep.assert_called_once_with(1.0)

    def test_retries_transient_hp_hwmon_absence(self):
        fan = Mock(spec=HpFanHwmon)
        with (
            patch(
                "omen_fanctl.hardware.HpFanHwmon",
                side_effect=[HardwareNotReadyError("not ready"), fan],
            ) as constructor,
            patch("omen_fanctl.hardware.time.monotonic", side_effect=[100.0, 100.0]),
            patch("omen_fanctl.hardware.time.sleep") as sleep,
        ):
            self.assertIs(wait_for_hp_fan_hwmon("8D87"), fan)

        self.assertEqual(constructor.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_fails_after_hp_hwmon_startup_timeout(self):
        with (
            patch(
                "omen_fanctl.hardware.HpFanHwmon",
                side_effect=HardwareNotReadyError("not ready"),
            ),
            patch("omen_fanctl.hardware.time.monotonic", side_effect=[100.0, 120.0]),
            patch("omen_fanctl.hardware.time.sleep") as sleep,
            self.assertRaisesRegex(
                HardwareError,
                "did not become ready within 20 seconds",
            ),
        ):
            wait_for_hp_fan_hwmon("8D87")

        sleep.assert_not_called()

    def test_does_not_retry_non_transient_hwmon_error(self):
        with (
            patch(
                "omen_fanctl.hardware.HpFanHwmon",
                side_effect=HardwareError("multiple hp devices"),
            ),
            patch("omen_fanctl.hardware.time.sleep") as sleep,
            self.assertRaisesRegex(HardwareError, "multiple hp devices"),
        ):
            wait_for_hp_fan_hwmon("8D87")

        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
