"""The fake station itself (tests/modbus_fake.py): the quirks other tests rely on
behave as measured on the stations."""

import asyncio
import struct

import pytest

from gnlt_charger.modbus import client as mc
from gnlt_charger.modbus.client import ModbusClient
from modbus_fake import Car, FakeClient, FakeStation, FakeTcpStation, SimClock, serial_words


def _read(fake, sid, addr, count):
    return fake.request(sid, 255, struct.pack(">BHH", 3, addr, count))


def _write(fake, sid, addr, value):
    return fake.request(sid, 255, struct.pack(">BHHBH", 0x10, addr, 1, 2, value))


def _words(result):
    assert result[0] == "reply", result
    pdu = result[1]
    return list(struct.unpack(f">{pdu[1] // 2}H", pdu[2:]))


def _session(fake):
    verdict, s = fake.connect()
    assert verdict == "accept"
    return s.sid


def test_registers_and_serial():
    fake = FakeStation("A3C_2.7")
    sid = _session(fake)
    assert _words(_read(fake, sid, 38, 3)) == serial_words("301234567890") == [0x3012, 0x3456, 0x7890]
    assert _words(_read(fake, sid, 0, 3)) == [1, 0, 0]
    # Stride 2: 32x3 -> 32/34/36, 109x3 -> 109/111/113.
    assert _words(_read(fake, sid, 32, 3)) == [160, 60, 160]
    assert _words(_read(fake, sid, 109, 3)) == [2350, 20, 10]  # A3C_2.7 idle L2 = 2.0 V, L3 = 1.0 V
    assert _words(_read(fake, sid, 0, 4))  # a station answered a 4-register read
    b07 = FakeStation("A3B_2.7", "101234567891")
    sid = _session(b07)
    assert _read(b07, sid, 0, 4) == ("reply", bytes([0x83, 0xFF]))


def test_fc06_and_other_functions():
    fake = FakeStation()
    sid = _session(fake)
    assert fake.request(sid, 255, struct.pack(">BHH", 6, 91, 80)) == ("silent",)
    assert fake.setpoint == 160
    assert fake.request(sid, 255, struct.pack(">BHH", 4, 0, 1)) == ("reply", bytes([0x84, 1]))
    assert fake.request(sid, 1, struct.pack(">BHH", 3, 0, 1)) == ("silent",)


def test_91_stored_without_validation():
    fake = FakeStation()
    sid = _session(fake)
    _write(fake, sid, 91, 0)
    assert fake.setpoint == 50  # 0 is stored as 50
    _write(fake, sid, 91, 1)
    assert fake.setpoint == 1
    fake.set_quirks(whole_amps=True)
    _write(fake, sid, 91, 61)
    assert fake.setpoint == 60


def test_start_stop_and_b07_stuck():
    clock = SimClock()
    fake = FakeStation("A3C_2.7", clock=clock)
    sid = _session(fake)
    fake.plug(Car())
    assert fake.status == 2
    _write(fake, sid, 95, 1)
    clock.advance(3)
    assert _words(_read(fake, sid, 0, 1)) == [4]
    assert fake.currents()[0] == pytest.approx(14.7)
    _write(fake, sid, 95, 2)
    assert fake.status == 2 and fake.currents() == [0, 0, 0]

    b07 = FakeStation("A3B_2.7", "101234567891", clock=clock)
    sid = _session(b07)
    b07.plug(Car())
    _write(b07, sid, 95, 1)
    clock.advance(5)
    _read(b07, sid, 0, 1)
    _write(b07, sid, 95, 2)
    assert b07.status == 4 and b07.currents()[0] == 7.5  # car minimum, still charging
    _write(b07, sid, 91, 80)
    assert b07.currents()[0] == 7.5  # stuck until a re-plug
    b07.unplug()
    b07.plug(Car())
    _write(b07, sid, 95, 1)
    clock.advance(5)
    _read(b07, sid, 0, 1)
    assert b07.status == 4


def test_pnc_and_95_with_93_zero():
    clock = SimClock()
    fake = FakeStation(clock=clock)
    sid = _session(fake)
    fake.mode = 0
    fake.plug(Car())
    clock.advance(3)
    _read(fake, sid, 0, 1)
    assert fake.status == 4  # Plug & Charge starts by itself
    fake.unplug()
    fake.plug(Car())  # still P&C
    fake.mode = 0
    fake.unplug()
    fake.mode = 1
    fake.plug(Car())
    fake.mode = 0
    _write(fake, sid, 95, 1)
    assert fake.status == 2 and fake.command == 1  # echoed, no effect (docs)


def test_restart_after_stop_e31_needs_77s():
    clock = SimClock()
    fake = FakeStation("A3C_3.1", "302606014614", clock=clock)
    sid = _session(fake)
    fake.plug(Car())
    _write(fake, sid, 95, 1)
    clock.advance(5)
    _read(fake, sid, 0, 1)
    _write(fake, sid, 95, 2)
    clock.advance(30)
    _write(fake, sid, 95, 1)
    assert fake.status == 2
    clock.advance(50)
    _write(fake, sid, 95, 1)
    assert fake.status == 3


def test_power_loss():
    clock = SimClock()
    fake = FakeStation("A3B_2.7", "101234567891", clock=clock)
    sid = _session(fake)
    fake.setpoint = 100
    fake.plug(Car())
    fake.power_loss()
    assert fake.connect() == ("refuse", None)
    assert fake.request(sid, 255, struct.pack(">BHH", 3, 0, 1)) == ("drop", None)
    clock.advance(21)
    sid = _session(fake)
    assert (fake.mode, fake.setpoint, fake.phases) == (0, 160, 3)
    clock.advance(3)
    _read(fake, sid, 0, 1)
    assert fake.status in (3, 4)  # B07 resumed by itself (P&C after power-on)


def test_lazy_reg28_and_zero_profiles():
    clock = SimClock()
    b07 = FakeStation("A3B_2.7", "101234567891", clock=clock)
    sid = _session(b07)
    b07.mode = 0
    b07.plug(Car())
    for _ in range(40):
        clock.advance(60)
        _read(b07, sid, 28, 2)
    value = _words(_read(b07, sid, 28, 2))[1]
    assert value > 0 and value % 3 == 0  # 0.3 kWh steps
    e27 = FakeStation("A3C_2.7", clock=clock)
    sid = _session(e27)
    e27.mode = 0
    e27.plug(Car())
    clock.advance(600)
    assert _words(_read(e27, sid, 28, 2)) == [0, 0]  # always 0 on A3C


def test_chatter_after_200_with_car_while_disabled():
    clock = SimClock()
    fake = FakeStation(clock=clock, chatter_on_200_with_car=True)
    sid = _session(fake)
    fake.plug(Car())
    _write(fake, sid, 200, 1)
    seen = set()
    for _ in range(10):
        clock.advance(1)
        result = _read(fake, sid, 0, 1)
        if result[0] == "reply":
            seen.add(_words(result)[0])
    assert seen == {2, 4}
    clock.advance(30)
    assert _read(fake, sid, 0, 1) == ("silent",)  # stops answering after 20-30 s
    # Written right before 95 = 1: no chatter.
    ok = FakeStation(clock=clock, chatter_on_200_with_car=True)
    sid = _session(ok)
    ok.plug(Car())
    _write(ok, sid, 200, 1)
    clock.advance(0.3)
    _write(ok, sid, 95, 1)
    clock.advance(5)
    assert _words(_read(ok, sid, 0, 1)) == [4] and ok._chatter_since is None


def test_menu_limit_and_dry_contact():
    clock = SimClock()
    fake = FakeStation(clock=clock, menu_limit_a=8.0)
    fake.mode = 0
    fake.plug(Car())
    clock.advance(3)
    fake._update()
    assert fake.currents()[0] == pytest.approx(7.4) and fake.register(91) == 160
    cap = FakeStation(clock=clock, dry_contact="cap_6a")
    cap.mode = 0
    cap.plug(Car(min_a=6.0, factor=1.0))
    clock.advance(3)
    cap._update()
    assert cap.currents()[0] == 6.0


def test_connection_rules():
    clock = SimClock()
    fake = FakeStation(clock=clock)
    first = _session(fake)
    verdict, second = fake.connect()
    assert verdict == "accept" and second.never_answer
    assert _read(fake, first, 0, 1) == ("silent",)  # the station hangs
    fake.close(first)
    fake.close(second.sid)
    clock.advance(10)
    sid = _session(fake)
    assert _read(fake, sid, 0, 1) == ("silent",)  # hang_s after both closed
    clock.advance(120)
    assert _read(fake, sid, 0, 1)[0] == "reply"
    fake.close(sid)
    fake.set_quirks(gap_mode="refuse")  # A3C_2.7 itself stays silent instead (test below)
    assert fake.connect() == ("refuse", None)  # within min_gap_s
    clock.advance(5)
    _session(fake)


def test_churn_refuses_even_with_gaps():
    clock = SimClock()
    fake = FakeStation(clock=clock, churn_limit=2, churn_window_s=60.0)
    verdicts = []
    for _ in range(5):
        verdict, s = fake.connect()
        verdicts.append(verdict)
        if s:
            fake.close(s.sid)
        clock.advance(10)
    assert verdicts[:2] == ["accept", "accept"] and "refuse" in verdicts[2:]


def test_half_open_session():
    clock = SimClock()
    fake = FakeStation(clock=clock)
    sid = _session(fake)
    fake.close(sid, fin=False)  # HA crashed: no FIN
    clock.advance(10)
    new = _session(fake)
    assert _read(fake, new, 0, 1) == ("silent",)
    clock.advance(120)
    assert _read(fake, new, 0, 1)[0] == "reply"


def test_zero_reads_after_power_on():
    clock = SimClock()
    fake = FakeStation(clock=clock, power_loss_zero_reads=2)
    fake.power_loss()
    clock.advance(21)
    sid = _session(fake)
    assert _words(_read(fake, sid, 38, 3)) == [0, 0, 0]
    assert _words(_read(fake, sid, 38, 3)) == [0, 0, 0]
    assert _words(_read(fake, sid, 38, 3)) == serial_words("301234567890")


def test_fake_client_rules():
    """Same rules as client.py (evcc): every timeout closes the session, the
    next request reconnects at once (the fake station's own reconnect rule
    then applies), a dropped read is retried once, a write never; an
    exception reply closes the session too (evcc)."""

    async def main():
        clock = SimClock()
        fake = FakeStation(clock=clock, min_gap_s=0.0)
        c = FakeClient(fake)
        fake.on_next("silent")
        with pytest.raises(mc.ModbusTimeoutError):
            await c.read_holding_registers(0, 1)
        assert c.consecutive_failures == 1 and c.state == mc.STATE_RECONNECTING
        assert await c.read_holding_registers(0, 1) == [1]  # no cooldown
        assert c.connections == 2 and c.consecutive_failures == 0
        fake.on_next("drop", fc=3)
        assert await c.read_holding_registers(0, 1) == [1]  # retried on a fresh session
        assert c.connections == 3
        fake.on_next("drop", fc=0x10)
        with pytest.raises(mc.ModbusConnectionError):
            await c.write_register(91, 80)
        assert fake.setpoint != 80 and c.connections == 3
        fake.on_next("exception", fc=3, code=2)
        with pytest.raises(mc.ModbusExceptionError):
            await c.read_holding_registers(0, 1)
        assert c.state == mc.STATE_DISCONNECTED and c.consecutive_failures == 0  # closed (evcc)
        assert c.connections == 4
        with pytest.raises(ValueError):
            await c.read_holding_registers(0, 4)

    asyncio.run(main())


def test_tcp_second_client_and_reconnect_silence():
    async def main():
        model = FakeStation(clock=SimClock(realtime=True), min_gap_s=0.0, hang_s=0.3)
        server = FakeTcpStation(model)
        await server.start()
        a = ModbusClient("127.0.0.1", server.port, delay=0.0, timeout=0.1)
        b = ModbusClient("127.0.0.1", server.port, delay=0.0, timeout=0.1)
        assert await a.read_holding_registers(0, 1) == [1]
        with pytest.raises(mc.ModbusTimeoutError):
            await b.read_holding_registers(0, 1)
        with pytest.raises(mc.ModbusTimeoutError):
            await a.read_holding_registers(0, 1)  # the first one hangs too
        await a.close()
        await b.close()
        await asyncio.sleep(0.4)
        c = ModbusClient("127.0.0.1", server.port, delay=0.0, timeout=0.1)
        assert await c.read_holding_registers(0, 1) == [1]
        assert server.accepts == 3
        await c.close()
        await server.stop()

    asyncio.run(main())


# --- fake drift fixed in step 2 round 2 -------------------------------


def test_a3c27_reconnect_silent_by_default_churn_switchable():
    """Measured: a 0 s reconnect is accepted but silent (timeout /
    empty), not refused (default). Repeated connects refused even with gaps
    is the churn rule - off by default since the evcc transport (it would
    never let the box recover, see test_n3_churn_rule_vs_evcc_reconnect),
    switched on here."""
    clock = SimClock()
    assert FakeStation("A3C_2.7", clock=clock).q.churn_limit is None
    fake = FakeStation("A3C_2.7", clock=clock, churn_limit=4)
    sid = _session(fake)
    fake.close(sid)
    verdict, s = fake.connect()  # 0 s after the close
    assert verdict == "accept" and _read(fake, s.sid, 0, 1) == ("silent",)
    fake.close(s.sid)
    verdicts = []
    for _ in range(3):
        clock.advance(10)
        verdict, s = fake.connect()
        verdicts.append(verdict)
        if s is not None:
            fake.close(s.sid)
    assert verdicts[-1] == "refuse"  # churn: 10 s gaps do not help


def test_sleeping_car_stays_status_2_after_start():
    """ needs it: 95 = 1 with a car asleep arms the box, status stays 2."""
    clock = SimClock()
    fake = FakeStation("A3C_2.7", clock=clock)
    sid = _session(fake)
    fake.plug(Car(asleep=True))
    _write(fake, sid, 95, 1)
    clock.advance(30)
    assert _words(_read(fake, sid, 0, 1)) == [2] and _words(_read(fake, sid, 95, 1)) == [1]


def test_stall_executes_writes_without_reply():
    clock = SimClock()
    fake = FakeStation("A3C_2.7", clock=clock, start_stall=True, stall_s=12.0)
    sid = _session(fake)
    fake.plug(Car())
    assert _write(fake, sid, 95, 1)[0] == "reply"
    clock.advance(1)
    assert _read(fake, sid, 0, 1) == ("silent",)
    assert _write(fake, sid, 95, 2) == ("silent",)  # no reply, but the box took it
    assert fake.status == 2 and fake.writes[-1][1:] == (95, 2)
    clock.advance(15)  # the first stall is over
    fake.set_quirks(stall_executes_writes=False)
    fake.plug(Car())
    assert _write(fake, sid, 95, 1)[0] == "reply"  # a new 12 s stall
    clock.advance(1)
    n = len(fake.writes)
    assert _write(fake, sid, 95, 2) == ("silent",) and len(fake.writes) == n


def test_fake_client_drop_after_write_ack_arrives():
    async def main():
        clock = SimClock()
        fake = FakeStation("unknown", clock=clock)
        client = FakeClient(fake)
        await client.read_holding_registers(0, 1)
        fake.on_next("drop_after_write", fc=0x10, ack=True, silence_s=30.0)
        await client.write_register(91, 80)  # acknowledged
        assert fake.setpoint == 80 and client.consecutive_failures == 0
        # The closed socket is found by the next request (EOF); the read is
        # sent again at once on a fresh connection (evcc), which this box
        # refuses 0 s after a close (fake rule min_gap_s).
        with pytest.raises(mc.ModbusConnectionError) as err:
            await client.read_holding_registers(91, 1)
        assert err.value.kind == mc.KIND_REFUSED
        assert client.connections == 1 and client.consecutive_failures == 2
        assert client.last_close_monotonic == clock()

    asyncio.run(main())


def test_fake_client_realistic_latency():
    async def main():
        clock = SimClock()
        fake = FakeStation("unknown", clock=clock)
        client = FakeClient(fake, realistic=True)
        t0 = clock()
        await client.read_holding_registers(0, 1)  # delay + connect (no settle) + one transaction
        t1 = clock()
        for _ in range(10):
            await client.read_holding_registers(0, 1)
        assert t1 - t0 == pytest.approx(0.06 + 0.03)
        assert (clock() - t1) / 10 == pytest.approx(0.09)

    asyncio.run(main())
