"""evcc charger interface of one GNLT ModBus station (Home Assistant side of the site layer).

Implements the:class:`gnlt_charger.site.api.Charger` protocol on top of the
existing station (``modbus/station.py``) and command layer
(``modbus/commands.py``). The loadpoint (evcc port, ``site/loadpoint.py``)
calls it synchronously:

* reads come from the latest station snapshot (the station polls by itself);
* writes are queued and executed in order, one at a time, as an asyncio task
  on the Home Assistant loop through ``Commander.apply`` with the automatic
  origin ``ORIGIN_SITE``. The adapter merges nothing (every loadpoint call is
  one ``apply``). Site writes are exempt from the 10 s coalescing of reg 91,
  like evcc which has none; the write gate (link, identity) and the
  start rate limit stay.
  Site currents never replace the user's stored current (Commander.site_current).
  A refusal inside the task is logged and kept in ``last_error``: like an evcc
  charger error the loadpoint sees the result through ``enabled()`` /
  ``status()`` in a later cycle.

Capabilities are declared explicitly (``site/api.py`` contract):

* ``ChargerEx`` (0.1 A) only when the 6.1 A probe found the decimal firmware
  class (learned ``decimal_current`` = yes);
* ``PhaseGetter`` and ``PhaseSwitcher`` when the station has a phase select
  (3-phase preset, reg 200 readable), like evcc's initialize (no option, not tied to the firmware class);
* ``Meter``, ``PhaseCurrents`` and ``PhaseVoltages`` always, like the EM2GO
  driver.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from ..modbus import const as C
from ..modbus.commands import ORIGIN_SITE, Target
from ..modbus.station import WriteRefused
from . import api
from .api import ChargeStatus

_LOGGER = logging.getLogger(__name__)

# station status (reg 0) -> IEC 61851 status; anything else raises (cycle skipped)
STATUS_MAP = {
    C.STATUS_STANDBY: ChargeStatus.A,
    C.STATUS_CONNECTED: ChargeStatus.B,
    C.STATUS_STARTING: ChargeStatus.B,
    C.STATUS_FINISHED: ChargeStatus.B,
    C.STATUS_CHARGING: ChargeStatus.C,
}


class GnltChargerAdapter:
    """The evcc ``api.Charger`` of one station (see the module docstring)."""

    def __init__(self, station: Any, commander: Any) -> None:
        self.station = station
        self.commander = commander
        self.capabilities: frozenset[type] = self.compute_capabilities()
        self._queue: list[Target] = []
        self._worker: asyncio.Task | None = None
        self._in_flight: Target | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None  # station clock: our last start went out
        self.sent: list[Target] = []  # every target handed to the command layer (diagnostics)

    # --- capabilities -----------------------------------------------------------------

    def compute_capabilities(self) -> frozenset[type]:
        """The capability set the station offers now (the coordinator rebuilds
        the loadpoint when it changes, e.g. after the 6.1 A probe)."""
        caps: set[type] = {api.Meter, api.PhaseCurrents, api.PhaseVoltages}
        if self.station.learned["decimal_current"] == "yes":
            caps.add(api.ChargerEx)
        if self.commander.phase_select_available:
            # evcc: the phase switcher whenever the 200 read succeeded
            # (brief item 6); while the firmware class is unknown
            # the command layer refuses a switch under current.
            caps.add(api.PhaseGetter)
            caps.add(api.PhaseSwitcher)
        return frozenset(caps)

    # --- reads ----------------------------------------------------------------------

    def status(self) -> ChargeStatus:
        code = self.station.snapshot.status
        status = STATUS_MAP.get(code)
        if status is None:
            raise api.NotAvailableError(f"station status {code} has no IEC 61851 state")
        return status

    def enabled(self) -> bool:
        """evcc Enabled() of the EM2GO driver: reg 95 == 1 (polled every cycle, our own acknowledged write shown at once); a
        queued or running enable/disable is reported as its target until it
        is done."""
        for target in reversed(self._queue):
            if target.enabled is not None:
                return target.enabled
        if self._in_flight is not None and self._in_flight.enabled is not None:
            return self._in_flight.enabled
        return self.station.snapshot.last_command == C.COMMAND_START

    def get_phases(self) -> int:
        phases = self.station.snapshot.phases
        return phases if phases in (1, 3) else 0

    def current_power(self) -> float:
        snap = self.station.snapshot
        if not snap.measurements_available or snap.power_w is None:
            raise api.NotAvailableError("no measurement")
        return float(snap.power_w)

    def currents(self) -> tuple[float, float, float]:
        snap = self.station.snapshot
        if not snap.measurements_available:
            raise api.NotAvailableError("no measurement")
        i1, i2, i3 = (float(c) if c is not None else 0.0 for c in snap.currents)
        return (i1, i2, i3)

    def voltages(self) -> tuple[float, float, float]:
        """Regs 109/111/113 as polled (EM2GO driver Voltages)."""
        snap = self.station.snapshot
        if not snap.measurements_available:
            raise api.NotAvailableError("no measurement")
        u1, u2, u3 = (float(u) if u is not None else 0.0 for u in snap.voltages)
        return (u1, u2, u3)

    # --- writes ---------------------------------------------------------------------

    def enable(self, enable: bool) -> None:
        if enable:
            self._check(C.REG_COMMAND, C.COMMAND_START)
        self._push(Target(enabled=bool(enable)))

    def max_current(self, current: int) -> None:
        self._set_current(float(int(current)))

    def max_current_millis(self, current: float) -> None:
        self._set_current(float(current))

    def phases1p3p(self, phases: int) -> None:
        if phases not in (1, 3):
            raise ValueError(f"invalid phases {phases}")
        try:
            # the command layer's synchronous checks at once: a
            # refusal is evcc's ErrNotAvailable (scalePhases adopts the configured phases)
            self.commander._validate_phases(phases, ORIGIN_SITE)
        except WriteRefused as err:
            self.last_error = err.key
            raise api.NotAvailableError(err.key) from err
        self._push(Target(phases=phases))

    def _set_current(self, amps: float) -> None:
        # floored to the station's 0.1 A step: never above what the loadpoint asked
        # for
        amps = math.floor(amps * 10 + 1e-6) / 10
        # Range and gate checks at once, like a driver error.
        try:
            self.commander.validate_current(amps, ORIGIN_SITE)
        except WriteRefused as err:
            self.last_error = err.key
            raise
        self._push(Target(current_a=amps))

    def _check(self, register: int, value: int) -> None:
        """The station's synchronous write gate (link, identity)."""
        try:
            self.station.check_write(register, value, ORIGIN_SITE)
        except WriteRefused as err:
            self.last_error = err.key
            raise

    # --- queue ----------------------------------------------------------------------

    @property
    def pending(self) -> int:
        return len(self._queue) + (1 if self._in_flight is not None else 0)

    def _push(self, target: Target) -> None:
        self._queue.append(target)
        if self._worker is None or self._worker.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._queue.remove(target)
                raise api.NotAvailableError("no event loop") from None
            self._worker = loop.create_task(self._run())

    async def _run(self) -> None:
        while self._queue:
            target = self._queue.pop(0)
            self._in_flight = target
            try:
                self.sent.append(target)
                await self.commander.apply(target, ORIGIN_SITE)
                self.last_error = None
                if target.enabled is True:
                    self.started_at = self.station.clock()
            except WriteRefused as err:
                self.last_error = err.key
                _LOGGER.info("%s: site write %s not done: %s", self.station.serial, target, err.key)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - a broken link etc.: logged like a driver error
                self.last_error = type(err).__name__
                _LOGGER.warning("%s: site write %s failed: %r", self.station.serial, target, err)
            finally:
                self._in_flight = None

    async def async_drain(self) -> None:
        """Wait until every queued write was handed to the command layer."""
        while self._worker is not None and not self._worker.done():
            await asyncio.wait({self._worker})

    def cancel_pending(self) -> None:
        """Drop queued writes that have not started (the running one completes)."""
        self._queue.clear()
