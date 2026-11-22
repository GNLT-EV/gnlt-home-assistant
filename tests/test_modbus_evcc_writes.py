"""Writes like evcc drives an EM2GO box: one request per write, no
fresh read, no read-back, no hold, no verification by sampling."""

import pytest

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus.commands import CommandOptions
from gnlt_charger.modbus.station import WriteRefused
from modbus_cmd_rig import Car, MemoryStore, go


def requests(rig, n=0):
    """(fc, address) of every request since ``n``."""
    return [(fc, a) for _t, fc, a, _x in rig.client.sent[n:]]


async def charging(R, **kw):
    car = kw.pop("car", None)
    rig = R(**kw)
    await rig.start()
    rig.plug(car)
    await rig.run(20)
    await rig.call(rig.cmd.async_start())
    await rig.run(60)
    assert rig.st.snapshot.delivering
    return rig


def test_start_is_phases_current_95_only_no_reads():
    """evcc core order - 200 (the choice differs) -> 91 -> 95 = 1, one
    request each; no fresh read, no read-back, no verification reads."""

    async def main(R):
        rig = await R().start()
        rig.plug(Car(phases=3))
        await rig.run(20)
        rig.st.desired["phases"] = 1
        n = len(rig.client.sent)
        await rig.call(rig.cmd.async_start())
        assert requests(rig, n) == [(16, 200), (16, 91), (16, 95)]
        assert rig.writes()[-3:] == [(200, 1), (91, 160), (95, 1)]
        assert rig.cmd.sequence is None

    go(main)


def test_stop_is_95_2_then_91_0_no_fresh_read():
    """evcc Enable(false) - 95 = 2 then 91 = 0, no 93 check, no fresh read."""

    async def main(R):
        rig = await charging(R)
        n = len(rig.client.sent)
        res = await rig.call(rig.cmd.async_stop())
        assert res.outcome == "sent"
        assert requests(rig, n) == [(16, 95), (16, 91)]
        assert rig.writes()[-2:] == [(95, 2), (91, 0)]

    go(main)


def test_stop_under_plug_and_charge_is_sent():
    """no 93 gate - a charge under Plug & Charge gets the evcc stop too."""

    async def main(R):
        rig = await R().start()
        rig.fake.mode = 0
        rig.plug()
        await rig.run(60)
        assert rig.st.snapshot.status == C.STATUS_CHARGING
        res = await rig.call(rig.cmd.async_stop())
        assert res.outcome == "sent" and rig.writes()[-2:] == [(95, 2), (91, 0)]

    go(main)


def test_stop_of_an_idle_station_writes_nothing():
    """reg 95 reads 2 and nothing charges - nothing to stop."""

    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        assert rig.st.snapshot.last_command == C.COMMAND_STOP
        res = await rig.call(rig.cmd.async_stop())
        assert res.outcome == "not_needed" and rig.writes() == []

    go(main)


def test_current_written_at_once_equal_value_skipped():
    """no 10 s coalescing, no fresh read; a value equal to the last
    written is not written again (evcc setLimit)."""

    async def main(R):
        rig = await R().start()
        n = len(rig.client.sent)
        await rig.call(rig.cmd.async_set_current(8.0))
        await rig.call(rig.cmd.async_set_current(9.0))
        await rig.call(rig.cmd.async_set_current(9.0))
        assert rig.writes() == [(91, 80), (91, 90)]
        assert requests(rig, n) == [(16, 91), (16, 91)]

    go(main)


def test_no_current_rewrite_and_no_not_applied_flag():
    """a write the station acknowledges but does not keep is not written
    again; no current_not_applied flag or notification."""

    async def main(R):
        rig = await R().start()
        rig.fake.on_next("ack_not_stored", fc=0x10)
        await rig.call(rig.cmd.async_set_current(8.0))
        await rig.run(30)
        assert rig.writes() == [(91, 80)]
        assert "current_not_applied" not in rig.kinds()
        assert "current_not_applied" not in rig.st.snapshot.problems

    go(main)


def test_phase_write_once_no_rewrite():
    """200 written once; no read-back, no phase_not_applied."""

    async def main(R):
        rig = await R().start()
        rig.fake.on_next("ack_not_stored", fc=0x10)
        n = len(rig.client.sent)
        await rig.call(rig.cmd.async_set_phases(1))
        await rig.run(30)
        assert rig.writes() == [(200, 1)]
        assert requests(rig, n)[:1] == [(16, 200)] and (3, 200) not in requests(rig, n)[:2]
        assert "phase_not_applied" not in rig.kinds()

    go(main)


def test_direct_1_to_3_no_200_when_the_minimum_write_failed():
    """evcc scalePhases returns when setLimit errors - 200 only after
    the minimum write went out; no read-back proof."""

    async def main(R):
        rig = await charging(R, car=Car(phases=3))
        rig.fake.phases = 1
        rig.st.desired["phases"] = 1
        await rig.run(30)
        rig.fake.on_next("exception", fc=0x10, addr=91, code=4)
        n = len(rig.fake.writes)
        with pytest.raises(WriteRefused):
            await rig.call(rig.cmd.async_set_phases(3))
        assert (200, 3) not in rig.writes()[n:]
        await rig.run(20)  # the exception closed the connection (evcc); the poll reconnects
        await rig.call(rig.cmd.async_set_phases(3))
        assert rig.writes()[n:][:2] == [(91, 60), (200, 3)]

    go(main)


def test_no_automatic_stop_on_status_5_or_7():
    """a start that ends in status 5/7 is not stopped by us (the
    faulted notification stays with the station's error code)."""

    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        rig.fake.force_status(C.STATUS_START_FAILED)
        await rig.run(120)
        assert (95, 2) not in rig.writes()

    go(main)


def test_stop_not_working_is_an_observation_only():
    """the stop went out, the polls still show current after 30 s ->
    the flag; no learned value, no notification, no extra reads."""

    async def main(R):
        rig = await charging(R, stop="ignored")
        n = len(rig.client.sent)
        await rig.call(rig.cmd.async_stop())
        await rig.run(45)
        assert rig.cmd.stop_not_working
        await rig.run(10)
        assert "stop_not_working" in rig.st.snapshot.problems
        assert "stop_not_working" not in rig.kinds() and "stop_works" not in str(rig.st.store_data().get("commands"))
        reads = [a for fc, a in requests(rig, n) if fc == 3]
        assert reads.count(0) == reads.count(95)  # only the polls read (no verification sampling)
        rig.fake.unplug()
        await rig.run(30)
        assert not rig.cmd.stop_not_working

    go(main)


def test_write_refused_in_a_status_evcc_does_not_accept():
    """no "first poll after a reconnect" or "link reconnecting" gate;
    only a status evcc's driver does not map (here 7) refuses."""

    async def main(R):
        rig = await R().start()
        rig.fake.force_status(C.STATUS_FAULT)
        await rig.run(20)
        with pytest.raises(WriteRefused) as err:
            await rig.call(rig.cmd.async_set_current(8.0))
        assert err.value.key == C.REFUSE_STATION_BUSY and rig.writes() == []

    go(main)


def test_write_on_a_new_connection_reads_the_serial_first():
    """One serial read per new connection, also before a write."""

    async def main(R):
        rig = await R().start()
        rig.st._identity_conn = None  # a connection whose serial was not read yet
        n = len(rig.client.sent)
        await rig.call(rig.cmd.async_set_current(8.0))
        assert requests(rig, n) == [(3, 38), (3, 40), (3, 42), (16, 91)]

    go(main)


def test_no_persisted_sequence_step_old_store_loads():
    """the running step is not saved; an old Store with one loads
    without a notification."""

    async def main(R):
        store = MemoryStore({"commands": {"sequence": {"kind": "start", "step": "start"}, "stop_not_working": True}})
        rig = await R(store=store).start()
        assert "sequence_interrupted" not in rig.kinds() and not rig.cmd.stop_not_working
        rig.plug()
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        assert "sequence" not in rig.st.store_data()["commands"]

    go(main)


def test_whole_amp_start_reasserts_one_phase_and_the_current():
    """The EM2GO driver's Enable(true) on the workaround class: after 95 = 1
    it writes 200 = 1 again (on one phase) and the current."""

    async def main(R):
        rig = await R(decimal="no", whole_amps=True).start()
        rig.fake.phases = 1
        rig.st.desired["phases"] = 1
        rig.plug(Car(phases=3))
        await rig.run(20)
        n = len(rig.fake.writes)
        await rig.call(rig.cmd.async_start())
        assert rig.writes()[n:] == [(91, 160), (95, 1), (200, 1), (91, 160)]

    go(main)


def test_options_unchanged_by_the_write_path():
    assert CommandOptions().empty_session_stop is True
