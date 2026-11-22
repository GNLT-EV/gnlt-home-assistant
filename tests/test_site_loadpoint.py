"""Site core loadpoint: control, PV hysteresis, charger sync, status, modes.

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Each test names its evcc source test. ``Loadpoint(...)`` mirrors evcc's Go
struct literal ``&Loadpoint{...}`` (zero values for everything not given);
``new_loadpoint()`` mirrors ``NewLoadpoint`` (evcc defaults).
Go durations of ``1`` in evcc tables are nanoseconds (``NS`` here).
"""

import queue

import pytest

from gnlt_charger.site import grid
from gnlt_charger.site.api import AlwaysCharge, ChargeMode, ChargeStatus
from gnlt_charger.site.clock import HOUR, MINUTE, MockClock, SystemClock
from gnlt_charger.site.loadpoint import (
    PHASE_SWITCH_DURATION,
    Loadpoint,
    ThresholdConfig,
    new_loadpoint,
    new_loadpoint_from_config,
    status_events,
)
from site_fakes import MAX_A, MIN_A, make_charger

NS = 1e-9  # Go time.Duration(1)
A, B, C = ChargeStatus.A, ChargeStatus.B, ChargeStatus.C


def attach_listeners(lp: Loadpoint) -> None:
    """evcc core/loadpoint_test.go: attachListeners.

    Prepare reads Enabled() == true and sets MaxCurrent(minCurrent), as evcc's
    gomock expectations require; the consumed calls are then cleared so the
    test can assert the following writes exactly.
    """
    charger = lp.charger
    saved = charger.enabled_value
    charger.enabled_value = True
    lp.prepare()
    charger.enabled_value = saved
    assert charger.called("enabled") == [()]
    assert actions(charger) == [("max_current", int(lp.min_current))]
    charger.clear()


def actions(charger) -> list[tuple]:
    """The calls that change the charger (gomock expectations of the evcc tests)."""
    return [c for c in charger.calls if c[0] in ("enable", "max_current", "max_current_millis", "phases1p3p")]


def test_new():
    # evcc core/loadpoint_test.go: TestNew
    lp = new_loadpoint()
    assert lp.phases == 0
    assert lp.max_current == MAX_A
    assert lp.status == ChargeStatus.NONE
    assert not lp.charging()


@pytest.mark.parametrize(
    ("status", "mode", "always", "expect"),
    [
        (A, ChargeMode.OFF, AlwaysCharge.OFF, [("enable", False)]),
        (A, ChargeMode.NOW, AlwaysCharge.OFF, [("enable", False)]),
        (A, ChargeMode.SMART, AlwaysCharge.ON, [("enable", False)]),
        (A, ChargeMode.SMART, AlwaysCharge.OFF, [("enable", False)]),
        (B, ChargeMode.OFF, AlwaysCharge.OFF, [("enable", False)]),
        (B, ChargeMode.NOW, AlwaysCharge.OFF, [("max_current", MAX_A)]),
        (B, ChargeMode.SMART, AlwaysCharge.ON, []),  # MaxCurrent omitted since identical value
        (B, ChargeMode.SMART, AlwaysCharge.OFF, [("enable", False)]),  # zero since update called with 0
        (C, ChargeMode.OFF, AlwaysCharge.OFF, [("enable", False)]),
        (C, ChargeMode.NOW, AlwaysCharge.OFF, [("max_current", MAX_A)]),
        (C, ChargeMode.SMART, AlwaysCharge.ON, []),
        (C, ChargeMode.SMART, AlwaysCharge.OFF, []),  # omitted since PV balanced
    ],
)
def test_update_power_zero(status, mode, always, expect):
    # evcc core/loadpoint_test.go: TestUpdatePowerZero
    charger = make_charger(status=status, enabled=True)
    lp = Loadpoint(
        clock=MockClock(),
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        phases=1,
        solar_share=1,
        status=status,
    )
    attach_listeners(lp)
    lp.mode = mode
    lp.always_charge = always
    lp.update(0, 0)
    assert actions(charger) == expect


DT = MINUTE
PH = 3
V = grid.VOLTAGE

PV_HYSTERESIS = [
    # keep disabled
    (False, 0, 0, [(0, 0, 0), (0, NS, 0), (0, DT - NS, 0), (0, DT + NS, 0)]),
    # enable when threshold not configured but min power met
    (False, 0, 0, [(-6 * V * PH, 0, 0), (-6 * V * PH, NS, 0), (-6 * V * PH, DT - NS, 0), (-6 * V * PH, DT + NS, MIN_A)]),
    # keep disabled when threshold not configured
    (False, 0, 0, [(-400, 0, 0), (-400, NS, 0), (-400, DT - NS, 0), (-400, DT + NS, 0)]),
    # keep disabled when threshold (lower minCurrent) not met
    (False, -500, 0, [(-400, 0, 0), (-400, NS, 0), (-400, DT - NS, 0), (-400, DT + NS, 0)]),
    # keep disabled when threshold (higher minCurrent) not met
    (False, -7 * V * PH, 0, [(-6 * V * PH, 0, 0), (-6 * V * PH, NS, 0), (-6 * V * PH, DT - NS, 0), (-6 * V * PH, DT + NS, 0)]),
    # enable when threshold met
    (False, -500, 0, [(-500, 0, 0), (-500, NS, 0), (-500, DT - NS, 0), (-500, DT + NS, MIN_A)]),
    # keep enabled at max
    (True, 500, 0, [(-16 * V * PH, 0, MAX_A), (-16 * V * PH, NS, MAX_A), (-16 * V * PH, DT - NS, MAX_A), (-16 * V * PH, DT + NS, MAX_A)]),
    # keep enabled at min
    (True, 500, 0, [(-6 * V * PH, 0, MIN_A), (-6 * V * PH, NS, MIN_A), (-6 * V * PH, DT - NS, MIN_A), (-6 * V * PH, DT + NS, MIN_A)]),
    # keep enabled at min (negative threshold)
    (True, 0, 500, [(-500, 0, MIN_A), (-500, NS, MIN_A), (-500, DT - NS, MIN_A), (-500, DT + NS, MIN_A)]),
    # disable when threshold met
    (True, 0, 500, [(500, 0, MIN_A), (500, NS, MIN_A), (500, DT - NS, MIN_A), (500, DT + NS, 0)]),
    # reset enable timer when threshold not met while timer active
    (False, -500, 0, [(-500, 0, 0), (-500, NS, 0), (-499, DT - NS, 0), (-500, DT + NS, 0), (-500, 2 * DT, 0), (-500, 2 * DT + NS, MIN_A)]),
    # reset enable timer when threshold not met while timer active and threshold not configured
    (False, 0, 0, [(-6 * V * 10 - 1, DT + NS, 0), (-6 * V * PH, DT + NS, 0), (-6 * V * PH, DT + 2 * NS, 0), (-6 * V * PH, 2 * DT, 0), (-6 * V * PH, 2 * DT + NS, MIN_A)]),
    # reset disable timer when threshold not met while timer active
    (True, 0, 500, [(500, 0, MIN_A), (500, NS, MIN_A), (499, DT - NS, MIN_A), (500, DT + NS, MIN_A), (500, 2 * DT, MIN_A), (500, 2 * DT + NS, 0)]),
]


@pytest.mark.parametrize("status", [B, C])
@pytest.mark.parametrize(("enabled", "enable_t", "disable_t", "series"), PV_HYSTERESIS)
def test_pv_hysteresis(status, enabled, enable_t, disable_t, series):
    # evcc core/loadpoint_test.go: TestPVHysteresis
    clock = MockClock()
    lp = Loadpoint(
        clock=clock,
        charger=make_charger(),
        min_current=MIN_A,
        max_current=MAX_A,
        phases=PH,
        measured_phases=PH,
        solar_share=1,
        enable=ThresholdConfig(threshold=enable_t, delay=DT),
        disable=ThresholdConfig(threshold=disable_t, delay=DT),
    )
    lp.status = status
    start = clock.now()
    for step, (site, delay, want) in enumerate(series):
        clock.set(start + delay)
        lp.enabled = enabled
        assert lp.pv_max_current(site, 0) == want, f"step {step}"
        assert lp.charger.calls == [], f"step {step}: unexpected charger call"


def test_pv_hysteresis_for_status_other_than_c():
    # evcc core/loadpoint_test.go: TestPVHysteresisForStatusOtherThanC
    lp = Loadpoint(
        clock=MockClock(), min_current=MIN_A, max_current=MAX_A, phases=PH, measured_phases=PH, solar_share=1
    )
    lp.status = A
    site_power = -float(PH) * MIN_A * V + 1  # 1W below min power
    assert lp.pv_max_current(site_power, 0) == 0


def test_set_mode_at_disconnect():
    # evcc core/loadpoint_test.go: TestSetModeAndSocAtDisconnect (mode part; SoC is excluded)
    clock = MockClock()
    charger = make_charger()
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        status=C,
        default_mode=ChargeMode.OFF,
    )
    attach_listeners(lp)
    lp.enabled = True
    lp.offered_current = MIN_A
    lp.mode = ChargeMode.NOW

    # charging at min
    charger.enabled_value = True
    charger.status_value = C
    lp.update(500, 0)
    assert actions(charger) == [("max_current", MAX_A)]

    # switch off when disconnected
    charger.clear()
    clock.add(5 * MINUTE)
    charger.status_value = A
    lp.update(-300, 0)
    assert actions(charger) == [("enable", False)]
    assert lp.get_mode() == ChargeMode.OFF


@pytest.mark.parametrize(
    "series",
    [
        # immediately disable when threshold met after phase switch
        [(2 * V * MIN_A, 0, MIN_A), (2 * V * MIN_A, NS, MIN_A), (2 * V * MIN_A, DT - NS, MIN_A), (2 * V * MIN_A, DT + NS, 0)],
        # stay enabled when threshold not met after phase switch
        [(5 * V, 0, MIN_A), (5 * V, NS, MIN_A), (5 * V, DT - NS, MIN_A), (5 * V, DT + NS, MIN_A)],
    ],
)
def test_pv_hysteresis_after_phase_switch(series):
    # evcc core/loadpoint_test.go: TestPVHysteresisAfterPhaseSwitch
    clock = MockClock()
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        solar_share=1,
        disable=ThresholdConfig(delay=DT),
        status=C,
        enabled=True,
        charge_power=3 * V * MIN_A,  # charging 3p at min current
    )
    start = clock.now()
    for step, (site, delay, want) in enumerate(series):
        clock.set(start + delay)
        assert lp.pv_max_current(site, 0) == want, f"step {step}"
    assert charger.called("phases1p3p") == [(1,)]


BAD = 2 * V * MIN_A + 100  # disable instead of scaling down
GOOD = V * MIN_A / 2  # 1p covered, 3p not: scale down


@pytest.mark.parametrize(
    ("name", "series", "scaled"),
    [
        ("disables within one delay", [(BAD, 0, MIN_A), (GOOD, 20, MIN_A), (BAD, 40, MIN_A), (BAD, DT + 1, 0)], False),
        ("scales down within one delay", [(BAD, 0, MIN_A), (GOOD, 20, MIN_A), (BAD, 40, MIN_A), (GOOD, DT + 1, MIN_A)], True),
    ],
)
def test_pv_disable_phase_timer_handover(name, series, scaled):
    # evcc core/loadpoint_test.go: TestPVDisablePhaseTimerHandover
    clock = MockClock()
    clock.add(HOUR)  # avoid time.IsZero
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        phases=3,
        measured_phases=3,
        solar_share=1,
        disable=ThresholdConfig(delay=DT),
        status=C,
        enabled=True,
        charge_power=3 * V * MIN_A,
    )
    start = clock.now()
    for step, (site, delay, want) in enumerate(series):
        clock.set(start + delay)
        assert lp.pv_max_current(site, 0) == want, f"{name} step {step}"
    assert charger.called("phases1p3p") == ([(1,)] if scaled else [])


def test_connection_duration_drop_detection():
    # evcc core/loadpoint_test.go: TestConnectionDurationDropDetection
    clock = MockClock()
    charger = make_charger("connection_timer", status=B)
    lp = Loadpoint(clock=clock, charger=charger, min_current=MIN_A, max_current=MAX_A)
    attach_listeners(lp)
    connected_time = clock.now() - 10 * MINUTE
    lp.enabled = True
    lp.status = C
    lp.connected_duration = 10 * MINUTE
    lp.connected_time = connected_time
    charger.enabled_value = True
    charger.connection = 0.0
    lp.update(500, 0)
    assert lp.connected_time != connected_time
    assert charger.called("max_current") == []


def _solar_lp(share, enabled, enable_t, disable_t):
    lp = Loadpoint(
        clock=MockClock(),
        charger=make_charger(),
        min_current=MIN_A,
        max_current=MAX_A,
        phases=3,
        measured_phases=3,
        enable=ThresholdConfig(threshold=enable_t),
        disable=ThresholdConfig(threshold=disable_t),
        solar_share=share,
    )
    lp.status = C
    lp.enabled = enabled
    return lp


def test_pv_solar_share():
    # evcc core/loadpoint_test.go: TestPVSolarShare (integrated-device asserts excluded)
    min_power = grid.current_to_power(MIN_A, 3)

    # enable: share 1.0 requires the full min power as surplus
    assert _solar_lp(1, False, 0, 0).pv_max_current(-min_power, 0) == MIN_A
    assert _solar_lp(1, False, 0, 0).pv_max_current(-min_power + 100, 0) == 0
    # enable: share 0.5 accepts half the min power from grid
    assert _solar_lp(0.5, False, 0, 0).pv_max_current(-min_power / 2, 0) == MIN_A
    # enable: share 0 starts as soon as there is no grid import
    assert _solar_lp(0, False, 0, 0).pv_max_current(0, 0) == MIN_A
    # disable: share 1.0 derives threshold 0, so feed-in keeps charging
    assert _solar_lp(1, True, 0, 0).pv_max_current(-500, 0) == MIN_A
    assert _solar_lp(1, True, 0, 0).pv_max_current(100, 0) == 0
    # disable: share 0.5 tolerates grid import up to half the min power
    assert _solar_lp(0.5, True, 0, 0).pv_max_current(min_power / 2 - 100, 0) == MIN_A
    assert _solar_lp(0.5, True, 0, 0).pv_max_current(min_power / 2 + 100, 0) == 0
    # configured thresholds take precedence over solar share
    assert _solar_lp(1, False, 5000, 0).pv_max_current(4000, 0) == MIN_A
    assert _solar_lp(1, True, 0, 5000).pv_max_current(100, 0) == MIN_A


def test_pv_solar_share_phases():
    # evcc core/loadpoint_test.go: TestPVSolarSharePhases
    charger = make_charger("switcher")

    def new_lp(enabled):
        lp = Loadpoint(
            clock=MockClock(),
            charger=charger,
            min_current=MIN_A,
            max_current=MAX_A,
            phases=3,
            phases_configured=3,
            solar_share=1,
        )
        lp.status = C
        lp.enabled = enabled
        return lp

    # must not enable at 1p surplus while pinned to 3p
    assert new_lp(False).pv_max_current(-grid.current_to_power(MIN_A, 1), 0) == 0
    # should enable at 3p surplus
    assert new_lp(False).pv_max_current(-grid.current_to_power(MIN_A, 3), 0) == MIN_A


def test_request_update():
    # evcc core/loadpoint_test.go: TestRequestUpdate
    ch = queue.Queue(maxsize=2)
    lp1 = Loadpoint(lp_chan=ch)
    lp2 = Loadpoint(lp_chan=ch)

    lp1.request_update()
    lp1.request_update()  # already queued
    lp2.request_update()

    assert ch.qsize() == 2
    assert ch.get_nowait().dequeued() is lp1
    assert ch.get_nowait().dequeued() is lp2

    # queued again once the site has taken the request
    lp1.request_update()
    assert ch.get_nowait() is lp1

    # not prepared yet
    Loadpoint().request_update()


# --- evcc core/loadpoint_sync_test.go ------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "actual", "corrected"),
    [
        (A, False, False, False),
        (C, False, False, True),  # disabled but charging
        (A, False, True, True),
        (A, True, False, False),
        (A, True, True, True),
    ],
)
def test_sync_charger(status, expected, actual, corrected):
    # evcc core/loadpoint_sync_test.go: TestSyncCharger
    charger = make_charger(enabled=actual)
    lp = Loadpoint(clock=SystemClock(), charger=charger, status=status, enabled=expected)
    lp.sync_charger()
    assert lp.enabled == corrected
    assert charger.called("enable") == ([(corrected,)] if status == C else [])


def test_sync_charger_during_phase_switch():
    # evcc core/loadpoint_sync_test.go: TestSyncChargerDuringPhaseSwitch
    clock = MockClock()
    charger = make_charger(status=B, enabled=False)
    lp = Loadpoint(clock=clock, charger=charger, status=C, enabled=True, phases_switched=clock.now())

    assert lp.get_status_changes() == []
    assert lp.get_status() == C

    lp.sync_charger()
    assert lp.enabled
    assert charger.called("enable") == [(True,)]


@pytest.mark.parametrize(
    ("lp_current", "actual", "out"),
    [
        (6, 5, 5),  # force
        (6, 6.1, 6),
        (6, 6.5, 6.5),
        (6, 7, 7),
    ],
)
def test_sync_charger_currents_by_getter(lp_current, actual, out):
    # evcc core/loadpoint_sync_test.go: TestSyncChargerCurrentsByGetter
    charger = make_charger("current_getter", enabled=True, charger_current=actual)
    lp = Loadpoint(
        clock=SystemClock(), charger=charger, status=C, enabled=True, phases=3, offered_current=lp_current
    )
    lp.sync_charger()
    assert lp.offered_current == out
    assert len(charger.called("get_max_current")) <= 1


@pytest.mark.parametrize(
    ("lp_current", "actual", "out"),
    [
        (6, 5, 6),  # ignore
        (6, 6.1, 6),
        (6, 6.5, 6),
        (6, 7, 6),  # ignore
        (6, 7.1, 7.1),
    ],
)
def test_sync_charger_currents_by_measurement(lp_current, actual, out):
    # evcc core/loadpoint_sync_test.go: TestSyncChargerCurrentsByMeasurement
    charger = make_charger(enabled=True)
    lp = Loadpoint(
        clock=SystemClock(),
        charger=charger,
        status=C,
        enabled=True,
        phases=3,
        offered_current=lp_current,
        charge_currents=[actual, 0, 0],
    )
    lp.sync_charger()
    assert lp.offered_current == out


@pytest.mark.parametrize(
    ("lp_phases", "actual", "out"),
    [
        (0, 0, 0),
        (0, 1, 1),  # seed
        (0, 3, 3),  # seed
        (1, 0, 1),
        (1, 1, 1),
        (1, 3, 3),
        (3, 0, 3),
        (3, 1, 1),  # force
        (3, 3, 3),
    ],
)
def test_sync_charger_phases_by_getter(lp_phases, actual, out):
    # evcc core/loadpoint_sync_test.go: TestSyncChargerPhasesByGetter
    charger = make_charger("switcher", "phase_getter", enabled=True, charger_phases=actual)
    lp = Loadpoint(
        clock=SystemClock(),
        charger=charger,
        status=C,
        enabled=True,
        phases=lp_phases,
        measured_phases=actual,
    )
    lp.sync_charger()
    assert lp.phases == out
    assert len(charger.called("get_phases")) <= 1


def test_sync_charger_phases_disabled():
    # evcc core/loadpoint_sync_test.go: TestSyncChargerPhasesDisabled
    charger = make_charger("switcher", "phase_getter", enabled=False, charger_phases=1)
    lp = Loadpoint(clock=SystemClock(), charger=charger, status=B, phases=3, measured_phases=3)
    lp.sync_charger()
    assert lp.phases == 1

    # measured currents are stale while disabled
    lp.measured_phases = 3
    lp.charger = make_charger("switcher", enabled=False)
    lp.sync_charger()
    assert lp.phases == 1


def test_sync_charger_phases_disabled_gates():
    # evcc core/loadpoint_sync_test.go: TestSyncChargerPhasesDisabledGates
    charger = make_charger("switcher", "phase_getter", enabled=False)
    lp = Loadpoint(clock=SystemClock(), charger=charger, status=B, phases=3, phases_configured=3)
    lp.sync_charger()
    assert lp.phases == 3
    assert charger.called("get_phases") == []

    lp.phases_configured = 0
    charger.errors["get_phases"] = TimeoutError("timeout")
    lp.sync_charger()
    assert lp.phases == 3


@pytest.mark.parametrize(
    ("lp_phases", "actual", "out"),
    [
        (0, 0, 0),
        (0, 1, 0),  # 1p vehicle on 3p possible
        (0, 2, 3),  # seed
        (0, 3, 3),  # seed
        (1, 0, 1),
        (1, 1, 1),
        (1, 3, 3),
        (3, 0, 3),
        (3, 1, 3),  # ignore
        (3, 3, 3),
    ],
)
def test_sync_charger_phases_by_measurement(lp_phases, actual, out):
    # evcc core/loadpoint_sync_test.go: TestSyncChargerPhasesByMeasurement
    charger = make_charger("switcher", enabled=True)
    lp = Loadpoint(
        clock=SystemClock(), charger=charger, status=C, enabled=True, phases=lp_phases, measured_phases=actual
    )
    lp.sync_charger()
    assert lp.phases == out


# --- evcc core/loadpoint_status_test.go ----------------------------------------

EV_CONNECT, EV_DISCONNECT, EV_START, EV_STOP = "connect", "disconnect", "start", "stop"


@pytest.mark.parametrize(
    ("frm", "to", "events"),
    [
        (ChargeStatus.NONE, A, [EV_DISCONNECT]),
        (ChargeStatus.NONE, B, [EV_CONNECT]),
        (ChargeStatus.NONE, C, [EV_CONNECT, EV_START]),
        (A, B, [EV_CONNECT]),
        (A, C, [EV_CONNECT, EV_START]),
        (B, A, [EV_DISCONNECT]),
        (B, C, [EV_START]),
        (C, A, [EV_STOP, EV_DISCONNECT]),
        (C, B, [EV_STOP]),
    ],
)
def test_status_events(frm, to, events):
    # evcc core/loadpoint_status_test.go: TestStatusEvents
    assert status_events(frm, to) == events


@pytest.mark.parametrize(
    ("since", "connection", "expected"),
    [
        # the connection duration drops on interruption, too, and must not surface as intermediate disconnect
        (PHASE_SWITCH_DURATION - 1, MINUTE, []),
        (PHASE_SWITCH_DURATION + 1, 2 * HOUR, [B]),
    ],
)
def test_phase_switch_interruption(since, connection, expected):
    # evcc core/loadpoint_status_test.go: TestPhaseSwitchInterruption
    charger = make_charger("connection_timer", status=B, connection=connection)
    clock = SystemClock()
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        status=C,
        phases_switched=clock.now() - since,
        connected_duration=HOUR,
    )
    assert lp.get_status_changes() == expected
    assert lp.get_status() == C
    # connection duration is tracked even while the interruption is ignored
    assert lp.connected_duration == connection


# --- evcc core/loadpoint_api_test.go -------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "since", "want"),
    [
        (True, 0, 7),  # switching, enabled
        (False, 0, 0.4),  # switching, disabled
        (True, 2 * PHASE_SWITCH_DURATION, 0.4),  # switch completed
    ],
)
def test_get_max_phase_current_phase_switch(enabled, since, want):
    # evcc core/loadpoint_api_test.go: TestGetMaxPhaseCurrentPhaseSwitch
    clock = MockClock()
    lp = Loadpoint(
        clock=clock,
        enabled=enabled,
        offered_current=7,
        charge_currents=[0.4, 0.3, 0.3],
        phases_switched=clock.now() - since,
    )
    assert lp.get_max_phase_current() == want


# --- evcc core/loadpoint_effective_test.go -------------------------------------


@pytest.mark.parametrize(
    ("charger_min", "charger_max", "eff_min", "eff_max"),
    [
        (0, 0, 6, 16),
        (2, 0, 2, 16),  # charger min lower, max empty - charger wins
        (7, 0, 7, 16),  # charger min higher, max empty (no practical use)
        (0, 10, 6, 10),  # charger max lower, min empty - loadpoint wins
        (0, 20, 6, 16),  # charger max higher, min empty - loadpoint wins
    ],
)
def test_effective_min_max_current(charger_min, charger_max, eff_min, eff_max):
    # evcc core/loadpoint_effective_test.go: TestEffectiveMinMaxCurrent (vehicle rows excluded)
    lp = new_loadpoint()
    if charger_min + charger_max > 0:
        lp.charger = make_charger("current_limiter", min_max_current=(charger_min, charger_max))
    else:
        lp.charger = make_charger()
    assert lp.effective_min_current() == eff_min
    assert lp.effective_max_current() == eff_max


@pytest.mark.parametrize(
    ("mode", "always", "status", "want"),
    [
        (ChargeMode.SMART, AlwaysCharge.OFF, B, 0),  # not charging
        (ChargeMode.SMART, AlwaysCharge.OFF, C, 2700),  # full power is flexible
        (ChargeMode.SMART, AlwaysCharge.ON, C, 2700 - 1380),  # surplus above min is flexible
        (ChargeMode.NOW, AlwaysCharge.OFF, C, 0),  # never flexible
    ],
)
def test_get_charge_power_flexibility(mode, always, status, want):
    # evcc core/loadpoint_effective_test.go: TestGetChargePowerFlexibility (plan/boost rows excluded)
    lp = new_loadpoint()
    lp.mode = mode
    lp.always_charge = always
    lp.status = status
    lp.charge_power = 2700
    lp.min_current = 6
    lp.phases = 1
    assert lp.get_charge_power_flexibility() == want


# --- evcc core/loadpoint_grid_test.go ------------------------------------------


@pytest.fixture
def it_grid():
    yield
    grid.set_no_neutral(False)


def test_effective_power_grid_it(it_grid):
    # evcc core/loadpoint_grid_test.go: TestEffectivePowerGridIT
    grid.set_no_neutral(True)
    lp = Loadpoint(phases_configured=3, phases=3, min_current=6, max_current=16)
    assert lp.effective_min_power() == pytest.approx(2390.23, abs=0.01)
    assert lp.effective_max_power() == pytest.approx(6373.94, abs=0.01)
    assert lp.effective_step_power() == pytest.approx(398.37, abs=0.01)


@pytest.mark.parametrize(
    ("it", "currents", "phases"),
    [
        (False, [10, 10, 0], 2),
        (True, [10, 10, 0], 1),
        (True, [0, 10, 10], 1),
        (True, [10, 10, 10], 3),
    ],
)
def test_phases_from_charge_currents_grid_it(it_grid, it, currents, phases):
    # evcc core/loadpoint_grid_test.go: TestPhasesFromChargeCurrentsGridIT
    grid.set_no_neutral(it)
    lp = Loadpoint(clock=MockClock(), status=C, charge_currents=currents)
    lp.phases_from_charge_currents()
    assert lp.get_measured_phases() == phases


# --- evcc core/loadpoint_alwayscharge_test.go ----------------------------------


def test_set_mode_legacy_aliases():
    # evcc core/loadpoint_alwayscharge_test.go: TestSetModeLegacyAliases
    lp = new_loadpoint()

    # minpv maps to smart and enables always charge
    lp.set_mode(ChargeMode.MINPV)
    assert lp.get_mode() == ChargeMode.SMART
    assert lp.get_always_charge() == AlwaysCharge.ON

    # smart leaves always charge untouched
    lp.set_mode(ChargeMode.OFF)
    lp.set_mode(ChargeMode.SMART)
    assert lp.get_always_charge() == AlwaysCharge.ON

    # pv maps to smart and disables always charge, even when mode is already smart
    lp.set_mode(ChargeMode.PV)
    assert lp.get_mode() == ChargeMode.SMART
    assert lp.get_always_charge() == AlwaysCharge.OFF

    # mode change does not clear always charge
    lp.set_always_charge(AlwaysCharge.ONCE)
    lp.set_mode(ChargeMode.NOW)
    assert lp.get_always_charge() == AlwaysCharge.ONCE


def test_always_charge_once_resets_on_disconnect():
    # evcc core/loadpoint_alwayscharge_test.go: TestAlwaysChargeOnceResetsOnDisconnect
    lp = new_loadpoint()
    lp.set_always_charge(AlwaysCharge.ONCE)
    lp.ev_vehicle_disconnect_handler()
    assert lp.get_always_charge() == AlwaysCharge.OFF

    lp.set_always_charge(AlwaysCharge.ON)
    lp.ev_vehicle_disconnect_handler()
    assert lp.get_always_charge() == AlwaysCharge.ON


@pytest.mark.parametrize(
    ("mode", "want_mode", "want_ac"),
    [
        (ChargeMode.MINPV, ChargeMode.SMART, AlwaysCharge.ON),
        (ChargeMode.PV, ChargeMode.SMART, AlwaysCharge.OFF),
        (ChargeMode.SMART, ChargeMode.SMART, None),
        (ChargeMode.NOW, ChargeMode.NOW, None),
    ],
)
def test_normalize_mode(mode, want_mode, want_ac):
    # evcc core/loadpoint_alwayscharge_test.go: TestNormalizeMode (feature-charger rows excluded)
    lp = Loadpoint()
    assert lp.normalize_mode(mode) == (want_mode, want_ac)


def test_set_default_mode_legacy_aliases():
    # evcc core/loadpoint_alwayscharge_test.go: TestSetDefaultModeLegacyAliases
    lp = new_loadpoint()
    for mode in (ChargeMode.MINPV, ChargeMode.PV):
        lp.set_default_mode(mode)
        assert lp.get_default_mode() == ChargeMode.SMART
        assert lp.get_always_charge() == AlwaysCharge.OFF


def test_legacy_default_mode_seeds_always_charge():
    # evcc core/loadpoint_alwayscharge_test.go: TestLegacyDefaultModeSeedsAlwaysCharge
    # (first boot only; settings persistence is excluded)
    lp = new_loadpoint_from_config(make_charger(), mode="minpv")
    assert lp.get_mode() == ChargeMode.SMART
    assert lp.get_default_mode() == ChargeMode.SMART
    assert lp.get_always_charge() == AlwaysCharge.ON


def test_effective_current_measured_plus_2a():
    # own test (no evcc test covers it): evcc core/loadpoint.go effectiveCurrent -
    # a vehicle staying below its target counts with its measured current + 2 A, capped at the offer
    lp = Loadpoint(status=C, offered_current=10, charge_currents=[6.5, 0, 0])
    assert lp.effective_current() == 8.5
    lp.charge_currents = [9.5, 9.4, 9.6]
    assert lp.effective_current() == 10
    lp.charge_currents = None
    assert lp.effective_current() == 10
    lp.status = B
    assert lp.effective_current() == 0


@pytest.mark.parametrize(
    ("caps", "configured", "want_configured", "want_phases"),
    [
        ((), 1, 1, 1),  # fixed 1p charger configured 1p: phases follow (setPhasesConfigured)
        ((), 3, 3, 3),
        ((), 0, 3, 3),  # 0 on a charger without switching is ignored (v > 0 || hasPhaseSwitching)
        ((), None, 3, 3),  # not configured: 3p default
        (("switcher",), 1, 1, 0),  # 1p3p charger: configuration, physical state unknown
        (("switcher",), 0, 0, 0),
    ],
)
def test_phases_configured_restored(caps, configured, want_configured, want_phases):
    # review item 2 - evcc core/loadpoint.go restoreSettings: PhasesConfigured is applied when
    # v > 0 || hasPhaseSwitching, via setPhasesConfigured (core/loadpoint_phases.go)
    lp = new_loadpoint_from_config(make_charger(*caps), phases_configured=configured)
    assert lp.phases_configured == want_configured
    assert lp.phases == want_phases


def test_fixed_1p_charger_enable_threshold():
    # review item 2 - a fixed-1p charger configured 1p must enable at 1p min power surplus
    lp = new_loadpoint_from_config(make_charger(), clock=MockClock(), phases_configured=1)
    lp.enable.delay = 0
    lp.status = B
    assert lp.pv_max_current(-grid.current_to_power(MIN_A, 1), 0) == MIN_A


class _PlainCharger:
    """evcc api.Charger only (MaxCurrent(int64)): no ChargerEx."""

    def __init__(self):
        self.calls = []

    def status(self):
        return C

    def enabled(self):
        return True

    def enable(self, enable):
        self.calls.append(("enable", enable))

    def max_current(self, current):
        self.calls.append(("max_current", current))


def test_coarse_current_default():
    # review item 3 - evcc core/loadpoint.go coarseCurrent/roundedCurrent: whole amps unless ChargerEx
    charger = _PlainCharger()
    lp = Loadpoint(clock=MockClock(), charger=charger, min_current=6, max_current=16, status=C, enabled=True)
    assert lp.coarse_current()
    lp.set_limit(7.9)
    assert charger.calls == [("max_current", 7)]
    assert isinstance(charger.calls[0][1], int)
    assert lp.offered_current == 7


def test_charger_ex_millis():
    # review item 3 - evcc core/loadpoint.go setLimit: ChargerEx.MaxCurrentMillis gets the fractional current
    charger = make_charger("millis", enabled=True)
    lp = Loadpoint(clock=MockClock(), charger=charger, min_current=6, max_current=16, status=C, enabled=True)
    assert not lp.coarse_current()
    lp.set_limit(7.9)
    assert actions(charger) == [("max_current_millis", 7.9)]


@pytest.mark.parametrize(
    ("desc", "change"),
    [
        ("always charge", lambda lp: lp.set_always_charge(AlwaysCharge.ON)),
        ("enable threshold", lambda lp: lp.set_enable_threshold(-500)),
        ("disable threshold", lambda lp: lp.set_disable_threshold(200)),
    ],
)
def test_setters_request_update(desc, change):
    # review item 5 - evcc core/loadpoint_api.go setAlwaysCharge (:223-243) and setThresholds
    # (:608-615, via SetEnableThreshold/SetDisableThreshold) call requestUpdate
    ch = queue.Queue(maxsize=1)
    lp = new_loadpoint()
    lp.lp_chan = ch
    change(lp)
    assert ch.qsize() == 1, desc


def test_unchanged_threshold_no_update():
    # review item 5 - evcc SetEnableThreshold/SetDisableThreshold act only on a change
    ch = queue.Queue(maxsize=1)
    lp = new_loadpoint()
    lp.lp_chan = ch
    lp.set_enable_threshold(0)
    lp.set_disable_threshold(0)
    assert ch.qsize() == 0


def test_threshold_config_copied_by_value():
    # review item 6 - Go copies ThresholdConfig structs by value; loadpoints must not share one
    shared_enable = ThresholdConfig(threshold=-500, delay=MINUTE)
    shared_disable = ThresholdConfig(threshold=0, delay=3 * MINUTE)
    lp1 = new_loadpoint_from_config(make_charger(), enable=shared_enable, disable=shared_disable)
    lp2 = new_loadpoint_from_config(make_charger(), enable=shared_enable, disable=shared_disable)
    lp3 = Loadpoint(enable=shared_enable, disable=shared_disable)
    lp1.set_enable_delay(10)
    lp1.set_disable_threshold(300)
    lp3.enable.threshold = -1000
    assert lp2.enable == ThresholdConfig(threshold=-500, delay=MINUTE)
    assert lp2.disable == ThresholdConfig(threshold=0, delay=3 * MINUTE)
    assert shared_enable == ThresholdConfig(threshold=-500, delay=MINUTE)


@pytest.mark.parametrize("bad", [ChargeStatus.E, ChargeStatus.NONE, "F"])
def test_invalid_status_skips_cycle(bad):
    # review item 7 - evcc drivers either return an error for E/F (api.ChargeStatusString)
    # or map E to A; a charger status error makes evcc's Update log and skip the cycle.
    # Contract here: status() returns only A/B/C; anything else is treated as a read error.
    charger = make_charger(status=C, enabled=True)
    lp = Loadpoint(
        clock=MockClock(), charger=charger, mode=ChargeMode.NOW, min_current=MIN_A, max_current=MAX_A, status=C
    )
    attach_listeners(lp)
    charger.status_value = bad
    lp.update(0, 0)
    assert actions(charger) == []
    assert lp.get_status() == C
    with pytest.raises(ValueError, match="invalid status"):
        lp.get_status_changes()
