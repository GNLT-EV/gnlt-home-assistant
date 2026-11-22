"""Command layer of the ModBus mode (no Home Assistant imports).

One ``Commander`` per station sits on top of ``station.Station`` (which owns the
write gate) and decides *what* to write - like evcc drives an EM2GO box:

* ``apply(Target(enabled, current_a, phases), origin)`` - the single entry
  point used by manual controls, the schedule, auto start, resume
  and the empty-session stop; the GNLT site uses it too;
* current: written at once, skipped only when equal to the last value
  written (evcc setLimit); a value the station holds after a change by
  someone else is adopted (evcc syncCharger);
* start: phases (if the choice differs) -> 91 -> 95 = 1, nothing else;
  on the whole-amp class the EM2GO driver's Enable(true) workaround follows
  (200 = 1 again on one phase, the current);
* stop: 95 = 2 then 91 = 0 when reg 95 reads 1 or the station charges;
  nothing for an idle station;
* phases: 200 directly (no car, car connected, the new class while charging
  with evcc core's minimum current before 1 -> 3); the whole-amp class
  follows the EM2GO workaround (``sequences.py``);
* the firmware class probe (evcc initialize), run by the start-up
  backoff (``async_initialize``);
* the supervisor fed by every poll and station event: adoption at start-up,
  empty session, status 6 after our stop,
  self start, "stop not working" as an observation of the polls,
  schedule, auto start and resume like the
  OCPP integration.

No fresh read, no read-back, no re-write, no verification by sampling, no
persisted step. Refusals are ``WriteRefused`` (``CommandError``
is one) carrying the translation key and placeholders of the Home Assistant
error. Notifications are published to ``subscribe_notifications``
listeners; the HA layer shows them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..logic import Schedule
from . import const as C
from .sequences import (
    G_SETPOINT,
    KIND_PHASE,
    KIND_START,
    START_ALREADY,
    START_SENT,
    STOP_FAILED,
    STOP_NOT_NEEDED,
    STOP_SENT,
    Sequence,
    SequenceCancelled,
    SequenceMixin,
    StartResult,
    StopResult,
)
from .client import ModbusConnectionError
from .station import WriteRefused, WriteResult, is_automatic

_LOGGER = logging.getLogger(__name__)

STORE_SECTION = "commands"
COMMANDS_STORE_VERSION = 1

# --- origins (only user/automation are not automatic) --------------------

ORIGIN_USER = "user"
ORIGIN_AUTOMATION = "automation"
ORIGIN_SCHEDULE = "schedule"
ORIGIN_AUTO_START = "auto_start"
ORIGIN_RESUME = "resume"
ORIGIN_EMPTY_SESSION = "empty_session"
ORIGIN_SEQUENCE = "sequence"
ORIGIN_RESTORE = "restore"  # the station's restore of 91 after a reconnect or restart
ORIGIN_PROBE = C.ORIGIN_PROBE  # the 6.1 A probe and its restore of 91
ORIGIN_SITE = C.ORIGIN_SITE  # the site coordinator (DLM + PV): automatic control, see C.SITE_ORIGINS

# --- refusal keys -----------------------------------------------------

KEY_PNC_ON = "plug_and_charge_on"
KEY_PHASE_CHARGING = "phase_refused_charging"  # a start/charge state that forbids it
KEY_PHASE_CLASS_UNKNOWN = "phase_refused_class_unknown"  # 6.1 A probe not done yet
KEY_PHASE_NA = "phase_refused_not_applicable"
KEY_NO_CAR = "no_car"  # start without a plugged car: evcc core enables only with a car connected
# Start refusals like the OCPP integration: a faulted
# station (reg 4 != 0 or status 7), and after an empty session until a re-plug.
KEY_FAULTED = "mb_faulted"
KEY_FAULTED_REPLUG = "mb_faulted_replug"  # vehicle faults 21/22: a re-plug clears them
KEY_REPLUG = "replug"
_NOT_READ = object()  # the 91 before the firmware probe, not read yet
REFUSAL_KEYS = frozenset(
    {
        C.REFUSE_NOT_CONNECTED, C.REFUSE_NOT_READY, C.REFUSE_IDENTITY_MISMATCH,
        C.REFUSE_STATION_BUSY, C.REFUSE_NOT_ALLOWED,
        C.REFUSE_CURRENT_RANGE, C.REFUSE_BUSY, KEY_PNC_ON,
        KEY_PHASE_CHARGING, KEY_PHASE_CLASS_UNKNOWN, KEY_PHASE_NA,
        KEY_NO_CAR, KEY_FAULTED, KEY_FAULTED_REPLUG, KEY_REPLUG,
    }
)

# --- notification kinds of the command layer ------------------------------------

NOTIFY_KINDS = (
    "empty_session", "station_restarted", "finished_replug", "pnc_reset", "current_clamped",
)

# Problem reasons owned by this layer (``control_problem`` attribute).
PROBLEM_EMPTY_SESSION = "empty_session"
PROBLEM_STOP_NOT_WORKING = "stop_not_working"
_SESSION_PROBLEMS = (PROBLEM_EMPTY_SESSION, PROBLEM_STOP_NOT_WORKING)

# Record reasons for the energy layer.
REASON_USER_STOP = "user_stop"
REASON_SCHEDULE_END = "schedule_end"
REASON_UNKNOWN = "unknown"

RESUME_WAIT_S = 600.0  # how long a decided resume waits for status 2
# Between automatic starts (schedule, auto start, resume) like the OCPP
# integration (charger.py AUTO_START_COOLDOWN_S). Manual
# and site starts are never limited (evcc has no start limit).
AUTO_START_COOLDOWN_S = 180.0
# evcc syncCharger: a station current more than this away from the last value
# written is adopted (smallest PWM step 0.234 A; in 0.1 A).
ADOPT_DELTA_TENTHS = 3


class CommandError(WriteRefused):
    """A command refused by this layer; ``key`` is the translation key."""

    @property
    def translation_key(self) -> str:
        return self.key


@dataclass(frozen=True)
class Target:
    """What the station should do. ``None`` leaves that part alone."""

    enabled: bool | None = None
    current_a: float | None = None
    phases: int | None = None


@dataclass
class CommandOptions:
    """Options of the command layer."""

    # False for 1-phase presets (no phase select).
    phases_available: bool = True
    auto_start: bool = False
    resume_after_power_loss: bool = False
    # stop of an empty session; False while the station is a member of a
    # GNLT site (site/coordinator.py decides; the flag and notification stay).
    empty_session_stop: bool = True
    schedule: Schedule = field(default_factory=Schedule)


def _parse(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _energy_reason(origin: str) -> str:
    if origin in C.USER_ORIGINS:
        return REASON_USER_STOP
    if origin == ORIGIN_SCHEDULE:
        return REASON_SCHEDULE_END
    return REASON_UNKNOWN


def _sent(res: WriteResult | None) -> bool:
    return res is not None and res.outcome not in (C.WRITE_NOT_SENT, C.WRITE_REJECTED)


def _not_taken(res: WriteResult) -> CommandError:
    return CommandError(C.REFUSE_NOT_CONNECTED if res.outcome == C.WRITE_NOT_SENT else C.REFUSE_STATION_BUSY)


class Commander(SequenceMixin):
    """Commands, sequences and automatic features of one station."""

    def __init__(
        self,
        station: Any,
        *,
        energy: Any = None,
        options: CommandOptions | None = None,
        wall: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.station = station
        self.energy = energy
        self.options = options or CommandOptions()
        self._clock = station.clock
        self._sleep = sleep
        self._wall = wall or (lambda: datetime.now(UTC))
        self._seq: Sequence | None = None
        self._tasks: set[asyncio.Task] = set()
        self._listeners: list[Callable[[str, dict[str, Any]], Any]] = []
        self.would_do: str | None = None  # what an automatic feature would do / why not

        # start-up and the state seen by the supervisor
        self._startup_done = False
        self._last_snap_time: float | None = None
        self._prev_status: int | None = None
        self._prev_record_open = False
        self._record_token = 0
        self._last_obs: tuple[float, int | None, float] | None = None

        # own start / plug-in
        self._own_start_at: float | None = None
        self._delivered_since_start = False
        self._plug_token = 0
        self._user_cmd_at: float | None = None
        self._last_auto_start: float | None = None  # 180 s between automatic starts

        # stops
        self._last_stop_at: float | None = None
        self._stop_not_working = False  # an observation of the polls, not persisted
        self._empty_watch: float | None = None  # our 95 = 1, checked once 180 s later
        self._charged_since_plug = False  # a 2 / 6 after a charge is not "plugged idle"

        # current
        self._slot: int | None = None
        # The last 91 we wrote (evcc lp.offeredCurrent): an equal value is not
        # written again; 0 after a stop, so the next start writes it.
        self._offered: int | None = None
        self._probe_before: Any = _NOT_READ
        # The GNLT site's current (0.1 A) while it controls the station: site writes
        # (ORIGIN_SITE) never replace the user's value in station.desired; starts
        # and sequences use this one instead. None = no site control (the site
        # resets it when it releases the station).
        self.site_current: int | None = None
        # Member of a GNLT site (persisted with site_current, cleared only by a
        # deliberate release): without a site current, restores and starts use
        # at most the station's present 91 / the station minimum.
        self.site_member = False
        station.restore_override = self._site_restore_target  # restores too
        self._slot_origin = ORIGIN_USER
        self._clamp_notified: int | None = None

        # phases
        self._in_phase_switch = False

        # schedule, auto start, resume like OCPP
        self._sched_open: bool | None = None  # persisted: a close edge across an HA restart
        self._resume_pending = False
        self._resume_until = 0.0
        self._closed = False

        # reports
        self._charging_without_session = False

        station.add_store_section(STORE_SECTION, self._dump, self._load)
        self._unsubscribe = [station.subscribe(self._on_snapshot), station.subscribe_events(self._on_event)]

    # --- persistence ---------------------------------------------------------------

    def _dump(self) -> dict[str, Any]:
        return {
            "version": COMMANDS_STORE_VERSION,
            "clamp_notified": self._clamp_notified,
            # Wall clock: survives an HA restart.
            "times": {
                "auto_start": self._to_wall(self._last_auto_start),
                "stop_sent": self._to_wall(self._last_stop_at),
            },
            "schedule": {"open": self._sched_open},
            "site": {"member": self.site_member, "current": self.site_current},
        }

    def _to_wall(self, mono: float | None) -> str | None:
        if mono is None:
            return None
        return (self._wall() - timedelta(seconds=self._clock() - mono)).isoformat()

    def _to_mono(self, text: Any) -> float | None:
        at = self._parse_past(text)
        if at is None:
            return None
        return self._clock() - (self._wall() - at).total_seconds()

    def _parse_past(self, text: Any) -> datetime | None:
        """A stored time; one later than now (wall clock behind at load, e.g.
        before NTP) is clamped to now."""
        at = _parse(text)
        if at is None:
            return None
        now = self._wall()
        return now if at > now else at

    def _load(self, data: Any) -> None:
        """Stored keys of removed features ("sequence", "stop_not_working",
        "stop_not_working_notified", "auto_starts", "missed_stop", the start
        and switch quotas, the window records) are ignored
."""
        if not isinstance(data, dict):
            return
        clamp = data.get("clamp_notified")
        self._clamp_notified = clamp if isinstance(clamp, int) else None
        times = data.get("times") if isinstance(data.get("times"), dict) else {}
        self._last_stop_at = self._to_mono(times.get("stop_sent"))
        # stored start/switch quotas ("starts", "switches") are ignored
        self._last_auto_start = self._to_mono(times.get("auto_start"))
        sched = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
        if isinstance(sched.get("open"), bool):
            self._sched_open = sched["open"]
        site = data.get("site") if isinstance(data.get("site"), dict) else {}
        self.site_member = site.get("member") is True
        current = site.get("current")
        self.site_current = current if self.site_member and isinstance(current, int) and not isinstance(current, bool) else None

    async def _save(self) -> None:
        try:
            await self.station.async_save()
        except Exception:
            _LOGGER.exception("%s: saving the command state failed", self.station.serial)

    def _schedule_save(self) -> None:
        self._spawn(self._save())

    # --- read-only state -------------------------------------------------------------------

    def set_site_member(self, on: bool) -> None:
        """A GNLT site takes the station over (True) or releases it on purpose
        (False: the site current is cleared). Persisted."""
        on = bool(on)
        changed = on != self.site_member or (not on and self.site_current is not None)
        self.site_member = on
        if not on:
            self.site_current = None
        else:
            # automatic actions decided before the site took over never fire
            # resume after power loss
            self._resume_pending = False
        if changed:
            self._schedule_save()

    def set_site_current(self, tenths: int | None) -> None:
        """The site's current without a write (e.g. its minimum after a site crash)."""
        if tenths != self.site_current:
            self.site_current = tenths
            self._schedule_save()

    def _site_restore_target(self, current_91: int | None) -> int | None:
        """What a station-layer 91 restore writes instead of desired (None = desired):
        the site's current; for a member without one, at most the present 91."""
        if self.site_current is not None:
            return self.site_current
        if not self.site_member:
            return None
        low = self.station.current_range_tenths()[0]
        values = [
            v for v in (self.station.desired.get("current"), current_91)
            if isinstance(v, int) and not isinstance(v, bool) and v not in C.STOP_SETPOINT_STORED
        ]
        return min(values) if values else low

    @property
    def sequence(self) -> str | None:
        return None if self._seq is None else self._seq.kind

    @property
    def stop_not_working(self) -> bool:
        return self._stop_not_working

    @property
    def charging_without_session(self) -> bool:
        return self._charging_without_session

    @property
    def charging_switch_state(self) -> bool:
        """``charging`` switch like OCPP: ON in status 4 or while a charge record is open that
        is not an empty session."""
        st = self.station
        return st.snapshot.status == C.STATUS_CHARGING or (
            st.record_open and not st.session_flags.get("empty_session")
        )

    @property
    def phase_select_available(self) -> bool:
        """no select for 1-phase presets or without the reg 200 capability
        (evcc initialize: PhaseSwitcher when the read of 200 succeeds)."""
        st = self.station
        return bool(self.options.phases_available and st.phase_capability is True)

    @property
    def phase_pending(self) -> bool:
        desired = self.station.desired.get("phases")
        return desired in (1, 3) and self.station.snapshot.phases not in (None, desired)

    def enabled(self) -> bool:
        """evcc Enabled() is reg 95 == 1; a station that
        charges is enabled too (evcc syncCharger: "disabled but charging")."""
        snap = self.station.snapshot
        return snap.last_command == C.COMMAND_START or snap.status in (C.STATUS_STARTING, C.STATUS_CHARGING)

    # --- notifications -----------------------------------------------------------------

    def subscribe_notifications(self, callback: Callable[[str, dict[str, Any]], Any]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _notify(self, kind: str, /, **data: Any) -> None:
        data.setdefault("startup", not self._startup_done)
        _LOGGER.info("%s: notification %s %s", self.station.serial, kind, data)
        for cb in list(self._listeners):
            try:
                cb(kind, data)
            except Exception:
                _LOGGER.exception("%s: notification listener failed for %s", self.station.serial, kind)

    def _observed(self) -> dict[str, Any]:
        snap = self.station.snapshot
        last = self._last_obs
        return {
            "status": last[1] if last is not None else snap.status,
            "current_a": last[2] if last is not None else None,
            "phases": snap.phases,
            "setpoint_a": snap.setpoint_a,
        }

    # --- tasks -----------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task | None:
        if self._closed:
            coro.close()  # type: ignore[union-attr] # unloaded: no new work
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()  # type: ignore[union-attr]
            return None
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("%s: command task failed: %r", self.station.serial, task.exception())

    async def wait_idle(self) -> None:
        """Wait until no task of this layer runs (tests, unload)."""
        while self._tasks:
            await asyncio.wait(set(self._tasks))

    async def async_close(self) -> None:
        """Unload: no new work; running tasks are cancelled."""
        self._closed = True
        self._slot = None
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        if self._seq is not None:
            self._cancel(self._seq, "unload")
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=2.0)

    def _new_seq(self, kind: str, origin: str) -> Sequence:
        seq = Sequence(kind, origin, self._clock())
        try:
            seq.committed = asyncio.get_running_loop().create_future()
        except RuntimeError:
            seq.committed = None
        self._seq = seq
        return seq

    def _cancel(self, seq: Sequence, reason: str) -> None:
        if not seq.cancelled:
            seq.cancelled = True
            seq.cancel_reason = reason
            seq.event.set()
            _LOGGER.info("%s: %s sequence cancelled (%s) at step %s", self.station.serial, seq.kind, reason, seq.step)

    async def _end_seq(self, seq: Sequence) -> None:
        if self._seq is not seq:
            return
        self._seq = None
        if self._slot is not None:
            # Current changes during the sequence went to the slot.
            self._spawn(self._flush_current())

    # --- the single entry point ----------------------------------------------------

    async def apply(self, target: Target, origin: str = ORIGIN_USER) -> Any:
        """Current, phases, start or stop with every gate and limit.

        Checks that need no request run first and raise at once; a start returns once its 95 = 1 went out, a stop once its
        95 = 2 went out.
        """
        if not is_automatic(origin):
            self._user_cmd_at = self._clock()
        if target.enabled is False:
            # A stop is never refused or delayed by the other fields: it goes
            # first; a refusal of the rest is raised after it.
            result = await self._stop(origin)
            error: WriteRefused | None = None
            if target.phases is not None:
                try:
                    self._keep_phase_choice(target.phases)
                except WriteRefused as err:
                    error = err
            if target.current_a is not None:
                try:
                    await self.apply(Target(current_a=target.current_a), origin)
                except WriteRefused as err:
                    error = error or err
            if error is not None:
                raise error
            return result
        tenths = None
        phase_action = None
        if target.current_a is not None:
            tenths = self.validate_current(target.current_a, origin)
        if target.phases is not None:
            phase_action = self._validate_phases(target.phases, origin)
        starting = target.enabled is True and not self._already_running()
        if starting:
            self._start_prechecks(origin)
        result: Any = None
        if tenths is not None:
            result = await self._set_current(tenths, origin, starting=starting)
        if phase_action is not None:
            result = await self._set_phases(target.phases, phase_action, origin)
        if target.enabled is True:
            result = await self._start(origin) if starting else StartResult(START_ALREADY)
        return result

    async def async_set_current(self, amps: float, origin: str = ORIGIN_USER) -> Any:
        return await self.apply(Target(current_a=amps), origin)

    async def async_set_phases(self, phases: int, origin: str = ORIGIN_USER) -> Any:
        return await self.apply(Target(phases=phases), origin)

    async def async_start(self, origin: str = ORIGIN_USER) -> Any:
        return await self.apply(Target(enabled=True), origin)

    async def async_stop(self, origin: str = ORIGIN_USER) -> Any:
        return await self.apply(Target(enabled=False), origin)

    async def _automatic_start(self, origin: str) -> Any:
        """Schedule, auto start and resume like OCPP ``_auto_start``:
        at most one automatic start per 180 s; a refusal is only logged."""
        now = self._clock()
        if self._last_auto_start is not None and now - self._last_auto_start < AUTO_START_COOLDOWN_S:
            self.would_do = f"{origin}: cooldown"
            return None
        self._last_auto_start = now
        self._schedule_save()
        return await self._auto_apply(Target(enabled=True), origin)

    async def _auto_apply(self, target: Target, origin: str) -> Any:
        """Automatic features: a refusal is logged (and shown as ``would_do``), never raised."""
        try:
            return await self.apply(target, origin)
        except WriteRefused as err:
            self.would_do = f"{origin}: {err.key}"
            _LOGGER.info("%s: %s %s not done: %s %s", self.station.serial, origin, target, err.key, err.placeholders)
            return None

    def _already_running(self) -> bool:
        seq = self._seq
        return self.station.snapshot.status in (C.STATUS_STARTING, C.STATUS_CHARGING) or (
            seq is not None and seq.kind in (KIND_START, KIND_PHASE) and not seq.cancelled
        )

    def _may_run(self) -> bool:
        """A charge may be running or about to: a stop is needed."""
        st = self.station
        return self._already_running() or st.record_open or st.start_pending or self.enabled()

    # --- current ---------------------------------------------------------------------

    def _floor(self, tenths: int, low: int) -> int:
        floored = tenths // 10 * 10
        return floored if floored >= low else floored + 10

    def validate_current(self, amps: float, origin: str = ORIGIN_USER) -> int:
        """0.1 A steps inside [reg 34, min(reg 32, reg 36, preset)] on
        every station; whole-amp stations
        floor; then the synchronous gate. Returns the value in 0.1 A."""
        st = self.station
        if isinstance(amps, bool) or not isinstance(amps, (int, float)):
            raise CommandError(C.REFUSE_CURRENT_RANGE)
        tenths = int(round(float(amps) * 10))
        low, high = st.current_range_tenths()
        if not low <= tenths <= high:
            raise CommandError(C.REFUSE_CURRENT_RANGE, min=low / 10, max=high / 10)
        if st.learned["decimal_current"] == "no":
            tenths = self._floor(tenths, low)
        st.check_write(C.REG_CURRENT_SETPOINT, tenths, origin)
        return tenths

    def _clamp(self, tenths: int) -> int:
        st = self.station
        low, high = st.current_range_tenths()
        value = min(max(tenths, low), high)
        if st.learned["decimal_current"] == "no":
            value = self._floor(value, low)
        return value

    def _start_current_target(self, notify: bool = False) -> int:
        """The desired current clamped to the range now; a desired value above
        a new maximum is announced once per value."""
        st = self.station
        low, high = st.current_range_tenths()
        if self.site_current is not None:
            desired = self.site_current
        elif self.site_member:
            desired = low  # a site member without a site current: never above the minimum
        else:
            desired = st.desired.get("current")
        if desired is None:
            value = st.snapshot.registers.get(C.REG_CURRENT_SETPOINT)
            desired = low if value is None or value in C.STOP_SETPOINT_STORED else value  # never the marker (C5)
        target = self._clamp(desired)
        if notify and desired > high and self._clamp_notified != desired:
            self._clamp_notified = desired
            self._notify("current_clamped", desired_a=desired / 10, applied_a=target / 10)
            self._schedule_save()
        return target

    async def _write_current(self, target: int, origin: str, *, force: bool = False) -> WriteResult | None:
        """One 91 write like evcc's setLimit: skipped when equal to the last
        value written (``force``: the start and the EM2GO workaround always
        write it). None when nothing was written."""
        if not force and target == self._offered:
            return None
        res = await self.station.async_write(C.REG_CURRENT_SETPOINT, target, origin=origin)
        if res.outcome == C.WRITE_OK:
            self._offered = target
        return res

    async def _set_current(self, tenths: int, origin: str, *, starting: bool = False) -> Any:
        st = self.station
        if origin in C.SITE_ORIGINS:
            if self.site_current != tenths:
                self.site_current = tenths  # the user's desired current stays as it is
                self._schedule_save()
        elif st.desired.get("current") != tenths:
            st.desired["current"] = tenths
            self._schedule_save()
        if starting:
            return None  # the start writes it first
        if self._seq is not None:
            # applied after the sequence; last value wins
            self._slot = tenths
            self._slot_origin = origin
            return None
        res = await self._write_current(self._clamp(tenths), origin)
        if res is not None and res.outcome == C.WRITE_NOT_SENT:
            raise CommandError(C.REFUSE_NOT_CONNECTED)
        return res

    async def _flush_current(self) -> Any:
        value, origin = self._slot, self._slot_origin
        self._slot = None
        if value is None or self._closed:
            return None
        if self._seq is not None:
            self._slot = value
            return None
        try:
            return await self._write_current(self._clamp(value), origin)
        except WriteRefused as err:
            _LOGGER.warning("%s: current %s after the sequence not written: %s", self.station.serial, value, err.key)
            return None

    # --- firmware class probe (evcc initialize) ------------------------------------

    def _probe_value(self) -> int | None:
        """6.1 A like evcc; a station whose minimum is above it gets the
        first 0.1 A value of its range (floored or kept tells the same)."""
        low, high = self.station.current_range_tenths()
        value = 61 if low <= 61 else low // 10 * 10 + 1
        if value < low:
            value += 10
        return value if value <= high else None

    def _learn_class(self, value: int, back: int | None) -> None:
        """evcc initialize: 6.1 read back as 6.0 is the workaround class
        (whole amps), 6.1 the new class. Only from the probe."""
        if back is None:
            return
        if back == value:
            self.station.learned.set("decimal_current", "yes")
        elif back == value // 10 * 10:
            _LOGGER.info("%s: the station keeps whole amps only (%s -> %s)", self.station.serial, value, back)
            self.station.learned.set("decimal_current", "no")

    async def async_initialize(self) -> None:
        """evcc's initialize at every integration start,
        run inside the start-up backoff after the serial check: 91 = 6.1 A,
        then reg 91 read (evcc GetMaxCurrent): 6.0 is the whole-amp
        ("workaround") class, 6.1 the new class. A request that fails raises,
        so the backoff tries again (an exception reply to the write too, as
        evcc). The station's current is put back right after (evcc's next
        setLimit): the site's or the user's current, else the value 91 held
        before; our stop marker (91 = 0) stays the marker."""
        st = self.station
        value = self._probe_value()
        if value is None:
            return
        if self._probe_before is _NOT_READ:
            # Kept from the FIRST try: a retry after the probe write would
            # read our own 61.
            self._probe_before = (await st.async_read(G_SETPOINT)).get(C.REG_CURRENT_SETPOINT)
        before = self._probe_before
        if before == value:
            # Our own probe value left by an earlier runtime whose setup gave
            # up after the probe write: not the station's value.
            before = None
        res = await st.async_write(C.REG_CURRENT_SETPOINT, value, origin=ORIGIN_PROBE)
        if res.outcome != C.WRITE_OK:
            raise ModbusConnectionError(f"firmware probe write: {res.outcome}", False)
        back = (await st.async_read(G_SETPOINT)).get(C.REG_CURRENT_SETPOINT)
        self._learn_class(value, back)
        _LOGGER.info(
            "%s: firmware probe 91 = %s read back %s: decimal_current %s",
            st.serial, value, back, st.learned["decimal_current"],
        )
        try:
            if before in C.STOP_SETPOINT_STORED:
                await st.async_write(C.REG_CURRENT_SETPOINT, C.STOP_SETPOINT, origin=ORIGIN_PROBE, stop_zero=True)
                self._offered = 0
                return
            if self.site_current is None and not self.site_member and st.desired.get("current") is None:
                target = self._clamp(before) if isinstance(before, int) else self._start_current_target()
            else:
                target = self._start_current_target()
            await self._write_current(target, ORIGIN_PROBE, force=True)
        except WriteRefused as err:
            _LOGGER.info("%s: 91 not put back after the firmware probe: %s", st.serial, err.key)

    # --- phases -----------------------------------------------------------

    def _phase_class(self) -> str:
        """Firmware class of evcc's 6.1 A probe: ``new`` keeps
        0.1 A (decimal_current yes), ``whole_amp`` floors (no), else
        ``unknown`` (probe not done yet)."""
        value = self.station.learned["decimal_current"]
        return {"yes": "new", "no": "whole_amp"}.get(value, "unknown")

    def _validate_phases(self, phases: int, origin: str) -> str:
        """The phase write by state, like evcc: no car and
        car connected -> 200 directly; charging -> by firmware class: new ->
        200 directly (evcc driver phases1p3p), whole-amp -> evcc's
        workaround, unknown (6.1 A probe not done yet) -> refused (evcc
        offers no switching before initialize)."""
        st = self.station
        if phases not in (1, 3) or isinstance(phases, bool):
            raise CommandError(C.REFUSE_NOT_ALLOWED)
        if not self.phase_select_available:
            raise CommandError(KEY_PHASE_NA)
        if self._seq is not None:
            raise CommandError(C.REFUSE_BUSY)
        snap = st.snapshot
        status = snap.status
        if status == C.STATUS_STANDBY:
            st.check_write(C.REG_PHASES, phases, origin)
            return "idle"
        # evcc lp.enabled = Enabled() = reg 95 == 1; a station that
        # charges is enabled too. No status-3 or Plug & Charge refusal.
        if status in (C.STATUS_CONNECTED, C.STATUS_FINISHED) and not self.enabled():
            st.check_write(C.REG_PHASES, phases, origin)
            return "car"
        if status in (C.STATUS_CONNECTED, C.STATUS_FINISHED, C.STATUS_STARTING, C.STATUS_CHARGING):
            if snap.phases == phases:
                return "none"
            klass = self._phase_class()
            if klass == "new":
                st.check_write(C.REG_PHASES, phases, origin)
                return "direct"
            if klass == "whole_amp":
                self._otf_prechecks(origin)
                return "otf"
            raise CommandError(KEY_PHASE_CLASS_UNKNOWN, reason="class_unknown")
        raise CommandError(C.REFUSE_STATION_BUSY, status=status)

    def _keep_phase_choice(self, phases: int) -> None:
        """A phase choice that comes with a stop: kept as
        the user's choice and written before 95 = 1 of the next start, like
        evcc core re-asserts the configured phases; never next to the stop."""
        st = self.station
        if phases not in (1, 3) or isinstance(phases, bool):
            raise CommandError(C.REFUSE_NOT_ALLOWED)
        if not self.phase_select_available:
            raise CommandError(KEY_PHASE_NA)
        if st.desired.get("phases") != phases:
            st.desired["phases"] = phases
            self._schedule_save()

    def _otf_prechecks(self, origin: str) -> None:
        """Whole-amp class while charging (the EM2GO workaround): only the
        write gate of its writes; no pre-check of our own."""
        st = self.station
        if self._seq is not None:
            raise CommandError(C.REFUSE_BUSY)
        st.check_write(C.REG_PHASES, 1, origin)
        st.check_write(C.REG_COMMAND, C.COMMAND_STOP, origin)
        st.check_write(C.REG_COMMAND, C.COMMAND_START, origin)

    def _phase_for_start(self) -> int | None:
        """The user's choice differs from the station's 200 at a start: it is
        written before the current and 95 = 1, like evcc core re-asserts the
        configured phases while a car is connected."""
        st = self.station
        desired = st.desired.get("phases")
        if desired not in (1, 3) or not self.phase_select_available:
            return None
        if st.snapshot.phases in (None, desired):
            return None
        return desired

    async def _write_phases(self, phases: int, origin: str) -> WriteResult:
        """One 200 write (evcc phases1p3p; no read-back)."""
        res = await self.station.async_write(C.REG_PHASES, phases, origin=origin)
        if res.outcome == C.WRITE_NOT_SENT:
            raise CommandError(C.REFUSE_NOT_CONNECTED)
        return res

    async def _set_phases(self, phases: int, action: str, origin: str) -> Any:
        st = self.station
        if st.desired.get("phases") != phases:
            st.desired["phases"] = phases  # the user's choice is kept
            self._schedule_save()
        if action == "none":
            return action
        if action in ("idle", "car"):
            # E4: evcc core writes 200 while the charger is disabled.
            if st.snapshot.phases == phases:
                return "not_needed"  # r4 MIN-19
            return await self._write_phases(phases, origin)
        if action == "direct":
            seq = self._new_seq(KIND_PHASE, origin)
            try:
                return await self._run_phase_direct(seq, phases)
            finally:
                await self._end_seq(seq)
        seq = self._new_seq(KIND_PHASE, origin)
        seq.task = self._spawn(self._phase_task(seq, phases))
        return await seq.committed

    async def _run_phase_direct(self, seq: Sequence, phases: int) -> WriteResult:
        """New class while charging (evcc driver phases1p3p: only 200).
        Logic ported from evcc core ``Loadpoint.scalePhases``
        (core/loadpoint.go, MIT, see NOTICE): before going 1 -> 3 phases
        while enabled the current drops to the minimum, so the 1-phase
        current is not applied to all phases; 3 -> 1 has no drop. evcc
        returns when setLimit fails: no 200 then; no read-back. The
        user's current comes back afterwards (evcc's next setLimit). The
        60 s no-measurement follows the switch (evcc phaseSwitchDuration);
        no quota, no settle refusal, no freeze of our own."""
        st = self.station
        origin = seq.origin
        seq.step = "direct"
        dropped = False
        try:
            if phases == 3 and st.snapshot.phases != 3:  # unknown counts as "not 3"
                low = st.current_range_tenths()[0]
                drop = await self._write_current(low, origin, force=True)
                if not _sent(drop):
                    _LOGGER.warning("%s: 91 = %s before 1 -> 3 not taken (%s): no phase switch", st.serial, low,
                                    drop.outcome if drop else None)
                    raise _not_taken(drop)
                dropped = True
            res = await self._write_phases(phases, origin)
        finally:
            if dropped:
                self._phase_current_back(seq)
        return res

    def _phase_current_back(self, seq: Sequence) -> None:
        """After the drop to the minimum before 1 -> 3 (evcc core scalePhases):
        the user's current back, flushed by ``_end_seq`` (evcc's next setLimit)
        - not over a newer slot value, never after a cancelling stop."""
        if self._slot is None and not (seq.cancelled and seq.cancel_reason == "stop"):
            self._slot = self._start_current_target()
            self._slot_origin = seq.origin

    async def _phase_task(self, seq: Sequence, phases: int) -> None:
        seq.commit(StartResult(START_SENT))
        try:
            await self._run_phase_switch(seq, phases)
        except SequenceCancelled:
            pass  # a stop or the unload cancelled it
        except WriteRefused as err:
            _LOGGER.info("%s: phase switch not done: %s", self.station.serial, err.key)
        except Exception:
            _LOGGER.exception("%s: phase switch failed", self.station.serial)
        finally:
            await self._end_seq(seq)

    # --- start --------------------------------------------------------------

    def _start_prechecks(self, origin: str) -> None:
        """Start refusals like the OCPP integration:
        a faulted station (reg 4 != 0 or status 7; while it lasts, not until
        an unplug), and after an empty session until the cable is re-plugged
        (OCPP "replug"); evcc core enables only with a car connected. No 93
        gate (evcc sends 95 = 1 anyway) and no start quota. All
        synchronous."""
        st = self.station
        snap = st.snapshot
        if self._seq is not None:
            raise CommandError(C.REFUSE_BUSY)
        if snap.error or snap.status == C.STATUS_FAULT:
            # a language-neutral detail: the screen code or the reg-4 code
            code = snap.error
            key = KEY_FAULTED_REPLUG if code in C.REPLUG_FAULTS else KEY_FAULTED
            raise CommandError(key, detail=C.SCREEN_CODE.get(code) or str(code or snap.status))
        st.check_write(C.REG_COMMAND, C.COMMAND_START, origin)
        if snap.cable != 1:
            raise CommandError(KEY_NO_CAR)
        if st.session_flags.get("empty_session"):
            raise CommandError(KEY_REPLUG)
        # From 2 or 6 (OCPP: a start from Finishing is sent; the automatic
        # callers decide "plugged idle" themselves).
        if snap.status not in (C.STATUS_CONNECTED, C.STATUS_FINISHED):
            raise CommandError(C.REFUSE_STATION_BUSY, status=snap.status)

    async def _start(self, origin: str) -> StartResult:
        """evcc core order: phases while disabled (if the choice
        differs) -> the current -> Enable(true) = 95 = 1, each one write. On
        the whole-amp class the EM2GO driver's Enable(true) follows: 200 = 1
        again on one phase, then the current (behaviour of em2go.go)."""
        st = self.station
        seq = self._new_seq(KIND_START, origin)
        try:
            phase_target = self._phase_for_start()
            if phase_target is not None:
                seq.step = "phases"
                r200 = await self._write_phases(phase_target, origin)
                if not _sent(r200):
                    raise _not_taken(r200)
            seq.step = "current"
            target = self._start_current_target(notify=True)
            if seq.cancelled:
                raise CommandError(C.REFUSE_BUSY, reason="cancelled")
            r91 = await self._write_current(target, origin, force=True)
            if not _sent(r91):
                raise _not_taken(r91)
            seq.step = "start"
            if seq.cancelled:
                raise CommandError(C.REFUSE_BUSY, reason="cancelled")
            r95 = await st.async_write(C.REG_COMMAND, C.COMMAND_START, origin=origin)
            if not _sent(r95):
                raise _not_taken(r95)
            self._after_start_sent(r95)
            if self._phase_class() == "whole_amp":
                # fw < 1.3 loses 1 phase and the current on enable (EM2GO driver)
                if st.snapshot.phases == 1:
                    await st.async_write(C.REG_PHASES, 1, origin=origin)
                await self._write_current(self._start_current_target(), origin, force=True)
            return StartResult(START_SENT, r95)
        finally:
            await self._end_seq(seq)

    def _after_start_sent(self, r95: WriteResult) -> None:
        """Bookkeeping of our 95 = 1."""
        at = r95.at
        if self._last_stop_at is None or self._last_stop_at < at:
            self._own_start_at = at
            self._empty_watch = at
            self._delivered_since_start = False
            self._set_stop_not_working(False)
            if self.energy is not None:
                # Only without a later stop: a stop sent meanwhile keeps its record reason (MIN 4).
                self.energy.note_start()

    # --- stop ----------------------------------------------------------------

    async def _stop(self, origin: str) -> StopResult:
        """evcc Enable(false) of the EM2GO driver: 95 = 2 then 91 = 0,
        when reg 95 reads 1 or the station charges; nothing for an idle
        station (OCPP stop does nothing there either). No 93 gate, no fresh
        read, no verification."""
        seq = self._seq
        if seq is not None:
            self._cancel(seq, "stop")  # the stop has priority
        if not self._may_run():
            _LOGGER.info("%s: stop (%s) not needed: station idle", self.station.serial, origin)
            return StopResult(STOP_NOT_NEEDED)
        res = await self.station.async_write(C.REG_COMMAND, C.COMMAND_STOP, origin=origin)
        if res.outcome == C.WRITE_NOT_SENT:
            return StopResult(STOP_FAILED, res)
        self._stop_bookkeeping(res, origin, note_energy=True, reason=_energy_reason(origin))
        if res.outcome == C.WRITE_REJECTED:
            return StopResult(STOP_FAILED, res)
        await self._write_stop_zero(res, origin)
        return StopResult(STOP_SENT, res)

    async def _write_stop_zero(self, res: WriteResult, origin: str) -> None:
        """ (evcc EM2GO driver Enable(false), both firmware classes):
        91 = 0 right after an acknowledged 95 = 2; the next start writes the
        current again (evcc: offered current 0 after a disable)."""
        if not res.acknowledged:
            return
        try:
            await self.station.async_write(C.REG_CURRENT_SETPOINT, C.STOP_SETPOINT, origin=origin, stop_zero=True)
        except WriteRefused as err:
            _LOGGER.info("%s: 91 = 0 after the stop not sent: %s", self.station.serial, err.key)
        self._offered = 0

    def _stop_bookkeeping(self, res: WriteResult, origin: str, *, note_energy: bool, reason: str | None = None) -> None:
        if res.outcome == C.WRITE_NOT_SENT:
            return
        self._last_stop_at = res.at
        self._own_start_at = None
        self._empty_watch = None
        if note_energy and self.energy is not None:
            self.energy.note_stop(reason or _energy_reason(origin))
        self._schedule_save()

    def _set_stop_not_working(self, on: bool) -> None:
        """shown, not acted on - no learning, no notification, no other
        feature looks at it."""
        if on != self._stop_not_working:
            self._stop_not_working = on
            self.station.set_problem(PROBLEM_STOP_NOT_WORKING, on)

    # --- Plug & Charge --------------------------------------------------------------

    async def async_set_plug_and_charge(self, on: bool, origin: str = ORIGIN_USER) -> Any:
        """P&C switch: ON -> 93 = 0, OFF -> 93 = 1; stored as the user's
        choice; one write (no fresh read)."""
        st = self.station
        value = C.MODE_PLUG_AND_CHARGE if on else C.MODE_COMMAND
        if self._seq is not None:
            raise CommandError(C.REFUSE_BUSY)
        st.check_write(C.REG_MODE, value, origin)
        if st.desired.get("mode") != value:
            st.desired["mode"] = value
            self._schedule_save()
        res = await st.async_write(C.REG_MODE, value, origin=origin)
        if res.outcome == C.WRITE_NOT_SENT:
            raise CommandError(C.REFUSE_NOT_CONNECTED)
        if res.outcome == C.WRITE_OK and value == C.MODE_COMMAND:
            st.set_problem(C.PROBLEM_PNC_RESET, False)  # the repair clears when 93 reads 1
        return res

    async def async_pnc_off_and_stop(self, origin: str = ORIGIN_USER) -> Any:
        """Button "Turn Plug & Charge off and stop": 93:= 1, then the stop."""
        res = await self.async_set_plug_and_charge(False, origin)
        if res is not None and res.outcome != C.WRITE_OK:
            raise CommandError(C.REFUSE_NOT_CONNECTED)
        self._user_cmd_at = self._clock()
        return await self._stop(origin)

    async def async_pnc_restore(self) -> Any:
        """Fix flow of the ``pnc_reset`` repair: 93 = 1 through the gate."""
        return await self.async_set_plug_and_charge(False, ORIGIN_USER)

    # --- schedule, auto start like OCPP --------------------------------

    def _plugged_idle(self) -> bool:
        """OCPP ``plugged_idle`` (logic.py) on reg 0:
        the cable is in, status 2 or 6, and no charge since the plug-in. A
        2 / 6 after a charge is the finished state (OCPP "Finishing after
        Charging"): never started into again by itself."""
        snap = self.station.snapshot
        return snap.cable == 1 and snap.status in (C.STATUS_CONNECTED, C.STATUS_FINISHED) and not self._charged_since_plug

    async def async_schedule_tick(self, now: datetime | None = None) -> None:
        """Every minute, like OCPP ``schedule_tick``: stop
        at the end of the window (edge) when a charge runs; at the opening one
        start also from a finished charge with the cable still in; while open
        a start when plugged idle (180 s between automatic starts). No attempt
        count, no "user stopped in this window" rule, no missed-stop retry."""
        local = now or self._wall()
        sched = self.options.schedule
        is_open = sched.active(local)
        was_open, self._sched_open = self._sched_open, is_open
        if was_open != is_open:
            self._schedule_save()
        st = self.station
        if not self._startup_done or st.snapshot.link != C.LINK_CONNECTED:
            return  # OCPP: not adopted / not connected
        charging = self._may_run()
        if charging and was_open and not is_open and sched.enabled and sched.stop_at_end:
            _LOGGER.info("%s: schedule window closed, stopping", st.serial)
            await self._auto_apply(Target(enabled=False), ORIGIN_SCHEDULE)
            return
        snap = st.snapshot
        if (
            is_open
            and was_open is False
            and not charging
            and snap.cable == 1
            and snap.status in (C.STATUS_CONNECTED, C.STATUS_FINISHED)
        ):
            # The window has just opened and the cable stayed in after an
            # earlier charge: once, at the opening only (OCPP).
            await self._automatic_start(ORIGIN_SCHEDULE)
            return
        if is_open and not charging and self._plugged_idle():
            await self._automatic_start(ORIGIN_SCHEDULE)

    def _check_plugged_idle_start(self, status: int | None) -> None:
        """OCPP ``_after_status``: the station turns plugged idle -> the
        automatic start (auto start; else an open schedule window starts now,
        not at the next minute). The 180 s cooldown is shared."""
        if status == self._prev_status or self._seq is not None or not self._plugged_idle():
            return
        if self.options.auto_start:
            self._spawn(self._automatic_start(ORIGIN_AUTO_START))
        elif self.options.schedule.active(self._wall()):
            self._spawn(self._automatic_start(ORIGIN_SCHEDULE))

    # --- events of the station ------------------------------------------------------------

    def _on_event(self, name: str, data: dict[str, Any]) -> None:
        st = self.station
        now = self._clock()
        if name == C.EVENT_PLUGGED:
            self._plug_token += 1
            self._new_plug_cycle()
        elif name == C.EVENT_UNPLUGGED:
            self._plug_token += 1
            self._new_plug_cycle()
            for problem in _SESSION_PROBLEMS:
                st.set_problem(problem, False)
        elif name == C.EVENT_RESTARTED:
            # OCPP: a charge interrupted by the restart is started again when the
            # option is on (firmware 3.1 >= 2.8, so always allowed).
            resume = bool(data.get("record_open_before")) and self.options.resume_after_power_loss
            self._resume_until = now + RESUME_WAIT_S
            self._notify(
                "station_restarted", signals=data.get("signals"), resume=resume,
                record_open_before=bool(data.get("record_open_before")),
            )
            self._resume_pending = resume


    def _new_plug_cycle(self) -> None:
        self._own_start_at = None
        self._empty_watch = None
        self._delivered_since_start = False
        self._charged_since_plug = False
        self._set_stop_not_working(False)

    # --- the supervisor (every poll) -----------------------------------------------------------

    def _observe_values(self, t: float, status: int | None, max_i: float, delivering: bool) -> None:
        if self._last_obs is None or t >= self._last_obs[0]:
            self._last_obs = (t, status, max_i)
        if delivering:
            self._delivered_since_start = True

    def _on_snapshot(self, snap: Any) -> None:
        if snap.time is None or snap.time == self._last_snap_time:
            return  # a failed or skipped poll
        self._last_snap_time = snap.time
        st = self.station
        status = snap.status
        max_i = max((c for c in snap.currents if c is not None), default=0.0)
        self._observe_values(snap.time, status, max_i, snap.delivering)
        record_open = st.record_open
        if record_open and not self._prev_record_open:
            self._record_token += 1
        self._adopt_current(snap)
        if not self._startup_done:
            # Start-up ends with the first complete poll; a running charge is
            # adopted as it is - no write of any kind.
            self._startup_done = True
            if status in (C.STATUS_STARTING, C.STATUS_CHARGING):
                _LOGGER.info("%s: charge in progress adopted (status %s), nothing written", st.serial, status)
        else:
            try:
                self._supervise(snap, status, max_i, record_open)
            except Exception:  # the supervisor must never break the poll
                _LOGGER.exception("%s: supervisor failed", st.serial)
        self._prev_status = status
        self._prev_record_open = record_open

    def _adopt_current(self, snap: Any) -> None:
        """evcc syncCharger: while enabled, a station current more than
        0.23 A away from the last value written is adopted, so the next
        equal request is written again (not
        restored)."""
        value = snap.registers.get(C.REG_CURRENT_SETPOINT)
        if not isinstance(value, int) or value in C.STOP_SETPOINT_STORED or not self.enabled():
            return
        if self._offered is None or abs(value - self._offered) >= ADOPT_DELTA_TENTHS:
            self._offered = value

    def _supervise(self, snap: Any, status: int | None, max_i: float, record_open: bool) -> None:
        st = self.station
        now = self._clock()
        running = status in (C.STATUS_STARTING, C.STATUS_CHARGING)
        if running:
            self._charged_since_plug = True
        # self start: 3/4 without our command, 93 = 1, no record open before.
        if (
            running
            and self._prev_status not in (C.STATUS_STARTING, C.STATUS_CHARGING)
            and not self._prev_record_open
            and snap.mode == C.MODE_COMMAND
            and self._seq is None
            and not self._own_start_recent(now)
        ):
            self._charging_without_session = True  # shown by the HA layer while it lasts
        if self._charging_without_session and not running and not record_open:
            self._charging_without_session = False
        # status 6 after our stop (not inside our own phase switch).
        if (
            status == C.STATUS_FINISHED
            and self._prev_status != C.STATUS_FINISHED
            and self._last_stop_at is not None
            and now - self._last_stop_at <= C.STOP_VERIFY_S + C.STOP_LATE_S
            and not self._in_phase_switch
        ):
            self._notify("finished_replug")
        self._check_stop_not_working(snap, running, now)
        self._check_empty_session(status, now)
        self._check_plugged_idle_start(status)
        if self._resume_pending and self._seq is None:
            # The box may show 10 (booting) or zeros first: wait for status 2
            # (older defect), give up once it resumed by itself,
            # the car left, or after RESUME_WAIT_S.
            if running or snap.cable == 0 or now > self._resume_until:
                self._resume_pending = False
            elif snap.cable == 1 and status == C.STATUS_CONNECTED:
                self._resume_pending = False
                self._spawn(self._automatic_start(ORIGIN_RESUME))

    def _check_stop_not_working(self, snap: Any, running: bool, now: float) -> None:
        """our stop went out at least 30 s ago, no start of ours since,
        and the poll still shows current flowing in status 4 -> shown as
        "stop not working"; cleared once the station is out of 3/4, on
        unplug or by our next start. Nothing else is done."""
        if not running:
            self._set_stop_not_working(False)
            return
        stop = self._last_stop_at
        if (
            stop is not None
            and now - stop >= C.STOP_VERIFY_S
            and (self._own_start_at is None or self._own_start_at < stop)
            and snap.delivering
            and not self._in_phase_switch
        ):
            self._set_stop_not_working(True)

    def _own_start_recent(self, now: float) -> bool:
        t = self._own_start_at
        return t is not None and now - t <= C.START_CURRENT_S + C.STOP_VERIFY_S

    def _check_empty_session(self, status: int | None, now: float) -> None:
        """OCPP's empty-session watch (``charger.py _watch_start``): 180 s after our 95 = 1, once - if the station still
        shows 2 or 6 and no current flowed since, the car did not take the
        charge: flag, notification and one stop (the start is then refused
        until a re-plug, OCPP "replug"). A car pause in status 4 is left alone."""
        st = self.station
        started = self._empty_watch
        if started is None or now - started < C.EMPTY_SESSION_S:
            return
        self._empty_watch = None  # once, like OCPP
        if self._delivered_since_start or status not in (C.STATUS_CONNECTED, C.STATUS_FINISHED):
            return
        if st.session_flags.get("empty_session"):
            return
        _LOGGER.warning("%s: the car did not take the charge", st.serial)
        st.session_flags["empty_session"] = True
        st.set_problem(PROBLEM_EMPTY_SESSION, True)
        self._notify("empty_session")
        if self.options.empty_session_stop:
            self._spawn(self._auto_apply(Target(enabled=False), ORIGIN_EMPTY_SESSION))

__all__ = [
    "CommandError",
    "CommandOptions",
    "Commander",
    "NOTIFY_KINDS",
    "REFUSAL_KEYS",
    "STORE_SECTION",
    "StartResult",
    "StopResult",
    "Target",
]
