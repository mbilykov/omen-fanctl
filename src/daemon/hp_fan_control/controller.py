"""Fan-control policy, runtime state machine, and telemetry."""

from __future__ import annotations

import csv
import logging
import os
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from .config import PWM_MAX, Settings, clamp, percent_to_pwm, pwm_to_percent
from .hardware import (
    AUTO_MODE,
    CONTROL_SENSORS,
    MAX_MODE,
    HardwareError,
    HpFanHwmon,
    PlatformProfileMonitor,
    Sensors,
    TemperatureSnapshot,
)


LOG = logging.getLogger("hp-fan-control")


class SystemdNotifier:
    """Minimal sd_notify client; inert outside a systemd notify service."""

    def __init__(
        self,
        address: str | None,
        watchdog_enabled: bool = True,
        watchdog_interval_s: float | None = None,
    ):
        self.address = address
        self.watchdog_enabled = watchdog_enabled
        self.watchdog_interval_s = watchdog_interval_s
        self.failed = False

    @classmethod
    def from_environment(cls) -> "SystemdNotifier":
        address = os.environ.get("NOTIFY_SOCKET")
        watchdog_pid = os.environ.get("WATCHDOG_PID")
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        watchdog_enabled = True
        if watchdog_pid:
            try:
                if int(watchdog_pid) != os.getpid():
                    watchdog_enabled = False
            except ValueError:
                watchdog_enabled = False
        watchdog_interval_s = None
        if watchdog_enabled and watchdog_usec:
            try:
                watchdog_timeout_s = int(watchdog_usec) / 1_000_000
                if watchdog_timeout_s > 0:
                    watchdog_interval_s = watchdog_timeout_s / 2
            except ValueError:
                pass
        if address and address.startswith("@"):
            address = "\0" + address[1:]
        return cls(address, watchdog_enabled, watchdog_interval_s)

    def notify(self, message: str) -> None:
        if self.address is None:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
                connection.sendto(message.encode("utf-8"), self.address)
        except OSError as exc:
            if not self.failed:
                LOG.warning("cannot notify systemd: %s", exc)
            self.failed = True
        else:
            if self.failed:
                LOG.info("systemd notification channel recovered")
            self.failed = False

    def ready(self) -> None:
        self.notify("READY=1")

    def watchdog(self) -> None:
        if self.watchdog_enabled:
            self.notify("WATCHDOG=1")

    def stopping(self) -> None:
        self.notify("STOPPING=1")

@dataclass
class Ewma:
    rise_alpha: float
    fall_alpha: float
    value: float | None = None

    def update(self, sample: float) -> float:
        if self.value is None:
            self.value = sample
        else:
            alpha = self.rise_alpha if sample >= self.value else self.fall_alpha
            self.value = alpha * sample + (1.0 - alpha) * self.value
        return self.value

class CsvLog:
    """Flush each telemetry row immediately so crashes retain prior samples."""

    FIELDS = (
        "timestamp",
        "elapsed_s",
        "profile",
        "state",
        "cpu_raw_c",
        "gpu_raw_c",
        "nvidia_power_draw_w",
        "nvidia_power_limit_w",
        "ir_raw_c",
        "acpi_raw_c",
        "cpu_ewma_c",
        "gpu_ewma_c",
        "ir_ewma_c",
        "acpi_ewma_c",
        "hottest_control_c",
        "curve_source",
        "winning_sensor",
        "cpu_target_percent",
        "gpu_target_percent",
        "ir_target_percent",
        "acpi_target_percent",
        "requested_pwm",
        "requested_percent",
        "actual_mode",
        "actual_pwm",
        "fan1_rpm",
        "fan2_rpm",
        "note",
    )

    def __init__(self, path: Path | None):
        self.path = path
        self.handle = None
        self.writer = None
        self.failed = False
        if path is not None:
            try:
                self._open()
            except OSError as exc:
                self._failure(exc)

    def _open(self) -> None:
        assert self.path is not None
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a", encoding="utf-8", newline="")
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            if os.fstat(handle.fileno()).st_size == 0:
                writer.writeheader()
            handle.flush()
        except OSError:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            raise
        self.handle = handle
        self.writer = writer

    def _failure(self, exc: OSError) -> None:
        if not self.failed:
            LOG.warning("CSV telemetry unavailable: %s", exc)
        self.failed = True

    def _discard_handle(self) -> None:
        handle = self.handle
        self.handle = None
        self.writer = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def write(self, row: dict[str, object]) -> None:
        if self.path is None:
            return
        try:
            if self.handle is None:
                self._open()
            assert self.handle is not None
            assert self.writer is not None
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.writer.writeheader()
            self.writer.writerow(row)
            self.handle.flush()
        except OSError as exc:
            self._discard_handle()
            self._failure(exc)
            return
        if self.failed:
            LOG.info("CSV telemetry recovered")
            self.failed = False

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        self.writer = None
        if handle is not None:
            try:
                handle.close()
            except OSError as exc:
                self._failure(exc)


class ControlPolicy:
    """Stateful fan-control decisions, independent of hardware and scheduling."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._commanded_pwm: int | None = None
        self._sensor_targets: dict[str, float | None] = {
            "cpu": None,
            "gpu": None,
            "ir": None,
            "acpi": None,
        }
        self._activated_sensors: set[str] = set()
        self._winning_sensor = ""

    @property
    def commanded_pwm(self) -> int | None:
        return self._commanded_pwm

    @property
    def sensor_targets(self) -> Mapping[str, float | None]:
        return MappingProxyType(self._sensor_targets)

    @property
    def winning_sensor(self) -> str:
        return self._winning_sensor

    def set_commanded_pwm(self, pwm: int | None) -> None:
        self._commanded_pwm = pwm

    def observe_activations(self, snapshot: TemperatureSnapshot) -> None:
        self._activated_sensors.update(self.activation_sources(snapshot))

    def exit_emergency(self, snapshot: TemperatureSnapshot) -> None:
        self._commanded_pwm = None
        self._sensor_targets = {
            "cpu": None,
            "gpu": None,
            "ir": None,
            "acpi": None,
        }
        self._activated_sensors = self.activation_sources(snapshot)
        self._winning_sensor = ""

    def reset(self) -> None:
        self._commanded_pwm = None
        self._sensor_targets = {
            "cpu": None,
            "gpu": None,
            "ir": None,
            "acpi": None,
        }
        self._activated_sensors.clear()
        self._winning_sensor = ""

    def activation_threshold(self, sensor: str) -> float:
        if sensor != "ir":
            return self.settings.activation_temp_c

        curve = self.settings.curve_for(sensor)
        manual_floor = percent_to_pwm(self.settings.minimum_manual_percent)
        for temperature, target in zip(curve.temperatures, curve.pwm_percent):
            if percent_to_pwm(target) > manual_floor:
                return temperature
        return float("inf")

    def cool_enough_for_auto(self, snapshot: TemperatureSnapshot) -> bool:
        for name, raw_value in snapshot.control_temperatures().items():
            if raw_value is None:
                continue
            # IR uses much lower curve temperatures than CPU/GPU. Do not
            # let a cool sensor that never activated control prevent a return
            # to firmware Auto after another sensor caused the Manual cycle.
            if name == "ir" and name not in self._activated_sensors:
                continue
            release = min(
                self.settings.release_temp_c,
                self.settings.fan_stop_temp_c,
            )
            if name == "ir":
                release = min(
                    release,
                    self.activation_threshold(name)
                    - self.settings.ir_release_hysteresis_c,
                )
            if raw_value > release:
                return False
        return True

    def activation_sources(self, snapshot: TemperatureSnapshot) -> set[str]:
        return {
            name
            for name, value in snapshot.control_temperatures().items()
            if value is not None and value >= self.activation_threshold(name)
        }

    def should_activate(self, snapshot: TemperatureSnapshot) -> bool:
        return bool(self.activation_sources(snapshot))

    def desired_pwm(
        self,
        filtered: dict[str, float | None],
        raw_temperatures: dict[str, float | None] | None = None,
    ) -> tuple[int, float]:
        temperatures = [
            value
            for name, value in filtered.items()
            if name in CONTROL_SENSORS and value is not None
        ]
        if not temperatures:
            raise HardwareError("no valid temperature is available for fan control")
        # Raw temperature gives prompt fan ramp-up. The filtered value remains
        # higher during cooldown and therefore controls the slower ramp-down.
        raw_control_temperatures = (
            []
            if raw_temperatures is None
            else [
                value
                for name, value in raw_temperatures.items()
                if name in CONTROL_SENSORS and value is not None
            ]
        )
        hottest = max(temperatures + raw_control_temperatures)

        targets: dict[str, float] = {}
        for name, filtered_temperature in filtered.items():
            if filtered_temperature is None:
                self._sensor_targets[name] = None
                continue
            raw_temperature = (
                None if raw_temperatures is None else raw_temperatures.get(name)
            )
            evaluating = (
                filtered_temperature
                if raw_temperature is None
                else max(filtered_temperature, raw_temperature)
            )
            curve = self.settings.curve_for(name)
            previous = self._sensor_targets[name]
            target = curve.target_percent(evaluating, previous)

            # Custom/legacy linear curves use the original generic decrease
            # hysteresis. Factory stepped CPU/GPU tables use their own lows.
            if (
                previous is not None
                and target < previous
                and curve.fall_temperatures is None
            ):
                target = min(
                    previous,
                    curve.target_percent(
                        evaluating + self.settings.decrease_hysteresis_c,
                        previous,
                    ),
                )
            self._sensor_targets[name] = target
            # acpitz is an opt-in diagnostic proxy. Preserve its evaluated
            # target in telemetry, but never let it drive fan state or PWM.
            if name in CONTROL_SENSORS:
                targets[name] = target

        self._winning_sensor = max(targets, key=targets.get)
        candidate = percent_to_pwm(targets[self._winning_sensor])

        minimum = percent_to_pwm(self.settings.minimum_manual_percent)
        candidate = max(candidate, minimum)

        if self._commanded_pwm is not None:
            rise = percent_to_pwm(self.settings.max_rise_percent_per_update)
            fall = percent_to_pwm(self.settings.max_fall_percent_per_update)
            candidate = min(candidate, self._commanded_pwm + rise)
            candidate = max(candidate, self._commanded_pwm - fall)
        return int(clamp(candidate, 1, PWM_MAX)), hottest


class Controller:
    """Coordinate the policy, hardware modes, scheduling, and fail-safe exits."""

    def __init__(
        self,
        settings: Settings,
        fan: HpFanHwmon,
        sensors: Sensors,
        apply: bool,
        duration_s: float | None,
        csv_log: CsvLog,
        profile_path: Path = Path("/sys/firmware/acpi/platform_profile"),
        status_interval_s: float = 1.0,
        notifier: SystemdNotifier | None = None,
        inactive_event_wait_s: float = 5.0,
        auto_guard_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], object] | None = None,
    ):
        settings.validate()
        self.settings = settings
        self.fan = fan
        self.sensors = sensors
        self.apply = apply
        self.duration_s = duration_s
        self.csv_log = csv_log
        self.profile_path = profile_path
        self.status_interval_s = status_interval_s
        self.notifier = notifier or SystemdNotifier(None)
        self.profile_monitor: PlatformProfileMonitor | None = None
        self.inactive_event_wait_s = inactive_event_wait_s
        self.auto_guard_path = auto_guard_path
        self.clock = clock
        self.wait = wait
        self.next_status_log = 0.0
        self.last_status_state = ""
        self.last_status_note = ""
        self.stop_requested = False
        self.manual_active = False
        self.emergency = False
        self.emergency_since: float | None = None
        self.auto_guard_until: float | None = None
        self.last_sensor_failure: tuple[str, str] | None = None
        self.filters = {
            "cpu": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
            "gpu": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
            "ir": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
            "acpi": Ewma(settings.ewma_rise_alpha, settings.ewma_fall_alpha),
        }
        self.policy = ControlPolicy(settings)

    def request_stop(self, signum: int, _frame: object) -> None:
        LOG.info("received signal %s", signum)
        self.stop_requested = True

    def _profile(self) -> str:
        if self.profile_monitor is None:
            raise HardwareError("platform profile monitor is not initialized")
        return self.profile_monitor.current

    def _wait_for_profile_change(self, timeout_s: float) -> None:
        assert self.profile_monitor is not None
        watchdog_interval = getattr(
            self.notifier, "watchdog_interval_s", None
        )
        if not isinstance(watchdog_interval, (int, float)):
            watchdog_interval = None
        remaining = timeout_s
        while remaining > 0 and not self.stop_requested:
            wait_s = (
                min(remaining, watchdog_interval)
                if watchdog_interval is not None and watchdog_interval > 0
                else remaining
            )
            if self.wait is not None:
                self.wait(wait_s)
                changed = self.profile_monitor.refresh()
            else:
                changed = self.profile_monitor.wait_for_change(wait_s)
            if changed or self.stop_requested:
                return
            remaining -= wait_s
            if remaining > 0:
                self.notifier.watchdog()

    def _filtered(self, snapshot: TemperatureSnapshot) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for name, value in (
            ("cpu", snapshot.cpu),
            ("gpu", snapshot.gpu),
            ("ir", snapshot.ir),
            ("acpi", snapshot.acpi),
        ):
            result[name] = None if value is None else self.filters[name].update(value)
        return result

    def _apply_manual(self, pwm: int) -> int:
        if not self.apply:
            self.manual_active = True
            self.policy.set_commanded_pwm(pwm)
            self._clear_auto_guard()
            return pwm
        if not self.manual_active:
            # Never reduce airflow at the Auto -> Manual boundary. hp-wmi's
            # pwm1 read reflects the current CPU fan level even in Auto mode.
            _, current_pwm, _, _ = self.fan.status()
            pwm = max(pwm, current_pwm)
            self.fan.set_manual(pwm)
            self.manual_active = True
            self._clear_auto_guard()
        else:
            # Check the hardware mode on every control tick even when the
            # requested PWM is unchanged. Firmware or another tool may have
            # reset pwm1_enable behind the controller's back.
            self.fan.update_manual(
                pwm,
                write_pwm=pwm != self.policy.commanded_pwm,
            )
        self.policy.set_commanded_pwm(pwm)
        return pwm

    def _maximum(self) -> None:
        if self.apply:
            mode, _, _, _ = self.fan.status()
            if mode != MAX_MODE:
                self.fan.set_maximum()
        self.manual_active = True
        self.policy.set_commanded_pwm(PWM_MAX)
        self._clear_auto_guard()

    def _start_auto_guard(self, now: float) -> None:
        # Firmware may stop the fans shortly after Auto is restored. Persist
        # this observation window so ExecStopPost can fail safe across a crash.
        self.auto_guard_until = now + self.settings.auto_guard_s
        if self.auto_guard_path is not None:
            try:
                self.auto_guard_path.write_text(
                    f"{self.auto_guard_until:.6f}\n", encoding="ascii"
                )
            except OSError as exc:
                raise HardwareError(
                    f"cannot persist firmware Auto guard: {exc}"
                ) from exc

    def _clear_auto_guard(self) -> None:
        self.auto_guard_until = None
        if self.auto_guard_path is not None:
            try:
                self.auto_guard_path.unlink(missing_ok=True)
            except OSError as exc:
                raise HardwareError(
                    f"cannot clear firmware Auto guard: {exc}"
                ) from exc

    def _auto_guard_active(self, now: float) -> bool:
        if self.auto_guard_until is None:
            return False
        if now < self.auto_guard_until:
            return True
        self._clear_auto_guard()
        return False

    def _failsafe_on_stop(self) -> None:
        guard_until = self.auto_guard_until
        guard_present = guard_until is not None
        guard_active = guard_present and self.clock() < guard_until
        if self.apply and (self.manual_active or self.emergency or guard_active):
            LOG.critical(
                "controller stopped while software cooling or Auto guard "
                "was active; selecting maximum fans"
            )
            try:
                self.fan.set_maximum()
            except HardwareError as exc:
                LOG.critical("FAILED TO SELECT MAXIMUM FANS: %s", exc)
                return
            try:
                self._clear_auto_guard()
            except HardwareError as exc:
                LOG.error(
                    "maximum fans selected but failed to clear Auto guard: %s",
                    exc,
                )
        elif guard_present:
            try:
                self._clear_auto_guard()
            except HardwareError as exc:
                LOG.error("failed to clear Auto guard: %s", exc)

    def _restore_auto(self, reason: str, now: float) -> None:
        if not (self.manual_active or self.emergency):
            return
        LOG.info("restoring firmware Auto: %s", reason)
        if self.apply:
            self.fan.restore_auto()
        self.manual_active = False
        self.emergency = False
        self.emergency_since = None
        self.policy.reset()
        self._start_auto_guard(now)

    def log_sample(
        self,
        started: float,
        profile: str,
        state: str,
        snapshot: TemperatureSnapshot,
        filtered: dict[str, float | None],
        hottest: float,
        requested: int | None,
        note: str = "",
    ) -> None:
        try:
            mode, actual_pwm, fan1, fan2 = self.fan.status()
        except HardwareError:
            mode = actual_pwm = fan1 = fan2 = -1

        def fmt(value: float | None) -> str:
            return "" if value is None else f"{value:.1f}"

        now = self.clock()
        if (
            state != self.last_status_state
            or note != self.last_status_note
            or now >= self.next_status_log
        ):
            LOG.info(
                "state=%-9s profile=%-11s CPU=%5.1f GPU=%5s GPUW=%6s IR=%5s ACPI=%5s "
                "control=%5.1f winner=%-4s request=%3s (%5s%%) "
                "actual=%d/%d fans=%d/%d%s",
                state,
                profile,
                snapshot.cpu,
                fmt(snapshot.gpu) or "n/a",
                fmt(snapshot.nvidia_power_draw_w) or "n/a",
                fmt(snapshot.ir) or "n/a",
                fmt(snapshot.acpi) or "n/a",
                hottest,
                self.policy.winning_sensor or "-",
                "-" if requested is None else requested,
                "-" if requested is None else f"{pwm_to_percent(requested):.1f}",
                mode,
                actual_pwm,
                fan1,
                fan2,
                f" note={note}" if note else "",
            )
            self.last_status_state = state
            self.last_status_note = note
            self.next_status_log = now + self.status_interval_s
        self.csv_log.write(
            {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "elapsed_s": f"{self.clock() - started:.1f}",
                "profile": profile,
                "state": state,
                "cpu_raw_c": fmt(snapshot.cpu),
                "gpu_raw_c": fmt(snapshot.gpu),
                "nvidia_power_draw_w": fmt(snapshot.nvidia_power_draw_w),
                "nvidia_power_limit_w": fmt(snapshot.nvidia_power_limit_w),
                "ir_raw_c": fmt(snapshot.ir),
                "acpi_raw_c": fmt(snapshot.acpi),
                "cpu_ewma_c": fmt(filtered["cpu"]),
                "gpu_ewma_c": fmt(filtered["gpu"]),
                "ir_ewma_c": fmt(filtered["ir"]),
                "acpi_ewma_c": fmt(filtered["acpi"]),
                "hottest_control_c": f"{hottest:.1f}",
                "curve_source": self.settings.curve_source,
                "winning_sensor": self.policy.winning_sensor,
                "cpu_target_percent": fmt(self.policy.sensor_targets["cpu"]),
                "gpu_target_percent": fmt(self.policy.sensor_targets["gpu"]),
                "ir_target_percent": fmt(self.policy.sensor_targets["ir"]),
                "acpi_target_percent": fmt(self.policy.sensor_targets["acpi"]),
                "requested_pwm": "" if requested is None else requested,
                "requested_percent": (
                    "" if requested is None else f"{pwm_to_percent(requested):.1f}"
                ),
                "actual_mode": mode,
                "actual_pwm": actual_pwm,
                "fan1_rpm": fan1,
                "fan2_rpm": fan2,
                "note": note,
            }
        )

    def run(self) -> None:
        started = self.clock()
        next_control = started
        if self.apply:
            initial_mode, _, _, _ = self.fan.status()
            if initial_mode == MAX_MODE:
                self.manual_active = True
                self.emergency = True
                self.emergency_since = started
                self.policy.set_commanded_pwm(PWM_MAX)
                LOG.warning("adopting maximum-fan fail-safe from previous service run")
            elif initial_mode != AUTO_MODE:
                raise HardwareError(
                    f"expected firmware Auto or fail-safe Max, found {initial_mode}; "
                    "another controller may be active"
                )

        self.profile_monitor = PlatformProfileMonitor(self.profile_path)
        self.notifier.ready()

        try:
            while not self.stop_requested:
                self.notifier.watchdog()
                now = self.clock()
                if self.duration_s is not None and now - started >= self.duration_s:
                    LOG.info("configured duration completed")
                    break

                profile = self._profile()
                outside_required_profile = profile != self.settings.required_profile
                auto_guard_active = self._auto_guard_active(now)
                if outside_required_profile and not (
                    self.manual_active or self.emergency or auto_guard_active
                ):
                    for temperature_filter in self.filters.values():
                        temperature_filter.value = None
                    if self.last_status_state != "sleeping":
                        LOG.info(
                            "state=sleeping profile=%s; waiting for %s",
                            profile,
                            self.settings.required_profile,
                        )
                        self.last_status_state = "sleeping"
                    self._wait_for_profile_change(self.inactive_event_wait_s)
                    continue

                try:
                    snapshot = self.sensors.read()
                except HardwareError as exc:
                    if self.manual_active or self.emergency or auto_guard_active:
                        failure = ("control", str(exc))
                        if failure != self.last_sensor_failure:
                            LOG.error(
                                "sensor failure during control; "
                                "selecting maximum: %s",
                                exc,
                            )
                        self._maximum()
                        self.emergency = True
                        self.emergency_since = self.emergency_since or now
                    else:
                        failure = ("bios-auto", str(exc))
                        if failure != self.last_sensor_failure:
                            LOG.error(
                                "sensor failure while BIOS Auto is active: %s",
                                exc,
                            )
                    self.last_sensor_failure = failure
                    self._wait_for_profile_change(self.settings.sample_interval_s)
                    continue
                self.last_sensor_failure = None

                filtered = self._filtered(snapshot)
                hottest = max(
                    value
                    for name, value in filtered.items()
                    if name in CONTROL_SENSORS and value is not None
                )
                note = ""

                self.policy.observe_activations(snapshot)

                # Branch order is the safety priority: critical heat, retained
                # emergency, stable Auto, cool Manual handoff, normal control.
                if snapshot.raw_control_hottest >= self.settings.critical_temp_c:
                    if not self.emergency:
                        LOG.warning(
                            "critical raw temperature %.1f C; selecting maximum fans",
                            snapshot.raw_control_hottest,
                        )
                    self._maximum()
                    self.emergency = True
                    self.emergency_since = self.emergency_since or now
                    state = "emergency"
                    requested = PWM_MAX
                    note = "raw critical threshold"
                elif self.emergency:
                    held_long_enough = (
                        self.emergency_since is not None
                        and now - self.emergency_since >= self.settings.emergency_hold_s
                    )
                    if (
                        hottest <= self.settings.critical_release_temp_c
                        and held_long_enough
                    ):
                        self.emergency = False
                        self.manual_active = False
                        self.policy.exit_emergency(snapshot)
                        self._clear_auto_guard()
                        requested, hottest = self.policy.desired_pwm(filtered)
                        requested = self._apply_manual(requested)
                        state = "manual"
                        note = "left emergency state"
                    else:
                        self._maximum()
                        state = "emergency"
                        requested = PWM_MAX
                        note = "waiting for critical release"
                elif (
                    not self.manual_active
                    and not self.policy.should_activate(snapshot)
                ):
                    state = "auto-guard" if auto_guard_active else "bios-auto"
                    requested = None
                    if auto_guard_active:
                        note = "monitoring firmware fan-stop window"
                elif self.manual_active and self.policy.cool_enough_for_auto(snapshot):
                    reason = "raw temperatures reached fan-stop thresholds"
                    if outside_required_profile:
                        reason += f" for profile {profile}"
                    self._restore_auto(reason, now)
                    auto_guard_active = True
                    state = "auto-guard"
                    requested = None
                    note = "monitoring firmware fan-stop window"
                else:
                    candidate, hottest = self.policy.desired_pwm(
                        filtered,
                        {
                            **snapshot.control_temperatures(),
                            "acpi": snapshot.acpi,
                        },
                    )
                    # Heating may raise the target on every sample. Fan-speed
                    # reductions remain limited to the normal control cadence.
                    should_update = (
                        self.policy.commanded_pwm is None
                        or candidate > self.policy.commanded_pwm
                        or now >= next_control
                    )
                    if should_update:
                        requested = self._apply_manual(candidate)
                        next_control = now + self.settings.control_interval_s
                    else:
                        requested = self.policy.commanded_pwm
                    state = "handoff" if outside_required_profile else "manual"
                    if outside_required_profile:
                        note = "cooling before firmware Auto"

                self.log_sample(
                    started,
                    profile,
                    state,
                    snapshot,
                    filtered,
                    hottest,
                    requested,
                    note,
                )
                self._wait_for_profile_change(self.settings.sample_interval_s)
        finally:
            self._failsafe_on_stop()
            self.notifier.stopping()
            self.profile_monitor.close()
