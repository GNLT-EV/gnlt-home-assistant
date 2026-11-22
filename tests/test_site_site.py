"""Site core site: site power, priorities, startup order, the update cycle.

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Sources: evcc core/prioritizer/prioritizer_test.go, core/site_prioritize_test.go,
core/site_startup_test.go, core/site_api_test.go; tests marked "own test" have
no evcc counterpart (evcc's site tests are about battery, tariffs, HEMS and the
optimizer, which are excluded).
"""

import pytest

from gnlt_charger.site import grid
from gnlt_charger.site.api import ChargeMode, ChargeStatus
from gnlt_charger.site.circuit import Circuit
from gnlt_charger.site.clock import ELAPSED, HOUR, MINUTE, ZERO_TIME, MockClock
from gnlt_charger.site.loadpoint import Loadpoint, ThresholdConfig, new_loadpoint_from_config
from gnlt_charger.site.prioritizer import Prioritizer
from gnlt_charger.site.site import STANDBY_POWER, Site, SiteState, startup_order
from site_fakes import MAX_A, MIN_A, FakeMeter, make_charger

A, B, C = ChargeStatus.A, ChargeStatus.B, ChargeStatus.C


class _PrioLp:
    """evcc loadpoint.MockAPI for the prioritizer."""

    def __init__(self, prio: int) -> None:
        self.prio = prio
        self.flex = 0.0

    def get_title(self) -> str:
        return f"lp{self.prio}"

    def effective_priority(self) -> int:
        return self.prio

    def get_charge_power_flexibility(self) -> float:
        return self.flex


def test_prioritizer():
    # evcc core/prioritizer/prioritizer_test.go: TestPrioritzer
    p = Prioritizer()
    lo = _PrioLp(0)
    hi = _PrioLp(1)

    # no additional power available
    lo.flex = 300.0
    p.update_charge_power_flexibility(lo)
    assert p.get_charge_power_flexibility(lo) == 0.0

    # additional power available
    hi.flex = 1e3
    p.update_charge_power_flexibility(hi)
    assert p.get_charge_power_flexibility(hi) == 300.0

    # additional power removed
    lo.flex = 0.0
    p.update_charge_power_flexibility(lo)
    assert p.get_charge_power_flexibility(hi) == 0.0


def new_pv_loadpoint(prio, mode, status, enabled, timer):
    """evcc core/site_prioritize_test.go: newPVLoadpoint."""
    return Loadpoint(
        clock=MockClock(),
        min_current=MIN_A,
        max_current=MAX_A,
        phases=1,
        mode=mode,
        status=status,
        enabled=enabled,
        pv_timer=timer,
        priority=prio,
    )


NOW = MockClock().now()


@pytest.mark.parametrize(
    ("name", "lp", "starting"),
    [
        ("enable timer running", new_pv_loadpoint(0, ChargeMode.SMART, B, False, NOW), True),
        ("enabled not charging", new_pv_loadpoint(0, ChargeMode.SMART, B, True, ZERO_TIME), False),
        ("enabled and charging", new_pv_loadpoint(0, ChargeMode.SMART, C, True, ZERO_TIME), False),
        ("disabled idle", new_pv_loadpoint(0, ChargeMode.SMART, B, False, ZERO_TIME), False),
        ("disconnected", new_pv_loadpoint(0, ChargeMode.SMART, A, False, NOW), False),
        ("not pv mode", new_pv_loadpoint(0, ChargeMode.NOW, B, False, NOW), False),
        # (evcc row "enable pending but car full" not ported: vehicle SoC excluded)
        # elapsed means a delay was skipped, e.g. by a feed-in pause, not an enable pending
        ("timer elapsed", new_pv_loadpoint(0, ChargeMode.SMART, B, False, ELAPSED), False),
    ],
)
def test_pv_charge_starting(name, lp, starting):
    # evcc core/site_prioritize_test.go: TestPvChargeStarting
    assert lp.pv_charge_starting() == starting, name


def test_reserved_pv_power():
    # evcc core/site_prioritize_test.go: TestReservedPVPower
    high = new_pv_loadpoint(1, ChargeMode.SMART, B, False, MockClock().now())
    low = new_pv_loadpoint(0, ChargeMode.SMART, B, False, ZERO_TIME)
    site = Site(loadpoints=[high, low])

    # low reserves high's anticipated max power while high is starting up
    assert site.reserved_pv_power(low) == high.effective_max_power()
    # high (top priority) reserves nothing
    assert site.reserved_pv_power(high) == 0

    # once high is charging it no longer reserves surplus from low
    high.status = C
    high.enabled = True
    high.pv_timer = ZERO_TIME
    assert site.reserved_pv_power(low) == 0

    # high stays enabled and connected but no longer draws (car full): no reservation (#31684)
    high.status = B
    assert site.reserved_pv_power(low) == 0


def test_startup_order():
    # evcc core/site_startup_test.go: TestStartupOrder
    def new_lp(power, status, err=None):
        charger = make_charger(status=status)
        if err is not None:
            charger.errors["status"] = err
        return Loadpoint(charger=charger, charge_power=power)

    idle1 = new_lp(0, A)
    connected1 = new_lp(0, B)
    charging1 = new_lp(7000, C)
    standby = new_lp(STANDBY_POWER, A)
    failing = new_lp(0, ChargeStatus.NONE, OSError("offline"))
    charging2 = new_lp(11000, B)  # charge power decides, not the status
    connected2 = new_lp(5, C)
    faulted = new_lp(0, ChargeStatus.E)

    got = startup_order([idle1, connected1, faulted, charging1, standby, failing, charging2, connected2])
    assert got == [
        charging1, charging2,  # charging
        connected1, connected2,  # vehicle connected
        idle1, faulted, standby, failing,  # remaining, order is kept within each group
    ]


def test_loadpoints_nil_slots():
    # evcc core/site_api_test.go: TestLoadpointsNilSlots
    lp0, lp2 = Loadpoint(), Loadpoint()
    site = Site(loadpoints=[lp0, None, lp2])
    assert len(site.loadpoints) == 3, "disabled loadpoints must keep their slot"
    assert site.loadpoints[1] is None
    assert site.active_loadpoints() == [lp0, lp2]
    assert site.is_configured()


# --- site power (own tests: evcc core/site.go sitePower without battery strategy) ---------


@pytest.mark.parametrize(
    ("desc", "grid_power", "pv", "battery", "aux", "residual", "flexible", "total", "expected"),
    [
        ("grid only, export", -3000, None, None, None, 0, 0, 0, -3000),
        ("grid import", 500, None, None, None, 0, 0, 0, 500),
        ("residual power margin", -3000, None, None, None, 100, 0, 0, -2900),
        ("prioritized flexible power", -1000, None, None, None, 0, 1500, 0, -2500),
        # without prioritySoc (excluded) a charging battery's power counts as available, as in evcc
        ("battery charging", 500, None, [-2000], None, 0, 0, 0, -1500),
        ("battery discharging", 0, None, [800], None, 0, 0, 0, 800),
        ("aux consumer", -2000, None, None, [500], 0, 0, 0, -2500),
        # no grid meter: grid power estimated from charge power and PV
        ("no grid meter", None, [4000], None, None, 0, 0, 1000, -3000),
    ],
)
def test_site_power(desc, grid_power, pv, battery, aux, residual, flexible, total, expected):
    # own test: evcc core/site.go updateMeters + sitePower
    site = Site(
        grid_meter=FakeMeter(grid_power) if grid_power is not None else None,
        pv_meters=[FakeMeter(p) for p in pv or []],
        battery_meters=[FakeMeter(p) for p in battery or []],
        aux_meters=[FakeMeter(p) for p in aux or []],
        residual_power=residual,
    )
    state = site.update_meters()
    res = site.site_power(state, total, flexible)
    assert res.power == expected, desc
    assert res.priority_adjustment == 0
    assert not res.battery_buffered and not res.battery_start


def test_site_power_pv_estimate():
    # own test: evcc core/site.go sitePower - pv power estimated from grid, charge and battery power
    published = {}
    site = Site(grid_meter=FakeMeter(-1000), battery_meters=[FakeMeter(-500)], publish_cb=published.__setitem__)
    state = site.update_meters()
    res = site.site_power(state, 2000, 0)
    assert res.state.pv_power == 2000 + 1000 + 500
    assert published["pvPower"] == 3500
    assert res.power == -1500


def test_pv_meters_sum_positive_only():
    # own test: evcc core/site.go updatePvMeters - negative pv readings do not reduce production
    site = Site(grid_meter=FakeMeter(0), pv_meters=[FakeMeter(3000), FakeMeter(-20)])
    assert site.update_meters().pv_power == 3000


def test_failing_pv_meter_counts_zero():
    # own test: evcc core/site.go collectMeters - a failing meter is logged and counts 0 W
    bad = FakeMeter(5000)
    bad.error = OSError("timeout")
    site = Site(grid_meter=FakeMeter(0), pv_meters=[FakeMeter(1000), bad])
    assert site.update_meters().pv_power == 1000


def test_grid_meter_error_skips_loadpoint_update():
    # own test: evcc core/site.go update - a grid meter error is logged and the loadpoint is not updated
    meter = FakeMeter(-5000)
    meter.error = OSError("grid meter offline")
    lp = Loadpoint(charger=make_charger())
    calls = []
    lp.update = lambda *a: calls.append(a)
    site = Site(grid_meter=meter, loadpoints=[lp])
    site.update(lp)
    assert calls == []


# --- the update cycle (own tests) ----------------------------------------------------------


def _pv_site(grid_power, *, circuit=None, clock=None):
    clock = clock or MockClock()
    clock.add(HOUR)  # mock clock past the elapsed sentinel (as evcc's tests do)
    charger = make_charger("meter", "phase_currents", "millis", status=B, enabled=False, track=True)
    lp = new_loadpoint_from_config(charger, clock=clock, mode="pv", circuit=circuit)
    meter = FakeMeter(grid_power)
    site = Site(grid_meter=meter, loadpoints=[lp], circuit=circuit)
    site.prepare()
    return site, lp, charger, meter, clock


def test_site_update_enables_after_delay():
    # own test: evcc core/site.go update -> Loadpoint.Update in smart mode (pv)
    min3p = grid.current_to_power(MIN_A, 3)
    site, lp, charger, meter, clock = _pv_site(-min3p - 100)
    assert lp.get_mode() == ChargeMode.SMART and not lp.get_always_charge().active()

    # connect event elapses the pv timer: charging starts at once (evcc evVehicleConnectHandler)
    site.update(lp)
    assert charger.called("enable") == [(True,)]
    assert charger.called("max_current_millis") == [(MIN_A,)]
    assert lp.enabled


def test_site_update_waits_enable_delay_without_connect_event():
    # own test: evcc core/loadpoint.go pvMaxCurrent - enable delay (evcc default 1 min)
    min3p = grid.current_to_power(MIN_A, 3)
    site, lp, charger, meter, clock = _pv_site(-min3p - 100)
    lp.status = B  # already connected before: no connect event
    charger.clear()

    site.update(lp)
    assert charger.called("enable") == []
    clock.add(MINUTE)
    site.update(lp)
    assert charger.called("enable") == [(True,)]
    assert lp.enable == ThresholdConfig(threshold=0.0, delay=MINUTE)


def test_site_update_circuit_caps_current():
    # own test: evcc core/site.go updateCircuits before the loadpoint update, setLimit via the circuit
    circuit = Circuit(title="main", max_current=10)
    charger = make_charger("meter", "phase_currents", "millis", status=C, enabled=True, track=True)
    lp = new_loadpoint_from_config(charger, clock=MockClock(), mode="now", circuit=circuit)
    site = Site(grid_meter=FakeMeter(0), loadpoints=[lp], circuit=circuit)
    site.prepare()  # enabled charger: min current set
    charger.phase_currents = (6.0, 6.0, 6.0)
    charger.power = 3 * 230 * 6.0
    charger.clear()

    site.update(lp)
    # circuit current 6 A measured on the loadpoint, 4 A headroom: 6 + 4 = 10 A
    assert circuit.get_max_phase_current() == 6.0
    assert charger.called("max_current_millis") == [(10.0,)]


def test_site_flexible_power_of_lower_priority():
    # own test: evcc core/site.go updatePower - a higher priority pv loadpoint may take the
    # flexible power of a lower priority one (evcc core/prioritizer)
    clock = MockClock()
    lo_charger = make_charger("meter", "millis", status=C, enabled=True)
    lo_charger.power = 3000.0
    hi_charger = make_charger("meter", "millis", status=B, enabled=False)
    lo = new_loadpoint_from_config(lo_charger, clock=clock, mode="pv", priority=0)
    hi = new_loadpoint_from_config(hi_charger, clock=clock, mode="pv", priority=1)
    site = Site(grid_meter=FakeMeter(-500), loadpoints=[lo, hi])
    site.prepare()
    lo.status = C  # charging

    seen = {}
    hi.update = lambda site_power, battery_power=0.0: seen.setdefault("hi", site_power)
    lo.update = lambda site_power, battery_power=0.0: seen.setdefault("lo", site_power)

    site.update(hi)
    site.update(lo)
    assert seen["hi"] == -500 - 3000  # lo's 3000 W count as available for hi
    assert seen["lo"] == -500


def test_site_round_robin_and_requests():
    # own test: evcc core/site.go Run / loopLoadpoints / lpUpdateChan
    clock = MockClock()
    lps = [
        new_loadpoint_from_config(make_charger("meter", status=A), clock=clock, mode="off", title=f"lp{i}")
        for i in range(3)
    ]
    site = Site(grid_meter=FakeMeter(0), loadpoints=lps)
    site.prepare()
    order = []
    for lp in lps:
        lp.update = lambda *a, _lp=lp: order.append(_lp.title)

    site.run_startup()
    assert order == ["lp0", "lp0", "lp1", "lp2"]  # first loop step, then every loadpoint once

    order.clear()
    site.tick()
    site.tick()
    site.tick()
    assert order == ["lp1", "lp2", "lp0"]

    order.clear()
    lps[2].request_update()
    lps[2].request_update()  # queued once
    site.handle_update_requests()
    assert order == ["lp2"]


def test_site_meter_only_mode():
    # own test: evcc core/site.go loopLoadpoints - no loadpoints: meter-only update
    site = Site(grid_meter=FakeMeter(-100))
    site.prepare()
    site.run_startup()
    site.tick()
    assert site.state().grid_power == -100
    assert isinstance(site.state(), SiteState)


def _boom(*_a, **_kw):
    raise RuntimeError("boom")


def test_loadpoint_update_contains_errors():
    # review item 8 - evcc logs a charger error and skips the cycle; nothing escapes Update,
    # including the steps before the strategy (meter voltages, offered current)
    charger = make_charger(status=C, enabled=True)
    lp = Loadpoint(clock=MockClock(), charger=charger, mode=ChargeMode.NOW, min_current=MIN_A, max_current=MAX_A)
    lp.update_charge_voltages = _boom
    lp.update(0, 0)  # must not raise
    assert [c for c in charger.calls if c[0] in ("enable", "max_current")] == []

    lp2 = Loadpoint(clock=MockClock(), charger=make_charger(status=C), publish_cb=_boom)
    lp2.update(0, 0)  # a failing publish callback must not escape either


def test_site_tick_contains_loadpoint_errors():
    # review item 8 - an exception of one loadpoint must not escape Site.tick(): log, skip it this cycle
    clock = MockClock()
    lps = [
        new_loadpoint_from_config(make_charger("meter", status=A), clock=clock, mode="off", title=f"lp{i}")
        for i in range(2)
    ]
    site = Site(grid_meter=FakeMeter(0), loadpoints=lps)
    site.prepare()
    lps[0].update = _boom
    lps[0].update_charge_power_and_currents = _boom
    seen = []
    lps[1].update = lambda *a: seen.append("lp1")

    site.run_startup()  # must not raise
    site.tick()
    site.tick()
    lps[0].request_update()
    site.handle_update_requests()
    assert seen == ["lp1", "lp1"]  # startup once + its round-robin turn; lp1's power still read
