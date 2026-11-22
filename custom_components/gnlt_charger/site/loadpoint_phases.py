"""Loadpoint phase bookkeeping (no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Source: evcc ``core/loadpoint_phases.go`` (mixin of:class:`.loadpoint.Loadpoint`).
Vehicles are excluded, so the vehicle phase count is always 0 (unknown).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import api

if TYPE_CHECKING:
    from .loadpoint import Loadpoint

_LOGGER = logging.getLogger(__name__)

UNKNOWN_PHASES = 3  # evcc: assume 3p for switchable charger during startup


def expect(phases: int) -> int:
    """evcc core/loadpoint_phases.go: expect."""
    if phases > 0:
        return phases
    return UNKNOWN_PHASES


class LoadpointPhases:
    """evcc core/loadpoint_phases.go."""

    def _set_phases_configured(self: Loadpoint, phases: int) -> None:
        """evcc core/loadpoint_phases.go: setPhasesConfigured."""
        self.phases_configured = phases
        self.publish("phasesConfigured", phases)
        # configured phases are actual phases for non-1p3p charger
        if not self.has_phase_switching():
            self.set_phases(phases)

    def set_phases(self: Loadpoint, phases: int) -> None:
        """evcc core/loadpoint_phases.go: SetPhases / setPhases."""
        if self.phases != phases:
            self.phases = phases
            # reset timer to disabled state
            self.reset_phase_timer()
            # measure phases after switching
            self.reset_measured_phases()

    def reset_measured_phases(self: Loadpoint) -> None:
        """evcc core/loadpoint_phases.go: ResetMeasuredPhases / resetMeasuredPhases."""
        self.measured_phases = 0
        self.publish("phasesActive", self.active_phases())

    def get_measured_phases(self: Loadpoint) -> int:
        """evcc core/loadpoint_phases.go: GetMeasuredPhases."""
        return self.measured_phases

    def active_phases(self: Loadpoint) -> int:
        """evcc core/loadpoint_phases.go: ActivePhases / activePhases.

        Expectedly active phases; unknown phases of a 1p3p charger count as 3p.
        """
        physical = self.phases
        vehicle = self.get_vehicle_phases()
        measured = self.get_measured_phases()
        charger = self.get_charger_physical_phases()

        active = min(expect(vehicle), expect(physical), expect(measured), expect(charger))

        # sanity check - we should not assume less active phases than actually measured
        if measured > 0 and active < measured:
            _LOGGER.warning(
                "phase mismatch between %dp measured for %dp vehicle and %dp charger", measured, vehicle, physical
            )
        return active

    def min_active_phases(self: Loadpoint) -> int:
        """evcc core/loadpoint_phases.go: MinActivePhases / minActivePhases."""
        # configured phases are both minimum and maximum, only automatic scales down to 1p
        if self.has_phase_switching() and self.phases_configured == 0:
            return 1
        return self.max_active_phases()

    def max_active_phases(self: Loadpoint) -> int:
        """evcc core/loadpoint_phases.go: MaxActivePhases / maxActivePhases."""
        physical = self.phases
        measured = self.get_measured_phases()
        vehicle = self.get_vehicle_phases()
        charger = self.get_charger_physical_phases()

        # during 1p or unknown config, 1p measured is not a restriction
        if physical <= 1 or vehicle == 1 or charger == 1:
            measured = 0

        # if 1p3p supported then assume configured limit or 3p
        if self.has_phase_switching():
            physical = self.phases_configured

        return min(expect(vehicle), expect(physical), expect(measured), expect(charger))

    def get_vehicle_phases(self: Loadpoint) -> int:
        """evcc core/loadpoint_phases.go: getVehiclePhases (vehicles excluded: unknown)."""
        return 0

    def get_charger_physical_phases(self: Loadpoint) -> int:
        """evcc core/loadpoint_phases.go: getChargerPhysicalPhases."""
        if api.has_cap(self.charger, api.PhaseDescriber):
            return self.charger.phases()
        return 0

    def has_phase_switching(self: Loadpoint) -> bool:
        """evcc core/loadpoint_phases.go: hasPhaseSwitching."""
        return api.has_cap(self.charger, api.PhaseSwitcher)

    def sync_charger_phases(self: Loadpoint, disabled: bool) -> None:
        """evcc core/loadpoint_phases.go: syncChargerPhases.

        Synchronizes the assumed phase state with the charger's actual state.
        Unknown phases (0, i.e. after startup) are seeded without warning.
        """
        phases = self.get_phases()
        if not self.has_phase_switching():
            return

        if api.has_cap(self.charger, api.PhaseGetter):
            try:
                charger_phases = self.charger.get_phases()
            except api.NotAvailableError:
                return
            except Exception as err:
                raise RuntimeError(f"charger get phases: {err}") from err

            if charger_phases > 0 and charger_phases != phases:
                # a disabled charger may switch phases without the loadpoint
                if phases > 0 and not disabled:
                    _LOGGER.warning(
                        "charger logic error: phases mismatch (got %d, expected %d)", charger_phases, phases
                    )
                self.set_phases(charger_phases)
            return

        # measured phases are stale while disabled, keep the enabled-only fallback
        if disabled:
            return

        # use measured phase currents for active phases as fallback if charger does not provide phases
        charger_phases = self.get_measured_phases()
        if charger_phases == 2:
            charger_phases = 3

        # 1p measured does not confirm 1p enabled (1p vehicle on 3p), hence never seeds unknown phases
        if charger_phases > max(phases, 1):
            if phases > 0:
                _LOGGER.warning(
                    "charger logic error: phases mismatch (got %d measured, expected %d)", charger_phases, phases
                )
            self.set_phases(charger_phases)
