"""Site coordinator: DLM + PV for the member stations of one GNLT site (Home Assistant side).

One asyncio task per site entry; one cycle every meter update period
(default 10 s, openWB's control interval). A cycle:

a. reads the grid meter entities (three phase currents, import positive,
   optional sign inversion) and every member station snapshot and builds the
   inputs of the DLM allocator (``site/dlm.py``). The meter is read every
   cycle; ``GridReading.updated_at`` = read time (monotonic) minus the age of
   the oldest phase's ``last_reported`` (else ``last_updated``), so a value
   not reported again is "not new" for the allocator; a state
   that is unavailable / unknown / not a number, or whose ``last_reported``
   (else ``last_updated``) is older than 3 x the meter period, is a meter
   error (the last good currents are passed with ``error=True``);
b. runs the allocator and hands each member's loadpoint its cap through a
   per-member circuit (:class:`DlmCircuit`, the ``site/api.py`` Circuit
   protocol: ``validate_current`` returns ``min(new, allowed)``);
c. enforces DLM decreases at once for every member (openWB sends every
   station every cycle): a station above its allowed current is set to it,
   allowed 0 disables it (the evcc disable path of the loadpoint), allowed
   ``None`` (reserved: station offline / no reading) sends nothing;
d. runs the evcc site tick (``site/site.py``) every 30 s (evcc's default
   interval): one loadpoint per tick, round-robin, as evcc; a new loadpoint is
   updated once at once (evcc Run) and update requests (mode changes) are
   served every cycle (evcc lpUpdateChan).

While a station is a member the coordinator is its only automatic writer:
its auto start, schedule and resume after power loss are filtered out of the
command-layer options (``ModbusRuntime.site_options_filter``), and its manual
controls act on the loadpoint (``site/members.py``).

Safety: if the task crashes, or the site entry unloads, every member is left
at its station minimum current (no stop, no start) and the HA side raises a
repair issue (``on_stopped``). DLM is active in every mode, including Now.

Loadpoint request: the DLM ``requested_a`` of a member is the last current
its loadpoint asked for in ``set_limit`` (before the circuit cap); before
the loadpoint's first update it is the station's present setpoint if it is
charging, else 0.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..modbus import const as MC
from ..modbus.commands import KEY_PHASE_NA, ORIGIN_SITE, CommandError, Target
from ..modbus.station import WriteRefused
from . import api, grid
from .api import ChargeMode
from .config import MemberConfig, SiteConfig
from .dlm import (
    DlmAllocator,
    DlmConfig,
    DlmResult,
    GridReading,
    MeterState,
    StationAllocation,
    StationInput,
    grid_phases,
)
from .gnlt_charger_adapter import GnltChargerAdapter
from .grid import VOLTAGE
from .loadpoint import Loadpoint, ThresholdConfig, new_loadpoint_from_config
from .members import no_automatic_features, site_data
from .site import Site

_LOGGER = logging.getLogger(__name__)

MODES = ["off", "now", "minpv", "pv"]  # evcc loadpoint modes (pv/minpv normalized to smart in the core)
PHASE_AUTO = "auto"
STALE_FACTOR = 3  # meter state older than 3 x the meter period = error
# evcc site loop interval (evcc default 30 s; evcc warns that < 30 s "can lead to
# unexpected behavior", core/site.go:1618-1621). The DLM allocation and the
# enforcement of decreases run every meter period.
EVCC_INTERVAL_S = 30.0
START_SETTLE_S = 30.0  # after our own start the station's measurement lags the meter
# Dead band of the DLM cap (A), increases only: a cap that rises by no more than
# this keeps the offered current. Decreases are enforced exactly.
# Same value as the allocator's overdraw tolerance (dlm.py overdraw_tolerance_a).
DEADBAND_A = DlmConfig.__dataclass_fields__["overdraw_tolerance_a"].default
_STEP_EPS = 0.05  # half the 0.1 A station step
_INVALID = ("unavailable", "unknown", "", None)


class _Clock:
    """``site/clock.py`` Clock protocol on a plain callable."""

    def __init__(self, fn: Callable[[], float]) -> None:
        self._fn = fn

    def now(self) -> float:
        return self._fn()




class DlmCircuit:
    """The DLM cap of one member in the evcc circuit interface (``site/api.py`` Circuit).

    ``allowed`` is the allocator's result for the station: a value caps the
    loadpoint at ``min(new, allowed)``; ``None`` (reserved, or not allocated
    yet) allows no increase. Power is not limited here: the allocator's
    current already includes the site power limit.

    A request above ``allowed`` may in addition take what the allocation left
    free on the station's grid phases this cycle (the DLM result's headroom
    after every booking and the hold pass, and the power left), like evcc's
    own circuit caps a request at the circuit's headroom. Without it a
    loadpoint that asked for 0 A in the last allocation (PV enable timer
    running, or mode just changed) could never start: its single "start at
    minimum" decision would always meet the cap of the 0 A request. Granted
    spare is shared between the members within the cycle.
    """

    def __init__(self, coord: SiteCoordinator | None = None, member: SiteMember | None = None) -> None:
        self.allowed: float | None = None
        self.coord = coord
        self.member = member

    def validate_current(self, old: float, new: float) -> float:
        if self.allowed is None:
            return min(new, max(old, 0.0))
        if new <= self.allowed or self.coord is None or self.member is None:
            cap = min(new, self.allowed)
        else:
            cap = self.allowed + self.coord.grant_spare(self.member, new - self.allowed)
        lp = self.member.loadpoint if self.member is not None else None
        if lp is not None and lp.enabled and new >= lp.offered_current and lp.offered_current <= cap <= lp.offered_current + DEADBAND_A:
            # an increase smaller than the dead band is not made (no 0.1 A
            # dithering of reg 91); a decrease is always made exactly
            return min(new, lp.offered_current)
        return cap

    def _power_limit(self) -> float | None:
        """The DLM cap as power: (allowed + spare granted) x the loadpoint's active
        phases x 230 V (evcc's circuit current <-> power conversion, util/grid);
        reserved: what it is offered now. None = no loadpoint yet."""
        lp = self.member.loadpoint if self.member is not None else None
        if lp is None:
            return None
        phases = lp.active_phases()
        if self.allowed is None:
            return grid.current_to_power(max(lp.offered_current, 0.0), phases)
        allowed = self.allowed + self.member.spare_granted
        return grid.current_to_power(allowed, phases)

    def validate_power(self, old: float, new: float) -> float:
        """evcc circuitAllowsPhases / setLimit see the DLM limit."""
        limit = self._power_limit()
        return new if limit is None else min(new, limit)

    def get_max_power(self) -> float:
        """evcc treats 0 as "no limit": a zero DLM cap is a tiny positive power."""
        limit = self._power_limit()
        if limit is None:
            return 0.0
        return max(limit, 1e-3)

    def update(self, loadpoints: list[Any]) -> None:
        return None

    def get_charge_power(self) -> float:
        return 0.0

    def get_max_phase_current(self) -> float:
        return 0.0

    def get_parent(self) -> Any:
        return None


class _GridMeter:
    """evcc site grid meter on the coordinator's meter read of this cycle."""

    capabilities = frozenset({api.Meter, api.PhaseCurrents})

    def __init__(self, coord: SiteCoordinator) -> None:
        self.coord = coord

    def current_power(self) -> float:
        coord = self.coord
        if coord.config.grid_power_entity:
            power = coord.read_power(coord.config.grid_power_entity)
            return -power if coord.config.grid_invert else power
        if coord.cycle_currents is None:
            raise api.NotAvailableError("grid meter")
        # no power sensor: signed phase currents x nominal voltage
        return sum(coord.cycle_currents) * VOLTAGE

    def currents(self) -> tuple[float, float, float]:
        if self.coord.cycle_currents is None:
            raise api.NotAvailableError("grid meter")
        return self.coord.cycle_currents


class _PowerMeter:
    capabilities = frozenset({api.Meter})

    def __init__(self, coord: SiteCoordinator, entity_id: str) -> None:
        self.coord = coord
        self.entity_id = entity_id

    def current_power(self) -> float:
        return self.coord.read_power(self.entity_id)


class SiteMember:
    """One member station: its runtime, evcc adapter, loadpoint and DLM state.

    The station entities talk to it while the site runs (``site/members.py``).
    """

    def __init__(self, coord: SiteCoordinator, cfg: MemberConfig) -> None:
        self.coord = coord
        self.cfg = cfg
        self.entry_id = cfg.entry_id
        self.runtime: Any = None
        self.adapter: GnltChargerAdapter | None = None
        self.loadpoint: Loadpoint | None = None
        self.circuit = DlmCircuit(coord, self)
        self.grid_phases: tuple[int, ...] = (0, 1, 2)
        self.spare_granted = 0.0
        self.requested: float | None = None
        self.charging_since: float | None = None
        self.last_seen: float | None = None
        self.allocation: StationAllocation | None = None
        self.needs_prepare = False
        self.needs_first_update = False
        self.settings: dict[str, Any] = {"mode": "off", "max_current": None, "phases": None}
        # last values seen while the station was attached: an unloaded entry is
        # reserved with them, not with zero
        self.last_setpoint = 0.0
        self.last_measured: tuple[float, float, float] | None = None
        self.last_phases = 3
        self.last_range = (6.0, 16.0)
        self.user_current: int | None = None  # station desired current (0.1 A) before it joined
        self.stored = False  # settings restored from the site Store (not a new member)
        self.suppress_limit = False  # prepare() of a charging member: no set_limit

    # --- state for the entities ---------------------------------------------------

    @property
    def title(self) -> str:
        rt = self.runtime
        return rt.serial if rt is not None else self.entry_id

    @property
    def mode(self) -> str:
        mode = self.settings.get("mode")
        return mode if mode in MODES else "off"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"invalid mode {mode}")
        self.settings["mode"] = mode
        self.stored = True  # the user's choice wins over the new-member default
        if self.loadpoint is not None:
            self.loadpoint.set_mode(mode)
        self.coord.save()
        self.coord.notify()

    def current_range(self) -> tuple[float, float]:
        rt = self.runtime
        if rt is None:
            return self.last_range
        low, high = rt.station.current_range_tenths()
        return low / 10, high / 10

    @property
    def max_current(self) -> float:
        low, high = self.current_range()
        value = self.settings.get("max_current")
        if not isinstance(value, (int, float)):
            return high
        return min(max(float(value), low), high)

    def set_max_current(self, value: float) -> None:
        low, high = self.current_range()
        value = round(min(max(float(value), low), high), 1)
        self.settings["max_current"] = value
        if self.loadpoint is not None:
            self.loadpoint.max_current = value
        self.coord.save()
        self.coord.notify()

    @property
    def can_auto(self) -> bool:
        return self.adapter is not None and api.PhaseSwitcher in self.adapter.capabilities

    @property
    def phase_option(self) -> str | None:
        if self.can_auto and self.loadpoint is not None:
            configured = self.loadpoint.phases_configured
            return PHASE_AUTO if configured == 0 else str(configured)
        return None

    async def async_select_phases(self, option: str) -> None:
        """``auto`` / ``1`` / ``3``: the loadpoint's phases_configured when the
        station can switch phases (evcc); otherwise the phase select writes as
        today (command layer, user origin, refused while charging) and the
        loadpoint follows the station's fixed phases."""
        if self.can_auto and self.loadpoint is not None:
            phases = 0 if option == PHASE_AUTO else int(option)
            try:
                self.loadpoint.set_phases_configured(phases)
            except ValueError as err:  # evcc's English text never reaches the user
                _LOGGER.info("%s: phases %s refused: %s", self.title, option, err)
                raise CommandError(KEY_PHASE_NA) from err
            self.settings["phases"] = phases
            self.coord.save()
            self.coord.notify()
            return
        if option == PHASE_AUTO:
            raise CommandError(KEY_PHASE_NA)  # translated, not an English ValueError
        await self.runtime.commander.async_set_phases(int(option))

    # --- inputs ---------------------------------------------------------------------

    def station_enabled(self) -> bool:
        """Charging, starting, our start sent and not seen yet, or a start queued."""
        rt = self.runtime
        if rt is None:
            return False
        if rt.commander.charging_switch_state or rt.station.start_pending:
            return True
        return self.adapter is not None and self.adapter.pending > 0 and self.adapter.enabled()

    def readback(self) -> float:
        """Reg 91 as last polled while the station is enabled, else 0."""
        if not self.station_enabled():
            return 0.0
        value = self.runtime.station.snapshot.setpoint_a
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0

    def setpoint(self) -> float:
        """The current the station may draw while enabled, else 0: the larger of
        the command layer's last sent / pending value (the poll lags our own
        write) and the read-back reg 91."""
        if not self.station_enabled():
            return 0.0
        desired = getattr(self.runtime.commander, "site_current", None)
        if desired is None:
            desired = self.runtime.station.desired.get("current")
        sent = desired / 10 if isinstance(desired, int) and not isinstance(desired, bool) else 0.0
        return max(sent, self.readback())


class SiteCoordinator:
    """See the module docstring."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: SiteConfig,
        *,
        store: Any = None,
        clock: Callable[[], float] | None = None,
        utcnow: Callable[[], datetime] | None = None,
        resolve: Callable[[str], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_stopped: Callable[[str], None] | None = None,
        on_member_removed: Callable[[str], None] | None = None,
        entry_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self._store = store
        self.clock: Callable[[], float] = clock or time.monotonic
        self._lp_clock = _Clock(self.clock)
        self._utcnow = utcnow or dt_util.utcnow
        self._resolve = resolve or self._default_resolve
        self._sleep = sleep
        self.on_stopped = on_stopped
        self.on_member_removed = on_member_removed
        self._entry_exists = entry_exists or (lambda eid: hass.config_entries.async_get_entry(eid) is not None)
        self._removed: set[str] = set()
        self.allocator = DlmAllocator(
            DlmConfig(
                fuse_limit_a=(config.fuse_limit_a,) * 3,
                max_total_power_w=config.max_total_power_w,
                meter_period_s=config.meter_period_s,
                meter_failure_action=config.meter_failure_action,
            ),
            clock=self.clock,
        )
        self.members = [SiteMember(self, m) for m in config.members]
        self.site: Site | None = None
        self._grid_meter = _GridMeter(self)
        self._pv_meters = [_PowerMeter(self, config.pv_power_entity)] if config.pv_power_entity else []
        self.result: DlmResult | None = None
        self.reading: GridReading | None = None
        self.cycle_currents: tuple[float, float, float] | None = None
        self._last_good: tuple[tuple[float, float, float], float] | None = None
        self.meter_error: str | None = None
        self.published: dict[str, Any] = {}
        self.pv_surplus_w: float | None = None
        self.spare: list[float] | None = None
        self.spare_power_w: float | None = None
        self.cycles = 0
        self._last_tick: float | None = None
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self.running = False
        self.stopped_reason: str | None = None

    # --- wiring ---------------------------------------------------------------------

    def _default_resolve(self, entry_id: str) -> Any:
        from ..modbus.runtime import modbus_data

        return modbus_data(self.hass)["entries"].get(entry_id)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # an entity must never break the cycle
                _LOGGER.exception("site entity update failed")
        for m in self.members:
            if m.runtime is not None:
                m.runtime.async_update()

    def member(self, station_entry_id: str) -> SiteMember | None:
        return next((m for m in self.members if m.entry_id == station_entry_id), None)

    # --- persistence ------------------------------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        data = await self._store.async_load() or {}
        stored = data.get("members") or {}
        for m in self.members:
            saved = stored.get(m.entry_id)
            if isinstance(saved, dict):
                m.settings.update({k: saved.get(k) for k in ("mode", "max_current", "phases") if k in saved})
                m.stored = True

    def _dump(self) -> dict[str, Any]:
        return {"members": {m.entry_id: dict(m.settings) for m in self.members}}

    def save(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self._dump, 1)

    # --- life cycle -------------------------------------------------------------------

    def start(self) -> None:
        self.running = True
        self._task = self.hass.async_create_background_task(self._run(), f"gnlt site {self.entry_id}")

    async def _run(self) -> None:
        try:
            while self.running:
                await self.async_cycle()
                await self._sleep(self.config.meter_period_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("GNLT site %s: coordinator crashed; members left at their minimum current", self.entry_id)
            self.running = False
            await self.async_failsafe()
            self.hold_at_minimum()
            self.stopped_reason = "crashed"
            if self.on_stopped is not None:
                self.on_stopped("crashed")
            self.notify()

    async def async_stop(self, reason: str = "unloaded") -> None:
        """Unload: the task ends, members are left at their minimum, then released."""
        self.running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._store is not None:
            try:
                await self._store.async_save(self._dump())  # flush the delayed save
            except Exception:  # noqa: BLE001 - the failsafe below must still run
                _LOGGER.exception("GNLT site %s: settings not saved", self.entry_id)
        if self.stopped_reason is None:
            await self.async_failsafe()
            self.detach_all()
            self.stopped_reason = reason
            if self.on_stopped is not None:
                self.on_stopped(reason)
        elif self.stopped_reason == "crashed":
            # the deliberate unload after a crash releases the members held at minimum
            for m in self.members:
                rt = self._resolve(m.entry_id)
                if rt is not None:
                    m.runtime = rt
                    self._release(m, deliberate=True)

    async def async_failsafe(self) -> None:
        """Every member at its station minimum current: no stop, no start."""
        for m in self.members:
            if m.adapter is not None:
                m.adapter.cancel_pending()
        for m in self.members:
            if m.adapter is not None:
                try:
                    await asyncio.wait_for(m.adapter.async_drain(), 30)
                except Exception:  # noqa: BLE001 - go on with the others
                    _LOGGER.warning("%s: site write still running at failsafe", m.title)
            rt = m.runtime
            if rt is None or not m.station_enabled():
                continue
            low = rt.station.current_range_tenths()[0] / 10
            # Always, whatever the polled reg 91 says: an increase may still be
            # pending in the command layer (the slot of a running sequence); it
            # replaces that, and the command layer skips a write equal to what the
            # station holds.
            try:
                await rt.commander.apply(Target(current_a=low), ORIGIN_SITE)
            except Exception as err:  # noqa: BLE001 - logged, the others go on
                _LOGGER.warning("%s: minimum current at site stop not written: %r", m.title, err)

    def _attach(self, m: SiteMember, rt: Any) -> None:
        desired = rt.station.desired.get("current")
        # the user's own current; site writes may replace the station's stored value
        m.user_current = desired if isinstance(desired, int) and not isinstance(desired, bool) else None
        m.runtime = rt
        m.adapter = GnltChargerAdapter(rt.station, rt.commander)
        m.loadpoint = None
        m.requested = None
        rt.site_options_filter = no_automatic_features
        rt.commander.options = rt.command_options()
        rt.commander.set_site_member(True)
        site_data(self.hass)["members"][m.entry_id] = m
        from .entities import async_ensure_member_entities

        async_ensure_member_entities(self.hass, rt)
        _LOGGER.info("GNLT site %s: station %s attached", self.entry_id, rt.serial)

    def attach_runtime(self, station_entry_id: str, rt: Any) -> None:
        """A member's station entry has just loaded (its platforms are being set up)."""
        m = self.member(station_entry_id)
        if m is None or m.runtime is rt:
            return
        if m.runtime is not None:
            self._release(m)
            self.site = None
        self._attach(m, rt)

    def _release(self, m: SiteMember, deliberate: bool = False) -> None:
        """Drop the member's runtime. ``deliberate`` (site unload / removal): the
        station gets its own control back (filter off, site membership and
        current cleared, the user's current restored). Otherwise (the station
        entry went away or reloaded) only the references are dropped: the
        persisted membership keeps restores and starts limited."""
        if not deliberate:
            members = site_data(self.hass)["members"]
            if members.get(m.entry_id) is m:
                members.pop(m.entry_id, None)
            m.runtime = None
            m.adapter = None
            m.loadpoint = None
            m.requested = None
            return
        rt = m.runtime
        members = site_data(self.hass)["members"]
        if members.get(m.entry_id) is m:
            members.pop(m.entry_id, None)
        if rt is not None:
            rt.commander.set_site_member(False)  # the user's desired current controls again
            if m.user_current is not None:
                # the user's chosen current is back on the station's controls;
                # the station itself keeps what the failsafe left (its minimum)
                rt.station.desired["current"] = m.user_current
                save = getattr(rt.station, "_schedule_save", None)
                if callable(save):
                    save()
            rt.site_options_filter = None
            try:
                rt.commander.options = rt.command_options()
            except Exception:  # noqa: BLE001 - the entry may be gone
                _LOGGER.debug("options of %s not restored", m.entry_id)
            rt.async_update()
        m.runtime = None
        m.adapter = None
        m.loadpoint = None
        m.requested = None

    def hold_at_minimum(self) -> None:
        """After a crash: not a release. Every attached member
        keeps the site options filter and its persisted membership, and the
        command layer holds it at its station minimum (site current = minimum):
        its own auto start / schedule / resume do not come back at full current
        without DLM. Only the coordinator's references are dropped."""
        for m in self.members:
            rt = m.runtime
            if rt is not None:
                rt.commander.set_site_current(rt.station.current_range_tenths()[0])
            self._release(m, deliberate=False)
        self.site = None

    def detach_all(self) -> None:
        for m in self.members:
            self._release(m, deliberate=True)
        self.site = None

    # --- loadpoints -------------------------------------------------------------------

    def _fixed_phases(self, m: SiteMember) -> int:
        rt = m.runtime
        if not rt.commander.options.phases_available:
            return 1
        phases = rt.station.snapshot.phases
        return phases if phases in (1, 3) else 3

    def _build_loadpoint(self, m: SiteMember) -> None:
        cfg = self.config
        adapter = m.adapter
        assert adapter is not None
        adapter.capabilities = adapter.compute_capabilities()
        low, _high = m.current_range()
        if not m.stored:
            # A new member: Off, so nothing starts by surprise - unless it is found
            # charging: then Now (still DLM-capped), so adding a site does not
            # stop a running charge.
            m.settings["mode"] = "now" if m.station_enabled() else "off"
            m.stored = True
            self.save()
        if api.PhaseSwitcher in adapter.capabilities:
            stored = m.settings.get("phases")
            phases = stored if stored in (0, 1, 3) else 0
        else:
            phases = self._fixed_phases(m)
        lp = new_loadpoint_from_config(
            adapter,
            clock=self._lp_clock,
            circuit=m.circuit,
            title=m.title,
            # evcc: a higher priority wins; DLM: priority 1 is served first
            priority=max(0, 100 - m.cfg.priority),
            min_current=low,
            max_current=m.max_current,
            phases_configured=phases,
            enable=ThresholdConfig(threshold=cfg.enable_threshold_w, delay=cfg.enable_delay_s),
            disable=ThresholdConfig(threshold=cfg.disable_threshold_w, delay=cfg.disable_delay_s),
        )
        lp.set_mode(ChargeMode(m.mode))
        original = lp.set_limit

        def set_limit(current: float, _orig: Callable[[float], None] = original) -> None:
            if m.suppress_limit:
                return
            m.requested = float(current)  # what the loadpoint wants, before the DLM cap
            _orig(current)

        lp.set_limit = set_limit  # type: ignore[method-assign]
        m.loadpoint = lp
        m.requested = None
        m.needs_prepare = True
        m.needs_first_update = True
        self.site = None  # rebuilt with the new loadpoint list

    def _prepare(self, m: SiteMember, lp: Loadpoint) -> None:
        """evcc Prepare. DEVIATION from evcc: evcc
        sets an enabled charger to its minimum current here ("defined current
        for PV mode"); a member found charging keeps its running current
        instead (offered = its present setpoint) - adding a site must not
        change a running charge. The DLM cap still applies from this cycle."""
        setpoint = m.setpoint()
        if setpoint <= 0:
            lp.prepare()
            return
        m.suppress_limit = True
        try:
            lp.prepare()
        finally:
            m.suppress_limit = False
        if lp.enabled:
            lp.offered_current = setpoint

    def _ensure_site(self) -> Site | None:
        lps = [m.loadpoint for m in self.members if m.loadpoint is not None]
        if not lps:
            self.site = None
            return None
        if self.site is None:
            self.site = Site(
                loadpoints=lps,
                grid_meter=self._grid_meter,
                pv_meters=self._pv_meters,
                publish_cb=self._publish,
            )
            for lp in lps:
                # a request queued in the old site's queue is gone with it
                lp._lp_queued = False
                lp.lp_chan = self.site.lp_update_chan
        return self.site

    def _publish(self, key: str, value: Any) -> None:
        self.published[key] = value

    # --- meter ------------------------------------------------------------------------

    def _state_value(self, entity_id: str, *, power: bool) -> float:
        return self._state_reading(entity_id, power=power)[0]

    def _state_reading(self, entity_id: str, *, power: bool) -> tuple[float, float]:
        """(value, age in s of its last report)."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _INVALID:
            raise ValueError(f"{entity_id}: {None if state is None else state.state}")
        value = float(state.state)
        if not math.isfinite(value):
            raise ValueError(f"{entity_id}: not finite")
        reported = getattr(state, "last_reported", None) or state.last_updated
        age = (self._utcnow() - reported).total_seconds()
        if age > STALE_FACTOR * self.config.meter_period_s:
            raise ValueError(f"{entity_id}: no update for {age:.0f} s")
        unit = state.attributes.get("unit_of_measurement")
        if power and unit == "kW":
            value *= 1000.0
        elif not power and unit == "mA":
            value /= 1000.0
        return value, max(age, 0.0)

    def read_power(self, entity_id: str) -> float:
        try:
            return self._state_value(entity_id, power=True)
        except (ValueError, TypeError) as err:
            raise api.NotAvailableError(str(err)) from err

    def _read_grid(self, now: float) -> GridReading | None:
        values: list[float] = []
        age = 0.0
        try:
            for entity_id in self.config.grid_entities:
                value, value_age = self._state_reading(entity_id, power=False)
                values.append(-value if self.config.grid_invert else value)
                age = max(age, value_age)
        except (ValueError, TypeError) as err:
            self.meter_error = str(err)
            self.cycle_currents = None
            if self._last_good is None:
                return None
            currents, at = self._last_good
            return GridReading(currents_a=currents, updated_at=at, error=True)
        currents = (values[0], values[1], values[2])
        self.meter_error = None
        self.cycle_currents = currents
        # the value's own age: read every cycle, but a value that was
        # not reported again is "not new" for the allocator (no increase)
        updated_at = now - age
        if self._last_good is not None and self._last_good[0] == currents:
            updated_at = max(updated_at, min(self._last_good[1], now))
        self._last_good = (currents, updated_at)
        return GridReading(currents_a=currents, updated_at=updated_at)

    # --- station inputs --------------------------------------------------------------

    def _station_input(self, m: SiteMember, now: float) -> StationInput:
        cfg = m.cfg
        rt = m.runtime
        base: dict[str, Any] = {"station_id": m.entry_id, "priority": cfg.priority, "phase_1": cfg.phase_1}
        if rt is None:
            low, high = m.current_range()
            m.grid_phases = grid_phases(cfg.phase_1, m.last_phases)
            return StationInput(
                **base,
                phases_in_use=m.last_phases,
                min_current_a=low,
                max_current_a=high,
                setpoint_a=m.last_setpoint,
                measured_a=m.last_measured,  # type: ignore[arg-type]
                online=False,
                last_seen=m.last_seen,
                hardware_max_a=high,
            )
        st = rt.station
        snap = st.snapshot
        low, high = m.current_range()
        if snap.time is not None:
            m.last_seen = now - max(st.clock() - snap.time, 0.0)
        online = snap.link == MC.LINK_CONNECTED and snap.identity == MC.IDENTITY_OK
        measured = None
        if snap.measurements_available:
            measured = tuple(float(c) if c is not None else 0.0 for c in snap.currents)
        setpoint = m.setpoint()
        if setpoint <= 0:
            m.charging_since = None
        elif m.charging_since is None:
            # first non-zero setpoint seen (our start, or one we found running)
            m.charging_since = now
        lp = m.loadpoint
        min_a = max(lp.effective_min_current(), low) if lp is not None else low
        max_a = min(lp.effective_max_current(), high) if lp is not None else high
        max_a = max(max_a, min_a)
        if m.requested is None:
            requested = setpoint
        else:
            requested = m.requested if m.requested >= min_a - 1e-9 else 0.0
        if rt.commander.options.phases_available:
            phases = snap.phases if snap.phases in (1, 3) else 3
        else:
            phases = 1
        if setpoint > 0 and self._draw_not_measured_yet(m, measured):
            # The meter sees a new draw before the station's next poll does (and
            # the start sequence holds the polls): its setpoint counts as its
            # draw, so it is not taken for base load (start/stop flapping).
            local = measured or (0.0, 0.0, 0.0)
            measured = tuple(max(local[k], setpoint) if k < phases else local[k] for k in range(3))
        extra = {"hardware_max_a": high}
        m.grid_phases = grid_phases(cfg.phase_1, phases)
        if snap.time is not None:
            m.last_setpoint, m.last_phases, m.last_range = setpoint, phases, (low, high)
            if measured is not None:
                m.last_measured = (measured[0], measured[1], measured[2])
        return StationInput(
            **base,
            phases_in_use=phases,
            min_current_a=min_a,
            max_current_a=max_a,
            requested_a=max(min(requested, max_a), 0.0),
            setpoint_a=setpoint,
            measured_a=measured,  # type: ignore[arg-type]
            charging_since=m.charging_since,
            online=online,
            last_seen=m.last_seen,
            **extra,
        )

    def _draw_not_measured_yet(self, m: SiteMember, measured: tuple[float, ...] | None) -> bool:
        rt = m.runtime
        st = rt.station
        if st.snapshot.status == MC.STATUS_STARTING or st.start_pending:
            return True
        started = m.adapter.started_at if m.adapter is not None else None
        if started is not None and st.clock() - started < START_SETTLE_S:
            return True
        return measured is None or max(measured) < 0.5  # enabled with a setpoint, nothing measured

    # --- the cycle --------------------------------------------------------------------

    async def async_cycle(self) -> None:
        now = self.clock()
        self.cycles += 1
        # members: (re)attach runtimes, build loadpoints once the station has polled
        for m in self.members:
            if m.entry_id in self._removed:
                continue
            if not self._entry_exists(m.entry_id):
                # the station's config entry was deleted: no station any more, nothing
                # reserved for it; the site options drop it
                self._removed.add(m.entry_id)
                if m.runtime is not None:
                    self._release(m)
                    self.site = None
                m.allocation = None
                m.circuit.allowed = None
                if self.on_member_removed is not None:
                    self.on_member_removed(m.entry_id)
                continue
            rt = self._resolve(m.entry_id)
            if rt is not m.runtime:
                if m.runtime is not None:
                    self._release(m)
                    self.site = None
                if rt is not None:
                    self._attach(m, rt)
            if m.runtime is None:
                continue
            if m.loadpoint is None:
                if m.runtime.station.snapshot.time is not None:
                    self._build_loadpoint(m)
            elif m.adapter is not None and m.adapter.compute_capabilities() != m.adapter.capabilities:
                _LOGGER.info("%s: station capabilities changed, loadpoint rebuilt", m.title)
                self._build_loadpoint(m)
            elif api.PhaseSwitcher not in m.adapter.capabilities:
                fixed = self._fixed_phases(m)
                if m.loadpoint.phases_configured != fixed:
                    m.loadpoint._set_phases_configured(fixed)

        # a. inputs, b. allocation and caps
        self.reading = self._read_grid(now)
        inputs = [self._station_input(m, now) for m in self.members if m.entry_id not in self._removed]
        self.result = self.allocator.allocate(self.reading, inputs)
        for m in self.members:
            if m.entry_id in self._removed:
                continue
            m.allocation = self.result.stations.get(m.entry_id)
            m.circuit.allowed = m.allocation.allowed_a if m.allocation is not None else None
            m.spare_granted = 0.0
        if self.result.meter_state is MeterState.OK:
            self.spare = [max(h, 0.0) for h in self.result.headroom_left_a]
            self.spare_power_w = self.result.power_left_w
        else:
            self.spare = None  # no increase beyond the allocation without a fresh meter value
            self.spare_power_w = None

        # c. DLM decreases at once, every member
        for m in self.members:
            self._enforce(m)

        # d. evcc site: new loadpoints once, else one loadpoint round-robin
        for m in self.members:
            if m.needs_prepare and m.loadpoint is not None:
                m.needs_prepare = False
                self._prepare(m, m.loadpoint)
        site = self._ensure_site()
        if site is not None:
            fresh = [m for m in self.members if m.needs_first_update and m.loadpoint is not None]
            if fresh:
                for m in fresh:
                    m.needs_first_update = False
                    site.update(m.loadpoint)
                if self._last_tick is None:
                    self._last_tick = now  # evcc Run: start-up, then the ticker
            elif self._last_tick is None or now - self._last_tick >= EVCC_INTERVAL_S - 1e-6:
                self._last_tick = now
                site.tick()
            site.handle_update_requests()
            state = site.state()
            total = sum(lp.get_charge_power() for lp in site.active_loadpoints())
            self.pv_surplus_w = max(0.0, total - state.grid_power)
        self.notify()

    def grant_spare(self, m: SiteMember, want: float) -> float:
        """Current above its allocation a member may take this cycle (see DlmCircuit)."""
        if self.spare is None or want <= 0:
            return 0.0
        others = [o for o in self.members if o is not m and o.spare_granted > 0]
        if others:
            # one member per cycle: the meter sees a new draw before the station's
            # next poll does, so two increases in one cycle would both be counted
            # as base load in the next allocation
            return 0.0
        free = []
        for p in m.grid_phases:
            free.append(self.spare[p] - sum(o.spare_granted for o in others if p in o.grid_phases))
        grant = min([want, *free])
        if self.spare_power_w is not None:
            used = sum(o.spare_granted * len(o.grid_phases) for o in others) * VOLTAGE
            grant = min(grant, (self.spare_power_w - used) / (VOLTAGE * max(len(m.grid_phases), 1)))
        grant = max(math.floor(grant * 10 + 1e-6) / 10, 0.0)
        m.spare_granted = grant
        return grant

    def _write_current(self, m: SiteMember, amps: float) -> None:
        if api.ChargerEx in m.adapter.capabilities:
            m.adapter.max_current_millis(amps)
        else:
            m.adapter.max_current(int(amps))

    def _pause(self, m: SiteMember, lp: Loadpoint | None, setpoint: float) -> None:
        """Allowed below the minimum: the station minimum FIRST, then disable.

        A station whose stop is refused (Plug & Charge on) then stays at its
        minimum instead of at its old current."""
        low = m.current_range()[0]
        offered = lp.offered_current if lp is not None else 0.0
        if max(setpoint, offered) > low + 1e-6:
            self._write_current(m, low)
            if lp is not None:
                lp.offered_current = low
        if lp is not None:
            # the loadpoint's own disable path (evcc setLimit below the minimum)
            Loadpoint.set_limit(lp, low)
        else:
            m.adapter.enable(False)

    def _enforce(self, m: SiteMember) -> None:
        allowed = m.circuit.allowed
        if allowed is None or m.adapter is None or m.runtime is None:
            return
        lp = m.loadpoint
        try:
            if lp is not None and lp.enabled:
                low = lp.effective_min_current()
                if allowed < low - 1e-9:
                    self._pause(m, lp, m.setpoint())
                elif lp.offered_current > allowed + _STEP_EPS:
                    # the loadpoint's own path: the circuit caps the offered current (evcc setLimit)
                    Loadpoint.set_limit(lp, lp.offered_current)
                elif m.readback() > allowed + _STEP_EPS:
                    # the station holds more than we sent (a write was lost): send again
                    self._write_current(m, allowed)
                    lp.offered_current = min(lp.offered_current, allowed)
                return
            setpoint = m.setpoint()
            if setpoint <= 0:
                return
            low = m.current_range()[0]
            if allowed < low - 1e-9:
                self._pause(m, None, setpoint)
            elif setpoint > allowed + _STEP_EPS:
                self._write_current(m, allowed)
        except WriteRefused as err:
            # What the write gate refuses (link, identity) refuses a stop too, so
            # there is no fallback here: the refusal is reported.
            _LOGGER.warning("%s: DLM limit %.1f A not applied: %s", m.title, allowed, err.key)
        except Exception as err:  # noqa: BLE001 - e.g. A3B: stop refused; the allocator books its draw
            _LOGGER.warning("%s: DLM limit %.1f A not applied: %r", m.title, allowed, err)
