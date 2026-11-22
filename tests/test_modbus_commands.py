"""Command layer (modbus/commands.py) against the fake station on a virtual
clock."""

import pytest

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus.commands import REFUSAL_KEYS, CommandError, CommandOptions, Target
from gnlt_charger.modbus.station import WriteRefused
from modbus_cmd_rig import Car, MemoryStore, go
from test_modbus_sequences import charging


def refused(excinfo, key):
    assert excinfo.value.key == key, excinfo.value.key
    return True


# --- adoption and start-up ----------------------------------------------


def test_foreign_charge_adopted_no_write():
    async def main(R):
        rig = R(options=CommandOptions(auto_start=True, resume_after_power_loss=True))
        rig.fake.mode = 0
        rig.plug()  # Plug & Charge starts it before HA comes up
        rig.fake._update()
        await rig.start(120)
        assert rig.writes() == []
        assert rig.meter.record is not None and rig.st.snapshot.delivering
        assert "self_start" not in rig.kinds() and rig.cmd.charging_switch_state

    go(main)


test_startup_no_automatic_write_while_charging = test_foreign_charge_adopted_no_write


def test_startup_exception_only_91_restore():
    async def main(R):
        restored = {"control_active": True, "desired": {"current": 100, "mode": 1, "phases": 3}}
        rig = R(restored=restored, options=CommandOptions(auto_start=True))
        rig.plug()
        rig.fake.external_start()  # a charge runs while HA starts (93 = 1, started at the station)
        await rig.start(120)
        assert rig.writes() == [(91, 100)]
        assert rig.st.diagnostics()["write_log"][0]["origin"] == "restore"

    go(main)


def test_user_stop_not_refused_by_startup():
    async def main(R):
        rig = R()
        rig.plug()
        rig.fake.external_start()
        rig.fake._update()
        await rig.start(3)  # the first poll is done, start-up just ended
        result = await rig.call(rig.cmd.async_stop())
        assert result.write.acknowledged and rig.writes() == [(95, 2), (91, 0)]

    go(main)


def test_record_closes_on_own_stop_any_outcome():
    async def main(R):
        rig = await charging(R)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        await rig.run(20)
        assert rig.meter.record is None and rig.meter.last.reason == "user_stop"

    go(main)


# --- empty session ------------------------------------------------------------------------


# --- status 6 ---------------------------------------------------------------------------


def test_manual_start_from_6_allowed():
    async def main(R):
        rig = await R().start()
        rig.plug()
        rig.fake.force_status(6)
        await rig.run(20)
        result = await rig.call(rig.cmd.async_start())
        assert result.outcome == "sent" and (95, 1) in rig.writes()

    go(main)


def test_status6_after_stop_notifies():
    async def main(R):
        rig = await charging(R)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        rig.fake.car_finish()  # the box shows 6 after our stop
        await rig.run(20)
        assert rig.kinds().count("finished_replug") == 1

    go(main)


# --- -------------------------------------------------------------------------------------


# --- one re-write ---------------------------------------------------------------------


async def idle_rig(R, **kw):
    rig = await R(**kw).start()
    await rig.run(15)  # past the 10 s coalescing window of nothing
    return rig




# ---: the 6.1 A firmware probe (evcc initialize) ----------------------------------------


@pytest.mark.parametrize(("whole", "learned"), [(False, "yes"), (True, "no")])
def test_firmware_probe_61_decides_class_and_restores_91(whole, learned):
    async def main(R):
        rig = R(decimal=None, whole_amps=whole)
        rig.fake.setpoint = 100
        await rig.start_like_setup(30)
        assert rig.writes() == [(91, 61), (91, 100)]
        assert rig.st.learned["decimal_current"] == learned
        assert rig.fake.setpoint == 100
        assert {e["origin"] for e in rig.st.diagnostics()["write_log"]} == {"firmware_probe"}
        await rig.run(600)
        assert len(rig.writes()) == 2  # once: the result is kept

    go(main)


def test_firmware_probe_runs_during_a_charge_and_restores_the_target():
    """the 6.1 A probe runs at every integration
    start like evcc initialize - also during a running charge - and the
    start target is written back right after (evcc's next setLimit)."""

    async def main(R):
        rig = R(decimal=None)
        rig.plug()
        rig.fake.external_start()
        rig.st.desired["current"] = 100
        await rig.start_like_setup(60)
        writes = rig.writes()
        i = writes.index((91, 61))
        assert writes[i + 1] == (91, 100)
        assert rig.st.learned["decimal_current"] == "yes" and rig.fake.setpoint == 100

    go(main)


@pytest.mark.parametrize("case", ["car", "plug_and_charge", "stop_marker", "learned_before"])
def test_firmware_probe_at_every_start_regardless_of_state(case):
    """No idle / no-car / command-mode / marker / learned conditions any more
    (evcc writes 6.1 A at every start); a stop marker (our 91 = 0) is put
    back as the marker, otherwise the start target."""

    async def main(R):
        restored = {"learned": {"decimal_current": "no"}} if case == "learned_before" else None
        rig = R(decimal=None, restored=restored)
        if case == "car":
            rig.plug()
        elif case == "plug_and_charge":
            rig.fake.mode = 0
        elif case == "stop_marker":
            rig.fake.setpoint = 0  # our last stop's 91 = 0
        await rig.start_like_setup(60)
        assert rig.writes()[0] == (91, 61)
        assert rig.st.learned["decimal_current"] == "yes"  # the box keeps 6.1 A
        if case == "stop_marker":
            assert rig.writes()[1] == (91, 0) and rig.fake.setpoint in (0, 50)  # the box may store 0 as 5.0 A
        else:
            assert rig.writes()[1] == (91, 160)
        await rig.run(600)
        assert rig.writes().count((91, 61)) == 1  # once per start

    go(main)


# --- range -------------------------------------------------------------------------------


def test_current_range_from_regs():
    async def main(R):
        rig = R()
        rig.fake.min_tenths, rig.fake.max_tenths, rig.fake.cable_max = 80, 320, 250
        await rig.start(20)
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_set_current(7.0))
        assert refused(err, C.REFUSE_CURRENT_RANGE) and err.value.placeholders == {"min": 8.0, "max": 25.0}
        with pytest.raises(WriteRefused):
            await rig.call(rig.cmd.async_set_current(25.1))
        await rig.call(rig.cmd.async_set_current(25.0))
        assert rig.writes() == [(91, 250)]

    go(main)


test_current_refuses_out_of_range = test_current_range_from_regs


def test_current_step_0_1():
    async def main(R):
        rig = await idle_rig(R)
        await rig.call(rig.cmd.async_set_current(8.3))
        assert rig.writes() == [(91, 83)] and rig.st.learned["decimal_current"] == "yes"

    go(main)


@pytest.mark.parametrize("profile", ["A3B_2.7", "A3C_2.7", "A3C_3.1", "unknown"])
def test_current_10_1_to_15_9_allowed_on_every_profile(profile):
    """: the "skip 10.1-15.9 A" option is gone - the E0020 at
    11-15 A was a car that takes nothing below 7.5 A, not the station. 6.0-max in 0.1 A steps."""

    async def main(R):
        rig = await idle_rig(R, profile=profile)
        for amps in (10.1, 12.0, 15.9):
            await rig.call(rig.cmd.async_set_current(amps))
            await rig.run(11)
        assert rig.writes() == [(91, 101), (91, 120), (91, 159)]
        assert not hasattr(rig.cmd.options, "skip_10_16")

    go(main)


def test_desired_above_new_max_clamped_at_start_notifies():
    async def main(R):
        rig = await R().start()
        rig.st.desired["current"] = 200  # stored when the range was larger
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.idle()
        assert rig.writes()[0] == (91, 160)
        await rig.run(200)
        await rig.call(rig.cmd.async_stop())
        await rig.idle()
        await rig.run(200)
        await rig.call(rig.cmd.async_start())
        assert rig.kinds().count("current_clamped") == 1  # once per desired value

    go(main)


# --- -------------------------------------------------------------------------




def test_current_lower_draw_no_alarm():
    async def main(R):
        rig = await charging(R, menu_limit_a=8.0)  # the station's own menu limit is lower
        await rig.run(61)
        await rig.call(rig.cmd.async_set_current(12.0))
        await rig.run(30)
        assert "current_not_applied" not in rig.kinds()

    go(main)


def test_current_idle_written_and_stored():
    async def main(R):
        rig = await idle_rig(R)
        await rig.call(rig.cmd.async_set_current(9.5))
        assert rig.writes() == [(91, 95)] and rig.st.desired["current"] == 95

    go(main)


# --- single paths ---------------------------------------------------------


def test_all_origins_use_apply():
    """Manual controls, schedule, auto start, resume and the empty-session
    stop all go through apply(); starts through _start, stops through
    _stop."""
    from gnlt_charger.logic import Schedule

    async def main(R):
        sched = Schedule(enabled=True, start_min=12 * 60 + 2, end_min=12 * 60 + 40, days="all", stop_at_end=True)
        rig = R(options=CommandOptions(schedule=sched))
        calls, starts, stops = [], [], []
        real_apply, real_start, real_stop = rig.cmd.apply, rig.cmd._start, rig.cmd._stop

        async def apply(target, origin="user"):
            calls.append((target.enabled, origin))
            return await real_apply(target, origin)

        async def run_start(origin, **kw):
            starts.append(origin)
            return await real_start(origin, **kw)

        async def stop_now(origin, **kw):
            stops.append(origin)
            return await real_stop(origin, **kw)

        rig.cmd.apply, rig.cmd._start, rig.cmd._stop = apply, run_start, stop_now
        await rig.start()
        rig.plug()
        for _ in range(45):
            await rig.call(rig.cmd.async_schedule_tick(), budget=10)
            await rig.run(60)
        assert (True, "schedule") in calls and (False, "schedule") in calls
        assert starts == ["schedule"] and stops == ["schedule"]
        await rig.call(rig.cmd.async_start())
        await rig.call(rig.cmd.async_stop())
        assert (True, "user") in calls and (False, "user") in calls

    go(main)


test_all_starts_use_sequence = test_all_origins_use_apply
test_all_stops_use_sequence = test_all_origins_use_apply


# --- Plug & Charge ------------------------------------------------------------------------


def test_pnc_switch_writes_93():
    async def main(R):
        rig = await idle_rig(R)
        await rig.call(rig.cmd.async_set_plug_and_charge(True))
        await rig.call(rig.cmd.async_set_plug_and_charge(False))
        assert rig.writes() == [(93, 0), (93, 1)] and rig.st.desired["mode"] == 1

    go(main)


@pytest.mark.parametrize("mode", [0, 2])
def test_start_and_stop_sent_when_93_not_1(mode):
    async def main(R):
        rig = await R().start()
        rig.fake.forced_mode = mode
        rig.plug()
        await rig.run(20)
        # no 93 gate on a start either - evcc sends 95 = 1 anyway
        await rig.call(rig.cmd.async_start())
        assert rig.writes() == [(91, 160), (95, 1)]
        rig.fake.external_start()
        await rig.run(30)
        # no 93 gate on a stop - sent like evcc's Enable(false)
        res = await rig.call(rig.cmd.async_stop())
        assert res.outcome == "sent" and rig.writes()[-2:] == [(95, 2), (91, 0)]

    go(main)


def test_93_value_2_shown_raw():
    async def main(R):
        rig = await R().start()
        rig.fake.forced_mode = 2
        await rig.run(11)
        assert rig.st.snapshot.mode == 2

    go(main)


def test_93_never_written_by_setup():
    async def main(R):
        rig = R()
        rig.fake.mode = 0  # factory default: Plug & Charge
        await rig.start(60)
        await rig.run(600)
        assert rig.writes() == []

    go(main)


def test_pnc_reset_repair_cleared_when_93_reads_1():
    async def main(R):
        rig = await R().start()
        rig.fake.menu_set_mode(0)
        await rig.run(11)
        assert C.PROBLEM_PNC_RESET in rig.st.snapshot.problems
        rig.fake.menu_set_mode(1)  # fixed at the station
        await rig.run(11)
        assert C.PROBLEM_PNC_RESET not in rig.st.snapshot.problems

    go(main)


def test_93_write_during_charge_allowed():
    async def main(R):
        rig = await charging(R)
        await rig.call(rig.cmd.async_set_plug_and_charge(True))
        assert rig.writes()[-1] == (93, 0) and rig.fake.status == 4

    go(main)


def test_pnc_off_and_stop_button():
    async def main(R):
        rig = R()
        rig.fake.mode = 0
        rig.plug()  # P&C charge
        await rig.start(60)
        result = await rig.call(rig.cmd.async_pnc_off_and_stop())
        assert rig.writes() == [(93, 1), (95, 2), (91, 0)] and result.write.acknowledged
        await rig.idle()
        assert rig.fake.status == 2

    go(main)


# --- -------------------------------------------------------------------------------------


def test_command_errors_translated():
    async def main(R):
        rig = await R().start()
        seen = set()
        for call in (rig.cmd.async_set_current(3.0), rig.cmd.async_set_phases(2)):
            with pytest.raises(WriteRefused) as err:
                await rig.call(call)
            seen.add(err.value.key)
        for call in (rig.cmd.async_start(),):
            with pytest.raises(CommandError) as err:
                await rig.call(call)
            seen.add(err.value.key)
            assert err.value.translation_key == err.value.key
        assert seen <= REFUSAL_KEYS and {"current_out_of_range", "no_car"} <= seen

    go(main)


# --- coalescing -------------------------------------------------------------------------




# --- site control writes: no coalescing, like evcc --------------

SITE = "site"


def test_site_origin_constant():
    from gnlt_charger.modbus.commands import ORIGIN_SITE

    assert ORIGIN_SITE == SITE


def test_no_write_breaker_automatic_oscillation_written():
    """no write breaker - an automatic A-B-A-B of 91 (an
    old breaker trip) is written every time, like evcc."""

    async def main(R):
        rig = await idle_rig(R)
        assert not hasattr(rig.st, "breaker")
        for origin in (SITE, "schedule"):
            for amps in (10.0, 11.0, 10.0, 11.0, 10.0, 11.0):
                await rig.call(rig.cmd.apply(Target(current_a=amps), origin))
                await rig.run(12)
        assert [v for r, v in rig.writes()] == [100, 110, 100, 110, 100, 110] * 2

    go(main)


def test_current_gate_synchronous_error():
    async def main(R):
        rig = await idle_rig(R)
        rig.st.identity = C.IDENTITY_MISMATCH  # another station answers
        with pytest.raises(WriteRefused) as err:
            rig.cmd.validate_current(10.0)  # no await: refused when issued
        assert refused(err, C.REFUSE_IDENTITY_MISMATCH)

    go(main)


# --- ------------------------------------------------------------------------------------


def test_code23_blocks_all_starts():
    async def main(R):
        rig = await R(options=CommandOptions(auto_start=True)).start()
        rig.fake.set_error(23)
        await rig.run(11)
        rig.plug()
        await rig.run(40)
        assert rig.writes() == []
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_start())
        assert refused(err, "mb_faulted")  # OCPP-like: refused while the station reports the fault

    go(main)


# --- foreign 91 while the link stays up ---------------------------------------------------


# --- evcc NewEm2Go: 89 once, heartbeat 87 every 89 / 2 s --------------


def test_comm_timeout_read_once_then_heartbeat_reads_87():
    async def main(R):
        rig = R()
        rig.fake.comm_timeout = 60
        rig.fake.fallback = 100
        await rig.st.async_first_contact()
        assert await rig.st.async_read_comm_timeout() == 60
        assert rig.st.heartbeat_interval() == 30.0
        await rig.start(1)
        n = len(rig.client.sent)
        await rig.run(95)
        reads = [a for _t, fc, a, _n in rig.client.sent[n:] if fc == 3]
        assert reads.count(87) == 3 and 89 not in reads
        snap = rig.st.snapshot
        assert snap.comm_timeout_s == 60 and snap.fallback_current_a == 10.0  # sensors
        await rig.st.async_stop()
        n = len(rig.client.sent)
        await rig.run(60)
        assert len(rig.client.sent) == n  # stopped with the station

    go(main)


def test_heartbeat_survives_any_error_only_cancel_ends_it(caplog):
    """like evcc's heartbeat loop, any error of one cycle is logged
    and the next cycle runs."""

    async def main(R):
        rig = R()
        rig.fake.comm_timeout = 60
        await rig.st.async_first_contact()
        await rig.st.async_read_comm_timeout()
        real = rig.st._read
        calls = []

        async def flaky(address, count):
            if address == C.REG_FALLBACK_CURRENT:
                calls.append(address)
                if len(calls) == 1:
                    raise RuntimeError("boom")
            return await real(address, count)

        rig.st._read = flaky
        await rig.start(1)
        await rig.run(65)
        assert len(calls) == 2  # the second cycle ran after the error
        assert rig.st._heartbeat is not None and not rig.st._heartbeat.done()
        assert "heartbeat" in caplog.text
        await rig.st.async_stop()

    go(main)


def test_no_heartbeat_when_comm_timeout_is_0():
    async def main(R):
        rig = R()
        rig.fake.comm_timeout = 0
        await rig.st.async_first_contact()
        await rig.st.async_read_comm_timeout()
        assert rig.st.heartbeat_interval() is None
        await rig.start(65)
        assert 87 not in [a for _t, fc, a, _n in rig.client.sent if fc == 3]

    go(main)


# --- start-up like evcc NewEm2Go ---------------------------------------


def test_request_timeout_3s_like_evccs_modbus_fork():
    from gnlt_charger.modbus.runtime import new_client

    assert C.REQUEST_TIMEOUT_S == 3.0 and C.CONNECT_TIMEOUT_S == 3.0
    assert new_client("192.0.2.1", 502)._timeout == 3.0


def test_probe_runs_inside_the_first_contact_before_the_first_poll():
    """the start-up backoff wraps evcc's initialize: serial ->
    91 = 6.1 A -> read 91 -> 200; the class is known before any poll."""

    async def main(R):
        rig = R(decimal=None)
        await rig.st.async_first_contact(initialize=rig.cmd.async_initialize)
        assert rig.st.snapshot.time is None  # no poll yet
        ops = [(fc, a) for _t, fc, a, _x in rig.client.sent]
        assert ops[:3] == [(3, 38), (3, 40), (3, 42)]
        assert ops[3:6] == [(3, 32), (3, 34), (3, 36)]  # the per-connection limits, before the probe
        assert ops[6:10] == [(3, 91), (16, 91), (3, 91), (16, 91)] and (3, 200) in ops[10:]
        assert rig.st.learned["decimal_current"] == "yes" and rig.st.phase_capability is True

    go(main)


def test_probe_write_not_taken_is_retried_by_the_backoff():
    """a probe that does not go out is retried like evcc's
    backoff.RetryWithData(initialize)."""

    async def main(R):
        rig = R(decimal=None, min_gap_s=0.0, n3_silent_within_s=0.0)  # the reconnect-gap rule is not under test here
        rig.fake.on_next("silent", fc=0x10, addr=91)
        await rig.call(rig.st.async_first_contact(initialize=rig.cmd.async_initialize, jitter=lambda: 0.5), budget=60)
        probes = [a for _t, fc, a, _x in rig.client.sent if fc == 0x10 and a == 91]
        assert len(probes) == 3  # 61 lost, 61 again after the backoff, the current back
        assert rig.writes()[0] == (91, 61) and rig.st.learned["decimal_current"] == "yes"

    go(main)


def _clear_after_first_wait(rig, regs):
    real = rig.st._sleep

    async def sleep(seconds):
        for r in regs:
            rig.fake.overrides.pop(r, None)
        await real(seconds)

    rig.st._sleep = sleep


def test_identity_zeros_at_start_up_retried_then_initialize_runs():
    """a serial still reading zeros right after a connect
    is a failed try of the start-up backoff, not the end of it - otherwise
    the 6.1 A probe never runs and the class stays unknown all runtime."""

    async def main(R):
        rig = R(decimal=None, min_gap_s=0.0, n3_silent_within_s=0.0)  # the gap rule is not under test here
        rig.fake.overrides.update({38: 0, 40: 0, 42: 0})
        _clear_after_first_wait(rig, (38, 40, 42))
        await rig.call(rig.st.async_first_contact(initialize=rig.cmd.async_initialize, jitter=lambda: 0.5), budget=60)
        assert rig.st.identity == C.IDENTITY_OK
        assert rig.st.learned["decimal_current"] == "yes"

    go(main)


def test_identity_exception_reply_at_start_up_retried_then_initialize_runs():
    async def main(R):
        rig = R(decimal=None, min_gap_s=0.0, n3_silent_within_s=0.0)  # the gap rule is not under test here
        rig.fake.on_next("exception", fc=0x03, addr=38, code=4)
        await rig.call(rig.st.async_first_contact(initialize=rig.cmd.async_initialize, jitter=lambda: 0.5), budget=60)
        assert rig.st.learned["decimal_current"] == "yes"

    go(main)


def test_identity_never_verified_gives_up_like_other_errors():
    from gnlt_charger.modbus.client import ModbusError

    async def main(R):
        rig = R(decimal=None)
        rig.fake.overrides.update({38: 0, 40: 0, 42: 0})
        with pytest.raises(ModbusError):
            await rig.call(rig.st.async_first_contact(initialize=rig.cmd.async_initialize), budget=60)
        assert rig.writes() == []  # nothing written to an unverified station

    go(main)


def _above_16(rig, setpoint=250):
    rig.fake.max_tenths = 320
    rig.fake.cable_max = 320
    rig.fake.setpoint = setpoint


def test_probe_put_back_above_16a_without_desired():
    """the put-back clamped with the default 6-16 A range because
    32/34/36 were read only at the first poll - a station at 25.0 A ended at
    16 A for good. The limits are read at the first contact now (the same
    once-per-connection read, earlier), not read again by the first poll."""

    async def main(R):
        rig = R(decimal=None)
        _above_16(rig)
        await rig.start_like_setup()
        assert rig.fake.setpoint == 250, rig.writes()
        limit_reads = [a for _t, fc, a, _x in rig.client.sent if fc == 0x03 and a == 32]
        assert len(limit_reads) == 1  # one connection, one read

    go(main)


def test_probe_put_back_above_16a_with_desired():
    async def main(R):
        rig = R(decimal=None, restored={"desired": {"current": 250}})
        _above_16(rig, setpoint=200)
        await rig.start_like_setup()
        assert rig.fake.setpoint == 250, rig.writes()
        assert (91, 160) not in rig.writes()  # no dip to 16 A

    go(main)


def test_probe_retry_keeps_the_value_from_before_the_first_probe():
    """a retry after the probe write must not take our own 61 as
    the station's previous value."""

    async def main(R):
        rig = R(decimal=None, min_gap_s=0.0, n3_silent_within_s=0.0)  # the retry may reconnect at once
        rig.fake.setpoint = 120  # 12.0 A, inside the default range: only the retry matters
        from gnlt_charger.modbus.client import ModbusTimeoutError

        real, calls = rig.st.async_read, []

        async def read(group):
            calls.append(group)
            if len(calls) == 2:  # the read back after the probe write: lost -> the backoff tries again
                raise ModbusTimeoutError("read 91+1: no reply")
            return await real(group)

        rig.st.async_read = read
        await rig.call(rig.st.async_first_contact(initialize=rig.cmd.async_initialize), budget=60)
        assert rig.writes().count((91, 61)) == 2, rig.writes()  # the backoff tried again
        assert rig.fake.setpoint == 120, rig.writes()

    go(main)


def test_retried_setup_does_not_take_our_own_probe_value_as_the_users_current():
    """the first runtime gives up after its probe write (91 =
    6.1 A stays on the station); HA retries the whole setup with a new
    runtime. Its "value before the probe" is our own 61 - it must not be put
    back nor seeded as the user's current (first install, no desired, no
    site)."""
    from gnlt_charger.modbus.client import ModbusError, ModbusTimeoutError

    async def main(R):
        rig1 = R(decimal=None, min_gap_s=0.0, n3_silent_within_s=0.0)
        real, calls = rig1.st.async_read, []

        async def read(group):
            calls.append(group)
            if len(calls) >= 2:  # every read after the probe write is lost
                raise ModbusTimeoutError("read 91+1: no reply")
            return await real(group)

        rig1.st.async_read = read
        with pytest.raises(ModbusError):
            await rig1.call(rig1.st.async_first_contact(initialize=rig1.cmd.async_initialize, jitter=lambda: 0.5), budget=60)
        assert rig1.fake.setpoint == 61
        await rig1.st.async_stop()
        await rig1.run(10)
        rig2 = R(decimal=None, fake=rig1.fake, clock=rig1.clock)
        await rig2.start_like_setup()
        assert rig2.st.desired["current"] != 61, rig2.writes()
        assert rig2.fake.setpoint != 61

    go(main)


def test_firmware_class_not_persisted():
    """the class comes from the probe of each start only."""

    async def main(R):
        rig = await R(decimal="no").start()
        assert "learned" not in rig.st.store_data()
        rig2 = R(decimal=None, restored={"learned": {"decimal_current": "no"}})
        assert rig2.st.learned["decimal_current"] == "unknown"

    go(main)
