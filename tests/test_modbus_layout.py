"""Request shape like evcc.

evcc's EM2GO driver reads every 16-bit register with its own FC03 request of
count 1 (``getPhaseValues``: 6, 8, 10 and 109, 111, 113 one by one) and a U32
value with one count-2 request. The integration does the same everywhere:
station polling, identity, fresh state and the config-flow probe. With no
block reads the block-read layout of the box no longer
matters, so its self-check, the probe's layout step and the
``block_read_layout_unconfirmed`` repair are gone.
"""

import asyncio

from gnlt_charger.modbus import config_flow as mb_flow
from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import notify as mb_notify
from gnlt_charger.modbus.station import Station
from modbus_fake import FakeClient, FakeStation, FakeTcpStation, SimClock
from modbus_ha import SERIAL, make_hass, stop, translations
from test_modbus_config_flow import through_address

U32_REGISTERS = {C.REG_POWER, C.REG_POWER_L1, C.REG_POWER_L2, C.REG_POWER_L3, C.REG_METER, C.REG_DURATION}


def run(coro):
    return asyncio.run(coro)


def _fc03(requests):
    return [(addr, count) for _t, fc, addr, count in requests if fc == 0x03]


def _assert_evcc_shape(reads):
    assert reads
    for addr, count in reads:
        if addr in U32_REGISTERS:
            assert count == 2, (addr, count)  # U32: one count-2 read (evcc CurrentPower / TotalEnergy)
        else:
            assert count == 1, (addr, count)  # 16-bit: one register per request (evcc getPhaseValues)


def test_station_poll_reads_like_evcc():
    """Identity, tier A, tier B and the phase probe of a fresh connection:
    count 1 per 16-bit register, count 2 only for U32 values; the serial is
    three single reads; no block-layout check (no (32, 2))."""

    async def main():
        clock = SimClock()
        fake = FakeStation("A3C_2.7", SERIAL, clock)
        client = FakeClient(fake)
        st = Station(client, SERIAL, clock=clock, sleep=clock.sleep)
        await st.async_tick()
        reads = _fc03(client.sent)
        _assert_evcc_shape(reads)
        assert reads[:3] == [(38, 1), (40, 1), (42, 1)]  # identity first
        assert (32, 2) not in reads
        assert [r for r in reads if r[0] in (6, 8, 10)] == [(6, 1), (8, 1), (10, 1)]
        assert [r for r in reads if r[0] in (109, 111, 113)] == [(109, 1), (111, 1), (113, 1)]
        assert st.identity == C.IDENTITY_OK
        snap = st.snapshot
        assert (snap.max_current_a, snap.min_current_a, snap.cable_max_a) == (16.0, 6.0, 16.0)
        await st.async_stop()

    run(main())


def test_contiguous_box_reads_the_same_values():
    """A box answering block reads a, a+1 (the documents' model) needs no
    detection any more: single reads give the same 16-bit values."""

    async def main():
        values = []
        for stride in (2, 1):
            clock = SimClock()
            fake = FakeStation("A3C_2.7", SERIAL, clock, stride=stride)
            fake.min_tenths = 70
            fake.cable_max = 130
            client = FakeClient(fake)
            st = Station(client, SERIAL, clock=clock, sleep=clock.sleep)
            await st.async_tick()
            snap = st.snapshot
            values.append((st.identity, snap.min_current_a, snap.cable_max_a, snap.voltages, snap.status_key))
            await st.async_stop()
        assert values[0] == values[1]
        assert values[0][:3] == (C.IDENTITY_OK, 7.0, 13.0)

    run(main())


def test_probe_reads_one_register_per_request():
    async def main():
        model = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True))
        model.min_tenths = 70
        model.cable_max = 130
        model.plug()
        model.set_error(19)
        server = FakeTcpStation(model)
        await server.start()
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result["step_id"] == "modbus_model", result
            assert "block_reads" not in flow._modbus
            regs = flow._modbus["registers"]
            assert regs[32] == 160 and regs[34] == 70 and regs[36] == 130, regs
            assert regs[2] == 1 and regs[4] == 19, regs
            reads = [(r[3], r[4]) for r in model.requests if r[2] == 3]
            assert reads and all(count == 1 for _a, count in reads), reads
            assert reads[:3] == [(38, 1), (40, 1), (42, 1)]
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_block_layout_check_and_repair_removed():
    assert not hasattr(C, "PROBLEM_STRIDE_UNCONFIRMED") and not hasattr(C, "EVENT_BLOCK_LAYOUT")
    assert not hasattr(C, "STRIDE_CONFIRMED") and not hasattr(C, "group_addresses")
    assert not hasattr(mb_flow, "async_step_layout_note")
    assert "block_read_layout_unconfirmed" not in mb_notify.ISSUES
    for lang in ("strings", "en", "ru", "pl"):
        t = translations(lang)
        assert "block_read_layout_unconfirmed" not in t["issues"], lang
        assert "modbus_layout_note" not in t["config"]["step"], lang
