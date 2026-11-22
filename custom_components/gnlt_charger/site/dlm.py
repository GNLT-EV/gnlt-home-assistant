"""Dynamic load management (DLM) for several stations behind one grid connection.

Pure Python, no Home Assistant imports. One call of:meth:`DlmAllocator.allocate` is one
control cycle: grid meter reading + station inputs in, allowed current per station out.

Behaviour source
----------------
The reference is openWB 2.x load management, described in our clean-room behaviour
specification (not part of this repository). openWB is
GPL-3.0: this module was written from that specification only, no openWB code was read.
The ``openWB file:line`` notes below are the citations the specification gives for each
behaviour; they are kept so a rule can be traced back to the reference.

Scope
-----
- One site, one grid counter. Intermediate (sub-distribution) counters and virtual
  counters of openWB are out of scope. The budget is kept in a
  ``_Counter`` object and bookings go through ``_branch()``, so a counter tree can be
  added later by returning every counter on a station's path; nothing of that exists yet.
- Out of scope as in the spec: PV surplus, bidirectional charging, phase switching,
  §14a dimming, charge modes. A station takes part when its loadpoint asks for current
  (``requested_a > 0``); "user priority list" = ``priority``.

Cycle (openWB control/algorithm/algorithm.py:24-60)
--------------------------------------------------------------
1. Meter health (spec §7.1, plus ADDITION (a) and DEVIATION hold_on_stale_meter).
2. Headroom per phase and power headroom.
3. Fixed bookings: reserved stations, measured draw above setpoint (ADDITIONS (b), (c)).
4. Per priority item, in ascending ``priority``; equal priority = one group:
   minimum pass, then additional equal-split pass.
5. Hold pass (DEVIATION hold_measured_until_drop): each station keeps max(new booking,
   present draw) until a reading shows its drop, so increases that do not fit yet are
   capped and come in the next cycle ("decrease first, increase next cycle").
6. Result: allowed current floored to 0.1 A (our station step; openWB rounds to 0.01 A) and a reason code.

There is no ramp, hysteresis or minimum on/off time: openWB has none in DLM. Reductions apply in the cycle that computes them; with the hold pass an increase
that needs current another station is still drawing waits one cycle.

DEVIATIONS from openWB - safety. Each is a switch whose
default is the safe value; the openWB value stays selectable. Proof = the named test.

- ``substitute_against_setpoint`` (True; openWB False) -
  test_review1_increase_is_booked_in_full_not_at_old_measured.
- ``hold_measured_until_drop`` (True; openWB False) -
  test_review5_stop_request_while_still_drawing_keeps_its_current_this_cycle.
- ``hold_on_stale_meter`` (True; openWB False) -
  test_review2_meter_in_error_grace_gives_no_increase.
- ``meter_error_grace_s`` (3 × meter_period_s = 30 s; openWB 60 s) -
  test_review2_error_grace_default_3_meter_periods.
- ``clamp_negative_available`` (True; openWB False) -
  test_review8_power_limit_not_inflated_by_negative_headroom_on_unused_phase.
- ``drawing_first_in_group`` (True; openWB False = list order; availability) -
  test_reviewN2_drawing_station_gets_minimum_before_idle_ones_in_its_group.
- ``substitute_against_setpoint`` also covers N1 (under-draw only after the setpoint stayed
  unraised > 60 s, internal per-station state) - test
  test_reviewN1_reading_lagging_a_raised_setpoint_is_not_an_under_drawer.

ADDITIONS - not in openWB, added for safety. Each one has a switch; the value that gives openWB behaviour is named.

- (a) Grid meter failure, ``meter_*`` settings: staleness by reading age, frozen-meter
  detection, three failure actions, fallback budget capped by the fuse.
- (b) Lost station / station not following its setpoint: offline or stale stations keep a
  reservation; a station without any reading since start is reserved at its maximum; a
  station drawing more than its setpoint is booked at its measured current.
- (c) Measured vs setpoint: openWB's "book measured when lower" is kept as is; the
  "book measured when higher" part of (b) is the addition.
- Overload: a meter phase above the fuse keeps its negative headroom (no clip to 0).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import time

PHASES = 3
_EPS = 1e-9
_EPS_HOLD = 1e-6  # float slack of the hold pass (A); far below the 0.1 A output step
OPENWB_ERRORCASE_VOLTAGE_V = 230.0  # spec §7.1: errorcase / 230 / 3, a fixed 230 V

Vec = tuple[float, float, float]
ZERO: Vec = (0.0, 0.0, 0.0)


class Reason(StrEnum):
    """Why a station got what it got (one code per station and cycle)."""

    NONE = "none"  # got its full required current
    NOT_REQUESTED = "not_requested"  # loadpoint asks for 0 A
    NOT_CHARGING = "not_charging"  # kept at its minimum: car not drawing
    MIN_NOT_FITTING = "min_not_fitting"  # minimum does not fit → 0 = pause
    CURRENT_LIMIT = "current_limit"  # fuse headroom
    POWER_LIMIT = "power_limit"  # total power limit
    UNBALANCED = "unbalanced"  # unbalanced-load limit
    METER_FALLBACK = "meter_fallback"  # grid meter failed (spec §7.1, ADDITION (a))
    OVERLOAD = "overload"  # meter phase above the fuse (ADDITION)
    WAIT_DROP = "wait_drop"  # increase deferred until other stations' cuts show in readings
    METER_HOLD = "meter_hold"  # no increase: the grid meter value is not new
    RESERVED_OFFLINE = "reserved_offline"  # lost station, current reserved (ADDITION (b))
    RESERVED_UNKNOWN = "reserved_unknown"  # no reading since start, max reserved (ADDITION (b))
    OFFLINE = "offline"  # lost station, nothing reserved (openWB, reservation switched off)


class MeterState(StrEnum):
    OK = "ok"
    ERROR_GRACE = "error_grace"  # error flag < meter_error_grace_s; held
    NOT_NEW = "not_new"  # no new value this cycle → last fresh base load, no increase
    FAILED_ERROR = "failed_error"  # error flag ≥ meter_error_grace_s
    FAILED_STALE = "failed_stale"  # ADDITION (a): reading too old, or no reading
    FAILED_FROZEN = "failed_frozen"  # ADDITION (a): identical values while own currents move

    @property
    def failed(self) -> bool:
        return self.value.startswith("failed")


class MeterFailureAction(StrEnum):
    """ADDITION (a): what to do while the grid meter has failed."""

    OPENWB = "openwb"  # fallback budget max_power_errorcase_w, capped by the fuse
    MINIMUM = "minimum"  # every requesting station at its minimum, by priority, within the fuse
    STOP = "stop"  # allowed 0 for every station


@dataclass(frozen=True)
class DlmConfig:
    """Site settings. Defaults follow spec §8 unless marked ADDITION."""

    fuse_limit_a: Vec  # per grid phase (openWB counter max_currents)
    max_total_power_w: float | None = None  # openWB max_total_power, grid counter only
    phase_voltage_v: float = 230.0  # power = current × this (openWB: station mean voltage)
    unbalanced_load: bool = False  # spec §2.2, openWB control/general.py:82-83
    unbalanced_load_limit_a: float = 18.0  # spec §2.2, openWB control/general.py:80-81
    # spec §4.3: openWB consider_less_charging=False (default) = substitution active
    book_measured_when_lower: bool = True
    # DEVIATION from openWB - safety, proof: test test_review1_increase_is_booked_in_full_not_at_old_measured.
    # True: under-draw is judged against the LAST setpoint (setpoint − nominal_difference ≥ measured
    # mean); then min(value, mean + max(value − setpoint, 0)) is booked, never below the minimum
    # while the value is ≥ the minimum. False = openWB: value − nominal_difference ≥ mean → book mean,
    # which books an increase at the old draw.
    substitute_against_setpoint: bool = True
    nominal_difference_a: float = 1.0  # spec §4.3, openWB control/ev/ev_template.py:22
    less_charging_timeout_s: float = 60.0  # spec §4.3, openWB control/algorithm/common.py:17
    standby_threshold_a: float = 0.5  # spec §2.7, openWB control/load_protocol.py:46
    # How often the grid meter delivers a new value; the two timeouts below default to 3× this.
    meter_period_s: float = 10.0
    # spec §7.1 MAX_EVU_ERROR_DURATION is 60 s (openWB control/counter.py:135).
    # DEVIATION from openWB - safety, proof: test test_review2_error_grace_default_3_meter_periods.
    # None = 3 × meter_period_s, at most 60 s (N6) → 30 s by default; 60.0 = openWB.
    meter_error_grace_s: float | None = None
    max_power_errorcase_w: float = 7000.0  # spec §7.1, openWB control/counter.py:28
    # ADDITION (a) - not in openWB, added for safety. meter_stale_check=False = off (openWB).
    # None = 3 × meter_period_s, at most 60 s (N6) → 30 s by default.
    meter_stale_check: bool = True
    meter_stale_after_s: float | None = None
    # DEVIATION from openWB - safety, proof: test test_review2_meter_in_error_grace_gives_no_increase.
    # True: while the meter value is not new (error grace, timestamp not newer than last cycle,
    # or identical to the last fresh value while own currents moved) the last FRESH base load is
    # used and no station gets an increase. False = openWB: the old value is used as valid.
    hold_on_stale_meter: bool = True
    # ADDITION (a) - not in openWB, added for safety. 0 = off (openWB).
    meter_frozen_cycles: int = 6
    meter_frozen_min_change_a: float = 1.0
    # N6: the MINIMUM failure action uses the last fresh base load only up to this age (s);
    # older or never seen → errorcase budget.
    minimum_base_max_age_s: float = 300.0
    # N4: meter resolution. Values within half a step count as identical; own currents must
    # move by meter_frozen_min_change_a + this before an unchanged value is suspicious.
    meter_resolution_a: float = 0.1
    # ADDITION (a) - not in openWB, added for safety. OPENWB = openWB's fallback.
    meter_failure_action: MeterFailureAction = MeterFailureAction.OPENWB
    # ADDITION (a) - not in openWB, added for safety. False = exact openWB.
    cap_errorcase_by_fuse: bool = True
    # ADDITION (b)/(c) - not in openWB, added for safety. False = openWB (excess invisible).
    book_measured_when_higher: bool = True
    overdraw_tolerance_a: float = 0.2
    # ADDITION (b) - not in openWB, added for safety. False = openWB (booking dropped).
    reserve_lost_stations: bool = True
    station_stale_after_s: float = 60.0  # openWB module comm-error grace 60 s
    # ADDITION (b) - not in openWB, added for safety. False = normal allocation without reading.
    reserve_unknown_stations: bool = True
    # ADDITION - not in openWB, added for safety. False = openWB clip to 0.
    overload_unclipped: bool = True
    # DEVIATION from openWB - safety, proof: test
    # test_review8_power_limit_not_inflated_by_negative_headroom_on_unused_phase.
    # True: available current below 0 counts as 0 before the power and unbalanced steps.
    # False = openWB (a negative phase lowers the power sum of the request).
    clamp_negative_available: bool = True
    # DEVIATION from openWB - safety, proof: test
    # test_review5_stop_request_while_still_drawing_keeps_its_current_this_cycle.
    # True: every fresh station keeps max(new booking) per phase until a
    # reading shows its drop; others' increases are capped to what is free NOW and come the
    # next cycle ("decrease first, increase next cycle"). False = openWB: current a station is
    # told to give up is handed to others in the same cycle while it is still drawing.
    hold_measured_until_drop: bool = True
    # DEVIATION from openWB order - availability, proof: test
    # test_reviewN2_drawing_station_gets_minimum_before_idle_ones_in_its_group.
    # True: inside one priority group, stations that are drawing (measured mean ≥ standby
    # threshold) take their minimum before idle ones, so plugged-in idle cars are refused first
    # and cannot keep a charging car paused. False = openWB: input (list) order.
    drawing_first_in_group: bool = True

    def __post_init__(self) -> None:
        if len(self.fuse_limit_a) != PHASES or not all(_pos(f) for f in self.fuse_limit_a):
            raise ValueError("fuse_limit_a must be three positive finite currents")
        if self.max_total_power_w is not None and not _pos(self.max_total_power_w):
            raise ValueError("max_total_power_w must be positive and finite, or None")
        for name in (
            "phase_voltage_v", "unbalanced_load_limit_a", "max_power_errorcase_w",
            "less_charging_timeout_s", "station_stale_after_s", "meter_period_s",
            "minimum_base_max_age_s",
        ):
            if not _pos(getattr(self, name)):
                raise ValueError(f"{name} must be positive and finite")
        for name in (
            "nominal_difference_a", "standby_threshold_a", "meter_frozen_min_change_a",
            "meter_resolution_a",
            "overdraw_tolerance_a",
        ):
            v = getattr(self, name)
            if not (math.isfinite(v) and v >= 0):
                raise ValueError(f"{name} must be finite and not negative")
        for name in ("meter_error_grace_s", "meter_stale_after_s"):
            v = getattr(self, name)
            if v is None:
                object.__setattr__(self, name, min(3 * self.meter_period_s, 60.0))  # N6
            elif not _pos(v):
                raise ValueError(f"{name} must be positive and finite, or None")
        if self.meter_frozen_cycles < 0 or (0 < self.meter_frozen_cycles < 2):
            raise ValueError("meter_frozen_cycles must be 0 (off) or at least 2")
        object.__setattr__(self, "fuse_limit_a", tuple(float(f) for f in self.fuse_limit_a))
        object.__setattr__(self, "meter_failure_action", MeterFailureAction(self.meter_failure_action))


@dataclass(frozen=True)
class GridReading:
    """Grid meter: signed per-phase currents (import positive) and their freshness.

    ``updated_at`` = clock time of the last SUCCESSFUL read/poll of the meter, not the time
    the value last changed. A meter read again with the same value gets a newer timestamp and
    counts as fresh. Do not use a Home Assistant state's ``last_changed`` (it stays put while
    the value is constant); use the poll time (or ``last_reported``). A timestamp that is not
    newer than in the previous cycle means "no new value" (state not_new, no increases).
    """

    currents_a: Vec
    updated_at: float  # time of the last successful read/poll (see class docstring)
    error: bool = False  # meter/read error reported by the device layer


@dataclass(frozen=True)
class StationInput:
    """One station (loadpoint) as seen in this cycle.

    ``measured_a`` and ``setpoint_a`` are the last known values; whether they are fresh is
    told by ``online`` and ``last_seen``. ``last_seen=None`` = no reading since start.
    Currents are in the station's own phase order L1..L3; ``phase_1`` maps them to the grid.
    """

    station_id: str
    priority: int = 0  # position in the user priority list; equal = one sharing group
    phase_1: int = 1  # grid phase of station L1
    phases_in_use: int = 3  # for a station without reading: pass its connected phases
    min_current_a: float = 6.0  # spec §2.4 EV min_current
    max_current_a: float = 16.0  # spec §2.4 EV max
    # N6: the station's real hardware limit, if known; reserved for a station without reading
    hardware_max_a: float | None = None
    requested_a: float = 0.0  # what the loadpoint wants; 0 = does not want to charge
    setpoint_a: float = 0.0  # last value sent to the station
    measured_a: Vec | None = None
    # spec §2.7 timestamp_charge_start: clock time the first non-zero setpoint was sent after a
    # zero setpoint (not the time the car started drawing); None while the setpoint is 0.
    charging_since: float | None = None
    online: bool = True
    last_seen: float | None = None


@dataclass(frozen=True)
class StationAllocation:
    """Result for one station.

    ``allowed_a`` - what to send:
    - a value ≥ the station minimum, floored to 0.1 A: set this current;
    - ``0.0``: the station must pause (or stay off);
    - ``None``: do NOT send anything - the station is reserved (offline, stale reading or
      no reading since start; ``reason`` is ``reserved_offline`` or ``reserved_unknown``).
      Its reservation is in ``booked_a``.
    """

    station_id: str
    allowed_a: float | None  # see class docstring
    reason: Reason
    required_a: float  # request after the min/max clamp; 0 = none
    booked_a: Vec  # what this station holds of the budget, per grid phase
    headroom_a: Vec  # grid headroom per phase when its last decision was taken


@dataclass(frozen=True)
class DlmResult:
    stations: dict[str, StationAllocation]
    meter_state: MeterState
    headroom_start_a: Vec  # after base load, before any booking
    headroom_left_a: Vec  # after all bookings
    power_left_w: float | None
    overload_a: Vec  # meter phase current above the fuse (0 if not)


# --- small helpers ------------------------------------------------------------------


def grid_phases(phase_1: int, phases: int) -> tuple[int, ...]:
    """Grid phase indices used by ``phases`` station phases (openWB
    helpermodules/phase_handling.py:8-9, 25-26; control/chargepoint/chargepoint.py:594-602)."""
    return tuple((phase_1 - 1 + k) % PHASES for k in range(phases))


def to_grid(phase_1: int, station_currents: Sequence[float]) -> Vec:
    """Station L1..L3 currents in grid order (openWB phase_handling.py:16-18)."""
    out = [0.0, 0.0, 0.0]
    for k in range(PHASES):
        out[(phase_1 - 1 + k) % PHASES] = float(station_currents[k])
    return (out[0], out[1], out[2])


def _pos(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _finite(*values: float | None) -> bool:
    return all(v is None or math.isfinite(v) for v in values)


def _floor01(x: float) -> float:
    return math.floor(x * 10 + 1e-6) / 10


def _ceil01(x: float) -> float:
    return math.ceil(x * 10 - 1e-6) / 10


def _vec(values: Iterable[float]) -> Vec:
    a, b, c = values
    return (a, b, c)


# --- working state ------------------------------------------------------------------


@dataclass
class _Counter:
    """Budget of one counter (here: the grid counter only).

    ``headroom`` = openWB raw_currents_left, ``power_left`` = raw_power_left; after bookings
    they are not clipped and may go negative (openWB control/counter.py:307).
    """

    headroom: list[float]
    power_left: float | None
    voltage: float
    unbalanced_limit: float | None


@dataclass
class _Load:
    inp: StationInput
    used: tuple[int, ...]
    min_a: float
    max_a: float
    required: float
    measured_grid: Vec | None  # None when not fresh / unknown
    mean: float
    floor: Vec  # ADDITION (b)/(c): booking never below this
    booked: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    current: float = 0.0  # openWB set.current / target_current
    reason: Reason = Reason.NONE
    headroom_seen: Vec = ZERO
    allowed: float | None = 0.0
    refused: bool = False
    under_drawer: bool = False  # N1: confirmed under-draw (setpoint unchanged > timeout)

    @property
    def charging(self) -> bool:
        return self.mean >= _EPS


class DlmAllocator:
    """Stateful for the meter health checks (error timer, frozen history, last fresh
    reading) and, per station id, for the time of the last setpoint increase (N1: a car
    counts as drawing less than its setpoint only when that setpoint has not been raised for
    more than ``less_charging_timeout_s``; a reading that lags a raise is not under-draw).
    Stations missing from an input are forgotten.

    ``clock`` returns seconds on the same base as every timestamp in the inputs.
    """

    def __init__(self, config: DlmConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._error_since: float | None = None
        n = config.meter_frozen_cycles
        self._history: deque[tuple[Vec, Vec]] = deque(maxlen=max(n, 1))
        # last fresh meter cycle (meter currents, base = meter − own)
        self._last_fresh: tuple[Vec, Vec, Vec] | None = None
        self._last_updated: float | None = None
        self._last_fresh_t: float | None = None  # N6: when _last_fresh was taken
        self._now = 0.0
        self._frozen_value: Vec | None = None  # latched stuck value
        self._history_t: float | None = None  # N4: timestamp of the last history entry
        # N1: station id → (last setpoint seen, time of the last setpoint increase)
        self._setpoint_since: dict[str, tuple[float, float]] = {}

    # --- public -----------------------------------------------------------------

    def allocate(self, meter: GridReading | None, stations: Sequence[StationInput]) -> DlmResult:
        cfg = self.config
        now = self._clock()
        self._now = now
        _validate(stations)
        loads = [self._make_load(s, now) for s in stations]
        fresh = [ld for ld in loads if ld.measured_grid is not None]
        self._track_setpoints(loads, now)

        state, currents, base, no_increase = self._meter_state(meter, fresh, now)
        failed = state.failed
        action = cfg.meter_failure_action if failed else None

        overload = ZERO
        if not failed:
            assert currents is not None and base is not None
            grid = self._grid_counter_from_base(currents, base)
            overload = _vec(max(currents[p] - cfg.fuse_limit_a[p], 0.0) for p in range(PHASES))
        else:
            grid = self._grid_counter_fallback(action)
        headroom_start = _vec(grid.headroom)
        power_start = grid.power_left

        reserved, inactive, active = [], [], []
        for ld in loads:
            if ld.reason in (Reason.RESERVED_OFFLINE, Reason.RESERVED_UNKNOWN, Reason.OFFLINE):
                reserved.append(ld)
            elif ld.required <= 0:
                inactive.append(ld)
            else:
                active.append(ld)

        # Fixed bookings first: they are drawn whatever we decide (ADDITIONS (b), (c)).
        for ld in loads:
            ld.headroom_seen = headroom_start
            self._book(grid, ld, ld.floor)

        if action is MeterFailureAction.STOP:
            for ld in active:
                ld.current, ld.reason = 0.0, Reason.METER_FALLBACK
        else:
            for group in _priority_groups(active, cfg.drawing_first_in_group):
                self._minimum_pass(grid, group, now)
                if action is not MeterFailureAction.MINIMUM:
                    self._additional_pass(grid, group, now)

        for ld in inactive:
            ld.current, ld.reason = 0.0, Reason.NOT_REQUESTED
        left, power_left = _vec(grid.headroom), grid.power_left
        if cfg.hold_measured_until_drop or no_increase:
            ordered = [ld for group in _priority_groups(active, cfg.drawing_first_in_group) for ld in group]
            left, power_left = self._hold_pass(
                headroom_start, power_start, loads, ordered, now, no_increase=no_increase
            )
        for ld in active:
            ld.allowed = _floor01(ld.current) if ld.current > 0 else 0.0
            ld.reason = self._final_reason(ld, failed, overload)
        for ld in inactive:
            ld.allowed = 0.0
        for ld in reserved:
            ld.allowed = 0.0 if ld.reason is Reason.OFFLINE else None

        return DlmResult(
            stations={
                ld.inp.station_id: StationAllocation(
                    station_id=ld.inp.station_id,
                    allowed_a=ld.allowed,
                    reason=ld.reason,
                    required_a=ld.required,
                    booked_a=_vec(ld.booked),
                    headroom_a=ld.headroom_seen,
                )
                for ld in loads
            },
            meter_state=state,
            headroom_start_a=headroom_start,
            headroom_left_a=left,
            power_left_w=power_left,
            overload_a=overload,
        )

    # --- inputs -----------------------------------------------------------------

    def _make_load(self, s: StationInput, now: float) -> _Load:
        cfg = self.config
        used = grid_phases(s.phase_1, s.phases_in_use)
        # spec §2.4: 0 stays 0; below min → min; above max → max (openWB control/ev/ev.py:224-258).
        # Min/max taken in our 0.1 A station step (min up, max down) so a granted value is sendable.
        min_a, max_a = _ceil01(s.min_current_a), _floor01(s.max_current_a)
        required = 0.0 if s.requested_a <= 0 else float(min(max(s.requested_a, min_a), max_a))
        unknown = s.last_seen is None
        stale = (not s.online) or (s.last_seen is not None and now - s.last_seen > cfg.station_stale_after_s)
        ld = _Load(s, used, min_a, max_a, required, None, 0.0, ZERO)
        if not _finite(*(s.measured_a or ()), s.setpoint_a, s.last_seen, s.charging_since):
            # a non-finite reading = station state unknown → stale, its maximum
            # reserved on its phases (cannot use the broken value for max(setpoint)).
            ld.floor = _vec(max_a if p in used else 0.0 for p in range(PHASES))
            ld.reason = Reason.RESERVED_OFFLINE if cfg.reserve_lost_stations else Reason.OFFLINE
            if ld.reason is Reason.OFFLINE:
                ld.floor = ZERO
            return ld

        if unknown and cfg.reserve_unknown_stations:
            # ADDITION (b) - not in openWB, added for safety: no reading since start → we do not
            # know what it draws; hold its maximum on its phases until the first fresh reading.
            # N6: its hardware limit when given (what it can really draw), else max_current_a.
            top = s.hardware_max_a if s.hardware_max_a is not None else max_a
            ld.floor = _vec(top if p in used else 0.0 for p in range(PHASES))
            ld.reason = Reason.RESERVED_UNKNOWN
            return ld
        if stale and not unknown:
            if cfg.reserve_lost_stations:
                # ADDITION (b) - not in openWB, added for safety (openWB drops the booking after
                # ~60 s, spec §7.2/§9b): keep max(last setpoint) per grid phase.
                # Its last measured currents are NOT removed from the meter: they are not fresh,
                # so the reservation stands on top of the observed load (conservative; if the
                # station still draws, that draw is counted twice until it is back).
                last = to_grid(s.phase_1, s.measured_a) if s.measured_a is not None else ZERO
                ld.floor = _vec(
                    max(s.setpoint_a if p in used else 0.0, last[p], 0.0) for p in range(PHASES)
                )
                ld.reason = Reason.RESERVED_OFFLINE
            else:
                ld.reason = Reason.OFFLINE  # openWB after its grace: unplugged, nothing booked
            return ld

        local = s.measured_a if s.measured_a is not None else ZERO
        ld.measured_grid = to_grid(s.phase_1, local)
        # spec §2.7 get_mean_current: mean of |I| ≥ 0.5 A, 0 if none (openWB control/load_protocol.py:46, 53-55)
        active_phases = [abs(c) for c in local if abs(c) >= cfg.standby_threshold_a]
        ld.mean = sum(active_phases) / len(active_phases) if active_phases else 0.0
        # every grid phase the station really draws on (≥ standby threshold) is
        # booked, even if phases_in_use says fewer (wrong count, 1p car reported as 3p or the
        # other way). Matches spec §6.2: openWB takes the measured phases for such stations.
        drawn = {p for p in range(PHASES) if abs(ld.measured_grid[p]) >= cfg.standby_threshold_a}
        ld.used = tuple(sorted(set(used) | drawn))
        if cfg.book_measured_when_higher:
            # ADDITION (b)/(c) - not in openWB, added for safety: the station draws more than its
            # setpoint (cannot go below ~7.5 A at 6 A, or cannot stop). Its measured current is
            # its booking floor on that phase, so the excess is taken from the other stations,
            # not the fuse. On a phase it should not use at all the setpoint is 0.
            ld.floor = _vec(
                max(c, 0.0)
                if c > (s.setpoint_a if p in ld.used else 0.0) + cfg.overdraw_tolerance_a
                else 0.0
                for p, c in enumerate(ld.measured_grid)
            )
        return ld

    def _track_setpoints(self, loads: list[_Load], now: float) -> None:
        """N1 - part of DEVIATION substitute_against_setpoint, proof: test
        test_reviewN1_reading_lagging_a_raised_setpoint_is_not_an_under_drawer.
        A setpoint increase (or a station seen for the first time) restarts the timer; a
        decrease does not (a car never draws more because its setpoint was lowered)."""
        cfg = self.config
        seen: dict[str, tuple[float, float]] = {}
        for ld in loads:
            s = ld.inp
            prev = self._setpoint_since.get(s.station_id)
            sp = s.setpoint_a if math.isfinite(s.setpoint_a) else 0.0
            since = now if prev is None or sp > prev[0] + 0.05 else prev[1]
            seen[s.station_id] = (sp, since)
            ld.under_drawer = (
                ld.measured_grid is not None
                and sp > 0
                and sp - cfg.nominal_difference_a >= ld.mean
                and now - since > cfg.less_charging_timeout_s
            )
        self._setpoint_since = seen

    # --- meter --------------------------------------------------------------------

    def _meter_state(
        self, meter: GridReading | None, fresh: list[_Load], now: float
    ) -> tuple[MeterState, Vec | None, Vec | None, bool]:
        """Spec §7.1 (openWB control/counter.py:157-180) plus ADDITION (a) and.

        Returns (state, meter currents, base load, no_increase). Currents and base are None
        when the meter has failed."""
        cfg = self.config
        if (
            meter is None
            or len(meter.currents_a) != PHASES
            or not _finite(*meter.currents_a, meter.updated_at)
        ):
            # a missing or non-finite value can never be headroom.
            self._history.clear()
            return MeterState.FAILED_STALE, None, None, False
        currents = _vec(float(c) for c in meter.currents_a)
        own = _vec(sum(ld.measured_grid[p] for ld in fresh) for p in range(PHASES))  # type: ignore[index]
        # spec §7.1: error timer starts at the first faulty cycle, any healthy cycle clears it
        if meter.error:
            if self._error_since is None:
                self._error_since = now
            if now - self._error_since >= cfg.meter_error_grace_s:  # type: ignore[operator]
                return MeterState.FAILED_ERROR, None, None, False
        else:
            self._error_since = None
        # ADDITION (a) - not in openWB, added for safety: age of the last fresh meter value
        if cfg.meter_stale_check and now - meter.updated_at > cfg.meter_stale_after_s:  # type: ignore[operator]
            return MeterState.FAILED_STALE, None, None, False
        # the frozen check runs during the error grace too
        if self._frozen(currents, own, meter.updated_at):
            return MeterState.FAILED_FROZEN, None, None, False
        base_now = _vec(currents[p] - own[p] for p in range(PHASES))
        if not cfg.hold_on_stale_meter:
            if not meter.error:
                self._last_fresh = (currents, own, base_now)  # used by the MINIMUM action
                self._last_fresh_t = now
            return (MeterState.ERROR_GRACE if meter.error else MeterState.OK), currents, base_now, False
        # DEVIATION from openWB - safety, proof: test
        # test_review2_meter_in_error_grace_gives_no_increase. A value is new only without an
        # error flag, with a newer timestamp, and not equal to the last fresh value while our
        # own currents moved (a stuck value would turn our own rise into "less base load").
        new = not meter.error and (self._last_updated is None or meter.updated_at > self._last_updated)
        if new and self._last_fresh is not None:
            f_cur, f_own, _ = self._last_fresh
            if self._same(currents, f_cur) and self._moved(own, f_own):
                new = False
        if new:
            self._last_fresh = (currents, own, base_now)
            self._last_fresh_t = now
            self._last_updated = meter.updated_at
            return MeterState.OK, currents, base_now, False
        held = MeterState.ERROR_GRACE if meter.error else MeterState.NOT_NEW
        if self._last_fresh is not None:
            return held, self._last_fresh[0], self._last_fresh[2], True
        return held, currents, base_now, True

    def _same(self, a: Vec, b: Vec) -> bool:
        """N4: equal within half a meter resolution step on every phase."""
        tol = self.config.meter_resolution_a / 2 + 1e-9
        return all(abs(a[p] - b[p]) <= tol for p in range(PHASES))

    def _moved(self, own_a: Vec, own_b: Vec) -> bool:
        """N4: own currents moved by more than the meter could hide by rounding."""
        cfg = self.config
        need = cfg.meter_frozen_min_change_a + cfg.meter_resolution_a
        return any(abs(own_a[p] - own_b[p]) >= need for p in range(PHASES))

    def _frozen(self, currents: Vec, own: Vec, updated_at: float) -> bool:
        """ADDITION (a) - not in openWB, added for safety: the last ``meter_frozen_cycles``
        meter UPDATES (N4: an entry only when updated_at is newer than the last entry) are
        identical within the resolution while our own fresh measured currents on some phase
        moved by more than ``meter_frozen_min_change_a`` + resolution across them."""
        cfg = self.config
        n = cfg.meter_frozen_cycles
        if n == 0:
            return False
        if self._frozen_value is not None:
            # latched until a reading differs from the stuck value
            if self._same(currents, self._frozen_value):
                return True
            self._frozen_value = None
            self._history.clear()
            self._history_t = None
        if self._history_t is None or updated_at > self._history_t:
            self._history.append((currents, own))
            self._history_t = updated_at
        if len(self._history) < n:
            return False
        first = self._history[0][0]
        if not all(self._same(r, first) for r, _ in self._history):
            return False
        lo = _vec(min(o[p] for _, o in self._history) for p in range(PHASES))
        hi = _vec(max(o[p] for _, o in self._history) for p in range(PHASES))
        if self._moved(hi, lo):
            self._frozen_value = first
            return True
        return False

    def _grid_counter_from_base(self, currents: Vec, base: Vec) -> _Counter:
        """Spec §4.1 (openWB control/counter.py:184-247) and §4.2 (counter.py:265-296).
        ``base`` = meter − own fresh measured currents (from the same fresh cycle)."""
        cfg = self.config
        headroom = []
        for p in range(PHASES):
            h = cfg.fuse_limit_a[p] - base[p]
            over = currents[p] > cfg.fuse_limit_a[p]
            # spec §4.1 step 3: negative clipped to 0 (openWB counter.py:228-232).
            # ADDITION - not in openWB, added for safety: when the meter phase itself is above
            # the fuse the negative value is kept. Note: headroom = own measured − excess on
            # that phase, so the stations there are cut, in total, to what they draw now minus
            # the measured excess. In
            # this single-counter model a negative and a zero headroom both refuse every
            # minimum on that phase; keeping the sign makes the deficit visible to fixed
            # bookings and diagnostics instead of hiding it.
            if not (cfg.overload_unclipped and over):
                h = max(h, 0.0)
            headroom.append(h)
        power_left = None
        if cfg.max_total_power_w is not None:
            # spec §4.2: base power = meter − own measured; not clipped. Power from currents × V.
            power_left = cfg.max_total_power_w - sum(base) * cfg.phase_voltage_v
        return self._counter(headroom, power_left)

    def _grid_counter_fallback(self, action: MeterFailureAction | None) -> _Counter:
        cfg = self.config
        if action is MeterFailureAction.MINIMUM:
            # ADDITION (a) - not in openWB, added for safety: only minimums are given (additional
            # pass skipped). budget = fuse − base load of the last fresh meter cycle;
            # never seen a fresh value → min(fuse, errorcase / 230 / 3) per phase.
            # N6: a base load older than minimum_base_max_age_s is not trusted either.
            if (
                self._last_fresh is not None
                and self._last_fresh_t is not None
                and self._now - self._last_fresh_t <= cfg.minimum_base_max_age_s
            ):
                base = self._last_fresh[2]
                headroom = [cfg.fuse_limit_a[p] - base[p] for p in range(PHASES)]
                power = None
                if cfg.max_total_power_w is not None:
                    power = cfg.max_total_power_w - sum(base) * cfg.phase_voltage_v
            else:
                per_phase = cfg.max_power_errorcase_w / OPENWB_ERRORCASE_VOLTAGE_V / PHASES
                headroom = [min(per_phase, f) for f in cfg.fuse_limit_a]
                power = cfg.max_total_power_w
            return self._counter(headroom, power)
        if action is MeterFailureAction.STOP:
            return self._counter([0.0, 0.0, 0.0], 0.0 if cfg.max_total_power_w is not None else None)
        # spec §7.1 fallback (openWB control/counter.py:245-247, 288-293): errorcase/230/3 per
        # phase, power budget = errorcase; base load and own draw ignored.
        per_phase = cfg.max_power_errorcase_w / OPENWB_ERRORCASE_VOLTAGE_V / PHASES
        power = cfg.max_power_errorcase_w
        headroom = [per_phase] * PHASES
        if cfg.cap_errorcase_by_fuse:
            # ADDITION (a) - not in openWB, added for safety: never above the fuse
            # or the configured power limit.
            headroom = [min(per_phase, f) for f in cfg.fuse_limit_a]
            if cfg.max_total_power_w is not None:
                power = min(power, cfg.max_total_power_w)
        return self._counter(headroom, power)

    def _counter(self, headroom: list[float], power_left: float | None) -> _Counter:
        cfg = self.config
        return _Counter(
            headroom=list(headroom),
            power_left=power_left,
            voltage=cfg.phase_voltage_v,
            unbalanced_limit=cfg.unbalanced_load_limit_a if cfg.unbalanced_load else None,
        )

    # --- booking and limits ---------------------------------------------------------

    def _branch(self, grid: _Counter, ld: _Load) -> list[_Counter]:
        """Counters on the station's path. Single-counter scope: the grid only."""
        return [grid]

    def _book(self, grid: _Counter, ld: _Load, new: Sequence[float]) -> None:
        """Spec §4.3 (openWB control/algorithm/common.py:63-87, control/counter.py:304-313):
        book the change from what the load holds to ``new`` on every counter of its branch.
        We track what each load really holds, so passes cannot disagree about it."""
        diff = [new[p] - ld.booked[p] for p in range(PHASES)]
        for c in self._branch(grid, ld):
            for p in range(PHASES):
                c.headroom[p] -= diff[p]
            if c.power_left is not None:
                c.power_left -= sum(diff) * c.voltage
        ld.booked = list(new)

    def _booking_for(self, ld: _Load, value: float, now: float) -> Vec:
        """What allocating ``value`` costs: spec §4.3 measured substitution
        (openWB control/algorithm/common.py:188-205), then the ADDITION floor."""
        cfg = self.config
        booked = max(value, 0.0)
        s = ld.inp
        if value > 0 and cfg.book_measured_when_lower and ld.measured_grid is not None:
            started = (
                s.charging_since is not None and now - s.charging_since > cfg.less_charging_timeout_s
            ) or (s.charging_since is None and s.setpoint_a != 0)
            if cfg.substitute_against_setpoint:
                # DEVIATION from openWB - safety, proof: tests
                # test_review1_increase_is_booked_in_full_not_at_old_measured and
                # test_reviewN1_reading_lagging_a_raised_setpoint_is_not_an_under_drawer.
                # Under-draw = setpoint − nominal_difference ≥ mean AND the setpoint has not been
                # raised for > less_charging_timeout_s (charging_since is not used).
                if ld.under_drawer:
                    booked = min(value, ld.mean + max(value - s.setpoint_a, 0.0))
                if value >= ld.min_a - _EPS:
                    booked = max(booked, ld.min_a)
            elif started and max(value - cfg.nominal_difference_a, 0.0) >= ld.mean:
                booked = ld.mean  # spec §4.3 exactly (openWB)
        return _vec(max(booked if p in ld.used else 0.0, ld.floor[p]) for p in range(PHASES))

    def _limits(
        self, grid: _Counter, missing: Sequence[float], ld: _Load, credit: Sequence[float] = ZERO
    ) -> tuple[list[float], Reason | None]:
        """Spec §4.4 (openWB control/loadmanagement.py:18-53): current, then power, then
        unbalanced load. Each step only lowers what the previous one left.

        ``credit`` = what the load already holds (used in the minimum pass)."""
        reason: Reason | None = None
        head = [grid.headroom[p] + credit[p] for p in range(PHASES)]
        # step 2: current (loadmanagement.py:163-179)
        avail = [min(missing[p], head[p]) for p in range(PHASES)]
        if any(avail[p] < missing[p] - _EPS for p in range(PHASES)):
            reason = Reason.CURRENT_LIMIT
        if self.config.clamp_negative_available:
            # DEVIATION from openWB - safety, proof: test
            # test_review8_power_limit_not_inflated_by_negative_headroom_on_unused_phase.
            # A negative phase (overload elsewhere) must not shrink the power sum; on a used
            # phase 0 is still below the request, so the phase stays "limited".
            avail = [max(a, 0.0) for a in avail]
        # step 3: power (loadmanagement.py:123-160)
        if grid.power_left is not None:
            power_left = grid.power_left + sum(credit) * grid.voltage
            total = abs(sum(avail) * grid.voltage)  # |Σ| like openWB
            if power_left <= 0:
                avail, reason = [0.0, 0.0, 0.0], Reason.POWER_LIMIT
            elif total > power_left + _EPS:
                avail = [a * power_left / total for a in avail]
                reason = Reason.POWER_LIMIT
        # step 4: unbalanced load, 1- or 2-phase loads only (loadmanagement.py:103-120,
        # counter.py:251-263)
        if grid.unbalanced_limit is not None and len(ld.used) < PHASES:
            after = [head[p] - avail[p] for p in range(PHASES)]
            top = max(after)
            exceed = [max(top - after[p] - grid.unbalanced_limit, 0.0) for p in range(PHASES)]
            if any(exceed[p] > _EPS for p in ld.used):
                for p in ld.used:
                    avail[p] -= exceed[p]
                reason = Reason.UNBALANCED
        return avail, reason

    # --- passes -----------------------------------------------------------------------

    def _minimum_pass(self, grid: _Counter, group: list[_Load], now: float) -> None:
        """Spec §5.1 (openWB control/algorithm/min_current.py:20-59): in list order, each
        load gets its minimum if it fits, else 0. No pre-emption of earlier loads."""
        for ld in group:
            # the request is what the minimum really costs - min_current on its phases,
            # or its measured draw where that is higher (ADDITION (b) floor) - checked against the
            # headroom plus what the load already holds. Without a floor this is spec §5.1:
            # available_for_load (common.py:114-147) < min_current ⇔ some used phase is limited.
            request = list(self._booking_for(ld, ld.min_a, now))
            ld.headroom_seen = _vec(grid.headroom)
            avail, _ = self._limits(grid, request, ld, credit=ld.booked)
            if any(
                avail[p] < request[p] - _EPS
                for p in range(PHASES)
                if p in ld.used or request[p] > 0
            ):
                # refused: holds nothing but its floor
                ld.current, ld.refused, ld.reason = 0.0, True, Reason.MIN_NOT_FITTING
                self._book(grid, ld, self._booking_for(ld, 0.0, now))
            else:
                ld.current, ld.reason = ld.min_a, Reason.NONE
                self._book(grid, ld, self._booking_for(ld, ld.min_a, now))

    def _additional_pass(self, grid: _Counter, group: list[_Load], now: float) -> None:
        """Spec §5.2 (openWB control/algorithm/additional_current.py:20-48): sequential
        water-filling in list order; each load takes min(own need, equal share per phase of
        what is left); what it does not take stays for the loads after it."""
        remaining = []
        for ld in group:
            if ld.refused:
                continue
            if not ld.charging:
                # spec §5.2 step 1: car not charging keeps its minimum (filter_chargepoints.py:143-149)
                ld.reason = Reason.NOT_CHARGING if ld.required > ld.min_a + _EPS else Reason.NONE
                continue
            remaining.append(ld)
        while remaining:
            missing = [0.0, 0.0, 0.0]
            count = [0, 0, 0]
            for other in remaining:
                for p in other.used:
                    missing[p] += other.required - other.min_a
                    count[p] += 1
            ld = remaining.pop(0)
            ld.headroom_seen = _vec(grid.headroom)
            avail, why = self._limits(grid, missing, ld)
            limited = [p for p in ld.used if avail[p] < missing[p] - _EPS]
            need = ld.required - ld.current
            share = min(min(need, avail[p] / count[p]) for p in limited) if limited else need
            # an allocation pass never lowers what the minimum pass gave
            share = max(share, 0.0)
            ld.current += share
            ld.reason = (why or Reason.CURRENT_LIMIT) if ld.current < ld.required - _EPS else Reason.NONE
            self._book(grid, ld, self._booking_for(ld, ld.current, now))

    def _hold_pass(
        self,
        headroom_start: Vec,
        power_start: float | None,
        loads: list[_Load],
        ordered: list[_Load],
        now: float,
        no_increase: bool = False,
    ) -> tuple[Vec, float | None]:
        """DEVIATION from openWB - safety, proof: test
        test_review5_stop_request_while_still_drawing_keeps_its_current_this_cycle.

        Physical check of this cycle's result. Every fresh station is assumed to draw
        max(what it draws now, what it will be booked at) until a reading shows otherwise;
        reserved stations hold their reservation. ``pool`` = start headroom minus all of that.
        In priority order each station's increase over its present draw must fit the pool
        (current per phase and power); if not, its value is lowered to the largest that fits
        (0 if that is below its minimum). Decreases always pass. Returns the pool left.

        N6 note: this step only lowers values found by the passes (which applied the
        unbalanced-load limit) and does not re-check that limit. Lowering a 1-phase load on a
        lightly loaded phase can widen the difference between phases for one cycle."""
        cfg = self.config
        volt = cfg.phase_voltage_v
        held = {
            id(ld): self._held(ld, no_increase) for ld in loads
            if ld.measured_grid is not None or ld.reason is not Reason.OFFLINE
        }
        pool = list(headroom_start)
        for h in held.values():
            for p in range(PHASES):
                pool[p] -= h[p]
        power = None if power_start is None else power_start - volt * sum(sum(h) for h in held.values())

        def need(ld: _Load, x: float) -> list[float]:
            cost = self._booking_for(ld, x, now)
            h = held[id(ld)]
            return [max(cost[p] - h[p], 0.0) for p in range(PHASES)]

        def fits(ld: _Load, x: float) -> bool:
            n = need(ld, x)
            if all(v <= _EPS_HOLD for v in n):
                return True
            if no_increase:
                return False
            if any(n[p] > _EPS_HOLD and n[p] > pool[p] + _EPS_HOLD for p in range(PHASES)):
                return False
            return power is None or sum(n) * volt <= power + _EPS_HOLD * volt

        for ld in ordered:
            if ld.current > 0 and not fits(ld, ld.current):
                lo, hi = 0.0, ld.current
                for _ in range(60):
                    mid = (lo + hi) / 2
                    if fits(ld, mid):
                        lo = mid
                    else:
                        hi = mid
                ld.current = lo if lo >= ld.min_a - _EPS else 0.0
                ld.reason = Reason.METER_HOLD if no_increase else Reason.WAIT_DROP
            n = need(ld, ld.current)
            for p in range(PHASES):
                pool[p] -= n[p]
            if power is not None:
                power -= sum(n) * volt
        for ld in loads:
            if id(ld) in held and ld.measured_grid is not None:
                cost = self._booking_for(ld, ld.current, now)
                ld.booked = [max(cost[p], held[id(ld)][p]) for p in range(PHASES)]
        return _vec(pool), power

    def _held(self, ld: _Load, no_increase: bool = False) -> Vec:
        """What a station is assumed to draw until the next reading: its reservation if it
        is reserved; else its measured current per phase and - N1, with
        substitute_against_setpoint - at least its last setpoint on its phases unless it is a
        confirmed under-drawer (a raised setpoint is drawn as soon as the car follows).

        N3: without a new meter value ("no increase") every station is held at least at its
        last setpoint, under-drawers included: no increase = not above the last commanded
        value, so an idle or just-started car keeps it instead of being cut to 0. It is safe
        because that setpoint is then counted as drawn in the pool."""
        if ld.measured_grid is None:
            return ld.floor
        meas = [max(c, 0.0) for c in ld.measured_grid]
        sp = ld.inp.setpoint_a
        keep_setpoint = no_increase or (self.config.substitute_against_setpoint and not ld.under_drawer)
        if keep_setpoint and sp > 0:
            for p in ld.used:
                meas[p] = max(meas[p], sp)
        return _vec(meas)

    # --- result -----------------------------------------------------------------------

    def _final_reason(self, ld: _Load, failed: bool, overload: Vec) -> Reason:
        cfg = self.config
        short = ld.allowed is not None and ld.allowed < ld.required - _EPS
        if failed:
            if ld.refused:
                return Reason.MIN_NOT_FITTING
            return Reason.METER_FALLBACK if short else Reason.NONE
        if (
            cfg.overload_unclipped
            and short
            and any(overload[p] > 0 for p in ld.used)
            and ld.reason in (Reason.CURRENT_LIMIT, Reason.MIN_NOT_FITTING)
        ):
            return Reason.OVERLOAD  # ADDITION - not in openWB, added for safety
        return ld.reason


def _priority_groups(loads: list[_Load], drawing_first: bool = False) -> list[list[_Load]]:
    """Spec §2.6 (openWB control/algorithm/filter_chargepoints.py:73-94, algorithm.py:35-52):
    ascending priority; equal priority = one group, in input order. With ``drawing_first``
    (DEVIATION drawing_first_in_group) drawing stations come first inside a group, each part
    keeping input order."""
    groups: dict[int, list[_Load]] = {}
    for ld in loads:
        groups.setdefault(ld.inp.priority, []).append(ld)
    out = [groups[k] for k in sorted(groups)]
    if drawing_first:
        out = [[ld for ld in g if ld.charging] + [ld for ld in g if not ld.charging] for g in out]
    return out


def _validate(stations: Sequence[StationInput]) -> None:
    seen: set[str] = set()
    for s in stations:
        if s.station_id in seen:
            raise ValueError(f"duplicate station id {s.station_id!r}")
        seen.add(s.station_id)
        if s.phase_1 not in (1, 2, 3):
            raise ValueError(f"{s.station_id}: phase_1 must be 1, 2 or 3")
        if s.phases_in_use not in (1, 2, 3):
            raise ValueError(f"{s.station_id}: phases_in_use must be 1, 2 or 3")
        if s.hardware_max_a is not None and not _pos(s.hardware_max_a):
            raise ValueError(f"{s.station_id}: hardware_max_a must be positive and finite")
        if not _finite(s.min_current_a, s.max_current_a, s.requested_a):
            raise ValueError(f"{s.station_id}: min/max/requested current must be finite")
        if s.min_current_a <= 0 or s.max_current_a < s.min_current_a:
            raise ValueError(f"{s.station_id}: need 0 < min_current_a ≤ max_current_a")
        if _ceil01(s.min_current_a) > _floor01(s.max_current_a):
            raise ValueError(f"{s.station_id}: no 0.1 A value between min and max current")
        if s.measured_a is not None and len(s.measured_a) != PHASES:
            raise ValueError(f"{s.station_id}: measured_a must have three values")
