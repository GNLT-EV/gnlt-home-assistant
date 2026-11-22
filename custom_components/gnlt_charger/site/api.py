"""Types and device interfaces of the site control core (no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Sources: evcc ``api/api.go`` (Charger, ChargerEx, PhaseSwitcher, PhaseGetter,
CurrentGetter, CurrentLimiter, ConnectionTimer, PhaseDescriber, Meter, PhaseCurrents,
PhaseVoltages, PhasePowers, Circuit, CircuitLoad), ``api/chargemodestatus.go``
(ChargeMode, AlwaysCharge, ChargeStatus), ``api/error.go`` (ErrNotAvailable,
ErrAsleep, ErrMustRetry), ``core/loadpoint/error.go`` (AcceptableError).

evcc discovers optional device capabilities with ``api.Cap[T](device)``; here
a device declares them in its ``capabilities`` attribute (a set of the
protocol classes below) and:func:`has_cap` checks the declaration. Go
errors become Python exceptions.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class ChargeMode(StrEnum):
    """evcc ``api.ChargeMode`` (api/chargemodestatus.go).

    evcc master keeps three modes (off, now, smart); the former ``pv`` and
    ``minpv`` are accepted on input and normalized to ``smart`` plus an
    class:`AlwaysCharge` state, exactly as:meth:`normalize` does.
    """

    EMPTY = ""
    OFF = "off"
    NOW = "now"
    SMART = "smart"
    # deprecated in evcc, accepted on input only, normalized to smart
    MINPV = "minpv"
    PV = "pv"

    def normalize(self) -> tuple[ChargeMode, AlwaysCharge | None]:
        """evcc api/chargemodestatus.go: ChargeMode.Normalize.

        ``None`` stands for evcc's empty AlwaysCharge ("leave unchanged").
        """
        if self is ChargeMode.MINPV:
            return ChargeMode.SMART, AlwaysCharge.ON
        if self is ChargeMode.PV:
            return ChargeMode.SMART, AlwaysCharge.OFF
        return self, None


class AlwaysCharge(StrEnum):
    """evcc ``api.AlwaysCharge``: smart mode charges at least at min power."""

    UNSET = ""  # Go zero value of the string type
    OFF = "off"
    ON = "on"
    ONCE = "once"  # resets on disconnect

    def active(self) -> bool:
        """evcc api/chargemodestatus.go: AlwaysCharge.Active."""
        return self in (AlwaysCharge.ON, AlwaysCharge.ONCE)


class ChargeStatus(StrEnum):
    """evcc ``api.ChargeStatus`` (IEC 61851 states)."""

    NONE = ""
    A = "A"  # not connected
    B = "B"  # connected, not charging
    C = "C"  # charging
    E = "E"  # error: a charger must not return it (raise, or map to A like evcc drivers)


# --- errors (evcc api/error.go) -------------------------------------------------


class NotAvailableError(Exception):
    """evcc ``api.ErrNotAvailable``: the device cannot do this right now."""


class AsleepError(Exception):
    """evcc ``api.ErrAsleep``."""


class MustRetryError(Exception):
    """evcc ``api.ErrMustRetry``."""


def acceptable_error(err: BaseException) -> bool:
    """evcc core/loadpoint/error.go: AcceptableError."""
    return isinstance(err, (AsleepError, MustRetryError, NotAvailableError))


# --- charger -------------------------------------------------------------------


@runtime_checkable
class Charger(Protocol):
    """What the loadpoint calls on every charger (evcc ``api.Charger``).

    ``max_current`` takes whole amps (evcc ``MaxCurrent(int64)``): unless the
    charger also offers:class:`ChargerEx`, the loadpoint truncates every
    current to whole amps first, as evcc does (``coarseCurrent``).

    Errors are raised;:class:`NotAvailableError` where evcc returns
    ``api.ErrNotAvailable``.
    """

    def status(self) -> ChargeStatus:
        """IEC 61851 status: only A, B or C.

        On a fault raise (evcc drivers return an error for E/F) or report A
        where evcc's driver maps E to A. Any other value is treated as a read
        error: the loadpoint skips the cycle.
        """

    def enabled(self) -> bool:
        """Whether the charger is enabled (allowed to charge)."""

    def enable(self, enable: bool) -> None:
        """Enable or disable charging."""

    def max_current(self, current: int) -> None:
        """Set the per-phase current limit in whole A."""


@runtime_checkable
class ChargerEx(Protocol):
    """evcc ``api.ChargerEx``: fractional current control (GNLT: 0.1 A steps).

    A charger offering it gets the untruncated current through
    ``max_current_millis`` (despite evcc's name the unit is A, as in evcc).
    """

    def max_current_millis(self, current: float) -> None:
        """Set the per-phase current limit in A (fractional)."""


@runtime_checkable
class PhaseSwitcher(Protocol):
    """evcc ``api.PhaseSwitcher``: 1p/3p switching (phase-switch capability)."""

    def phases1p3p(self, phases: int) -> None:
        """Switch to 1 or 3 phases; raise NotAvailableError if not possible now."""


@runtime_checkable
class PhaseGetter(Protocol):
    """evcc ``api.PhaseGetter``: the phases the charger has enabled."""

    def get_phases(self) -> int:
        """1 or 3 (0 = unknown)."""


@runtime_checkable
class CurrentGetter(Protocol):
    """evcc ``api.CurrentGetter``: the current limit the charger applies."""

    def get_max_current(self) -> float:
        """Current limit in A."""


@runtime_checkable
class CurrentLimiter(Protocol):
    """evcc ``api.CurrentLimiter``: device min/max current."""

    def get_min_max_current(self) -> tuple[float, float]:
        """(min, max) in A, 0 = not set."""


@runtime_checkable
class ConnectionTimer(Protocol):
    """evcc ``api.ConnectionTimer``: duration of the current connection."""

    def connection_duration(self) -> float:
        """Seconds since the vehicle connected."""


@runtime_checkable
class PhaseDescriber(Protocol):
    """evcc ``api.PhaseDescriber``: physically connected phases."""

    def phases(self) -> int:
        """1 or 3."""


# --- meters --------------------------------------------------------------------


@runtime_checkable
class Meter(Protocol):
    """evcc ``api.Meter``: total active power in W (charge power for a charger)."""

    def current_power(self) -> float:
        """Power in W."""


@runtime_checkable
class PhaseCurrents(Protocol):
    """evcc ``api.PhaseCurrents``: currents L1..L3 in A."""

    def currents(self) -> tuple[float, float, float]:
        """(L1, L2, L3) in A."""


@runtime_checkable
class PhaseVoltages(Protocol):
    """evcc ``api.PhaseVoltages``: voltages L1..L3 in V."""

    def voltages(self) -> tuple[float, float, float]:
        """(L1, L2, L3) in V."""


@runtime_checkable
class PhasePowers(Protocol):
    """evcc ``api.PhasePowers``: signed powers L1..L3 in W."""

    def powers(self) -> tuple[float, float, float]:
        """(L1, L2, L3) in W."""


def has_cap(device: object, cap: type) -> bool:
    """evcc ``api.HasCap[T]``: whether ``device`` offers the capability ``cap``.

    A device declares its optional capabilities explicitly in the attribute
    ``capabilities`` (a set of the protocol classes of this module, e.g.
    ``{PhaseSwitcher, PhaseGetter, Meter}``); methods or attributes that only
    look like a capability do not count. A declared capability whose methods
    are missing raises:class:`TypeError`.
    """
    if device is None:
        return False
    declared = getattr(device, "capabilities", None)
    if not declared or cap not in declared:
        return False
    if not isinstance(device, cap):
        raise TypeError(f"{type(device).__name__} declares {cap.__name__} but lacks its methods")
    return True


def sign_from_power(current: float, power: float) -> float:
    """evcc util/metering.go: SignFromPower."""
    if current > 0 and power < 0:
        return -current
    return current


# --- circuits ------------------------------------------------------------------


@runtime_checkable
class CircuitLoad(Protocol):
    """evcc ``api.CircuitLoad``: what a circuit reads from a loadpoint."""

    def get_charge_power(self) -> float:
        """Charge power in W."""

    def get_max_phase_current(self) -> float:
        """Largest phase current in A."""

    def get_circuit(self) -> Circuit | None:
        """The circuit the loadpoint is attached to."""


@runtime_checkable
class Circuit(Protocol):
    """The load-management hook (subset of evcc ``api.Circuit`` the core calls).

    This is the interface through which site limits act on the loadpoints.
    class:`gnlt_charger.site.circuit.Circuit` is evcc's own implementation;
    another allocator (e.g. a per-phase DLM) may implement the same methods:

    * ``validate_current(old, new)`` - a loadpoint asks to go from ``old`` to
      ``new`` A per phase; return the current it may use (``<= new``).
    * ``validate_power(old, new)`` - same for power in W.
    * ``get_max_power()`` - configured power limit in W, 0 = none (evcc
      ``circuitMaxPower`` caps the loadpoint's effective max power with it).
    * ``update(loadpoints)`` - called once per site cycle before the
      loadpoint update, with all loadpoints, to refresh measured load.
    * ``get_charge_power()`` / ``get_max_phase_current()`` - the load measured
      on this circuit (for a parent circuit without meter).
    * ``get_parent()`` - the parent circuit or ``None``.
    """

    def validate_current(self, old: float, new: float) -> float:
        """Allowed per-phase current for a requested change."""

    def validate_power(self, old: float, new: float) -> float:
        """Allowed power for a requested change."""

    def get_max_power(self) -> float:
        """Power limit in W, 0 = unlimited."""

    def update(self, loadpoints: list[CircuitLoad]) -> None:
        """Refresh measured power and current."""

    def get_charge_power(self) -> float:
        """Measured power in W."""

    def get_max_phase_current(self) -> float:
        """Measured largest phase current in A."""

    def get_parent(self) -> Circuit | None:
        """Parent circuit."""
