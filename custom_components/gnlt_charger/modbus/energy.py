"""Energy accounting of the ModBus mode (no Home Assistant imports).

Over ModBus there is no transaction with a meterStart / meterStop, so the
charge record and its energy are built here from the polled registers.

* ``EnergyRecord`` - one station charge (parity with an OCPP transaction). It
  opens when status enters 4 and stays open across car pauses (4 -> 2 with the
  cable in) and our own phase sequences; it closes on a confirmed unplug, on
  our stop once the station is out of 4, on status 6, on a classified station
  restart (power loss) or when a phase switch did not restart the charge.
* Two energy paths are computed always (a test with a car
  picks the default): the **integral** of reg 12 (trapezoid, monotonic clock)
  and the **register** path (delta of the lifetime meter reg 28). The option
  ``energy_source`` picks which one feeds the session, totals and money; a
  record keeps the source it opened with.
* Totals and money use the shared ``logic.Tariff`` / ``logic.Totals`` (never copied).
* ``EnergyMeter.as_dict`` / ``load`` is the energy part of the per-serial
  Store; ``StationEnergy`` connects a ``Station`` to the meter and puts
  that part into the station's Store.

Two clocks: the station's monotonic clock (``Sample.t``) for the integral, so
a wall-clock jump never adds or removes energy; the wall clock (aware
``datetime``) for prices, day/month totals, record times and the windows that
must survive an HA restart (late reg-28 steps, the reg-28 plausibility bound).

Reg 72 (station session energy) is never read here, nor are the
per-phase powers 16/20/24 (unit unresolved, C-42) or reg 78 (C-37).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from ..logic import Tariff, Totals, apparent_power_va
from ..protocol import AC_PRESETS, preset_key
from . import const as C

_LOGGER = logging.getLogger(__name__)

STORE_SECTION = "energy"
ENERGY_STORE_VERSION = 1

SOURCE_INTEGRAL = "integral"
SOURCE_REGISTER = "register"
SOURCES = (SOURCE_INTEGRAL, SOURCE_REGISTER)
# Provisional: reg 28 reads 0 on A3C_2.7 and on another EVE during charges, so
# the register path would lose everything there. A test with a car decides.
DEFAULT_SOURCE = SOURCE_INTEGRAL

# Why a record closed (attribute ``reason`` of the last charge).
REASON_USER_STOP = "user_stop"
REASON_SCHEDULE_END = "schedule_end"
REASON_STATION = "station"  # status 6 (the car or the station ended it)
REASON_UNPLUG = "unplug"
REASON_POWER_LOSS = "power_loss"  # classified station restart
REASON_PHASE_SWITCH = "phase_switch"  # the charge did not restart
REASON_UNKNOWN = "unknown"
REASONS = (
    REASON_USER_STOP, REASON_SCHEDULE_END, REASON_STATION, REASON_UNPLUG,
    REASON_POWER_LOSS, REASON_PHASE_SWITCH, REASON_UNKNOWN,
)
STOP_REASONS = (REASON_USER_STOP, REASON_SCHEDULE_END)

# Notes that made a record approximate.
NOTE_GAP = "gap"  # > ENERGY_GAP_MAX_S without a power sample (link loss)
NOTE_HA_RESTART = "ha_restart"  # HA was down while the record was open
NOTE_REG28_REBASE = "reg28_rebase"  # reg 28 dropped or jumped implausibly
NOTE_STORE = "store_repaired"  # a stored field was unreadable and reset

# Our stop decides how a record ends only for as long as a stop can still take
# effect (verification + late stop). B07 ignores the stop: a car pause
# an hour later must not end the record as "user_stop".
STOP_EXPIRE_S = C.STOP_VERIFY_S + C.STOP_LATE_S

# The integral runs while the station starts or charges; not gated
# by the 1 A "delivering" threshold, so the < 1 A tail is counted.
ENERGY_STATUSES = frozenset({C.STATUS_STARTING, C.STATUS_CHARGING})

# plausibility: a reg-28 step may not exceed P_max x (time the station
# spent in 3/4 since the last change + REG28_LATE_S) + one lazy step of 0.3 kWh
# (B07 adds the energy in 0.3 kWh blocks about every 10 min, up to ~15 min
# after the charge). Idle time never widens the bound: a
# lifetime value appearing after days of zeros is not energy.
REG28_SLACK_WH = 300.0
WH_PER_TENTH = 100.0  # reg 28 counts 0.1 kWh
# Largest AC preset: the bound when the preset is unknown (generous on purpose:
# a too tight bound would throw away real energy).
P_MAX_FALLBACK_W = max(kw for _phases, kw, _amps in AC_PRESETS) * 1000.0

# (port of the OCPP power-mismatch watchdog): reg 12 above
# 1.2 x sum U*I + 200 W is a firmware glitch (262144 W for 4 W seen elsewhere).
POWER_SLACK_FACTOR = 1.2
POWER_SLACK_W = 200.0


def p_max_w(preset: str | None) -> float:
    """Highest power of an AC preset (``3_11`` -> 11000 W); the largest preset
    if unknown."""
    for phases, kw, _amps in AC_PRESETS:
        if preset is not None and preset_key(phases, kw) == preset:
            return kw * 1000.0
    return P_MAX_FALLBACK_W


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at is not None else None


def _parse(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _u32(raw: dict[int, int], reg: int) -> int | None:
    # Both words of a pair come from one read, high word first.
    if reg in raw and reg + 1 in raw:
        return (raw[reg] << 16) | raw[reg + 1]
    return None


# --- input ------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """What the energy layer needs from one poll (or one verification read).

    ``poll=False``: a verification read of a sequence. It feeds the
    integral (no gap during a start or phase switch) and may open
    a record on status 4, but never closes one: closing needs the full state
    model of a poll (confirmed unplug, status 6).
    """

    t: float  # station monotonic clock
    status: int | None
    power_w: float | None
    currents: tuple[float | None, float | None, float | None] = (None, None, None)
    voltages: tuple[float | None, float | None, float | None] = (None, None, None)
    meter_tenths: int | None = None  # reg 28 raw (0.1 kWh)
    plugged: bool | None = None  # confirmed cable state
    delivering: bool = False
    poll: bool = True


def sample_from_snapshot(snap: Any) -> Sample:
    """A ``station.Snapshot`` as an energy sample."""
    return Sample(
        t=snap.time,
        status=snap.status,
        power_w=snap.power_w,
        currents=tuple(snap.currents),
        voltages=tuple(snap.voltages),
        meter_tenths=_u32(snap.registers, C.REG_METER),
        plugged=snap.plugged,
        delivering=snap.delivering,
    )


def sample_from_registers(
    t: float, raw: dict[int, int], voltages: tuple[float | None, float | None, float | None] = (None, None, None)
) -> Sample:
    """Verification reads of a sequence: (0,3), (6,3), (10,1), (12,2).

    They carry no voltages; the last polled ones are used so the check
    and the U x I integral keep working through a sequence.
    """
    currents = tuple(
        None if raw.get(r) is None else round(raw[r] * C.SCALE_CURRENT, 3)
        for r in (C.REG_CURRENT_L1, C.REG_CURRENT_L2, C.REG_CURRENT_L3)
    )
    status = raw.get(C.REG_STATUS)
    max_i = max((c for c in currents if c is not None), default=0.0)
    return Sample(
        t=t,
        status=status,
        power_w=_u32(raw, C.REG_POWER),
        currents=currents,
        voltages=tuple(voltages),
        delivering=status == C.STATUS_CHARGING and max_i >= C.DELIVERING_ON_A,
        poll=False,
    )


def _ui_w(s: Sample) -> float | None:
    """Sum of U x I (shared ``logic.apparent_power_va``); None unless the
    currents and the L1 voltage are known."""
    if s.voltages[0] is None or any(c is None for c in s.currents):
        return None
    phases = ("L1", "L2", "L3")
    voltage = {ph: v for ph, v in zip(phases, s.voltages) if v is not None}
    current = {ph: c for ph, c in zip(phases, s.currents) if c is not None}
    return apparent_power_va(voltage, current)


# --- the record -------------------------------------------------------------------


@dataclass
class EnergyRecord:
    """One station charge with the accumulators of both paths."""

    rid: int
    source: str  # fixed when the record opens
    started: str  # ISO, wall clock
    finished: str | None = None
    reason: str | None = None
    integral_wh: float = 0.0  # reg 12 integral
    ui_wh: float = 0.0  # sum U x I integral, for the car test (attribute energy_ui_kwh)
    register_wh: float = 0.0  # reg 28 deltas
    cost: float = 0.0  # money of the record's own source
    approximate: bool = False
    notes: list[str] = field(default_factory=list)
    delivered: bool = False  # reached "delivering": a real charge, counted once (W23/W24)
    register_seen: bool = False  # reg 28 was read while the record was open
    register_nonzero: bool = False
    power_dropped: bool = False  # logged once per charge

    @property
    def energy_wh(self) -> float:
        return self.register_wh if self.source == SOURCE_REGISTER else self.integral_wh

    def note(self, why: str) -> None:
        self.approximate = True
        if why not in self.notes:
            self.notes.append(why)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> EnergyRecord | None:
        """Field by field: a Store written by another
        version or damaged on disk must never break the setup. A record
        without a readable id or start is dropped; any other unreadable field
        gets its default and the record is marked approximate."""
        if not data:
            return None
        rid = data.get("rid") if isinstance(data, dict) else None
        started = data.get("started") if isinstance(data, dict) else None
        if not isinstance(rid, int) or isinstance(rid, bool) or _parse(started) is None:
            _LOGGER.warning("stored charge record unreadable, dropped: %.200r", data)
            return None
        source = data.get("source")
        rec = cls(rid=rid, source=source if source in SOURCES else DEFAULT_SOURCE, started=started)
        repaired = source not in SOURCES
        for f in fields(cls):
            if f.name in ("rid", "source", "started") or f.name not in data:
                continue
            value, default = data[f.name], getattr(rec, f.name)
            if f.name in ("finished", "reason"):
                ok = value is None or (isinstance(value, str) and (f.name == "reason" or _parse(value)))
            elif f.name == "notes":
                ok = isinstance(value, list) and all(isinstance(n, str) for n in value)
                value = list(value) if ok else value
            elif isinstance(default, bool):
                ok = isinstance(value, bool)
            else:  # float accumulators
                ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
                value = float(value) if ok else value
            if ok:
                setattr(rec, f.name, value)
            else:
                repaired = True
        if repaired:
            rec.note(NOTE_STORE)
        return rec


def _totals_from(data: Any) -> Totals:
    """``logic.Totals.from_dict`` takes values as they are; check types first
    so a damaged Store cannot put None into a counter."""
    if not isinstance(data, dict):
        return Totals()
    default = Totals()
    clean: dict[str, Any] = {}
    for name in Totals.__dataclass_fields__:
        value, ref = data.get(name), getattr(default, name)
        if isinstance(ref, bool):
            ok = isinstance(value, bool)
        elif isinstance(ref, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(ref, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, str)
        if ok:
            clean[name] = float(value) if isinstance(ref, float) else value
    return Totals.from_dict(clean)


class SensorValue(NamedTuple):
    value: Any
    last_reset: datetime | None = None
    attributes: dict[str, Any] | None = None


# Sensor classes, exactly as the OCPP sensors of the same key (; a test
# compares them with sensor.SENSORS): key -> (device class, state class, has
# last_reset). Strings so this stays free of Home Assistant; the entity layer
# maps them to its enums.
SENSOR_CLASSES: dict[str, tuple[str | None, str | None, bool]] = {
    # Starts from zero with every charge: "total" with the record start.
    "session_energy": ("energy", "total", True),
    # For the Energy dashboard: "total" without last_reset, always available.
    "total_energy": ("energy", "total", False),
    # No state class (OCPP sensor.py): the money of one charge drops to 0 at
    # the next one, which a "total" without last_reset would book as negative
    # .
    "session_cost": ("monetary", None, False),
    # Energy periods: last_reset only for a period seen from zero (Totals.day_from_zero).
    "energy_today": ("energy", "total", True),
    "energy_month": ("energy", "total", True),
    "cost_today": ("monetary", "total", True),
    "cost_month": ("monetary", "total", True),
    "sessions_month": (None, "total_increasing", False),
    "last_session": ("energy", None, False),
}


# --- the meter --------------------------------------------------------------------


class EnergyMeter:
    """Charge records, both energy paths, totals and money of one station."""

    def __init__(
        self,
        *,
        source: str = DEFAULT_SOURCE,
        tariff: Tariff | None = None,
        tz_name: str = "UTC",
        p_max_w: float = P_MAX_FALLBACK_W,
        wall: Callable[[], datetime] | None = None,
        name: str = "modbus",
    ) -> None:
        self.source = source if source in SOURCES else DEFAULT_SOURCE
        self.tariff = tariff or Tariff()
        self.tz_name = tz_name
        self.p_max_w = p_max_w
        self._wall = wall or (lambda: datetime.now(UTC))
        self.name = name  # the serial, for logs
        self._clear()

    def _clear(self) -> None:
        """Fresh state (also the fallback when a stored state cannot be read)."""
        self.totals = Totals()
        self.total_wh = 0.0  # booked energy (both sources, each record by its own)
        # After an HA crash: HA's last total_energy and the integral booked
        # since. Kept apart from total_wh: the register
        # path recovers the energy between the last save and the crash by
        # itself, the integral does not.
        self._floor_wh: float | None = None
        self._integral_since_floor = 0.0
        # The floor is folded into the booked total once nothing can recover
        # the energy any more: at once for the integral, after the first
        # reg-28 read for the register path (its stored baseline brings the
        # steps since the last save back by itself;/E3).
        self._fold_pending = False
        self._reg28_read_since_load = False
        self.record: EnergyRecord | None = None  # the open record
        self.last: EnergyRecord | None = None  # the last closed record (last_session)
        self.last_closed_at: datetime | None = None
        # The last closed record that delivered: the only one late reg-28
        # steps can belong to (an empty record never takes them).
        self.last_charge: EnergyRecord | None = None
        self.last_charge_closed_at: datetime | None = None
        self.last_session: dict[str, Any] | None = None
        self._next_rid = 1

        # integral path
        self._prev: Sample | None = None
        self._prev_power: float | None = None  # last plausible reg 12
        # Energy in status 3 before the record opens on 4: held
        # and given to the record, or to the totals if the start fails.
        self._pre: list[tuple[float, float, datetime]] = []

        # register path
        self._reg28: int | None = None  # baseline (0.1 kWh)
        self._reg28_charge_s = 0.0  # time in status 3/4 since the baseline last moved
        # Wall time and status of the last sample, saved: HA downtime while a
        # charge ran is charging time for the bound.
        self._last_sample_at: datetime | None = None
        self._last_status: int | None = None
        self._downtime_from: tuple[datetime, int | None] | None = None
        self._reg28_suspect: int | None = None  # an implausible value waiting for a second read
        self.register_available: bool | None = None  # False: reg 28 stayed 0 over a whole charge
        self._reg28_zero_logged = False

        # lifecycle
        self._stop_reason: str | None = None
        self._stop_at: datetime | None = None
        self._phase_switch = False  # inside an own sequence
        self._restored_open = False
        self._save_requested = False

        self.diag: dict[str, Any] = {
            "gaps": 0,
            "power_dropped": 0,
            "reg28_rebases": 0,
            "reg28_glitches": 0,
            "reg28_zero_ignored": 0,
            "late_steps_wh": 0.0,
            "late_steps_ignored_wh": 0.0,
            "unattributed_wh": 0.0,
            "out_of_order": 0,
        }

    # --- views ----------------------------------------------------------------------

    @property
    def record_open(self) -> bool:
        return self.record is not None

    @property
    def tariff_set(self) -> bool:
        return self.tariff.price > 0 or bool(self.tariff.night_price)

    @property
    def total_energy_wh(self) -> float:
        """``total_energy``: never below HA's last state after a crash.
        Register energy is complete by itself (stored baseline); the integral
        lost between the last save and the crash is covered by the floor."""
        if self._floor_wh is None:
            return self.total_wh
        return max(self.total_wh, self._floor_wh + self._integral_since_floor)

    def _local(self, at: datetime) -> datetime:
        return at.astimezone(ZoneInfo(self.tz_name))

    # --- options and commands (called by the command layer) -------------------

    def set_source(self, source: str) -> None:
        """Option ``energy_source``: applies to the next record; the open one
        keeps its source, so nothing is counted twice or lost."""
        if source in SOURCES:
            self.source = source

    def note_stop(self, reason: str = REASON_USER_STOP) -> None:
        """Our stop command was sent (any outcome): the record closes once the
        station is out of 4, within STOP_EXPIRE_S. A stop that leaves
        the station in 4 (B07 ignores it) keeps the record open, and a car
        pause after that is a pause, not our stop."""
        if self.record is not None:
            self._stop_reason = reason if reason in STOP_REASONS else REASON_UNKNOWN
            self._stop_at = self._wall()

    def note_start(self) -> None:
        """Our start: a stop sent earlier no longer decides how this record ends."""
        self._stop_reason = None
        self._stop_at = None

    def begin_phase_switch(self) -> None:
        """Start of an own on-the-fly phase switch: the station leaves
        4 by our stop; a 6 seen meanwhile is part of the sequence, not the
        car ending the charge."""
        self._phase_switch = True

    def end_phase_switch(self, restarted: bool) -> None:
        """End of. The record stays open through it; if the charge did
        not restart, it closes as ``phase_switch`` at the next poll out of 4."""
        self._phase_switch = False
        if not restarted and self.record is not None:
            self._stop_reason = REASON_PHASE_SWITCH
            self._stop_at = self._wall()

    def station_restarted(self) -> None:
        """ classified a station restart: close the record as
        ``power_loss``. The box was off, so nothing is integrated across it."""
        self._flush_pre()
        self.close(REASON_POWER_LOSS)
        self._prev = None
        self._prev_power = None
        self._phase_switch = False

    def restore_total(self, last_state_kwh: float | None) -> None:
        """After an HA crash the Store may be up to 60 s old: ``total_energy``
        never steps back below what HA last showed."""
        if isinstance(last_state_kwh, (int, float)) and not isinstance(last_state_kwh, bool):
            floor = float(last_state_kwh) * 1000.0
            if self._floor_wh is None or floor > self._floor_wh:
                self._floor_wh = floor
                self._integral_since_floor = 0.0
            if self._register_may_recover() and not self._reg28_read_since_load:
                self._fold_pending = True
            else:
                self._fold()

    def _register_may_recover(self) -> bool:
        """Could reg 28 still book energy charged between the last save and
        the crash? Only with a stored baseline and a register-sourced charge
        (or register mode / a register charge's late steps outside records)."""
        if self._reg28 is None:
            return False
        if self.record is not None:
            return self.record.source == SOURCE_REGISTER
        last = self.last_charge
        return self.source == SOURCE_REGISTER or (last is not None and last.source == SOURCE_REGISTER)

    def _fold(self) -> None:
        """Make the floor part of the booked total, once: later energy of any
        source adds on top of it, and a second restore cannot
        count the recovered steps again (E2)."""
        if self._floor_wh is not None:
            self.total_wh = max(self.total_wh, self._floor_wh + self._integral_since_floor)
        self._floor_wh = None
        self._integral_since_floor = 0.0
        self._fold_pending = False

    def roll(self, now: datetime | None = None) -> None:
        """Day / month roll, called at second 0 of every minute (as OCPP).
        Wiring the timer is step 5."""
        self.totals.roll(self._local(now or self._wall()))

    def take_save_request(self) -> bool:
        """True once after a record closed: save the Store now."""
        requested, self._save_requested = self._save_requested, False
        return requested

    # --- samples --------------------------------------------------------------------

    def sample(self, s: Sample) -> None:
        if s.t is None:
            return
        in_order = self._prev is None or s.t > self._prev.t
        if not in_order:
            if not s.poll:
                return  # an old verification read: nothing it could add
            # The poll's time is when it started; a verification read taken
            # during it may be newer. Its energy is skipped (the
            # read is integrated already), its state still counts.
            self.diag["out_of_order"] += 1
        now = self._wall()
        local = self._local(now)
        if self._downtime_from is not None:
            since, status = self._downtime_from
            self._downtime_from = None
            if self.record is not None or status in ENERGY_STATUSES:
                self._reg28_charge_s += max((now - since).total_seconds(), 0.0)
        if self._restored_open:
            # HA was down while the record was open: that time is never
            # integrated, so the record is not exact any more.
            self._restored_open = False
            if self.record is not None:
                self.record.note(NOTE_HA_RESTART)
        if s.status == C.STATUS_CHARGING and self.record is None:
            self._open(now)
        power = None
        if in_order:
            power = self._plausible_power(s)
            prev = self._prev
            if prev is not None:
                dt = s.t - prev.t
                # A gap (link loss) is unknown time: the car may have charged.
                if dt > C.ENERGY_GAP_MAX_S or prev.status in ENERGY_STATUSES or s.status in ENERGY_STATUSES:
                    self._reg28_charge_s += dt
            self._integrate(s, power, local)
            if self.record is None and s.status != C.STATUS_STARTING:
                self._flush_pre()
        if s.meter_tenths is not None:
            self._register(s.meter_tenths, now, local)
        rec = self.record
        if rec is not None and s.delivering and not rec.delivered:
            # A real charge, counted once even at 0 Wh (W23/W24).
            rec.delivered = True
            self.totals.add_session(local)
        if s.poll:
            self._lifecycle(s, now)
        if in_order:
            self._prev = s
            self._prev_power = power
            self._last_sample_at, self._last_status = now, s.status

    def _lifecycle(self, s: Sample, now: datetime) -> None:
        """Closing rules of, after this sample's energy went to the record."""
        if self.record is None:
            return
        if (
            self._stop_reason is not None
            and self._stop_at is not None
            and (now - self._stop_at).total_seconds() > STOP_EXPIRE_S
        ):
            _LOGGER.info("%s: our %s did not end the charge within %.0f s", self.name, self._stop_reason, STOP_EXPIRE_S)
            self._stop_reason = self._stop_at = None
        if self._stop_reason is not None and s.status is not None and s.status != C.STATUS_CHARGING:
            self.close(self._stop_reason, now)
        elif s.plugged is False:
            self.close(REASON_UNPLUG, now)
        elif s.status == C.STATUS_FINISHED and not self._phase_switch:
            self.close(REASON_STATION, now)

    def _open(self, now: datetime) -> None:
        rec = EnergyRecord(rid=self._next_rid, source=self.source, started=now.isoformat())
        self.record = rec
        self._next_rid += 1
        self._stop_reason = self._stop_at = None
        # Energy of status 3 just before belongs to this charge,
        # each piece priced at its own moment.
        for wh, ui_wh, at in self._pre:
            rec.integral_wh += wh
            rec.ui_wh += ui_wh
            self._book(SOURCE_INTEGRAL, wh, at, rec)
        self._pre = []
        _LOGGER.info("%s: charge record %s opened (source %s)", self.name, rec.rid, rec.source)

    def close(self, reason: str, now: datetime | None = None) -> EnergyRecord | None:
        rec = self.record
        if rec is None:
            return None
        now = now or self._wall()
        rec.finished = now.isoformat()
        rec.reason = reason if reason in REASONS else REASON_UNKNOWN
        if rec.delivered and rec.register_seen and not rec.register_nonzero:
            # reg 28 stayed 0 over a whole charge (A3C_2.7).
            if self.register_available is not False:
                _LOGGER.warning("%s: reg 28 stayed 0 during a charge: register energy path unavailable", self.name)
            self.register_available = False
        self.record = None
        self.last = rec
        self.last_closed_at = now
        if rec.delivered:
            self.last_charge = rec
            self.last_charge_closed_at = now
        self._stop_reason = self._stop_at = None
        self._phase_switch = False
        self._refresh_last_session()
        self._save_requested = True
        _LOGGER.info(
            "%s: charge record %s closed (%s): %.3f kWh by %s, integral %.3f, register %.3f%s",
            self.name, rec.rid, rec.reason, rec.energy_wh / 1000, rec.source, rec.integral_wh / 1000,
            rec.register_wh / 1000, " approximate" if rec.approximate else "",
        )
        return rec

    # --- money ----------------------------------------------------------------------

    def _book(
        self, path: str, wh: float, local: datetime, rec: EnergyRecord | None, source: str | None = None
    ) -> None:
        """Energy of one path goes to totals and money only if it is the
        source of its record (outside records: ``source``, else the option)."""
        if wh <= 0 or path != (rec.source if rec is not None else (source or self.source)):
            return
        # Priced at its own moment: a night tariff starting mid-charge
        # prices the later energy at the night price.
        cost = wh / 1000.0 * self.tariff.price_at(local)
        self.totals.add(wh, cost, local)
        self.total_wh += wh
        if path == SOURCE_INTEGRAL and self._floor_wh is not None:
            self._integral_since_floor += wh
        if rec is not None:
            rec.cost += cost
        else:
            self.diag["unattributed_wh"] += wh

    # --- integral path -------------------------------------------------

    def _plausible_power(self, s: Sample) -> float | None:
        p = s.power_w
        if p is None:
            return None
        ui = _ui_w(s)
        if p >= 0 and (ui is None or p <= POWER_SLACK_FACTOR * ui + POWER_SLACK_W):
            return float(p)
        self.diag["power_dropped"] += 1
        rec = self.record
        if rec is not None and not rec.power_dropped:
            rec.power_dropped = True
            _LOGGER.warning(
                "%s: reg 12 = %s W with sum U x I = %s W: sample dropped, previous power kept", self.name, p, ui
            )
        return self._prev_power

    @staticmethod
    def _flowing(s: Sample, power: float | None) -> float:
        if s.status in ENERGY_STATUSES and power is not None and power > 0:
            return power
        return 0.0

    def _integrate(self, s: Sample, power: float | None, local: datetime) -> None:
        prev = self._prev
        if prev is None:
            return
        dt = s.t - prev.t
        if dt > C.ENERGY_GAP_MAX_S:
            # Link loss or a stalled poll: what happened in between is unknown.
            # Not integrated, and the record says so.
            self.diag["gaps"] += 1
            if self.record is not None:
                self.record.note(NOTE_GAP)
                _LOGGER.info("%s: %.0f s without a power sample: not integrated", self.name, dt)
            return
        p0, p1 = self._flowing(prev, self._prev_power), self._flowing(s, power)
        wh = (p0 + p1) / 2.0 * dt / 3600.0
        ui0 = _ui_w(prev) if prev.status in ENERGY_STATUSES else 0.0
        ui1 = _ui_w(s) if s.status in ENERGY_STATUSES else 0.0
        ui_wh = ((ui0 or 0.0) + (ui1 or 0.0)) / 2.0 * dt / 3600.0
        rec = self.record
        if rec is None and s.status == C.STATUS_STARTING:
            if wh > 0 or ui_wh > 0:
                self._pre.append((wh, ui_wh, local))
            return
        if rec is not None:
            rec.integral_wh += wh
            rec.ui_wh += ui_wh
        self._book(SOURCE_INTEGRAL, wh, local, rec)

    def _flush_pre(self) -> None:
        """A start that never reached 4: its energy still went into the car."""
        for wh, _ui_wh, at in self._pre:
            self._book(SOURCE_INTEGRAL, wh, at, None)
        self._pre = []

    # --- register path --------------------------------------------------

    def _plausible_step(self, delta: int) -> bool:
        seconds = self._reg28_charge_s + C.REG28_LATE_S
        return 0 < delta * WH_PER_TENTH <= self.p_max_w * seconds / 3600.0 + REG28_SLACK_WH

    def _set_base(self, value: int) -> None:
        self._reg28 = value
        self._reg28_charge_s = 0.0
        self._reg28_suspect = None

    def _register(self, value: int, now: datetime, local: datetime) -> None:
        self._register_value(value, now, local)
        self._reg28_read_since_load = True
        if self._fold_pending:
            self._fold()

    def _register_value(self, value: int, now: datetime, local: datetime) -> None:
        rec = self.record
        if rec is not None:
            rec.register_seen = True
            if value:
                rec.register_nonzero = True
        if value and self.register_available is False:
            _LOGGER.info("%s: reg 28 reads %s again: register energy path available", self.name, value)
        if value:
            self.register_available = True
        base = self._reg28
        if base is None:
            self._set_base(value)
            return
        if value == 0 and base > 0:
            # A 32-bit lifetime meter does not return to 0: a 0 after a
            # non-zero value is not a reading (it is shown as read, M17, but
            # never moves the baseline). A real meter reset shows up as a
            # drop on the next non-zero value and is rebased then.
            self.diag["reg28_zero_ignored"] += 1
            if not self._reg28_zero_logged:
                self._reg28_zero_logged = True
                _LOGGER.warning("%s: reg 28 read 0 after %s: ignored for energy", self.name, base)
            return
        if base == 0 and value > 0:
            # The first lifetime value after zeros (A3C reports 0; a firmware
            # starts reporting it): a baseline, never energy.
            _LOGGER.info("%s: reg 28 reports %s after zeros: new baseline", self.name, value)
            self._set_base(value)
            return
        delta = value - base
        if delta == 0 or self._plausible_step(delta):
            if self._reg28_suspect is not None:
                # One odd read between two good ones: a glitch, the energy
                # between the good reads is kept.
                self.diag["reg28_glitches"] += 1
                _LOGGER.info("%s: reg 28 read %s once between %s and %s: ignored", self.name,
                             self._reg28_suspect, base, value)
                self._reg28_suspect = None
            if delta:
                self._set_base(value)
                self._attribute_register(delta * WH_PER_TENTH, now, local)
            return
        suspect = self._reg28_suspect
        if suspect is not None and (value == suspect or self._plausible_step(value - suspect)):
            # The new level holds over two reads: a drop (meter replaced,
            # firmware) or a jump (factor-10 firmware bug) is real for the
            # register, never energy. Count on from the new level.
            self.diag["reg28_rebases"] += 1
            _LOGGER.warning("%s: reg 28 %s -> %s held over two reads: rebased", self.name, base, suspect)
            if rec is not None:
                rec.note(NOTE_REG28_REBASE)
            self._set_base(value)
            if value > suspect:
                self._attribute_register((value - suspect) * WH_PER_TENTH, now, local)
            return
        # Implausible against the baseline: wait for the next read.
        self._reg28_suspect = value

    def _attribute_register(self, wh: float, now: datetime, local: datetime) -> None:
        """the box adds a charge to reg 28 lazily, up to ~15 min late.

        A step within REG28_LATE_S of the close of the last charge goes to that
        charge until a newer record has started delivering (timing decides). Only a closed record whose own source is
        ``register`` takes it - for an integral record it is ignored for the
        record and for the totals, so a source change never counts energy
        twice. A record that never delivered is no charge and
        takes nothing. The late energy is priced at the
        end of its charge: it was delivered then, not when the
        box got round to adding it.
        """
        rec = self.record
        last = self.last_charge
        late = (
            last is not None
            and self.last_charge_closed_at is not None
            and (now - self.last_charge_closed_at).total_seconds() <= C.REG28_LATE_S
            and not (rec is not None and rec.delivered)
        )
        if late:
            if last.source != SOURCE_REGISTER:
                self.diag["late_steps_ignored_wh"] += wh
                _LOGGER.debug("%s: late reg 28 step %.0f Wh ignored (record %s is integral)", self.name, wh, last.rid)
                return
            last.register_wh += wh
            self.diag["late_steps_wh"] += wh
            finished = _parse(last.finished)
            self._book(SOURCE_REGISTER, wh, self._local(finished) if finished else local, last)
            self._refresh_last_session()
            self._save_requested = True
            _LOGGER.info("%s: late reg 28 step %.0f Wh added to record %s", self.name, wh, last.rid)
            return
        if rec is not None:
            rec.register_wh += wh
            self._book(SOURCE_REGISTER, wh, local, rec)
            return
        # Outside any record and later than REG28_LATE_S: totals only. While
        # the last charge is still the newest one, its source decides, not
        # the option changed since.
        source = last.source if last is not None else self.source
        if source != SOURCE_REGISTER:
            self.diag["late_steps_ignored_wh"] += wh
        self._book(SOURCE_REGISTER, wh, local, None, source=source)

    # --- sensors ------------------------------------------------------

    def _session_dict(self, rec: EnergyRecord) -> dict[str, Any]:
        started, finished = _parse(rec.started), _parse(rec.finished)
        minutes = round((finished - started).total_seconds() / 60) if started and finished else None
        return {
            "rid": rec.rid,
            "energy_kwh": round(rec.energy_wh / 1000.0, 3),
            "started": rec.started,
            "finished": rec.finished,
            "minutes": minutes,
            "cost": round(rec.cost, 2),
            "approximate": rec.approximate,
            "reason": rec.reason,
            "energy_source": rec.source,
            "energy_integral_kwh": round(rec.integral_wh / 1000.0, 3),
            "energy_register_kwh": round(rec.register_wh / 1000.0, 3),
            "energy_ui_kwh": round(rec.ui_wh / 1000.0, 3),
        }

    def _refresh_last_session(self) -> None:
        if self.last is not None:
            self.last_session = self._session_dict(self.last)

    def _money(self, value: float) -> float | None:
        # No tariff: "unknown", not 0.00 (zero reads as "charging is free"; OCPP parity).
        return round(value, 2) if self.tariff_set else None

    def sensor_values(self) -> dict[str, SensorValue]:
        rec = self.record or self.last
        session_attrs = None
        if rec is not None:
            session_attrs = {
                "energy_source": rec.source,
                "energy_integral_kwh": round(rec.integral_wh / 1000.0, 3),
                "energy_register_kwh": round(rec.register_wh / 1000.0, 3),
                "energy_ui_kwh": round(rec.ui_wh / 1000.0, 3),
                "approximate": rec.approximate,
                "register_path": None
                if self.register_available is None
                else ("available" if self.register_available else "unavailable"),
            }
        totals = self.totals
        last = self.last_session
        return {
            "session_energy": SensorValue(
                None if rec is None else round(rec.energy_wh / 1000.0, 3),
                None if rec is None else _parse(rec.started),
                session_attrs,
            ),
            "total_energy": SensorValue(round(self.total_energy_wh / 1000.0, 3)),
            "session_cost": SensorValue(self._money(rec.cost if rec is not None else 0.0)),
            "energy_today": SensorValue(
                round(totals.day_wh / 1000.0, 3), totals.start_of("day", self.tz_name, announced_only=True)
            ),
            "energy_month": SensorValue(
                round(totals.month_wh / 1000.0, 3), totals.start_of("month", self.tz_name, announced_only=True)
            ),
            "cost_today": SensorValue(
                self._money(totals.day_cost), totals.start_of("day", self.tz_name, announced_only=False)
            ),
            "cost_month": SensorValue(
                self._money(totals.month_cost), totals.start_of("month", self.tz_name, announced_only=False)
            ),
            "sessions_month": SensorValue(totals.month_sessions),
            "last_session": SensorValue(
                None if last is None else last.get("energy_kwh"),
                None,
                None
                if last is None
                else {
                    k: last.get(k)
                    for k in ("started", "finished", "minutes", "cost", "approximate", "reason", "energy_source")
                },
            ),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "record": self.record.as_dict() if self.record else None,
            "last": self.last.as_dict() if self.last else None,
            "total_wh": round(self.total_energy_wh, 1),
            "booked_wh": round(self.total_wh, 1),
            "floor_wh": self._floor_wh,
            "reg28_baseline": self._reg28,
            "reg28_suspect": self._reg28_suspect,
            "reg28_charge_s": round(self._reg28_charge_s),
            "register_available": self.register_available,
            **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.diag.items()},
        }

    # --- persistence ---------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        last_charge = self.last_charge
        return {
            "version": ENERGY_STORE_VERSION,
            "record": self.record.as_dict() if self.record else None,
            "last": self.last.as_dict() if self.last else None,
            "last_closed_at": _iso(self.last_closed_at),
            "last_charge": None if last_charge is None or last_charge is self.last else last_charge.as_dict(),
            "last_charge_is_last": last_charge is not None and last_charge is self.last,
            "last_charge_closed_at": _iso(self.last_charge_closed_at),
            "last_session": self.last_session,
            "next_rid": self._next_rid,
            # Booked energy and the crash floor apart: a save before the
            # floor is folded must not let the next restore count twice (E2).
            "total_wh": self.total_wh,
            "floor_wh": self._floor_wh,
            "integral_since_floor": self._integral_since_floor,
            "fold_pending": self._fold_pending,
            "last_sample_at": _iso(self._last_sample_at),
            "last_status": self._last_status,
            "totals": self.totals.as_dict(),
            "reg28": self._reg28,
            "reg28_charge_s": self._reg28_charge_s,
            "register_available": self.register_available,
            "stop_reason": self._stop_reason,
            "stop_at": _iso(self._stop_at),
            "diag": dict(self.diag),
        }

    def load(self, data: Any) -> None:
        """Restore before the first poll. The monotonic clock does not survive
        an HA restart, so integration starts again from the first new sample
        (HA downtime is never integrated); reg 28 continues from its stored
        baseline, so the register path loses nothing.

        Never raises: unreadable fields are skipped, a
        newer format is not read as this one."""
        if not isinstance(data, dict):
            return
        version = data.get("version")
        if isinstance(version, int) and version > ENERGY_STORE_VERSION:
            _LOGGER.warning("%s: energy store version %s is newer than %s: not read", self.name, version,
                            ENERGY_STORE_VERSION)
            return
        try:
            self._load(data)
        except Exception:  # a damaged Store must not stop the station from starting
            _LOGGER.exception("%s: energy store unreadable: starting from zero", self.name)
            self._clear()

    def _load(self, data: dict[str, Any]) -> None:
        self._clear()
        self.record = EnergyRecord.from_dict(data.get("record"))
        self.last = EnergyRecord.from_dict(data.get("last"))
        self.last_closed_at = _parse(data.get("last_closed_at"))
        if data.get("last_charge_is_last") is True:
            self.last_charge = self.last
        else:
            self.last_charge = EnergyRecord.from_dict(data.get("last_charge"))
        self.last_charge_closed_at = _parse(data.get("last_charge_closed_at"))
        last_session = data.get("last_session")
        self.last_session = dict(last_session) if isinstance(last_session, dict) else None
        rids = [r.rid for r in (self.record, self.last, self.last_charge) if r is not None]
        next_rid = data.get("next_rid")
        valid_rid = isinstance(next_rid, int) and not isinstance(next_rid, bool)
        self._next_rid = max([next_rid if valid_rid else 1, *(r + 1 for r in rids)])
        total = data.get("total_wh")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            self.total_wh = max(float(total), 0.0)
        self.totals = _totals_from(data.get("totals"))
        reg28 = data.get("reg28")
        if isinstance(reg28, int) and not isinstance(reg28, bool) and reg28 >= 0:
            self._reg28 = reg28
        charge_s = data.get("reg28_charge_s")
        if isinstance(charge_s, (int, float)) and not isinstance(charge_s, bool) and charge_s >= 0:
            self._reg28_charge_s = float(charge_s)
        available = data.get("register_available")
        self.register_available = available if isinstance(available, bool) else None
        floor = data.get("floor_wh")
        if isinstance(floor, (int, float)) and not isinstance(floor, bool) and floor >= 0:
            self._floor_wh = float(floor)
            since = data.get("integral_since_floor")
            if isinstance(since, (int, float)) and not isinstance(since, bool) and since >= 0:
                self._integral_since_floor = float(since)
            self._fold_pending = data.get("fold_pending") is True
        last_at = _parse(data.get("last_sample_at"))
        if last_at is not None:
            last_status = data.get("last_status")
            self._last_sample_at = last_at
            self._last_status = last_status if isinstance(last_status, int) and not isinstance(last_status, bool) else None
            self._downtime_from = (last_at, self._last_status)
        if self.record is not None:
            stop_reason = data.get("stop_reason")
            if stop_reason in REASONS:
                self._stop_reason = stop_reason
                # A stop without a time (older Store) counts from now.
                self._stop_at = _parse(data.get("stop_at")) or self._wall()
            # The flag is not restored: a sequence does not survive an
            # HA restart, and a stale flag would keep status 6 from closing
            # the record.
        diag = data.get("diag")
        if isinstance(diag, dict):
            for key in self.diag:
                value = diag.get(key)
                if isinstance(value, type(self.diag[key])) and not isinstance(value, bool):
                    self.diag[key] = value
        self._restored_open = self.record is not None


# --- the station adapter --------------------------------------------------------------


class StationEnergy:
    """Feeds one ``Station`` into an ``EnergyMeter``.

    * Every new poll (``Snapshot.time`` changed) becomes a sample; failed
      polls and data of another station never reach the meter (the station
      layer does not commit them).
    * The station's ``station_restarted`` event closes the record as
      ``power_loss``; the event is emitted before the snapshot of the same
      poll, so a charge the box resumed by itself opens a new record.
    * ``station.record_open`` follows the meter (the stop of an idle station
      needs no write).
    * The meter state is a section of the station's per-serial Store; it is
      saved with the station's debounced saves and at once when a record
      closes.
    """

    def __init__(self, station: Any, meter: EnergyMeter) -> None:
        self.station = station
        self.meter = meter
        self._last_time: float | None = None
        self._tasks: set[asyncio.Task] = set()
        station.add_store_section(STORE_SECTION, meter.as_dict, meter.load)
        self._unsubscribe = [station.subscribe(self._on_snapshot), station.subscribe_events(self._on_event)]
        station.record_open = meter.record_open

    def _on_event(self, name: str, data: dict[str, Any]) -> None:
        if name == C.EVENT_RESTARTED:
            self.meter.station_restarted()
            self._after()

    def _on_snapshot(self, snap: Any) -> None:
        if snap.time is None or snap.time == self._last_time:
            return  # a failed or skipped poll: no new data
        self._last_time = snap.time
        self.meter.sample(sample_from_snapshot(snap))
        self._after()

    def feed(self, t: float, raw: dict[int, int]) -> None:
        """Verification reads of a sequence: power keeps being
        integrated while tier A is paused, so there is no gap."""
        self.meter.sample(sample_from_registers(t, raw, tuple(self.station.snapshot.voltages)))
        self._after()

    # command layer hooks
    def note_stop(self, reason: str = REASON_USER_STOP) -> None:
        self.meter.note_stop(reason)

    def note_start(self) -> None:
        self.meter.note_start()

    def begin_phase_switch(self) -> None:
        self.meter.begin_phase_switch()

    def end_phase_switch(self, restarted: bool) -> None:
        self.meter.end_phase_switch(restarted)

    def _after(self) -> None:
        self.station.record_open = self.meter.record_open
        if self.meter.take_save_request():
            self._save_now()

    def _save_now(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (plain unit use): the next save carries it
        task = loop.create_task(self.station.async_save())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []


__all__ = [
    "DEFAULT_SOURCE",
    "EnergyMeter",
    "EnergyRecord",
    "REASONS",
    "SENSOR_CLASSES",
    "SOURCES",
    "Sample",
    "SensorValue",
    "StationEnergy",
    "p_max_w",
    "sample_from_registers",
    "sample_from_snapshot",
]
