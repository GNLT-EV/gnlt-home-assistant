"""Site: site power from meters, priorities, the loadpoint update loop (no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Sources: evcc ``core/site.go`` (updateGridMeter, updatePvMeters,
updateBatteryMeters, updateAuxMeters, updateMeters, sitePower,
updateLoadpoints, loadpointSitePower, reservedPVPower, update, updatePower,
loopLoadpoints, Run), ``core/site_circuits.go`` (updateCircuits),
``core/site_api.go`` (activeLoadpoints, loadpointsAsCircuitDevices,
residual power), ``core/site_startup.go`` (startupOrder, startupRank).

How evcc drives the loadpoints (kept here): one site cycle updates ONE
loadpoint. Every cycle reads all loadpoints' charge power (and their
flexible power for the prioritizer), refreshes the circuits, reads the site
meters, computes the site power and calls ``Update`` of that loadpoint.
Loadpoints take turns round-robin (:meth:`Site.tick`); after start every
loadpoint is updated once, charging ones first (:meth:`Site.run_startup`);
a loadpoint that requested an update is served in between
(:meth:`Site.handle_update_requests`). Priorities do not pass a "remaining
power" from one loadpoint to the next: a surplus-flexible loadpoint counts
the flexible power of lower-priority loadpoints as available
(:mod:`.prioritizer`), and a lower-priority loadpoint holds back the max
power of higher-priority ones that are about to start (``reserved_pv_power``).

The caller schedules:meth:`Site.tick` every interval (evcc default 30 s);
nothing here sleeps.

Excluded: battery strategy (prioritySoc, bufferSoc, bufferStartSoc, battery
boost and modes, discharge limits; battery *power* still enters the site
power), tariffs, the optimizer and its suggestions, HEMS, green share and
statistics, excess DC power, energy collectors, Modbus retry/backoff.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from . import api
from .api import ChargeStatus
from .loadpoint import Loadpoint, surplus_flexible
from .prioritizer import Prioritizer

_LOGGER = logging.getLogger(__name__)

STANDBY_POWER = 10  # W, consider less than 10W as charger in standby


@dataclass
class SiteState:
    """evcc core/site.go: siteState (cached measurement state, battery soc excluded)."""

    grid_power: float = 0.0
    pv_power: float = 0.0
    aux_power: float = 0.0
    battery_power: float = 0.0


@dataclass
class SitePowerResult:
    """evcc core/site.go: sitePowerResult."""

    state: SiteState
    # net power exported by the site minus a residual margin (negative: grid export, battery charging)
    power: float
    # battery strategy excluded: always False / 0 (evcc values without prioritySoc/bufferSoc)
    battery_buffered: bool = False
    battery_start: bool = False
    priority_adjustment: float = 0.0


def startup_rank(lp: Loadpoint) -> int:
    """evcc core/site_startup.go: startupRank (0 charging, 1 vehicle connected, 2 otherwise)."""
    if lp.get_charge_power() > STANDBY_POWER:
        return 0
    try:
        status = lp.charger.status()
    except Exception:  # noqa: BLE001 - evcc ranks an unreadable charger last
        return 2
    if status in (ChargeStatus.B, ChargeStatus.C):
        return 1
    return 2


def startup_order(lps: list[Loadpoint]) -> list[Loadpoint]:
    """evcc core/site_startup.go: startupOrder (stable sort by startup rank)."""
    ranked = [(startup_rank(lp), i, lp) for i, lp in enumerate(lps)]
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [lp for _, _, lp in ranked]


class Site:
    """evcc core/site.go: Site."""

    def __init__(
        self,
        *,
        loadpoints: list[Loadpoint | None] | None = None,
        grid_meter: Any = None,
        pv_meters: list[Any] | None = None,
        battery_meters: list[Any] | None = None,
        aux_meters: list[Any] | None = None,
        circuit: Any = None,
        residual_power: float = 0.0,
        publish_cb: Callable[[str, Any], None] | None = None,
    ) -> None:
        """``residual_power`` (W): grid meter: household safety margin (evcc ``residualPower``)."""
        self.loadpoints: list[Loadpoint | None] = list(loadpoints or [])
        self.grid_meter = grid_meter
        self.pv_meters: list[Any] = list(pv_meters or [])
        self.battery_meters: list[Any] = list(battery_meters or [])
        self.aux_meters: list[Any] = list(aux_meters or [])
        self.circuit = circuit
        self.residual_power = residual_power
        self.publish_cb = publish_cb

        self.prioritizer = Prioritizer()
        self.lp_update_chan: queue.Queue = queue.Queue(maxsize=max(1, len(self.loadpoints)))
        self._state = SiteState()
        self._loop: Iterator[Loadpoint | None] | None = None

    # ------------------------------------------------------------------
    # api (evcc core/site_api.go)
    # ------------------------------------------------------------------

    def publish(self, key: str, val: Any) -> None:
        """evcc core/site.go: publish (optional callback here)."""
        if self.publish_cb is not None:
            self.publish_cb(key, val)

    def active_loadpoints(self) -> list[Loadpoint]:
        """evcc core/site_api.go: activeLoadpoints (disabled slots are None)."""
        return [lp for lp in self.loadpoints if lp is not None]

    def loadpoints_as_circuit_devices(self) -> list[Loadpoint]:
        """evcc core/site_api.go: loadpointsAsCircuitDevices."""
        return self.active_loadpoints()

    def has_meters(self) -> bool:
        """evcc core/site_api.go: hasMeters (ext meters excluded)."""
        return (
            self.grid_meter is not None
            or len(self.pv_meters) > 0
            or len(self.battery_meters) > 0
            or len(self.aux_meters) > 0
        )

    def is_configured(self) -> bool:
        """evcc core/site_api.go: IsConfigured (loadpoints or meters present)."""
        return bool(self.active_loadpoints()) or self.has_meters()

    def get_residual_power(self) -> float:
        """evcc core/site_api.go: GetResidualPower."""
        return self.residual_power

    def set_residual_power(self, power: float) -> None:
        """evcc core/site_api.go: SetResidualPower (persistence excluded)."""
        if self.residual_power != power:
            self.residual_power = power
            self.publish("residualPower", power)

    def get_circuit(self) -> Any:
        """evcc core/site_api.go: GetCircuit (root circuit)."""
        return self.circuit

    def state(self) -> SiteState:
        """evcc core/site.go: state (copy of the cached measurement state)."""
        return replace(self._state)

    # ------------------------------------------------------------------
    # meters (evcc core/site.go)
    # ------------------------------------------------------------------

    def _collect_powers(self, key: str, meters: list[Any]) -> list[float]:
        """evcc core/site.go: collectMeters (power only; energy excluded)."""
        res = []
        for i, meter in enumerate(meters):
            power = 0.0
            try:
                power = meter.current_power()
                _LOGGER.debug("%s %d power: %.0fW", key, i + 1, power)
            except api.NotAvailableError:
                pass
            except Exception as err:  # noqa: BLE001 - evcc logs, the meter counts 0 W
                _LOGGER.error("%s %d power: %s", key, i + 1, err)
            res.append(power)
        return res

    def update_pv_meters(self) -> None:
        """evcc core/site.go: updatePvMeters (excess DC excluded)."""
        if not self.pv_meters:
            return
        powers = self._collect_powers("pv", self.pv_meters)
        for i, power in enumerate(powers):
            if power < -500:
                _LOGGER.warning(
                    "pv %d power: %.0fW is negative - check configuration if sign is correct", i + 1, power
                )
        pv_power = sum(max(0.0, p) for p in powers)
        self._state.pv_power = pv_power
        self.publish("pvPower", pv_power)

    def update_battery_meters(self) -> None:
        """evcc core/site.go: updateBatteryMeters (power only; SoC, limits, modes excluded)."""
        if not self.battery_meters:
            return
        powers = self._collect_powers("battery", self.battery_meters)
        self._state.battery_power = sum(powers)
        self.publish("batteryPower", self._state.battery_power)

    def update_aux_meters(self) -> None:
        """evcc core/site.go: updateAuxMeters."""
        if not self.aux_meters:
            return
        powers = self._collect_powers("aux", self.aux_meters)
        self._state.aux_power = sum(powers)
        self.publish("auxPower", self._state.aux_power)

    def update_grid_meter(self) -> None:
        """evcc core/site.go: updateGridMeter (power and signed phase currents; energy excluded)."""
        if self.grid_meter is None:
            return

        try:
            power = self.grid_meter.current_power()
        except api.NotAvailableError:
            pass
        except Exception as err:
            raise RuntimeError(f"grid power: {err}") from err
        else:
            self._state.grid_power = power
            _LOGGER.debug("grid power: %.0fW", power)
            self.publish("gridPower", power)

        # grid phase currents (signed)
        if api.has_cap(self.grid_meter, api.PhaseCurrents):
            p1 = p2 = p3 = 0.0
            if api.has_cap(self.grid_meter, api.PhasePowers):
                try:
                    p1, p2, p3 = self.grid_meter.powers()
                except api.NotAvailableError:
                    pass
                except Exception as err:  # noqa: BLE001 - evcc logs and continues
                    _LOGGER.error("grid powers: %s", err)
            try:
                i1, i2, i3 = self.grid_meter.currents()
            except api.NotAvailableError:
                pass
            except Exception as err:  # noqa: BLE001 - evcc logs and continues
                _LOGGER.error("grid currents: %s", err)
            else:
                currents = [api.sign_from_power(i1, p1), api.sign_from_power(i2, p2), api.sign_from_power(i3, p3)]
                self.publish("gridCurrents", currents)

    def update_meters(self) -> SiteState:
        """evcc core/site.go: updateMeters (reads all meters; a grid meter error is raised)."""
        self.update_pv_meters()
        self.update_battery_meters()
        self.update_aux_meters()
        self.update_grid_meter()
        return self.state()

    # ------------------------------------------------------------------
    # site power (evcc core/site.go)
    # ------------------------------------------------------------------

    def site_power(self, state: SiteState, total_charge_power: float, flexible_power: float) -> SitePowerResult:
        """evcc core/site.go: sitePower (battery strategy excluded).

        Site power balance from the measured state; negative = surplus.
        """
        residual_power = self.get_residual_power()

        # allow using PV as estimate for grid power
        if self.grid_meter is None:
            state.grid_power = total_charge_power - state.pv_power
            self.publish("gridPower", state.grid_power)

        # (battery priority below prioritySoc excluded: no priority adjustment)

        # allow using grid, charge and battery power as estimate for pv power
        if not self.pv_meters and self.grid_meter is not None:
            state.pv_power = max(0.0, total_charge_power - state.grid_power - state.battery_power + residual_power)
            _LOGGER.debug("pv power: %.0fW", state.pv_power)
            self.publish("pvPower", state.pv_power)

        # (bufferSoc / bufferStartSoc excluded: battery_buffered and battery_start stay False)
        battery_power = state.battery_power

        site_power = state.grid_power + battery_power + residual_power - state.aux_power - flexible_power
        _LOGGER.debug(
            "site power: %.0fW%s",
            site_power,
            f" (including {flexible_power:.0f}W prioritized power)" if flexible_power > 0 else "",
        )
        return SitePowerResult(state=state, power=site_power)

    def update_loadpoints(self) -> float:
        """evcc core/site.go: updateLoadpoints (all loadpoints' charge power and flexibility)."""
        total = 0.0
        for lp in self.active_loadpoints():
            try:
                power = lp.update_charge_power_and_currents()
                self.prioritizer.update_charge_power_flexibility(lp)
            except Exception:  # noqa: BLE001 - one loadpoint must not stop the site cycle
                _LOGGER.exception("loadpoint %s: charge power failed", lp.get_title())
                continue
            total += power
        return total

    def update_circuits(self) -> None:
        """evcc core/site_circuits.go: updateCircuits."""
        if self.circuit is None:
            return
        try:
            self.circuit.update(self.loadpoints_as_circuit_devices())
        except Exception as err:  # noqa: BLE001 - evcc logs and continues
            _LOGGER.error("%s", err)

    def loadpoint_site_power(self, lp: Loadpoint, res: SitePowerResult) -> float:
        """evcc core/site.go: loadpointSitePower (boost/optimizer adjustments excluded).

        The site power the loadpoint controls against, including the surplus
        reserved for higher-priority loadpoints that are starting up.
        """
        return res.power + self.reserved_pv_power(lp)

    def reserved_pv_power(self, lp: Loadpoint) -> float:
        """evcc core/site.go: reservedPVPower (evcc #31194).

        Anticipated surplus claimed by higher-priority PV loadpoints that are starting up.
        """
        if not surplus_flexible(lp):
            return 0.0

        prio = lp.effective_priority()
        reserved = 0.0
        for other in self.active_loadpoints():
            if other is lp:
                continue
            if other.effective_priority() > prio and other.pv_charge_starting():
                reserved += other.effective_max_power()

        if reserved > 0:
            _LOGGER.debug(
                "lp %s reserves %.0fW for higher-priority loadpoints starting up", lp.get_title(), reserved
            )
        return reserved

    # ------------------------------------------------------------------
    # update cycle (evcc core/site.go)
    # ------------------------------------------------------------------

    def update(self, lp: Loadpoint | None) -> None:
        """evcc core/site.go: update (one site cycle for one loadpoint; tariffs/battery/optimizer excluded).

        No exception escapes: an error is logged and that loadpoint is skipped
        for this cycle, like an evcc charger error.
        """
        try:
            self._update(lp)
        except Exception:  # noqa: BLE001 - the site loop must keep running
            _LOGGER.exception("site update failed, loadpoint %s skipped", lp.get_title() if lp is not None else "-")

    def _update(self, lp: Loadpoint | None) -> None:
        """evcc core/site.go: update (body)."""
        _LOGGER.debug("----")

        # update loadpoints
        total_charge_power = self.update_loadpoints()

        self.update_circuits()

        try:
            state = self.update_meters()
        except Exception as err:  # noqa: BLE001 - evcc logs and skips the loadpoint update
            _LOGGER.error("%s", err)
            return

        self.update_power(lp, state, total_charge_power)

    def update_power(self, lp: Loadpoint | None, state: SiteState, total_charge_power: float) -> None:
        """evcc core/site.go: updatePower (calculates the site power balance and updates the loadpoint)."""
        # prioritize if possible
        flexible_power = 0.0
        if lp is not None and surplus_flexible(lp):
            flexible_power = self.prioritizer.get_charge_power_flexibility(lp)

        res = self.site_power(state, total_charge_power, flexible_power)
        state = res.state

        # retain the estimates for the api getters
        if self.grid_meter is None or not self.pv_meters:
            self._state.grid_power, self._state.pv_power = state.grid_power, state.pv_power

        # ignore negative pvPower values as that means it is not an energy source but consumption
        home_power = state.grid_power + max(0.0, state.pv_power) + state.battery_power - total_charge_power
        self.publish("homePower", max(home_power, 0.0))

        if lp is not None:
            lp.update(self.loadpoint_site_power(lp, res), state.battery_power)

    # ------------------------------------------------------------------
    # loop (evcc core/site.go: Prepare, loopLoadpoints, Run)
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """evcc core/site.go: Prepare (attaches the update queue and prepares the loadpoints)."""
        self.lp_update_chan = queue.Queue(maxsize=max(1, len(self.loadpoints)))
        for lp in self.active_loadpoints():
            lp.lp_chan = self.lp_update_chan
            lp.prepare()

    def loop_loadpoints(self) -> Iterator[Loadpoint | None]:
        """evcc core/site.go: loopLoadpoints (round-robin; None in meter-only mode)."""
        active = self.active_loadpoints()
        while True:
            if not active:
                yield None
            else:
                yield from active

    def run_startup(self) -> None:
        """evcc core/site.go: Run, start: first loop step, then every loadpoint once (startupOrder)."""
        self._loop = self.loop_loadpoints()
        self.update(next(self._loop))  # start immediately
        # don't let the others wait for their turn after startup
        for lp in startup_order(self.active_loadpoints()):
            self.update(lp)

    def tick(self) -> None:
        """evcc core/site.go: Run, one interval tick: update the next loadpoint round-robin."""
        if self._loop is None:
            self._loop = self.loop_loadpoints()
        self.update(next(self._loop))

    def handle_update_requests(self) -> None:
        """evcc core/site.go: Run, lpUpdateChan: update the loadpoints that requested it."""
        while True:
            try:
                lp = self.lp_update_chan.get_nowait()
            except queue.Empty:
                return
            self.update(lp.dequeued())
