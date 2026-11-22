"""Site core: types, grid conversion, clock and the fake capability check.

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)
"""

import math

import pytest

from gnlt_charger.site import api, grid
from gnlt_charger.site.api import AlwaysCharge, ChargeMode
from gnlt_charger.site.clock import ELAPSED, ZERO_TIME, MockClock, is_zero, since
from site_fakes import make_charger


@pytest.mark.parametrize(
    ("mode", "want_mode", "want_ac"),
    [
        (ChargeMode.MINPV, ChargeMode.SMART, AlwaysCharge.ON),
        (ChargeMode.PV, ChargeMode.SMART, AlwaysCharge.OFF),
        (ChargeMode.SMART, ChargeMode.SMART, None),
        (ChargeMode.NOW, ChargeMode.NOW, None),
    ],
)
def test_charge_mode_normalize(mode, want_mode, want_ac):
    # evcc core/loadpoint_alwayscharge_test.go: TestNormalizeMode (rows without feature chargers)
    assert mode.normalize() == (want_mode, want_ac)


def test_always_charge_active():
    # evcc api/chargemodestatus.go: AlwaysCharge.Active (own test)
    assert AlwaysCharge.ON.active()
    assert AlwaysCharge.ONCE.active()
    assert not AlwaysCharge.OFF.active()
    assert not AlwaysCharge.UNSET.active()


def test_grid_conversion():
    # evcc util/grid: CurrentToPower / PowerToCurrent (own test)
    assert grid.current_to_power(6, 3) == 4140
    assert grid.current_to_power(6, 1) == 1380
    assert grid.power_to_current(4140, 3) == 6


def test_grid_it_factor():
    # evcc util/grid: Factor for IT grids (own test)
    grid.set_no_neutral(True)
    try:
        assert grid.factor(3) == math.sqrt(3)
        assert grid.factor(1) == 1
    finally:
        grid.set_no_neutral(False)


def test_sign_from_power():
    # evcc util/metering.go: SignFromPower (own test)
    assert api.sign_from_power(5, -100) == -5
    assert api.sign_from_power(5, 100) == 5
    assert api.sign_from_power(-5, -100) == -5


def test_clock_sentinels():
    # Go zero time and evcc's elapsed sentinel (own test)
    clock = MockClock()
    assert is_zero(ZERO_TIME)
    assert not is_zero(ELAPSED)
    assert since(clock, ZERO_TIME) == math.inf
    clock.add(3600)
    assert since(clock, ELAPSED) >= 60


def test_capabilities():
    # evcc api.Cap / api.HasCap on composed charger mocks (own test; capabilities declared by make_charger)
    plain = make_charger()
    switcher = make_charger("switcher", "phase_getter")
    assert isinstance(plain, api.Charger)  # the core role is structural, not a declared capability
    assert not api.has_cap(plain, api.PhaseSwitcher)
    assert not api.has_cap(plain, api.PhaseDescriber)
    assert api.has_cap(switcher, api.PhaseSwitcher)
    assert api.has_cap(switcher, api.PhaseGetter)
    assert not api.has_cap(None, api.PhaseSwitcher)


def test_system_clock_monotonic(monkeypatch):
    # review item 4 - evcc's benbjohnson clock.New() measures elapsed time monotonically;
    # the offset keeps ELAPSED (1 ns) far in the past right after boot
    from gnlt_charger.site import clock as clock_mod

    monkeypatch.setattr(clock_mod.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(clock_mod.time, "time", lambda: 1.0)  # a wall clock step must not matter
    c = clock_mod.SystemClock()
    assert c.now() == 5.0 + 1e9
    assert since(c, ELAPSED) > 3600


class _Undeclared:
    """A charger whose attributes merely look like capabilities."""

    phases = 1  # an int attribute, not api.PhaseDescriber

    def status(self):
        return api.ChargeStatus.B

    def enabled(self):
        return False

    def enable(self, enable):
        pass

    def max_current(self, current):
        pass

    def phases1p3p(self, phases):  # method present but capability not declared
        raise AssertionError("must not be called")


def test_capabilities_are_declared_not_guessed():
    # review item 8 - capabilities come from an explicit declaration, not from method names
    from gnlt_charger.site.loadpoint import Loadpoint

    dev = _Undeclared()
    assert not api.has_cap(dev, api.PhaseDescriber)
    assert not api.has_cap(dev, api.PhaseSwitcher)
    lp = Loadpoint(charger=dev)
    assert lp.get_charger_physical_phases() == 0
    assert not lp.has_phase_switching()
    assert lp.active_phases() == 3


def test_declared_capability_without_methods_rejected():
    # review item 8 - a declaration without the methods is a programming error
    class Broken:
        capabilities = frozenset({api.PhaseSwitcher})

    with pytest.raises(TypeError, match="PhaseSwitcher"):
        api.has_cap(Broken(), api.PhaseSwitcher)
