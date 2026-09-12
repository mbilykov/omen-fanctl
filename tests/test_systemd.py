"""Systemd notification, unit, and log-rotation tests."""

import os
import unittest
from unittest.mock import Mock, patch

from omen_fanctl.cli import (
    CONFIGURATION_ERROR_EXIT_STATUS,
)
from omen_fanctl.controller import (
    SystemdNotifier,
)
from omen_fanctl.hardware import (
    HP_HWMON_STARTUP_TIMEOUT_S,
    K10TEMP_STARTUP_TIMEOUT_S,
    PLATFORM_PROFILE_STARTUP_TIMEOUT_S,
)

from tests import PROJECT_ROOT

SERVICE_PATH = PROJECT_ROOT / "src" / "systemd" / "omen-fanctl.service"
LOGROTATE_PATH = PROJECT_ROOT / "src" / "logrotate" / "omen-fanctl"


def parse_systemd_settings(unit: str) -> dict[str, str]:
    settings = {}
    for raw_line in unit.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key] = value
    return settings


class SystemdNotifierTests(unittest.TestCase):
    def test_abstract_notify_socket_is_supported(self):
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        with (
            patch.dict(
                "omen_fanctl.controller.os.environ",
                {
                    "NOTIFY_SOCKET": "@notify",
                    "WATCHDOG_PID": str(os.getpid()),
                    "WATCHDOG_USEC": "15000000",
                },
                clear=True,
            ),
            patch("omen_fanctl.controller.socket.socket", return_value=context),
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
                "omen_fanctl.controller.os.environ",
                {"NOTIFY_SOCKET": "/run/notify", "WATCHDOG_PID": "999999"},
                clear=True,
            ),
            patch("omen_fanctl.controller.socket.socket", return_value=context),
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
                "omen_fanctl.controller.socket.socket",
                side_effect=[OSError("temporary failure"), context],
            ) as socket_factory,
            patch("omen_fanctl.controller.LOG.warning") as warning,
            patch("omen_fanctl.controller.LOG.info") as info,
        ):
            notifier.ready()
            notifier.watchdog()

        self.assertEqual(socket_factory.call_count, 2)
        warning.assert_called_once()
        info.assert_called_once_with("systemd notification channel recovered")
        connection.sendto.assert_called_once_with(b"WATCHDOG=1", "/run/notify")
        self.assertFalse(notifier.failed)


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
            "/var/log/omen-fanctl/omen-fanctl.csv {\n",
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
