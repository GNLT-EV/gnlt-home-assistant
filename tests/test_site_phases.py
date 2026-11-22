"""Site core loadpoint phases: active/min/max phases, 1p/3p switching.

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Source: evcc core/loadpoint_phases_test.go. Vehicles are excluded from the
port, so rows of ``phaseTests`` with a vehicle phase count other than 0
(unknown) are not ported.
"""

import pytest

from gnlt_charger.site import grid
from gnlt_charger.site.api import ChargeMode, ChargeStatus, NotAvailableError
from gnlt_charger.site.clock import ELAPSED, HOUR, MINUTE, SECOND, ZERO_TIME, MockClock, is_zero
from gnlt_charger.site.loadpoint import UNKNOWN_PHASES, Loadpoint, ThresholdConfig, new_loadpoint
from site_fakes import MAX_A, MIN_A, FakeCircuit, make_charger

V = grid.VOLTAGE
B, C = ChargeStatus.B, ChargeStatus.C

# capable=0 signals 1p3p as set during loadpoint init
# physical/vehicle=0 signals unknown, measured<>0 signals previous measurement
# (capable, physical, vehicle, act_expected, max_expected, min_expected, scale)
PHASE_TESTS_ALL = [
    # 1p
    (1, 1, 0, 0, 1, 1, 1, ""),
    (1, 1, 0, 1, 1, 1, 1, ""),
    (1, 1, 1, 0, 1, 1, 1, ""),
    (1, 1, 2, 0, 1, 1, 1, ""),
    (1, 1, 3, 0, 1, 1, 1, ""),
    # 3p
    (3, 3, 0, 0, UNKNOWN_PHASES, 3, 3, ""),
    (3, 3, 0, 1, 1, 1, 1, ""),
    (3, 3, 0, 2, 2, 2, 2, ""),
    (3, 3, 0, 3, 3, 3, 3, ""),
    (3, 3, 1, 0, 1, 1, 1, ""),
    (3, 3, 2, 0, 2, 2, 2, ""),
    (3, 3, 3, 0, 3, 3, 3, ""),
    # 1p3p initial
    (0, 0, 0, 0, UNKNOWN_PHASES, 3, 1, "du"),
    (0, 0, 0, 1, 1, 3, 1, "u"),
    (0, 0, 0, 2, 2, 3, 1, "du"),
    (0, 0, 0, 3, 3, 3, 1, "du"),
    (0, 0, 1, 0, 1, 1, 1, ""),
    (0, 0, 2, 0, 2, 2, 1, "du"),
    (0, 0, 3, 0, 3, 3, 1, "du"),
    # 1p3p, 1 currently active
    (0, 1, 0, 0, 1, 3, 1, "u"),
    (0, 1, 0, 1, 1, 3, 1, "u"),
    (0, 1, 1, 0, 1, 1, 1, ""),
    (0, 1, 2, 0, 1, 2, 1, "u"),
    (0, 1, 3, 0, 1, 3, 1, "u"),
    # 1p3p, 3 currently active
    (0, 3, 0, 0, UNKNOWN_PHASES, 3, 1, "d"),
    (0, 3, 0, 1, 1, 1, 1, ""),
    (0, 3, 0, 2, 2, 2, 1, "d"),
    (0, 3, 0, 3, 3, 3, 1, "d"),
    (0, 3, 1, 0, 1, 1, 1, ""),
    (0, 3, 2, 0, 2, 2, 1, "d"),
    (0, 3, 3, 0, 3, 3, 1, "d"),
]
# vehicles are excluded: only the rows with unknown vehicle phases
PHASE_TESTS = [tc for tc in PHASE_TESTS_ALL if tc[2] == 0]


def _charger_for(capable, **kw):
    return make_charger("switcher", **kw) if capable == 0 else make_charger(**kw)


@pytest.mark.parametrize("configured", [0, 1, 3])
@pytest.mark.parametrize("tc", PHASE_TESTS)
def test_max_active_phases(configured, tc):
    # evcc core/loadpoint_phases_test.go: TestMaxActivePhases
    capable, physical, _vehicle, measured, _act, max_expected, _min, _scale = tc
    if configured == 0 and capable != 0:
        pytest.skip("invalid config (free scaling for simple charger)")
    lp = Loadpoint(
        phases_configured=configured, phases=physical, measured_phases=measured, charger=_charger_for(capable)
    )
    expected = max_expected
    if capable == 0 and 0 < configured < max_expected:
        expected = configured
    assert lp.max_active_phases() == expected


@pytest.mark.parametrize("configured", [0, 1, 3])
@pytest.mark.parametrize("tc", PHASE_TESTS)
def test_min_active_phases(configured, tc):
    # evcc core/loadpoint_phases_test.go: TestMinActivePhases
    capable, physical, _vehicle, measured, _act, _max, min_expected, _scale = tc
    if configured == 0 and capable != 0:
        pytest.skip("invalid config (free scaling for simple charger)")
    if configured != 0 and capable != 0 and configured != physical:
        pytest.skip("physical config different than configured")
    lp = Loadpoint(
        phases_configured=configured, phases=physical, measured_phases=measured, charger=_charger_for(capable)
    )
    expected = min_expected
    if capable == 0 and configured > 1:
        expected = lp.max_active_phases()
    assert lp.min_active_phases() == expected


@pytest.mark.parametrize(("configured", "expected"), [(0, 1380), (1, 1380), (3, 4140)])
def test_effective_min_power_fixed_phases(configured, expected):
    # evcc core/loadpoint_phases_test.go: TestEffectiveMinPowerFixedPhases
    lp = Loadpoint(phases_configured=configured, min_current=6, charger=make_charger("switcher"))
    assert lp.effective_min_power() == expected


def _test_scale(lp, site_power, direction, tc):
    """evcc core/loadpoint_phases_test.go: testScale."""
    scale = tc[7]
    test_direction = direction[0]
    expectation = scale
    if test_direction == "u" and test_direction in expectation:
        # scale-up should only execute when the 1p max current is exceeded
        max_amp = -site_power / V
        if max_amp < MAX_A:
            scaled = lp.pv_scale_phases(site_power, MIN_A, max_amp - 0.0001, True)
            assert scaled == 3, f"{tc} missing scale {direction} at reduced max current {max_amp:.1f}A"
            expectation = expectation.replace(test_direction, "")

    scaled = lp.pv_scale_phases(site_power, MIN_A, MAX_A, True)
    if test_direction in expectation:
        assert scaled != 0, f"{tc} missing scale {direction}"
    else:
        assert scaled == 0, f"{tc} unexpected scale {direction}"


@pytest.mark.parametrize("tc", PHASE_TESTS)
def test_pv_scale_phases(tc):
    # evcc core/loadpoint_phases_test.go: TestPvScalePhases
    capable, physical, _vehicle, measured, act_expected, max_expected, _min, _scale = tc
    charger = _charger_for(capable, enabled=True)
    lp = Loadpoint(
        clock=MockClock(),
        charger=charger,
        mode=ChargeMode.NOW,
        min_current=MIN_A,
        max_current=MAX_A,
        phases_configured=0,  # allow switching
        phases=physical,
        status=C,
    )
    lp.prepare()
    assert charger.called("max_current") == [(MIN_A,)]  # MaxCurrentEx not implemented

    lp.measured_phases = measured
    assert lp.phases == physical, "wrong phases"
    assert lp.active_phases() == act_expected, "expected active phases"
    assert lp.max_active_phases() == max_expected, "expected max active phases"

    if capable != 0:
        return

    # scale down
    charger.clear()
    lp.phase_timer = ZERO_TIME
    lp.charge_power = float(lp.active_phases()) * MIN_A * V  # charging at min current
    _test_scale(lp, 0.1, "down", tc)
    assert len(charger.called("enable")) <= 1 and set(charger.called("enable")) <= {(False,)}
    assert len(charger.called("phases1p3p")) <= 1 and set(charger.called("phases1p3p")) <= {(1,)}

    # scale up
    charger.clear()
    min3p = float(max_expected) * MIN_A * V
    lp.phase_timer = ZERO_TIME
    lp.phases = physical  # reset to initial state
    lp.measured_phases = measured
    _test_scale(lp, -min3p, "up", tc)
    assert len(charger.called("enable")) <= 1 and set(charger.called("enable")) <= {(False,)}
    assert len(charger.called("phases1p3p")) <= 1 and set(charger.called("phases1p3p")) <= {(3,)}


def _prep_running(lp):
    lp.phase_timer = lp.clock.now()


def _with(**fields):
    def prepare(lp):
        for key, value in fields.items():
            if value == "now":
                value = lp.clock.now()
            setattr(lp, key, value)

    return prepare


PV_TIMER_TESTS = [
    # switch up from 1p/1p configured/active
    ("1/1->3, not enough power", 1, 1, 0, 1, 0, None),
    ("1/1->3, kickoff", 1, 1, -3 * V * MIN_A, 1, 0, _with(phase_timer=ZERO_TIME)),
    ("1/1->3, timer running", 1, 1, -3 * V * MIN_A, 1, 0, _with(phase_timer="now")),
    ("1/1->3, timer elapsed", 1, 1, -3 * V * MIN_A, 3, 3, _with(phase_timer=ELAPSED)),
    # omit to switch up (again) from 3p/1p configured/active
    ("3/1->3, not enough power", 3, 1, 0, 3, 0, None),
    ("3/1->3, kickoff", 3, 1, -3 * V * MIN_A, 3, 0, _with(phase_timer=ZERO_TIME)),
    ("3/1->3, timer running", 3, 1, -3 * V * MIN_A, 3, 0, _with(phase_timer="now")),
    ("3/1->3, timer elapsed", 3, 1, -3 * V * MIN_A, 3, 0, _with(phase_timer=ELAPSED)),
    # omit to switch down from 3p/1p configured/active
    ("3/1->1, not enough power", 3, 1, 0, 3, 0, None),
    ("3/1->1, kickoff", 3, 1, -1 * V * MIN_A, 3, 0, _with(phase_timer=ZERO_TIME)),
    ("3/1->1, timer running", 3, 1, -1 * V * MIN_A, 3, 0, _with(phase_timer="now")),
    ("3/1->1, timer elapsed", 3, 1, -1 * V * MIN_A, 3, 0, _with(phase_timer=ELAPSED)),
    # switch down from 3p/3p configured/active
    ("3/3->1, enough power", 3, 3, 0, 3, 0, None),
    (
        "3/3->1, enough power, timer elapsed, load point enabled",
        3, 3, 0, 3, 0, _with(phase_timer=ELAPSED, enabled=True),
    ),
    # a disabled charger switches when it is enabled (#35165)
    (
        "3/3->1, not enough power, timer elapsed, load point disabled",
        3, 3, 0.1, 3, 0, _with(phase_timer=ELAPSED, enabled=False),
    ),
    ("3/3->1, kickoff", 3, 3, 0.1, 3, 0, _with(phase_timer=ZERO_TIME, charge_power=3 * V * MIN_A)),
    ("3/3->1, timer running", 3, 3, 0.1, 3, 0, _with(phase_timer="now", charge_power=3 * V * MIN_A)),
    ("3/3->1, timer elapsed", 3, 3, 0.1, 1, 1, _with(phase_timer=ELAPSED, charge_power=3 * V * MIN_A)),
    # charging with insufficient power for 1p: disable instead of scaling down
    ("3/3->1, insufficient for 1p, charging", 3, 3, 0.1, 3, 0, _with(phase_timer=ELAPSED, enabled=True)),
    (
        "3/3->1, sufficient for 1p, charging",
        3, 3, 3 * V * MIN_A / 2, 1, 1, _with(phase_timer=ELAPSED, enabled=True, charge_power=3 * V * MIN_A),
    ),
    # minpv never disables, so scale down even if 1p is not sustainable (#33208)
    (
        "3/3->1, insufficient for 1p, charging, minpv",
        3, 3, 0.1, 1, 1, _with(phase_timer=ELAPSED, enabled=True, mode=ChargeMode.MINPV),
    ),
    # (evcc row "climater active" not ported: vehicles excluded)
    # enabled but not yet charging: timers apply, disable instead of scaling down without 1p surplus
    ("3/0->1, not enough power, not charging", 3, 0, 0.1, 3, 0, _with(phase_timer=ELAPSED, status=B)),
    ("1/0->3, enough power, not charging, kickoff", 1, 0, -3 * V * MIN_A, 1, 0, _with(status=B)),
    (
        "1/0->3, enough power, not charging, timer elapsed",
        1, 0, -3 * V * MIN_A, 3, 3, _with(phase_timer=ELAPSED, status=B),
    ),
    # disabled: no switch, pvEnablePhases decides when enabling
    ("3/0->1, not enough power, disabled", 3, 0, 0.1, 3, 0, _with(status=B, enabled=False)),
    ("1/0->3, enough power, disabled", 1, 0, -3 * V * MIN_A, 1, 0, _with(status=B, enabled=False)),
    # error states from 1p/3p misconfiguration - no correction for time being (stay at 1p)
    ("1/3->1, enough power", 1, 3, -1 * V * MAX_A, 1, 0, None),
    ("1/3->1, kickoff, correct phase setting", 1, 3, 0.1, 1, 0, _with(phase_timer=ZERO_TIME)),
    ("1/3->1, timer running, correct phase setting", 1, 3, 0.1, 1, 0, _with(phase_timer="now")),
    ("1/3->1, switch not executed", 1, 3, 0.1, 1, 0, _with(phase_timer=ELAPSED)),
]


@pytest.mark.parametrize(
    ("desc", "phases", "measured", "site_power", "to_phases", "res", "prepare"),
    PV_TIMER_TESTS,
    ids=[t[0] for t in PV_TIMER_TESTS],
)
def test_pv_scale_phases_timer(desc, phases, measured, site_power, to_phases, res, prepare):
    # evcc core/loadpoint_phases_test.go: TestPvScalePhasesTimer
    clock = MockClock()
    clock.add(HOUR)  # avoid time.IsZero
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        phases=phases,
        measured_phases=measured,
        status=C,
        enabled=True,
        enable=ThresholdConfig(delay=MINUTE),
        disable=ThresholdConfig(delay=MINUTE),
    )
    if prepare is not None:
        prepare(lp)

    got = lp.pv_scale_phases(site_power, MIN_A, MAX_A, lp.mode != ChargeMode.MINPV)

    assert got == res, desc
    assert lp.phases == to_phases, desc
    assert charger.called("phases1p3p") == ([(to_phases,)] if res != 0 else []), desc


SITE = 500.0


@pytest.mark.parametrize(
    ("desc", "phases", "charge_power", "projected"),
    [
        ("at min current", 3, 3 * V * MIN_A, SITE - 2 * V * MIN_A),
        ("above min current", 3, 3 * V * (MIN_A + 1), SITE - 3 * V * (MIN_A + 1) + V * MIN_A),
        ("below min power", 3, V * MIN_A, SITE - 2 * V * MIN_A),
        # a scale up pending at 1p leaves site power alone
        ("scale up at 1p", 1, V * (MIN_A + 1), SITE),
    ],
)
def test_project_phase_switch(desc, phases, charge_power, projected):
    # evcc core/loadpoint_phases_test.go: TestProjectPhaseSwitch
    lp = Loadpoint(
        clock=MockClock(),
        charger=make_charger("switcher"),
        phases=phases,
        measured_phases=phases,
        charge_power=charge_power,
        phase_timer=1.0,  # time.Unix(1, 0)
        enabled=True,
    )
    assert lp.project_phase_switch(SITE, MIN_A) == (projected, 1), desc


@pytest.mark.parametrize(
    ("dflt", "physical", "max_expected"),
    [(0, 0, 3), (0, 1, 3), (0, 3, 3), (1, 0, 1), (1, 1, 1), (1, 3, 1), (3, 0, 3), (3, 1, 3), (3, 3, 3)],
)
def test_scale_phases_if_available(dflt, physical, max_expected):
    # evcc core/loadpoint_phases_test.go: TestScalePhasesIfAvailable
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=MockClock(), charger=charger, min_current=MIN_A, phases_configured=dflt, phases=physical
    )
    lp.scale_phases_if_available(3)
    # evcc sets the expectation "if dflt == 0 || dflt != physical", but its shared gomock
    # controller verifies missing calls only at the first ctrl.Finish(), so for the row
    # (0, 3, 3) it is never enforced; evcc's scalePhases switches only when the enabled
    # phases differ from the target, which is what is asserted here.
    if physical != max_expected:
        assert charger.called("phases1p3p") == [(max_expected,)]
    else:
        assert charger.called("phases1p3p") == []


def test_scale_phases_not_available():
    # evcc core/loadpoint_phases_test.go: TestScalePhasesNotAvailable
    charger = make_charger("switcher")
    charger.errors["phases1p3p"] = NotAvailableError()
    lp = Loadpoint(clock=MockClock(), charger=charger, phases=1)
    with pytest.raises(NotAvailableError):
        lp.scale_phases(3)
    # switch did not complete - phase count unchanged
    assert lp.get_phases() == 1


def test_scale_phases_up_min_current():
    # evcc core/loadpoint_phases_test.go: TestScalePhasesUpMinCurrent
    charger = make_charger("switcher")
    lp = new_loadpoint()
    lp.clock = MockClock()
    lp.charger = charger
    lp.min_current = MIN_A
    lp.max_current = MAX_A
    lp.enabled = True
    lp.offered_current = MAX_A
    lp.phases = 1

    lp.scale_phases(3)
    assert charger.calls == [("max_current", MIN_A), ("phases1p3p", 3)]
    assert lp.get_phases() == 3
    assert lp.offered_current == MIN_A


@pytest.mark.parametrize(
    ("desc", "configured", "phases", "expected"),
    [
        ("auto, 3p active scales down to 1p", 0, 3, 1),
        ("auto, already 1p", 0, 1, 1),
        ("fixed 3p cannot scale down", 3, 3, 3),
        ("fixed 1p stays 1p", 1, 1, 1),
    ],
)
def test_min_charging_phase_scaling(desc, configured, phases, expected):
    # evcc core/loadpoint_phases_test.go: TestMinChargingPhaseScaling
    lp = new_loadpoint()
    lp.min_current = 6
    lp.max_current = 16
    lp.phases_configured = configured
    lp.phases = phases
    lp.offered_current = 0  # ensure MaxCurrent is called
    charger = make_charger("switcher", enabled=True)
    lp.charger = charger

    lp.min_charging()

    assert charger.called("phases1p3p") == ([(expected,)] if phases != expected else [])
    assert charger.called("max_current") == [(lp.min_current,)]
    assert lp.phases == expected, desc
    assert is_zero(lp.phase_timer), "phase timer must not remain active"


# desc, configured, active, measured, charge_power, status, available, expected, no_circuit, in_progress, timer_running
FAST = [
    ("no circuit", 0, 3, 0, 0, B, 0, 3, True, False, True),
    # without load management scaling up cannot overload anything- no delay
    ("no circuit, charging at 1p", 0, 1, 0, 3680, C, 0, 3, True, False, True),
    # unknown phases after restart must not prevent switching
    ("unknown phases, high limit", 0, 0, 0, 0, B, 11040, 3, False, False, False),
    ("unknown phases, low limit", 0, 0, 0, 0, B, 3680, 1, False, False, False),
    # measured phases substitute the unknown enabled phase count, scale up remains delayed
    ("unknown phases, charging 1p measured, high limit", 0, 0, 1, 3680, C, 22080, 3, False, False, False),
    ("unknown phases, charging 2p measured, low limit", 0, 0, 2, 3680, C, 3680, 1, False, False, False),
    ("fixed 1p, 1p active", 1, 1, 0, 0, B, 11040, 1, False, False, False),
    ("fixed 1p, 3p active", 1, 3, 0, 0, B, 11040, 1, False, False, False),
    ("fixed 3p, 3p active", 3, 3, 0, 0, B, 3680, 3, False, False, False),
    ("fixed 3p, 1p active", 3, 1, 0, 0, B, 3680, 3, False, False, False),
    ("low limit, no surplus", 0, 3, 0, 0, B, 3680, 1, False, False, True),
    ("low limit, with surplus", 0, 1, 0, 0, B, 11040, 3, False, False, False),
    ("charging, low limit", 0, 3, 0, 3680, C, 3680, 1, False, False, False),
    ("charging, high limit", 0, 1, 0, 3680, C, 11040, 3, False, False, False),
    ("switching down just below 3p minimum", 0, 3, 0, 0, B, 4140 - 1, 1, False, False, False),
    ("exactly at 3p minimum", 0, 3, 0, 0, B, 4140, 3, False, False, False),
    # the 10% scale up buffer is expected literally- deriving it from phaseScaleUpBuffer would be circular
    ("switching up at 3p minimum plus buffer", 0, 1, 0, 0, B, 230 * 1.1 * 6 * 3, 3, False, False, False),
    (
        "edge case: staying at 1p at 3p minimum plus buffer - 1",
        0, 1, 0, 0, B, 230 * 1.1 * 6 * 3 - 1, 1, False, False, True,
    ),
    ("phase switch in progress", 0, 1, 0, 0, B, 11040, 1, False, True, True),
    # measured phases must not be mistaken for the enabled phase count
    ("2p vehicle on 3p, low limit", 0, 3, 2, 3680, C, 3680, 1, False, False, False),
    ("1p vehicle on 3p, high limit", 0, 3, 1, 3680, C, 11040, 3, False, False, False),
]


@pytest.mark.parametrize(
    ("desc", "configured", "active", "measured", "charge_power", "status", "available", "expected",
     "no_circuit", "in_progress", "timer_running"),
    FAST,
    ids=[t[0] for t in FAST],
)
def test_fast_charging_circuit_based_phase_scaling(
    desc, configured, active, measured, charge_power, status, available, expected, no_circuit, in_progress,
    timer_running,
):
    # evcc core/loadpoint_phases_test.go: TestFastChargingCircuitBasedPhaseScaling
    clock = MockClock()
    clock.add(HOUR)  # avoid IsZero issues
    lp = new_loadpoint()
    lp.clock = clock
    lp.min_current = 6
    lp.max_current = 16
    lp.enable.delay = MINUTE
    lp.phases_configured = configured
    lp.phases = active
    lp.measured_phases = measured
    lp.charge_power = charge_power
    lp.status = status
    lp.enabled = status == C
    lp.offered_current = 0  # ensure MaxCurrent is called

    if in_progress:
        lp.phases_switched = clock.now()
    # a pending pv scale timer must not survive fast charging
    if timer_running:
        lp.phase_timer = clock.now()

    charger = make_charger("switcher", enabled=True)
    lp.charger = charger
    if not no_circuit:
        # ValidatePower caps the request at the available circuit power
        lp.circuit = FakeCircuit(
            validate_power=lambda _old, new: min(new, available), expect_power_old=charge_power
        )

    lp.fast_charging()

    # handle scale phases up delay
    at1p = active == 1 or (active == 0 and measured == 1)
    if not no_circuit and at1p and expected == 3 and status == C:
        assert lp.phases == active, "should not scale up immediately while charging"
        clock.add(lp.enable.delay - SECOND)
        lp.fast_charging()
        assert lp.phases == active, "should not scale up before delay elapsed"
        clock.add(SECOND)
        lp.fast_charging()

    assert lp.phases == expected, desc
    assert is_zero(lp.phase_timer), "phase timer must not remain active"
    switched = charger.called("phases1p3p")
    if active != expected:
        assert switched == [(expected,)]
    else:
        assert switched == []


def test_update_phase_switch_not_available():
    # evcc core/loadpoint_phases_test.go: TestUpdatePhaseSwitchNotAvailable
    charger = make_charger("switcher", status=C, enabled=True)
    charger.errors["phases1p3p"] = NotAvailableError()
    lp = Loadpoint(
        clock=MockClock(),
        charger=charger,
        mode=ChargeMode.NOW,
        min_current=MIN_A,
        max_current=MAX_A,
        status=C,
        phases_configured=3,  # fixed 3p
        phases=0,  # unknown
    )
    lp.prepare()

    # first cycle attempts the switch, gets api.ErrNotAvailable, adopts 3p
    lp.update(0, 0)
    assert lp.get_phases() == 3, "configured phases should be adopted"

    # second cycle must not attempt the switch again
    lp.update(0, 0)
    assert lp.get_phases() == 3
    assert charger.called("phases1p3p") == [(3,)]


@pytest.mark.parametrize(
    ("desc", "phases", "site_power", "circuit_current", "circuit_power", "expected"),
    [
        # 5520W available equals 24A on 1p, below the 32A loadpoint limit
        ("no circuit limit", 1, -5520, 0, 0, 0),
        ("current limit below 1p target", 1, -5520, 20, 0, 3),
        ("power limit below 3p minimum", 1, -5520, 16, 4000, 0),
        ("current limit below minimum", 1, -5520, 2, 0, 0),
        # 11040W available equals 16A on 3p, 4140W required for 3p minimum
        ("3p, no circuit limit", 3, -11040, 0, 0, 0),
        ("3p, power limit below 3p minimum", 3, -11040, 0, 3000, 1),
    ],
)
def test_pv_scale_phases_circuit_limits(desc, phases, site_power, circuit_current, circuit_power, expected):
    # evcc core/loadpoint_phases_test.go: TestPvScalePhasesCircuitLimits
    # (the evcc test's 3p vehicle equals "unknown" here: both count as 3 phases)
    charger = make_charger("switcher", enabled=True)
    circuit = FakeCircuit(
        validate_current=lambda _old, cur: cur if circuit_current == 0 else min(cur, circuit_current),
        validate_power=lambda _old, power: power if circuit_power == 0 else min(power, circuit_power),
    )
    lp = Loadpoint(
        clock=MockClock(),
        charger=charger,
        mode=ChargeMode.NOW,
        min_current=6,
        max_current=32,
        phases=phases,
        measured_phases=phases,
        status=C,
        enabled=True,
        circuit=circuit,
    )
    assert lp.pv_scale_phases(site_power, lp.min_current, lp.max_current, True) == expected, desc
    # evcc's mocks allow these AnyTimes; the exact writes are asserted here
    writes = [c for c in charger.calls if c[0] in ("enable", "max_current", "max_current_millis", "phases1p3p")]
    if expected == 3:
        # scaling up while enabled drops to min current first
        assert writes == [("max_current", 6), ("phases1p3p", 3)], desc
    elif expected == 1:
        assert writes == [("phases1p3p", 1)], desc
    else:
        assert writes == [], desc


MIN1P = grid.current_to_power(MIN_A, 1)
MIN3P = grid.current_to_power(MIN_A, 3)


@pytest.mark.parametrize(
    ("desc", "phases", "site_power", "current", "to_phases"),
    [
        ("3p, no surplus", 3, 500, 0, 3),
        ("3p, 1p surplus", 3, -MIN1P, MIN_A, 1),
        ("3p, 3p surplus", 3, -MIN3P, MIN_A, 3),
        ("1p, 1p surplus", 1, -MIN1P, MIN_A, 1),
        ("1p, 3p surplus above 1p max", 1, -MIN3P, MIN_A, 3),
    ],
)
def test_pv_enable_phases(desc, phases, site_power, current, to_phases):
    # evcc core/loadpoint_phases_test.go: TestPvEnablePhases
    clock = MockClock()
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        phases=phases,
        enable=ThresholdConfig(delay=MINUTE),
        solar_share=1,
        status=B,
    )
    # no switch while the enable timer runs
    assert lp.pv_max_current(site_power, 0) == 0
    assert lp.phases == phases
    assert charger.calls == [], "no charger call while the enable timer runs"

    clock.add(MINUTE)
    assert lp.pv_max_current(site_power, 0) == current
    assert lp.phases == to_phases
    assert charger.called("phases1p3p") == ([(to_phases,)] if to_phases != phases else [])


def test_pv_enable_phases_switch_failed():
    # evcc core/loadpoint_phases_test.go: TestPvEnablePhasesSwitchFailed
    clock = MockClock()
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        phases=3,
        enable=ThresholdConfig(delay=MINUTE),
        solar_share=1,
        status=B,
    )
    surplus = -grid.current_to_power(MIN_A, 1)
    assert lp.pv_max_current(surplus, 0) == 0

    # 3p min power would exceed the 1p surplus
    charger.errors["phases1p3p"] = NotAvailableError()
    clock.add(MINUTE)
    assert lp.pv_max_current(surplus, 0) == 0
    assert lp.phases == 3
    assert charger.called("phases1p3p") == [(1,)]


def test_pv_enable_phases_switch_settling():
    # evcc core/loadpoint_phases_test.go: TestPvEnablePhasesSwitchSettling
    clock = MockClock()
    charger = make_charger("switcher")
    lp = Loadpoint(
        clock=clock,
        charger=charger,
        min_current=MIN_A,
        max_current=MAX_A,
        phases=3,
        phases_switched=clock.now(),
        enable=ThresholdConfig(delay=30),
        solar_share=1,
        status=B,
    )
    surplus = -grid.current_to_power(MIN_A, 1)
    assert lp.pv_max_current(surplus, 0) == 0

    clock.add(30)
    assert lp.pv_max_current(surplus, 0) == 0, "switch not settled"
    assert lp.phases == 3
    assert charger.called("phases1p3p") == []

    clock.add(31)
    assert lp.pv_max_current(surplus, 0) == MIN_A, "enable timer must not restart"
    assert lp.phases == 1
    assert charger.called("phases1p3p") == [(1,)]
