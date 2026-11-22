"""Station layer (modbus/station.py) against the fake station.

Most tests run the station on ``FakeClient`` with a simulated clock (minutes
pass instantly); the ones about sockets, overlapping requests and unload use
the real ``ModbusClient`` over ``FakeTcpStation``.
"""

import asyncio
import logging
import re
from pathlib import Path

import pytest

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus.client import STATE_CLOSED, ModbusClient
from gnlt_charger.modbus.station import (
    Snapshot,
    Station,
    StationTiming,
    WriteRefused,
    decode_serial,
    decode_u32,
    error_key,
    status_key,
    store_key,
)
from modbus_fake import DOCUMENTED, Car, FakeClient, FakeStation, FakeTcpStation, SimClock

SERIAL = "301234567890"
OTHER = "302606014614"
STATION_PY = Path(__file__).resolve().parents[1] / "custom_components" / "gnlt_charger" / "modbus" / "station.py"


def run(coro):
    return asyncio.run(coro)


def make(
    profile="A3C_2.7", *, restored=None, preset_max_a=None, realistic=False, **quirks,
):
    """``realistic``: the client spends ~0.09 s per transaction (evcc's 60 ms
    delay + latency), no settle after a connect."""
    clock = SimClock()
    fake = FakeStation(profile, SERIAL, clock, **quirks)
    client = FakeClient(fake, realistic=realistic)
    station = Station(
        client, SERIAL, restored=restored, clock=clock, sleep=clock.sleep,
        preset_max_a=preset_max_a,
    )
    return fake, client, station, clock


async def drive(station, clock, seconds):
    """What the poll loop does, on the simulated clock."""
    end = clock() + seconds
    while clock() < end:
        started = clock()
        await station.async_tick()
        elapsed = clock() - started
        await clock.sleep(max(station.next_delay() - elapsed, station.timing.min_poll_s - elapsed, 0.0))


def writes(fake):
    return [(reg, value) for _t, reg, value in fake.writes]


def reads(client):
    return [(addr, arg) for _t, fc, addr, arg in client.sent if fc == 0x03]


class MemoryStore:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0
        self.removed = False
        self.delayed = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data
        self.saves += 1

    async def async_remove(self):
        self.data = None
        self.removed = True

    def async_delay_save(self, func, delay):
        self.delayed += 1


# --- decoding -------------------------------------------------------------


def test_decode_u32_hi_first():
    assert decode_u32({12: 0x0000, 13: 0x2B20}, 12) == 11040
    assert decode_u32({28: 0, 29: 486}, 28) * C.SCALE_ENERGY == pytest.approx(48.6)
    assert decode_u32({28: 0x0000, 29: 0x8339}, 28) * C.SCALE_ENERGY == pytest.approx(3359.3)
    assert decode_u32({28: 1}, 28) is None  # half a pair is never a value


def test_decode_serial_bcd():
    assert decode_serial([0x1012, 0x3456, 0x7891]) == "101234567891"
    assert decode_serial([0x3012, 0x3456, 0x7890]) == SERIAL
    assert decode_serial([0x3012, 0x34AB, 0x7890]) is None
    assert decode_serial([0x3012, 0x3456]) is None


def test_decode_scales():
    async def main():
        fake, _c, st, _clock = make()
        fake.overrides.update({6: 0x0039, 109: 0x0938})
        await st.async_tick()
        assert st.snapshot.currents[0] == pytest.approx(5.7)
        assert st.snapshot.voltages[0] == pytest.approx(236.0)
        assert st.snapshot.setpoint_a == pytest.approx(16.0)

    run(main())


def test_u32_pairs_in_one_request():
    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await drive(st, clock, 130)
        for addr, count in reads(client):
            for pair in (12, 16, 20, 24, 28, 78):
                if addr == pair or addr == pair + 1:
                    assert (addr, count) == (pair, 2)

    run(main())


def test_per_phase_power_raw_only():
    async def main():
        fake, _c, st, clock = make(per_phase_unit=10)
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 80)  # tier B (60 s) read while charging
        snap = st.snapshot
        assert snap.per_phase_power_raw[0] and snap.per_phase_power_raw[0] < snap.power_w
        assert snap.power_w == decode_u32(snap.registers, 12)  # reg 12 as read, no sum

    run(main())


# --- read plan -----------------------------------------------


def test_read_plan_tier_b_and_connect():
    async def main():
        _f, client, st, clock = make()
        await st.async_tick()
        plan = reads(client)
        assert plan[:3] == [(38, 1), (40, 1), (42, 1)]  # identity first, no layout check
        tier_a = [r for g in C.TIER_A for r in g.requests]
        tier_b = [r for g in C.TIER_B for r in g.requests]
        assert plan[3 : 3 + len(tier_a)] == tier_a
        assert all(g in plan for g in tier_b)
        client.sent.clear()
        await clock.sleep(10)
        await st.async_tick()
        assert not any(g in reads(client) for g in tier_b)
        client.sent.clear()
        await clock.sleep(51)
        await st.async_tick()
        assert all(g in reads(client) for g in tier_b)

    run(main())


def test_read_plan_tier_a():
    async def main():
        _f, client, st, clock = make()
        await st.async_tick()
        client.sent.clear()
        await clock.sleep(10)
        await st.async_tick()
        expected = [r for g in C.TIER_A for r in g.requests] + [(200, 1)]
        assert reads(client) == expected  # 200 read because the capability is known

    run(main())


def test_read_plan_count_1_or_u32_count_2():
    """evcc request shape: count 1 per 16-bit register, count 2 only for a U32."""
    for g in (*C.TIER_A, *C.TIER_B, C.SERIAL_GROUP, C.PHASES_GROUP, *C.CONNECTION_GROUPS):
        if g.u32:
            assert g.requests == ((g.address, 2),) and g.used == (g.address, g.address + 1), g
        else:
            assert g.requests == tuple((r, 1) for r in g.used), g

    async def main():
        _f, client, st, clock = make()
        await drive(st, clock, 300)
        assert max(count for _a, count in reads(client)) <= 2

    run(main())


def test_station_exception_reply_closes_socket_poll_dropped_then_identity():
    """Single reads (evcc). An exception reply is a valid reply (no link
    failure) but evcc closes the connection on it: the next request of the
    poll opens a new one, so that poll is dropped and the next poll checks
    the identity on the new connection first. No odd filler address is read.
    (fake min_gap_s=0: a box that accepts a 0-s reconnect.)"""

    async def main():
        fake, client, st, clock = make(min_gap_s=0.0)
        await st.async_tick()
        fake.on_next("exception", fc=3, addr=78, code=2)
        await clock.sleep(10)
        client.sent.clear()
        await st.async_tick()
        assert client.connections == 2 and st.last_failure is None
        plan = reads(client)
        assert (78, 2) in plan and plan[-1] == (91, 1)  # the poll stopped at the new connection
        await clock.sleep(10)
        client.sent.clear()
        await st.async_tick()
        plan = reads(client)
        assert plan[:3] == [(38, 1), (40, 1), (42, 1)] and (78, 2) in plan
        assert {a for a, _n in plan} <= DOCUMENTED  # no filler address between registers
        assert st.snapshot.duration_s is not None
        assert st.link_state == C.LINK_CONNECTED

    run(main())


def test_no_daheimladen_registers_used():
    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await drive(st, clock, 600)
        touched = {a + 2 * i for a, n in reads(client) for i in range(n)}  # U32: words a and a + 2
        assert not touched & C.DAHEIMLADEN_ONLY_REGS
        assert max(n for _a, n in reads(client)) <= 2

    run(main())


# --- identity -----------------------------------------------------------


def test_identity_read_first_on_connect():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        assert reads(client)[:3] == [(38, 1), (40, 1), (42, 1)]
        fake.kill_sessions()  # the station drops the socket
        await clock.sleep(10)
        client.sent.clear()
        await st.async_tick()  # idle close found: EOF, the read goes again after the 5 s gap
        await clock.sleep(10)
        await st.async_tick()
        assert client.connections == 2
        assert reads(client)[:3] == [(38, 1), (40, 1), (42, 1)]

    run(main())


def test_identity_mismatch_drops_data():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        other = FakeStation("A3C_3.1", OTHER, clock)
        other.setpoint = 99
        other.plug(Car())
        client.retarget(other)
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        await clock.sleep(10)
        await st.async_tick()
        await clock.sleep(10)
        await st.async_tick()
        snap = st.snapshot
        assert st.identity == C.IDENTITY_MISMATCH and C.EVENT_IDENTITY_MISMATCH in events
        assert snap.setpoint_a == 16.0 and snap.cable == 0  # nothing of the other station
        assert C.PROBLEM_IDENTITY_MISMATCH in snap.problems
        # The connection keeps re-checking 38/40/42 only.
        n = len(reads(client))
        for _ in range(2):
            await clock.sleep(10)
            await st.async_tick()
        assert {a for a, _n in reads(client)[n:]} == {38, 40, 42}
        await clock.sleep(60)
        await st.async_tick()
        assert not st.snapshot.measurements_available

    run(main())


def test_identity_mismatch_blocks_writes():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        other = FakeStation("A3C_3.1", OTHER, clock)
        client.retarget(other)
        await clock.sleep(10)
        await st.async_tick()
        await clock.sleep(10)
        await st.async_tick()
        with pytest.raises(WriteRefused) as err:
            await st.async_write(91, 80, origin="user")
        assert err.value.key == C.REFUSE_IDENTITY_MISMATCH
        assert other.writes == [] and fake.writes == []
        # The socket is never closed as a recovery tactic.
        assert client.connections == 2 and client.state == "connected"

    run(main())


def test_identity_restored_clears():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        other = FakeStation("A3C_3.1", OTHER, clock)
        client.retarget(other)
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        for _ in range(2):
            await clock.sleep(10)
            await st.async_tick()
        assert st.identity == C.IDENTITY_MISMATCH
        client.retarget(fake)
        for _ in range(2):
            await clock.sleep(10)
            await st.async_tick()
        assert st.identity == C.IDENTITY_OK and C.EVENT_IDENTITY_RESTORED in events
        assert C.PROBLEM_IDENTITY_MISMATCH not in st.snapshot.problems
        assert st.polled_since_connect

    run(main())


def test_identity_read_retried_3x():
    async def main():
        fake, client, st, clock = make(power_loss_zero_reads=0)
        fake._zero_reads = 6  # two identity attempts of three single reads answer zeros
        await st.async_tick()
        assert [a for a, _n in reads(client)][:9] == [38, 40, 42] * 3
        assert st.identity == C.IDENTITY_OK and st.snapshot.time is not None

    run(main())


def test_partly_zero_serial_is_not_ready_not_another_station():
    """zeros right after a connect may straddle the three single
    reads (7 zero replies: [0,0,0], [0,0,0], [0,x,x]). A partly zero serial
    is "not ready", never another station - unless the same words come back
    on two tries in a row (a real serial word can be 0000)."""

    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        fake._zero_reads = 7
        await st.async_tick()
        assert st.identity == C.IDENTITY_UNVERIFIED and st.last_failure == C.FAIL_NOT_READY
        assert C.EVENT_IDENTITY_MISMATCH not in events
        await clock.sleep(10)
        await st.async_tick()
        assert st.identity == C.IDENTITY_OK
        # The same partly zero words twice: taken as they are (here another serial).
        fake2, client2, st2, clock2 = make()
        fake2.overrides[40] = 0
        await st2.async_tick()
        assert st2.identity == C.IDENTITY_MISMATCH and st2._serial_seen == "301200007890"

    run(main())


def test_all_zero_first_read_is_not_ready():
    async def main():
        fake, client, st, clock = make()
        fake._zero_reads = 9
        await st.async_tick()
        assert [a for a, _n in reads(client)] == [38, 40, 42] * 3
        assert st.last_failure == C.FAIL_NOT_READY
        assert st.identity == C.IDENTITY_UNVERIFIED  # zeros never mean "another device"
        assert st.snapshot.time is None
        # Serial fine, but every fast-group register 0 right after connect.
        fake2, client2, st2, clock2 = make()
        fake2.overrides.update({a + 2 * i: 0 for g in C.TIER_A for a, n in g.requests for i in range(n)})  # the words really read
        await st2.async_tick()
        assert st2.last_failure == C.FAIL_NOT_READY and st2.snapshot.time is None
        assert not st2.polled_since_connect

    run(main())


# --- link health ---------------------------------------------


def test_station_counts_timeouts_for_link_state():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        assert st.link_state == C.LINK_CONNECTED
        for _ in range(3):
            fake.on_next("silent", fc=3, addr=0)
        for i in range(3):
            await clock.sleep(10)
            await st.async_tick()
            if i == 0:
                assert client.consecutive_failures == 1  # every timeout closes the socket (evcc)
        assert st.link_state == C.LINK_RECONNECTING

    run(main())


def test_link_states_thresholds():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        fake.set_quirks(silences=[])
        fake.host_down = True
        fake.kill_sessions()
        states = []
        for _ in range(40):
            await clock.sleep(10)
            await st.async_tick()
            states.append(st.link_state)
        assert C.LINK_RECONNECTING in states and states[-1] == C.LINK_LOST
        assert states.index(C.LINK_LOST) > 0

    run(main())


def test_entities_keep_values_60s():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        good = clock()
        fake.host_down = True
        fake.kill_sessions()
        while clock() - good < 59:
            await clock.sleep(5)
            await st.async_tick()
            if clock() - good < 60:
                assert st.snapshot.measurements_available
                assert st.snapshot.setpoint_a == 16.0
        await clock.sleep(5)
        await st.async_tick()
        assert not st.snapshot.measurements_available
        assert st.snapshot.setpoint_a == 16.0  # last value kept for diagnostics

    run(main())


def test_lost_notification_after_180s_and_restore():
    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        await st.async_tick()
        fake.host_down = True
        fake.kill_sessions()
        await drive(st, clock, 150)
        assert C.EVENT_LINK_LOST not in events
        await drive(st, clock, 60)
        assert events.count(C.EVENT_LINK_LOST) == 1
        fake.host_down = False
        await drive(st, clock, 200)
        assert events.count(C.EVENT_LINK_RESTORED) == 1 and st.link_state == C.LINK_CONNECTED

    run(main())


def test_link_restored_only_after_lost():
    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        await st.async_tick()
        fake.on_next("silent", fc=3, addr=0)
        await drive(st, clock, 60)
        assert C.EVENT_LINK_RESTORED not in events and C.EVENT_LINK_LOST not in events

    run(main())


def test_station_poll_atomic_on_partial_failure():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        before = st.snapshot.registers
        fake.plug(Car())
        fake.setpoint = 100
        fake.on_next("silent", fc=3, addr=109)
        await clock.sleep(10)
        await st.async_tick()
        assert st.snapshot.registers == before  # nothing half-updated
        assert st.snapshot.cable == 0 and st.snapshot.setpoint_a == 16.0

    run(main())


def test_station_poll_stops_at_first_failure():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        client.sent.clear()
        fake.on_next("silent", fc=3, addr=0)
        await clock.sleep(10)
        await st.async_tick()
        assert reads(client) == [(0, 1)]

    run(main())


def test_failure_classes_station():
    async def main():
        fake, client, st, clock = make()
        fake.port_closed = True
        await st.async_tick()
        assert st.last_failure == C.FAIL_REFUSED
        fake2, client2, st2, clock2 = make()
        fake2.host_down = True
        await st2.async_tick()
        assert st2.last_failure == C.FAIL_CONNECT_TIMEOUT
        fake3, client3, st3, clock3 = make(min_gap_s=0.0)
        await st3.async_tick()
        fake3.on_next("silent", fc=3)
        await clock3.sleep(10)
        await st3.async_tick()
        assert st3.last_failure == C.FAIL_NO_ANSWER
        # A read dropped once is sent again on a fresh connection (evcc): no
        # failure; dropped twice, the poll fails as closed by the station.
        await clock3.sleep(10)
        await st3.async_tick()
        fake3.on_next("drop", fc=3)
        fake3.on_next("drop", fc=3)
        await clock3.sleep(10)
        await st3.async_tick()
        assert st3.last_failure == C.FAIL_CLOSED_BY_STATION

    run(main())


def test_link_hint_only_after_persistent_failure():
    async def main():
        fake, client, st, clock = make()
        fake.port_closed = True
        await st.async_tick()
        assert st.link_hint is None  # one refused connect proves nothing (measured)
        await drive(st, clock, 100)
        assert st.link_hint is None
        await drive(st, clock, 120)
        assert st.diagnostics()["link"]["failed_polls_in_row"] >= 3
        assert st.link_hint == C.FAIL_REFUSED
        fake.port_closed = False
        await drive(st, clock, 130)
        assert st.link_hint is None and st.link_state == C.LINK_CONNECTED

    run(main())


def test_n3_churn_rule_vs_evcc_reconnect():
    """Measured: after quick reconnects the box refused even with
    10 s gaps. The fake's churn rule (N connects per 60 s -> refused while
    attempts go on; here N = 1) is an unverified extrapolation of it. With the evcc
    transport there is no backoff: every poll tries to connect, so under
    that rule the box keeps refusing for as long as polling goes on, and
    serves again once connects pause for a minute. Recorded;
    the rule is off in the default fake profile."""

    async def main():
        fake, client, st, clock = make(churn_limit=1, churn_window_s=60.0)
        await st.async_tick()
        fake.kill_sessions()
        await drive(st, clock, 400)
        attempts = [t for t, _verdict in fake.connect_log]
        gaps = [b - a for a, b in zip(attempts[1:], attempts[2:])]
        assert gaps and max(gaps) <= 10.0 + 1e-6  # an attempt on every poll, no growing pause
        assert [v for _t, v in fake.connect_log[-20:]] == ["refuse"] * 20
        assert st.link_state == C.LINK_LOST
        await clock.sleep(61)  # no connect for a minute (e.g. polling stopped)
        await st.async_tick()
        assert fake.connect_log[-1][1] == "accept" and st.snapshot.time is not None

    run(main())


@pytest.mark.parametrize("realistic", [False, True])
def test_start_stall_12s_reconnects_after_stall(realistic):
    """Every missing reply in the stall closes the socket (evcc); after the
    stall a new connection with the identity first and one full poll."""

    async def main():
        fake, client, st, clock = make(realistic=realistic, start_stall=True, stall_s=12.0)
        fake.plug(Car())
        await drive(st, clock, 20)
        result = await st.async_write(95, 1, origin="test")
        assert result.acknowledged  # read-back fell into the stall
        await drive(st, clock, 40)
        assert client.connections >= 2 and client.consecutive_failures == 0
        assert st.snapshot.status == 4 and st.polled_since_connect

    run(main())


# --- reload, unreachable station, start-up ---------------


def test_reload_connects_at_once():
    """ (evcc): no reload gap any more -
    a new entry instance connects on its first poll, right after the old
    one closed."""

    async def main():
        fake, client, st, clock = make(min_gap_s=0.0)
        await st.async_tick()
        closed = await st.async_stop()
        assert closed == clock()
        client2 = FakeClient(fake)
        st2 = Station(client2, SERIAL, clock=clock, sleep=clock.sleep)
        n = len(fake.requests)
        assert st2.next_delay() == C.POLL_S
        await st2.async_tick()
        assert client2.connections == 1 and fake.requests[n][2:4] == (3, 38)
        assert st2.identity == C.IDENTITY_OK

    run(main())


def test_unreachable_station_tried_on_every_poll():
    """Audit no client backoff - every poll tries to connect (evcc: the
    next request reconnects)."""

    async def main():
        fake, client, st, clock = make()
        fake.port_closed = True
        await st.async_tick()  # immediately, no exception
        assert len(fake.connect_log) == 1
        await clock.sleep(5)
        await st.async_tick()
        assert len(fake.connect_log) == 2
        await clock.sleep(5)
        await st.async_tick()
        assert len(fake.connect_log) == 3
        assert st.link_state == C.LINK_RECONNECTING

    run(main())


def test_first_contact_backoff_like_evcc_then_raises():
    """Start-up like evcc's NewEm2Go: retries 2 s apart, x1.5, at most 10 s
    apart, given up when the next wait would pass 30 s; then the last error
    is raised (the HA layer turns it into ConfigEntryNotReady)."""
    from gnlt_charger.modbus.client import ModbusConnectionError

    async def main():
        fake, client, st, clock = make()
        fake.port_closed = True
        t0 = clock()
        with pytest.raises(ModbusConnectionError):
            await st.async_first_contact(jitter=lambda: 0.5)  # no randomization
        tries = [round(t - t0, 2) for t, _v in fake.connect_log]
        assert tries == [0.0, 2.0, 5.0, 9.5, 16.25, 26.25], tries
        assert clock() - t0 < C.STARTUP_BACKOFF_GIVE_UP_S

    run(main())


def test_first_contact_ends_on_any_reply():
    async def main():
        fake, client, st, clock = make()
        fake.port_closed = True
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                fake.port_closed = False  # the station answers from the third try on
            await clock.sleep(seconds)

        st._sleep = sleep
        await st.async_first_contact(jitter=lambda: 0.5)
        assert waits == [2.0, 3.0]
        assert st.identity == C.IDENTITY_OK and client.connections == 1
        assert [v for _t, v in fake.connect_log] == ["refuse", "refuse", "accept"]
        # The first poll needs no second identity read on that connection.
        client.sent.clear()
        await st.async_tick()
        assert (38, 1) not in reads(client) and st.polled_since_connect
        # Another serial is a reply too: the poll reports it as usual.
        fake2, client2, st2, clock2 = make()
        fake2.overrides.update({38: 0x1012, 40: 0x3456, 42: 0x7891})
        await st2.async_first_contact()
        assert st2.identity == C.IDENTITY_MISMATCH and len(fake2.connect_log) == 1

    run(main())


def test_unload_leaves_no_socket():
    async def main():
        model = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True))
        server = FakeTcpStation(model)
        await server.start()
        client = ModbusClient("127.0.0.1", server.port, delay=0.0, timeout=0.3)
        st = Station(client, SERIAL, timing=StationTiming(poll_s=0.2, min_poll_s=0.05))
        await st.async_start()
        await asyncio.sleep(0.5)
        assert server.open_connections == 1
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await st.async_stop()
        assert loop.time() - t0 < 1.0
        await asyncio.sleep(0.1)
        assert server.open_connections == 0 and client.state == STATE_CLOSED
        assert server.accepts == 1
        await server.stop()

    run(main())


# --- sockets over TCP ---------------------------------------------


async def _tcp_station(**timing):
    model = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True))
    server = FakeTcpStation(model)
    await server.start()
    client = ModbusClient("127.0.0.1", server.port, delay=0.0, timeout=0.5)
    st = Station(client, SERIAL, timing=StationTiming(**timing))
    return model, server, client, st


def test_client_single_socket_over_100_polls():
    async def main():
        model, server, client, st = await _tcp_station()
        for _ in range(100):
            await st.async_tick()
        assert server.accepts == 1 and server.max_open == 1
        assert st.link_state == C.LINK_CONNECTED
        await st.async_stop()
        await server.stop()

    run(main())


def test_refresh_reuses_socket():
    async def main():
        model, server, client, st = await _tcp_station()
        await st.async_tick()
        n = len(server.frames)
        assert await st.async_refresh() is True
        assert len(server.frames) > n and server.accepts == 1
        assert await st.async_refresh() is False  # at most once per 5 s
        await st.async_stop()
        await server.stop()

    run(main())


def test_diagnostics_opens_no_socket():
    async def main():
        model, server, client, st = await _tcp_station()
        diag = st.diagnostics()
        assert server.accepts == 0 and diag["serial"] == SERIAL
        await st.async_tick()
        n = len(server.frames)
        st.diagnostics()
        assert len(server.frames) == n and server.accepts == 1
        await st.async_stop()
        await server.stop()

    run(main())


def test_client_never_two_in_flight():
    async def main():
        model, server, client, st = await _tcp_station()
        server.detect_overlap = True
        await st.async_tick()
        await asyncio.gather(
            st.async_tick(),
            st.async_write(91, 80, origin="user"),
            st.async_refresh(),
            st.async_write(93, 1, origin="user"),
            st.async_tick(),
        )
        assert server.overlaps == 0 and server.accepts == 1
        await st.async_stop()
        await server.stop()

    run(main())


def test_station_skips_overlapping_tick(caplog):
    async def main():
        fake, client, st, clock = make()
        gate = asyncio.Event()
        real = client.read_holding_registers

        async def slow(address, count):
            await gate.wait()
            return await real(address, count)

        client.read_holding_registers = slow
        first = asyncio.ensure_future(st.async_tick())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert await st.async_tick() is False
        gate.set()
        assert await first is True
        assert st.diagnostics()["link"]["skipped_ticks"] == 1

    with caplog.at_level(logging.WARNING):
        run(main())
    assert "poll skipped" in caplog.text


def test_two_fakes_swapped_addresses():
    async def main():
        a = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True), min_gap_s=0.0)
        b = FakeStation("A3C_3.1", OTHER, SimClock(realtime=True), min_gap_s=0.0)
        sa, sb = FakeTcpStation(a), FakeTcpStation(b)
        await sa.start()
        await sb.start()
        kw = dict(delay=0.0, timeout=0.3)
        ca, cb = ModbusClient("127.0.0.1", sa.port, **kw), ModbusClient("127.0.0.1", sb.port, **kw)
        st_a, st_b = Station(ca, SERIAL), Station(cb, OTHER)
        await st_a.async_tick()
        await st_b.async_tick()
        assert st_a.identity == st_b.identity == C.IDENTITY_OK
        FakeTcpStation.swap_addresses(sa, sb)
        await asyncio.sleep(0.05)
        for _ in range(3):
            await st_a.async_tick()
            await st_b.async_tick()
            await asyncio.sleep(0.02)
        assert st_a.identity == st_b.identity == C.IDENTITY_MISMATCH
        with pytest.raises(WriteRefused):
            await st_a.async_write(91, 80, origin="user")
        assert a.writes == [] and b.writes == []
        FakeTcpStation.swap_addresses(sa, sb)
        await asyncio.sleep(0.05)
        for _ in range(3):
            await st_a.async_tick()
            await st_b.async_tick()
            await asyncio.sleep(0.02)
        assert st_a.identity == st_b.identity == C.IDENTITY_OK
        for st in (st_a, st_b):
            await st.async_stop()
        await sa.stop()
        await sb.stop()

    run(main())


# --- polling interval ----------------------------------------------


def test_poll_interval_10s():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        assert st.next_delay() == 10.0
        times = []
        real = client.read_holding_registers

        async def spy(address, count):
            if address == 0:
                times.append(clock())
            return await real(address, count)

        client.read_holding_registers = spy
        await drive(st, clock, 100)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert gaps and all(g == pytest.approx(10.0, abs=0.01) for g in gaps)

    run(main())


def test_poll_never_faster_than_2s():
    async def main():
        clock = SimClock()
        fake = FakeStation("A3C_2.7", SERIAL, clock)
        client = FakeClient(fake)
        delays = []

        async def sleep(s):
            delays.append(s)
            if len(delays) > 5:
                st._stopped = True
            await clock.sleep(s)

        st = Station(client, SERIAL, clock=clock, sleep=sleep, timing=StationTiming(poll_s=0.5))
        await st._run()
        assert min(delays) >= 2.0

    run(main())


def test_reg89_short_warning():
    async def main():
        # 89 is read once at setup; the flag follows that value.
        for value, short in ((20, True), (0, False), (3600, False)):
            fake, client, st, clock = make()
            fake.comm_timeout = value
            await st.async_read_comm_timeout()
            await st.async_tick()
            assert st.snapshot.flags["comm_timeout_short"] is short, value

    run(main())


def test_controller_hook_interval():
    async def main():
        clock = SimClock()
        fake = FakeStation("A3C_2.7", SERIAL, clock)
        client = FakeClient(fake)
        st = Station(client, SERIAL, clock=clock, sleep=None)
        calls = []

        def controller(snapshot):
            assert isinstance(snapshot, Snapshot)
            calls.append((clock(), len(client.sent)))

        with pytest.raises(ValueError):
            st.add_controller(controller, 10)
        st.add_controller(controller, 30)

        start = clock()

        async def sleep(s):
            if clock() - start > 125:
                st._stopped = True
            await clock.sleep(s)

        st._sleep = sleep
        await st._run()
        assert len(calls) == 4
        assert all(b[0] - a[0] >= 30 for a, b in zip(calls, calls[1:]))

    run(main())


# --- status, error, cable -----------------------------------


def test_status_map_all_codes():
    expected = [
        "unknown", "standby", "connected", "starting", "charging", "start_failed",
        "finished", "fault", "scheduled", "firmware_update", "booting",
    ]
    assert [status_key(i) for i in range(11)] == expected
    for odd in (11, 31, 255):
        assert status_key(odd) == C.STATUS_UNKNOWN

    async def main():
        fake, client, st, clock = make()
        fake.force_status(31)
        await st.async_tick()
        assert st.snapshot.status_key == "unknown" and st.snapshot.flags["raw_status"] == 31

    run(main())


def test_status_7_not_error():
    async def main():
        fake, client, st, clock = make()
        fake.force_status(7)
        await st.async_tick()
        assert st.snapshot.status_key == "fault" and st.link_state == C.LINK_CONNECTED
        assert st.last_failure is None

    run(main())


def test_status_5_7_problem_with_reg4_zero():
    async def main():
        fake, client, st, clock = make()
        for status in (5, 7):
            fake.force_status(status)
            fake.set_error(0)
            await drive(st, clock, 10)
            assert C.PROBLEM_STATION_FAULT_5_7 in st.snapshot.problems
            fake.set_error(19)
            await drive(st, clock, 10)
            assert C.PROBLEM_STATION_FAULT_5_7 not in st.snapshot.problems
        fake.force_status(None)
        fake.set_error(0)
        await drive(st, clock, 10)
        assert C.PROBLEM_STATION_FAULT_5_7 not in st.snapshot.problems

    run(main())


def test_status_cable_cross_check_logged(caplog):
    async def main():
        fake, client, st, clock = make()
        fake.force_status(4)
        await drive(st, clock, 15)
        assert st.snapshot.flags["status_cable_mismatch"]
        assert st.snapshot.cable == 0  # the cable entity still follows reg 2

    with caplog.at_level(logging.INFO):
        run(main())
    assert "reg 2 (cable) = 0 for 2 polls" in caplog.text


def test_error_map_all_codes():
    table = {
        0: "no_error", 11: "cp_fault", 12: "emergency_stop", 13: "under_voltage", 14: "over_voltage",
        15: "over_temperature", 16: "meter_fault", 17: "leakage_fault", 18: "output_short",
        19: "over_current", 21: "vehicle_fault", 22: "vehicle_timeout", 23: "relay_welded",
        24: "rcd_fault", 25: "pe_fault", 26: "startup_fault",
    }
    for code, key in table.items():
        assert error_key(code) == key
    for odd in (20, 27, 99):
        assert error_key(odd) == C.ERROR_UNKNOWN


def test_error_sticky_until_zero():
    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append((name, data.get("code"))))
        await st.async_tick()
        fake.set_error(19)
        await drive(st, clock, 60)
        assert st.snapshot.error_key == "over_current"
        assert events.count((C.EVENT_FAULT, 19)) == 1  # once per change, not per poll
        fake.set_error(13)
        await drive(st, clock, 10)
        assert events.count((C.EVENT_FAULT, 13)) == 1
        fake.set_error(0)
        await drive(st, clock, 10)
        assert (C.EVENT_FAULT_CLEARED, 13) in events and st.snapshot.error == 0

    run(main())


def test_error_23_is_relay_welded():
    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await drive(st, clock, 20)
        fake.set_error(23)
        await drive(st, clock, 10)
        assert st.snapshot.error_key == "relay_welded"
        assert C.PROBLEM_RELAY_WELDED in st.snapshot.problems
        fake.set_error(0)  # code gone: no sticky block until an unplug any more
        await drive(st, clock, 20)
        assert C.PROBLEM_RELAY_WELDED not in st.snapshot.problems

    run(main())


def test_cable_from_reg2_not_reg1():
    """Cable = reg 2, read on its own (evcc request shape) - not reg 1
    (never read) and not reg 4 (the error)."""

    async def main():
        fake, client, st, clock = make()
        fake.overrides[1] = 1  # undocumented: must never be read
        fake.set_error(1)  # reg 4 = 1 with no cable
        await st.async_tick()
        assert st.snapshot.cable == 0 and st.snapshot.error == 1
        assert 1 not in st.snapshot.registers
        fake.plug(Car())
        fake.set_error(0)
        await clock.sleep(10)
        await st.async_tick()
        assert st.snapshot.cable == 1 and st.snapshot.error == 0

    run(main())


def test_error_from_reg4_not_reg2():
    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await st.async_tick()
        assert st.snapshot.cable == 1 and st.snapshot.error == 0
        assert C.PROBLEM_STATION_FAULT_5_7 not in st.snapshot.problems

    run(main())


def test_delivering_needs_current():
    async def main():
        fake, client, st, clock = make()
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 20)
        assert st.snapshot.status == 4 and st.snapshot.delivering
        fake.overrides.update({6: 8})  # 0.8 A: above the 0.7 A off threshold
        await drive(st, clock, 5)
        assert st.snapshot.delivering
        fake.overrides.update({6: 6})
        await drive(st, clock, 5)
        assert not st.snapshot.delivering
        fake.overrides.update({6: 8})
        await drive(st, clock, 10)
        assert not st.snapshot.delivering  # back on only from 1.0 A

    run(main())


def test_status4_zero_amps_not_delivering():
    async def main():
        fake, client, st, clock = make()
        fake.mode = 0
        fake.plug(Car(ramp_s=27))
        await drive(st, clock, 15)
        assert st.snapshot.status == 4 and not st.snapshot.delivering
        assert st.snapshot.status_key == "charging"  # the status shows reg 0 as is
        await drive(st, clock, 30)
        assert st.snapshot.delivering

    run(main())


def test_reg95_not_used_as_state():
    async def main():
        fake, client, st, clock = make()
        fake.command = 1  # echo of a start written without a car
        await drive(st, clock, 70)
        snap = st.snapshot
        assert snap.last_command == 1 and snap.status_key == "standby"
        assert not snap.delivering and not snap.waiting_for_start
        assert not hasattr(snap, "enabled")

    run(main())


def test_state_changes_logged(caplog):
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        fake.plug(Car())
        await drive(st, clock, 10)

    with caplog.at_level(logging.INFO):
        run(main())
    assert re.search(r"status 1 -> 2 \(poll at", caplog.text)
    assert re.search(r"cable 0 -> 1 \(poll at", caplog.text)


# --- phases in use ----------------------------------------------------------------


def _set_currents(fake, l1, l2, l3):
    fake.overrides.update({0: 4, 2: 1, 6: int(l1 * 10), 8: int(l2 * 10), 10: int(l3 * 10)})


def test_phases_in_use_hysteresis():
    async def main():
        fake, client, st, clock = make()
        _set_currents(fake, 16, 16, 16)
        await drive(st, clock, 10)
        assert st.snapshot.phases_in_use == 3
        _set_currents(fake, 16, 0.8, 0.8)  # above the 0.7 A off threshold: still on
        await drive(st, clock, 15)
        assert st.snapshot.phases_in_use == 3
        _set_currents(fake, 16, 0.5, 0.5)
        await drive(st, clock, 5)
        assert st.snapshot.phases_in_use == 3  # one reading is not a change
        await drive(st, clock, 5)
        assert st.snapshot.phases_in_use == 1

    run(main())


def test_phases_not_from_voltage():
    async def main():
        fake, client, st, clock = make("unknown")  # idle L2/L3 show 230 V
        fake.mode = 0
        fake.plug(Car(phases=1))
        await drive(st, clock, 30)
        assert st.snapshot.voltages[1] == 230.0 and st.snapshot.phases_in_use == 1

    run(main())


def test_phases_suppressed_after_switch():
    async def main():
        fake, client, st, clock = make()
        _set_currents(fake, 16, 0, 0)
        await drive(st, clock, 10)
        assert st.snapshot.phases_in_use == 1
        st.suppress_phase_count()
        _set_currents(fake, 16, 16, 16)
        await drive(st, clock, 55)
        assert st.snapshot.phases_in_use == 1
        await drive(st, clock, 15)
        assert st.snapshot.phases_in_use == 3

    run(main())


def test_own_phase_switch_not_logged_as_unexpected_phase_change(caplog):
    """Step 2: after our own 200 write the new phase count was
    logged as "charging on 3 phase(s) instead of 1" once the 60 s settle
    ended. An own switch is an expected change; a change nobody ordered is
    still logged."""

    async def main():
        fake, client, st, clock = make()
        _set_currents(fake, 16, 0, 0)
        await drive(st, clock, 20)
        assert st.snapshot.phases_in_use == 1
        res = await st.async_write(C.REG_PHASES, 3, origin="user")
        assert res.acknowledged
        _set_currents(fake, 16, 16, 16)
        with caplog.at_level(logging.DEBUG):
            await drive(st, clock, 90)
        assert st.snapshot.phases_in_use == 3
        info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert not any("instead of" in m for m in info), info
        assert any("as switched" in r.getMessage() for r in caplog.records)
        caplog.clear()
        _set_currents(fake, 16, 0, 0)  # nobody ordered this one
        with caplog.at_level(logging.INFO):
            await drive(st, clock, 30)
        assert "charging on 1 phase(s) instead of 3" in caplog.text

    run(main())


def test_own_phase_switch_expectation_expires(caplog):
    """the expectation of an own 200 write must not live forever -
    a switch the station never made, and a change to that count much later
    that nobody ordered, is logged at INFO again."""

    async def main():
        fake, client, st, clock = make()
        _set_currents(fake, 16, 0, 0)
        await drive(st, clock, 20)
        res = await st.async_write(C.REG_PHASES, 3, origin="user")
        assert res.acknowledged
        await drive(st, clock, 600)  # the car stays on one phase
        assert st.snapshot.phases_in_use == 1
        _set_currents(fake, 16, 16, 16)  # much later, nobody ordered it
        with caplog.at_level(logging.INFO):
            await drive(st, clock, 30)
        assert "charging on 3 phase(s) instead of 1" in caplog.text

    run(main())


# --- plug events -------------------------------------------------------------------


def test_unplug_resets_flags_no_write():
    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await drive(st, clock, 30)
        st.session_flags.update({"empty_session": True})
        fake.unplug()
        await drive(st, clock, 10)
        assert st.session_flags == {}  # at the first poll
        assert fake.writes == []

    run(main())


# --- restart detection and restore -----------------------------------


def test_restart_signature_93_and_91():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 100
        events = []
        st.subscribe_events(lambda name, data: events.append((name, data)))
        await drive(st, clock, 20)
        fake.power_loss()
        await drive(st, clock, 120)
        names = [n for n, _d in events]
        assert names.count(C.EVENT_RESTARTED) == 1
        assert C.EVENT_FOREIGN_CHANGE not in names

    run(main())


def test_restart_on_status_10():
    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append((name, data)))
        await st.async_tick()
        fake.force_status(10)
        await drive(st, clock, 10)
        restarts = [d for n, d in events if n == C.EVENT_RESTARTED]
        assert restarts and restarts[0]["signals"] == ["status_10"]

    run(main())


def test_single_signal_not_restart():
    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append((name, data)))
        fake.setpoint = 100
        fake.plug(Car())
        await drive(st, clock, 20)
        fake.menu_set_mode(0)  # 93 from the station menu
        await drive(st, clock, 10)
        fake.mode = 1
        fake.status = 4
        fake._enter_charging(clock())
        await drive(st, clock, 200)
        fake.car_pause()  # 72 resets on a car pause
        await drive(st, clock, 10)
        fake.car_resume()
        await drive(st, clock, 10)
        fake.unplug()
        fake.foreign_set_current(60)  # 91 changed on unplug (some boxes)
        await drive(st, clock, 20)
        # A link drop with only one signal is not a restart either.
        fake.kill_sessions()
        fake.menu_set_mode(0)
        await drive(st, clock, 60)
        names = [n for n, _d in events]
        assert C.EVENT_RESTARTED not in names
        foreign = [d["register"] for n, d in events if n == C.EVENT_FOREIGN_CHANGE]
        assert 93 in foreign and 91 in foreign

    run(main())


def test_restart_detected_on_first_poll_after_ha_start():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 100
        await drive(st, clock, 20)
        saved = st.store_data()
        await st.async_stop()
        fake.power_loss()  # whole-house power cut while HA is down
        await clock.sleep(60)
        client2 = FakeClient(fake)
        st2 = Station(client2, SERIAL, restored=saved, clock=clock, sleep=clock.sleep)
        events = []
        st2.subscribe_events(lambda name, data: events.append((name, data)))
        await st2.async_tick()
        restarts = [d for n, d in events if n == C.EVENT_RESTARTED]
        assert len(restarts) == 1 and restarts[0]["first_poll"]

    run(main())


def test_snapshot_persisted_for_restart_detection():
    async def main():
        fake, client, st, clock = make()
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 200)
        snap = st.store_data()["snapshot"]
        assert {"91", "93", "200", "78", "status", "record_open"} <= set(snap) and "72" not in snap
        assert snap["status"] == 4 and snap["record_open"] and snap["78"] >= 120

    run(main())


def test_no_restart_on_own_write():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 100
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        await drive(st, clock, 20)
        await st.async_write(93, 0, origin="user")
        await st.async_write(91, 160, origin="user")
        fake.kill_sessions()
        await drive(st, clock, 60)
        assert C.EVENT_RESTARTED not in events and C.EVENT_FOREIGN_CHANGE not in events

    run(main())


def test_foreign_91_change_link_up_warns_and_flags_out_of_range():
    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append((name, data)))
        await drive(st, clock, 20)
        fake.foreign_set_current(100)
        await drive(st, clock, 10)
        assert [d["new"] for n, d in events if n == C.EVENT_FOREIGN_CHANGE] == [100]
        assert not st.snapshot.flags["setpoint_out_of_range"]
        fake.foreign_set_current(30)  # below 6 A: out of range
        await drive(st, clock, 10)
        assert st.snapshot.flags["setpoint_out_of_range"]
        assert fake.writes == []  # the restore of an out-of-range 91 is a command

    run(main())


def test_restore_91_after_link_drop_control_on():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 100
        await drive(st, clock, 20)
        assert st.desired["current"] == 100
        fake.power_loss()
        await drive(st, clock, 200)
        assert writes(fake) == [(91, 100)]
        assert st.diagnostics()["write_log"][0]["origin"] == "restore"
        assert fake.setpoint == 100

    run(main())


def test_restore_91_after_ha_start():
    async def main():
        restored = {"control_active": True, "desired": {"current": 100, "mode": 1, "phases": 3}}
        fake, client, st, clock = make(restored=restored)
        fake.setpoint = 160  # e.g. a P&C user whose box restarted: only 91 changed
        await st.async_tick()
        assert writes(fake) == [(91, 100)]

    run(main())


def test_restore_91_skips_stop_marker_after_ha_start():
    """C5: 91 = 0 of our stop is stored as 50; a stopped box in command mode keeps it."""

    async def main():
        restored = {"control_active": True, "desired": {"current": 100, "mode": 1, "phases": 3}}
        fake, client, st, clock = make(restored=restored)
        fake.mode = 1
        fake.plug(Car())
        fake.setpoint = 50
        await st.async_tick()
        assert writes(fake) == []
        assert not st.snapshot.flags["setpoint_out_of_range"]
        fake.mode = 0  # under Plug & Charge the box would charge at 91: restored
        fake.kill_sessions()
        await drive(st, clock, 200)
        assert writes(fake) == [(91, 100)]

    run(main())


def test_pnc_reset_repair_no_write():
    async def main():
        fake, client, st, clock = make()
        await drive(st, clock, 20)
        assert st.desired["mode"] == 1
        fake.power_loss()
        await drive(st, clock, 200)
        assert C.PROBLEM_PNC_RESET in st.snapshot.problems
        assert (93, 0) not in writes(fake) and (93, 1) not in writes(fake)

    run(main())


# --- desired values and capabilities ------------------------------------------


def test_seed_desired_once():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 100
        fake.phases = 1
        await st.async_tick()
        assert st.desired == {"current": 100, "mode": 1, "phases": 1}
        fake.foreign_set_current(120)
        fake.menu_set_mode(0)
        await drive(st, clock, 30)
        assert st.desired == {"current": 100, "mode": 1, "phases": 1}

    run(main())


def test_seed_invalid_91_to_min():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 10
        fake.min_tenths = 80
        await st.async_tick()
        assert st.desired["current"] == 80
        fake2, client2, st2, clock2 = make()
        fake2.setpoint = 10
        fake2.min_tenths = 0
        await st2.async_tick()
        assert st2.desired["current"] == 60

    run(main())


@pytest.mark.parametrize("marker", [0, 50])
def test_seed_desired_skips_stop_marker(marker):
    """C5: 91 = 0 / 50 is our stop's marker, never the user's current."""

    async def main():
        fake, client, st, clock = make()
        fake.setpoint = marker
        await st.async_tick()
        assert st.desired["current"] is None and st.desired["mode"] == 1
        fake.foreign_set_current(130)
        await drive(st, clock, 15)
        assert st.desired["current"] == 130  # the first real value seeds it

    run(main())


def test_reg200_read_once_exception_means_no_capability():
    """evcc initialize reads reg 200 once: an error means no phase switching
    capability. Here at the first contact / first poll of this instance; it
    is never read again while not capable, and writes are not refused."""

    async def main():
        fake, client, st, clock = make(phases_readable=False)
        await drive(st, clock, 900)
        assert st.phase_capability is False
        assert [a for a, _n in reads(client)].count(200) == 1  # once in 15 min
        assert st.link_state == C.LINK_CONNECTED
        st.check_write(91, 100, "user")  # not refused

    run(main())


def test_phase_capability_decided_once_per_start():
    """evcc decides the capability once at driver initialize: no re-probe,
    no "lost after 3 connects" - a new Station instance (integration start
    or reload) decides again."""

    async def main():
        fake, client, st, clock = make(phases_readable=False)
        await st.async_tick()
        assert st.phase_capability is False
        fake.set_quirks(phases_readable=True)
        fake.kill_sessions()
        await drive(st, clock, 1800)
        assert st.phase_capability is False  # not probed again
        await st.async_stop()  # e.g. a reload: the old instance closes first
        await clock.sleep(60)
        st2 = Station(FakeClient(fake), SERIAL, clock=clock, sleep=clock.sleep)
        await st2.async_first_contact()
        assert st2.phase_capability is True

    run(main())


def test_phase_capability_never_dropped_while_running():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        assert st.phase_capability is True
        fake.set_quirks(phases_readable=False)
        for _ in range(4):
            fake.kill_sessions()
            await drive(st, clock, 20)
        assert st.phase_capability is True  # decided once at start (evcc)

    run(main())


# --- write gate -----------------------------------------


@pytest.mark.parametrize(
    "condition,key",
    [
        ("not_connected", C.REFUSE_NOT_CONNECTED),
        ("identity_mismatch", C.REFUSE_IDENTITY_MISMATCH),
        ("status_9", C.REFUSE_STATION_BUSY),
        ("status_10", C.REFUSE_STATION_BUSY),
    ],
)
def test_write_gate_each_condition(condition, key):
    async def main():
        fake, client, st, clock = make()
        register, value, origin = 91, 80, "user"
        await st.async_tick()
        if condition == "not_connected":
            fake.host_down = True
            fake.kill_sessions()
            await drive(st, clock, 40)
        elif condition == "identity_mismatch":
            client.retarget(FakeStation("A3C_3.1", OTHER, clock))
            await drive(st, clock, 20)
        elif condition in ("status_9", "status_10"):
            fake.force_status(int(condition[-1:]) if condition == "status_9" else 10)
            await drive(st, clock, 10)
        elif condition == "state_changed":
            register, value = 200, 1
            fake.plug(Car())  # plugged after the snapshot the command was decided on
        with pytest.raises(WriteRefused) as err:
            await st.async_write(register, value, origin=origin)
        assert err.value.key == key
        assert fake.writes == []

    run(main())


def test_no_write_in_status_9_10():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        for status in (9, 10):
            fake.force_status(status)
            await drive(st, clock, 10)
            for register, value in ((91, 80), (95, 1), (95, 2), (93, 1), (200, 1)):
                with pytest.raises(WriteRefused):
                    await st.async_write(register, value, origin="user")
        assert fake.writes == []

    run(main())


def test_write_allow_list():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        for register, value in ((87, 60), (89, 60), (92, 0), (96, 0), (28, 0), (32, 160), (54, 1), (184, 1)):
            with pytest.raises(WriteRefused) as err:
                await st.async_write(register, value, origin="user")
            assert err.value.key == C.REFUSE_NOT_ALLOWED
        for register, value in ((93, 2), (95, 0), (95, 3), (200, 2), (200, 0)):
            with pytest.raises(WriteRefused) as err:
                await st.async_write(register, value, origin="user")
            assert err.value.key == C.REFUSE_NOT_ALLOWED
        assert fake.writes == []

    run(main())


def test_writes_quantity_1_only():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        await st.async_write(91, 80, origin="user")
        await st.async_write(93, 1, origin="user")
        await st.async_write(200, 1, origin="user")
        sent = [arg for _t, fc, _a, arg in client.sent if fc == 0x10]
        assert len(sent) == 3 and all(len(words) == 1 for words in sent)

    run(main())


def test_allow_list_central():
    code = STATION_PY.read_text(encoding="utf-8")
    assert code.count("write_register(") == 1  # one place in the station writes
    assert "write_registers(" not in code


@pytest.mark.parametrize("realistic", [False, True])
def test_every_write_one_request_and_logged(caplog, realistic):
    """one FC16 request, no read-back (evcc's driver); the write log
    keeps the acknowledgement."""

    async def main():
        fake, client, st, clock = make(realistic=realistic)
        await st.async_tick()
        n = len(client.sent)
        result = await st.async_write(91, 80, origin="user")
        assert result.outcome == C.WRITE_OK and result.acknowledged
        ops = [(fc, addr) for _t, fc, addr, _a in client.sent[n:]]
        assert ops == [(0x10, 91)], ops
        entry = st.diagnostics()["write_log"][-1]
        assert {"t", "register", "value", "origin", "result", "acknowledged"} <= set(entry)
        assert (entry["register"], entry["value"], entry["result"]) == (91, 80, "ok")
        assert st.snapshot.setpoint_a == 8.0  # shown at once
        fake.on_next("silent", fc=0x10)
        result = await st.async_write(91, 90, origin="user")
        assert not result.acknowledged and result.outcome == C.WRITE_UNKNOWN

    with caplog.at_level(logging.INFO):
        run(main())
    assert "wrote 91 = 80 (user): ok" in caplog.text


def test_write_exception_reply_is_rejected_link_healthy():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        fake.on_next("exception", fc=0x10, code=4)
        result = await st.async_write(91, 80, origin="user")
        assert result.outcome == C.WRITE_REJECTED
        assert st.link_state == C.LINK_CONNECTED and client.connections == 1

    run(main())


def test_write_drops_tcp_then_silence():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        fake.on_next("drop_after_write", fc=0x10, ack=False, silence_s=90.0)
        result = await st.async_write(91, 100, origin="user")
        assert result.outcome == C.WRITE_UNKNOWN
        assert fake.setpoint == 100  # it did execute
        with pytest.raises(WriteRefused) as err:
            await st.async_write(91, 80, origin="user")
        assert err.value.key == C.REFUSE_NOT_CONNECTED
        await drive(st, clock, 200)
        assert st.link_state == C.LINK_CONNECTED and st.polled_since_connect

    run(main())


def test_never_writes_0_or_below_min():
    async def main():
        fake, client, st, clock = make()
        fake.min_tenths = 70
        await st.async_tick()
        for value in [0, 1, 50, 59, 60, 69, 161, 200, 320]:
            with pytest.raises(WriteRefused) as err:
                await st.async_write(91, value, origin="user")
            assert err.value.key == C.REFUSE_CURRENT_RANGE
        with pytest.raises(WriteRefused):
            await st.async_write(91, 0, origin="restore")
        assert fake.writes == []
        await st.async_write(91, 70, origin="user")
        await st.async_write(91, 160, origin="user")
        assert all(v >= 70 for _r, v in writes(fake))

    run(main())


def test_reg36_zero_ignored():
    async def main():
        fake, client, st, clock = make()
        fake.cable_max = 0
        fake.max_tenths = 320
        await st.async_tick()
        assert st.current_range_tenths() == (60, 320)
        fake.cable_max = 130
        await drive(st, clock, 70)
        assert st.current_range_tenths() == (60, 320)  # read once per connection
        fake.kill_sessions()
        await drive(st, clock, 20)
        assert st.current_range_tenths() == (60, 130)

    run(main())


def test_current_max_48a_preset():
    async def main():
        fake, client, st, clock = make(preset_max_a=48)
        fake.max_tenths = 480
        fake.cable_max = 480
        await st.async_tick()
        assert st.current_range_tenths() == (60, 480)
        await st.async_write(91, 480, origin="user")  # never an assumed 32 A cap
        fake2, client2, st2, clock2 = make(preset_max_a=16)
        fake2.max_tenths = 320
        await st2.async_tick()
        assert st2.current_range_tenths()[1] == 160

    run(main())


def test_no_unprompted_writes_24h_sim():
    async def main():
        fake, client, st, clock = make()
        for hour in range(24):
            if hour % 6 == 1:
                fake.plug(Car())
            if hour % 6 == 2:
                fake.external_start()
            if hour % 6 == 3:
                fake.car_finish()
            if hour % 6 == 4:
                fake.unplug()
                fake.menu_set_mode(fake.mode)
            await drive(st, clock, 3600)
        assert fake.writes == []

    run(main())


# --- station-side limits ----------------------------------------------------------


def test_flat_current_below_91_flags_station_limit():
    async def main():
        fake, client, st, clock = make(menu_limit_a=8.0)
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 120)
        assert st.snapshot.setpoint_a == 16.0 and st.snapshot.currents[0] < 8.0
        assert C.PROBLEM_STATION_LIMIT_LOWER in st.snapshot.problems

    run(main())


def test_station_limit_no_rewrite():
    async def main():
        fake, client, st, clock = make(menu_limit_a=8.0)
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 600)
        assert fake.writes == []

    run(main())


def test_6a_cap_flags_dry_contact_hint():
    async def main():
        fake, client, st, clock = make(dry_contact="cap_6a")
        fake.mode = 0
        fake.plug(Car(min_a=6.0, factor=1.0))
        await drive(st, clock, 120)
        assert C.PROBLEM_CAPPED_6A in st.snapshot.problems
        assert C.PROBLEM_STATION_LIMIT_LOWER not in st.snapshot.problems

    run(main())


def test_diag_explains_effective_limit():
    fake, client, st, clock = make()
    assert "lower of its own menu current and reg 91" in st.diagnostics()["effective_limit_note"]


# --- persistence, learned behaviour, diagnostics ----------------------


def test_store_save_load_and_remove_hook():
    async def main():
        store = MemoryStore()
        clock = SimClock()
        fake = FakeStation("A3C_2.7", SERIAL, clock)
        st = Station(FakeClient(fake), SERIAL, store=store, clock=clock, sleep=clock.sleep)
        await st.async_tick()
        await st.async_stop()
        # One save at unload; no "control_active" key any more.
        assert store.saves == 1 and "control_active" not in store.data and store.delayed >= 1
        st2 = Station(FakeClient(fake), SERIAL, store=store, clock=clock, sleep=clock.sleep)
        await st2.async_load()
        assert st2.desired["current"] == 160
        await st2.async_remove_store()
        assert store.removed
        st3 = Station(FakeClient(fake), SERIAL, store=store, clock=clock, sleep=clock.sleep)
        await st3.async_load()
        assert st3.desired["current"] is None  # a re-added entry starts clean
        assert store_key(SERIAL) == "gnlt_charger.modbus_301234567890"

    run(main())


def test_diagnostics_content():
    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        await st.async_write(91, 80, origin="user")
        diag = st.diagnostics()
        for key in (
            "learned", "desired", "link", "registers_raw", "decoded",
            "problems", "flags", "write_log", "transactions", "restarts", "foreign_changes",
        ):
            assert key in diag
        assert diag["link"]["consecutive_failures"] == 0 and "seconds_until_retry" not in diag["link"]
        assert diag["serial_seen"] == SERIAL and diag["registers_raw"][91] == 80
        assert "host" not in str(diag.keys())

    run(main())


def test_client_trace_ring_200():
    async def main():
        fake, client, st, clock = make()
        await drive(st, clock, 600)
        ring = st.diagnostics()["transactions"]
        assert len(ring) == 200
        assert {"t", "op", "addr", "arg", "result", "ms"} <= set(ring[-1])

    run(main())


def test_write_log_ring():
    fake, client, st, clock = make()
    assert st._write_log.maxlen == 200


# --- review round 4 (stop path, pair, quiet window, lock per group) ----------------------------


def _charging(**quirks):
    """A station charging after our own start (status 4, current flowing)."""

    async def setup():
        fake, client, st, clock = make(**quirks)
        fake.plug(Car())
        await drive(st, clock, 20)
        await st.async_write(95, 1, origin="user")
        await drive(st, clock, 40)
        assert st.snapshot.delivering
        return fake, client, st, clock

    return setup()


@pytest.mark.parametrize("realistic", [False, True])
def test_stop_preempts_poll_after_in_flight_group(realistic):
    async def main():
        fake, client, st, clock = await _charging(realistic=realistic)
        await clock.sleep(10)
        n = len(client.sent)
        await asyncio.gather(st.async_tick(), st.async_write(95, 2, origin="user"))
        ops = [(fc, addr) for _t, fc, addr, _a in client.sent[n:]]
        stop_at = ops.index((0x10, 95))
        # Only the request in flight went before the stop's fresh read 0, 2, 4, 93.
        assert stop_at <= 5, ops

    run(main())


# --- mutation-driven additions ---------------------------------------------------------------


def test_stop_after_reconnect_without_complete_poll():
    async def main():
        fake, client, st, clock = await _charging()
        fake.kill_sessions()
        fake.overrides.update({a + 2 * i: 0 for g in C.TIER_A for a, n in g.requests for i in range(n)})
        await clock.sleep(10)
        await st.async_tick()  # identity checked on the new socket, the poll is "not ready"
        assert not st.polled_since_connect and client.connections == 2
        fake.overrides.clear()
        result = await st.async_write(95, 2, origin="user")
        assert result.acknowledged and fake.status == 2

    run(main())


def test_two_foreign_changes_link_up_not_restart():
    async def main():
        fake, client, st, clock = make()
        fake.setpoint = 100
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        await drive(st, clock, 20)
        fake.menu_set_mode(0)
        fake.foreign_set_current(160)  # the power-on signature, but the link never dropped
        await drive(st, clock, 10)
        assert C.EVENT_RESTARTED not in events and events.count(C.EVENT_FOREIGN_CHANGE) == 2

    run(main())


def test_link_hint_needs_3_attempts():
    async def main():
        clock = SimClock()
        fake = FakeStation("A3C_2.7", SERIAL, clock)
        client = FakeClient(fake)
        st = Station(client, SERIAL, clock=clock, sleep=clock.sleep, timing=StationTiming(hint_after_s=0.0))
        fake.port_closed = True
        hints = []
        for _ in range(8):
            await st.async_tick()
            hints.append((st.diagnostics()["link"]["failed_polls_in_row"], st.link_hint))
            await clock.sleep(10)
        assert all(h is None for n, h in hints if n < 3)
        assert any(h == C.FAIL_REFUSED for n, h in hints if n >= 3)

    run(main())


# --- step 2 review round 2 (reviewer probes P1-P10) ----------


def test_91_restore_compares_expected_clamped_floored():
    """ / the expected value is desired clamped to
    the range and floored on whole-amp stations."""

    async def main():
        restored = {
            "control_active": True,
            "desired": {"current": 65, "mode": 1, "phases": 3},
        }
        fake, client, st, clock = make(restored=restored)
        st.learned.set("decimal_current", "no")  # the probe of this start found whole amps
        fake.setpoint = 160
        await st.async_tick()
        assert writes(fake) == [(91, 60)]
        restored = {"control_active": True, "desired": {"current": 320, "mode": 1, "phases": 3}}
        fake, client, st, clock = make(restored=restored)
        fake.setpoint = 160  # 32 A wanted, 16 A is the maximum: nothing to restore
        await st.async_tick()
        assert fake.writes == []

    run(main())


@pytest.mark.parametrize("realistic", [False, True])
def test_stop_in_start_stall_sent(realistic):
    """Probe P4 with the evcc transport: the read-back of
    95 = 1 times out in the stall and closes the socket; the stop's identity
    read on the new connection times out too, and the stop goes out anyway
    (result inconclusive). A box that accepts a 0-s
    reconnect (min_gap_s=0) takes it inside the stall."""

    async def main():
        fake, client, st, clock = make(
            start_stall=True, stall_s=12.0, realistic=realistic, min_gap_s=0.0
        )
        fake.plug(Car())
        await drive(st, clock, 20)
        await st.async_write(95, 1, origin="user")
        result = await st.async_write(95, 2, origin="user")
        assert not result.acknowledged and result.outcome == C.WRITE_UNKNOWN
        await drive(st, clock, 40)
        assert (95, 2) in writes(fake) and fake.status == 2
        assert client.connections >= 2  # every missing reply closed the socket

    run(main())


def test_stop_in_start_stall_on_a3c27_reconnect_rule():
    """Recorded: with no read-back of 95 = 1 the stop
    goes out at once on the same connection, so the measured A3C_2.7 rule
    (a reconnect within 5 s of a close is accepted but silent, as measured) does
    not swallow it: received, outcome unknown in the stall."""

    async def main():
        fake, client, st, clock = make(start_stall=True, stall_s=12.0)
        assert fake.q.gap_mode == "silent" and fake.q.min_gap_s == 5.0
        fake.plug(Car())
        await drive(st, clock, 20)
        await st.async_write(95, 1, origin="user")
        result = await st.async_write(95, 2, origin="user")
        assert result.outcome == C.WRITE_UNKNOWN
        assert (95, 2) in writes(fake)

    run(main())


def test_a3c27_reconnect_rule_with_10s_polling_while_delivering():
    """Recorded (evcc transport, no own reconnect gap): with the
    10 s poll also while delivering the next poll after a timeout comes
    more than 5 s after the close - outside the measured A3C_2.7 window in
    which a new connection is accepted but silent - so the station comes
    back (the old 5 s rate did not)."""

    async def main():
        for gap, recovers in ((5.0, True), (0.0, True)):
            fake, client, st, clock = await _charging(min_gap_s=gap)
            fake.on_next("silent", fc=3, addr=0)
            await drive(st, clock, 120)
            accepted = [t for t, v in fake.connect_log if v == "accept"]
            assert len(accepted) >= (2 if recovers else 10), (gap, accepted)
            assert (st.link_state == C.LINK_CONNECTED) is recovers, (gap, st.link_state)

    run(main())


def test_restart_72_78_reset_is_one_signal():
    """Probe P5: a car pause during a link outage resets
    78 (72 is not read); that is one signal, not a restart."""

    async def main():
        fake, client, st, clock = make()
        events = []
        st.subscribe_events(lambda name, data: events.append((name, data)))
        fake.mode = 0  # P&C user: plug -> starts
        fake.plug(Car())
        await drive(st, clock, 600)
        assert st.store_data()["snapshot"]["record_open"]
        fake.kill_sessions()
        fake.host_down = True
        fake.car_pause()
        await drive(st, clock, 40)
        fake.car_resume()  # 78 restarts from 0
        fake.host_down = False
        await drive(st, clock, 60)
        assert st.snapshot.status == 4 and st.snapshot.duration_s is not None
        assert [d for n, d in events if n == C.EVENT_RESTARTED] == []
        # 78 plus a 93 change is still two signals: a restart.
        fake.kill_sessions()
        await drive(st, clock, 600)
        fake.kill_sessions()
        fake.host_down = True
        fake.menu_set_mode(1)
        fake.car_pause()
        await drive(st, clock, 40)
        fake.car_resume()
        fake.host_down = False
        await drive(st, clock, 60)
        restarts = [d for n, d in events if n == C.EVENT_RESTARTED]
        assert len(restarts) == 1 and sorted(restarts[0]["signals"]) == ["78", "93"]

    run(main())


def test_restart_baseline_uses_record_layer():
    """the persisted baseline follows the record
    (record layer), not only status 3/4."""

    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await drive(st, clock, 20)
        assert st.store_data()["snapshot"]["record_open"] is False
        st.record_open = True  # the record layer keeps a record open (car pause)
        await drive(st, clock, 10)
        assert st.snapshot.status == 2 and st.store_data()["snapshot"]["record_open"] is True

    run(main())


@pytest.mark.parametrize("realistic", [False, True])
def test_stop_not_delayed_by_identity_retries(realistic):
    """Probe P9: the poll's identity retries
    release the lock across their pause; a stop waits at most for the
    request in flight and re-reads the serial at once."""

    async def main():
        fake, client, st, clock = await _charging(realistic=realistic)
        fake.kill_sessions()
        client._drop()
        await clock.sleep(6)
        fake._zero_reads = 2  # zeros right after connect (E5-035)
        t0 = clock()
        tick = asyncio.ensure_future(st.async_tick())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        res = await st.async_write(95, 2, origin="user")
        await tick
        stop_t = [t for t, fc, addr, _a in client.sent if fc == 0x10 and addr == 95][-1]
        # no 0.7 s identity pause in front of the stop: the in-flight read +
        # the stop's own reads (serial x2 attempts, fresh x4) + its write
        bound = 0.5 if not realistic else 12 * 0.09 + 0.01
        assert stop_t - t0 <= bound, stop_t - t0
        times = [t for t, _fc, _a, _v in client.sent if t0 <= t <= stop_t]
        assert all(b - a < 0.7 for a, b in zip(times, times[1:])), times
        assert res.acknowledged and st.identity == C.IDENTITY_OK
        assert st.polled_since_connect  # the poll went on after the stop

    run(main())


def test_poll_loop_survives_unexpected_exception():
    """polling never stops."""

    async def main():
        fake, client, st, clock = make()
        calls = []
        real = st.async_tick

        async def tick():
            calls.append(clock())
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) >= 4:
                st._stopped = True
            return await real()

        st.async_tick = tick
        await asyncio.wait_for(st._run(), 5)
        assert len(calls) >= 4

    run(main())


def test_identity_mismatch_link_not_connected():
    """another station answering is not "connected"."""

    async def main():
        fake, client, st, clock = make()
        await drive(st, clock, 20)
        assert st.link_state == C.LINK_CONNECTED
        client.retarget(FakeStation("A3C_3.1", OTHER, clock))
        await drive(st, clock, 20)
        assert st.identity == C.IDENTITY_MISMATCH and st.link_state != C.LINK_CONNECTED
        assert st.snapshot.link != C.LINK_CONNECTED

    run(main())


def test_constants_match_spec():
    """Review MINOR 9."""
    assert C.LINK_HINT_AFTER_S >= 180


def test_store_save_debounced_with_real_ha_store(tmp_path):
    """Energy HA's Store moves the write time forward on every
    ``async_delay_save``; the station must not call it on every poll, or
    nothing is written while polling goes on."""
    ha_core = pytest.importorskip("homeassistant.core")
    storage = pytest.importorskip("homeassistant.helpers.storage")

    async def main():
        hass = ha_core.HomeAssistant(str(tmp_path))
        await hass.async_start()
        try:
            real = storage.Store(hass, 1, "gnlt_modbus_probe")
            path = tmp_path / ".storage" / "gnlt_modbus_probe"
            scale = 200.0  # 60 s -> 0.3 s; the station's clock runs 200x as well

            class ScaledStore:
                async def async_load(self):
                    return await real.async_load()

                async def async_save(self, data):
                    await real.async_save(data)

                def async_delay_save(self, func, delay):
                    real.async_delay_save(func, delay / scale)

            clock = SimClock(realtime=True)
            fake = FakeStation("A3C_2.7", SERIAL, clock)
            st = Station(
                FakeClient(fake), SERIAL, store=ScaledStore(), clock=lambda: clock() * scale, sleep=clock.sleep,
            )
            await st.async_load()
            written_at = None
            t0 = clock()
            while clock() - t0 < 2.0:  # polls every "5 s" (0.05 s) for 40 "minutes"
                assert await st.async_tick()
                await asyncio.sleep(0.05)
                if written_at is None and path.exists():
                    written_at = clock() - t0
            assert written_at is not None and written_at < 1.0, written_at
        finally:
            await hass.async_stop(force=True)

    run(main())


def test_new_connection_mid_poll_drops_the_poll():
    """a socket reopened in the middle of a poll (here to
    another station after a DHCP swap) has no identity: nothing of that
    poll is committed, and the next poll checks 38-40 first."""

    async def main():
        fake, client, st, clock = make()
        await drive(st, clock, 20)
        good = st.snapshot.time
        other = FakeStation("A3C_3.1", OTHER, clock)
        other.setpoint = 99
        real = client.read_holding_registers
        swapped = []

        async def swap(address, count):
            if address == C.REG_CURRENT_L1 and not swapped:
                swapped.append(True)
                client.retarget(other)
                client._drop()
                await clock.sleep(6)  # past the reconnect gap: this read reconnects
            return await real(address, count)

        client.read_holding_registers = swap
        await clock.sleep(10)
        await st.async_tick()
        assert client.connections == 2
        assert st.snapshot.time == good and st.snapshot.setpoint_a == 16.0
        await st.async_tick()
        assert st.identity == C.IDENTITY_MISMATCH and st.snapshot.time == good

    run(main())


# --- review round 2 of step 2 (STATION) -------------


class _HaDelayStore(MemoryStore):
    """HA semantics: every ``async_delay_save`` moves the write time forward."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.func, self.due, self.times = clock, None, None, []

    def async_delay_save(self, func, delay):
        self.func, self.due = func, self.clock() + delay

    async def fire_due(self):
        if self.due is not None and self.clock() >= self.due:
            self.due = None
            await self.async_save(self.func())
            self.times.append(self.clock())


def test_store_saves_at_most_60s_apart_and_at_once_on_control():
    """S5: debounced saves land <= 60 s apart while charging; a
    Control change is saved at once."""

    async def main():
        clock = SimClock()
        fake = FakeStation("A3C_2.7", SERIAL, clock)
        store = _HaDelayStore(clock)
        st = Station(FakeClient(fake), SERIAL, store=store, clock=clock, sleep=clock.sleep)
        await st.async_load()
        fake.plug(Car())
        fake.mode = 0
        fake.external_start()
        end = clock() + 1800
        while clock() < end:
            started = clock()
            await st.async_tick()
            await store.fire_due()
            elapsed = clock() - started
            await clock.sleep(max(st.next_delay() - elapsed, st.timing.min_poll_s - elapsed, 0.0))
        gaps = [b - a for a, b in zip(store.times, store.times[1:])]
        assert len(store.times) >= 25 and max(gaps) <= 60.0, gaps
        saves = store.saves
        st._save_soon()  # an immediate save (a state change of our own)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert store.saves == saves + 1

    run(main())


def test_91_restore_retried_after_status_10():
    """Pre-existing: a first poll after a power loss that sees
    status 10 must not cancel the 91 restore for good."""

    async def main():
        fake, client, st, clock = make(power_loss_status10_s=15.0)
        fake.plug(Car())
        await drive(st, clock, 30)
        await st.async_write(91, 100, origin="user")
        st.desired["current"] = 100
        await drive(st, clock, 20)
        fake.power_loss()
        await drive(st, clock, 300)
        assert fake.setpoint == 100

    run(main())


def test_status_10_restart_event_once_per_boot():
    async def main():
        fake, client, st, clock = make(power_loss_status10_s=30.0)
        events = []
        st.subscribe_events(lambda name, data: events.append(name))
        fake.plug(Car())
        await drive(st, clock, 30)
        fake.power_loss()
        await drive(st, clock, 120)
        assert events.count(C.EVENT_RESTARTED) == 1

    run(main())


def test_fake_n3_connect_series_not_easier_than_measured():
    """The reconnect procedure (open/read/close with the client, raw connect
    after the gap, read, close; 6 s between rounds) on the fake fails at
    least as often as measured on a station (0 s TEE, 2 s TRO, 5 s OTR, 10 s OOR)."""
    measured_ok = {0: 0, 2: 1, 5: 1, 10: 2}
    clock = SimClock()
    fake = FakeStation("A3C_2.7", SERIAL, clock, n3_connects=True)
    got = {}
    for gap in (0, 2, 5, 10):
        clock.advance(120)  # the series were minutes apart
        outcomes = ""
        for _round in range(3):
            verdict, s = fake.connect()
            if s is not None:
                fake.request(s.sid, 255, bytes([3, 0, 0, 0, 1]))
                fake.close(s.sid)
            clock.advance(gap)
            verdict, s = fake.connect()
            if verdict != "accept":
                outcomes += "R" if verdict == "refuse" else "T"
            else:
                outcomes += "O" if fake.request(s.sid, 255, bytes([3, 0, 0, 0, 1]))[0] == "reply" else "E"
                fake.close(s.sid)
            clock.advance(6)
        got[gap] = outcomes
    assert all(got[g].count("O") <= measured_ok[g] for g in got), got
    assert all(any(ch in "TRE" for ch in got[g]) for g in got), got


@pytest.mark.parametrize("n3", [False, True])
def test_stop_after_drop_with_n3_reconnects(n3):
    """The stop after a link drop also works when reconnects behave as
    measured (a few backoff rounds; never a second socket)."""

    async def main():
        fake, client, st, clock = await _charging(n3_connects=n3)
        fake.kill_sessions()
        result = None
        for _ in range(60):
            try:
                result = await st.async_write(95, 2, origin="user")
                break
            except WriteRefused:
                await drive(st, clock, 10)
        assert result is not None and result.acknowledged and fake.status == 2
        assert fake.open_sessions() <= 1

    run(main())


def test_stop_after_a_close_reconnects_at_once():
    """evcc: after a close the next request reconnects at once - a stop too
    (fake min_gap_s=0: a box that accepts the 0-s reconnect)."""

    async def main():
        fake, client, st, clock = await _charging(min_gap_s=0.0)
        fake.on_next("silent", fc=3, addr=0)
        await clock.sleep(10)
        await st.async_tick()  # the poll's timeout closes the socket
        closed = client.last_close_monotonic
        n = len(fake.connect_log)
        result = await st.async_write(95, 2, origin="user")
        assert result.acknowledged and fake.status == 2
        assert fake.connect_log[n][0] - closed < 1.0  # no gap of our own

    run(main())


@pytest.mark.parametrize("executes", [True, False])
def test_stop_in_start_stall_both_write_models(executes):
    """F2: a write inside the stall may be executed (reply lost) or lost; the
    stop goes out either way and its outcome is unknown. (min_gap_s=0: a box
    that accepts the 0-s reconnect of the evcc transport.)"""

    async def main():
        fake, client, st, clock = make(
            start_stall=True, stall_s=12.0, stall_executes_writes=executes, min_gap_s=0.0
        )
        fake.plug(Car())
        await drive(st, clock, 20)
        await st.async_write(95, 1, origin="user")
        result = await st.async_write(95, 2, origin="user")
        assert result.outcome == C.WRITE_UNKNOWN
        await drive(st, clock, 40)
        assert ((95, 2) in writes(fake)) is executes
        assert fake.status == (2 if executes else 4)

    run(main())


test_91_restored_after_any_link_drop = test_restore_91_after_link_drop_control_on
test_power_loss_after_ha_start_restores_91 = test_restore_91_after_ha_start
test_93_zero_with_stored_1_raises_repair_no_write = test_pnc_reset_repair_no_write


# --- stride-2 block reads: no longer used, single reads like evcc ---------------


def test_fake_answers_block_reads_with_stride_2_as_measured():
    """Measured on a station: 32x3 -> [160, 60, 160]; 109x3 -> U1/U2/U3;
    38x3 -> serial 3012 3456 7890; odd 33 read singly -> other bytes."""
    fake = FakeStation("A3C_2.7", SERIAL, SimClock())
    assert fake._read(32, 3) == [160, 60, 160]
    assert fake._read(38, 3) == [0x3012, 0x3456, 0x7890]
    assert fake._read(109, 3) == [2350, 20, 10]
    assert fake._read(33, 1) != [60] and fake._read(110, 1) != [20]


def test_current_range_6_to_16_from_stride_2_reads():
    """Build 376c806 read 32x3 as 32/33/34 -> min = value of 36 (160): the
    current number had min = max = 16 A. With stride 2: min 6 A, max 16 A."""

    async def main():
        fake, client, st, clock = make()
        await st.async_tick()
        snap = st.snapshot
        assert (snap.max_current_a, snap.min_current_a, snap.cable_max_a) == (16.0, 6.0, 16.0)
        assert st.current_range_tenths() == (60, 160)
        assert snap.voltages == (235.0, 2.0, 1.0)  # A3C_2.7 idle L2/L3, not neighbour bytes
        assert st.identity == C.IDENTITY_OK  # serial from 38/40/42
        assert snap.mode == 1 and snap.setpoint_a == 16.0 and snap.error == 0
        await st.async_write(91, 80, origin="user")
        assert fake.setpoint == 80

    run(main())


def test_u32_power_decoded_from_stride_2_pair():
    async def main():
        fake, client, st, clock = make()
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 40)
        assert st.snapshot.power_w == fake.power_w() > 0
        assert st.snapshot.duration_s == fake._duration()

    run(main())


# --- final review (MINOR-4) -------------------------------------------------------------


def test_diagnostics_u32_as_word_pairs():
    async def main():
        fake, client, st, clock = make()
        fake.mode = 0
        fake.plug(Car())
        await drive(st, clock, 80)
        raw = st.diagnostics()["registers_raw"]
        assert raw[12] == [fake.register(12), fake.register(14)]
        assert raw[28] == [fake.register(28), fake.register(30)] and raw[78][1] == fake.register(80)
        assert not {13, 17, 21, 25, 29, 79} & set(raw)

    run(main())


def test_no_master_switch_stored_control_active_ignored():
    """no "Control active" switch - a write goes out
    without one, and an old Store with "control_active": false still loads
    and is ignored."""

    async def main():
        fake, client, st, clock = make(restored={"control_active": False, "desired": {"current": 100}})
        assert not hasattr(st, "set_control_active") and not hasattr(st, "control_active")
        await st.async_tick()
        res = await st.async_write(91, 80, origin="user")
        assert res.outcome == C.WRITE_OK and (91, 80) in writes(fake)
        assert "control_active" not in st.store_data() and "control_active" not in st.diagnostics()

    run(main())


def test_no_write_breaker_stored_breaker_ignored():
    """no write breaker; a Store with a tripped breaker
    loads, and automatic writes still go out."""

    async def main():
        fake, client, st, clock = make(restored={"breaker": {"tripped": True, "reason": "oscillation"}})
        await st.async_tick()
        for value in (80, 90, 80, 90, 80):
            res = await st.async_write(91, value, origin="schedule")
            assert res.outcome == C.WRITE_OK
        assert "breaker" not in st.store_data() and "breaker" not in st.diagnostics()
        assert not hasattr(C, "REFUSE_WRITE_BREAKER")

    run(main())


# --- polling like evcc / OCPP ----------------------


def test_poll_95_every_poll_limits_once_per_connection_no_72_87_89():
    """95 every poll (evcc Enabled); 32/34/36 once per connection
    (like evcc Diagnose, no 60 s read); 72 never; 87/89 are not
    polled (89 once at setup, 87 by the heartbeat)."""

    async def main():
        fake, client, st, clock = make()
        await drive(st, clock, 200)
        reads = [a for _t, fc, a, _n in client.sent if fc == 3]
        assert reads.count(95) == reads.count(0) >= 15
        assert (reads.count(32), reads.count(34), reads.count(36)) == (1, 1, 1)
        assert not {72, 87, 89} & set(reads)
        assert st.current_range_tenths() == (60, 160)  # kept between their reads
        fake.kill_sessions()
        await drive(st, clock, 30)
        reads = [a for _t, fc, a, _n in client.sent if fc == 3]
        assert reads.count(32) == 2  # again on the new connection
        assert st.snapshot.last_command is not None

    run(main())


def test_poll_every_10s_also_while_charging_period_from_option():
    """10 s always (no 5 s rate while current flows); the period is an
    option like OCPP meter_interval."""

    async def main():
        fake, client, st, clock = make()
        fake.plug(Car())
        await drive(st, clock, 20)
        await st.async_write(95, 1, origin="user")
        await drive(st, clock, 60)
        assert st.snapshot.delivering and st.next_delay() == 10.0
        st.timing.poll_s = 30.0
        assert st.next_delay() == 30.0

    run(main())


def test_phase_power_registers_in_10_w():
    """Phase power registers 16/20/24 in 10 W."""

    async def main():
        fake, client, st, clock = make()
        # U32 = words (16, 18), (20, 22), (24, 26) on the box (stride 2)
        fake.overrides.update({16: 0, 18: 204, 20: 0, 22: 0, 24: 0, 26: 7})
        await drive(st, clock, 20)
        assert st.snapshot.phase_power_w == (2040.0, 0.0, 70.0)
        assert st.diagnostics()["decoded"]["phase_power_w"] == (2040.0, 0.0, 70.0)
        assert not hasattr(st.snapshot, "session_energy_kwh")

    run(main())
