"""Site core circuits: hierarchical current/power limits and the loadpoint hook.

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Sources: evcc core/circuit/circuit_test.go, core/loadpoint_circuit_test.go.
"""

import sys

import pytest

from gnlt_charger.site import api
from gnlt_charger.site.api import ChargeStatus
from gnlt_charger.site.circuit import Circuit, CircuitError
from gnlt_charger.site.clock import MINUTE, MockClock
from gnlt_charger.site.loadpoint import Loadpoint
from site_fakes import FakeCircuit, FakeMeter, FakePhaseMeter, make_charger

# current values for parent, circuit 1, circuit 2; old/new demand values and allowed result
CIRCUIT_TESTS = [
    # no load
    (0, 0, 0, 0, 0, 0),  # =
    (0, 0, 0, 0, 1, 1),  # +
    (0, 0, 0, 0, 2, 1),  # +
    (0, 0, 0, 1, 1, 1),  # =
    # circuit 1 loaded
    (0, 1, 0, 0, 0, 0),  # =
    (0, 1, 0, 0, 1, 0),  # +
    (0, 1, 0, 0, 2, 0),  # +
    (0, 1, 0, 1, 1, 1),  # =
    (0, 1, 0, 2, 1, 1),  # -
    # circuit 1 overloaded
    (0, 2, 0, 0, 0, 0),  # =
    (0, 2, 0, 0, 1, 0),  # +
    (0, 2, 0, 1, 1, 0),  # =
    (0, 2, 0, 2, 2, 1),  # =
    (0, 2, 0, 2, 3, 1),  # +
    (0, 2, 0, 2, 1, 1),  # -
    (0, 1.1, 0, 2, 1, 1),  # -
    (0, 1.1, 0, 1, 0, 0),  # -
    # parent loaded
    (1, 0, 0, 0, 0, 0),  # =
    (1, 0, 0, 0, 1, 0),  # +
    (1, 0, 0, 0, 2, 0),  # +
    (1, 0, 0, 1, 1, 1),  # =
    (1, 0, 0, 2, 1, 1),  # -
    # parent overloaded
    (2, 0, 0, 0, 0, 0),  # =
    (2, 0, 0, 0, 1, 0),  # +
    (2, 0, 0, 1, 1, 0),  # =
    (2, 0, 0, 2, 2, 1),  # =
    (2, 0, 0, 2, 3, 1),  # +
    (2, 0, 0, 2, 1, 1),  # -
    (1.1, 0, 0, 2, 1, 1),  # -
    (1.1, 0, 0, 1, 0, 0),  # -
    # negative load
    (-1, -1, 0, 0, 2, 2),  # +
]


@pytest.mark.parametrize(("p", "c1", "c2", "old", "new", "res"), CIRCUIT_TESTS)
def test_circuit_power(p, c1, c2, old, new, res):
    # evcc core/circuit/circuit_test.go: TestCircuitPower
    pc = Circuit(title="foo", max_power=1, meter=FakeMeter(p), timeout=0)
    ci1 = Circuit(title="foo", max_power=1, meter=FakeMeter(c1), timeout=0)
    ci2 = Circuit(title="foo", max_power=1, meter=FakeMeter(c2), timeout=0)
    ci1.set_parent(pc)
    ci2.set_parent(pc)

    pc.update([])
    assert ci1.validate_power(old, new) == res


@pytest.mark.parametrize(("p", "c1", "c2", "old", "new", "res"), CIRCUIT_TESTS)
def test_circuit_currents(p, c1, c2, old, new, res):
    # evcc core/circuit/circuit_test.go: TestCircuitCurrents
    pc = Circuit(title="foo", max_current=1, meter=FakePhaseMeter(0, (p, p, p)), timeout=0)
    ci1 = Circuit(title="foo", max_current=1, meter=FakePhaseMeter(0, (c1, c1, c1)), timeout=0)
    ci2 = Circuit(title="foo", max_current=1, meter=FakePhaseMeter(0, (c2, c2, c2)), timeout=0)
    ci1.set_parent(pc)
    ci2.set_parent(pc)

    pc.update([])
    assert ci1.validate_current(old, new) == res


@pytest.mark.parametrize(
    ("name", "max_power", "requested", "want"),
    [
        ("no hems, no max", 0, 9000, 9000),
        ("no hems, max set", 5000, 9000, 5000),
    ],
)
def test_validate_power_max_power(name, max_power, requested, want):
    # evcc core/circuit/circuit_test.go: TestHEMSConsumptionClamp (rows without HEMS; HEMS excluded)
    c = Circuit(title="root", max_power=max_power, timeout=0)
    assert c.validate_power(0, requested) == want, name


class _FailingPowersMeter(FakePhaseMeter):
    capabilities = FakePhaseMeter.capabilities | {api.PhasePowers}

    def powers(self):
        raise RuntimeError("powers failed")


def test_circuit_powers_failure():
    # evcc core/circuit/circuit_test.go: TestCircuitPowersFailure
    pc = Circuit(title="parent", meter=FakeMeter(1000), timeout=MINUTE)
    c = Circuit(title="child", max_current=16, meter=_FailingPowersMeter(0, (2.0, 5.0, 1.0)), timeout=MINUTE)
    c.set_parent(pc)

    with pytest.raises(CircuitError, match="circuit powers"):
        pc.update([])
    assert c.get_max_phase_current() == 5.0, "unsigned current"
    assert pc.get_charge_power() == 1000.0, "parent updated"


def test_circuit_meter_without_phase_currents_rejected():
    # evcc core/circuit/circuit.go: New - "meter does not support phase currents" (own test)
    with pytest.raises(ValueError, match="phase currents"):
        Circuit(title="x", max_current=16, meter=FakeMeter(0))


def test_circuit_without_meter_sums_loadpoints_and_children():
    # evcc core/circuit/circuit.go: Update / updateLoadpoints without meter (own test)
    root = Circuit(title="root", max_current=32)
    child = Circuit(title="child", max_current=16)
    child.set_parent(root)

    lp1 = Loadpoint(circuit=child, charge_power=2000, offered_current=8)
    lp2 = Loadpoint(circuit=root, charge_power=1000, charge_currents=[4, 3, 0])
    lp3 = Loadpoint(circuit=None, charge_power=5000)

    root.update([lp1, lp2, lp3])
    assert child.get_charge_power() == 2000
    assert child.get_max_phase_current() == 8
    assert root.get_charge_power() == 3000
    assert root.get_max_phase_current() == 12  # loadpoint currents are summed (evcc)
    # child limit: 16 - 8 = 8 A headroom
    assert child.validate_current(8, 20) == 16
    # parent limit applies, too: 32 - 12 = 20 A headroom
    assert child.validate_current(0, 30) == 8


def test_circuit_overload_on_meter_error():
    # evcc core/circuit/circuit.go: overloadOnError - a meter silent longer than the timeout counts as overload
    # (own test)
    clock = MockClock()
    meter = FakeMeter(1000)
    c = Circuit(title="x", max_power=5000, meter=meter, timeout=MINUTE, clock=clock)
    c.update([])
    assert c.get_charge_power() == 1000

    meter.error = OSError("read failed")
    clock.add(30)
    with pytest.raises(CircuitError, match="circuit power"):
        c.update([])
    assert c.get_charge_power() == 1000  # within timeout: last value kept

    clock.add(31)
    with pytest.raises(CircuitError):
        c.update([])
    assert c.get_charge_power() == sys.float_info.max
    assert c.validate_power(0, 1000) == 0


def test_circuit_dynamic_limits():
    # evcc core/circuit/circuit.go: GetMaxCurrent / GetMaxPower with getters (own test)
    c = Circuit(title="x", max_current=16, max_power=11000, get_max_current=lambda: 10.0)
    assert c.get_max_current() == 10.0
    assert c.get_max_power() == 11000

    def failing():
        raise OSError("no value")

    c.get_max_current_fn = failing
    assert c.get_max_current() == 16


def test_set_parent_cycle():
    # evcc core/circuit/circuit.go: setParent - cycle and double parent (own test)
    a = Circuit(title="a")
    b = Circuit(title="b")
    b.set_parent(a)
    with pytest.raises(ValueError, match="cycle"):
        a.set_parent(b)
    with pytest.raises(ValueError, match="already has a parent"):
        b.set_parent(Circuit(title="c"))


@pytest.mark.parametrize(
    ("name", "headroom", "status", "disable"),
    [
        ("within limits", 0, ChargeStatus.C, False),
        ("over limit", -2, ChargeStatus.C, True),
        ("not charging", 0, ChargeStatus.B, True),
    ],
)
def test_set_limit_circuit_keeps_min_current(name, headroom, status, disable):
    # evcc core/loadpoint_circuit_test.go: TestSetLimitCircuitKeepsMinCurrent
    charger = make_charger()
    circuit = FakeCircuit(validate_current=lambda old, new: min(new, max(0, old + headroom)))
    lp = Loadpoint(
        clock=MockClock(),
        charger=charger,
        circuit=circuit,
        min_current=6,
        max_current=16,
        phases=3,
        status=status,
        enabled=True,
        offered_current=6,
        charge_currents=[5.9, 5.9, 5.9],
    )
    lp.set_limit(16)
    assert lp.enabled == (not disable), name
    assert charger.called("enable") == ([(False,)] if disable else [])
    assert charger.called("max_current") == []
