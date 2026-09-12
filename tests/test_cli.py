"""CLI locking, startup, recovery, and entry-point tests."""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

import omen_fanctl as omen_fanctl_package
from omen_fanctl.cli import (
    AUTO_GUARD_PATH,
    CONFIGURATION_ERROR_EXIT_STATUS,
    acquire_lock,
    clear_confirmed_board,
    clear_confirmed_board_if_safe,
    dry_run_lock_path,
    ensure_failsafe_fan_state,
    main,
    record_confirmed_board,
    recovery_allowed_boards,
    restore_firmware_auto,
    run_actuator_test,
    systemd_owns_runtime_directory,
)
from omen_fanctl.config import (
    DEFAULT_ALLOWED_BOARDS,
    PWM_MAX,
    ConfigurationError,
    Settings,
    extended_performance_curves,
    hp_level_percent,
    hp_level_to_pwm,
    load_allowed_boards,
    percent_to_pwm,
)
from omen_fanctl.controller import (
    Controller,
    CsvLog,
    SystemdNotifier,
)
from omen_fanctl.hardware import (
    AUTO_MODE,
    MANUAL_MODE,
    MAX_MODE,
    HardwareError,
    HpFanHwmon,
    Sensors,
)

from tests import CONFIG_PATH, DAEMON_PATH, PROJECT_ROOT
from tests.helpers import (
    FakeFan,
    FakeSensors,
    fixed_policy_settings,
    settings_with,
)

ENTRY_POINT_PATH = PROJECT_ROOT / "src" / "daemon" / "omen-fanctl"


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
            "omen_fanctl.cli.CONFIRMED_BOARD_PATH", self.confirmed_board_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)


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
                patch("omen_fanctl.cli.os.fdopen", side_effect=capture_handle),
                patch(
                    "omen_fanctl.cli.fcntl.flock",
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
                patch("omen_fanctl.cli.os.open", return_value=123),
                patch(
                    "omen_fanctl.cli.os.fstat",
                    return_value=SimpleNamespace(
                        st_mode=0o100600,
                        st_uid=os.geteuid(),
                    ),
                ),
                patch("omen_fanctl.cli.os.fchmod"),
                patch("omen_fanctl.cli.os.fdopen", return_value=handle),
                patch("omen_fanctl.cli.fcntl.flock"),
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
                        patch("omen_fanctl.cli.os.open", return_value=123),
                        patch("omen_fanctl.cli.os.fstat", return_value=metadata),
                        patch("omen_fanctl.cli.os.close") as close,
                        self.assertRaisesRegex(
                            HardwareError,
                            "lock must be a regular file owned by uid",
                        ),
                    ):
                        acquire_lock(lock)

                    close.assert_called_once_with(123)

    def test_dry_run_lock_is_scoped_to_effective_uid(self):
        with patch("omen_fanctl.cli.os.geteuid", return_value=1234):
            self.assertEqual(
                dry_run_lock_path(),
                Path("/tmp/omen-fanctl-dry-run-1234.lock"),
            )


class AllowedBoardRecoveryTests(RuntimeMarkerIsolation):
    def _safe_fan(self, mode=AUTO_MODE):
        fan = Mock(spec=HpFanHwmon)
        fan.path = Path("/sys/class/hwmon/hwmon7")
        fan.status.return_value = (mode, 0, 3000, 3000)
        return fan

    def _write_config(self, body):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = Path(directory) / "omen-fanctl.toml"
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
        path = Path(directory) / "omen-fanctl.toml"
        path.write_bytes(b'\xff\xfe[daemon]\nallowed_boards = ["8C99"]\n')
        self.assertEqual(load_allowed_boards(path), DEFAULT_ALLOWED_BOARDS)

    def test_recovery_allowlist_keeps_a_confirmed_board_after_config_damage(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        damaged = directory / "omen-fanctl.toml"
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
        damaged = directory / "omen-fanctl.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        fan = self._safe_fan()
        with (
            patch("omen_fanctl.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=Mock()),
            patch("omen_fanctl.cli.HpFanHwmon", return_value=fan),
            patch("omen_fanctl.cli.ensure_failsafe_fan_state") as failsafe,
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
            patch("omen_fanctl.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=Mock()),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon") as wait_fan,
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                return_value=Mock(spec=Sensors),
            ),
            patch("omen_fanctl.cli.CsvLog", return_value=Mock(spec=CsvLog)),
            patch(
                "omen_fanctl.cli.SystemdNotifier.from_environment",
                return_value=Mock(spec=SystemdNotifier),
            ),
            patch("omen_fanctl.cli.Controller", return_value=Mock(spec=Controller)),
            patch("omen_fanctl.cli.signal.signal"),
        ):
            fan = Mock(spec=HpFanHwmon)
            fan.path = Path("/sys/class/hwmon/hwmon7")

            def discover_fan(board_name):
                self.assertEqual(board_name, "8C99")
                observed.append(confirmed.read_text(encoding="ascii"))
                return fan

            wait_fan.side_effect = discover_fan
            with patch.dict(os.environ, {"RUNTIME_DIRECTORY": str(confirmed.parent)}):
                result = main(["--apply"])

        self.assertEqual(result, 0)
        self.assertEqual(observed, ["8C99\n"])

    def _apply_run_patches(self, confirmed, settings, extra=()):
        """Patch a full apply-mode startup down to a Mock controller."""
        wait_fan = patch("omen_fanctl.cli.wait_for_hp_fan_hwmon")
        fan = self._safe_fan()
        started = wait_fan.start()
        started.return_value = fan
        self.fan = fan
        self.addCleanup(wait_fan.stop)
        for target in (
            patch("omen_fanctl.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=Mock()),
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                return_value=Mock(spec=Sensors),
            ),
            patch("omen_fanctl.cli.CsvLog", return_value=Mock(spec=CsvLog)),
            patch(
                "omen_fanctl.cli.SystemdNotifier.from_environment",
                return_value=Mock(spec=SystemdNotifier),
            ),
            patch("omen_fanctl.cli.signal.signal"),
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
            extra=(patch("omen_fanctl.cli.Controller", return_value=controller),),
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
            extra=(patch("omen_fanctl.cli.Controller", return_value=controller),),
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
            extra=(patch("omen_fanctl.cli.run_actuator_test"),),
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
                    "omen_fanctl.cli.Controller",
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
        damaged = directory / "omen-fanctl.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        with (
            patch("omen_fanctl.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=Mock()),
            patch("omen_fanctl.cli.HpFanHwmon", return_value=self._safe_fan()),
            patch("omen_fanctl.cli.ensure_failsafe_fan_state"),
        ):
            result = main(["--config", str(damaged), "--failsafe"])

        self.assertEqual(result, 0)
        self.assertFalse(confirmed.exists())

    def test_rejected_recovery_keeps_the_marker(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        damaged = directory / "omen-fanctl.toml"
        damaged.write_bytes(b"\xff\xfe")
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        with (
            patch("omen_fanctl.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("omen_fanctl.cli.read_text", return_value="8DFF"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.ensure_failsafe_fan_state") as failsafe,
            self.assertLogs("omen-fanctl", level="ERROR"),
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
            extra=(patch("omen_fanctl.cli.Controller", return_value=controller),),
        )
        # The controller stop path only logs when it cannot select maximum fans.
        self.fan.status.return_value = (MANUAL_MODE, 128, 4200, 4400)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            with self.assertLogs("omen-fanctl", level="ERROR") as logs:
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
                    "omen_fanctl.cli.run_actuator_test",
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
                    "omen_fanctl.cli.Controller",
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
        with self.assertLogs("omen-fanctl", level="ERROR") as logs:
            clear_confirmed_board_if_safe(confirmed, fan)

        self.assertTrue(confirmed.exists())
        self.assertIn("keeping the confirmed board marker", "\n".join(logs.output))

    def test_marker_cleanup_can_discover_the_fan_for_a_mode_read(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        fan = Mock(spec=HpFanHwmon)
        fan.status.return_value = (AUTO_MODE, 100, 2400, 2600)

        with patch("omen_fanctl.cli.HpFanHwmon", return_value=fan) as fan_type:
            clear_confirmed_board_if_safe(confirmed, None)

        fan_type.assert_called_once_with()
        self.assertFalse(confirmed.exists())

    def test_cleanup_never_raises_out_of_the_shutdown_path(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        record_confirmed_board("8C99", confirmed)
        fan = Mock(spec=HpFanHwmon)
        fan.status.side_effect = RuntimeError("unexpected")
        with self.assertLogs("omen-fanctl", level="ERROR"):
            clear_confirmed_board_if_safe(confirmed, fan)

        self.assertTrue(confirmed.exists())

    def test_runtime_directory_identifies_only_the_managed_directory(self):
        marker = Path("/run/omen-fanctl/board")
        cases = {
            "": False,
            "/run/other-unit": False,
            "/run/omen-fanctl-backup": False,
            "/run/omen-fanctl": True,
            "/run/other-unit:/run/omen-fanctl": True,
        }
        for value, expected in cases.items():
            with self.subTest(runtime_directory=value):
                with patch.dict(os.environ, {"RUNTIME_DIRECTORY": value}):
                    self.assertEqual(systemd_owns_runtime_directory(marker), expected)

    def test_inherited_invocation_id_does_not_claim_ownership(self):
        marker = Path("/run/omen-fanctl/board")
        with patch.dict(os.environ, {"INVOCATION_ID": "b3f0"}):
            os.environ.pop("RUNTIME_DIRECTORY", None)
            self.assertFalse(systemd_owns_runtime_directory(marker))

    def test_clear_confirmed_board_reports_rather_than_raises(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        clear_confirmed_board(directory / "absent")

        with (
            patch("pathlib.Path.unlink", side_effect=OSError("read-only file system")),
            self.assertLogs("omen-fanctl", level="ERROR") as logs,
        ):
            clear_confirmed_board(directory / "board")

        self.assertIn("cannot clear the confirmed board marker", "\n".join(logs.output))

    def test_dry_run_does_not_record_a_confirmed_board(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        confirmed = directory / "board"
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        with (
            patch("omen_fanctl.cli.CONFIRMED_BOARD_PATH", confirmed),
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.acquire_lock", return_value=Mock()),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon") as wait_fan,
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                return_value=Mock(spec=Sensors),
            ),
            patch("omen_fanctl.cli.CsvLog", return_value=Mock(spec=CsvLog)),
            patch(
                "omen_fanctl.cli.SystemdNotifier.from_environment",
                return_value=Mock(spec=SystemdNotifier),
            ),
            patch("omen_fanctl.cli.Controller", return_value=Mock(spec=Controller)),
            patch("omen_fanctl.cli.signal.signal"),
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
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=Mock()),
            patch("omen_fanctl.cli.HpFanHwmon", return_value=fan),
            patch("omen_fanctl.cli.ensure_failsafe_fan_state") as failsafe,
        ):
            result = main(["--config", str(path), "--failsafe"])

        self.assertEqual(result, 0)
        failsafe.assert_called_once_with(fan, AUTO_GUARD_PATH)

    def test_failsafe_rejects_a_board_outside_the_allowlist(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        with (
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.ensure_failsafe_fan_state") as failsafe,
            self.assertLogs("omen-fanctl", level="ERROR") as logs,
        ):
            result = main(["--config", str(path), "--failsafe"])

        self.assertEqual(result, 1)
        self.assertIn("fan recovery is only allowed", "\n".join(logs.output))
        failsafe.assert_not_called()

    def test_restore_auto_rejects_a_board_outside_the_allowlist(self):
        path = self._write_config('[daemon]\nallowed_boards = ["8C99"]\n')
        with (
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.restore_firmware_auto") as restore,
            self.assertLogs("omen-fanctl", level="ERROR") as logs,
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
        fan.supports_independent_pwm = False
        fan.pwm_abi = "single"
        fan.manual_max_level = 56
        fan.manual_pwm_max = hp_level_to_pwm(fan.manual_max_level)
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
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch(
                "omen_fanctl.cli.CONFIRMED_BOARD_PATH",
                self._confirmed_board_path(),
            ),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon", return_value=fan),
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                return_value=sensors,
            ),
            patch("omen_fanctl.cli.CsvLog", return_value=csv_log) as csv_type,
            patch("omen_fanctl.cli.LOG.warning") as startup_log,
            patch(
                "omen_fanctl.cli.SystemdNotifier.from_environment",
                return_value=notifier,
            ),
            patch("omen_fanctl.cli.Controller", return_value=controller) as factory,
            patch("omen_fanctl.cli.signal.signal") as install_signal,
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
        startup_log.assert_called_once_with(
            "%s mode; board=%s curves=%s pwm=%s manual_max=level%s hp_hwmon=%s log=%s",
            "APPLY",
            "8D87",
            settings.curve_source,
            "single",
            56,
            fan.path,
            log_path,
        )
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

    def test_main_rejects_extended_curve_on_single_channel_hwmon(self):
        curves = extended_performance_curves()
        settings = replace(
            fixed_policy_settings(),
            curve=curves["cpu"],
            curves=tuple(curves.items()),
            curve_source="performance-extended",
        )
        fan = self._safe_fan()
        fan.supports_independent_pwm = False
        wait_for_sensors = Mock()
        lock = Mock()
        with (
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon", return_value=fan),
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                wait_for_sensors,
            ),
            patch("omen_fanctl.cli.LOG.error") as log_error,
        ):
            result = main(["--no-log-file"])

        self.assertEqual(result, CONFIGURATION_ERROR_EXIT_STATUS)
        self.assertIn("only pwm1", str(log_error.call_args.args[1]))
        wait_for_sensors.assert_not_called()
        lock.close.assert_called_once_with()

    def test_main_does_not_restart_an_unmapped_dual_channel_board(self):
        settings = replace(fixed_policy_settings(), allowed_boards=("8C99",))
        failure = ConfigurationError(
            "dual-channel Manual control has no captured CPU/GPU mapping "
            "for board '8C99'"
        )
        wait_for_sensors = Mock()
        lock = Mock()
        with (
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8C99"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch(
                "omen_fanctl.cli.wait_for_hp_fan_hwmon",
                side_effect=failure,
            ),
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                wait_for_sensors,
            ),
            patch("omen_fanctl.cli.LOG.error") as log_error,
        ):
            result = main(["--no-log-file"])

        self.assertEqual(result, CONFIGURATION_ERROR_EXIT_STATUS)
        self.assertEqual(log_error.call_args.args[1], failure)
        wait_for_sensors.assert_not_called()
        lock.close.assert_called_once_with()

    def test_main_dispatches_actuator_test_without_constructing_controller(self):
        settings = fixed_policy_settings()
        lock = Mock()
        fan = self._safe_fan()
        sensors = Mock(spec=Sensors)
        with (
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch(
                "omen_fanctl.cli.CONFIRMED_BOARD_PATH",
                self._confirmed_board_path(),
            ),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon", return_value=fan),
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                return_value=sensors,
            ),
            patch("omen_fanctl.cli.run_actuator_test") as actuator_test,
            patch("omen_fanctl.cli.Controller") as controller_type,
            patch("omen_fanctl.cli.CsvLog") as csv_type,
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
                    patch("omen_fanctl.cli.Settings.load", return_value=base),
                    patch("omen_fanctl.cli.read_text", return_value="8D87"),
                    patch("omen_fanctl.cli.validate_required_profile"),
                    patch("omen_fanctl.cli.dry_run_lock_path"),
                    patch("omen_fanctl.cli.acquire_lock", return_value=lock),
                    patch("omen_fanctl.cli.wait_for_hp_fan_hwmon"),
                    patch(
                        "omen_fanctl.cli.wait_for_temperature_sensors",
                        wait_for_sensors,
                    ),
                    patch("omen_fanctl.cli.LOG.error"),
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
                        "omen_fanctl.cli.Settings.load",
                        return_value=settings,
                    ),
                    patch("omen_fanctl.cli.read_text", return_value="8D87"),
                    patch("omen_fanctl.cli.validate_required_profile"),
                    patch(
                        "omen_fanctl.cli.os.geteuid",
                        return_value=effective_uid,
                    ),
                    patch("omen_fanctl.cli.acquire_lock", acquire),
                    patch("omen_fanctl.cli.LOG.error"),
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
            patch("omen_fanctl.cli.read_text", side_effect=fake_read_text),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch(
                "omen_fanctl.cli.wait_for_hp_fan_hwmon",
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
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.validate_required_profile"),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon"),
            patch(
                "omen_fanctl.cli.wait_for_temperature_sensors",
                side_effect=HardwareError("k10temp startup timeout"),
            ),
        ):
            self.assertEqual(
                main(["--config", str(CONFIG_PATH), "--no-log-file"]),
                1,
            )

        lock.close.assert_called_once_with()

    def test_main_preserves_successful_system_exit_without_explicit_code(self):
        with patch("omen_fanctl.cli.parse_args", side_effect=SystemExit(None)):
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
            patch("omen_fanctl.cli.Settings.load", return_value=settings),
            patch("omen_fanctl.cli.read_text", side_effect=fake_read_text),
            patch(
                "omen_fanctl.cli.validate_required_profile",
                side_effect=HardwareError("required platform profile startup timeout"),
            ),
            patch("omen_fanctl.cli.acquire_lock", acquire),
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
            patch("omen_fanctl.cli.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
            patch("omen_fanctl.cli.time.sleep"),
            patch("omen_fanctl.cli.signal.signal"),
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

    def test_actuator_test_respects_single_channel_manual_maximum(self):
        fan = FakeFan()
        fan.manual_pwm_max = percent_to_pwm(hp_level_percent(56))

        with self.assertRaisesRegex(
            ConfigurationError,
            "safe Manual maximum",
        ):
            run_actuator_test(fan, FakeSensors(50), 100.0, 1)

        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertEqual(fan.actions, [])

    def test_actuator_test_does_not_reduce_high_firmware_auto_pwm(self):
        fan = FakeFan()
        fan.pwm = PWM_MAX
        fan.manual_pwm_max = percent_to_pwm(hp_level_percent(56))

        with self.assertRaisesRegex(HardwareError, "refusing to reduce airflow"):
            run_actuator_test(fan, FakeSensors(50), 60.0, 1)

        self.assertEqual(fan.mode, AUTO_MODE)
        self.assertEqual(fan.actions, [])

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
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch("omen_fanctl.cli.HpFanHwmon", return_value=fan),
            patch("omen_fanctl.cli.Settings.load") as load_settings,
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
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch("omen_fanctl.cli.HpFanHwmon", return_value=fan),
            patch("omen_fanctl.cli.wait_for_hp_fan_hwmon") as wait_for_hwmon,
            patch("omen_fanctl.cli.AUTO_GUARD_PATH", guard),
            patch("omen_fanctl.cli.Settings.load") as load_settings,
        ):
            self.assertEqual(main(["--failsafe"]), 0)
        load_settings.assert_not_called()
        wait_for_hwmon.assert_not_called()
        lock.close.assert_called_once_with()
        self.assertEqual(fan.mode, MAX_MODE)

    def test_failsafe_closes_lock_when_hwmon_initialization_fails(self):
        lock = Mock()
        with (
            patch("omen_fanctl.cli.read_text", return_value="8D87"),
            patch("omen_fanctl.cli.os.geteuid", return_value=0),
            patch("omen_fanctl.cli.acquire_lock", return_value=lock),
            patch(
                "omen_fanctl.cli.HpFanHwmon",
                side_effect=HardwareError("hp hwmon unavailable"),
            ),
        ):
            self.assertEqual(main(["--failsafe"]), 1)

        lock.close.assert_called_once_with()


class EntryPointTests(unittest.TestCase):
    def test_package_all_matches_every_public_binding(self):
        exports = omen_fanctl_package.__all__
        public_bindings = {
            name
            for name, value in vars(omen_fanctl_package).items()
            if not name.startswith("_") and not isinstance(value, ModuleType)
        }

        self.assertEqual(len(exports), len(set(exports)))
        self.assertEqual(set(exports), public_bindings)

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
            [sys.executable, "-m", "omen_fanctl", "--help"],
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


if __name__ == "__main__":
    unittest.main()
