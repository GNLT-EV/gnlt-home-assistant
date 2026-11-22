"""Fakes for the site core tests.

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

evcc composes optional charger capabilities from mocks
(``struct{ *api.MockCharger; *api.MockPhaseSwitcher }``); here
func:`make_charger` builds a fake class with the requested capability
mixins. Every call is recorded in ``calls`` so a test can assert what was
called (gomock fails on unexpected calls; the tests check the log instead).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gnlt_charger.site import api
from gnlt_charger.site.api import ChargeStatus

MIN_A = 6.0  # evcc core/loadpoint_test.go: minA
MAX_A = 16.0  # evcc core/loadpoint_test.go: maxA


class FakeCharger:
    """evcc ``api.MockCharger``: status, enabled, enable, max_current.

    Return values are fixed (like a gomock ``Return``) unless ``track`` is
    set, then ``enable`` changes what ``enabled`` reports. Without the
    ``millis`` capability it is evcc's plain ``api.Charger`` (whole amps).
    """

    def __init__(
        self,
        *,
        status: ChargeStatus = ChargeStatus.NONE,
        enabled: bool = False,
        track: bool = False,
        **caps: Any,
    ) -> None:
        self.calls: list[tuple] = []
        self.status_value = status
        self.enabled_value = enabled
        self.track = track
        self.errors: dict[str, BaseException] = {}
        for key, value in caps.items():
            setattr(self, key, value)

    # --- helpers -----------------------------------------------------------
    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        err = self.errors.get(name)
        if err is not None:
            raise err

    def called(self, name: str) -> list[tuple]:
        """Arguments of every call of ``name``."""
        return [c[1:] for c in self.calls if c[0] == name]

    def clear(self) -> None:
        self.calls.clear()

    # --- api.Charger -------------------------------------------------------
    def status(self) -> ChargeStatus:
        self._call("status")
        value = self.status_value
        return value() if callable(value) else value

    def enabled(self) -> bool:
        self._call("enabled")
        return self.enabled_value

    def enable(self, enable: bool) -> None:
        self._call("enable", enable)
        if self.track:
            self.enabled_value = enable

    def max_current(self, current: float) -> None:
        self._call("max_current", current)


class ChargerExCap:
    """evcc ``api.ChargerEx`` (MaxCurrentMillis)."""

    def max_current_millis(self, current: float) -> None:
        self._call("max_current_millis", current)


class PhaseSwitcherCap:
    """evcc ``api.MockPhaseSwitcher``."""

    def phases1p3p(self, phases: int) -> None:
        self._call("phases1p3p", phases)


class PhaseGetterCap:
    """evcc ``api.MockPhaseGetter``; ``charger_phases`` is the reply."""

    charger_phases = 0

    def get_phases(self) -> int:
        self._call("get_phases")
        return self.charger_phases


class CurrentGetterCap:
    """evcc ``api.MockCurrentGetter``; ``charger_current`` is the reply."""

    charger_current = 0.0

    def get_max_current(self) -> float:
        self._call("get_max_current")
        return self.charger_current


class CurrentLimiterCap:
    """evcc ``api.MockCurrentLimiter``."""

    min_max_current = (0.0, 0.0)

    def get_min_max_current(self) -> tuple[float, float]:
        return self.min_max_current


class PhaseDescriberCap:
    """evcc ``api.PhaseDescriber``; ``physical_phases`` is the reply."""

    physical_phases = 3

    def phases(self) -> int:
        return self.physical_phases


class MeterCap:
    """Charge meter integrated in the charger (evcc ``api.Meter``)."""

    power = 0.0

    def current_power(self) -> float:
        return self.power


class PhaseCurrentsCap:
    """evcc ``api.PhaseCurrents``."""

    phase_currents: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def currents(self) -> tuple[float, float, float]:
        return self.phase_currents


class ConnectionTimerCap:
    """evcc ``api.MockConnectionTimer``; ``connection`` is the reply (s)."""

    connection = 0.0

    def connection_duration(self) -> float:
        self._call("connection_duration")
        return self.connection


# name: (mixin, declared capability protocol)
CAPS = {
    "millis": (ChargerExCap, api.ChargerEx),
    "switcher": (PhaseSwitcherCap, api.PhaseSwitcher),
    "phase_getter": (PhaseGetterCap, api.PhaseGetter),
    "current_getter": (CurrentGetterCap, api.CurrentGetter),
    "current_limiter": (CurrentLimiterCap, api.CurrentLimiter),
    "describer": (PhaseDescriberCap, api.PhaseDescriber),
    "meter": (MeterCap, api.Meter),
    "phase_currents": (PhaseCurrentsCap, api.PhaseCurrents),
    "connection_timer": (ConnectionTimerCap, api.ConnectionTimer),
}


def make_charger(*caps: str, **kw: Any) -> FakeCharger:
    """A fake charger with the named capabilities (keys of:data:`CAPS`), declared in ``capabilities``."""
    bases = tuple(CAPS[c][0] for c in caps) + (FakeCharger,)
    declared = frozenset(CAPS[c][1] for c in caps)
    cls = type("Fake_" + "_".join(caps or ("plain",)), bases, {"capabilities": declared})
    return cls(**kw)


class FakeCircuit:
    """evcc ``api.MockCircuit`` with ``DoAndReturn`` functions.

    ``expect_power_old`` mirrors a gomock argument matcher on ``old``.
    """

    def __init__(
        self,
        validate_current: Callable[[float, float], float] | None = None,
        validate_power: Callable[[float, float], float] | None = None,
        max_power: float = 0.0,
        expect_power_old: float | None = None,
    ) -> None:
        self._vc = validate_current or (lambda _old, new: new)
        self._vp = validate_power or (lambda _old, new: new)
        self.max_power = max_power
        self.expect_power_old = expect_power_old
        self.calls: list[tuple] = []

    def validate_current(self, old: float, new: float) -> float:
        self.calls.append(("validate_current", old, new))
        return self._vc(old, new)

    def validate_power(self, old: float, new: float) -> float:
        self.calls.append(("validate_power", old, new))
        if self.expect_power_old is not None:
            assert old == self.expect_power_old, f"unexpected ValidatePower old={old}"
        return self._vp(old, new)

    def get_max_power(self) -> float:
        return self.max_power

    def update(self, loadpoints) -> None:
        self.calls.append(("update",))

    def get_charge_power(self) -> float:
        return 0.0

    def get_max_phase_current(self) -> float:
        return 0.0

    def get_parent(self):
        return None


class FakeMeter:
    """evcc ``api.MockMeter`` (+ optional phase currents/powers)."""

    capabilities = frozenset({api.Meter})

    def __init__(self, power: float = 0.0) -> None:
        self.power = power
        self.error: BaseException | None = None

    def current_power(self) -> float:
        if self.error is not None:
            raise self.error
        return self.power


class FakePhaseMeter(FakeMeter):
    """Meter with ``currents`` (evcc ``api.MockPhaseCurrents``)."""

    capabilities = frozenset({api.Meter, api.PhaseCurrents})

    def __init__(self, power: float = 0.0, currents=(0.0, 0.0, 0.0)) -> None:
        super().__init__(power)
        self.phase_currents = currents

    def currents(self) -> tuple[float, float, float]:
        return self.phase_currents
