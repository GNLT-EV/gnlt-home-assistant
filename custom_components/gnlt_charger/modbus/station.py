"""Station layer of the ModBus mode (no Home Assistant imports).

Transport like evcc: no reload gap and no client backoff; no block reads
(every register is its own request), so no group is split after an
exception; the setup waits for a first answer (backoff like evcc's
NewEm2Go) and raises ConfigEntryNotReady after 30 s instead of loading
without one.

One ``Station`` per config entry drives one ``ModbusClient``: it polls the
registers in tiers, keeps the state model (status, cable, error, currents,
phases in use, plug events), checks the identity of whoever answers, probes
capabilities, tells a station restart from foreign single changes, judges the
link only by valid ModBus replies, and owns the single write gate every
command must pass. Commands and sequences, energy and the
Home Assistant entities build on this API:

* ``snapshot`` / ``subscribe`` / ``subscribe_events``: the latest state and
  change notifications;
* ``async_start`` / ``async_stop`` / ``async_tick`` / ``async_refresh``;
* ``async_write``: one FC16 request like evcc's EM2GO driver - the gate
  (allow-list, current range, identity, a status evcc accepts), the serial
  read on a new connection and the write log; no fresh read, no
  read-back, no hold;
* ``store_data`` / ``async_save`` / ``async_remove_store`` and ``diagnostics``.

Times use one monotonic clock shared with the client (``time.monotonic`` by
default; tests pass a simulated one).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from ..const import DOMAIN
from . import const as C
from .client import (
    KIND_CONNECT_FAILED,
    KIND_CONNECT_TIMEOUT,
    KIND_REFUSED,
    KIND_UNREACHABLE,
    STATE_CLOSED,
    STATE_CONNECTED,
    ModbusClosedError,
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
)
from .profiles import LearnedCaps

_LOGGER = logging.getLogger(__name__)


STORE_VERSION = 1
CONTROLLER_MIN_S = 30.0  # stage-2 decision loops never run faster (S-12)
_CONNECT_KINDS = {KIND_REFUSED, KIND_CONNECT_TIMEOUT, KIND_UNREACHABLE, KIND_CONNECT_FAILED}
# Keys of the station's own Store content; a section of another layer never replaces them.
_OWN_STORE_KEYS = frozenset(
    {"version", "serial", "desired", "capabilities", "flags",
     "session_flags", "snapshot"}
)


def store_key(serial: str) -> str:
    """One HA Store per station serial."""
    return f"{DOMAIN}.modbus_{serial}"


# --- decoding -------------------------------------------------------------------


def decode_serial(words: list[int] | tuple[int, ...]) -> str | None:
    """Regs 38, 40, 42 hold the serial as BCD words: 0x1012 0x3456 0x7891 -> 101234567891.

    Three single reads (38, 40, 42); never 38..53 as UTF-16 text (evcc).
    Anything that is not 12 decimal digits is not a GNLT serial.
    """
    if len(words) != 3:
        return None
    digits = "".join(f"{w:04X}" for w in words)
    return digits if digits.isdigit() else None


def decode_u32(raw: dict[int, int], reg: int) -> int | None:
    """U32 of a register pair, high word first; both words come from one read."""
    if reg in raw and reg + 1 in raw:
        return (raw[reg] << 16) | raw[reg + 1]
    return None


def status_key(code: int | None) -> str | None:
    """Reg 0 to its state key; unknown codes (e.g. 31) are ``unknown``, never an error."""
    if code is None:
        return None
    return C.STATUS_KEYS.get(code, C.STATUS_UNKNOWN)


def error_key(code: int | None) -> str | None:
    if code is None:
        return None
    return C.ERROR_KEYS.get(code, C.ERROR_UNKNOWN)


def _scaled(raw: dict[int, int], reg: int, scale: float) -> float | None:
    value = raw.get(reg)
    return None if value is None else round(value * scale, 3)


# --- small parts -------------------------------------------------------------------


@dataclass
class StationTiming:
    """Timing constants; tests over real TCP scale them down."""

    poll_s: float = C.POLL_S
    min_poll_s: float = C.MIN_POLL_S
    slow_poll_s: float = C.SLOW_POLL_S
    identity_retries: int = C.IDENTITY_RETRIES
    identity_retry_delay_s: float = C.IDENTITY_RETRY_DELAY_S
    startup_initial_s: float = C.STARTUP_BACKOFF_INITIAL_S
    startup_max_s: float = C.STARTUP_BACKOFF_MAX_S
    startup_give_up_s: float = C.STARTUP_BACKOFF_GIVE_UP_S
    link_down_polls: int = C.LINK_DOWN_POLLS
    unavailable_after_s: float = C.UNAVAILABLE_AFTER_S
    lost_after_s: float = C.LOST_NOTIFY_AFTER_S
    hint_after_attempts: int = C.LINK_HINT_AFTER_ATTEMPTS
    hint_after_s: float = C.LINK_HINT_AFTER_S
    refresh_min_s: float = C.REFRESH_MIN_S
    settle_s: float = C.SETTLE_S
    stop_timeout_s: float = 1.0
    save_delay_s: float = 60.0  # debounced Store save; at most one pending at a time


class StoreLike(Protocol):
    """The subset of ``homeassistant.helpers.storage.Store`` used here."""

    async def async_load(self) -> Any: ...

    async def async_save(self, data: Any) -> None: ...

    async def async_remove(self) -> None: ...


class PhaseCounter:
    """Phases carrying the charge; copy of the OCPP rule (``charger.py``
    ``_count_phases``, kept separate so the OCPP file stays untouched).

    A phase counts from 1.0 A and stops counting below 0.7 A; a change is
    taken when two readings in a row agree. Never inferred from voltages
    (EM2GO shows ~236 V on idle phases, A3B 0 V).
    """

    def __init__(self) -> None:
        self.phases_in_use: int | None = None
        self._on: set[str] = set()
        self._seen: int | None = None
        self._confirmed = False
        self._expected: int | None = None  # after our own 200 write
        self._expected_until = 0.0

    def expect(self, phases: int, until: float) -> None:
        """Our own phase switch was sent: a change to ``phases`` up to
        ``until`` is expected, not a change nobody ordered; after that it is
        logged again."""
        self._expected = phases
        self._expected_until = until

    def update(self, current: dict[str, float], now: float | None = None) -> bool:
        if self._expected is not None and now is not None and now > self._expected_until:
            self._expected = None  # the switch did not show in time: expectation gone
        previous = self.phases_in_use
        active = 0
        for ph, amps in current.items():
            was_on = previous is not None and ph in self._on
            if amps > C.PHASE_ACTIVE_A or (was_on and amps > C.PHASE_OFF_A):
                active += 1
        if not active:
            return False
        seen, self._seen = self._seen, active
        if active == previous:
            self._confirmed = self._confirmed or seen == active
            if active == self._expected and seen == active:
                self._expected = None
            return False
        if previous is not None and seen != active:
            return False
        self._on = {ph for ph, a in current.items() if a > C.PHASE_ACTIVE_A}
        if active == self._expected:
            _LOGGER.debug("charging on %s phase(s) as switched (was %s), currents %s A", active, previous, current)
            self._expected = None
        elif previous is not None and self._confirmed:
            _LOGGER.info("charging on %s phase(s) instead of %s, currents %s A", active, previous, current)
        self.phases_in_use = active
        self._confirmed = False
        return True


def is_automatic(origin: str) -> bool:
    """User commands and HA automations are not automatic."""
    return origin not in C.USER_ORIGINS


class WriteRefused(Exception):
    """A write did not pass the gate; nothing was sent. ``key`` is the
    translation key of the error the HA layer raises."""

    def __init__(self, key: str, **placeholders: Any) -> None:
        self.key = key
        self.placeholders = placeholders
        super().__init__(key if not placeholders else f"{key} {placeholders}")


@dataclass(frozen=True)
class WriteResult:
    """One write as evcc's driver sees it: ``ok`` = the station acknowledged
    the request; no read-back."""

    register: int
    value: int
    origin: str
    outcome: str  # const WRITE_*
    acknowledged: bool
    at: float
    error: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """State after the latest poll; register values as read and scaled."""

    time: float | None = None  # clock time of the poll the data came from
    registers: dict[int, int] = field(default_factory=dict)
    status: int | None = None
    status_key: str | None = None
    cable: int | None = None
    error: int | None = None
    error_key: str | None = None
    currents: tuple[float | None, float | None, float | None] = (None, None, None)
    voltages: tuple[float | None, float | None, float | None] = (None, None, None)
    power_w: int | None = None
    per_phase_power_raw: tuple[int | None, int | None, int | None] = (None, None, None)
    phase_power_w: tuple[float | None, float | None, float | None] = (None, None, None)
    meter_kwh: float | None = None
    duration_s: int | None = None
    setpoint_a: float | None = None
    min_current_a: float | None = None
    max_current_a: float | None = None
    cable_max_a: float | None = None
    fallback_current_a: float | None = None
    comm_timeout_s: int | None = None
    mode: int | None = None
    last_command: int | None = None  # echo of the last 95 write only
    phases: int | None = None
    delivering: bool = False
    phases_in_use: int | None = None
    plugged: bool | None = None
    waiting_for_start: bool = False
    link: str = C.LINK_RECONNECTING
    identity: str = C.IDENTITY_UNVERIFIED
    measurements_available: bool = False
    problems: frozenset[str] = frozenset()
    flags: dict[str, Any] = field(default_factory=dict)


class _PollException(Exception):
    """A required register answered a ModBus exception: the poll has no data."""

    def __init__(self, err: ModbusExceptionError) -> None:
        self.err = err


class _NewConnection(Exception):
    """The client opened a new socket in the middle of a poll."""


# --- the station --------------------------------------------------------------------


class Station:
    """Poller, state model and write gate of one ModBus station."""

    def __init__(
        self,
        client: Any,
        serial: str,
        *,
        restored: dict[str, Any] | None = None,
        store: StoreLike | None = None,
        preset_max_a: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timing: StationTiming | None = None,
    ) -> None:
        self._client = client
        self.serial = serial
        self._clock = clock
        self._sleep = sleep
        self.timing = timing or StationTiming()
        self._store = store
        self.preset_max_a = preset_max_a
        self.learned = LearnedCaps()
        self.desired: dict[str, int | None] = {"current": None, "mode": None, "phases": None}
        self._caps_phases: bool | None = None
        # evcc initialize reads reg 200 once: decided at the first contact
        # (or the first full poll) of this instance, never again (exactly evcc).
        self._phases_decided = False
        self._baseline: dict[str, Any] | None = None
        self._restored_baseline = False
        self.session_flags: dict[str, Any] = {}
        self._problems: set[str] = set()

        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._tick_running = False
        # ``record_open`` (property): the energy/record layer's value
        # OR'd with a conservative one of our own - status 3/4 seen since the
        # plug-in and no own stop sent since. A stop of an
        # idle station with neither and no own start pending is a success
        # without a write.
        self._record_open_ext = False
        self._charge_seen = False
        self._start_pending = False
        self._started_at = clock()
        self._stopped = False

        # connection / identity
        self.identity = C.IDENTITY_UNVERIFIED
        self._identity_conn: int | None = None
        self._data_conn: int | None = None
        self._serial_seen: str | None = None

        # link health
        self._last_valid: float | None = None
        self._last_good_poll: float | None = None
        self._failed_polls = 0
        self._fail_run_start: float | None = None
        self._fail_run_attempts = 0
        self.last_failure: str | None = None
        self.last_failure_text: str | None = None
        self._errors: deque[float] = deque()
        self._link_state = C.LINK_RECONNECTING
        self._lost_announced = False
        self._skipped_ticks = 0
        self._last_tier_b: float | None = None
        self._last_refresh: float | None = None

        # state model
        self._raw: dict[int, int] = {}
        self._snap = Snapshot()
        self._delivering = False
        self._phases = PhaseCounter()
        self._phase_suppress_until = 0.0
        self._limits_conn: int | None = None  # connection whose 32/34/36 the first contact read
        self._plugged: bool | None = None
        self._mismatch_polls = 0
        self._flat_low_polls = 0
        self._capped_polls = 0
        self._last_max_i: float | None = None
        self._foreign: deque[dict[str, Any]] = deque(maxlen=20)
        self._restarts: deque[dict[str, Any]] = deque(maxlen=20)
        self._first_poll_done = False
        self._own_writes: dict[int, float] = {}
        self._pending_restore = False
        # The 91 a restore writes instead of desired when it returns a value (the
        # GNLT site's current while it controls the station, Commander.site_current):
        # a restore never writes more than the site last sent.
        self.restore_override: Callable[[int | None], int | None] | None = None
        self._restore_wait_ready = False  # restore refused while status 9/10: retried after
        self._booting = False
        self._reg36_ignored_logged = False

        # diagnostics rings
        self._ring: deque[dict[str, Any]] = deque(maxlen=C.RING_SIZE)
        self._write_log: deque[dict[str, Any]] = deque(maxlen=C.RING_SIZE)
        self._refusals: deque[dict[str, Any]] = deque(maxlen=50)

        self._listeners: list[Callable[[Snapshot], Any]] = []
        self._event_listeners: list[Callable[[str, dict[str, Any]], Any]] = []
        self._controllers: list[dict[str, Any]] = []

        # Sections of the per-serial Store owned by other layers (energy):
        # name -> (dump, load). One Store per serial, so
        # their state is saved and restored together with the station's.
        self._store_sections: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}
        self._restored_data: dict[str, Any] | None = None
        self._save_due: float | None = None
        self._save_tasks: set[asyncio.Task] = set()

        self._loaded = restored is not None
        if restored:
            self._restore(restored)

    # --- persisted state --------------------------------------------------------

    def _restore(self, data: dict[str, Any]) -> None:
        # A stored "control_active" (the removed master switch) and "profile"
        # are ignored; so is a stored firmware
        # class: the 6.1 A probe decides it at every start.
        # A stored "learned" is ignored as well.
        desired = data.get("desired") or {}
        for key in self.desired:
            value = desired.get(key)
            self.desired[key] = value if isinstance(value, int) else None
        caps = data.get("capabilities") or {}
        if isinstance(caps.get("phases"), bool):
            self._caps_phases = caps["phases"]
        # A stored "breaker" (the removed write breaker) is ignored.
        # a stored "flags.relay_welded_block" (the sticky start block) is ignored
        self.session_flags = dict(data.get("session_flags") or {})
        snapshot = data.get("snapshot")
        if isinstance(snapshot, dict):
            self._baseline = {**snapshot, "time": None}
            self._restored_baseline = True
        self._restored_data = data
        for name, (_dump, load) in self._store_sections.items():
            load(data.get(name))

    def add_store_section(self, name: str, dump: Callable[[], Any], load: Callable[[Any], None]) -> None:
        """Keep another layer's state in this station's Store (energy).

        ``dump()`` is saved under ``name`` with every save; ``load`` gets the
        stored value at once if the Store was already restored, else when it is.
        """
        self._store_sections[name] = (dump, load)
        if self._restored_data is not None:
            load(self._restored_data.get(name))

    def store_data(self) -> dict[str, Any]:
        sections = {name: dump() for name, (dump, _load) in self._store_sections.items()}
        return {
            "version": STORE_VERSION,
            "serial": self.serial,
            "desired": dict(self.desired),
            "capabilities": {"phases": self._caps_phases},
            "session_flags": dict(self.session_flags),
            # Baseline for restart detection after an HA restart.
            "snapshot": {k: v for k, v in (self._baseline or {}).items() if k != "time"} or None,
        } | {name: value for name, value in sections.items() if name not in _OWN_STORE_KEYS}

    async def async_load(self) -> None:
        """Restore from the Store before the first poll (if not given restored data)."""
        if self._store is None:
            return
        data = await self._store.async_load()
        self._loaded = True
        if isinstance(data, dict):
            self._restore(data)

    def _schedule_save(self) -> None:
        """Debounced save. HA's ``Store.async_delay_save`` moves the write
        time forward on every call, so calling it on every poll would never
        write while polling goes on: schedule
        only when no save is pending. The next one is scheduled a poll
        interval after the write was due, so a poll landing on the deadline
        (60 s is a multiple of the poll interval) cannot push the write the
        Store is just about to do. The data is taken at write time."""
        delay_save = getattr(self._store, "async_delay_save", None)
        if delay_save is None:
            return
        now = self._clock()
        if self._save_due is not None and now < self._save_due:
            return
        # writes land at most save_delay_s apart. The Store
        # writes ``delay`` after scheduling; the next scheduling waits one
        # more poll interval so a poll landing on the deadline never pushes
        # the pending write (HA moves it on every call): interval =
        # delay + poll... delay + 2 x poll <= save_delay_s.
        poll = max(self.timing.poll_s, self.timing.min_poll_s)
        delay = max(self.timing.save_delay_s - 2 * poll, 1.0)
        self._save_due = now + delay + poll
        delay_save(self.store_data, delay)

    def _save_soon(self) -> None:
        """Save now (learned changes); the debounced save covers the rest."""
        if self._store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._schedule_save()
            return
        task = loop.create_task(self._save_logged())
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)

    async def _save_logged(self) -> None:
        try:
            await self.async_save()
        except Exception:
            _LOGGER.exception("%s: saving the station state failed", self.serial)

    async def async_save(self) -> None:
        if self._store is not None:
            await self._store.async_save(self.store_data())

    async def async_remove_store(self) -> None:
        """Hook for ``async_remove_entry``: a re-added entry must start clean
        (nothing learned), so the per-serial Store goes too."""
        if self._store is not None:
            await self._store.async_remove()

    # --- read-only state --------------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        return self._snap

    @property
    def record_open(self) -> bool:
        """A charge may be running: the record layer's open record, or
        status 3/4 seen since the plug-in with no own stop sent since."""
        return self._record_open_ext or self._charge_seen

    @record_open.setter
    def record_open(self, value: bool) -> None:
        # The record layer closing its record (own stop, unplug, status 6,
        # restart) also ends our own conservative "charge seen".
        if self._record_open_ext and not value:
            self._charge_seen = False
        self._record_open_ext = bool(value)

    @property
    def link_state(self) -> str:
        return self._link_state

    @property
    def phase_capability(self) -> bool | None:
        return self._caps_phases

    @property
    def polled_since_connect(self) -> bool:
        """A complete good poll exists on the current connection."""
        return (
            self.identity == C.IDENTITY_OK
            and self._data_conn is not None
            and self._data_conn == self._client.connections
        )

    @property
    def last_close_monotonic(self) -> float | None:
        return self._client.last_close_monotonic

    @property
    def link_hint(self) -> str | None:
        """Failure class for "check the menu / network" texts, only after a
        persistent run of failures (live N3: single refusals mean nothing)."""
        if (
            self._fail_run_start is not None
            and self._fail_run_attempts >= self.timing.hint_after_attempts
            and self._clock() - self._fail_run_start >= self.timing.hint_after_s
        ):
            return self.last_failure
        return None

    def current_range_tenths(self) -> tuple[int, int]:
        """Allowed 91 range: [reg 34, min(reg 32, reg 36, preset max)], never below 6 A.

        Defaults 6.0-16.0 A while unknown; reg 36 = 0 or below reg 34 is
        ignored (logged once).
        """
        raw = self._raw
        low = max(raw.get(C.REG_MIN_CURRENT) or C.DEFAULT_MIN_TENTHS, C.CURRENT_FLOOR_TENTHS)
        high = [raw.get(C.REG_MAX_CURRENT) or C.DEFAULT_MAX_TENTHS]
        cable = raw.get(C.REG_CABLE_MAX)
        if cable is not None:
            if cable >= low:
                high.append(cable)
            elif not self._reg36_ignored_logged:
                self._reg36_ignored_logged = True
                _LOGGER.info("%s: reg 36 (cable max) = %s ignored (0 or below the minimum)", self.serial, cable)
        if self.preset_max_a:
            high.append(int(round(self.preset_max_a * 10)))
        return low, min(high)

    # --- subscriptions ----------------------------------------------------------------

    def subscribe(self, callback: Callable[[Snapshot], Any]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def subscribe_events(self, callback: Callable[[str, dict[str, Any]], Any]) -> Callable[[], None]:
        self._event_listeners.append(callback)
        return lambda: (
            self._event_listeners.remove(callback) if callback in self._event_listeners else None
        )

    def add_controller(self, callback: Callable[[Snapshot], Any], interval_s: float) -> Callable[[], None]:
        """A decision loop with its own interval. It receives the
        latest snapshot and never sends ModBus requests itself."""
        if interval_s < CONTROLLER_MIN_S:
            raise ValueError(f"controller interval below {CONTROLLER_MIN_S} s")
        entry = {"cb": callback, "interval": interval_s, "next": self._clock() + interval_s}
        self._controllers.append(entry)
        return lambda: self._controllers.remove(entry) if entry in self._controllers else None

    def _emit(self, name: str, **data: Any) -> None:
        data.setdefault("first_poll", not self._first_poll_done)
        for cb in list(self._event_listeners):
            try:
                cb(name, data)
            except Exception:  # a listener must never break the poll
                _LOGGER.exception("%s: event listener failed for %s", self.serial, name)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb(self._snap)
            except Exception:
                _LOGGER.exception("%s: snapshot listener failed", self.serial)

    # --- learned ------------------------------------------------------------------------

    # --- hooks for the command layer (modbus/commands.py) ---------------------

    @property
    def clock(self) -> Callable[[], float]:
        """The monotonic clock shared with the client (and the command layer)."""
        return self._clock

    @property
    def start_pending(self) -> bool:
        """Our 95 = 1 went out and no status 3/4 has been seen since."""
        return self._start_pending

    @property
    def connection_id(self) -> int:
        """Changes with every new socket: a sequence that sees it change lost the link."""
        return self._client.connections

    @property
    def identity_current(self) -> bool:
        """The identity is verified on the socket in use now: a
        verification read of a sequence is data only then. A closed socket
        is none: any error closes it (evcc) and the next request would open
        a new, unchecked one."""
        c = self._client
        return self.identity == C.IDENTITY_OK and self._identity_conn == c.connections and c.state == STATE_CONNECTED

    def check_write(self, register: int, value: int, origin: str) -> None:
        """The synchronous part of the write gate; raises ``WriteRefused``."""
        stop = register == C.REG_COMMAND and value == C.COMMAND_STOP
        self._gate(register, value, origin, stop)

    def set_problem(self, reason: str, on: bool) -> None:
        """Problem flags owned by the command layer (start_not_taken...)."""
        if on:
            self._problems.add(reason)
        else:
            self._problems.discard(reason)

    def suppress_phase_count(self, seconds: float | None = None) -> None:
        """No phase counting for SETTLE_S after a start or a phase switch."""
        self._phase_suppress_until = max(
            self._phase_suppress_until, self._clock() + (self.timing.settle_s if seconds is None else seconds)
        )

    # --- lifecycle ------------------------------------------------------------------

    async def async_first_contact(
        self,
        *,
        initialize: Callable[[], Awaitable[None]] | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        """Start-up like evcc's ``NewEm2Go``: its ``initialize`` is retried
        with an exponential backoff (2 s, x1.5, at most 10 s apart) and given
        up after 30 s - then the last ``ModbusError`` is raised and the
        caller lets Home Assistant retry the setup (ConfigEntryNotReady).

        One try: the serial on the new connection first
        (kept: nothing is written to another station), then
        ``initialize`` - the command layer's 6.1 A probe (evcc initialize) -
        then one read of reg 200 (its error means no phase switching, not
        retried). Only a confirmed other serial ends it (the poll reports
        the identity as usual). Zeros or an exception reply to the serial
        (not verified), a failed request or a probe write that did not
        go out are a failed try: retried like evcc retries initialize until
        it succeeds, and raised after the give-up.
        ``jitter`` returns 0..1 (the randomization of the backoff).
        """
        started = self._clock()
        interval = self.timing.startup_initial_s
        while True:
            try:
                try:
                    async with self._lock:
                        verified = await self._check_identity()
                except _PollException as exc:
                    raise ModbusConnectionError(f"serial: exception {exc.err.code}", False) from exc
                if not verified and self.identity != C.IDENTITY_MISMATCH:
                    raise ModbusConnectionError("serial not verified (zeros)", False)
                if verified:
                    # 32/34/36 once per connection - here, before the
                    # probe, so its put-back clamps with the station's real
                    # range, not the 6-16 A defaults.
                    async with self._lock:
                        await self._read_limits()
                    if initialize is not None:
                        try:
                            await initialize()
                        except WriteRefused as refused:
                            raise ModbusConnectionError(f"initialize: {refused.key}", False) from refused
                    if not self._phases_decided:
                        async with self._lock:
                            await self._decide_phases({})
                return
            except ModbusClosedError:
                raise  # closed (unload): nothing more to try
            except ModbusError as err:
                spread = C.STARTUP_BACKOFF_RANDOMIZATION * interval
                wait = interval - spread + 2 * spread * jitter()
                interval = min(interval * C.STARTUP_BACKOFF_MULTIPLIER, self.timing.startup_max_s)
                elapsed = self._clock() - started
                if elapsed + wait > self.timing.startup_give_up_s:
                    _LOGGER.debug(
                        "%s: no answer at start-up for %.0f s (%s); setup given up for now",
                        self.serial, elapsed, getattr(err, "kind", type(err).__name__),
                    )
                    raise
                _LOGGER.debug("%s: start-up contact failed (%s); next try in %.1f s", self.serial, err, wait)
                await self._sleep(wait)

    async def async_read_comm_timeout(self) -> int:
        """Reg 89 once after the first contact, like evcc's NewEm2Go: a failed read fails the setup (the caller raises
        ConfigEntryNotReady, Home Assistant retries). Its value drives the
        heartbeat and the comm-timeout sensor."""
        async with self._lock:
            value = (await self._read(C.REG_COMM_TIMEOUT, 1))[0]
        self._raw[C.REG_COMM_TIMEOUT] = value
        return value

    def heartbeat_interval(self) -> float | None:
        """evcc's EM2GO heartbeat: reg 87 every reg89 / 2 s when 89 > 0."""
        timeout = self._raw.get(C.REG_COMM_TIMEOUT)
        return timeout / 2 if isinstance(timeout, int) and timeout > 0 else None

    async def _run_heartbeat(self, interval: float) -> None:
        """Read 87 every ``interval`` s, independent of the poll; any error is
        only logged and the loop goes on - only cancellation or the station's
        stop ends it (behaviour of em2go.go heartbeat, nothing copied)."""
        while not self._stopped:
            await self._sleep(interval)
            if self._stopped:
                return
            try:
                async with self._lock:
                    self._raw[C.REG_FALLBACK_CURRENT] = (await self._read(C.REG_FALLBACK_CURRENT, 1))[0]
            except ModbusClosedError:
                _LOGGER.debug("%s: heartbeat: client closed", self.serial)
            except ModbusError as err:
                _LOGGER.error("%s: heartbeat: %s", self.serial, getattr(err, "kind", None) or type(err).__name__)
            except Exception:  # noqa: BLE001 - like evcc's loop: log, keep running
                _LOGGER.exception("%s: heartbeat: unexpected error; next cycle runs", self.serial)

    async def async_start(self) -> None:
        """Start polling at once; never raises - a station that stops
        answering later is shown by the entities, polling goes on."""
        if self._store is not None and not self._loaded:
            await self.async_load()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())
        interval = self.heartbeat_interval()
        if interval is not None and self._heartbeat is None:
            self._heartbeat = asyncio.get_running_loop().create_task(self._run_heartbeat(interval))

    async def async_stop(self) -> float | None:
        """Cancel polling and close the socket within about a second."""
        self._stopped = True
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.wait({heartbeat}, timeout=self.timing.stop_timeout_s)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=self.timing.stop_timeout_s)
            if task.done() and not task.cancelled() and task.exception() is not None:
                _LOGGER.error("%s: poll task failed: %r", self.serial, task.exception())
        await self._client.close()
        await self.async_save()
        return self._client.last_close_monotonic

    def next_delay(self) -> float:
        """The poll period: 10 s or the option, also while charging."""
        return max(self.timing.poll_s, self.timing.min_poll_s)

    async def _run(self) -> None:
        while not self._stopped:
            started = self._clock()
            try:
                await self.async_tick()
                self._run_controllers()
            except Exception:  # polling must never stop
                _LOGGER.exception("%s: poll failed unexpectedly; polling goes on", self.serial)
            elapsed = self._clock() - started
            delay = max(self.next_delay() - elapsed, self.timing.min_poll_s - elapsed, 0.0)
            await self._sleep(delay)

    def _run_controllers(self) -> None:
        now = self._clock()
        for entry in list(self._controllers):
            if now >= entry["next"]:
                entry["next"] = now + entry["interval"]
                try:
                    entry["cb"](self._snap)
                except Exception:
                    _LOGGER.exception("%s: controller failed", self.serial)

    async def async_refresh(self) -> bool:
        """Immediate poll on the existing connection, at most every 5 s."""
        now = self._clock()
        if self._last_refresh is not None and now - self._last_refresh < self.timing.refresh_min_s:
            return False
        self._last_refresh = now
        return await self.async_tick()

    async def async_tick(self) -> bool:
        """One poll. Skipped (False) if the previous one still runs."""
        if self._tick_running:
            self._skipped_ticks += 1
            _LOGGER.warning("%s: poll skipped, the previous one is still running", self.serial)
            return False
        self._tick_running = True
        try:
            await self._poll()
            self._evaluate_link()
            self._snap = self._build_snapshot()
            self._notify()
            await self._after_poll()
        finally:
            self._tick_running = False
        return True

    # --- requests -------------------------------------------------------------------

    def _trace(self, op: str, addr: int, arg: Any, result: Any, started: float) -> None:
        self._ring.append(
            {
                "t": round(started, 3),
                "op": op,
                "addr": addr,
                "arg": arg,
                "result": result,
                "ms": round((self._clock() - started) * 1000),
            }
        )

    def _valid_response(self) -> None:
        self._last_valid = self._clock()
        self._fail_run_start = None
        self._fail_run_attempts = 0

    async def _read(self, address: int, count: int) -> list[int]:
        started = self._clock()
        try:
            words = await self._client.read_holding_registers(address, count)
        except ModbusExceptionError as err:
            self._valid_response()  # a healthy round trip
            self._trace("read", address, count, f"exception {err.code}", started)
            raise
        except ModbusClosedError:
            raise
        except ModbusError as err:
            self._trace("read", address, count, getattr(err, "kind", "error"), started)
            raise
        self._valid_response()
        self._trace("read", address, count, list(words), started)
        return list(words)

    async def async_read(self, group: C.ReadGroup) -> dict[int, int]:
        """Read one group (verification reads of a sequence). Raises ModbusError."""
        raw: dict[int, int] = {}
        async with self._lock:
            await self._read_group(group, raw)
        return raw

    async def _read_group(self, g: C.ReadGroup, raw: dict[int, int]) -> None:
        """Every request of the group in a row; the caller holds the lock."""
        for address, count in g.requests:
            await self._read_request(g, address, count, raw)

    async def _read_request(self, g: C.ReadGroup, address: int, count: int, raw: dict[int, int]) -> None:
        """One request of a group (count 1, or 2 for a U32; evcc EM2GO).
        An exception reply fails a required group; otherwise the value is
        just unknown."""
        try:
            words = await self._read(address, count)
        except ModbusExceptionError as err:
            if g.required:
                raise _PollException(err) from None
            return
        if g.u32:
            raw[address], raw[address + 1] = words
        else:
            raw[address] = words[0]

    def _check_connection(self) -> None:
        if self._client.connections != self._identity_conn:
            raise _NewConnection

    # --- polling --------------------------------------------------------------------

    def _identity_needed(self) -> bool:
        c = self._client
        return c.connections != self._identity_conn or self.identity != C.IDENTITY_OK or c.state != STATE_CONNECTED

    async def _poll(self) -> None:
        """One poll. The station lock is taken per transaction (identity
        attempt or read group), never for the whole poll and never across a
        wait, so a stop waits at most for the request in flight. No pause after a start: evcc reads every cycle."""
        now = self._clock()
        try:
            if self._identity_needed():
                if not await self._check_identity(poll=True):
                    return
            new_connection = self._data_conn != self._identity_conn
            raw: dict[int, int] = {}
            groups = list(C.TIER_A)
            if self._caps_phases:
                groups.append(C.PHASES_GROUP)
            tier_b = new_connection or self._last_tier_b is None or now - self._last_tier_b >= self.timing.slow_poll_s
            if tier_b:
                groups += list(C.TIER_B)
            if new_connection and self._limits_conn != self._client.connections:
                groups += list(C.CONNECTION_GROUPS)
            for g in groups:
                # The lock per request: a stop waits at most for the one in flight.
                for address, count in g.requests:
                    async with self._lock:
                        await self._read_request(g, address, count, raw)
                    self._check_connection()
            if not self._phases_decided:
                # Not decided at the first contact (e.g. no answer then): once
                # here, after the first full poll (evcc initialize).
                async with self._lock:
                    await self._decide_phases(raw, propagate=True)
                self._check_connection()
        except ModbusClosedError:
            return  # the client is closed (unload): nothing was sent
        except _NewConnection:
            _LOGGER.debug("%s: new connection during a poll; identity first next time", self.serial)
            return
        except _PollException as exc:
            self._poll_failed(exc.err, C.FAIL_EXCEPTION, link=False)
            return
        except ModbusError as err:
            self._poll_failed(err, getattr(err, "kind", C.FAIL_OTHER))
            return
        if raw.get(C.REG_STATUS, 0) == 0:
            # Status 0 is no state (evcc's driver returns an error and the
            # loadpoint skips the cycle, the poll is dropped,
            # e.g. the zeros right after a connect.
            self._poll_failed(None, C.FAIL_NOT_READY, link=False)
            return
        self._commit(raw, new_connection, tier_b, now)

    async def _read_limits(self) -> None:
        """The per-connection limits group (the caller holds the lock); the
        first poll on the same connection does not read it again."""
        raw: dict[int, int] = {}
        for g in C.CONNECTION_GROUPS:
            await self._read_group(g, raw)
        self._raw.update(raw)
        self._limits_conn = self._client.connections

    async def _check_identity(self, *, poll: bool = False) -> bool:
        """38/40/42 first on every connection; nothing else is accepted until
        it matches the entry serial.

        ``poll``: the lock is taken per attempt and released across the retry
        pause, so a write that came meanwhile goes first. Otherwise
        the caller holds the lock (a write on a new connection) and the
        attempts follow each other at once.
        """
        c = self._client
        words: list[int] = [0, 0, 0]
        previous: list[int] | None = None
        stable = False
        for attempt in range(self.timing.identity_retries):
            raw: dict[int, int] = {}
            if not poll:
                await self._read_group(C.SERIAL_GROUP, raw)
            else:
                async with self._lock:
                    if attempt and not self._identity_needed():
                        return True  # a write verified this connection meanwhile
                    await self._read_group(C.SERIAL_GROUP, raw)
            words = [raw.get(r, 0) for r in C.SERIAL_GROUP.used]
            # Three single reads: a box still answering zeros right after a
            # connect may zero only some of them; read again then.
            if all(words):
                break
            if any(words) and words == previous:
                stable = True  # the same partly zero words twice: a real 0000 word
                break
            previous = words
            if attempt + 1 < self.timing.identity_retries and poll:
                await asyncio.sleep(0)  # let a waiting stop take the station first
                if self._lock.locked():
                    async with self._lock:
                        pass
                await self._sleep(self.timing.identity_retry_delay_s)
        if not all(words) and not stable:
            # All zero, or zeros straddling the three single reads: not ready,
            # never "another station".
            self._poll_failed(None, C.FAIL_NOT_READY, link=False)
            if self.identity == C.IDENTITY_OK:
                self.identity = C.IDENTITY_UNVERIFIED
            return False
        serial = decode_serial(words)
        if serial != self.serial:
            self._serial_seen = serial or " ".join(f"{w:04X}" for w in words)
            if self.identity != C.IDENTITY_MISMATCH:
                _LOGGER.warning(
                    "%s: another station answers at this address (serial %s); no data, no writes",
                    self.serial, self._serial_seen,
                )
                self.identity = C.IDENTITY_MISMATCH
                self._emit(C.EVENT_IDENTITY_MISMATCH, serial_seen=self._serial_seen)
            self._identity_conn = None  # keep re-checking 38/40/42 every poll
            self._poll_failed(None, C.FAIL_IDENTITY_MISMATCH, link=False)
            return False
        if self.identity == C.IDENTITY_MISMATCH:
            _LOGGER.warning("%s: the right station answers again", self.serial)
            self._emit(C.EVENT_IDENTITY_RESTORED)
        self.identity = C.IDENTITY_OK
        self._serial_seen = serial
        self._identity_conn = c.connections
        return True

    async def _decide_phases(self, raw: dict[int, int], *, propagate: bool = False) -> None:
        """Phase capability exactly like evcc's initialize: one read of reg 200 - it succeeds ->
        PhaseSwitcher/PhaseGetter, any error -> none. Decided once per
        instance (integration start or reload); never re-probed or dropped.
        ``propagate``: a link error is raised after deciding (poll counting)."""
        error: ModbusError | None = None
        if C.REG_PHASES not in raw:
            try:
                raw[C.REG_PHASES] = (await self._read(C.REG_PHASES, 1))[0]
            except ModbusError as err:
                error = err
        capable = error is None
        self._phases_decided = True
        if capable != self._caps_phases:
            _LOGGER.info("%s: phase switching register %s", self.serial, "readable" if capable else "not readable")
        self._caps_phases = capable
        if error is not None and propagate and not isinstance(error, ModbusExceptionError):
            raise error

    def _poll_failed(self, err: ModbusError | None, kind: str, link: bool = True) -> None:
        now = self._clock()
        self.last_failure = kind
        self.last_failure_text = str(err) if err is not None else kind
        if not link:
            return  # a valid reply came (exception / zeros / other serial)
        self._errors.append(now)
        self._failed_polls += 1
        if self._fail_run_start is None:
            self._fail_run_start = now
        self._fail_run_attempts += 1
        _LOGGER.debug("%s: poll failed (%s): %s", self.serial, kind, self.last_failure_text)

    # --- state model ---------------------------------------------------------------------

    def _commit(self, raw: dict[int, int], new_connection: bool, tier_b: bool, now: float) -> None:
        prev = self._snap
        merged = dict(raw)
        # Tier B (60 s), per-connection (32/34/36) and setup/heartbeat (89/87)
        # registers keep their last value between their reads; anything else
        # not read this poll (e.g. 200 without the capability) is unknown
        # rather than stale.
        for reg in C.STICKY_REGS:
            if reg not in merged and reg in self._raw:
                merged[reg] = self._raw[reg]
        if tier_b:
            self._last_tier_b = now
        self._raw = merged
        first = not self._first_poll_done
        status = merged.get(C.REG_STATUS)
        cable = merged.get(C.REG_CABLE)
        error = merged.get(C.REG_ERROR)
        currents = tuple(_scaled(merged, r, C.SCALE_CURRENT) for r in (C.REG_CURRENT_L1, C.REG_CURRENT_L2, C.REG_CURRENT_L3))
        max_i = max((c for c in currents if c is not None), default=0.0)

        # delivering = status 4 and current flows
        if status != C.STATUS_CHARGING:
            self._delivering = False
        elif self._delivering:
            self._delivering = max_i >= C.DELIVERING_OFF_A
        else:
            self._delivering = max_i >= C.DELIVERING_ON_A
        if self._delivering and now >= self._phase_suppress_until:
            self._phases.update({ph: (c or 0.0) for ph, c in zip(("L1", "L2", "L3"), currents)}, now)

        if status in (C.STATUS_STARTING, C.STATUS_CHARGING):
            self._start_pending = False
            self._charge_seen = True
        self._log_changes(prev, status, cable, error, now)
        self._plug_events(cable)
        self._error_events(prev.error if prev.time is not None else None, error, first)
        self._cross_check(status, cable)
        self._limit_checks(status, max_i, merged.get(C.REG_CURRENT_SETPOINT), now)
        self._seed_desired(merged)
        self._classify(merged, new_connection, now)
        if new_connection:
            self._restore_wait_ready = False
            self._pending_restore = self._restore_needed(merged, now)
        elif self._restore_wait_ready and status in C.CONTROL_STATUSES:
            # The first poll after a power loss saw the box booting.
            self._restore_wait_ready = False
            self._pending_restore = self._restore_needed(merged, now)
        self._check_pnc_reset(merged)

        self._baseline = {
            "91": merged.get(C.REG_CURRENT_SETPOINT),
            "93": merged.get(C.REG_MODE),
            "200": merged.get(C.REG_PHASES),
            "78": decode_u32(merged, C.REG_DURATION),
            "status": status,
            # record (record layer OR status 3/4 seen).
            "record_open": self.record_open,
            "time": now,
        }
        self._restored_baseline = False
        self._data_conn = self._identity_conn
        self._last_good_poll = now
        self._failed_polls = 0
        self._first_poll_done = True
        self._schedule_save()

    def _log_changes(self, prev: Snapshot, status, cable, error, now: float) -> None:
        if prev.time is None:
            return
        for name, old, new in (("status", prev.status, status), ("cable", prev.cable, cable), ("error", prev.error, error)):
            if old != new:
                _LOGGER.info("%s: %s %s -> %s (poll at %.1f)", self.serial, name, old, new, now)
                if name == "status":
                    self._emit(C.EVENT_STATUS, old=old, new=new)

    def _plug_events(self, cable: int | None) -> None:
        """Plug-in / unplug as reg 2 shows it, at once (no 2-poll vote)."""
        if cable is None:
            return
        plugged = bool(cable)
        if self._plugged is None:
            self._plugged = plugged
            if not plugged:
                self._charge_seen = False
            return
        if plugged == self._plugged:
            return
        self._plugged = plugged
        if plugged:
            _LOGGER.info("%s: cable plugged in", self.serial)
            self._emit(C.EVENT_PLUGGED)
            return
        _LOGGER.info("%s: cable unplugged; session flags reset", self.serial)
        self.session_flags.clear()
        self._start_pending = False
        self._charge_seen = False
        self._emit(C.EVENT_UNPLUGGED)

    def _error_events(self, old: int | None, new: int | None, first: bool) -> None:
        if new is None:
            return
        if new == C.ERROR_RELAY_WELDED and old != new:
            _LOGGER.error("%s: reg 4 = 23, contactor welded: starts refused while it lasts", self.serial)
        if new == old:
            return
        if new:
            self._emit(C.EVENT_FAULT, code=new, key=error_key(new))
        elif old:
            self._emit(C.EVENT_FAULT_CLEARED, code=old)

    def _cross_check(self, status: int | None, cable: int | None) -> None:
        """Reg 0 in {2, 3, 4, 6} implies a plugged cable; 2 polls of
        disagreement are logged (the cable entity still follows reg 2)."""
        if status in C.CABLE_STATUSES and cable == 0:
            self._mismatch_polls += 1
            if self._mismatch_polls == 2:
                _LOGGER.info("%s: status %s but reg 2 (cable) = 0 for 2 polls", self.serial, status)
        else:
            self._mismatch_polls = 0

    def _limit_checks(self, status: int | None, max_i: float, setpoint: int | None, now: float) -> None:
        """Flat current clearly below 91 (station menu limit) or held
        at 6 A (the charger's "Current 6A" input mode): problem flags, never a write."""
        if not self._delivering or setpoint is None or now < self._phase_suppress_until:
            self._flat_low_polls = self._capped_polls = 0
            self._last_max_i = max_i if self._delivering else None
            return
        flat = self._last_max_i is not None and abs(max_i - self._last_max_i) <= 0.3
        self._last_max_i = max_i
        capped = C.CAPPED_6A_LOW <= max_i <= C.CAPPED_6A_HIGH and setpoint >= 70
        self._capped_polls = self._capped_polls + 1 if capped else 0
        low = flat and max_i <= setpoint / 10 - 1.0 and not capped
        self._flat_low_polls = self._flat_low_polls + 1 if low else 0
        self.set_problem(C.PROBLEM_CAPPED_6A, self._capped_polls >= 3)
        self.set_problem(C.PROBLEM_STATION_LIMIT_LOWER, self._flat_low_polls >= 2)

    def _seed_desired(self, raw: dict[int, int]) -> None:
        """Desired values are seeded once from the first good read."""
        changed = False
        if self.desired["current"] is None and raw.get(C.REG_CURRENT_SETPOINT) not in (None, *C.STOP_SETPOINT_STORED):
            # The stop marker 0 / 50 (C5) is never taken as the user's current.
            value = raw[C.REG_CURRENT_SETPOINT]
            low = max(raw.get(C.REG_MIN_CURRENT) or C.DEFAULT_MIN_TENTHS, C.CURRENT_FLOOR_TENTHS)
            self.desired["current"] = value if value >= low else low
            changed = True
        if self.desired["mode"] is None and raw.get(C.REG_MODE) is not None:
            self.desired["mode"] = raw[C.REG_MODE]
            changed = True
        if self.desired["phases"] is None and raw.get(C.REG_PHASES) in (1, 3):
            self.desired["phases"] = raw[C.REG_PHASES]
            changed = True
        if changed:
            _LOGGER.info("%s: desired values seeded from the station: %s", self.serial, self.desired)

    def _own_write_since(self, reg: int, since: float | None) -> bool:
        t = self._own_writes.get(reg)
        return t is not None and (since is None or t >= since)

    def _classify(self, raw: dict[int, int], link_dropped: bool, now: float) -> None:
        """Station restart vs single foreign changes.

        A restart needs status 10, or 93 -> 0 together with 91 -> 160, or a
        link drop / HA restart plus >= 2 of {93, 91 changed, 78 reset
        mid-record}, none caused by our writes. Single signals are only
        logged as foreign changes. The classification closes the record
        (power_loss) and drives resume; restoring 91 is separate (below).
        """
        status = raw.get(C.REG_STATUS)
        base = self._baseline
        if status == C.STATUS_BOOTING:
            # One event per boot, not one per poll while it shows 10.
            if not self._booting:
                self._booting = True
                self._restart_event(["status_10"], raw)
            return
        self._booting = False
        if base is None:
            return
        since = base.get("time")
        cur = {
            "91": raw.get(C.REG_CURRENT_SETPOINT),
            "93": raw.get(C.REG_MODE),
            "200": raw.get(C.REG_PHASES),
        }
        changed = {
            k: (base.get(k), v)
            for k, v in cur.items()
            if base.get(k) is not None and v is not None and v != base.get(k)
            and not self._own_write_since(int(k), since)
        }
        if link_dropped:
            signals = [k for k in ("93", "91") if k in changed]
            if base.get("record_open") and (base.get("78") or 0) > 0 and decode_u32(raw, C.REG_DURATION) == 0:
                # 78 restarts with every charge (reg 72 is not read any more).
                signals.append("78")
            signature = (
                "93" in changed and cur["93"] == C.MODE_PLUG_AND_CHARGE
                and "91" in changed and cur["91"] == C.POWER_ON_SETPOINT
            )
            if signature or len(signals) >= 2:
                self._restart_event(signals, raw)
                return
        for key, (old, new) in changed.items():
            entry = {"t": now, "register": int(key), "old": old, "new": new, "link_dropped": link_dropped}
            self._foreign.append(entry)
            _LOGGER.warning("%s: reg %s changed %s -> %s without a write of ours", self.serial, key, old, new)
            self._emit(C.EVENT_FOREIGN_CHANGE, register=int(key), old=old, new=new, link_dropped=link_dropped)

    def _restart_event(self, signals: list[str], raw: dict[int, int]) -> None:
        entry = {
            "t": self._clock(),
            "signals": signals,
            "91": raw.get(C.REG_CURRENT_SETPOINT),
            "93": raw.get(C.REG_MODE),
            "status": raw.get(C.REG_STATUS),
            # Was a charge running before the restart (for resume)?
            "record_open_before": bool((self._baseline or {}).get("record_open")),
        }
        self._restarts.append(entry)
        _LOGGER.warning("%s: station restart detected (%s)", self.serial, ", ".join(signals))
        # Like OCPP (ChargeRate re-sent after a boot): 91 is restored once the
        # station takes writes again.
        self._restore_wait_ready = True
        self._emit(C.EVENT_RESTARTED, **entry)

    def _restore_target(self) -> int | None:
        """The expected 91: desired, clamped to the current
        range and floored on whole-amp stations; the site's
        current instead while a GNLT site controls the station."""
        override = None
        if self.restore_override is not None:
            override = self.restore_override(self._raw.get(C.REG_CURRENT_SETPOINT))
        desired = override if override is not None else self.desired.get("current")
        if desired is None:
            return None
        low, high = self.current_range_tenths()
        target = min(max(desired, low), high)
        if self.learned["decimal_current"] == "no":
            floored = target // 10 * 10
            target = floored if floored >= low else floored + 10
        return target

    def _restore_needed(self, raw: dict[int, int], now: float) -> bool:
        """After a link drop, an HA start or a detected station restart: is 91
        away from the user's value? Like OCPP, which re-sends ChargeRate after
        every boot when it differs: one restore each time,
        no 30-min limit, no "link dropped right after it" rule."""
        target = self._restore_target()
        current = raw.get(C.REG_CURRENT_SETPOINT)
        if target is None or current is None or current == target:
            return False
        if self.setpoint_is_stop_marker(raw):
            _LOGGER.info("%s: 91 = %s is the stop marker (C5): not restored; the next start writes 91", self.serial, current)
            return False
        return True

    def setpoint_is_stop_marker(self, raw: dict[int, int] | None = None) -> bool:
        """91 holds the stop's 0 (stored 0 or 50) while the station
        is stopped in command mode: our (or evcc's) stop, not a foreign
        change. Under Plug & Charge or while charging it is not exempt."""
        raw = self._raw if raw is None else raw
        return (
            raw.get(C.REG_CURRENT_SETPOINT) in C.STOP_SETPOINT_STORED
            and raw.get(C.REG_MODE) == C.MODE_COMMAND
            and raw.get(C.REG_STATUS) not in (C.STATUS_STARTING, C.STATUS_CHARGING)
        )

    def _check_pnc_reset(self, raw: dict[int, int]) -> None:
        """93 went to 0 while the user's choice is 1: Plug & Charge is on
        again (power loss resets it). Repair only; no automatic write."""
        reset = self.desired.get("mode") == C.MODE_COMMAND and raw.get(C.REG_MODE) == C.MODE_PLUG_AND_CHARGE
        if reset and C.PROBLEM_PNC_RESET not in self._problems:
            _LOGGER.warning("%s: Plug & Charge is on again (reg 93 = 0) though the choice is off", self.serial)
        self.set_problem(C.PROBLEM_PNC_RESET, reset)

    async def _after_poll(self) -> None:
        if not self._pending_restore:
            return
        self._pending_restore = False
        target = self._restore_target()
        if target is None:
            return
        try:
            result = await self.async_write(C.REG_CURRENT_SETPOINT, target, origin="restore")
        except WriteRefused as err:
            _LOGGER.warning("%s: 91 not restored to %s: %s", self.serial, target, err.key)
            if err.key == C.REFUSE_STATION_BUSY:
                self._restore_wait_ready = True  # booting / firmware update: retry when ready
            return
        _LOGGER.info("%s: 91 restored to %s (%s)", self.serial, target, result.outcome)

    # --- link --------------------------------------------------------------------------

    def _evaluate_link(self) -> None:
        now = self._clock()
        while self._errors and now - self._errors[0] > 86400:
            self._errors.popleft()
        reference = self._last_valid if self._last_valid is not None else self._started_at
        if now - reference >= self.timing.lost_after_s:
            state = C.LINK_LOST
        elif (
            self._last_good_poll is None
            or self._failed_polls >= self.timing.link_down_polls
            or self.identity == C.IDENTITY_MISMATCH  # another station answers: not our link
        ):
            state = C.LINK_RECONNECTING
        else:
            state = C.LINK_CONNECTED
        if state == C.LINK_LOST and not self._lost_announced:
            self._lost_announced = True
            _LOGGER.warning("%s: no valid ModBus reply for %.0f s", self.serial, now - reference)
            self._emit(C.EVENT_LINK_LOST, hint=self.link_hint or self.last_failure)
        elif state != C.LINK_LOST and self._lost_announced:
            self._lost_announced = False
            self._emit(C.EVENT_LINK_RESTORED)
        self._link_state = state

    def _build_snapshot(self) -> Snapshot:
        raw = self._raw
        now = self._clock()
        status = raw.get(C.REG_STATUS)
        error = raw.get(C.REG_ERROR)
        problems = set(self._problems)
        if self.identity == C.IDENTITY_MISMATCH:
            problems.add(C.PROBLEM_IDENTITY_MISMATCH)
        if status in (C.STATUS_START_FAILED, C.STATUS_FAULT) and error == 0:
            problems.add(C.PROBLEM_STATION_FAULT_5_7)
        if error == C.ERROR_RELAY_WELDED:
            problems.add(C.PROBLEM_RELAY_WELDED)
        comm_timeout = raw.get(C.REG_COMM_TIMEOUT)
        setpoint = raw.get(C.REG_CURRENT_SETPOINT)
        low, high = self.current_range_tenths()
        flags = {
            "comm_timeout_short": comm_timeout is not None and 0 < comm_timeout < C.REG89_WARN_BELOW_S,
            "status_cable_mismatch": self._mismatch_polls >= 2,
            "setpoint_out_of_range": (
                setpoint is not None and not low <= setpoint <= high and not self.setpoint_is_stop_marker(raw)
            ),
            "foreign_change": [dict(e) for e in self._foreign][-3:],
            "raw_status": status if status not in C.STATUS_KEYS else None,
            "raw_error": error if error not in C.ERROR_KEYS else None,
        }
        good = self._last_good_poll
        return Snapshot(
            time=good,
            registers=dict(raw),
            status=status,
            status_key=status_key(status),
            cable=raw.get(C.REG_CABLE),
            error=error,
            error_key=error_key(error),
            currents=tuple(_scaled(raw, r, C.SCALE_CURRENT) for r in (C.REG_CURRENT_L1, C.REG_CURRENT_L2, C.REG_CURRENT_L3)),
            voltages=tuple(_scaled(raw, r, C.SCALE_VOLTAGE) for r in (C.REG_VOLTAGE_L1, C.REG_VOLTAGE_L2, C.REG_VOLTAGE_L3)),
            power_w=decode_u32(raw, C.REG_POWER),
            per_phase_power_raw=tuple(decode_u32(raw, r) for r in (C.REG_POWER_L1, C.REG_POWER_L2, C.REG_POWER_L3)),
            phase_power_w=tuple(
                None if decode_u32(raw, r) is None else decode_u32(raw, r) * C.SCALE_PHASE_POWER
                for r in (C.REG_POWER_L1, C.REG_POWER_L2, C.REG_POWER_L3)
            ),
            meter_kwh=None if decode_u32(raw, C.REG_METER) is None else round(decode_u32(raw, C.REG_METER) * C.SCALE_ENERGY, 1),
            duration_s=decode_u32(raw, C.REG_DURATION),
            setpoint_a=_scaled(raw, C.REG_CURRENT_SETPOINT, C.SCALE_CURRENT),
            min_current_a=_scaled(raw, C.REG_MIN_CURRENT, C.SCALE_CURRENT),
            max_current_a=_scaled(raw, C.REG_MAX_CURRENT, C.SCALE_CURRENT),
            cable_max_a=_scaled(raw, C.REG_CABLE_MAX, C.SCALE_CURRENT),
            fallback_current_a=_scaled(raw, C.REG_FALLBACK_CURRENT, C.SCALE_CURRENT),
            comm_timeout_s=comm_timeout,
            mode=raw.get(C.REG_MODE),
            last_command=raw.get(C.REG_COMMAND),
            phases=raw.get(C.REG_PHASES),
            delivering=self._delivering,
            phases_in_use=self._phases.phases_in_use,
            plugged=self._plugged,
            waiting_for_start=status == C.STATUS_CONNECTED and raw.get(C.REG_CABLE) == 1 and raw.get(C.REG_MODE) == C.MODE_COMMAND,
            link=self._link_state,
            identity=self.identity,
            measurements_available=good is not None and now - good < self.timing.unavailable_after_s,
            problems=frozenset(problems),
            flags=flags,
        )

    # --- write gate -----------------------------------

    def _refuse(self, key: str, register: int, value: int, origin: str, **placeholders: Any) -> WriteRefused:
        self._refusals.append({"t": self._clock(), "register": register, "value": value, "origin": origin, "key": key})
        _LOGGER.info("%s: write %s = %s (%s) refused: %s", self.serial, register, value, origin, key)
        return WriteRefused(key, **placeholders)

    def _gate(self, register: int, value: int, origin: str, stop: bool, stop_zero: bool = False) -> None:
        """Checks that need no request (one place for every caller). Like evcc: a write goes out when asked;
        only an identity mismatch, a value the registers do not take,
        and a status evcc treats as invalid (anything but 1/2/3/4/6, the
        loadpoint skips its cycle) refuse it. ``stop_zero``: the 91 = 0 that
        follows a stop's 95 = 2. The 6.1 A probe of the start-up
        (evcc initialize) writes before the first poll."""
        if register not in C.WRITE_ALLOW_LIST:
            raise self._refuse(C.REFUSE_NOT_ALLOWED, register, value, origin)
        allowed = C.ALLOWED_WRITE_VALUES.get(register)
        if (allowed is not None and value not in allowed) or not isinstance(value, int) or isinstance(value, bool):
            raise self._refuse(C.REFUSE_NOT_ALLOWED, register, value, origin)
        if stop_zero and (register, value) != (C.REG_CURRENT_SETPOINT, C.STOP_SETPOINT):
            raise self._refuse(C.REFUSE_NOT_ALLOWED, register, value, origin)
        if register == C.REG_CURRENT_SETPOINT and not stop_zero:
            low, high = self.current_range_tenths()
            if not low <= value <= high:
                # Never 0 (except the stop step, C5), never below 6 A or reg 34,
                # never above the maximum.
                raise self._refuse(C.REFUSE_CURRENT_RANGE, register, value, origin, min=low / 10, max=high / 10)
        if self.identity == C.IDENTITY_MISMATCH:
            raise self._refuse(C.REFUSE_IDENTITY_MISMATCH, register, value, origin)
        status = self._raw.get(C.REG_STATUS)
        if origin != C.ORIGIN_PROBE and status not in C.CONTROL_STATUSES:
            raise self._refuse(C.REFUSE_STATION_BUSY, register, value, origin, status=status)

    async def async_write(self, register: int, value: int, *, origin: str, stop_zero: bool = False) -> WriteResult:
        """The single write path: one FC16 request like evcc's EM2GO driver.
        Raises ``WriteRefused`` (nothing sent) or returns the outcome: ``ok``
        (acknowledged), ``unknown`` (no reply), ``rejected`` (exception
        reply), ``not_sent`` (no connection). On a new connection the serial
        is read first. No fresh read, no read-back, no hold
.

        * ``origin``: ``user`` / ``automation`` or an automatic one
          (``schedule``, ``restore``, ``sequence``, ``site``...).
        * ``stop_zero``: the 91 = 0 right after a stop's 95 = 2 (evcc
          Enable(false)) - the only 91 = 0 the gate allows.
        """
        stop = register == C.REG_COMMAND and value == C.COMMAND_STOP
        self._gate(register, value, origin, stop, stop_zero)
        async with self._lock:
            # The state may have changed while waiting for the lock.
            self._gate(register, value, origin, stop, stop_zero)
            if self._identity_needed():
                try:
                    verified = await self._check_identity()
                except (ModbusError, _PollException) as err:
                    raise self._refuse(C.REFUSE_NOT_CONNECTED, register, value, origin) from err
                if not verified:
                    key = C.REFUSE_IDENTITY_MISMATCH if self.identity == C.IDENTITY_MISMATCH else C.REFUSE_NOT_READY
                    raise self._refuse(key, register, value, origin)
            return await self._send(register, value, origin, stop)

    async def _send(self, register: int, value: int, origin: str, stop: bool) -> WriteResult:
        start = register == C.REG_COMMAND and value == C.COMMAND_START
        started = self._clock()
        acknowledged = False
        outcome = C.WRITE_UNKNOWN
        error: str | None = None
        before = (self._start_pending, self._own_writes.get(register))
        # An unknown outcome counts as written: mark before
        # sending, so a cancel while the reply is in flight leaves "own start
        # pending" / "own write" set; undone only when the station certainly
        # did not take the write.
        self._own_writes[register] = started
        if start:
            self._start_pending = True
        try:
            await self._client.write_register(register, value)
            acknowledged = True
            outcome = C.WRITE_OK
        except ModbusClosedError as err:
            self._undo_marks(register, before)
            raise self._refuse(C.REFUSE_NOT_CONNECTED, register, value, origin) from err
        except ModbusExceptionError as err:
            self._valid_response()
            outcome, error = C.WRITE_REJECTED, f"exception {err.code}"
        except ModbusTimeoutError as err:
            outcome, error = C.WRITE_UNKNOWN, str(err)
        except ModbusError as err:
            kind = getattr(err, "kind", "")
            outcome = C.WRITE_NOT_SENT if kind in _CONNECT_KINDS else C.WRITE_UNKNOWN
            error = str(err)
        else:
            self._valid_response()
        self._trace("write", register, value, "ack" if acknowledged else (error or outcome), started)
        sent = outcome not in (C.WRITE_REJECTED, C.WRITE_NOT_SENT)
        if not sent:
            self._undo_marks(register, before)
        elif start or register == C.REG_PHASES:
            self.suppress_phase_count()
            if register == C.REG_PHASES and value in (1, 3):
                # expected for the settle window plus two readings (a change
                # is taken when two polls agree)
                self._phases.expect(value, self._phase_suppress_until + C.PHASE_EXPECT_EXTRA_S)
        elif stop:
            self._start_pending = False
            self._charge_seen = False
        if acknowledged:
            # The station takes it: the snapshot shows it at once (evcc keeps
            # what it wrote; the next poll shows what the station holds).
            self._raw[register] = value
            self._refresh_written(register, value)
        result = WriteResult(register, value, origin, outcome, acknowledged, started, error)
        entry = {
            "t": round(started, 3),
            "wall": time.time(),
            "register": register,
            "value": value,
            "origin": origin,
            "acknowledged": acknowledged,
            "result": outcome,
            "error": error,
        }
        self._write_log.append(entry)
        _LOGGER.info("%s: wrote %s = %s (%s): %s", self.serial, register, value, origin, outcome)
        self._emit(C.EVENT_WRITE, **entry)
        return result

    def _undo_marks(self, register: int, before: tuple) -> None:
        pending, own = before
        self._start_pending = pending
        if own is None:
            self._own_writes.pop(register, None)
        else:
            self._own_writes[register] = own

    def _refresh_written(self, register: int, value: int) -> None:
        """An acknowledged write is the station's state now: the snapshot
        shows it at once (a stop right after our
        start sees 95 = 1)."""
        snap = self._snap
        if snap.time is None:
            return
        fields = {"registers": {**snap.registers, register: value}}
        if register == C.REG_MODE:
            fields["mode"] = value
        elif register == C.REG_PHASES:
            fields["phases"] = value
        elif register == C.REG_CURRENT_SETPOINT:
            fields["setpoint_a"] = round(value * C.SCALE_CURRENT, 3)
        elif register == C.REG_COMMAND:
            fields["last_command"] = value
        self._snap = replace(snap, **fields)

    # --- diagnostics --------------------------------------------------------

    def _raw_by_register(self) -> dict[int, Any]:
        """Raw values by the register they were read from; a U32 field as the
        pair of words of its count-2 read, ``{12: [w0, w1]}``."""
        raw = dict(self._raw)
        out: dict[int, Any] = {}
        for g in (*C.TIER_A, *C.TIER_B):
            if g.u32 and g.address in raw:
                out[g.address] = [raw.pop(g.address), raw.pop(g.address + 1, None)]
        out.update(raw)
        return dict(sorted(out.items()))

    def diagnostics(self) -> dict[str, Any]:
        """Everything support needs; never touches the socket."""
        now = self._clock()
        snap = self._snap
        return {
            "serial": self.serial,
            "serial_seen": self._serial_seen,
            "learned": self.learned.as_dict(),
            "desired": dict(self.desired),
            "capabilities": {"phases": self._caps_phases, "phases_decided": self._phases_decided},
            "link": {
                "state": self._link_state,
                "identity": self.identity,
                "client_state": self._client.state,
                "consecutive_failures": self._client.consecutive_failures,
                "failed_polls_in_row": self._failed_polls,
                "errors_24h": len(self._errors),
                "last_failure": self.last_failure,
                "last_failure_text": self.last_failure_text,
                "hint": self.link_hint,
                "seconds_since_valid_reply": None if self._last_valid is None else round(now - self._last_valid, 1),
                "connections": self._client.connections,
                "skipped_ticks": self._skipped_ticks,
            },
            "registers_raw": self._raw_by_register(),
            "decoded": {
                "status": snap.status_key,
                "error": snap.error_key,
                "currents_a": snap.currents,
                "voltages_v": snap.voltages,
                "power_w": snap.power_w,
                "per_phase_power_raw": snap.per_phase_power_raw,
                "phase_power_w": snap.phase_power_w,
                "meter_kwh": snap.meter_kwh,
                "duration_s": snap.duration_s,
                "setpoint_a": snap.setpoint_a,
                "delivering": snap.delivering,
                "phases_in_use": snap.phases_in_use,
            },
            "effective_limit_note": (
                "The station applies the lower of its own menu current and reg 91; "
                "91 reads back what was written even when the menu limit is lower."
            ),
            "display_fault_bits": {f"{bit:04X}": name for bit, name in C.DISPLAY_FAULT_BITS.items()},
            "problems": sorted(snap.problems),
            "flags": snap.flags,
            "session_flags": dict(self.session_flags),
            "restarts": list(self._restarts),
            "foreign_changes": list(self._foreign),
            "write_log": list(self._write_log),
            "refusals": list(self._refusals),
            "transactions": list(self._ring),
        }


__all__ = [
    "PhaseCounter",
    "Snapshot",
    "Station",
    "StationTiming",
    "WriteRefused",
    "WriteResult",
    "decode_serial",
    "decode_u32",
    "error_key",
    "status_key",
    "store_key",
]
