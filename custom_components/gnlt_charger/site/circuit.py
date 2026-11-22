"""Circuits: hierarchical current and power limits (no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Source: evcc ``core/circuit/circuit.go`` (evcc's own implementation of the
``api.Circuit`` interface, see:class:`gnlt_charger.site.api.Circuit`).

A circuit limits the largest phase current (``max_current``, A) and/or the
total power (``max_power``, W) of everything below it. Its load is measured
by an optional meter (power, and phase currents when ``max_current`` is set)
or, without a meter, summed from the loadpoints attached to it and its child
circuits. Loadpoints ask:meth:`Circuit.validate_current` /
meth:`Circuit.validate_power` before raising their demand; the request is
capped at the remaining headroom of this circuit and of every parent.

Excluded: HEMS consumption cap (``SetHEMS`` / ``EffectiveMaxPower`` returns
the configured max power), plugin-based dynamic limits (plain callables
here), config registry, Modbus retry/backoff around meter reads.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from . import api
from .clock import ZERO_TIME, Clock, SystemClock, since

_LOGGER = logging.getLogger(__name__)

MAX_FLOAT = sys.float_info.max  # Go math.MaxFloat64


class CircuitError(Exception):
    """Joined errors of a circuit update (evcc returns errors.Join)."""


class Circuit:
    """evcc core/circuit/circuit.go: Circuit."""

    def __init__(
        self,
        *,
        title: str = "",
        max_current: float = 0.0,
        max_power: float = 0.0,
        meter: Any = None,
        timeout: float = 60.0,
        get_max_current: Callable[[], float] | None = None,
        get_max_power: Callable[[], float] | None = None,
        clock: Clock | None = None,
    ) -> None:
        """evcc core/circuit/circuit.go: New (+ the dynamic getters of NewConfigurableFromConfig).

        ``timeout`` (s, evcc default 1 min): a meter failing longer than this
        counts as overload. 0 disables it.
        """
        self.title = title
        self.parent: Circuit | None = None
        self.children: list[Circuit] = []
        self.meter = meter
        self.timeout = timeout
        self.clock: Clock = clock if clock is not None else SystemClock()

        self.max_current = max_current
        self.max_power = max_power
        self.get_max_current_fn = get_max_current
        self.get_max_power_fn = get_max_power

        self.current = 0.0
        self.power = 0.0

        self.current_updated = ZERO_TIME
        self.power_updated = ZERO_TIME

        if max_power == 0:
            _LOGGER.debug("validation of max power disabled")

        if max_current == 0:
            _LOGGER.debug("validation of max phase current disabled")
        elif meter is not None and not api.has_cap(meter, api.PhaseCurrents):
            raise ValueError("meter does not support phase currents")

    def get_title(self) -> str:
        """evcc core/circuit/circuit.go: GetTitle."""
        return self.title

    def set_title(self, title: str) -> None:
        """evcc core/circuit/circuit.go: SetTitle."""
        self.title = title

    def get_parent(self) -> Circuit | None:
        """evcc core/circuit/circuit.go: GetParent."""
        return self.parent

    def set_parent(self, parent: Circuit) -> None:
        """evcc core/circuit/circuit.go: setParent."""
        # prevent cyclical dependency
        p = parent.get_parent()
        while p is not None:
            if p is self:
                raise ValueError(
                    f"cycle detected: {self.get_title()} and {parent.get_title()} cannot be mutual parents"
                )
            p = p.get_parent()
        if self.parent is not None:
            raise ValueError("circuit already has a parent")
        self.parent = parent
        parent.register_child(self)

    def has_meter(self) -> bool:
        """evcc core/circuit/circuit.go: HasMeter."""
        return self.meter is not None

    def get_max_power(self) -> float:
        """evcc core/circuit/circuit.go: GetMaxPower (dynamic getter first, configured value on error)."""
        if self.get_max_power_fn is not None:
            try:
                return self.get_max_power_fn()
            except Exception as err:  # noqa: BLE001 - evcc warns and falls back
                _LOGGER.warning("get max power: %s", err)
        return self.max_power

    def effective_max_power(self) -> float:
        """evcc core/circuit/circuit.go: EffectiveMaxPower (HEMS clamp excluded)."""
        return self.get_max_power()

    def set_max_power(self, power: float) -> None:
        """evcc core/circuit/circuit.go: SetMaxPower."""
        self.max_power = power

    def get_max_current(self) -> float:
        """evcc core/circuit/circuit.go: GetMaxCurrent (dynamic getter first, configured value on error)."""
        if self.get_max_current_fn is not None:
            try:
                return self.get_max_current_fn()
            except Exception as err:  # noqa: BLE001 - evcc warns and falls back
                _LOGGER.warning("get max current: %s", err)
        return self.max_current

    def set_max_current(self, current: float) -> None:
        """evcc core/circuit/circuit.go: SetMaxCurrent."""
        self.max_current = current

    def register_child(self, child: Circuit) -> None:
        """evcc core/circuit/circuit.go: RegisterChild."""
        self.children.append(child)

    def _update_loadpoints(self, loadpoints: list[Any]) -> None:
        """evcc core/circuit/circuit.go: updateLoadpoints (sum of the loadpoints on this circuit)."""
        self.power = 0.0
        self.current = 0.0
        for lp in loadpoints:
            if lp.get_circuit() is not self:
                continue
            self.power += lp.get_charge_power()
            self.current += lp.get_max_phase_current()

    def _overload_on_error(self, updated: float) -> bool:
        """evcc core/circuit/circuit.go: overloadOnError (True: treat the value as overload)."""
        return self.timeout > 0 and since(self.clock, updated) > self.timeout

    def _update_meters(self) -> None:
        """evcc core/circuit/circuit.go: updateMeters (no retry/backoff)."""
        try:
            power = self.meter.current_power()
        except Exception as err:
            if self._overload_on_error(self.power_updated):
                self.power = MAX_FLOAT
            raise CircuitError(f"circuit power: {err}") from err
        self.power = power
        self.power_updated = self.clock.now()

        if api.has_cap(self.meter, api.PhaseCurrents):
            try:
                i1, i2, i3 = self.meter.currents()
            except Exception as err:
                if self._overload_on_error(self.current_updated):
                    self.current = MAX_FLOAT
                raise CircuitError(f"circuit currents: {err}") from err

            p1 = p2 = p3 = 0.0
            powers_err: BaseException | None = None
            if api.has_cap(self.meter, api.PhasePowers):
                # phases needed for signed currents, unsigned currents overestimate load which is safe
                try:
                    p1, p2, p3 = self.meter.powers()
                except Exception as err:  # noqa: BLE001 - reported after the update
                    p1 = p2 = p3 = 0.0
                    powers_err = err

            self.current = max(
                api.sign_from_power(i1, p1), api.sign_from_power(i2, p2), api.sign_from_power(i3, p3)
            )
            self.current_updated = self.clock.now()

            if powers_err is not None:
                raise CircuitError(f"circuit powers: {powers_err}") from powers_err

    def update(self, loadpoints: list[Any]) -> None:
        """evcc core/circuit/circuit.go: Update.

        Children first (depth-first); a failing child does not leave this
        circuit stale. Raises:class:`CircuitError` with all errors joined.
        """
        max_power = self.get_max_power()
        max_current = self.get_max_current()
        errors: list[str] = []

        try:
            # update children depth-first, a failing child must not leave this circuit stale
            for child in self.children:
                try:
                    child.update(loadpoints)
                except Exception as err:  # noqa: BLE001 - joined below
                    errors.append(str(err))

            if self.meter is not None:
                # meter available
                try:
                    self._update_meters()
                except Exception as err:  # noqa: BLE001 - joined below
                    errors.append(str(err))
            else:
                # no meter available
                self._update_loadpoints(loadpoints)
                for child in self.children:
                    self.power += child.get_charge_power()
                    self.current += child.get_max_phase_current()
        finally:
            if max_power != 0 and self.power > max_power:
                _LOGGER.warning("%s: over power detected: %.0fW > %.0fW", self.title, self.power, max_power)
            if max_current != 0 and self.current > max_current:
                _LOGGER.warning("%s: over current detected: %.3gA > %.3gA", self.title, self.current, max_current)

        if errors:
            raise CircuitError("\n".join(errors))

    def get_charge_power(self) -> float:
        """evcc core/circuit/circuit.go: GetChargePower (actual power)."""
        return self.power

    def get_max_phase_current(self) -> float:
        """evcc core/circuit/circuit.go: GetMaxPhaseCurrent (actual current)."""
        return self.current

    def validate_power(self, old: float, new: float) -> float:
        """evcc core/circuit/circuit.go: ValidatePower.

        A load wants to go from ``old`` to ``new`` W; returns what it may use
        here and in every parent circuit.
        """
        max_power = self.effective_max_power()
        if max_power != 0:
            delta = max(0.0, new - old)
            potential = max_power - self.power
            if delta > potential:
                capped = min(new, max(0.0, old + potential))
                _LOGGER.debug(
                    "validate power: %.0fW + (%.0fW -> %.0fW) > %.0fW capped at %.0fW",
                    self.power,
                    old,
                    new,
                    max_power,
                    capped,
                )
                new = capped

        if self.parent is None:
            return new
        return self.parent.validate_power(old, new)

    def validate_current(self, old: float, new: float) -> float:
        """evcc core/circuit/circuit.go: ValidateCurrent.

        A load wants to go from ``old`` to ``new`` A per phase; returns what it
        may use here and in every parent circuit.
        """
        max_current = self.get_max_current()
        if max_current != 0:
            delta = max(0.0, new - old)
            potential = max_current - self.current
            if delta > potential:
                capped = min(new, max(0.0, old + potential))
                _LOGGER.debug(
                    "validate current: %.3gA + (%.3gA -> %.3gA) > %.3gA capped at %.3gA",
                    self.current,
                    old,
                    new,
                    max_current,
                    capped,
                )
                new = capped

        if self.parent is None:
            return new
        return self.parent.validate_current(old, new)
