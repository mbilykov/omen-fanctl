"""Command-line entry point, recovery commands, and process locking."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import stat
import time
from collections.abc import Iterable
from dataclasses import replace
from io import TextIOWrapper
from pathlib import Path

from .config import (
    ConfigurationError,
    Settings,
    hp_level_percent,
    load_allowed_boards,
    percent_to_pwm,
    pwm_to_percent,
)
from .controller import Controller, CsvLog, SystemdNotifier
from .hardware import (
    AUTO_MODE,
    MAX_MODE,
    HardwareError,
    HpFanHwmon,
    Sensors,
    read_text,
    validate_required_profile,
    wait_for_hp_fan_hwmon,
    wait_for_temperature_sensors,
)


LOG = logging.getLogger("omen-fanctl")
AUTO_GUARD_PATH = Path("/run/omen-fanctl/auto-guard")
CONFIRMED_BOARD_PATH = Path("/run/omen-fanctl/board")
CONFIGURATION_ERROR_EXIT_STATUS = 78


def acquire_lock(path: Path) -> TextIOWrapper:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise HardwareError(f"cannot open lock {path}: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise HardwareError(
                f"lock must be a regular file owned by uid {os.geteuid()}: {path}"
            )
        os.fchmod(descriptor, 0o600)
        handle: TextIOWrapper = os.fdopen(descriptor, "r+", encoding="ascii")
    except Exception:
        os.close(descriptor)
        raise

    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise HardwareError(f"another controller holds {path}") from exc
    except OSError as exc:
        handle.close()
        raise HardwareError(f"cannot acquire lock {path}: {exc}") from exc

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except Exception:
        handle.close()
        raise
    return handle


def dry_run_lock_path() -> Path:
    return Path("/tmp") / f"omen-fanctl-dry-run-{os.geteuid()}.lock"


def _csv_log_path(args: argparse.Namespace) -> Path | None:
    if args.no_log_file:
        return None
    if args.log_file:
        return args.log_file
    return Path.cwd() / "omen-fanctl.csv"


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Experimental standalone automatic fan controller for HP hp-wmi "
            "boards listed in allowed_boards"
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/omen-fanctl/omen-fanctl.toml"),
        help="configuration file",
    )
    operation_group = parser.add_mutually_exclusive_group()
    operation_group.add_argument(
        "--apply",
        action="store_true",
        help="actually write fan controls (default is dry-run)",
    )
    operation_group.add_argument(
        "--restore-auto",
        action="store_true",
        help="force firmware Auto and exit (unsafe while the machine is hot)",
    )
    operation_group.add_argument(
        "--failsafe",
        action="store_true",
        help="preserve Auto or replace a userspace-owned mode with maximum fans",
    )
    parser.add_argument(
        "--actuator-test",
        type=float,
        metavar="PERCENT",
        help="hold a fixed PWM for a short hardware test, then restore Auto",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many seconds; hot Manual/Max exits fail safe to Max",
    )
    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "--log-file",
        type=Path,
        help="CSV output path (default: omen-fanctl.csv in the current directory)",
    )
    log_group.add_argument(
        "--no-log-file", action="store_true", help="disable CSV output"
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=1.0,
        help="seconds between periodic console/journal status lines",
    )
    sensor_group = parser.add_mutually_exclusive_group()
    sensor_group.add_argument(
        "--cpu-only",
        action="store_true",
        help="ignore GPU, WMI IR, and ACPI sensors (controlled CPU test mode)",
    )
    sensor_group.add_argument(
        "--include-acpi-proxy",
        action="store_true",
        help="log and compare acpitz as a diagnostic proxy; never control fans",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.actuator_test is not None and (args.restore_auto or args.failsafe):
        parser.error("--actuator-test cannot be combined with recovery operations")
    return args


def run_actuator_test(
    fan: HpFanHwmon,
    sensors: Sensors,
    percent: float,
    duration_s: float,
    minimum_percent: float = hp_level_percent(19),
) -> None:
    if not minimum_percent <= percent <= 100.0:
        raise ConfigurationError(
            f"--actuator-test must be between {minimum_percent:g} and 100 percent"
        )
    requested_pwm = percent_to_pwm(percent)
    if requested_pwm > fan.manual_pwm_max:
        raise ConfigurationError(
            f"--actuator-test maps above this hp-wmi interface's safe Manual "
            f"maximum of {pwm_to_percent(fan.manual_pwm_max):.1f} percent"
        )
    if not 1.0 <= duration_s <= 60.0:
        raise ConfigurationError(
            "actuator-test duration must be between 1 and 60 seconds"
        )
    mode, current_pwm, fan1, fan2 = fan.status()
    if mode != AUTO_MODE:
        raise HardwareError(
            f"actuator test requires firmware Auto (pwm1_enable=2), found {mode}"
        )
    if current_pwm > fan.manual_pwm_max:
        raise HardwareError(
            f"firmware Auto PWM {current_pwm} is above this interface's safe "
            f"Manual maximum {fan.manual_pwm_max}; refusing to reduce airflow"
        )

    # Do not reduce airflow when entering the test.
    target = max(requested_pwm, current_pwm)
    stop = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop
        LOG.info("received signal %s", signum)
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    LOG.warning(
        "actuator test: current=%d (%.1f%%), requested=%d (%.1f%%), fans=%d/%d",
        current_pwm,
        pwm_to_percent(current_pwm),
        target,
        pwm_to_percent(target),
        fan1,
        fan2,
    )
    started = time.monotonic()
    try:
        fan.set_manual(target)
        while not stop and time.monotonic() - started < duration_s:
            snapshot = sensors.read()
            mode, actual_pwm, fan1, fan2 = fan.status()
            LOG.info(
                "test mode=%d pwm=%d (%.1f%%) fans=%d/%d CPU=%.1f GPU=%s IR=%s ACPI=%s",
                mode,
                actual_pwm,
                pwm_to_percent(actual_pwm),
                fan1,
                fan2,
                snapshot.cpu,
                "n/a" if snapshot.gpu is None else f"{snapshot.gpu:.1f}",
                "n/a" if snapshot.ir is None else f"{snapshot.ir:.1f}",
                "n/a" if snapshot.acpi is None else f"{snapshot.acpi:.1f}",
            )
            time.sleep(1.0)
    finally:
        LOG.info("actuator test finished; restoring firmware Auto")
        fan.restore_auto()
        restored_mode, _, restored_fan1, restored_fan2 = fan.status()
        if restored_mode != AUTO_MODE:
            raise HardwareError(
                f"failed to verify firmware Auto after test: mode={restored_mode}"
            )
        LOG.info("firmware Auto restored; fans=%d/%d", restored_fan1, restored_fan2)


def restore_firmware_auto(fan: HpFanHwmon) -> None:
    mode, _, _, _ = fan.status()
    if mode != AUTO_MODE:
        fan.restore_auto()
    restored_mode, _, fan1, fan2 = fan.status()
    if restored_mode != AUTO_MODE:
        raise HardwareError(f"failed to verify firmware Auto: mode={restored_mode}")
    LOG.info("firmware Auto verified; fans=%d/%d", fan1, fan2)


def ensure_failsafe_fan_state(
    fan: HpFanHwmon,
    auto_guard_path: Path | None = None,
) -> None:
    """Preserve stable Auto; turn owned or guarded fan states into Max."""
    mode, _, _, _ = fan.status()
    guarded_auto = auto_guard_path is not None and auto_guard_path.exists()
    if mode != AUTO_MODE or guarded_auto:
        fan.set_maximum()
        if auto_guard_path is not None:
            try:
                auto_guard_path.unlink(missing_ok=True)
            except OSError as exc:
                raise HardwareError(
                    f"cannot clear firmware Auto guard during recovery: {exc}"
                ) from exc
    safe_mode, _, fan1, fan2 = fan.status()
    if safe_mode not in (AUTO_MODE, MAX_MODE):
        raise HardwareError(
            f"failed to establish Auto or maximum fail-safe: mode={safe_mode}"
        )
    state = "firmware Auto" if safe_mode == AUTO_MODE else "maximum fail-safe"
    LOG.info("%s verified; fans=%d/%d", state, fan1, fan2)


def record_confirmed_board(board: str, path: Path) -> None:
    """Persist the board this process validated against a readable config.

    Recovery must not depend on the configuration still being parsable. A file
    damaged while the service owns the fans would otherwise narrow the
    allowlist back to the built-in board and reject the ExecStopPost
    fail-safe.

    The marker must not outlive fan ownership. Under systemd that is handled by
    the managed runtime directory; a manual run owns no such directory and
    clears the marker itself through ``clear_confirmed_board``.
    """
    try:
        path.write_text(f"{board}\n", encoding="ascii")
    except OSError as exc:
        raise HardwareError(f"cannot persist the confirmed board: {exc}") from exc


def clear_confirmed_board(path: Path) -> None:
    """Drop the marker written by a run that systemd does not clean up after.

    This runs while the controller lock is still held, and reports rather than
    raises so it cannot mask the error that ended the run.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOG.error("cannot clear the confirmed board marker %s: %s", path, exc)


def clear_confirmed_board_if_safe(path: Path, fan: HpFanHwmon | None) -> None:
    """Drop the marker only once the fans are back in a safe firmware state.

    Software control can survive a failed shutdown: an actuator test may fail
    to restore Auto, and the controller's stop path only logs when it cannot
    select maximum fans. Retaining the marker is the safe failure direction,
    because dropping it would block the very recovery that has to clean up.
    """
    try:
        if fan is None:
            fan = HpFanHwmon()
        mode, _, _, _ = fan.status()
    except Exception as exc:
        # Best-effort cleanup runs while an earlier failure is being handled and
        # must never replace it. Any unreadable state keeps the marker.
        LOG.error("keeping the confirmed board marker %s: %s", path, exc)
        return
    if mode not in (AUTO_MODE, MAX_MODE):
        LOG.error(
            "keeping the confirmed board marker %s: fan mode %s is not safe",
            path,
            mode,
        )
        return
    clear_confirmed_board(path)


def systemd_owns_runtime_directory(path: Path) -> bool:
    """True when systemd created and will remove the directory holding *path*.

    ``INVOCATION_ID`` is unusable here: it is set for every unit and is
    inherited by anything started from one, including an interactive shell.
    ``RUNTIME_DIRECTORY`` is set only by ``RuntimeDirectory=`` and names the
    exact directories systemd will remove when the unit stops.
    """
    directories = os.environ.get("RUNTIME_DIRECTORY", "")
    return any(
        directory and Path(directory) == path.parent
        for directory in directories.split(":")
    )


def recovery_allowed_boards(
    config_path: Path,
    confirmed_path: Path,
) -> tuple[str, ...]:
    """Boards the recovery commands may act on.

    The configured list is extended by the board a running service confirmed at
    startup. Only root can write the runtime marker, and it is present only
    while a process may still own the fans.
    """
    boards = load_allowed_boards(config_path)
    try:
        confirmed = confirmed_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return boards
    if not confirmed or confirmed in boards:
        return boards
    return boards + (confirmed,)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return (
            0 if exc.code is None or exc.code == 0 else CONFIGURATION_ERROR_EXIT_STATUS
        )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    lock_handle: TextIOWrapper | None = None
    owned_board_marker: Path | None = None
    fan: HpFanHwmon | None = None
    try:
        if args.restore_auto or args.failsafe:
            board = read_text(Path("/sys/class/dmi/id/board_name"))
            allowed = recovery_allowed_boards(args.config, CONFIRMED_BOARD_PATH)
            if board not in allowed:
                raise HardwareError(
                    f"fan recovery is only allowed on boards {allowed}, found {board!r}"
                )
            if os.geteuid() != 0:
                raise HardwareError("fan-control writes must be run as root (use sudo)")
            lock_handle = acquire_lock(Path("/run/omen-fanctl/control.lock"))
            # Recovery commands deliberately do not wait for hwmon: --failsafe
            # runs from ExecStopPost and must never delay service shutdown.
            fan = HpFanHwmon(board_name=board)
            if args.failsafe:
                ensure_failsafe_fan_state(fan, AUTO_GUARD_PATH)
            else:
                restore_firmware_auto(fan)
            # The controller lock is held, so no process can still own the fans:
            # the marker has done its job and a restarted service rewrites it.
            clear_confirmed_board_if_safe(CONFIRMED_BOARD_PATH, fan)
            return 0

        settings = Settings.load(args.config)
        if args.cpu_only:
            settings = replace(
                settings,
                include_acpi=False,
                include_amd_gpu=False,
                include_nvidia_gpu=False,
                include_hp_wmi_ir=False,
            )
        elif args.include_acpi_proxy:
            settings = replace(settings, include_acpi=True)
        board = read_text(Path("/sys/class/dmi/id/board_name"))
        if board not in settings.allowed_boards:
            raise ConfigurationError(
                f"board {board!r} is not allowlisted: {settings.allowed_boards}"
            )
        validate_required_profile(settings.required_profile)
        if args.apply and os.geteuid() != 0:
            raise HardwareError("fan-control writes must be run as root (use sudo)")
        if args.duration is not None and args.duration <= 0:
            raise ConfigurationError("--duration must be positive")
        if args.status_interval < settings.sample_interval_s:
            raise ConfigurationError(
                "--status-interval must be >= daemon sample_interval_s"
            )

        lock_path = Path("/run/omen-fanctl/control.lock")
        if not args.apply:
            lock_path = dry_run_lock_path()
        # Keep the file object alive for the lifetime of main; closing it releases
        # the advisory lock.
        lock_handle = acquire_lock(lock_path)
        if args.apply:
            record_confirmed_board(board, CONFIRMED_BOARD_PATH)
            # systemd discards RuntimeDirectory when the service stops. A manual
            # run has no such owner, so it must take the marker back down itself
            # rather than leave the recovery allowlist widened until reboot.
            if not systemd_owns_runtime_directory(CONFIRMED_BOARD_PATH):
                owned_board_marker = CONFIRMED_BOARD_PATH

        fan = wait_for_hp_fan_hwmon(board)
        settings.validate_fan_interface(
            independent_pwm_channels=fan.supports_independent_pwm
        )
        sensors = wait_for_temperature_sensors(settings)
        if args.actuator_test is not None:
            if not args.apply:
                raise ConfigurationError("--actuator-test also requires --apply")
            test_duration = 15.0 if args.duration is None else args.duration
            run_actuator_test(
                fan,
                sensors,
                args.actuator_test,
                test_duration,
                settings.minimum_manual_percent,
            )
            return 0
        log_path = _csv_log_path(args)
        csv_log = CsvLog(log_path)

        LOG.warning(
            "%s mode; board=%s curves=%s pwm=%s manual_max=level%s hp_hwmon=%s log=%s",
            "APPLY" if args.apply else "DRY-RUN",
            board,
            settings.curve_source,
            fan.pwm_abi,
            fan.manual_max_level,
            fan.path,
            log_path or "disabled",
        )
        controller = Controller(
            settings=settings,
            fan=fan,
            sensors=sensors,
            apply=args.apply,
            duration_s=args.duration,
            csv_log=csv_log,
            status_interval_s=args.status_interval,
            notifier=SystemdNotifier.from_environment(),
            auto_guard_path=AUTO_GUARD_PATH if args.apply else None,
        )
        signal.signal(signal.SIGINT, controller.request_stop)
        signal.signal(signal.SIGTERM, controller.request_stop)
        try:
            controller.run()
        finally:
            csv_log.close()
        return 0
    except ConfigurationError as exc:
        LOG.error("%s", exc)
        return CONFIGURATION_ERROR_EXIT_STATUS
    except (HardwareError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    finally:
        if owned_board_marker is not None:
            clear_confirmed_board_if_safe(owned_board_marker, fan)
        if lock_handle is not None:
            try:
                lock_handle.close()
            except OSError as exc:
                LOG.error("cannot close controller lock: %s", exc)
