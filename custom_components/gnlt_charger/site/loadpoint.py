"""Loadpoint charging control (no Home Assistant imports, no sleeps).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Sources: evcc ``core/loadpoint.go``, ``core/loadpoint_api.go``,
``core/loadpoint_effective.go``, ``core/loadpoint_charger.go``,
``core/loadpoint_meter.go``, ``core/loadpoint/api.go``; the phase
bookkeeping of ``core/loadpoint_phases.go`` is the mixin
class:`.loadpoint_phases.LoadpointPhases`. Every method names its evcc
function; behaviour follows evcc line by line.

Removed with the excluded features (see the package docstring): vehicles
(SoC, limits, plans, min SoC, welcome charge, climater, wake-up), tariffs
(smart cost / feed-in limits), battery strategy (boost, battery support,
prioritySoc), optimizer suggestions, sessions/metrics, §14a dimming,
heating/integrated/continuous/switch devices, persistence. Where evcc reads
one of these, the port uses the value evcc has when the feature is not
configured (e.g. no vehicle: vehicle phases 0, no limit reached).

Differences of form (not behaviour): no mutexes (single caller);
``Update`` takes only ``site_power`` and ``battery_power``; the event bus is
replaced by direct handler calls in the same order; Go errors are raised
exceptions; ``publish`` is an optional callback.
"""

from __future__ import annotations

import logging
import math
import queue
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from . import api, grid
from .api import AlwaysCharge, ChargeMode, ChargeStatus
from .clock import ELAPSED, MINUTE, ZERO_TIME, Clock, SystemClock, is_zero, since
from .loadpoint_phases import UNKNOWN_PHASES, LoadpointPhases

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "PHASE_SWITCH_DURATION",
    "UNKNOWN_PHASES",
    "Loadpoint",
    "ThresholdConfig",
    "new_loadpoint",
    "new_loadpoint_from_config",
    "status_events",
]

# evcc core/loadpoint.go constants
EV_CHARGE_START = "start"
EV_CHARGE_STOP = "stop"
EV_VEHICLE_CONNECT = "connect"
EV_VEHICLE_DISCONNECT = "disconnect"

PV_TIMER = "pv"
PV_ENABLE = "enable"
PV_DISABLE = "disable"

PHASE_TIMER = "phase"
PHASE_SCALE_1P = "scale1p"
PHASE_SCALE_3P = "scale3p"

TIMER_INACTIVE = "inactive"

MIN_ACTIVE_CURRENT = 1.0  # A, minimum current at which a phase is treated as active
MIN_ACTIVE_VOLTAGE = 207  # V, minimum voltage at which a phase is treated as active

CHARGER_SWITCH_DURATION = 60.0  # s, allow out of sync during this timespan
PHASE_SWITCH_DURATION = 60.0  # s, allow out of sync and do not measure phases during this timespan

PHASE_SCALE_UP_BUFFER = 1.1  # min current headroom required before load management allows scaling up again


_VALID_STATUS = (ChargeStatus.A, ChargeStatus.B, ChargeStatus.C)


@dataclass
class ThresholdConfig:
    """evcc core/loadpoint/config.go: ThresholdConfig (W, s)."""

    threshold: float = 0.0
    delay: float = 0.0


def status_events(prev_status: ChargeStatus, status: ChargeStatus) -> list[str]:
    """evcc core/loadpoint.go: statusEvents.

    Converts the observed charger status change into a logical sequence of events.
    """
    res: list[str] = []
    # changed from A - connected
    if prev_status == ChargeStatus.A or (status != ChargeStatus.A and prev_status == ChargeStatus.NONE):
        res.append(EV_VEHICLE_CONNECT)
    # changed to C - start charging
    if status == ChargeStatus.C:
        res.append(EV_CHARGE_START)
    # changed from C - stop charging
    if prev_status == ChargeStatus.C:
        res.append(EV_CHARGE_STOP)
    # changed to A - disconnected
    if status == ChargeStatus.A:
        res.append(EV_VEHICLE_DISCONNECT)
    return res


def surplus_flexible(lp: Loadpoint) -> bool:
    """evcc core/loadpoint/api.go: SurplusFlexible (smart mode without always charge)."""
    return lp.get_mode() == ChargeMode.SMART and not lp.get_always_charge().active()


def always_charge_active(lp: Loadpoint) -> bool:
    """evcc core/loadpoint/api.go: AlwaysChargeActive."""
    return lp.get_mode() == ChargeMode.SMART and lp.get_always_charge().active()


class _FakeChargeMeter:
    """evcc core/wrapper.ChargeMeter: power simulated from the offered current."""

    def __init__(self) -> None:
        self.power = 0.0

    def set_power(self, power: float) -> None:
        self.power = power

    def current_power(self) -> float:
        return self.power


@dataclass(eq=False, kw_only=True)
class Loadpoint(LoadpointPhases):
    """evcc core/loadpoint.go: Loadpoint.

    Constructing ``Loadpoint(...)`` directly gives Go zero values for every
    field not passed, like evcc's struct literal;:func:`new_loadpoint` and
    func:`new_loadpoint_from_config` apply evcc's defaults.
    """

    clock: Clock = field(default_factory=SystemClock)
    charger: Any = None
    circuit: Any = None  # api.Circuit
    charge_meter: Any = None  # api.Meter; None = the charger (or a simulated meter)
    lp_chan: queue.Queue | None = None  # site update requests (evcc lpChan)
    publish_cb: Callable[[str, Any], None] | None = None

    # configuration
    title: str = ""
    priority: int = 0
    default_mode: ChargeMode = ChargeMode.EMPTY
    min_current: float = 0.0  # PV mode: start current, Min+PV mode: min current
    max_current: float = 0.0  # max allowed current, physically ensured by the charger
    phases_configured: int = 0  # charger configured phase mode 0/1/3
    solar_share: float = 0.0  # share of min charging power that must be solar in pv mode (0..1)
    enable: ThresholdConfig = field(default_factory=ThresholdConfig)
    disable: ThresholdConfig = field(default_factory=ThresholdConfig)

    # state
    mode: ChargeMode = ChargeMode.EMPTY
    always_charge: AlwaysCharge = AlwaysCharge.UNSET
    enabled: bool = False  # charger enabled state
    phases: int = 0  # charger enabled phases
    measured_phases: int = 0  # charger physically measured phases
    offered_current: float = 0.0  # charger current limit
    connected_time: float = ZERO_TIME  # vehicle connected timestamp
    connected_duration: float = 0.0  # from api.ConnectionTimer
    charger_switched: float = ZERO_TIME  # charger enabled/disabled timestamp
    phases_switched: float = ZERO_TIME  # phase switch timestamp
    status: ChargeStatus = ChargeStatus.NONE
    charge_power: float = 0.0
    charge_currents: list[float] | None = None
    pv_timer: float = ZERO_TIME  # PV enable/disable timer
    phase_timer: float = ZERO_TIME  # 1p3p switch timer

    _lp_queued: bool = field(default=False, init=False, repr=False)
    _fake_meter: _FakeChargeMeter | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Go copies ThresholdConfig structs by value: never share them between loadpoints
        self.enable = replace(self.enable)
        self.disable = replace(self.disable)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def publish(self, key: str, val: Any) -> None:
        """evcc core/loadpoint.go: publish (optional callback here)."""
        if self.publish_cb is not None:
            self.publish_cb(key, val)

    def _meter_source(self) -> Any:
        """The device behind the charge meter (evcc chargeMeter.source)."""
        return self.charge_meter if self.charge_meter is not None else self.charger

    def _charge_meter(self) -> Any:
        """evcc core/loadpoint_meter.go: newChargeMeter (simulated meter if the source has no api.Meter).

        A separate ``charge_meter`` is a meter by role; the charger counts as
        meter only if it declares:class:`api.Meter`.
        """
        if self.charge_meter is not None:
            return self.charge_meter
        if api.has_cap(self.charger, api.Meter):
            return self.charger
        if self._fake_meter is None:
            self._fake_meter = _FakeChargeMeter()
        return self._fake_meter

    def _bus_publish(self, event: str, *args: Any) -> None:
        """evcc event bus: the handlers subscribed in configureChargerType and Prepare, in order."""
        if event == EV_CHARGE_START:
            self.ev_charge_start_handler()
        elif event == EV_CHARGE_STOP:
            if self._fake_meter is not None:
                self._fake_meter.set_power(0)
            self.ev_charge_stop_handler()
        elif event == EV_VEHICLE_CONNECT:
            self.ev_vehicle_connect_handler()
        elif event == EV_VEHICLE_DISCONNECT:
            self.ev_vehicle_disconnect_handler()

    def _ev_charge_current(self, current: float) -> None:
        """evcc evChargeCurrent: evChargeCurrentWrappedMeterHandler + evChargeCurrentHandler."""
        if isinstance(self._charge_meter(), _FakeChargeMeter):
            power = grid.current_to_power(current, self.active_phases())
            # if disabled we cannot be charging
            if not self.enabled or not self.charging():
                power = 0.0
            self._charge_meter().set_power(power)
        self.publish("offeredCurrent", current if self.enabled else 0.0)

    # ------------------------------------------------------------------
    # update requests (evcc core/loadpoint.go)
    # ------------------------------------------------------------------

    def request_update(self) -> None:
        """evcc core/loadpoint.go: RequestUpdate / requestUpdate.

        Queues the loadpoint at most once so the site queue never overflows.
        """
        if self.lp_chan is None or self._lp_queued:
            return
        self._lp_queued = True
        try:
            self.lp_chan.put_nowait(self)
        except queue.Full:
            self._lp_queued = False

    def dequeued(self) -> Loadpoint:
        """evcc core/loadpoint.go: dequeued."""
        self._lp_queued = False
        return self

    # ------------------------------------------------------------------
    # event handlers (evcc core/loadpoint.go)
    # ------------------------------------------------------------------

    def ev_charge_start_handler(self) -> None:
        """evcc core/loadpoint.go: evChargeStartHandler (session/wake-up parts excluded)."""
        _LOGGER.info("start charging ->")

    def ev_charge_stop_handler(self) -> None:
        """evcc core/loadpoint.go: evChargeStopHandler (session parts excluded)."""
        _LOGGER.info("stop charging <-")
        # reset pv enable/disable timer (evcc #2289)
        if self.pv_timer != ELAPSED:
            self.reset_pv_timer()

    def ev_vehicle_connect_handler(self) -> None:
        """evcc core/loadpoint.go: evVehicleConnectHandler (vehicle/session parts excluded)."""
        _LOGGER.info("car connected")
        self.connected_time = self.clock.now()
        self.publish("connectedDuration", 0.0)

        # charger may have reconfigured phases internally while disconnected
        try:
            self.sync_charger_phases(not self.enabled)
        except Exception as err:  # noqa: BLE001 - evcc logs and continues
            _LOGGER.error("%s", err)

        # immediately allow pv mode activity
        self.elapse_pv_timer()

    def ev_vehicle_disconnect_handler(self) -> None:
        """evcc core/loadpoint.go: evVehicleDisconnectHandler (vehicle/session parts excluded)."""
        _LOGGER.info("car disconnected")

        # phases are unknown when vehicle disconnects
        self.reset_measured_phases()

        self.publish("connectedDuration", since(self.clock, self.connected_time))

        # set default mode on disconnect
        self.apply_default_mode()

        # always charge once ends with the session
        if self.get_always_charge() == AlwaysCharge.ONCE:
            self.set_always_charge(AlwaysCharge.OFF)

    def apply_default_mode(self) -> None:
        """evcc core/loadpoint.go: defaultMode (renamed: ``default_mode`` is the field)."""
        mode = self.default_mode
        if mode != ChargeMode.EMPTY and mode != self.get_mode():
            self.set_mode(mode)

    # ------------------------------------------------------------------
    # preparation (evcc core/loadpoint.go: Prepare)
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """evcc core/loadpoint.go: Prepare (publishing, settings and vehicles excluded)."""
        phases = self.get_charger_physical_phases()
        if phases != 0:
            if self.phases_configured != phases and self.phases_configured != 0:
                _LOGGER.warning(
                    "configured phases %d do not match physical phases %d", self.phases_configured, phases
                )
            self.phases = phases
            self.phases_configured = phases

        self.publish("phasesConfigured", self.phases_configured)
        self.publish("chargerPhases1p3p", self.has_phase_switching())
        self.publish("phasesActive", self.active_phases())
        self.publish_timer(PHASE_TIMER, 0, TIMER_INACTIVE)
        self.publish_timer(PV_TIMER, 0, TIMER_INACTIVE)

        # read initial charger state to prevent immediately disabling charger
        try:
            enabled = self.charger.enabled()
        except Exception as err:  # noqa: BLE001 - evcc logs and continues
            _LOGGER.error("charger enabled: %s", err)
            return
        self.enabled = enabled
        if enabled:
            # set defined current for use by pv mode
            try:
                self.set_limit(self.effective_min_current())
            except Exception:  # noqa: BLE001 - evcc ignores this error
                pass

    def set_and_publish_enabled(self, enabled: bool) -> None:
        """evcc core/loadpoint.go: setAndPublishEnabled."""
        if enabled != self.enabled:
            _LOGGER.debug("charger %s", "enable" if enabled else "disable")
            self.enabled = enabled
        self.publish("enabled", enabled)

    # ------------------------------------------------------------------
    # charger sync (evcc core/loadpoint.go: syncCharger)
    # ------------------------------------------------------------------

    def sync_charger(self) -> None:
        """evcc core/loadpoint.go: syncCharger.

        Updates charger status and synchronizes it with expectations.
        """
        enabled = self.charger.enabled()
        should_be_consistent = self.should_be_consistent()

        try:
            # #1: check charger logic, fix charger state if necessary (chargers that start charging while disabled)
            if not enabled and self.charging() and self.phase_switch_completed():
                _LOGGER.warning("charger logic error: disabled but charging")
                # treat as enabled when charging for further validations
                enabled = True
                if should_be_consistent:
                    # also enable charger to correct internal state
                    self.charger.enable(True)
                    # elapse PV timer so loadpoint can immediately switch charger if necessary
                    self.elapse_pv_timer()
                    return

            # #2: sync charger
            if enabled and self.enabled:
                self._sync_max_current(should_be_consistent)
                # sync phases
                if should_be_consistent:
                    self.sync_charger_phases(False)

            elif enabled == self.enabled:
                # sync disabled state, adopt phases so the next enable switches them
                # fixed phases would be switched back right away, see scalePhasesRequired
                if self.phases_configured == 0 and self.phase_switch_completed():
                    try:
                        self.sync_charger_phases(True)
                    except Exception as err:  # noqa: BLE001 - evcc logs and continues
                        _LOGGER.error("%s", err)

            elif not enabled and not self.phase_switch_completed():
                # some chargers disable themselves to be able to switch phases -> enable charger
                self.charger.enable(True)

            elif should_be_consistent and (enabled or self.connected()):
                # ignore disabled state if vehicle was disconnected (!lp.enabled && !lp.connected)
                _LOGGER.warning(
                    "charger out of sync: expected %sd, got %sd",
                    "enable" if self.enabled else "disable",
                    "enable" if enabled else "disable",
                )
        finally:
            if should_be_consistent:
                self.set_and_publish_enabled(enabled)

    def _sync_max_current(self, should_be_consistent: bool) -> None:
        """evcc core/loadpoint.go: syncCharger, case enabled && lp.enabled (max current part)."""
        not_available = False
        is_cg = api.has_cap(self.charger, api.CurrentGetter)

        # use chargers actual set current if available
        if is_cg:
            try:
                current = self.charger.get_max_current()
            except api.NotAvailableError:
                not_available = True
            except Exception as err:
                if not api.acceptable_error(err):
                    raise RuntimeError(f"charger get max current: {err}") from err
            else:
                # smallest adjustment most PWM-Controllers can do is: 100%÷256×0,6A = 0.234A
                delta = abs(self.offered_current - current)
                if delta > 0.23:
                    if should_be_consistent and delta >= 1:
                        _LOGGER.warning(
                            "charger logic error: current mismatch (got %.3gA, expected %.3gA)",
                            current,
                            self.offered_current,
                        )
                    self.offered_current = current
                    self._ev_charge_current(self.offered_current)

        # use measured phase currents as fallback if charger does not provide max current
        if not is_cg or not_available:
            # validate if current too high by more than 1A (evcc #14731)
            current = self.get_max_phase_current()
            if current > self.offered_current + 1.0:
                if should_be_consistent:
                    _LOGGER.warning(
                        "charger logic error: current mismatch (got %.3gA measured, expected %.3gA)",
                        current,
                        self.offered_current,
                    )
                self.offered_current = current
                self._ev_charge_current(self.offered_current)

    # ------------------------------------------------------------------
    # current control
    # ------------------------------------------------------------------

    def coarse_current(self) -> bool:
        """evcc core/loadpoint.go: coarseCurrent (charger requires full amp steps).

        evcc: ``!api.HasCap[api.ChargerEx](charger)`` (vehicle feature excluded).
        """
        return not api.has_cap(self.charger, api.ChargerEx)

    def rounded_current(self, current: float) -> float:
        """evcc core/loadpoint.go: roundedCurrent."""
        if self.coarse_current():
            current = float(math.trunc(current))
        return current

    def actual_max_charge_current(self) -> float:
        """evcc core/loadpoint.go: actualMaxChargeCurrent."""
        if self.charge_currents is not None:
            return max(self.charge_currents[0], self.charge_currents[1], self.charge_currents[2])
        if self.charging():
            return self.offered_current
        return 0.0

    def set_limit(self, current: float) -> None:
        """evcc core/loadpoint.go: setLimit.

        Applies charger current limits and enables/disables accordingly:
        current first, then enable/disable.
        """
        current = self.rounded_current(current)

        # apply circuit limits
        if self.circuit is not None:
            current_limit = self.circuit.validate_current(self.actual_max_charge_current(), current)

            active_phases = self.active_phases()
            requested_power = grid.current_to_power(current, active_phases)
            power_limit = self.circuit.validate_power(self.charge_power, requested_power)

            # avoid float round trip through power if the circuit does not cap
            limited = current_limit
            if power_limit < requested_power:
                limited = min(limited, grid.power_to_current(power_limit, active_phases))
            limited = self.rounded_current(limited)

            min_current = self.effective_min_current()
            if limited < min_current and current >= min_current:
                # a charging loadpoint measures below its nominal request, so on a full circuit the gap
                # alone would cap it below min current; keep min current unless the circuit is over limit
                min_power = grid.current_to_power(min_current, active_phases)
                if (
                    self.charging()
                    and self.offered_current >= min_current
                    and self.circuit.validate_current(min_current, min_current) >= min_current
                    and self.circuit.validate_power(min_power, min_power) >= min_power
                ):
                    limited = self.rounded_current(min_current)
                else:
                    _LOGGER.debug("circuit limit %.3gA below min current %.3gA", limited, min_current)
            current = limited

        # evcc #16309
        eff_min_current = self.effective_min_current()
        eff_max_current = self.effective_max_current()
        if eff_min_current > eff_max_current:
            raise ValueError(
                f"invalid config: min current {eff_min_current:.3g}A exceeds max current {eff_max_current:.3g}A"
            )

        # set current
        if current != self.offered_current and current >= eff_min_current:
            if api.has_cap(self.charger, api.ChargerEx):
                self.charger.max_current_millis(current)
            else:
                self.charger.max_current(int(current))
            _LOGGER.debug("set charge current limit: %.3gA", current)
            self.offered_current = current
            self._ev_charge_current(current)

        # set enabled/disabled
        enabled = current >= eff_min_current
        if enabled != self.enabled:
            self.charger.enable(enabled)

            self.set_and_publish_enabled(enabled)
            self.charger_switched = self.clock.now()

            # ensure we always re-set current when enabling charger
            if not enabled:
                self.offered_current = 0.0

            self._ev_charge_current(current)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def connected(self) -> bool:
        """evcc core/loadpoint.go: connected."""
        status = self.get_status()
        return status in (ChargeStatus.B, ChargeStatus.C)

    def charging(self) -> bool:
        """evcc core/loadpoint.go: charging."""
        return self.get_status() == ChargeStatus.C

    def pv_charge_starting(self) -> bool:
        """evcc core/loadpoint.go: PvChargeStarting.

        A PV loadpoint that claimed surplus via a running enable timer but is
        not yet drawing it (evcc #31194, #31684).
        """
        enabled = self.enabled
        # an elapsed timer means a delay was skipped, not an enable pending
        pv_timer_running = not is_zero(self.pv_timer) and self.pv_timer != ELAPSED

        if self.get_mode() != ChargeMode.SMART or not self.connected() or self.charge_goal_reached(enabled):
            return False

        # enable timer running (not yet enabled)
        return not enabled and pv_timer_running

    def charge_goal_reached(self, enabled: bool) -> bool:
        """evcc core/loadpoint.go: chargeGoalReached (SoC and energy limits excluded)."""
        # enabled but drawing nothing: it won't ramp up
        return enabled and not self.charging()

    def get_status_changes(self) -> list[ChargeStatus]:
        """evcc core/loadpoint.go: getStatusChanges.

        Checks charger status and returns a chronological list of status changes.
        """
        res: list[ChargeStatus] = []
        status = self.charger.status()
        # contract: A, B or C only. evcc drivers return an error for E/F
        # (api.ChargeStatusString) or map E to A; an error skips the cycle.
        if status not in _VALID_STATUS:
            raise ValueError(f"charger status: invalid status {status!r}")
        _LOGGER.debug("charger status: %s", status)

        prev_status = self.get_status()

        # ignore charge interruption while switching phases. Status is left unchanged,
        # hence a real interruption is detected once the timespan has elapsed.
        ignore = status == ChargeStatus.B and prev_status == ChargeStatus.C and not self.phase_switch_completed()

        if ignore:
            _LOGGER.debug("ignoring charge interruption during phase switch")
        elif status != prev_status:
            res = [status]

        # check charger connection duration
        if api.has_cap(self.charger, api.ConnectionTimer):
            duration = self.charger.connection_duration()
            # connection duration dropped without disconnect status, indicates intermediate disconnect
            if (
                not ignore
                and status != ChargeStatus.A
                and prev_status != ChargeStatus.A
                and duration < self.connected_duration
            ):
                _LOGGER.debug("connection duration drop detected (%s -> %s)", self.connected_duration, duration)
                res = [ChargeStatus.A, status]
            self.connected_duration = duration

        return res

    def update_charger_status(self) -> None:
        """evcc core/loadpoint.go: updateChargerStatus (push notifications excluded)."""
        status_changes = self.get_status_changes()
        if not status_changes:
            return

        for status in status_changes:
            prev_status = self.get_status()
            self.status = status
            for event in status_events(prev_status, status):
                self._bus_publish(event)

        # update whenever there is a state change
        self._ev_charge_current(self.offered_current)

    def effective_current(self) -> float:
        """evcc core/loadpoint.go: effectiveCurrent (the currently effective charging current)."""
        if not self.charging():
            return 0.0
        # adjust actual current for vehicles like Zoe where it remains below target
        if self.charge_currents is not None:
            cur = max(self.charge_currents[0], self.charge_currents[1], self.charge_currents[2])
            return min(cur + 2.0, self.offered_current)
        return self.offered_current

    # ------------------------------------------------------------------
    # timers
    # ------------------------------------------------------------------

    def elapse_pv_timer(self) -> None:
        """evcc core/loadpoint.go: elapsePVTimer."""
        if self.pv_timer == ELAPSED:
            return
        _LOGGER.debug("pv timer elapse")
        self.pv_timer = ELAPSED
        self.publish_timer(PV_TIMER, 0, TIMER_INACTIVE)

    def reset_pv_timer(self, typ: str | None = None) -> None:
        """evcc core/loadpoint.go: resetPVTimer."""
        if is_zero(self.pv_timer):
            return
        _LOGGER.debug("pv %stimer reset", f"{typ} " if typ else "")
        self.pv_timer = ZERO_TIME
        self.publish_timer(PV_TIMER, 0, TIMER_INACTIVE)

    def reset_phase_timer(self) -> None:
        """evcc core/loadpoint.go: resetPhaseTimer."""
        if is_zero(self.phase_timer):
            return
        self.phase_timer = ZERO_TIME
        self.publish_timer(PHASE_TIMER, 0, TIMER_INACTIVE)

    def phase_timer_elapsed(self, delay: float, action: str) -> bool:
        """evcc core/loadpoint.go: phaseTimerElapsed.

        Starts or continues the phase switch timer and returns True once delay
        has elapsed. Scaling is immediate while disabled.
        """
        if not self.enabled:  # scale immediately before enabling
            self.phase_timer = ELAPSED

        if is_zero(self.phase_timer):
            _LOGGER.debug("start phase %s timer", action)
            self.phase_timer = self.clock.now()

        self.publish_timer(PHASE_TIMER, delay, action)
        return since(self.clock, self.phase_timer) >= delay

    def publish_timer(self, name: str, delay: float, action: str) -> None:
        """evcc core/loadpoint.go: publishTimer."""
        timer = self.phase_timer if name == PHASE_TIMER else self.pv_timer
        remaining = max(delay - since(self.clock, timer), 0.0)
        self.publish(name + "Action", action)
        self.publish(name + "Remaining", remaining)

    # ------------------------------------------------------------------
    # phase switching (evcc core/loadpoint.go)
    # ------------------------------------------------------------------

    def scale_phases_required(self) -> bool:
        """evcc core/loadpoint.go: scalePhasesRequired (fixed phases differ from enabled phases)."""
        return (
            self.has_phase_switching() and self.phases_configured != 0 and self.phases_configured != self.get_phases()
        )

    def scale_phases_if_available(self, phases: int) -> None:
        """evcc core/loadpoint.go: scalePhasesIfAvailable."""
        if self.phases_configured != 0:
            phases = self.phases_configured
        if self.has_phase_switching():
            self.scale_phases(phases)

    def scale_phases(self, phases: int) -> None:
        """evcc core/loadpoint.go: scalePhases.

        Adjusts the number of active phases. While enabled, drops to min current
        before scaling up so the 1p current is not applied to all phases.
        Raises:class:`api.NotAvailableError` if the charger cannot switch now.
        """
        if not api.has_cap(self.charger, api.PhaseSwitcher):
            raise RuntimeError("charger does not implement api.PhaseSwitcher")

        if self.get_phases() != phases:
            # drop to min current before scaling up so the 1p current is not applied to all phases
            if self.enabled and phases > 1:
                self.set_limit(self.effective_min_current())

            # switch phases
            self.charger.phases1p3p(phases)
            _LOGGER.debug("switched phases: %dp", phases)

            # prevent premature measurement of active phases
            self.phases_switched = self.clock.now()

            # update setting and reset timer
            self.set_phases(phases)

    def circuit_allows_phases(self, phases: int, min_current: float) -> bool:
        """evcc core/loadpoint.go: circuitAllowsPhases (circuit power limit allows min current on phases)."""
        if self.circuit is None:
            return True
        min_power = grid.current_to_power(min_current, phases)
        power_limit = self.circuit.validate_power(self.charge_power, min_power)
        if power_limit < min_power:
            _LOGGER.debug("available circuit power %.0fW < %.0fW min %dp power", power_limit, min_power, phases)
            return False
        return True

    def fast_charging(self) -> None:
        """evcc core/loadpoint.go: fastCharging (scale to 3p if available, set max current)."""
        try:
            waiting = self.fast_charging_phases()
        except Exception:
            self.reset_phase_timer()
            raise
        if not waiting:
            self.reset_phase_timer()
        self.set_limit(self.effective_max_current())

    def fast_charging_phases(self) -> bool:
        """evcc core/loadpoint.go: fastChargingPhases.

        Scales to 3p if load management allows; returns True while waiting for
        the scale-up delay.
        """
        if not self.has_phase_switching() or not self.phase_switch_completed():
            return False

        if self.circuit is None:
            # ignore api.ErrNotAvailable: continue with the current phase configuration
            try:
                self.scale_phases_if_available(3)
            except api.NotAvailableError:
                pass
            return False

        target_phases = 3
        if not self.circuit_allows_phases(3, self.effective_min_current()):
            target_phases = 1

        if self.phases_configured != 0:
            # user configured fixed phase count overrides automatic switching
            if self.phases_configured == 3 and target_phases == 1:
                _LOGGER.debug("configured fixed phase count %dp prevents switching to 1p", self.phases_configured)
            target_phases = self.phases_configured

        # use enabled phases - scalePhases compares against the same value
        phases = self.get_phases()

        # unknown phases: derive from measured phases while charging, more than 1p measured implies 3p
        if phases == 0:
            measured = self.get_measured_phases()
            if measured == 1:
                phases = 1
            elif measured > 1:
                phases = 3
            else:
                try:
                    self.scale_phases(target_phases)
                except api.NotAvailableError:
                    pass
                return False

        # scale down: immediate
        if target_phases == 1 and phases == 3:
            try:
                self.scale_phases(1)
            except api.NotAvailableError:
                pass
            return False

        # scale up: delayed and buffered against immediately undoing a scale down.
        # a fixed phase configuration is not subject to load management, hence no buffer.
        if (
            target_phases == 3
            and phases == 1
            and (
                self.phases_configured == 3
                or self.circuit_allows_phases(3, PHASE_SCALE_UP_BUFFER * self.effective_min_current())
            )
        ):
            if not self.phase_timer_elapsed(self.get_enable_delay(), PHASE_SCALE_3P):
                return True
            try:
                self.scale_phases(3)
            except api.NotAvailableError:
                pass

        return False

    def min_charging(self) -> None:
        """evcc core/loadpoint.go: minCharging (scale to 1p if available, set min current)."""
        if self.has_phase_switching():
            try:
                self.scale_phases_if_available(1)
            except api.NotAvailableError:
                pass
        self.set_limit(self.effective_min_current())

    def pv_scale_phases(self, site_power: float, min_current: float, max_current: float, may_disable: bool) -> int:
        """evcc core/loadpoint.go: pvScalePhases.

        Switches phases if necessary and returns the number of phases switched
        to (0 = no switch). ``may_disable``: insufficient surplus can stop
        charging via the pv disable timer.
        """
        phases = self.get_phases()

        # observed phase state inconsistency (evcc #1572, #2230, #2613)
        measured_phases = self.get_measured_phases()
        if 0 < phases < measured_phases:
            if self.charger_update_completed() and self.phase_switch_completed():
                _LOGGER.warning("ignoring inconsistent phases: %dp < %dp observed active", phases, measured_phases)
            self.reset_measured_phases()

        # a disabled charger switches right before it is enabled, see pvEnablePhases
        if not self.enabled:
            if not is_zero(self.phase_timer):
                self.reset_phase_timer()
            return 0

        waiting = False
        disable = False
        active_phases = self.active_phases()
        available_power = self.charge_power - site_power
        scalable = active_phases > 1 and self.phases_configured < 3

        if scalable:
            insufficient = site_power > 0 and grid.power_to_current(available_power, active_phases) < min_current
            if insufficient:
                _LOGGER.debug(
                    "available power %.0fW < %.0fW min %dp threshold",
                    available_power,
                    grid.current_to_power(min_current, active_phases),
                    active_phases,
                )

            # scaling down only helps if 1p is sustainable, otherwise the loadpoint
            # disables once the phase timer elapses (evcc #33208). Climater keep-alive excluded.
            useful = not may_disable or grid.power_to_current(available_power, 1) >= min_current
            disable = insufficient and not useful
            if disable:
                _LOGGER.debug(
                    "available power %.0fW < %.0fW min 1p threshold, disabling instead of scaling down",
                    available_power,
                    grid.current_to_power(min_current, 1),
                )

            # scaling down also frees load management headroom for min power on activePhases
            scalable = insufficient or not self.circuit_allows_phases(active_phases, min_current)

        # scale down phases. The phase timer keeps running while 3p min is not covered,
        # whether 1p would be sustainable or not (evcc #34746)
        if scalable:
            if self.phase_timer_elapsed(self.get_disable_delay(), PHASE_SCALE_1P):
                if disable:
                    self.elapse_pv_timer()
                    return 0
                try:
                    self.scale_phases(1)
                except Exception as err:  # noqa: BLE001 - evcc logs; switch did not complete
                    if not isinstance(err, api.NotAvailableError):
                        _LOGGER.error("%s", err)
                    return phases
                return 1
            waiting = True

        # load management may cap the 1p current far below the theoretical maximum
        if self.circuit is not None:
            max_current = self.circuit.validate_current(self.actual_max_charge_current(), max_current)

        max_phases = self.max_active_phases()
        target_1p_current = grid.power_to_current(available_power, 1)

        # scaling up is pointless unless load management allows min current and power on maxPhases
        scalable = (
            max_phases > 1
            and phases < max_phases
            and target_1p_current > max_current
            and max_current >= min_current
            and self.circuit_allows_phases(max_phases, min_current)
        )

        # scale up phases
        target_current = grid.power_to_current(available_power, max_phases)
        if target_current >= min_current and scalable:
            _LOGGER.debug(
                "available power %.0fW > %.0fW min %dp threshold",
                available_power,
                grid.current_to_power(min_current, max_phases),
                max_phases,
            )
            if self.phase_timer_elapsed(self.get_enable_delay(), PHASE_SCALE_3P):
                try:
                    self.scale_phases(3)
                except Exception as err:  # noqa: BLE001 - evcc logs; switch did not complete
                    if not isinstance(err, api.NotAvailableError):
                        _LOGGER.error("%s", err)
                    return phases
                return 3
            waiting = True

        # reset timer to disabled state
        if not waiting and not is_zero(self.phase_timer):
            self.reset_phase_timer()

        return 0

    def project_phase_switch(self, site_power: float, min_current: float) -> tuple[float, int]:
        """evcc core/loadpoint.go: projectPhaseSwitch.

        Site power and phases after a pending scale down to 1p.
        """
        # a disabled charger starts on 1p unless surplus covers more, see pvEnablePhases
        if not self.enabled and self.min_active_phases() == 1:
            return site_power, 1

        phases = self.active_phases()
        if self.has_phase_switching() and not is_zero(self.phase_timer) and phases > 1:
            # site power loses the current draw, at least min power on all phases, and gains 1p min power
            site_power -= max(self.charge_power, grid.current_to_power(min_current, phases)) - grid.current_to_power(
                min_current, 1
            )
            phases = 1
        return site_power, phases

    # ------------------------------------------------------------------
    # PV thresholds (evcc core/loadpoint.go)
    # ------------------------------------------------------------------

    def custom_thresholds(self) -> bool:
        """evcc core/loadpoint.go: customThresholds."""
        return self.enable.threshold != 0 or self.disable.threshold != 0

    def effective_solar_share(self, battery_power: float) -> float:
        """evcc core/loadpoint.go: effectiveSolarShare (optimizer and prioritySoc parts excluded)."""
        return self.get_solar_share()

    def pv_disable_threshold(self, min_current: float, battery_power: float, phases: int) -> float:
        """evcc core/loadpoint.go: pvDisableThreshold."""
        if self.custom_thresholds():
            return self.disable.threshold
        # allow grid import for the non-solar part of the min power
        return (1 - self.effective_solar_share(battery_power)) * grid.current_to_power(min_current, phases)

    def pv_enable_decision(
        self, effective_current: float, min_current: float, site_power: float, battery_power: float, phases: int
    ) -> tuple[float, bool]:
        """evcc core/loadpoint.go: pvEnableDecision."""
        threshold = self.enable.threshold
        if not self.custom_thresholds():
            # require the solar share of the min current to come from surplus
            threshold = grid.current_to_power(
                effective_current - self.effective_solar_share(battery_power) * min_current, phases
            )
        elif threshold == 0:
            threshold = grid.current_to_power(effective_current - min_current, phases)
        return threshold, site_power <= threshold

    def pv_max_current(self, site_power: float, battery_power: float = 0.0) -> float:
        """evcc core/loadpoint.go: pvMaxCurrent (battery boost/support excluded).

        Maximum target current for smart mode from the site power balance
        (negative = export). Runs the enable/disable timers and the 1p/3p
        switching.
        """
        # read only once to simplify testing
        min_current = self.effective_min_current()
        max_current = self.effective_max_current()
        always_charge = self.get_always_charge().active()

        # battery boost / battery support excluded: evcc's ``battery`` is false
        battery = False
        may_disable = not always_charge and not battery

        # switch phases up/down
        scaled_to = 0
        if self.has_phase_switching() and self.phase_switch_completed():
            scaled_to = self.pv_scale_phases(site_power, min_current, max_current, may_disable)

        # a disabled charger decides its start on the phases it switches to when enabled
        enable_phases = self.pv_enable_phases(site_power, battery_power, min_current, max_current)

        # calculate target charge current from delta power and actual current
        active_phases = self.active_phases()
        if enable_phases > 0:
            active_phases = min(enable_phases, self.max_active_phases())
        effective_current = self.effective_current()
        if scaled_to == 3:
            # if we did scale, spread the power measured before the switch over the new phase count
            effective_current = grid.power_to_current(self.charge_power, self.max_active_phases())
        delta_current = grid.power_to_current(-site_power, active_phases)
        available_current = effective_current + delta_current
        target_current = max(available_current, 0.0)

        # with always charge return at least minCurrent
        if (always_charge or battery) and target_current < min_current:
            _LOGGER.debug(
                "pv charge current: min %.3gA > %.3gA (%.0fW @ %dp)",
                min_current,
                target_current,
                site_power,
                active_phases,
            )
            if not self.pv_enable_scale_phases(enable_phases) and not always_charge:
                return 0.0
            return min_current

        _LOGGER.debug(
            "pv charge current: %.3gA = %.3gA + %.3gA (%.0fW @ %dp)",
            target_current,
            effective_current,
            delta_current,
            site_power,
            active_phases,
        )

        if not always_charge and self.enabled and target_current < min_current:
            projected_site_power, projected_phases = self.project_phase_switch(site_power, min_current)
            disable_threshold = self.pv_disable_threshold(min_current, battery_power, projected_phases)

            # kick off disable sequence
            if projected_site_power >= disable_threshold:
                _LOGGER.debug(
                    "projected site power %.0fW >= %.0fW disable threshold", projected_site_power, disable_threshold
                )
                if is_zero(self.pv_timer):
                    _LOGGER.debug("pv disable timer start: %ss", self.get_disable_delay())
                    self.pv_timer = self.clock.now()

                self.publish_timer(PV_TIMER, self.get_disable_delay(), PV_DISABLE)

                elapsed = since(self.clock, self.pv_timer)
                if elapsed >= self.get_disable_delay():
                    _LOGGER.debug("pv disable timer elapsed")
                    # reset timer to prevent immediate charger re-enabling
                    self.reset_pv_timer()
                    return 0.0
            else:
                # reset timer
                self.reset_pv_timer("disable")

            return min_current

        if not always_charge and not self.enabled:
            enable_threshold, should_enable = self.pv_enable_decision(
                effective_current, min_current, site_power, battery_power, active_phases
            )

            # kick off enable sequence
            if should_enable:
                _LOGGER.debug("site power %.0fW <= %.0fW enable threshold", site_power, enable_threshold)
                if is_zero(self.pv_timer):
                    _LOGGER.debug("pv enable timer start: %ss", self.get_enable_delay())
                    self.pv_timer = self.clock.now()

                self.publish_timer(PV_TIMER, self.get_enable_delay(), PV_ENABLE)

                elapsed = since(self.clock, self.pv_timer)
                if elapsed >= self.get_enable_delay():
                    # enabling on the wrong phases would draw more than the surplus allows, retry next cycle
                    if not self.pv_enable_scale_phases(enable_phases):
                        return 0.0
                    _LOGGER.debug("pv enable timer elapsed")
                    # reset timer to prevent immediate charger re-disabling
                    self.reset_pv_timer()
                    return min_current
            else:
                # reset timer
                self.reset_pv_timer("enable")

            return 0.0

        # reset timer to disabled state
        self.reset_pv_timer()

        # always charge enables a disabled charger at min current, the next cycle ramps up on the new phases
        if enable_phases > 0:
            self.pv_enable_scale_phases(enable_phases)
            return min_current

        # cap at maximum current
        return min(target_current, max_current)

    def pv_enable_phases(self, site_power: float, battery_power: float, min_current: float, max_current: float) -> int:
        """evcc core/loadpoint.go: pvEnablePhases.

        The phases a disabled charger switches to before it is enabled, or 0 to
        keep its phases.
        """
        if self.enabled or not self.has_phase_switching() or self.phases_configured != 0:
            return 0

        max_phases = self.max_active_phases()
        if max_phases <= 1:
            return 0

        # load management may cap the 1p current far below the theoretical maximum
        if self.circuit is not None:
            max_current = self.circuit.validate_current(self.actual_max_charge_current(), max_current)

        available_power = self.charge_power - site_power
        _, enable = self.pv_enable_decision(0, min_current, site_power, battery_power, max_phases)
        start_max = (
            enable
            and grid.power_to_current(available_power, max_phases) >= min_current
            and max_current >= min_current
            and self.circuit_allows_phases(max_phases, min_current)
        )

        phases = self.get_phases()
        if phases == 1 and start_max and grid.power_to_current(available_power, 1) > max_current:
            return 3
        if phases != 1 and not start_max:
            return 1
        return 0

    def pv_enable_scale_phases(self, phases: int) -> bool:
        """evcc core/loadpoint.go: pvEnableScalePhases.

        Switches a disabled charger to the phases it is enabled on; True on success.
        """
        if phases == 0:
            return True

        # the previous switch has not settled yet
        if not self.phase_switch_completed():
            return False

        try:
            self.scale_phases(phases)
        except Exception as err:  # noqa: BLE001 - evcc logs; switch failed
            if not isinstance(err, api.NotAvailableError):
                _LOGGER.error("%s", err)
            return False
        return True

    # ------------------------------------------------------------------
    # measurements (evcc core/loadpoint.go)
    # ------------------------------------------------------------------

    def update_charge_power_and_currents(self) -> float:
        """evcc core/loadpoint.go: UpdateChargePowerAndCurrents (no retry/backoff).

        Reads charge power and phase currents for load management.
        """
        meter = self._charge_meter()
        try:
            power = meter.current_power()
        except Exception as err:  # noqa: BLE001 - evcc logs and continues with 0
            power = 0.0
            _LOGGER.error("charge power: %s", err)
        else:
            self.charge_power = power
            _LOGGER.debug("charge power: %.0fW", power)
            self.publish("chargePower", power)
            # evcc #2153, #6986, #13378
            if power < -100 and self.should_be_consistent():
                _LOGGER.warning("charge power must not be negative: %.0f", power)

        # update charge currents
        self.charge_currents = None
        source = self._meter_source()
        if api.has_cap(source, api.PhaseCurrents):
            try:
                i1, i2, i3 = source.currents()
            except api.NotAvailableError:
                pass
            except Exception as err:  # noqa: BLE001 - evcc logs and continues
                _LOGGER.error("charge currents: %s", err)
            else:
                self.charge_currents = [i1, i2, i3]
                _LOGGER.debug("charge currents: %s", self.charge_currents)
                self.publish("chargeCurrents", self.charge_currents)

        return power

    def phases_from_charge_currents(self) -> None:
        """evcc core/loadpoint.go: phasesFromChargeCurrents (count phases with current > 1 A)."""
        if self.charge_currents is None:
            return

        if self.charging() and self.phase_switch_completed():
            phases = sum(1 for i in self.charge_currents if i > MIN_ACTIVE_CURRENT)

            # single phase loads in IT grids draw current on two line conductors
            if grid.no_neutral() and phases == 2:
                phases = 1

            if phases >= 1:
                self.measured_phases = phases
                _LOGGER.debug("detected active phases: %dp", phases)
                self.publish("phasesActive", phases)

    def update_charge_voltages(self) -> None:
        """evcc core/loadpoint.go: updateChargeVoltages (count phases with nominal grid voltage)."""
        source = self._meter_source()
        if not api.has_cap(source, api.PhaseVoltages):
            return  # don't guess

        try:
            u1, u2, u3 = source.voltages()
        except api.NotAvailableError:
            return
        except Exception as err:  # noqa: BLE001 - evcc logs and continues
            _LOGGER.error("charge voltages: %s", err)
            return

        self.publish("chargeVoltages", [u1, u2, u3])

        if self.has_phase_switching():
            return  # we don't need the voltages, but publish

        # without neutral, phase voltages don't reveal the connected phases
        if grid.no_neutral():
            return

        a1, a2, a3 = u1 >= MIN_ACTIVE_VOLTAGE, u2 >= MIN_ACTIVE_VOLTAGE, u3 >= MIN_ACTIVE_VOLTAGE

        if (not a1 and a3) or (a2 and not a3) or (not a2 and a3):
            _LOGGER.warning("invalid phase wiring between charge meter and charger")

        phases = 0
        if a1 or a2 or a3:
            phases = 3
        if a1 and not a2 and not a3:
            phases = 1

        if phases >= 1:
            _LOGGER.debug("detected connected phases: %dp", phases)
            self.set_phases(phases)

    def should_be_consistent(self) -> bool:
        """evcc core/loadpoint.go: shouldBeConsistent."""
        return self.charger_update_completed() and self.phase_switch_completed()

    def charger_update_completed(self) -> bool:
        """evcc core/loadpoint.go: chargerUpdateCompleted (60 s after enable/disable)."""
        return since(self.clock, self.charger_switched) > CHARGER_SWITCH_DURATION

    def phase_switch_completed(self) -> bool:
        """evcc core/loadpoint.go: phaseSwitchCompleted (60 s after a phase switch)."""
        return since(self.clock, self.phases_switched) > PHASE_SWITCH_DURATION

    # ------------------------------------------------------------------
    # main control (evcc core/loadpoint.go: Update)
    # ------------------------------------------------------------------

    def update(self, site_power: float, battery_power: float = 0.0) -> None:
        """evcc core/loadpoint.go: Update, the main control function.

        Charge power and currents must have been read before
        (:meth:`update_charge_power_and_currents`, done by the site).
        No exception escapes: like an evcc charger error, it is logged and
        the rest of this cycle is skipped.
        """
        try:
            self._update(site_power, battery_power)
        except Exception:  # noqa: BLE001 - evcc logs and skips the cycle
            _LOGGER.exception("loadpoint %s: update failed, cycle skipped", self.title)

    def _update(self, site_power: float, battery_power: float) -> None:
        """evcc core/loadpoint.go: Update (body)."""
        # read and publish meters first
        self.update_charge_voltages()
        self.phases_from_charge_currents()

        self._ev_charge_current(self.offered_current)

        # read and publish status
        try:
            self.update_charger_status()
        except Exception as err:  # noqa: BLE001 - evcc logs and skips this cycle
            _LOGGER.error("charger status: %s", err)
            return

        self.publish("connected", self.connected())
        self.publish("charging", self.charging())

        # sync settings with charger
        try:
            self.sync_charger()
        except Exception as err:  # noqa: BLE001 - evcc logs and skips this cycle
            _LOGGER.error("%s", err)
            return

        mode = self.get_mode()
        self.publish("mode", mode)

        # execute loading strategy
        try:
            if not self.connected():
                # always disable charger if not connected (evcc #105)
                self.set_limit(0)

            elif self.scale_phases_required():
                try:
                    self.scale_phases(self.phases_configured)
                except api.NotAvailableError:
                    # the charger cannot switch phases right now. Adopt the configured
                    # phase count so the switch is not re-attempted on every cycle (evcc #29974).
                    self.set_phases(self.phases_configured)

            elif mode == ChargeMode.OFF:
                self.set_limit(0)

            elif mode == ChargeMode.NOW:
                self.fast_charging()

            elif mode == ChargeMode.SMART:
                target_current = self.pv_max_current(site_power, battery_power)
                self.set_limit(target_current)
        except Exception as err:  # noqa: BLE001 - evcc logs any error
            _LOGGER.error("%s", err)

    # ------------------------------------------------------------------
    # effective values (evcc core/loadpoint_effective.go)
    # ------------------------------------------------------------------

    def effective_priority(self) -> int:
        """evcc core/loadpoint_effective.go: EffectivePriority (vehicle priority excluded)."""
        return self.get_priority()

    def effective_min_current(self) -> float:
        """evcc core/loadpoint_effective.go: effectiveMinCurrent (vehicle and power limiter excluded)."""
        lp_min = self.min_current
        vehicle_min = 0.0
        charger_min = 0.0

        if api.has_cap(self.charger, api.CurrentLimiter):
            try:
                charger_min, _ = self.charger.get_min_max_current()
            except Exception:  # noqa: BLE001 - evcc ignores the error
                charger_min = 0.0

        if max(vehicle_min, charger_min) == 0:
            return lp_min
        if charger_min > 0:
            return max(vehicle_min, charger_min)
        return max(vehicle_min, lp_min)

    def effective_max_current(self) -> float:
        """evcc core/loadpoint_effective.go: effectiveMaxCurrent (vehicle and power limiter excluded)."""
        max_current = self.max_current

        if api.has_cap(self.charger, api.CurrentLimiter):
            try:
                _, res = self.charger.get_min_max_current()
            except Exception:  # noqa: BLE001 - evcc ignores the error
                res = 0.0
            if res > 0:
                max_current = min(max_current, res)

        return max_current

    def effective_step_power(self) -> float:
        """evcc core/loadpoint_effective.go: EffectiveStepPower."""
        return grid.current_to_power(1, self.active_phases())

    def effective_min_power(self) -> float:
        """evcc core/loadpoint_effective.go: EffectiveMinPower (min current on the minimum active phases)."""
        return grid.current_to_power(self.effective_min_current(), self.min_active_phases())

    def effective_max_power(self) -> float:
        """evcc core/loadpoint_effective.go: EffectiveMaxPower (capped by the circuit power limit)."""
        circuit_max_power = self.circuit.get_max_power() if self.circuit is not None else 0.0
        if circuit_max_power > 0:
            return min(self._effective_max_power(), circuit_max_power)
        return self._effective_max_power()

    def _effective_max_power(self) -> float:
        """evcc core/loadpoint_effective.go: effectiveMaxPower (vehicle max power excluded)."""
        return grid.current_to_power(self.effective_max_current(), self.max_active_phases())

    # ------------------------------------------------------------------
    # api (evcc core/loadpoint_api.go)
    # ------------------------------------------------------------------

    def get_title(self) -> str:
        """evcc core/loadpoint_api.go: GetTitle."""
        return self.title

    def get_status(self) -> ChargeStatus:
        """evcc core/loadpoint_api.go: GetStatus."""
        return self.status

    def get_mode(self) -> ChargeMode:
        """evcc core/loadpoint_api.go: GetMode."""
        return self.mode

    def normalize_mode(self, mode: ChargeMode) -> tuple[ChargeMode, AlwaysCharge | None]:
        """evcc core/loadpoint_api.go: normalizeMode (switch devices excluded: always supported)."""
        return ChargeMode(mode).normalize()

    def set_mode(self, mode: ChargeMode | str) -> None:
        """evcc core/loadpoint_api.go: SetMode.

        ``pv`` and ``minpv`` map to ``smart`` plus always charge off / on.
        """
        try:
            mode = ChargeMode(str(mode).lower())
        except ValueError:
            _LOGGER.error("invalid charge mode: %s", mode)
            return

        _LOGGER.debug("set charge mode: %s", mode)

        # normalize deprecated aliases; must happen before the change check since
        # pv/minpv carry an always charge side effect even when mode stays smart
        mode, always_charge = self.normalize_mode(mode)
        if always_charge is not None:
            self._set_always_charge(always_charge)

        # apply immediately
        if self.mode != mode:
            self.mode = mode
            self.publish("mode", mode)

            # reset timers
            if mode in (ChargeMode.NOW, ChargeMode.OFF):
                self.reset_phase_timer()
                self.reset_pv_timer()
            elif mode == ChargeMode.SMART and self.always_charge.active():
                self.reset_pv_timer()

            self.request_update()

    def get_always_charge(self) -> AlwaysCharge:
        """evcc core/loadpoint_api.go: GetAlwaysCharge."""
        return self.always_charge

    def _set_always_charge(self, always_charge: AlwaysCharge) -> None:
        """evcc core/loadpoint_api.go: setAlwaysCharge (persistence excluded)."""
        if self.always_charge == always_charge:
            return
        self.always_charge = always_charge
        self.publish("alwaysCharge", always_charge)
        if always_charge.active():
            self.reset_pv_timer()
        self.request_update()

    def set_always_charge(self, always_charge: AlwaysCharge) -> None:
        """evcc core/loadpoint_api.go: SetAlwaysCharge (switch devices excluded: always supported)."""
        _LOGGER.debug("set always charge: %s", always_charge)
        if always_charge != self.always_charge:
            self._set_always_charge(AlwaysCharge(always_charge))

    def get_default_mode(self) -> ChargeMode:
        """evcc core/loadpoint_api.go: GetDefaultMode."""
        return self.default_mode

    def set_default_mode(self, mode: ChargeMode) -> None:
        """evcc core/loadpoint_api.go: SetDefaultMode (pv/minpv map to smart, always charge untouched)."""
        mode, _ = self.normalize_mode(mode)
        self.default_mode = mode

    def get_priority(self) -> int:
        """evcc core/loadpoint_api.go: GetPriority."""
        return self.priority

    def set_priority(self, priority: int) -> None:
        """evcc core/loadpoint_api.go: SetPriority."""
        if self.priority != priority:
            self.priority = priority
            self.publish("priority", priority)

    def get_phases(self) -> int:
        """evcc core/loadpoint_api.go: GetPhases (enabled phases)."""
        return self.phases

    def get_phases_configured(self) -> int:
        """evcc core/loadpoint_api.go: GetPhasesConfigured."""
        return self.phases_configured

    def set_phases_configured(self, phases: int) -> None:
        """evcc core/loadpoint_api.go: SetPhasesConfigured (validates, then setPhasesConfigured)."""
        # limit auto mode (phases=0) to scalable charger
        if not self.has_phase_switching() and phases == 0:
            raise ValueError("charger does not support phase switching")
        if phases not in (0, 1, 3):
            raise ValueError(f"invalid number of phases: {phases}")
        physical = self.get_charger_physical_phases()
        if physical != 0 and phases > physical:
            raise ValueError(f"cannot configure more phases than physically connected: {phases} > {physical}")
        self._set_phases_configured(phases)
        self.request_update()

    def get_enable_delay(self) -> float:
        """evcc core/loadpoint_api.go: GetEnableDelay."""
        return self.enable.delay

    def set_enable_delay(self, delay: float) -> None:
        """evcc core/loadpoint_api.go: SetEnableDelay."""
        self.enable.delay = delay

    def get_disable_delay(self) -> float:
        """evcc core/loadpoint_api.go: GetDisableDelay."""
        return self.disable.delay

    def set_disable_delay(self, delay: float) -> None:
        """evcc core/loadpoint_api.go: SetDisableDelay."""
        self.disable.delay = delay

    def set_thresholds(self, enable: ThresholdConfig, disable: ThresholdConfig) -> None:
        """evcc core/loadpoint_api.go: SetThresholds / setThresholds (persistence excluded)."""
        self.enable = replace(enable)
        self.disable = replace(disable)
        self.publish("enableThreshold", self.enable.threshold)
        self.publish("disableThreshold", self.disable.threshold)
        self.request_update()

    def set_enable_threshold(self, threshold: float) -> None:
        """evcc core/loadpoint_api.go: SetEnableThreshold (only on a change, via setThresholds)."""
        if self.enable.threshold != threshold:
            self.enable.threshold = threshold
            self.set_thresholds(self.enable, self.disable)

    def set_disable_threshold(self, threshold: float) -> None:
        """evcc core/loadpoint_api.go: SetDisableThreshold (only on a change, via setThresholds)."""
        if self.disable.threshold != threshold:
            self.disable.threshold = threshold
            self.set_thresholds(self.enable, self.disable)

    def get_solar_share(self) -> float:
        """evcc core/loadpoint_api.go: GetSolarShare."""
        return self.solar_share

    def get_charge_power(self) -> float:
        """evcc core/loadpoint_api.go: GetChargePower."""
        return self.charge_power

    def get_charge_power_flexibility(self) -> float:
        """evcc core/loadpoint_api.go: GetChargePowerFlexibility.

        The flexible amount of the current charging power (min SoC, plans,
        battery boost, smart cost and optimizer excluded).
        """
        mode = self.get_mode()
        if mode == ChargeMode.NOW or not self.charging():
            return 0.0
        if surplus_flexible(self):
            return self.get_charge_power()
        return max(0.0, self.get_charge_power() - self.effective_min_power())

    def get_max_phase_current(self) -> float:
        """evcc core/loadpoint_api.go: GetMaxPhaseCurrent.

        Largest charge current per phase, or the offered current if not measured.
        """
        if self.charge_currents is None:
            return self.offered_current
        current = max(self.charge_currents[0], self.charge_currents[1], self.charge_currents[2])
        # measurements drop to ~0 while phases switch, reserve the offered current
        if self.enabled and not self.phase_switch_completed():
            current = max(current, self.offered_current)
        return current

    def get_min_current(self) -> float:
        """evcc core/loadpoint_api.go: GetMinCurrent."""
        return self.min_current

    def set_min_current(self, current: float) -> None:
        """evcc core/loadpoint_api.go: SetMinCurrent."""
        if current > self.max_current:
            raise ValueError("min current must be smaller or equal than max current")
        self.min_current = current

    def get_max_current(self) -> float:
        """evcc core/loadpoint_api.go: GetMaxCurrent."""
        return self.max_current

    def set_max_current(self, current: float) -> None:
        """evcc core/loadpoint_api.go: SetMaxCurrent."""
        if current < self.min_current:
            raise ValueError("max current must be greater or equal than min current")
        self.max_current = current

    def get_circuit(self) -> Any:
        """evcc core/loadpoint_api.go: GetCircuit."""
        return self.circuit


def new_loadpoint(clock: Clock | None = None) -> Loadpoint:
    """evcc core/loadpoint.go: NewLoadpoint (a Loadpoint with evcc's defaults)."""
    return Loadpoint(
        clock=clock if clock is not None else SystemClock(),
        mode=ChargeMode.OFF,
        always_charge=AlwaysCharge.OFF,
        status=ChargeStatus.NONE,
        min_current=6.0,  # A
        max_current=16.0,  # A
        solar_share=1.0,  # full surplus
        enable=ThresholdConfig(threshold=0.0, delay=1 * MINUTE),
        disable=ThresholdConfig(threshold=0.0, delay=3 * MINUTE),
    )


def new_loadpoint_from_config(
    charger: Any,
    *,
    clock: Clock | None = None,
    circuit: Any = None,
    charge_meter: Any = None,
    title: str = "",
    priority: int = 0,
    mode: ChargeMode | str = ChargeMode.EMPTY,
    min_current: float | None = None,
    max_current: float | None = None,
    phases_configured: int | None = None,
    enable: ThresholdConfig | None = None,
    disable: ThresholdConfig | None = None,
    solar_share: float | None = None,
    publish_cb: Callable[[str, Any], None] | None = None,
) -> Loadpoint:
    """evcc core/loadpoint.go: NewLoadpointFromConfig (+ the settings evcc restores).

    ``mode`` is evcc's ``DefaultMode``. Vehicles, charge meter registry and
    settings persistence are excluded; a legacy default mode (pv/minpv) seeds
    always charge on every start (evcc: only on the first boot).
    """
    lp = new_loadpoint(clock)
    lp.charger = charger
    lp.circuit = circuit
    lp.charge_meter = charge_meter
    lp.title = title
    lp.priority = priority
    lp.publish_cb = publish_cb
    if min_current is not None:
        lp.min_current = min_current
    if max_current is not None:
        lp.max_current = max_current
    if enable is not None:
        lp.enable = replace(enable)  # Go copies the struct by value
    if disable is not None:
        lp.disable = replace(disable)
    if solar_share is not None:
        lp.solar_share = solar_share

    # validate thresholds
    if lp.enable.threshold > lp.disable.threshold:
        _LOGGER.warning(
            "PV mode enable threshold (%.0fW) is larger than disable threshold (%.0fW)",
            lp.enable.threshold,
            lp.disable.threshold,
        )
    elif lp.enable.threshold > 0:
        _LOGGER.warning(
            "PV mode enable threshold %.0fW > 0 will start PV charging on grid power consumption. Did you mean -%.0f?",
            lp.enable.threshold,
            lp.enable.threshold,
        )

    # choose sane default if mode is not set
    lp.default_mode = ChargeMode(mode)
    lp.mode = lp.default_mode if lp.default_mode != ChargeMode.EMPTY else ChargeMode.OFF

    # migrate deprecated default modes; a legacy default seeds always charge
    lp.mode, always_charge = lp.normalize_mode(lp.mode)
    if always_charge is not None:
        lp.always_charge = always_charge
    lp.default_mode, _ = lp.normalize_mode(lp.default_mode)

    # phase switching defaults based on charger capabilities
    if not lp.has_phase_switching():
        phases = lp.get_charger_physical_phases()
        if phases == 0:
            phases = 3  # default to 3p if no charger phases are known
        lp.phases_configured = phases
        lp.phases = phases

    # evcc restoreSettings: the configured phases apply when v > 0 || hasPhaseSwitching;
    # setPhasesConfigured also sets the phases of a charger without phase switching
    if phases_configured is not None and (phases_configured > 0 or lp.has_phase_switching()):
        lp._set_phases_configured(phases_configured)

    return lp
