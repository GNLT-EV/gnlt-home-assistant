"""Start refusals, start limits and phase rules like evcc / the OCPP
integration."""

import pytest

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus.commands import ORIGIN_AUTO_START, ORIGIN_RESUME
from gnlt_charger.modbus.station import WriteRefused
from modbus_cmd_rig import Car, go


async def plugged(R, **kw):
    rig = await R(**kw).start()
    rig.plug(Car(phases=3))
    await rig.run(20)
    return rig


def test_manual_and_site_starts_not_limited():
    """evcc has no start limit - four manual starts in a few minutes all go out."""

    async def main(R):
        rig = await plugged(R)
        for _ in range(4):
            await rig.call(rig.cmd.async_start())
            await rig.run(30)
            await rig.call(rig.cmd.async_stop())
            await rig.run(20)
        assert rig.writes().count((95, 1)) == 4
        assert not hasattr(rig.cmd, "limits")

    go(main)


def test_automatic_starts_180s_apart_like_ocpp():
    """one automatic start per 180 s (OCPP AUTO_START_COOLDOWN_S), shared
    by the schedule, auto start and resume."""

    async def main(R):
        rig = await plugged(R)
        await rig.call(rig.cmd._automatic_start(ORIGIN_AUTO_START))
        await rig.run(20)
        await rig.call(rig.cmd.async_stop())
        await rig.run(20)
        await rig.call(rig.cmd._automatic_start(ORIGIN_RESUME))
        assert rig.writes().count((95, 1)) == 1 and rig.cmd.would_do == "resume: cooldown"
        await rig.run(180)
        await rig.call(rig.cmd._automatic_start(ORIGIN_RESUME))
        assert rig.writes().count((95, 1)) == 2

    go(main)


def test_start_refused_while_faulted_not_sticky():
    """a start is refused while reg 4 != 0 (OCPP: Faulted); once the
    error is gone it is allowed again - no "until unplug" (relay welded too)."""

    async def main(R):
        rig = await plugged(R)
        rig.fake.set_error(C.ERROR_RELAY_WELDED)
        await rig.run(20)
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_start())
        assert err.value.key == "mb_faulted" and rig.writes() == []
        from gnlt_charger.modbus.entities import _detail

        # step 2: the reg-4 code (or the code on the charger screen), not an English key
        assert _detail(err.value) == "23"
        rig.fake.set_error(0)
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        assert (95, 1) in rig.writes()
        assert not hasattr(rig.st, "relay_welded_block")

    go(main)


def test_start_under_plug_and_charge_is_sent():
    """no 93 = 1 gate on a start (evcc sends 95 = 1 anyway)."""

    async def main(R):
        rig = await plugged(R)
        rig.fake.menu_set_mode(0)
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        assert rig.writes()[-1] == (95, 1)

    go(main)


def test_start_refused_after_an_empty_session_until_replug():
    """OCPP "replug" - after an empty session the start is refused until
    the cable is re-plugged."""

    async def main(R):
        rig = await plugged(R)
        rig.st.session_flags["empty_session"] = True
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_start())
        assert err.value.key == "replug"
        rig.fake.unplug()
        await rig.run(30)
        rig.plug()
        await rig.run(30)
        await rig.call(rig.cmd.async_start())
        assert (95, 1) in rig.writes()

    go(main)


def test_phase_switches_no_quota_no_freeze_status_3_allowed():
    """no switch quota, no 60 s block after a start or switch, no
    freeze after foreign changes, no status-3 refusal - like evcc."""

    async def main(R):
        rig = await R().start()
        for phases in (1, 3, 1, 3, 1):
            await rig.call(rig.cmd.async_set_phases(phases))
        assert [v for r, v in rig.writes() if r == 200] == [1, 3, 1, 3, 1]
        for value in (3, 1, 3, 1, 3):
            rig.fake.phases = value  # someone else switches back and forth
            await rig.run(10)
        await rig.call(rig.cmd.async_set_phases(1))
        assert rig.writes()[-1] == (200, 1)
        rig.plug(Car(phases=3))
        await rig.run(20)
        rig.fake.force_status(C.STATUS_STARTING)
        await rig.run(10)
        await rig.call(rig.cmd.async_set_phases(3))  # status 3: like charging, never refused for it
        assert (200, 3) in rig.writes()[-3:]

    go(main)


# --- empty session, schedule, auto start like OCPP -------------


def _window(start_off=5, length=30, stop=True):
    from gnlt_charger.logic import Schedule

    start = 12 * 60 + start_off
    return Schedule(enabled=True, start_min=start, end_min=start + length, days="all", stop_at_end=stop)


async def _minutes(rig, n):
    for _ in range(n):
        await rig.call(rig.cmd.async_schedule_tick(), budget=10)
        await rig.run(60)


def test_empty_session_like_ocpp_after_180s_one_stop_then_replug():
    """180 s after our 95 = 1 the station still shows 2 and no current
    flowed: flag, notification, one stop; the next start needs a re-plug."""

    async def main(R):
        rig = await R().start()
        rig.plug(Car(asleep=True))
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.run(170)
        assert "empty_session" not in rig.kinds()
        await rig.run(30)
        assert "empty_session" in rig.kinds() and "empty_session" in rig.st.snapshot.problems
        assert rig.writes()[-2:] == [(95, 2), (91, 0)] and rig.writes().count((95, 2)) == 1
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_start())
        assert err.value.key == "replug"

    go(main)


def test_car_pause_in_status_4_is_left_alone():
    """status 4 without current is a car pause on our boxes - no stop,
    no empty session (OCPP leaves SuspendedEV / SuspendedEVSE alone)."""

    async def main(R):
        rig = await R().start()
        rig.plug(Car(ramp_s=600.0))
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.run(400)
        assert rig.st.snapshot.status == C.STATUS_CHARGING
        assert (95, 2) not in rig.writes() and "empty_session" not in rig.kinds()

    go(main)


def test_schedule_starts_once_from_6_at_the_window_opening():
    """at the opening one start also from a finished charge with the
    cable still in (OCPP: Finishing at the opening); no attempt count."""

    async def main(R):
        from gnlt_charger.modbus.commands import CommandOptions

        rig = await R(options=CommandOptions(schedule=_window(start_off=5))).start()
        rig.plug()
        await rig.run(20)
        rig.fake.force_status(C.STATUS_FINISHED)
        await rig.run(20)
        await _minutes(rig, 8)
        assert rig.writes().count((95, 1)) == 1

    go(main)


def test_no_restart_by_schedule_after_a_charge_in_the_window():
    """a 2 after a charge is the finished state - the schedule does not
    start into it again (OCPP Finishing after Charging); no extra rule for a
    user stop is needed."""

    async def main(R):
        from gnlt_charger.modbus.commands import CommandOptions

        rig = await R(options=CommandOptions(schedule=_window(start_off=0, length=60, stop=False))).start()
        rig.plug()
        await _minutes(rig, 3)
        assert rig.writes().count((95, 1)) == 1 and rig.st.snapshot.status == C.STATUS_CHARGING
        await rig.call(rig.cmd.async_stop())
        await _minutes(rig, 15)
        assert rig.writes().count((95, 1)) == 1

    go(main)


def test_auto_start_on_every_plug_no_daily_cap():
    """no daily cap - seven plug-ins, seven automatic starts (the 180 s
    cooldown between them is kept)."""

    async def main(R):
        from gnlt_charger.modbus.commands import CommandOptions

        rig = await R(options=CommandOptions(auto_start=True)).start()
        for _ in range(7):
            rig.plug()
            await rig.run(60)
            rig.fake.unplug()
            await rig.run(150)
        assert rig.writes().count((95, 1)) == 7

    go(main)


# --- state model ------------------------------------------


def test_status_0_drops_the_poll_other_zeros_are_data():
    """like evcc, status 0 is no state (the cycle is skipped); the old
    "every tier-A register zero" rule is gone."""

    async def main(R):
        rig = await R().start()
        rig.fake.force_status(0)
        before = rig.st.snapshot.time
        await rig.run(20)
        assert rig.st.snapshot.time == before and rig.st.last_failure == C.FAIL_NOT_READY
        rig.fake.force_status(C.STATUS_STANDBY)
        await rig.run(20)
        assert rig.st.snapshot.time != before and rig.st.snapshot.status == C.STATUS_STANDBY

    go(main)


def test_plug_in_seen_at_the_first_poll():
    """the cable as reg 2 shows it, at once (OCPP cable sensor)."""

    async def main(R):
        rig = await R().start()
        events = []
        rig.st.subscribe_events(lambda name, data: events.append(name))
        rig.plug()
        await rig.run(10)
        assert C.EVENT_PLUGGED in events and rig.st.snapshot.plugged is True

    go(main)


def test_91_restored_after_every_reconnect():
    """like OCPP after every boot - no "once per 30 min" limit."""

    async def main(R):
        rig = await R().start()
        rig.st.desired["current"] = 100
        for _ in range(2):
            rig.fake.foreign_set_current(160)
            rig.fake.kill_sessions()
            await rig.run(30)
        assert rig.writes().count((91, 100)) == 2
        assert "restore_dropped" not in rig.kinds()

    go(main)


def test_foreign_91_adopted_not_restored():
    """a 91 set by someone else while the link is up is adopted (evcc),
    also out of the range - never written back by us."""

    async def main(R):
        rig = await R().start()
        rig.fake.foreign_set_current(30)
        await rig.run(60)
        assert rig.writes() == []

    go(main)


def test_charge_on_the_stop_marker_not_replaced():
    """after our stop (91 = 0, stored 5.0 A) a charge started at the
    station runs on that value; evcc leaves 91 until its next enable."""

    async def main(R):
        rig = await plugged(R)
        await rig.call(rig.cmd.async_start())
        await rig.run(60)
        await rig.call(rig.cmd.async_stop())
        await rig.run(20)
        n = len(rig.writes())
        rig.fake.external_start()
        await rig.run(120)
        assert rig.writes()[n:] == []

    go(main)


# --- HA side like OCPP -------------------------------------------------


def test_charging_switch_like_ocpp_status_4_or_open_record_not_empty():
    """ON in status 4 or while a charge record is open that is not an
    empty session; not during status 3 alone."""

    async def main(R):
        rig = await plugged(R)
        assert not rig.cmd.charging_switch_state
        await rig.call(rig.cmd.async_start())
        await rig.run(60)
        assert rig.st.snapshot.status == C.STATUS_CHARGING and rig.cmd.charging_switch_state
        rig.fake.force_status(C.STATUS_CONNECTED)  # a pause the box shows as 2: the record stays open
        await rig.run(10)
        assert rig.st.record_open and rig.cmd.charging_switch_state
        rig.st.session_flags["empty_session"] = True
        assert not rig.cmd.charging_switch_state  # an empty session is not a charge

    go(main)


def test_nothing_learned_no_reset_button_no_control_problem():
    """no learned stop/phase values and no reset button; no
    control_problem sensor (stop_not_working stays)."""
    from gnlt_charger.modbus import entities as mb_entities
    from gnlt_charger.modbus.profiles import LearnedCaps

    assert set(LearnedCaps().as_dict()) == {"decimal_current"}
    assert not hasattr(mb_entities, "ResetLearnedButton")
    keys = {d.key for d in mb_entities.BINARY_SENSORS}
    assert "control_problem" not in keys and "stop_not_working" in keys
