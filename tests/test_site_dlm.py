"""DLM allocator (custom_components/gnlt_charger/site/dlm.py).

Behaviour source: a clean-room behaviour specification of openWB 2.x load
management (not part of this repository). Each test names the spec section it checks; tests for
behaviour openWB does not have are marked ADDITION.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from gnlt_charger.site.dlm import (
    DlmAllocator,
    DlmConfig,
    GridReading,
    MeterFailureAction,
    MeterState,
    Reason,
    StationInput,
    grid_phases,
    to_grid,
)

T0 = 10_000.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(**cfg):
    cfg.setdefault("fuse_limit_a", (25.0, 25.0, 25.0))
    clock = Clock()
    return DlmAllocator(DlmConfig(**cfg), clock=clock), clock


def meter(l1, l2, l3, t=T0, error=False):
    return GridReading(currents_a=(l1, l2, l3), updated_at=t, error=error)


def st(sid, **kw):
    """A fresh, online station; by default charging for 30 s at 6 A of 6 A."""
    kw.setdefault("requested_a", 16.0)
    kw.setdefault("setpoint_a", 6.0)
    kw.setdefault("measured_a", (6.0, 6.0, 6.0))
    kw.setdefault("charging_since", T0 - 30)
    kw.setdefault("last_seen", T0)
    return StationInput(station_id=sid, **kw)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def vec_close(a, b, tol=1e-6):
    return all(close(x, y, tol) for x, y in zip(a, b, strict=True))


# --- helpers: phase rotation -----------------------------------------


def test_s2_3_phase_rotation_tables():
    """§2.3: phase_1 = grid phase of station L1; n phases go to grid L(phase_1), next, next."""
    assert grid_phases(1, 3) == (0, 1, 2)
    assert grid_phases(2, 3) == (1, 2, 0)
    assert grid_phases(3, 3) == (2, 0, 1)
    assert grid_phases(2, 1) == (1,)
    assert grid_phases(3, 1) == (2,)
    # measured station currents to grid order: station L1 lands on grid L(phase_1)
    assert to_grid(2, (7.0, 8.0, 9.0)) == (9.0, 7.0, 8.0)
    assert to_grid(3, (7.0, 8.0, 9.0)) == (8.0, 9.0, 7.0)


# --- headroom, minimum and additional passes ------------------------------------


def test_s4_1_s5_two_3p_stations_25a_fuse_with_house_load():
    """§4.1 headroom = limit −; §5.1 minimums; §5.2 equal split.
    House 5 A, both cars at 6 A → meter 17 A, base 5 A, headroom 20 A → 10 A each."""
    dlm, _ = make()
    res = dlm.allocate(meter(17, 17, 17), [st("a"), st("b")])
    assert res.meter_state is MeterState.OK
    assert vec_close(res.headroom_start_a, (20, 20, 20))
    assert res.stations["a"].allowed_a == 10.0
    assert res.stations["b"].allowed_a == 10.0
    assert res.stations["a"].reason is Reason.CURRENT_LIMIT
    assert res.stations["b"].reason is Reason.CURRENT_LIMIT
    assert vec_close(res.headroom_left_a, (0, 0, 0))
    assert vec_close(res.stations["a"].booked_a, (10, 10, 10))


def test_s5_1_11a_for_two_6a_minimums_second_gets_zero():
    """§5.1: the minimum is tried in list order; one that does not fit gets 0 (pause)."""
    dlm, _ = make()
    idle = dict(measured_a=(0.0, 0.0, 0.0), setpoint_a=0.0, charging_since=None)
    res = dlm.allocate(meter(14, 14, 14), [st("a", **idle), st("b", **idle)])
    assert vec_close(res.headroom_start_a, (11, 11, 11))
    assert res.stations["a"].allowed_a == 6.0
    assert res.stations["b"].allowed_a == 0.0
    assert res.stations["b"].reason is Reason.MIN_NOT_FITTING
    # §5.2: a car that is not drawing is kept at its minimum in the additional pass
    assert res.stations["a"].reason is Reason.NOT_CHARGING


def test_s5_1_lower_priority_loses_the_minimum_not_earlier_one():
    """§5.1 step 4 + §2.6: the later item is refused; no pre-emption of the earlier one."""
    dlm, _ = make()
    idle = dict(measured_a=(0.0, 0.0, 0.0), setpoint_a=0.0, charging_since=None)
    res = dlm.allocate(meter(14, 14, 14), [st("b", priority=2, **idle), st("a", priority=1, **idle)])
    assert res.stations["a"].allowed_a == 6.0
    assert res.stations["b"].allowed_a == 0.0


def test_s2_6_strict_priority_first_item_full_before_next_minimum():
    """§2.6: the whole allocation runs per priority item; A takes 16 A before B gets 6 A.
    B's 6 A are still drawn this cycle, so A rises to 14 A now and 16 A next cycle."""
    dlm, clock = make()
    res = dlm.allocate(meter(17, 17, 17), [st("a", priority=0), st("b", priority=1)])
    assert res.stations["a"].allowed_a == 14.0
    assert res.stations["a"].reason is Reason.WAIT_DROP
    assert res.stations["b"].allowed_a == 0.0
    assert res.stations["b"].reason is Reason.MIN_NOT_FITTING
    clock.now += 10
    a = st("a", priority=0, setpoint_a=14.0, measured_a=(14.0,) * 3, last_seen=clock.now)
    b = st("b", priority=1, setpoint_a=0.0, measured_a=(0.0,) * 3, charging_since=None, last_seen=clock.now)
    res = dlm.allocate(meter(19, 19, 19, t=clock.now), [a, b])
    assert res.stations["a"].allowed_a == 16.0
    assert res.stations["a"].reason is Reason.NONE
    assert res.stations["b"].allowed_a == 0.0


def test_s5_2_equal_split_water_filling_one_car_capped_at_max():
    """§5.2: each load takes min(own need, equal share); A (max 10) needs less, B gets the rest."""
    dlm, _ = make()
    res = dlm.allocate(meter(12, 12, 12), [st("a", max_current_a=10.0), st("b")])
    assert res.stations["a"].allowed_a == 10.0
    assert res.stations["a"].reason is Reason.NONE
    assert res.stations["b"].allowed_a == 15.0
    assert res.stations["b"].reason is Reason.CURRENT_LIMIT


def test_s5_2_list_order_matters_capped_car_last_leaves_headroom_unused():
    """§5.2 'in list order': sequential filling does not hand back what a LATER load leaves.
    B first takes its equal share 6.5 A → 12.5 A; A takes 4 A → 10 A; 2.5 A stay unused.
    Characterisation of openWB behaviour (reported as an open point)."""
    dlm, _ = make()
    res = dlm.allocate(meter(12, 12, 12), [st("b"), st("a", max_current_a=10.0)])
    assert res.stations["b"].allowed_a == 12.5
    assert res.stations["a"].allowed_a == 10.0
    assert vec_close(res.headroom_left_a, (2.5, 2.5, 2.5))


def test_s5_2_three_way_split_floored_to_0_1a():
    """§5.2 + our 0.1 A step: 20 A of extra headroom over three cars → 6 + 4.666… → 10.6 A (floor)."""
    dlm, _ = make(fuse_limit_a=(38.0, 38.0, 38.0))
    res = dlm.allocate(meter(18, 18, 18), [st("a"), st("b"), st("c")])
    # headroom 38, minimums 18, rest 20 → 6.666… each
    for sid in "abc":
        assert res.stations[sid].allowed_a == 12.6


def test_s5_2_share_counted_per_phase_1p_and_3p_in_one_group():
    """§5.2: count[p] = loads using phase p. L1 is shared by two (13 A / 2), L2/L3 by one
    → the 3p car takes 6.5 A on top, the 1p car on L1 the remaining 6.5 A: 12.5 A each."""
    dlm, _ = make()
    res = dlm.allocate(
        meter(12, 6, 6), [st("a"), st("b", phases_in_use=1, measured_a=(6.0, 0.0, 0.0))]
    )
    assert res.stations["a"].allowed_a == 12.5
    assert res.stations["b"].allowed_a == 12.5
    assert vec_close(res.headroom_left_a, (0, 12.5, 12.5))


def test_s2_3_1p_car_on_rotated_phase_l2_uses_only_grid_l2():
    """§2.3: 1-phase car, phase_1=2 → books grid L2 only; L1/L3 (5 A left) do not matter."""
    dlm, _ = make()
    car = st("a", phase_1=2, phases_in_use=1, measured_a=(6.0, 0.0, 0.0))
    res = dlm.allocate(meter(20, 11, 20), [car])
    assert vec_close(res.headroom_start_a, (5, 20, 5))
    assert res.stations["a"].allowed_a == 16.0
    assert vec_close(res.stations["a"].booked_a, (0, 16, 0))
    # the same headroom refuses a 3-phase car (L1 has only 5 A)
    dlm2, _ = make()
    res2 = dlm2.allocate(meter(20, 11, 20), [st("b", measured_a=(0.0, 6.0, 0.0), phases_in_use=3)])
    assert res2.stations["b"].allowed_a == 0.0


def test_s4_1_own_measured_currents_removed_after_rotation():
    """§4.1: own measured currents are removed from the meter in grid order (phase_1=2)."""
    dlm, _ = make()
    res = dlm.allocate(meter(19, 17, 18), [st("a", phase_1=2, measured_a=(7.0, 8.0, 9.0), requested_a=0.0)])
    # grid = (9, 7, 8) → base (10, 10, 10) → headroom 15
    assert vec_close(res.headroom_start_a, (15, 15, 15))


def test_s4_1_export_on_a_phase_raises_headroom_above_the_fuse():
    """§4.1 step 2: headroom = limit − base, base signed; export (−5 A) gives 30 A on a 25 A fuse."""
    dlm, _ = make()
    res = dlm.allocate(meter(-5, 0, 0), [st("a", requested_a=0.0, measured_a=(0.0, 0.0, 0.0))])
    assert vec_close(res.headroom_start_a, (30, 25, 25))


def test_s2_4_request_clamped_to_min_and_max_zero_stays_zero():
    """§2.4: non-zero below min → min; above max → max; 0 → no charging (allowed 0)."""
    dlm, _ = make(fuse_limit_a=(63.0, 63.0, 63.0))
    res = dlm.allocate(
        meter(18, 18, 18),
        [st("low", requested_a=3.0), st("high", requested_a=40.0), st("off", requested_a=0.0)],
    )
    assert res.stations["low"].allowed_a == 6.0
    assert res.stations["low"].required_a == 6.0
    assert res.stations["high"].allowed_a == 16.0
    assert res.stations["off"].allowed_a == 0.0
    assert res.stations["off"].reason is Reason.NOT_REQUESTED


def test_s6_1_no_hysteresis_restart_and_reduction_next_cycle():
    """§6.1/§5.3: no delay or ramp; refused now, started the very next cycle when it fits."""
    dlm, clock = make()
    idle = dict(measured_a=(0.0, 0.0, 0.0), setpoint_a=0.0, charging_since=None)
    res = dlm.allocate(meter(22, 22, 22), [st("a", **idle)])
    assert res.stations["a"].allowed_a == 0.0
    clock.now += 10
    res = dlm.allocate(meter(19, 19, 19, t=clock.now), [st("a", **idle, last_seen=clock.now)])
    assert res.stations["a"].allowed_a == 6.0
    # a charging car at 16 A cut to the new headroom in one step
    clock.now += 10
    car = st("a", setpoint_a=16.0, measured_a=(16.0,) * 3, last_seen=clock.now)
    res = dlm.allocate(meter(26, 26, 26, t=clock.now), [car])
    assert res.stations["a"].allowed_a == 15.0


# --- §4.3 / §5.3 measured vs setpoint ---------------------------------------------


def settled(stations, reading, **cfg):
    """Run one cycle 61 s earlier with the same setpoints, then this one (N1: under-draw
    needs the setpoint unchanged for more than 60 s, as seen by the allocator)."""
    dlm, clock = make(**cfg)
    clock.now = T0 - 61
    dlm.allocate(replace(reading, updated_at=T0 - 61), [replace(x, last_seen=T0 - 61) for x in stations])
    clock.now = T0
    return dlm.allocate(reading, stations)


def _under_drawing(since):
    a = st("a", setpoint_a=16.0, measured_a=(10.0,) * 3, charging_since=since)
    b = st("b", setpoint_a=6.0, measured_a=(6.0,) * 3, charging_since=since)
    return [a, b]


def test_s4_3_under_drawing_car_after_60s_booked_at_measured():
    """§4.3: A draws 10 A at 12.5 A (≥ 1 A less) and charges > 60 s → booked 10 A; B gets 15 A.
    N1: "> 60 s" counts from the last setpoint increase seen by the allocator."""
    res = settled(_under_drawing(T0 - 120), meter(16, 16, 16))
    assert res.stations["a"].allowed_a == 12.5
    assert vec_close(res.stations["a"].booked_a, (10, 10, 10))
    assert res.stations["b"].allowed_a == 15.0


def test_s4_3_under_drawing_car_before_60s_booked_at_setpoint():
    """§4.3: charging for 30 s only → setpoint booked → equal 12.5 / 12.5 (allocation only;
    with the hold step A's old 16 A setpoint would defer B's rise by one cycle)."""
    dlm, _ = make(hold_measured_until_drop=False)
    res = dlm.allocate(meter(16, 16, 16), _under_drawing(T0 - 30))
    assert res.stations["a"].allowed_a == 12.5
    assert res.stations["b"].allowed_a == 12.5


def test_s4_3_substitution_switch_off_books_setpoint_always():
    """§4.3 consider_less_charging=True in openWB: full setpoint always booked."""
    dlm, _ = make(book_measured_when_lower=False, hold_measured_until_drop=False)
    res = dlm.allocate(meter(16, 16, 16), _under_drawing(T0 - 120))
    assert res.stations["b"].allowed_a == 12.5


def test_s4_3_no_charge_start_but_setpoint_sent_counts_as_started():
    """§4.3: no charge-start time and last sent current ≠ 0 → substitution allowed."""
    res = settled(_under_drawing(None), meter(16, 16, 16))
    assert res.stations["b"].allowed_a == 15.0


def test_s4_3_less_than_nominal_difference_booked_at_setpoint():
    """§4.3/§9(c): 0.7 A less than the value booked → setpoint booked, no substitution."""
    dlm, _ = make()
    a = st("a", requested_a=6.0, setpoint_a=6.0, measured_a=(5.3,) * 3, charging_since=T0 - 120)
    res = dlm.allocate(meter(5.3, 5.3, 5.3), [a])
    assert vec_close(res.stations["a"].booked_a, (6, 6, 6))


def test_s5_3_quirk_not_copied_car_below_minimum_booked_at_minimum():
    """§5.3 DERIVED quirk (openWB: 4.5 A drawn at min 6 → 2×4.5−6 = 3.0 A booked).
    an active car allowed ≥ its minimum is never booked below the minimum."""
    dlm, _ = make()
    a = st("a", requested_a=6.0, setpoint_a=6.0, measured_a=(4.5,) * 3, charging_since=T0 - 120)
    res = dlm.allocate(meter(4.5, 4.5, 4.5), [a])
    assert vec_close(res.stations["a"].booked_a, (6, 6, 6))


def test_s5_1_quirk_not_copied_refused_minimum_holds_nothing():
    """§5.1 DERIVED quirk: a refused minimum must not stay booked while its current is 0."""
    dlm, _ = make()
    idle = dict(measured_a=(0.0, 0.0, 0.0), setpoint_a=0.0, charging_since=None)
    res = dlm.allocate(meter(14, 14, 14), [st("a", **idle), st("b", **idle)])
    assert vec_close(res.stations["b"].booked_a, (0, 0, 0))
    assert vec_close(res.headroom_left_a, (5, 5, 5))


# --- ADDITION (b)/(c): station drawing more than its setpoint --------------------


def _overdraw_pair():
    a = st("a", requested_a=6.0, setpoint_a=6.0, measured_a=(7.5,) * 3)
    b = st("b", setpoint_a=8.0, measured_a=(8.0,) * 3)
    return [a, b]


def test_addition_car_drawing_7_5a_at_6a_setpoint_booked_at_measured():
    """ADDITION (b)/(c): A cannot go below ~7.5 A at 6 A → booked 7.5 A, so B gets 8.5 A
    and the 16 A fuse holds (7.5 + 8.5). openWB would give B 10 A → 17.5 A real."""
    dlm, _ = make(fuse_limit_a=(16.0, 16.0, 16.0))
    res = dlm.allocate(meter(15.5, 15.5, 15.5), _overdraw_pair())
    assert res.stations["a"].allowed_a == 6.0
    assert vec_close(res.stations["a"].booked_a, (7.5, 7.5, 7.5))
    assert res.stations["b"].allowed_a == 8.5


def test_addition_overdraw_booking_off_is_openwb():
    """ADDITION switch: book_measured_when_higher=False → openWB: excess invisible."""
    dlm, _ = make(fuse_limit_a=(16.0, 16.0, 16.0), book_measured_when_higher=False,
                  hold_measured_until_drop=False)
    res = dlm.allocate(meter(15.5, 15.5, 15.5), _overdraw_pair())
    assert res.stations["b"].allowed_a == 10.0


def test_addition_station_that_cannot_stop_keeps_its_measured_booked():
    """ADDITION (b): requested 0, setpoint 0, still drawing 10 A → 10 A booked, B gets less."""
    dlm, _ = make()
    a = st("a", requested_a=0.0, setpoint_a=0.0, measured_a=(10.0,) * 3)
    b = st("b", setpoint_a=6.0, measured_a=(6.0,) * 3)
    res = dlm.allocate(meter(16, 16, 16), [a, b])
    assert res.stations["a"].allowed_a == 0.0
    assert vec_close(res.stations["a"].booked_a, (10, 10, 10))
    assert res.stations["b"].allowed_a == 15.0


def test_addition_small_excess_within_tolerance_not_booked():
    """ADDITION tolerance: 6.15 A at 6 A (within 0.2 A) is not treated as overdraw."""
    dlm, _ = make()
    a = st("a", requested_a=6.0, setpoint_a=6.0, measured_a=(6.15,) * 3)
    res = dlm.allocate(meter(6.15, 6.15, 6.15), [a])
    assert res.stations["a"].allowed_a == 6.0


# --- ADDITION (b): lost and unknown stations ---------------------------------------


def test_addition_offline_station_reserved_until_back():
    """ADDITION (b): offline station keeps max(last setpoint) per phase.
    Its last values are NOT removed from the meter. openWB drops it after ~60 s."""
    dlm, _ = make()
    lost = st("lost", online=False, setpoint_a=10.0, measured_a=(12.0, 0.0, 0.0), last_seen=T0 - 5)
    res = dlm.allocate(meter(9, 9, 9), [lost, st("b")])
    assert res.stations["lost"].allowed_a is None
    assert res.stations["lost"].reason is Reason.RESERVED_OFFLINE
    assert vec_close(res.stations["lost"].booked_a, (12, 10, 10))
    # base 3 (only B's 6 A removed) → headroom 22; minus (12,10,10) → (10,12,12) → B 10 A
    assert vec_close(res.headroom_start_a, (22, 22, 22))
    assert res.stations["b"].allowed_a == 10.0


def test_addition_station_stale_by_age_is_reserved():
    """ADDITION (b): online flag set but last reading older than station_stale_after_s (60 s)."""
    dlm, _ = make()
    lost = st("lost", setpoint_a=10.0, measured_a=(10.0,) * 3, last_seen=T0 - 61)
    res = dlm.allocate(meter(3, 3, 3), [lost])
    assert res.stations["lost"].reason is Reason.RESERVED_OFFLINE
    fresh = st("lost", setpoint_a=10.0, measured_a=(10.0,) * 3, last_seen=T0 - 59)
    dlm2, _ = make()
    assert dlm2.allocate(meter(13, 13, 13), [fresh]).stations["lost"].reason is not Reason.RESERVED_OFFLINE


def test_addition_offline_reservation_off_is_openwb_drop():
    """ADDITION switch: reserve_lost_stations=False → booking dropped (openWB after its grace)."""
    dlm, _ = make(reserve_lost_stations=False)
    lost = st("lost", online=False, setpoint_a=10.0, measured_a=(12.0, 0.0, 0.0), last_seen=T0 - 5)
    res = dlm.allocate(meter(9, 9, 9), [lost, st("b")])
    assert res.stations["b"].allowed_a == 16.0
    assert res.stations["lost"].allowed_a == 0.0
    assert res.stations["lost"].reason is Reason.OFFLINE


def test_addition_first_seen_station_reserved_at_max_until_first_reading():
    """ADDITION (b): no reading since restart → max current reserved on its phases."""
    dlm, clock = make()
    new = StationInput(station_id="new", max_current_a=16.0, requested_a=16.0, last_seen=None, measured_a=None)
    res = dlm.allocate(meter(3, 3, 3), [new, st("b", measured_a=(0.0,) * 3)])
    assert res.stations["new"].allowed_a is None
    assert res.stations["new"].reason is Reason.RESERVED_UNKNOWN
    assert vec_close(res.stations["new"].booked_a, (16, 16, 16))
    assert res.stations["b"].allowed_a == 6.0
    # first fresh reading → normal allocation
    clock.now += 10
    new2 = StationInput(
        station_id="new", requested_a=16.0, measured_a=(0.0,) * 3, last_seen=clock.now, priority=0
    )
    b2 = st("b", setpoint_a=6.0, measured_a=(6.0,) * 3, last_seen=clock.now)
    res = dlm.allocate(meter(9, 9, 9, t=clock.now), [new2, b2])
    assert res.stations["new"].allowed_a == 6.0
    assert res.stations["new"].reason is Reason.NOT_CHARGING


def test_addition_first_seen_reservation_off():
    """ADDITION switch: reserve_unknown_stations=False → nothing reserved, normal allocation."""
    dlm, _ = make(reserve_unknown_stations=False)
    new = StationInput(station_id="new", requested_a=16.0, last_seen=None, measured_a=None)
    res = dlm.allocate(meter(3, 3, 3), [new])
    assert res.stations["new"].allowed_a == 6.0


# --- §7.1 + ADDITION (a): grid meter failure ---------------------------------------


def test_s7_1_error_flag_grace_60s_then_fallback_and_reset():
    """§7.1 with openWB's 60 s grace selected: error < 60 s → grace; ≥ 60 s → fallback;
    a healthy cycle resets the timer. during the grace no increase is given."""
    dlm, clock = make(meter_error_grace_s=60.0)
    car = lambda: st("a", last_seen=clock.now)  # noqa: E731
    res = dlm.allocate(meter(17, 17, 17, error=True), [car()])
    assert res.meter_state is MeterState.ERROR_GRACE
    assert res.stations["a"].allowed_a == 6.0
    assert res.stations["a"].reason is Reason.METER_HOLD
    clock.now += 59
    res = dlm.allocate(meter(17, 17, 17, t=clock.now, error=True), [car()])
    assert res.meter_state is MeterState.ERROR_GRACE
    clock.now += 1
    res = dlm.allocate(meter(17, 17, 17, t=clock.now, error=True), [car()])
    assert res.meter_state is MeterState.FAILED_ERROR
    clock.now += 10
    res = dlm.allocate(meter(17, 17, 17, t=clock.now), [car()])
    assert res.meter_state is MeterState.OK
    clock.now += 10
    res = dlm.allocate(meter(17, 17, 17, t=clock.now, error=True), [car()])
    assert res.meter_state is MeterState.ERROR_GRACE


def test_review2_error_grace_default_3_meter_periods():
    """ (DEVIATION from openWB - safety): grace = 3 × meter_period_s (10 s) = 30 s."""
    assert DlmConfig(fuse_limit_a=(25.0, 25.0, 25.0)).meter_error_grace_s == 30.0
    dlm, clock = make()
    car = lambda: st("a", last_seen=clock.now)  # noqa: E731
    dlm.allocate(meter(17, 17, 17, t=T0, error=True), [car()])
    clock.now += 29
    res = dlm.allocate(meter(17, 17, 17, t=clock.now, error=True), [car()])
    assert res.meter_state is MeterState.ERROR_GRACE
    clock.now += 1
    res = dlm.allocate(meter(17, 17, 17, t=clock.now, error=True), [car()])
    assert res.meter_state is MeterState.FAILED_ERROR


def test_addition_meter_stale_by_age():
    """ADDITION (a): a reading older than meter_stale_after_s is a failure; the
    default is 3 × meter_period_s = 30 s."""
    assert DlmConfig(fuse_limit_a=(25.0, 25.0, 25.0)).meter_stale_after_s == 30.0
    assert make()[0].allocate(meter(17, 17, 17, t=T0 - 30), [st("a")]).meter_state is MeterState.OK
    assert make()[0].allocate(meter(17, 17, 17, t=T0 - 31), [st("a")]).meter_state is MeterState.FAILED_STALE
    assert make()[0].allocate(None, [st("a")]).meter_state is MeterState.FAILED_STALE
    dlm60, _ = make(meter_stale_after_s=60.0)
    assert dlm60.allocate(meter(17, 17, 17, t=T0 - 60), [st("a")]).meter_state is MeterState.OK


def test_addition_meter_stale_check_off_is_openwb():
    """ADDITION switch: meter_stale_check=False → old values used without an error (openWB)."""
    dlm, _ = make(meter_stale_check=False)
    assert dlm.allocate(meter(17, 17, 17, t=T0 - 600), [st("a")]).meter_state is MeterState.OK


def test_addition_meter_stale_action_openwb_capped_by_fuse():
    """§7.1 fallback 7000 W / 230 / 3 = 10.14 A per phase, ADDITION: capped by the fuse (L1 8 A)."""
    dlm, _ = make(fuse_limit_a=(8.0, 25.0, 25.0))
    res = dlm.allocate(meter(1, 1, 1, t=T0 - 120), [st("a")])
    assert res.meter_state is MeterState.FAILED_STALE
    assert vec_close(res.headroom_start_a, (8.0, 7000 / 690, 7000 / 690))
    assert res.stations["a"].allowed_a == 8.0
    assert res.stations["a"].reason is Reason.METER_FALLBACK


def test_s7_1_meter_stale_action_openwb_uncapped_is_exact_openwb():
    """§7.1 exact openWB: fallback budget ignores the fuse → 10.1 A on an 8 A fuse."""
    dlm, _ = make(fuse_limit_a=(8.0, 25.0, 25.0), cap_errorcase_by_fuse=False)
    res = dlm.allocate(meter(1, 1, 1, t=T0 - 120), [st("a")])
    assert res.stations["a"].allowed_a == 10.1


def test_addition_meter_stale_action_minimum():
    """ADDITION (a) 'minimum': every station at its minimum; lowest priority paused if needed."""
    dlm, clock = make(meter_failure_action=MeterFailureAction.MINIMUM)
    cars = lambda: [st("a", priority=0, last_seen=clock.now), st("b", priority=1, last_seen=clock.now)]  # noqa: E731
    dlm.allocate(meter(13, 13, 13), cars())  # fresh: base 1 A
    clock.now += 60
    res = dlm.allocate(meter(13, 13, 13, t=T0), cars())
    assert res.meter_state is MeterState.FAILED_STALE
    assert res.stations["a"].allowed_a == 6.0
    assert res.stations["b"].allowed_a == 6.0
    assert res.stations["a"].reason is Reason.METER_FALLBACK
    dlm2, _ = make(fuse_limit_a=(10.0, 10.0, 10.0), meter_failure_action=MeterFailureAction.MINIMUM)
    res = dlm2.allocate(meter(1, 1, 1, t=T0 - 120), [st("b", priority=1), st("a", priority=0)])
    assert res.stations["a"].allowed_a == 6.0
    assert res.stations["b"].allowed_a == 0.0
    assert res.stations["b"].reason is Reason.MIN_NOT_FITTING


def test_addition_meter_stale_action_stop():
    """ADDITION (a) 'stop': allowed 0 for every station; reserved stations stay reserved."""
    dlm, _ = make(meter_failure_action=MeterFailureAction.STOP)
    lost = st("lost", online=False, last_seen=T0 - 5)
    res = dlm.allocate(meter(1, 1, 1, t=T0 - 120), [st("a"), lost])
    assert res.stations["a"].allowed_a == 0.0
    assert res.stations["a"].reason is Reason.METER_FALLBACK
    assert res.stations["lost"].allowed_a is None


def test_addition_meter_fallback_keeps_reservations_inside_budget():
    """ADDITION (a)+(b): the fallback budget still pays for reserved stations."""
    dlm, _ = make()
    lost = st("lost", online=False, setpoint_a=6.0, measured_a=(6.0,) * 3, last_seen=T0 - 5)
    res = dlm.allocate(meter(1, 1, 1, t=T0 - 120), [st("a"), lost])
    # 10.14 − 6 = 4.14 < 6 → refused
    assert res.stations["a"].allowed_a == 0.0


def test_addition_frozen_meter_detected():
    """ADDITION (a): identical meter values for N readings while own currents move → failure."""
    dlm, clock = make(meter_frozen_cycles=3)
    states = []
    for amps in (6.0, 10.0, 14.0):
        car = st("a", setpoint_a=amps, measured_a=(amps,) * 3, last_seen=clock.now)
        states.append(dlm.allocate(meter(17, 17, 17, t=clock.now), [car]).meter_state)
        clock.now += 10
    # the 2nd reading equals the last fresh one while own currents moved → not new
    assert states == [MeterState.OK, MeterState.NOT_NEW, MeterState.FAILED_FROZEN]
    # a changed value clears it
    car = st("a", setpoint_a=14.0, measured_a=(14.0,) * 3, last_seen=clock.now)
    assert dlm.allocate(meter(25, 25, 25, t=clock.now), [car]).meter_state is MeterState.OK


def test_addition_frozen_meter_not_flagged_when_own_currents_steady():
    """ADDITION (a): identical values while own currents do not change is a normal steady state."""
    dlm, clock = make(meter_frozen_cycles=3)
    for _ in range(5):
        res = dlm.allocate(meter(17, 17, 17, t=clock.now), [st("a", last_seen=clock.now)])
        clock.now += 10
    assert res.meter_state is MeterState.OK


def test_addition_frozen_meter_check_off():
    """ADDITION switch: meter_frozen_cycles=0 → no frozen check (openWB)."""
    dlm, clock = make(meter_frozen_cycles=0)
    for amps in (6.0, 10.0, 14.0, 6.0, 10.0, 14.0, 6.0, 10.0):
        car = st("a", setpoint_a=amps, measured_a=(amps,) * 3, last_seen=clock.now)
        res = dlm.allocate(meter(17, 17, 17, t=clock.now), [car])
        clock.now += 10
        assert res.meter_state is not MeterState.FAILED_FROZEN


# --- ADDITION: overload on the real meter ------------------------------------------


def test_addition_overload_on_one_phase_reduces_stations_on_that_phase_by_excess():
    """ADDITION overload: L1 at 28 A on 25 A (excess 3 A) → the 1p car on L1 drops from 16 to 13 A;
    the 1p car on L2 is untouched."""
    dlm, _ = make()
    a = st("a", phases_in_use=1, setpoint_a=16.0, measured_a=(16.0, 0.0, 0.0))
    b = st("b", phase_1=2, phases_in_use=1, setpoint_a=16.0, measured_a=(16.0, 0.0, 0.0))
    res = dlm.allocate(meter(28, 21, 5), [a, b])
    assert vec_close(res.overload_a, (3, 0, 0))
    assert res.stations["a"].allowed_a == 13.0
    assert res.stations["a"].reason is Reason.OVERLOAD
    assert res.stations["b"].allowed_a == 16.0


def test_addition_overload_headroom_not_clipped_when_base_over_fuse():
    """ADDITION overload: house load alone above the fuse → negative headroom kept (not 0)."""
    dlm, _ = make()
    a = st("a", phases_in_use=1, setpoint_a=8.0, measured_a=(8.0, 0.0, 0.0))
    res = dlm.allocate(meter(35, 5, 5), [a])
    assert close(res.headroom_start_a[0], -2.0)
    assert res.stations["a"].allowed_a == 0.0
    assert res.stations["a"].reason is Reason.OVERLOAD


def test_addition_overload_switch_off_clips_like_openwb():
    """ADDITION switch: overload_unclipped=False → base headroom clipped to 0."""
    dlm, _ = make(overload_unclipped=False)
    a = st("a", phases_in_use=1, setpoint_a=8.0, measured_a=(8.0, 0.0, 0.0))
    res = dlm.allocate(meter(35, 5, 5), [a])
    assert close(res.headroom_start_a[0], 0.0)
    assert res.stations["a"].allowed_a == 0.0
    assert res.stations["a"].reason is Reason.MIN_NOT_FITTING


# --- §4.2 / §4.4 power limit and unbalanced load -----------------------------------


def test_s4_4_power_limit_scales_the_request():
    """§4.2/§4.4 step 3: 11 kW limit; after 6 A × 3 × 230 V the remaining 6860 W scale 10 A to 9.94 A."""
    dlm, _ = make(fuse_limit_a=(32.0, 32.0, 32.0), max_total_power_w=11000.0)
    res = dlm.allocate(meter(6, 6, 6), [st("a")])
    assert res.stations["a"].allowed_a == 15.9
    assert res.stations["a"].reason is Reason.POWER_LIMIT


def test_s4_2_power_limit_counts_base_load():
    """§4.2: base power = meter − own measured; house 2 A/phase = 1380 W → 13.9 A."""
    dlm, _ = make(fuse_limit_a=(32.0, 32.0, 32.0), max_total_power_w=11000.0)
    res = dlm.allocate(meter(8, 8, 8), [st("a")])
    assert res.stations["a"].allowed_a == 13.9
    assert close(res.power_left_w, 0.0, 1e-3)


def test_s4_4_power_limit_exhausted_refuses_minimum():
    """§4.4 step 3: raw_power_left ≤ 0 → available 0 → minimum refused."""
    dlm, _ = make(fuse_limit_a=(32.0, 32.0, 32.0), max_total_power_w=3000.0)
    res = dlm.allocate(meter(6, 6, 6), [st("a")])
    assert res.stations["a"].allowed_a == 0.0
    assert res.stations["a"].reason is Reason.MIN_NOT_FITTING


def test_s4_4_unbalanced_load_limits_1p_car_when_enabled():
    """§4.4 step 4: limit 18 A between phases; a 1p car on a 35 A fuse stops at 18 A."""
    dlm, _ = make(fuse_limit_a=(35.0, 35.0, 35.0), unbalanced_load=True)
    car = st("a", phases_in_use=1, requested_a=32.0, max_current_a=32.0, measured_a=(6.0, 0.0, 0.0))
    res = dlm.allocate(meter(6, 0, 0), [car])
    assert res.stations["a"].allowed_a == 18.0
    assert res.stations["a"].reason is Reason.UNBALANCED


def test_s4_4_unbalanced_load_off_by_default():
    """§2.2: unbalanced load is off by default → the 1p car gets 32 A."""
    dlm, _ = make(fuse_limit_a=(35.0, 35.0, 35.0))
    car = st("a", phases_in_use=1, requested_a=32.0, max_current_a=32.0, measured_a=(6.0, 0.0, 0.0))
    res = dlm.allocate(meter(6, 0, 0), [car])
    assert res.stations["a"].allowed_a == 32.0


def test_s4_4_unbalanced_load_never_corrects_3p_car():
    """§4.4 step 4: a 3-phase load is never corrected for unbalance."""
    dlm, _ = make(fuse_limit_a=(35.0, 35.0, 35.0), unbalanced_load=True)
    car = st("a", requested_a=32.0, max_current_a=32.0)
    res = dlm.allocate(meter(26, 6, 6), [car])  # base L1 20 → headroom (15, 35, 35)
    assert res.stations["a"].allowed_a == 15.0
    assert res.stations["a"].reason is Reason.CURRENT_LIMIT


# --- input checks and determinism --------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        dict(phase_1=4),
        dict(phases_in_use=0),
        dict(min_current_a=10.0, max_current_a=8.0),
        dict(min_current_a=0.0),
    ],
)
def test_invalid_station_rejected(bad):
    dlm, _ = make()
    with pytest.raises(ValueError):
        dlm.allocate(meter(0, 0, 0), [st("a", **bad)])


def test_duplicate_station_id_rejected():
    dlm, _ = make()
    with pytest.raises(ValueError):
        dlm.allocate(meter(0, 0, 0), [st("a"), st("a")])


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        DlmConfig(fuse_limit_a=(25.0, 0.0, 25.0))
    with pytest.raises(ValueError):
        DlmConfig(fuse_limit_a=(25.0, 25.0, 25.0), meter_failure_action="nonsense")


def test_deterministic_same_inputs_same_result():
    a1, _ = make()
    a2, _ = make()
    inputs = [st("a"), st("b", phase_1=3, phases_in_use=1, measured_a=(6.0, 0, 0)), st("c", priority=3)]
    assert a1.allocate(meter(20, 18, 25), inputs) == a2.allocate(meter(20, 18, 25), inputs)


# ---.11 (probe inputs from scratchpad/rev/probe*.py) ----------

NAN = float("nan")
INF = float("inf")


@pytest.mark.parametrize(
    "reading",
    [meter(NAN, 10, 10), meter(-INF, 10, 10), meter(24, 24, 24, t=NAN)],
)
def test_review3_non_finite_meter_value_or_time_is_a_meter_failure(reading):
    """NaN/inf in the meter must not become headroom → meter failure (stale)."""
    dlm, _ = make()
    res = dlm.allocate(reading, [st("a"), st("b")])
    assert res.meter_state is MeterState.FAILED_STALE


def test_review3_non_finite_station_reading_makes_the_station_stale():
    """a NaN measured current → station treated as stale: max reserved, not commanded."""
    dlm, _ = make()
    res = dlm.allocate(meter(30, 30, 30), [st("a", measured_a=(NAN, 6.0, 6.0)), st("b")])
    assert res.stations["a"].allowed_a is None
    assert res.stations["a"].reason is Reason.RESERVED_OFFLINE
    assert vec_close(res.stations["a"].booked_a, (16, 16, 16))
    assert all(x == x for x in res.headroom_left_a)  # no NaN anywhere


@pytest.mark.parametrize(
    "bad",
    [
        dict(fuse_limit_a=(NAN, 25.0, 25.0)),
        dict(fuse_limit_a=(25.0, 25.0)),
        dict(fuse_limit_a=(25.0, 25.0, INF)),
        dict(max_total_power_w=INF),
        dict(phase_voltage_v=NAN),
        dict(nominal_difference_a=NAN),
        dict(overdraw_tolerance_a=-1.0),
    ],
)
def test_review3_non_finite_or_wrong_config_rejected(bad):
    bad.setdefault("fuse_limit_a", (25.0, 25.0, 25.0))
    with pytest.raises(ValueError):
        DlmConfig(**bad)


def test_review10_min_above_max_after_0_1a_rounding_rejected():
    """min 6.05 → 6.1, max 6.08 → 6.0: no sendable value → configuration error."""
    dlm, _ = make()
    with pytest.raises(ValueError):
        dlm.allocate(meter(0, 0, 0), [st("a", min_current_a=6.05, max_current_a=6.08)])


def test_review10_overdraw_tolerance_default_0_2a():
    """6.3 A at 6 A is an overdraw with the 0.2 A default → B gets 9.7 A, not 10 A."""
    assert DlmConfig(fuse_limit_a=(16.0, 16.0, 16.0)).overdraw_tolerance_a == 0.2
    dlm, _ = make(fuse_limit_a=(16.0, 16.0, 16.0))
    a = st("a", requested_a=6.0, setpoint_a=6.0, measured_a=(6.3,) * 3)
    res = dlm.allocate(meter(12.3, 12.3, 12.3), [a, st("b")])
    assert res.stations["b"].allowed_a == 9.7


def test_review10_fallback_per_phase_uses_230v_like_openwb():
    """ / spec §7.1: errorcase / 230 / 3, independent of phase_voltage_v."""
    dlm, _ = make(phase_voltage_v=240.0)
    res = dlm.allocate(None, [])
    assert vec_close(res.headroom_start_a, (7000 / 690,) * 3)


def test_review10_negative_total_power_scaled_with_abs():
    """L1 deeply negative (house 85 A on 25 A) makes Σ available negative; the power
    check uses |Σ| like openWB, so a 6 A minimum on L2 is not granted with 450 W left."""
    dlm, _ = make(max_total_power_w=20000.0)
    car = st("a", phase_1=2, phases_in_use=1, setpoint_a=0.0, measured_a=(0.0,) * 3, charging_since=None)
    res = dlm.allocate(meter(85, 0, 0), [car])
    assert res.stations["a"].allowed_a == 0.0


def _power_probe():
    return st("A", phase_1=2, phases_in_use=1, requested_a=32.0, max_current_a=32.0, measured_a=(6.0, 0, 0))


def test_review8_power_limit_not_inflated_by_negative_headroom_on_unused_phase():
    """ (DEVIATION from openWB - safety): L1 at −5 A headroom must not add −5 A to the
    power sum of a 1p car on L2. 11 kW − 30 A × 230 V = 4100 W → 17.8 A, real ≤ 11 kW."""
    dlm, _ = make(max_total_power_w=11000.0)
    res = dlm.allocate(meter(30, 6, 0), [_power_probe()])
    allowed = res.stations["A"].allowed_a
    assert (30 + allowed) * 230 <= 11000
    assert allowed == 17.8
    assert res.stations["A"].reason is Reason.POWER_LIMIT


def test_review8_switch_off_is_openwb():
    """clamp_negative_available=False keeps openWB's sum (22.0 A, 11.96 kW)."""
    dlm, _ = make(max_total_power_w=11000.0, clamp_negative_available=False,
                  hold_measured_until_drop=False)
    assert dlm.allocate(meter(30, 6, 0), [_power_probe()]).stations["A"].allowed_a == 22.0


def test_review6_phases_drawn_beyond_phases_in_use_are_booked():
    """A says 1 phase but draws 10 A on all three; B is 1p on L2. A must book L2/L3
    too, else B gets 16 A and L2 = 5 + 10 + 16 = 31 A on a 25 A fuse."""
    dlm, _ = make()
    a = st("A", phases_in_use=1, setpoint_a=10.0, measured_a=(10.0, 10.0, 10.0))
    b = st("B", phase_1=2, phases_in_use=1, setpoint_a=6.0, measured_a=(6.0, 0, 0))
    res = dlm.allocate(meter(15, 21, 15), [a, b])
    assert 5 + 10 + res.stations["B"].allowed_a <= 25
    assert res.stations["A"].allowed_a == 10.0
    assert res.stations["B"].allowed_a == 10.0
    assert vec_close(res.stations["A"].booked_a, (10, 10, 10))


def test_review6_small_draw_outside_used_phases_checked_against_zero():
    """0.3 A on a phase the station should not use (below the 0.5 A standby level, so
    not a used phase) is an overdraw against 0 → booked."""
    dlm, _ = make()
    a = st("A", phases_in_use=1, setpoint_a=10.0, measured_a=(10.0, 0.3, 0.0), requested_a=10.0)
    res = dlm.allocate(meter(10, 0.3, 0), [a])
    assert vec_close(res.stations["A"].booked_a, (10, 0.3, 0))


def test_review7_overdrawing_station_paused_when_its_real_draw_does_not_fit():
    """A draws 10 A at a 6 A setpoint; house 8 A on a 16 A fuse leaves 8 A. Its
    minimum really costs 10 A (it does not follow lower setpoints) → only a pause helps."""
    dlm, _ = make(fuse_limit_a=(16.0, 16.0, 16.0))
    a = st("A", requested_a=16.0, setpoint_a=6.0, measured_a=(10.0,) * 3)
    res = dlm.allocate(meter(18, 18, 18), [a])
    assert res.stations["A"].allowed_a == 0.0


def test_review7_overdrawing_station_keeps_minimum_when_its_draw_fits():
    """same station, house 5 A → 11 A left ≥ 10 A → minimum kept."""
    dlm, _ = make(fuse_limit_a=(16.0, 16.0, 16.0))
    a = st("A", requested_a=6.0, setpoint_a=6.0, measured_a=(10.0,) * 3)
    res = dlm.allocate(meter(15, 15, 15), [a])
    assert res.stations["A"].allowed_a == 6.0


def _review1_cars():
    return [
        st(k, setpoint_a=6.0, measured_a=(6.0,) * 3, charging_since=T0 - 100) for k in ("a", "b")
    ]


def test_review1_increase_is_booked_in_full_not_at_old_measured():
    """ (DEVIATION from openWB - safety): both cars follow 6 A for > 60 s; house 5 A on
    25 A. openWB books a's step to 10 A at its old 6 A draw and gives b 14 A → 29 A. Under-draw is
    judged against the LAST setpoint, so a's increase is booked in full → 10 / 10."""
    dlm, _ = make()
    res = dlm.allocate(meter(17, 17, 17), _review1_cars())
    assert res.stations["a"].allowed_a == 10.0
    assert res.stations["b"].allowed_a == 10.0
    assert 5 + 10 + 10 <= 25


def test_review1_switch_off_is_openwb():
    """substitute_against_setpoint=False → openWB substitution (10 / 14)."""
    dlm, _ = make(substitute_against_setpoint=False)
    res = dlm.allocate(meter(17, 17, 17), _review1_cars())
    assert (res.stations["a"].allowed_a, res.stations["b"].allowed_a) == (10.0, 14.0)


def test_review1_increase_above_setpoint_of_under_drawing_car_booked_on_top_of_measured():
    """a car drawing 10 A at 12 A (> 60 s) raised to 16 A books 10 + (16 − 12) = 14 A."""
    a = st("a", setpoint_a=12.0, measured_a=(10.0,) * 3, charging_since=T0 - 120)
    res = settled([a], meter(10, 10, 10), fuse_limit_a=(40.0, 40.0, 40.0))
    assert res.stations["a"].allowed_a == 16.0
    assert vec_close(res.stations["a"].booked_a, (14, 14, 14))


def test_review1_car_not_drawing_but_allowed_minimum_is_booked_at_minimum():
    """commanded 6 A, not drawing yet (no charge-start time) → its 6 A stay booked,
    so C does not take them (house 4 A, fuse 16 A: 4 + 6 + 6 + C ≤ 16)."""
    dlm, _ = make(fuse_limit_a=(16.0, 16.0, 16.0))
    idle = dict(setpoint_a=6.0, measured_a=(0.0,) * 3, charging_since=None)
    res = dlm.allocate(meter(10, 10, 10), [st("A", **idle), st("B", **idle), st("C")])
    total = sum(v.allowed_a for v in res.stations.values())
    assert 4 + total <= 16
    assert vec_close(res.stations["A"].booked_a, (6, 6, 6))


def test_review5_stop_request_while_still_drawing_keeps_its_current_this_cycle():
    """ (DEVIATION from openWB - safety): a asks for 0 but still draws 16 A; its 16 A are
    not given to b until a reading shows the drop (house 3 A: 3 + 16 + b ≤ 25 → b stays 6 A)."""
    dlm, _ = make()
    a = st("a", requested_a=0.0, setpoint_a=16.0, measured_a=(16.0,) * 3)
    res = dlm.allocate(meter(25, 25, 25), [a, st("b")])
    assert 3 + 16 + res.stations["b"].allowed_a <= 25
    assert res.stations["b"].allowed_a == 6.0
    assert res.stations["b"].reason is Reason.WAIT_DROP
    assert vec_close(res.stations["a"].booked_a, (16, 16, 16))


def test_review5_refused_station_still_drawing_blocks_a_new_start_on_that_phase():
    """A (prio 0) loses its minimum on L1 but still draws 6 A on L2; B (1p L2, house
    14 A there) may not start into A's 6 A this cycle; it starts when A's drop is measured."""
    dlm, clock = make()
    a = st("A", priority=0)
    b = st("B", priority=1, phase_1=2, phases_in_use=1, setpoint_a=0.0, measured_a=(0.0,) * 3,
           charging_since=None)
    res = dlm.allocate(meter(28, 20, 6), [a, b])
    assert res.stations["A"].allowed_a == 0.0
    assert res.stations["B"].allowed_a == 0.0
    assert res.stations["B"].reason is Reason.WAIT_DROP
    clock.now += 10
    a2 = st("A", priority=0, setpoint_a=0.0, measured_a=(0.0,) * 3, charging_since=None, last_seen=clock.now)
    b2 = st("B", priority=1, phase_1=2, phases_in_use=1, setpoint_a=0.0, measured_a=(0.0,) * 3,
            charging_since=None, last_seen=clock.now)
    res = dlm.allocate(meter(22, 14, 0, t=clock.now), [a2, b2])
    assert res.stations["B"].allowed_a == 6.0


def test_review5_switch_off_is_openwb_same_cycle_release():
    """hold_measured_until_drop=False → openWB: b gets a's 16 A at once."""
    dlm, _ = make(hold_measured_until_drop=False)
    a = st("a", requested_a=0.0, setpoint_a=16.0, measured_a=(16.0,) * 3)
    assert dlm.allocate(meter(25, 25, 25), [a, st("b")]).stations["b"].allowed_a == 16.0


def _feedback_loop(dlm, clock, error, cycles, fixed_time):
    """Probe 7/8: house 5 A, two cars following their setpoints; the meter keeps showing 17 A."""
    cur = {"a": 6.0, "b": 6.0}
    worst = 0.0
    for _ in range(cycles):
        cars = [st(k, setpoint_a=v, measured_a=(v,) * 3, last_seen=clock.now, charging_since=T0 - 100)
                for k, v in cur.items()]
        res = dlm.allocate(meter(17, 17, 17, t=T0 if fixed_time else clock.now, error=error), cars)
        cur = {k: v.allowed_a for k, v in res.stations.items()}
        worst = max(worst, 5 + sum(cur.values()))
        clock.now += 10
    return worst


def test_review2_meter_in_error_grace_gives_no_increase():
    """ (DEVIATION from openWB - safety): openWB treats the stuck value as valid during
    the grace, own currents rise, base falls, 37 A on 25 A. We hold: real grid ≤ 25 A."""
    dlm, clock = make()
    assert _feedback_loop(dlm, clock, error=True, cycles=7, fixed_time=True) <= 25


def test_review2_frozen_meter_without_error_gives_no_feedback():
    """values identical to the last fresh reading while own currents moved are not
    new → last fresh base load, no increase → real grid ≤ 25 A before FAILED_FROZEN."""
    dlm, clock = make()
    assert _feedback_loop(dlm, clock, error=False, cycles=16, fixed_time=False) <= 25


def test_review2_reading_not_newer_than_previous_gives_no_increase():
    """updated_at not newer than the previous cycle → NOT_NEW, no increase."""
    dlm, clock = make()
    dlm.allocate(meter(17, 17, 17), [st("a"), st("b")])
    clock.now += 10
    cars = [st(k, last_seen=clock.now) for k in "ab"]
    res = dlm.allocate(meter(17, 17, 17, t=T0), cars)
    assert res.meter_state is MeterState.NOT_NEW
    assert res.stations["a"].allowed_a == 6.0
    assert res.stations["a"].reason is Reason.METER_HOLD


def test_review2_frozen_check_runs_during_error_grace():
    """an error-flagged meter repeating its value while own currents move is frozen."""
    dlm, clock = make(meter_frozen_cycles=3, meter_error_grace_s=60.0)
    states = []
    for amps in (6.0, 10.0, 14.0):
        car = st("a", setpoint_a=amps, measured_a=(amps,) * 3, last_seen=clock.now)
        states.append(dlm.allocate(meter(17, 17, 17, t=clock.now, error=True), [car]).meter_state)
        clock.now += 10
    assert states[-1] is MeterState.FAILED_FROZEN


def test_review2_switch_off_is_openwb_stale_values_used():
    """hold_on_stale_meter=False → openWB: stuck value used as valid (feedback)."""
    dlm, clock = make(hold_on_stale_meter=False, meter_error_grace_s=60.0)
    assert _feedback_loop(dlm, clock, error=True, cycles=3, fixed_time=True) > 25


def test_review4_frozen_state_latched_until_value_changes():
    """once frozen, steady own currents must not 'unfreeze' the same stuck value."""
    dlm, clock = make(meter_frozen_cycles=3)
    for amps in (6.0, 10.0, 14.0):
        car = st("a", setpoint_a=amps, measured_a=(amps,) * 3, last_seen=clock.now)
        res = dlm.allocate(meter(17, 17, 17, t=clock.now), [car])
        clock.now += 10
    assert res.meter_state is MeterState.FAILED_FROZEN
    for _ in range(5):
        car = st("a", setpoint_a=10.1, measured_a=(10.1,) * 3, last_seen=clock.now)
        res = dlm.allocate(meter(17, 17, 17, t=clock.now), [car])
        clock.now += 10
        assert res.meter_state is MeterState.FAILED_FROZEN
    car = st("a", setpoint_a=10.1, measured_a=(10.1,) * 3, last_seen=clock.now)
    assert dlm.allocate(meter(15.1, 15.1, 15.1, t=clock.now), [car]).meter_state is MeterState.OK


def test_review9_minimum_action_budget_is_fuse_minus_last_fresh_base_load():
    """house 15 A known from the last fresh cycle → 10 A left → only one 6 A minimum."""
    dlm, clock = make(meter_failure_action=MeterFailureAction.MINIMUM)
    cars = lambda: [st(x, last_seen=clock.now) for x in "ab"]  # noqa: E731
    dlm.allocate(meter(27, 27, 27), cars())  # base 15
    clock.now += 60
    res = dlm.allocate(None, cars())
    assert vec_close(res.headroom_start_a, (10, 10, 10))
    assert [res.stations[x].allowed_a for x in "ab"] == [6.0, 0.0]


def test_review9_minimum_action_unknown_base_uses_errorcase_budget():
    """no fresh reading ever → min(fuse, 7000 W / 230 / 3 = 10.14 A) → one minimum of four."""
    dlm, _ = make(meter_failure_action=MeterFailureAction.MINIMUM)
    res = dlm.allocate(None, [st(x) for x in "abcd"])
    assert vec_close(res.headroom_start_a, (7000 / 690,) * 3)
    assert [res.stations[x].allowed_a for x in "abcd"] == [6.0, 0.0, 0.0, 0.0]


def test_reviewN1_reading_lagging_a_raised_setpoint_is_not_an_under_drawer():
    """N1 (probe5 A): both cars charge > 60 s; cycle 0 gives 10 / 10; in cycle 1 the
    setpoints are 10 but the readings still show 6. That is a lag, not under-draw: the raised
    setpoint stays booked and held → still 10 / 10, never 10 / 14 (29 A on 25 A)."""
    dlm, clock = make()
    cars0 = [st(k, setpoint_a=6.0, charging_since=T0 - 300, last_seen=clock.now) for k in "ab"]
    res = dlm.allocate(meter(17, 17, 17, t=clock.now), cars0)
    assert [res.stations[k].allowed_a for k in "ab"] == [10.0, 10.0]
    clock.now += 10
    cars1 = [st(k, setpoint_a=10.0, measured_a=(6.0,) * 3, charging_since=T0 - 300, last_seen=clock.now)
             for k in "ab"]
    res = dlm.allocate(meter(17, 17, 17, t=clock.now), cars1)
    assert 5 + sum(res.stations[k].allowed_a for k in "ab") <= 25


def test_reviewN1_under_drawer_confirmed_after_setpoint_unchanged_60s():
    """N1: the same lagging reading 61 s after the last raise is a real under-draw → substituted."""
    dlm, clock = make()
    for dt in (0, 61):
        clock.now = T0 + dt
        a = st("a", setpoint_a=16.0, measured_a=(10.0,) * 3, charging_since=T0 - 300, last_seen=clock.now)
        b = st("b", setpoint_a=6.0, measured_a=(6.0,) * 3, charging_since=T0 - 300, last_seen=clock.now)
        res = dlm.allocate(meter(16, 16, 16, t=clock.now), [a, b])
    assert vec_close(res.stations["a"].booked_a, (10, 10, 10))
    assert res.stations["b"].allowed_a == 15.0


def test_reviewN1_hold_keeps_old_setpoint_of_a_car_cut_this_cycle():
    """N1: A (setpoint 16, reading 10 for 30 s) is cut to 12.5 A but may still rise to 16 A
    until it gets the new value → B rises only to 25 − 16 = 9 A now."""
    dlm, _ = make()
    res = dlm.allocate(meter(16, 16, 16), _under_drawing(T0 - 30))
    assert res.stations["a"].allowed_a == 12.5
    assert res.stations["b"].allowed_a == 9.0
    assert res.stations["b"].reason is Reason.WAIT_DROP


def test_reviewN3_no_new_meter_value_keeps_last_commanded_value_of_idle_car():
    """The meter timestamp is new only every other cycle. An idle car
    (allowed 6 A, draws 0) must keep its 6 A in the no-increase cycles - 'no increase' means not
    above the last commanded value - also after 60 s, when it counts as an under-drawer."""
    dlm, clock = make()
    cur, since, seen = 0.0, None, []
    for i in range(10):
        t_meter = clock.now if i % 2 == 0 else clock.now - 10
        car = st("idle", setpoint_a=cur, measured_a=(0.0,) * 3, charging_since=since, last_seen=clock.now)
        res = dlm.allocate(meter(5, 5, 5, t=t_meter), [car])
        allowed = res.stations["idle"].allowed_a
        seen.append(allowed)
        if cur == 0 and allowed > 0:
            since = clock.now
        cur = allowed
        clock.now += 10
    assert seen == [6.0] * 10


def _probe5_b(**cfg):
    """fuse 16 A, house 4 A; A and B plugged in and asking for 16 A but not
    drawing; C (last in the group) charging. Returns C's allowed value per cycle."""
    dlm, clock = make(fuse_limit_a=(16.0,) * 3, **cfg)
    cur = {"A": 0.0, "B": 0.0, "C": 6.0}
    draw = dict(cur)
    cap = {"A": 0.0, "B": 0.0, "C": 16.0}
    out = []
    for _ in range(6):
        m_ = 4.0 + sum(draw.values())
        cars = [st(k, setpoint_a=cur[k], measured_a=(draw[k],) * 3,
                   charging_since=(T0 - 300 if cur[k] > 0 else None), last_seen=clock.now) for k in "ABC"]
        res = dlm.allocate(meter(m_, m_, m_, t=clock.now), cars)
        cur = {k: res.stations[k].allowed_a for k in "ABC"}
        out.append(cur["C"])
        draw = {k: min(cur[k], cap[k]) for k in "ABC"}
        clock.now += 10
    return out


def test_reviewN2_drawing_station_gets_minimum_before_idle_ones_in_its_group():
    """N2 (DEVIATION from openWB order - availability): idle plugged-in cars earlier in the
    group must not keep the charging car C paused; drawing stations take the minimum first."""
    assert _probe5_b() == [6.0] * 6


def test_reviewN2_switch_off_is_openwb_list_order():
    """N2 switch: drawing_first_in_group=False → openWB list order: C stays paused."""
    assert _probe5_b(drawing_first_in_group=False)[1:] == [0.0] * 5


def test_reviewN4_integer_meter_and_1a_taper_is_not_frozen():
    """N4: integer-amp meter (resolution 1 A); the car tapers 16 → 15 A
    while the house load rises by 1 A, so the meter keeps 19 A. Not frozen, not 'not new'."""
    dlm, clock = make(fuse_limit_a=(32.0,) * 3, meter_resolution_a=1.0)
    allowed = 16.0
    for i, draw in enumerate([16.0, 16.0] + [15.0] * 9):
        house = 3.4 if draw == 16.0 else 4.4
        m_ = float(round(house + draw))
        car = st("a", requested_a=32.0, max_current_a=32.0, setpoint_a=allowed, measured_a=(draw,) * 3,
                 charging_since=T0 - 300, last_seen=clock.now)
        res = dlm.allocate(meter(m_, m_, m_, t=clock.now), [car])
        assert res.meter_state is MeterState.OK, (i, res.meter_state)
        allowed = res.stations["a"].allowed_a
        clock.now += 10


def test_reviewN4_slow_meter_frozen_history_counts_meter_updates_not_cycles():
    """N4: meter period 60 s, control cycle 10 s, a constant value repeated
    between updates; own current moves at an update. Six control cycles are one meter update,
    so FAILED_FROZEN must not appear."""
    dlm, clock = make(meter_period_s=60.0)
    t_meter, draw = clock.now, 6.0
    value = 5.0 + draw
    for i in range(19):
        if clock.now - t_meter >= 60:
            t_meter, value = clock.now, 5.0 + draw + (0.2 if i % 2 else -0.2)
        car = st("a", setpoint_a=draw, measured_a=(draw,) * 3, charging_since=T0 - 300, last_seen=clock.now)
        res = dlm.allocate(meter(value, value, value, t=t_meter), [car])
        assert res.meter_state is not MeterState.FAILED_FROZEN, i
        draw = res.stations["a"].allowed_a
        clock.now += 10


def test_reviewN6_default_timeouts_capped_at_60s():
    """N6: 3 × meter_period_s, but at most 60 s unless set explicitly."""
    cfg = DlmConfig(fuse_limit_a=(25.0, 25.0, 25.0), meter_period_s=60.0)
    assert (cfg.meter_error_grace_s, cfg.meter_stale_after_s) == (60.0, 60.0)
    cfg = DlmConfig(fuse_limit_a=(25.0, 25.0, 25.0), meter_period_s=60.0, meter_stale_after_s=180.0)
    assert cfg.meter_stale_after_s == 180.0


def test_reviewN6_minimum_action_ignores_base_load_older_than_300s():
    """N6: the last fresh base load is used by the MINIMUM action only up to 300 s old;
    older → errorcase budget min(fuse, 10.14 A)."""
    dlm, clock = make(meter_failure_action=MeterFailureAction.MINIMUM)
    dlm.allocate(meter(1, 1, 1), [])  # base 1 A
    clock.now += 301
    res = dlm.allocate(None, [])
    assert vec_close(res.headroom_start_a, (7000 / 690,) * 3)
    dlm2, clock2 = make(meter_failure_action=MeterFailureAction.MINIMUM)
    dlm2.allocate(meter(1, 1, 1), [])
    clock2.now += 299
    assert vec_close(dlm2.allocate(None, []).headroom_start_a, (24, 24, 24))


def test_reviewN6_first_seen_station_reserved_at_hardware_max_when_given():
    """N6: a station with no reading yet is reserved at its real hardware limit if known."""
    dlm, _ = make(fuse_limit_a=(40.0, 40.0, 40.0))
    new = StationInput(station_id="new", max_current_a=16.0, hardware_max_a=32.0, last_seen=None)
    res = dlm.allocate(meter(0, 0, 0), [new])
    assert vec_close(res.stations["new"].booked_a, (32, 32, 32))
