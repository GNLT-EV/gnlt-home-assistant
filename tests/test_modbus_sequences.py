"""Sequences (modbus/sequences.py via modbus/commands.py) against the fake
station on a virtual clock."""

import asyncio

import pytest

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus.commands import CommandOptions, Target
from gnlt_charger.modbus.station import WriteRefused
from modbus_cmd_rig import Car, MemoryStore, go

OTF = CommandOptions()  # phase switching while charging has no option any more


async def charging(R, **kw):
    """A rig whose car charges after our own start."""
    car = kw.pop("car", None)
    rig = R(**kw)
    await rig.start()
    rig.plug(car)
    await rig.run(20)
    await rig.call(rig.cmd.async_start())
    await rig.idle()
    await rig.run(40)
    assert rig.st.snapshot.delivering
    return rig


def ops(rig, n=0):
    return [(fc, addr) for _t, fc, addr, _a in rig.client.sent[n:]]


# --- start -----------------------------------------------------------------------


def test_start_writes_91_even_if_equal():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        assert rig.st.snapshot.setpoint_a == 16.0
        await rig.call(rig.cmd.async_start())
        assert rig.writes()[0] == (91, 160)

    go(main)


def test_start_writes_200_only_when_the_choice_differs():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        assert [r for r, _v in rig.writes()] == [91, 95]
        rig2 = await R().start()
        rig2.plug()
        await rig2.run(20)
        rig2.fake.phases = 1  # changed at the station while the car is in
        await rig2.run(11)
        assert rig2.writes() == []  # nothing written unprompted
        await rig2.call(rig2.cmd.async_start())
        assert rig2.writes()[:3] == [(200, 3), (91, 160), (95, 1)]

    go(main)


# --- stop ------------------------------------------------------------------------


def test_stop_writes_95_2_then_91_0():
    """, evcc Enable(false): 95 = 2, then 91 = 0 (stored as 5.0 A)."""

    async def main(R):
        rig = await charging(R)
        n = len(rig.writes())
        result = await rig.call(rig.cmd.async_stop())
        assert result.write.register == 95 and result.write.value == 2
        await rig.idle()
        assert rig.writes()[n:] == [(95, 2), (91, 0)]
        assert rig.fake.setpoint == 50
        assert rig.st.diagnostics()["write_log"][-1]["result"] == C.WRITE_OK  # acknowledged (no read-back)

    go(main)


def test_stop_91_0_also_when_stop_ignored():
    async def main(R):
        rig = await charging(R, stop="ignored")
        n = len(rig.writes())
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        assert rig.writes()[n:] == [(95, 2), (91, 0)]
        # 91 = 0 is allowed for the stop step only: never on its own.
        with pytest.raises(WriteRefused) as err:
            await rig.st.async_write(91, 0, origin="user")
        assert err.value.key == C.REFUSE_CURRENT_RANGE

    go(main)


def test_stop_91_0_not_restored_and_not_leaking_into_start():
    async def main(R):
        rig = await charging(R)
        rig.st.desired["current"] = 100
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        n = len(rig.writes())
        rig.fake.kill_sessions()  # link drop: must not "restore" the stop marker
        await rig.run(200)
        assert rig.st.snapshot.registers[91] == 50
        assert rig.writes()[n:] == []
        assert not [d for e, d in rig.events if e == C.EVENT_FOREIGN_CHANGE and d.get("register") == 91]
        await rig.call(rig.cmd.async_start())
        await rig.idle()
        assert rig.writes()[n:][:2] == [(91, 100), (95, 1)]  # the start writes the current first
        assert rig.fake.setpoint == 100

    go(main)


def test_stop_marker_kept_when_our_stop_did_not_end_the_charge():
    async def main(R):
        rig = await charging(R, stop="ignored")
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        n = len(rig.writes())
        await rig.run(300)
        assert rig.fake.status == 4 and rig.writes()[n:] == []  # as evcc leaves it

    go(main)


def verdicts_of(rig):
    seen = []
    real = rig.cmd._stop_result

    def spy(outcome, *a):
        seen.append(outcome)
        return real(outcome, *a)

    rig.cmd._stop_result = spy
    return seen




def test_start_200_link_drop_no_95():
    async def main(R):
        rig = await R().start()
        rig.st.desired["phases"] = 1
        rig.plug()
        await rig.run(20)
        rig.fake.on_next("drop", fc=0x10, addr=200)
        with pytest.raises(WriteRefused):
            await rig.call(rig.cmd.async_start())
        assert (95, 1) not in rig.writes()

    go(main)



# --- (continued) -------------------------------------------------------------------


# --- rate limits ----------------------------------------------------------------------


def test_no_start_pause_after_stop():
    """: no 180 s pause after a stop (not in evcc; A3C_3.1 restarted
    77 s after a stop). The start quota stays."""

    async def main(R):
        rig = await charging(R)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        await rig.run(10)  # a poll sees the stopped station (10 s polls)
        assert (await rig.call(rig.cmd.async_start())).outcome == "sent"
        await rig.idle()
        assert rig.writes()[-2:] == [(91, 160), (95, 1)]

    go(main)


def test_stop_never_limited():
    async def main(R):
        rig = await charging(R, stop="ignored")
        for _ in range(3):
            result = await rig.call(rig.cmd.async_stop())
            assert result.write.acknowledged
        assert rig.writes().count((95, 2)) == 3

    go(main)


# --- single flight ---------------------------------------------------------------------


# --- persistence -----------------------------------------------------------------------


# --- link loss -------------------------------------------------------------------------


# --- -------------------------------------------------------------------------


def test_no_disarm_after_status4_seen():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.run(8)
        rig.fake.car_pause()  # 4 seen, back to 2
        await rig.idle()
        assert (95, 2) not in rig.writes() and "start_not_taken" not in rig.kinds()

    go(main)


# --- sampling --------------------------------------------------------------------------


def test_no_energy_gap_during_sequence():
    async def main(R):
        rig = await R(start_stall=True, stall_s=12.0).start()
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.idle()
        await rig.run(300)
        assert rig.meter.diag["gaps"] == 0 and rig.meter.record is not None
        assert not rig.meter.record.approximate and rig.meter.record.integral_wh > 0

    go(main)


# --- -----------------------------------------------------------------------------------


# --- -----------------------------------------------------------------------------------




# --- phase switch while charging -------------------------------------------


async def otf_rig(R, *, options=OTF, car=None, decimal="no", **quirks):
    """Charging on 3 phases with the beta option on. Default: the whole-amp
    class; ``decimal="yes"`` is the new
    class (200 written directly)."""
    if decimal == "no":
        quirks.setdefault("whole_amps", True)
    rig = await charging(R, options=options, car=car or Car(phases=3), decimal=decimal, **quirks)
    await rig.run(61)  # past SETTLE_S of the start
    assert rig.st.snapshot.phases_in_use == 3
    return rig


def timed(rig, n=0):
    return [(t, r, v) for t, r, v in rig.fake.writes[n:]]


# --- whole-amp class: the EM2GO driver's workaround -------------------


def test_workaround_switch_3_to_1_stop_10s_start_then_200():
    """Whole-amp class charging on 3 phases, the user picks 1: like the EM2GO
    workaround - reg 95 reads 1 -> 95 = 2, 91 = 0, 10 s, 95 = 1, the current,
    then 200 = 1. No verification, no learning, no chatter watchdog."""

    async def main(R):
        rig = await otf_rig(R)
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_set_phases(1))
        await rig.idle()
        w = timed(rig, n)
        assert [(r, v) for _t, r, v in w] == [(95, 2), (91, 0), (95, 1), (91, 160), (200, 1)]
        t = {(r, v): at for at, r, v in w}
        assert t[(95, 1)] - t[(95, 2)] >= 10.0
        await rig.run(40)
        assert rig.st.snapshot.phases == 1 and rig.st.snapshot.delivering
        assert "phase_write_with_car" not in rig.st.learned.as_dict()
        assert not {"phase_chatter", "phase_switch_failed", "phase_reverted"} & set(rig.kinds())

    go(main)


def test_workaround_switch_not_logged_as_unexpected_phase_change(caplog):
    """ / step 2: the whole-amp path (stop, 10 s, start, 200) is an
    own switch too - no INFO "instead of" for it."""
    import logging

    async def main(R):
        rig = await otf_rig(R)
        await rig.run(30)
        assert rig.st.snapshot.phases_in_use == 3
        with caplog.at_level(logging.DEBUG):
            await rig.call(rig.cmd.async_set_phases(1))
            await rig.idle()
            await rig.run(120)
        assert rig.st.snapshot.phases_in_use == 1
        info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert not any("instead of" in m for m in info), info

    go(main)


def test_workaround_switch_1_to_3_minimum_first_200_1_again_then_3():
    """1 -> 3: evcc core scalePhases drops to the minimum first (every
    charger); the workaround's start re-writes 200 = 1 and the current (the
    minimum), then 200 = 3; the user's current comes back afterwards."""

    async def main(R):
        rig = await one_phase_charge(R, decimal="no", whole_amps=True)
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_set_phases(3))
        await rig.idle()
        await rig.run(15)
        assert rig.writes()[n:] == [(91, 60), (95, 2), (91, 0), (95, 1), (200, 1), (91, 60), (200, 3), (91, 160)]

    go(main)


def test_workaround_switch_not_enabled_writes_200_only():
    """evcc Enabled() is reg 95 == 1: a charge under Plug & Charge (95 never
    written, reads 2) gets only the 200 write."""

    async def main(R):
        rig = await R(decimal="no", whole_amps=True).start()
        rig.fake.mode = 0
        rig.plug(Car(phases=3))
        await rig.run(60)
        assert rig.fake.status == 4 and rig.fake.command == 2
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_set_phases(1))
        await rig.idle()
        assert rig.writes()[n:] == [(200, 1)]

    go(main)


def test_workaround_user_stop_in_the_pause_ends_it_record_user_stop():
    """A user stop during the 10 s pause cancels the switch at once (no start
    after it) and closes the record as user_stop."""

    async def main(R):
        rig = await otf_rig(R)
        await rig.call(rig.cmd.async_set_phases(1))
        for _ in range(20):
            await rig.run(1)
            if rig.cmd._seq is not None and rig.cmd._seq.step == "pause":
                break
        assert rig.cmd._seq.step == "pause"
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        await rig.run(30)
        assert (95, 1) not in rig.writes()[n:] and (200, 1) not in rig.writes()[n:]
        assert rig.meter.record is None and rig.meter.last.reason == "user_stop"

    go(main)


def test_workaround_options_and_learned_value_removed():
    """Item 7: no pause/settle options, no learned phase_write_with_car; a
    Store with the old key still loads."""
    import dataclasses

    from gnlt_charger.modbus.profiles import LearnedCaps

    names = {f.name for f in dataclasses.fields(CommandOptions)}
    assert not {"phase_switch_pause_s", "phase_car_settle_s"} & names
    assert "phase_write_with_car" not in LearnedCaps({"phase_write_with_car": "bad"}).as_dict()


def test_direct_switch_3_to_1_new_class_writes_200_only():
    """evcc driver phases1p3p (new class): only 200, also while charging; no
    stop, no current drop before 3 -> 1 (evcc core scalePhases); no settle
    rule of our own before the next switch."""

    async def main(R):
        rig = await otf_rig(R, decimal="yes")
        rid = rig.meter.record.rid
        n = len(rig.fake.writes)
        res = await rig.call(rig.cmd.async_set_phases(1))
        assert res.outcome == C.WRITE_OK
        await rig.run(30)
        assert rig.writes()[n:] == [(200, 1)]
        assert rig.st.snapshot.delivering and rig.meter.record.rid == rid
        assert rig.cmd.sequence is None
        await rig.call(rig.cmd.async_set_phases(3))  # at once: no 60 s settle refusal
        assert (200, 3) in rig.writes()[n:]

    go(main)


def test_direct_switch_1_to_3_drops_to_minimum_first():
    async def main(R):
        rig = await charging(R, options=OTF, car=Car(phases=3))
        rig.fake.phases = 1
        rig.st.desired["phases"] = 1
        await rig.run(80)
        assert rig.st.snapshot.phases == 1
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_set_phases(3))
        await rig.idle()
        await rig.run(15)
        # the user's current back at once (evcc's next setLimit; no coalescing)
        assert rig.writes()[n:] == [(91, 60), (200, 3), (91, 160)]

    go(main)


async def one_phase_charge(R, **kw):
    rig = await charging(R, options=kw.pop("options", OTF), car=Car(phases=3), **kw)
    rig.fake.phases = 1
    rig.st.desired["phases"] = 1
    await rig.run(80)
    assert rig.st.snapshot.phases == 1
    return rig


def test_direct_switch_drops_when_station_phases_unknown():
    import dataclasses

    async def main(R):
        rig = await one_phase_charge(R)
        n = len(rig.fake.writes)
        rig.st._snap = dataclasses.replace(rig.st.snapshot, phases=None)  # 200 not read this poll
        await rig.call(rig.cmd.async_set_phases(3))
        assert rig.writes()[n:][:2] == [(91, 60), (200, 3)]

    go(main)


@pytest.mark.parametrize("during", ["current", "stop"])
def test_direct_switch_slot_not_stale(during):
    """After the 1 -> 3 drop the user's current goes back only if nothing
    newer is in the slot, recomputed, and never after a cancelling stop."""

    async def main(R):
        rig = await one_phase_charge(R)
        real = rig.st.async_write

        async def write(register, value, **kw):
            if register == 200:
                if during == "current":
                    await rig.cmd.async_set_current(12.0)  # goes to the slot
                else:
                    rig.cmd._cancel(rig.cmd._seq, "stop")
            return await real(register, value, **kw)

        rig.st.async_write = write
        n = len(rig.fake.writes)
        try:
            await rig.call(rig.cmd.async_set_phases(3))
        except WriteRefused:
            assert during == "stop"
        await rig.idle()
        await rig.run(15)
        after = [w for w in rig.writes()[n:] if w[0] == 91]
        assert after == ([(91, 60), (91, 120)] if during == "current" else [(91, 60)])

    go(main)


@pytest.mark.parametrize("case", ["plug_and_charge"])
def test_direct_switch_no_prechecks_of_our_own(case):
    """the new class switches like evcc's
    phases1p3p - no Plug & Charge or learned-chatter refusal of our own."""

    async def main(R):
        rig = await otf_rig(R, decimal="yes")
        if case == "plug_and_charge":
            rig.fake.menu_set_mode(0)
            await rig.run(11)
        else:
            rig.st.learned.set("phase_write_with_car", "bad")
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_set_phases(1))
        assert rig.writes()[n:] == [(200, 1)]

    go(main)


def test_direct_switch_refused_for_unknown_class():
    async def main(R):
        rig = await otf_rig(R, decimal="yes")
        rig.st.learned.set("decimal_current", "unknown")
        n = len(rig.fake.writes)
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_set_phases(1))
        assert err.value.key == "phase_refused_class_unknown"
        assert len(rig.fake.writes) == n

    go(main)




def test_phase_charging_new_class_switches_directly_with_default_options():
    """no "phase switching while charging" option -
    the new class always writes 200 directly while charging, like evcc's
    phases1p3p (1 -> 3: the current drops to the minimum first, as evcc core
    scalePhases)."""

    async def main(R):
        rig = await otf_rig(R, options=CommandOptions(), decimal="yes")
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_set_phases(1))
        await rig.idle()
        assert (200, 1) in rig.writes()[n:] and (95, 2) not in rig.writes()[n:]
        assert rig.st.desired["phases"] == 1

    go(main)


def test_phase_switch_while_charging_option_removed():
    import dataclasses

    assert "phase_switch_while_charging" not in {f.name for f in dataclasses.fields(CommandOptions)}


# --- settle window ----------------------------------------------------------------------


# --- schedule -------------------------------------------------------------------------


def window(start_off=5, length=30, stop=True):
    from gnlt_charger.logic import Schedule

    start = 12 * 60 + start_off
    return Schedule(enabled=True, start_min=start, end_min=start + length, days="all", stop_at_end=stop)


async def minutes(rig, n):
    for _ in range(n):
        await rig.call(rig.cmd.async_schedule_tick(), budget=10)
        await rig.run(60)


def sched(**kw):
    return CommandOptions(schedule=window(**kw))


def test_schedule_start_once_per_window():
    async def main(R):
        rig = await R(options=sched()).start()
        rig.plug()
        await minutes(rig, 12)
        assert rig.writes().count((95, 1)) == 1
        rig.fake.car_finish()
        await minutes(rig, 15)
        assert rig.writes().count((95, 1)) == 1

    go(main)


def test_schedule_stop_sent_even_with_flag():
    async def main(R):
        rig = await R(options=sched()).start()
        rig.plug()
        await minutes(rig, 10)
        assert rig.fake.status == 4
        rig.cmd._set_stop_not_working(True)
        await minutes(rig, 30)
        assert rig.writes()[-2:] == [(95, 2), (91, 0)]

    go(main)


test_flag_does_not_suppress_configured_stop = test_schedule_stop_sent_even_with_flag


def test_schedule_end_stops_paused_charge():
    async def main(R):
        rig = await R(options=sched()).start()
        rig.plug()
        await minutes(rig, 10)
        rig.fake.car_pause()
        await minutes(rig, 30)
        assert rig.writes()[-2:] == [(95, 2), (91, 0)] and rig.writes().count((95, 1)) == 1

    go(main)


test_schedule_no_start_into_long_car_pause = test_schedule_end_stops_paused_charge


@pytest.mark.parametrize("late", [False, True])
def test_schedule_no_restart_after_user_stop_in_window(late):
    async def main(R):
        rig = await R(options=sched(stop=False)).start()
        rig.plug()
        await minutes(rig, 8)
        await rig.idle()
        assert rig.fake.status == 4
        if late:
            rig.fake.on_next("late", fc=0x10, addr=95, delay=3.0)
        await rig.call(rig.cmd.async_stop())
        await minutes(rig, 20)
        assert rig.writes().count((95, 1)) == 1

    go(main)


def test_schedule_respects_unknown_outcome_stop():
    test_schedule_no_restart_after_user_stop_in_window(True)


def test_idle_stop_does_not_block_schedule():
    async def main(R):
        rig = await R(options=sched()).start()
        rig.plug()
        await rig.run(20)
        result = await rig.call(rig.cmd.async_stop())
        assert result.outcome == "not_needed" and rig.writes() == []
        await minutes(rig, 8)
        assert rig.writes().count((95, 1)) == 1

    go(main)


# --- auto start ------------------------------------------------------------------------

AUTO = CommandOptions(auto_start=True)


def test_auto_start_once_per_physical_plug():
    async def main(R):
        rig = await R(options=AUTO).start()
        rig.plug()
        await rig.run(40)
        await rig.idle()
        assert rig.writes().count((95, 1)) == 1
        rig.fake.car_finish()
        await rig.run(300)
        assert rig.writes().count((95, 1)) == 1
        rig.fake.unplug()
        await rig.run(30)
        rig.plug()
        await rig.run(40)
        assert rig.writes().count((95, 1)) == 2

    go(main)


def test_auto_start_flapping_status_no_loop():
    async def main(R):
        rig = await R(options=AUTO).start()
        rig.plug(Car(asleep=True))
        await rig.run(30)
        await rig.idle()
        for code in (1, 2, 1, 2, None):
            rig.fake.force_status(code)  # CP fault flaps, the cable stays
            await rig.run(20)
        await rig.run(300)
        assert rig.writes().count((95, 1)) == 1

    go(main)


# --- restart actions -----------------------------------------------------------------


async def power_cut(R, **kw):
    rig = await R(power_loss_93_to_0=False, **kw).start()
    rig.st.desired["current"] = 100
    rig.plug()
    await rig.run(20)
    await rig.call(rig.cmd.async_start())
    await rig.idle()
    await rig.run(130)
    assert rig.st.snapshot.delivering
    n = len(rig.fake.writes)
    rig.fake.power_loss()
    await rig.run(400)
    return rig, rig.writes()[n:]


def test_power_loss_resume_on_after_startup():
    async def main(R):
        rig, after = await power_cut(R, options=CommandOptions(resume_after_power_loss=True))
        assert (91, 100) in after and (95, 1) in after
        assert any(k == "station_restarted" and d["resume"] for k, d in rig.notes)
        assert rig.st.diagnostics()["write_log"][-1]["origin"] in ("resume", "restore")

    go(main)


test_resume_after_startup_ends = test_power_loss_resume_on_after_startup


def test_power_loss_resume_off_notifies_no_stop():
    async def main(R):
        rig, after = await power_cut(R)
        assert after == [(91, 100)]  # only the restore
        assert "station_restarted" in rig.kinds()

    go(main)


def test_93_repair_one_click_restore():
    async def main(R):
        rig = await R().start()
        rig.fake.menu_set_mode(0)
        await rig.run(11)
        assert C.PROBLEM_PNC_RESET in rig.st.snapshot.problems and rig.writes() == []
        await rig.call(rig.cmd.async_pnc_restore())
        assert rig.writes() == [(93, 1)]
        await rig.run(11)
        assert C.PROBLEM_PNC_RESET not in rig.st.snapshot.problems

    go(main)


# --- self start ----------------------------------------------------------------------


def test_self_start_reported_not_stopped():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        rig.fake.external_start()
        await rig.run(40)
        assert rig.cmd.charging_without_session  # shown by the HA layer while it lasts
        assert rig.writes() == []
        rig.fake.unplug()
        await rig.run(30)
        assert not rig.cmd.charging_without_session

    go(main)


test_charging_without_session_flag = test_self_start_reported_not_stopped


def test_no_self_start_on_resume_after_pause():
    async def main(R):
        rig = await charging(R)
        rig.fake.car_pause()
        await rig.run(120)
        rig.fake.car_resume()
        await rig.run(40)
        assert "self_start" not in rig.kinds() and not rig.cmd.charging_without_session

    go(main)



# --- phase selection -----------------------------------------------------------------


def test_phase_idle_no_car_writes_now():
    async def main(R):
        rig = await R().start()
        assert (await rig.call(rig.cmd.async_set_phases(1))).outcome == C.WRITE_OK
        assert rig.writes() == [(200, 1)] and rig.st.desired["phases"] == 1

    go(main)


def test_idle_200_skip_if_equal():
    async def main(R):
        rig = await R().start()
        assert await rig.call(rig.cmd.async_set_phases(3)) == "not_needed"
        assert rig.writes() == []

    go(main)


def test_phase_car_connected_written_directly():
    """: evcc core writes 200 while the charger is disabled (fresh
    state + read-back as for any write); no option."""

    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        res = await rig.call(rig.cmd.async_set_phases(1))
        assert res.outcome == C.WRITE_OK
        assert rig.writes() == [(200, 1)] and rig.st.desired["phases"] == 1 and not rig.cmd.phase_pending
        await rig.run(120)
        await rig.call(rig.cmd.async_start())
        assert rig.writes() == [(200, 1), (91, 160), (95, 1)]

    go(main)


@pytest.mark.parametrize("case", ["car_pause", "plug_and_charge"])
def test_phase_car_connected_with_open_session_follows_charging_rules(case):
    """evcc lp.enabled: a station with an open/armed session (or in Plug &
    Charge, where it starts by itself) is "while charging": the class rules
    apply - the new class writes 200 directly (no option any more)."""

    async def main(R):
        if case == "car_pause":
            rig = await charging(R)
            rig.fake.car_pause()  # 4 -> 2, the record stays open
        else:
            rig = await R().start()
            rig.plug()
            await rig.run(20)
            rig.fake.mode = 0
        await rig.run(20)
        assert rig.st.snapshot.status == 2
        n = len(rig.writes())
        await rig.call(rig.cmd.async_set_phases(1))
        assert rig.writes()[n:] == [(200, 1)]

    go(main)


def test_phase_car_pause_option_on_new_class_drops_before_1_to_3():
    async def main(R):
        rig = await one_phase_charge(R)
        rig.fake.car_pause()
        await rig.run(20)
        assert rig.st.snapshot.status == 2 and rig.st.record_open
        n = len(rig.writes())
        await rig.call(rig.cmd.async_set_phases(3))
        assert rig.writes()[n:][:2] == [(91, 60), (200, 3)]

    go(main)


def test_station_change_keeps_user_choice_pending():
    async def main(R):
        rig = await R().start()
        rig.fake.phases = 1  # changed in the station menu
        await rig.run(11)
        assert rig.st.desired["phases"] == 3 and rig.cmd.phase_pending
        assert rig.writes() == []

    go(main)


def test_no_phase_write_after_unplug():
    """The after-unplug write of a pending choice is gone (E4): a differing
    choice is written at the next start only."""

    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        rig.fake.phases = 1  # the station changed it while the car is in
        await rig.run(40)
        rig.fake.unplug()
        await rig.run(120)
        assert rig.writes() == [] and rig.cmd.phase_pending

    go(main)


def test_phase_never_written_unprompted():
    async def main(R):
        rig = await R(options=CommandOptions(auto_start=True)).start()
        for _ in range(6):
            rig.plug()
            await rig.run(600)
            rig.fake.car_finish()
            await rig.run(600)
            rig.fake.unplug()
            await rig.run(2400)
        assert all(r != 200 for r, _v in rig.writes())

    go(main)


# --- cancellable waits -------------------------------------------------------------------


def test_user_stop_cancels_wait_immediately():
    async def main(R):
        rig = await otf_rig(R)
        await rig.call(rig.cmd.async_set_phases(1))
        await rig.run(5)  # inside the stop verification / pause of the whole-amp sequence
        assert rig.cmd.sequence == "phase"
        t0 = rig.clock()
        await rig.call(rig.cmd.async_stop(), step=0.1)
        await rig.run(0.5)
        assert rig.cmd.sequence in (None, "stop") and rig.clock() - t0 < 2.0
        await rig.idle()
        assert (200, 1) not in rig.writes()

    go(main)




def test_no_gentle_stop_in_stage1():
    """ dropped: a user stop from 3 phases is one immediate 95 = 2 (no current drop first)."""

    async def main(R):
        rig = await charging(R, car=Car(phases=3))
        n = len(rig.fake.writes)
        t0 = rig.clock()
        await rig.call(rig.cmd.async_stop(), step=0.1)
        assert rig.writes()[n:] == [(95, 2), (91, 0)] and rig.fake.writes[n][0] - t0 < 1.0

    go(main)


# --- mutation-pass gaps ---------------------------------------------------------------


def test_direct_switch_inside_settle_window_not_refused():
    """No 60 s settle refusal of our own after a start (evcc has none for phases1p3p)."""

    async def main(R):
        rig = await charging(R, options=OTF, car=Car(phases=3))
        n = len(rig.writes())
        await rig.call(rig.cmd.async_set_phases(1))
        assert rig.writes()[n:] == [(200, 1)]

    go(main)


def test_no_self_start_report_for_plug_and_charge():
    async def main(R):
        rig = await R().start()
        rig.fake.mode = 0
        rig.plug()  # Plug & Charge starts by itself: expected, not a self start
        await rig.run(60)
        assert rig.fake.status == 4 and "self_start" not in rig.kinds()

    go(main)


# --- step-3 probes ported -------------------


def test_stop_right_after_pnc_off_not_refused():
    """M3 (probe P3): the stop decides on its own fresh 93, and the snapshot
    shows a confirmed 93 write at once."""

    async def main(R):
        rig = await R().start()
        rig.fake.mode = 0
        rig.plug()
        await rig.run(60)
        assert rig.fake.status == 4
        await rig.run(4.0)
        await rig.call(rig.cmd.async_set_plug_and_charge(False))
        assert rig.st.snapshot.mode == 1
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        assert (95, 2) in rig.writes() and rig.fake.status == 2

    go(main)


def test_apply_stop_with_bad_current_still_stops():
    """MIN 1 (probe P5)."""

    async def main(R):
        rig = await charging(R)
        with pytest.raises(WriteRefused):
            await rig.call(rig.cmd.apply(Target(enabled=False, current_a=5.0)))
        await rig.idle()
        assert (95, 2) in rig.writes()

    go(main)


def test_unload_before_commit_releases_caller():
    """MIN 3 (probe P7)."""

    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        gate = asyncio.Event()
        real = rig.st.async_write

        async def slow_write(register, value, **kw):
            if register == 91:
                await gate.wait()
            return await real(register, value, **kw)

        rig.st.async_write = slow_write
        caller = asyncio.ensure_future(rig.cmd.async_start())
        await rig.run(2)
        assert not caller.done() and rig.cmd.sequence == "start"
        await rig.cmd.async_close()
        gate.set()  # the write in flight completes (the client is closed at a real unload)
        await rig.run(5)
        assert caller.done()
        with pytest.raises(WriteRefused):
            caller.result()
        assert (95, 1) not in rig.writes()  # nothing after the unload

    go(main)


def test_stop_after_start_keeps_record_reason():
    """MIN 4: a stop that lands while our 95 = 1 is still being judged keeps
    its record reason (note_start never comes after it)."""

    async def main(R):
        rig = await charging(R)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        await rig.run(20)
        assert rig.meter.last.reason == "user_stop"
        from gnlt_charger.modbus.station import WriteResult

        rig.cmd._last_stop_at = rig.clock() + 5  # a stop after this 95 = 1
        calls = []
        rig.se.note_start = lambda: calls.append(1)
        rig.cmd._after_start_sent(WriteResult(95, 1, "user", C.WRITE_OK, True, 1, rig.clock()))
        assert calls == []

    go(main)


# --- mutants of round 1 that need tests (50, 54) ------------------------------------


# --- step-3 probes ported ---------------
# (test_rr_* = the reviewer's Q/P probes, asserting the spec or the orchestrator decision)

from datetime import timedelta  # noqa: E402

def _w(rig, n=0):
    return [(round(t), r, v) for t, r, v in rig.fake.writes[n:]]


# P2' (round-1 P2 with its wrong status assert removed).
def test_rr_p5_stop_first_then_refusal():
    async def main(R):
        rig = await charging(R)
        err = None
        try:
            await rig.call(rig.cmd.apply(Target(enabled=False, current_a=5.0)))
        except Exception as e:  # noqa: BLE001
            err = e
        await rig.idle()
        assert (95, 2) in rig.writes()
        assert err is not None and getattr(err, "key", None) == C.REFUSE_CURRENT_RANGE

    go(main)


# Q1 M4 across an HA restart: a house power cut takes HA and the station down
# together over a stop-at-end window end; with resume ON the charge must not
# be started again after the window closed.
def test_rr_q4_wall_clock_behind_at_load():
    async def main(R):
        store = MemoryStore()
        rig = await charging(R, store=store)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        await rig.run(5)
        await rig.st.async_save()
        saved = dict(store.data)
        await rig.close()
        rig2 = R(store=MemoryStore(saved), clock=rig.clock, fake=rig.fake)
        rig2.cmd._wall = lambda: rig.wall() - timedelta(hours=1)  # clock 1 h behind when HA starts
        await rig2.start(20)
        rig2.cmd._wall = rig.wall  # NTP fixed the clock
        await rig2.run(300)  # 5 min after the stop
        err = None
        try:
            await rig2.call(rig2.cmd.async_start())
        except Exception as e:  # noqa: BLE001
            err = e
        assert err is None, err

    go(main)


# Q5 corrupt store section: a non-list "starts" must not break the setup.
def test_rr_q5_corrupt_times_does_not_break_load():
    async def main(R):
        store = MemoryStore()
        rig = await charging(R, store=store)
        await rig.st.async_save()
        saved = dict(store.data)
        saved["commands"] = dict(saved["commands"])
        saved["commands"]["times"] = dict(saved["commands"]["times"], starts=5, last_stop=12)
        await rig.close()
        rig2 = R(store=MemoryStore(saved), clock=rig.clock, fake=rig.fake)
        await rig2.start(20)

    go(main)


# Q10 unload while a start waits in its 91 step and a current change sits in
# the slot: nothing may be written after the unload.
def test_rr_q10_unload_spawns_no_task():
    import asyncio

    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        gate = asyncio.Event()
        real = rig.st.async_write

        async def slow_write(register, value, **kw):
            if register == 91 and not gate.is_set():
                await gate.wait()
            return await real(register, value, **kw)

        rig.st.async_write = slow_write
        caller = asyncio.ensure_future(rig.cmd.async_start())
        await rig.run(2)
        assert rig.cmd.sequence == "start"
        cur = asyncio.ensure_future(rig.cmd.async_set_current(10.0))
        await rig.run(1)
        n = len(rig.fake.writes)
        await rig.cmd.async_close()
        gate.set()
        await rig.run(60)
        assert not rig.cmd._tasks
        # only the start's write that was in flight; no 95 = 1, no slot flush after the unload
        assert [(r, v) for _t, r, v in rig.fake.writes[n:]] in ([], [(91, 160)])
        assert caller.done() and isinstance(caller.exception(), Exception)  # released, not left waiting

    go(main)


# Q11 MIN 1 follow-up: "stop and switch to 1 phase" (stage-2 style target).
# The stop goes out; the phase choice must at least be kept as the user's
# pending choice, not refused "busy" by the stop's own verification.
def test_rr_q11_stop_plus_phases():
    async def main(R):
        rig = await charging(R, car=Car(phases=3))
        err = None
        try:
            await rig.call(rig.cmd.apply(Target(enabled=False, phases=1)))
        except Exception as e:  # noqa: BLE001
            err = e
        await rig.idle()
        assert (95, 2) in rig.writes()
        assert rig.st.desired.get("phases") == 1, err

    go(main)


# Q12 M4 race (HA up): the station comes back before the window end but is
# still booting (status 10) when the stop-at-end window closes; the resume
# decided at the restart event runs after the window closed.
@pytest.mark.parametrize("quirk", [{"power_loss_status10_s": 30.0}, {"power_loss_zero_reads": 1}, {}])
def test_rr_q12b_resume_with_status10(quirk):
    async def main(R):
        rig, after = await power_cut(R, options=CommandOptions(resume_after_power_loss=True), **quirk)
        assert (95, 1) in after, (after, rig.kinds())

    go(main)


# Q13 missed stop when the link drops right at the window end (no
# cooldown yet): the stop's write is "not sent" instead of refused.
def test_rr_q14_ha_down_over_window_end_stop_sent():
    async def main(R):
        store = MemoryStore()
        opts = CommandOptions(schedule=window(start_off=5, length=10))
        rig = await R(options=opts, store=store).start()
        rig.plug()
        await minutes(rig, 12)
        await rig.idle()
        await rig.st.async_save()
        saved = dict(store.data)
        await rig.close()
        await rig.run(5 * 60)  # HA down 12:12-12:17
        rig2 = R(options=opts, store=MemoryStore(saved), clock=rig.clock, fake=rig.fake)
        await rig2.start(5)
        await minutes(rig2, 3)
        await rig2.idle()
        assert rig2.writes().count((95, 2)) == 1

    go(main)


# Q15 kills mutant m10 (start quota not restored): 3 starts in 15 min, HA
# restart, a 4th start must be refused (start_rate_limited).
# --- round-3 mutation gaps --------------------------------------------------------------------


def test_no_task_spawned_after_close():
    async def main(R):
        rig = await R().start()
        await rig.cmd.async_close()
        task = rig.cmd._spawn(asyncio.sleep(0))
        assert task is None and not rig.cmd._tasks
        rig.plug()
        await rig.run(40)  # plug event, snapshots: nothing may start
        assert not rig.cmd._tasks and rig.writes() == []

    go(main)


# --- final review ------------------------------------------------


def test_call_in_flight_at_unload_returns():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        gate = asyncio.Event()
        real = rig.st.async_write

        async def slow_write(register, value, **kw):
            if register == 91 and not gate.is_set():
                await gate.wait()
            return await real(register, value, **kw)

        rig.st.async_write = slow_write
        caller = asyncio.ensure_future(rig.cmd.apply(Target(enabled=True, current_a=10.0)))
        await rig.run(2)
        await rig.cmd.async_close()
        gate.set()
        await rig.run(120)
        assert caller.done()
        if not caller.cancelled():
            caller.exception()

    go(main)


def test_plan_reads_only_even_documented_registers():
    groups = list(C.TIER_A) + list(C.TIER_B) + [
        C.SERIAL_GROUP, C.PHASES_GROUP, *C.CONNECTION_GROUPS, C.HEARTBEAT_GROUP, C.COMM_TIMEOUT_GROUP,
    ]
    for g in groups:
        if g.u32:
            assert g.requests == ((g.address, 2),) and g.used == (g.address, g.address + 1)
        else:
            assert g.requests == tuple((r, 1) for r in g.used), g
    # The registers on the wire: a count-2 read covers a and a + 2 on a stride-2 box.
    real = {a + 2 * i for g in groups for a, n in g.requests for i in range(n)}
    assert real == {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42,
                    78, 80, 87, 89, 91, 93, 95, 109, 111, 113, 200}, sorted(real)


@pytest.mark.parametrize("quirk", [{"power_loss_status10_s": 30.0}, {"power_loss_zero_reads": 1}, {}])
def test_resume_with_93_reset_ends_charging(quirk):
    """R3-4 without its mode assertion (orchestrator): after a power cut that
    resets 93 to 0 and shows 10 / zeros first, the car ends up
    charging - by Plug & Charge itself or by our resume."""

    async def main(R):
        rig = await R(options=CommandOptions(resume_after_power_loss=True), power_loss_93_to_0=True, **quirk).start()
        rig.st.desired["current"] = 100
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.idle()
        await rig.run(130)
        rig.fake.power_loss()
        await rig.run(400)
        await rig.idle()
        assert rig.fake.status in (3, 4)

    go(main)
