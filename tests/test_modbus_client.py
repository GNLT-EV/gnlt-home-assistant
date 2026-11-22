"""ModBus TCP client against an in-process fake station (asyncio server).

The fake speaks MBAP like the wall EVE: FC03 and FC16, unit 255, replies
echoing the transaction id. A script of actions per request lets a test make
the station answer an exception, stay silent, answer late, drop the
connection, or send stale frames first.
"""

import asyncio
import errno
import socket
import struct
import time

import pytest

from gnlt_charger.modbus.client import (
    STATE_CLOSED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_RECONNECTING,
    ModbusClient,
    ModbusClosedError,
    ModbusConnectionError,
    ModbusExceptionError,
    ModbusProtocolError,
    ModbusTimeoutError,
    from_u32,
    to_u32,
    u16,
)


class FakeStation:
    def __init__(self) -> None:
        self.regs: dict[int, int] = {}
        self.frames: list[tuple[float, bytes]] = []  # (loop time received, raw request)
        self.replied: dict[int, float] = {}  # transaction id -> loop time the reply went out
        self.script: list = []  # one action per request, in order; default "ok"
        self.connections = 0
        self.accepted_at: list[float] = []
        self.refuse = False  # accept, then close at once (station not serving)
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self.kick()
        self.server.close()
        await self.server.wait_closed()

    def kick(self) -> None:
        """Close every open connection from the station side."""
        for writer in self._writers:
            writer.close()

    def inject(self, data: bytes) -> None:
        """Send unsolicited bytes on every open connection."""
        for writer in self._writers:
            if not writer.is_closing():
                writer.write(data)

    def request(self, i: int) -> tuple[int, int, int]:
        """(transaction id, function, address) of request ``i``."""
        raw = self.frames[i][1]
        tid, _p, _l, _u, fc, addr = struct.unpack(">HHHBBH", raw[:10])
        return tid, fc, addr

    def _reply(self, tid: int, unit: int, body: bytes) -> bytes:
        fc = body[0]
        if fc == 0x03:
            addr, n = struct.unpack(">HH", body[1:5])
            words = [self.regs.get(addr + i, 0) for i in range(n)]
            pdu = bytes([fc, 2 * n]) + struct.pack(f">{n}H", *words)
        elif fc == 0x10:
            addr, n, _bc = struct.unpack(">HHB", body[1:6])
            for i, v in enumerate(struct.unpack(f">{n}H", body[6 : 6 + 2 * n])):
                self.regs[addr + i] = v
            pdu = body[:5]
        else:
            pdu = bytes([fc | 0x80, 1])
        return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu

    def _send(self, writer: asyncio.StreamWriter, tid: int, data: bytes) -> None:
        writer.write(data)
        self.replied[tid] = asyncio.get_running_loop().time()

    async def _later(self, delay: float, writer, tid: int, data: bytes) -> None:
        await asyncio.sleep(delay)
        if not writer.is_closing():
            self._send(writer, tid, data)

    async def _handle(self, reader, writer) -> None:
        loop = asyncio.get_running_loop()
        self.connections += 1
        self.accepted_at.append(loop.time())
        self._writers.append(writer)
        if self.refuse:
            writer.close()
            return
        try:
            while True:
                head = await reader.readexactly(7)
                tid, _proto, length, unit = struct.unpack(">HHHB", head)
                body = await reader.readexactly(length - 1)
                self.frames.append((loop.time(), head + body))
                action = self.script.pop(0) if self.script else "ok"
                name = action if isinstance(action, str) else action[0]
                if name == "ok":
                    self._send(writer, tid, self._reply(tid, unit, body))
                elif name == "exception":
                    pdu = bytes([body[0] | 0x80, action[1]])
                    self._send(writer, tid, struct.pack(">HHHB", tid, 0, 3, unit) + pdu)
                elif name == "silent":
                    pass
                elif name == "late":  # answer after a delay, keep reading meanwhile
                    data = self._reply(tid, unit, body)
                    task = loop.create_task(self._later(action[1], writer, tid, data))
                    self._tasks.add(task)
                elif name == "stale":  # frames with foreign ids before the real reply
                    for i in range(action[1]):
                        bogus = (tid + 100 + i) & 0xFFFF
                        writer.write(self._reply(bogus, unit, bytes([0x03, 0, 0, 0, 1])))
                    self._send(writer, tid, self._reply(tid, unit, body))
                elif name == "partial":  # header only, then nothing
                    writer.write(self._reply(tid, unit, body)[:7])
                elif name == "split":  # header now, the rest after a delay
                    data = self._reply(tid, unit, body)
                    writer.write(data[:7])
                    task = loop.create_task(self._later(action[1], writer, tid, data[7:]))
                    self._tasks.add(task)
                elif name == "late_split":  # header after d1, the rest after d2
                    data = self._reply(tid, unit, body)
                    for delay, part in ((action[1], data[:7]), (action[2], data[7:])):
                        task = loop.create_task(self._later(delay, writer, tid, part))
                        self._tasks.add(task)
                elif name == "raw":  # this PDU under the right transaction id
                    pdu = action[1]
                    self._send(writer, tid, struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
                elif name == "garbage":  # these bytes instead of a reply
                    writer.write(action[1])
                elif name == "drop":  # close without answering
                    writer.close()
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def run(body, **client_kw):
    """Run ``body(station, client)`` with a fresh fake station and client."""

    async def main():
        st = FakeStation()
        await st.start()
        kw = {"delay": 0.0, "timeout": 0.5} | client_kw
        client = ModbusClient("127.0.0.1", st.port, **kw)
        try:
            await body(st, client)
        finally:
            await client.close()
            await st.stop()

    asyncio.run(main())


# --- request frames, byte for byte ---------------------------------------------


def test_read_frame_is_fc03_unit_255():
    async def body(st, c):
        assert await c.read_holding_registers(0, 3) == [0, 0, 0]
        assert st.frames[0][1] == bytes.fromhex("0001 0000 0006 ff 03 0000 0003")

    run(body)


def test_single_write_is_fc16_quantity_one():
    async def body(st, c):
        await c.read_holding_registers(0, 1)
        await c.write_register(95, 2)
        # The manufacturer's stop frame is 0020 0000 0009 ff 10 005f 0001 02 0002:
        # identical except for the transaction id.
        assert st.frames[1][1] == bytes.fromhex("0002 0000 0009 ff 10 005f 0001 02 0002")
        assert st.regs[95] == 2

    run(body)


def test_multiple_write_is_fc16():
    async def body(st, c):
        await c.write_registers(91, [80, 1])
        assert st.frames[0][1] == bytes.fromhex("0001 0000 000b ff 10 005b 0002 04 0050 0001")
        assert st.regs[91] == 80 and st.regs[92] == 1

    run(body)


def test_fc06_is_never_sent():
    async def body(st, c):
        await c.write_register(91, 80)
        await c.write_registers(200, [3])
        assert {raw[7] for _t, raw in st.frames} == {0x10}

    run(body)


def test_bad_values_are_refused_before_sending():
    async def body(st, c):
        for bad in (-1, 65536, 1.5, True):
            with pytest.raises(ValueError):
                await c.write_register(91, bad)
        with pytest.raises(ValueError):
            await c.read_holding_registers(0, 4)  # station answers 0xFF to > 3
        with pytest.raises(ValueError):
            await c.read_holding_registers(0, 0)
        assert st.frames == []

    run(body)


# --- reply parsing ----------------------------------------------------------------


def test_reply_u16_and_u32_high_word_first():
    async def body(st, c):
        st.regs.update({0: 4, 2: 1, 12: 0x0000, 13: 0x2B20, 28: 0, 29: 486, 91: 160})
        assert await c.read_holding_registers(0, 3) == [4, 0, 1]
        power = await c.read_holding_registers(12, 2)
        assert to_u32(power) == 11040  # W, documented example
        assert to_u32(await c.read_holding_registers(28, 2)) == 486  # 48.6 kWh, measured
        assert await c.read_holding_registers(91, 1) == [160]

    run(body)


def test_u16_u32_helpers():
    assert to_u32([1, 2]) == 65538
    assert to_u32([9, 0xFFFF, 0x0001], 1) == 0xFFFF0001
    assert from_u32(65538) == [1, 2]
    assert from_u32(0xFFFFFFFF) == [0xFFFF, 0xFFFF]
    assert u16(0) == 0 and u16(65535) == 65535
    for bad in (-1, 1 << 32):
        with pytest.raises(ValueError):
            from_u32(bad)
    with pytest.raises(ValueError):
        u16(65536)


def test_no_block_read_helper_one_request_per_call():
    """evcc request shape: the client sends exactly the request it
    is asked for; there is no stride-2 block helper any more."""

    async def body(st, c):
        assert not hasattr(c, "read_registers")
        st.regs.update({12: 0, 13: 0x2B20})
        assert to_u32(await c.read_holding_registers(12, 2)) == 11040
        assert await c.read_holding_registers(38, 1) == [0]
        quantities = [struct.unpack(">HH", raw[8:12]) for _t, raw in st.frames]
        assert quantities == [(12, 2), (38, 1)]

    run(body)


# --- station answers with an exception ----------------------------------------


def test_exception_reply_is_typed_and_closes_the_socket():
    """Exactly evcc: Connection.exec closes the
    connection on any error, a ModBus exception reply included; it is still
    a reply (no failure counted) and the next request reconnects at once."""

    async def body(st, c):
        st.script = [("exception", 2), ("exception", 0xFF)]
        with pytest.raises(ModbusExceptionError) as err:
            await c.read_holding_registers(500, 1)
        assert (err.value.function, err.value.code) == (0x03, 2)
        assert c.state == STATE_DISCONNECTED and c.consecutive_failures == 0
        with pytest.raises(ModbusExceptionError) as err:
            await c.write_register(91, 80)
        assert (err.value.function, err.value.code) == (0x10, 0xFF)
        assert await c.read_holding_registers(0, 1) == [0]
        assert st.connections == 3 and c.connections == 3 and c.state == STATE_CONNECTED

    run(body)


# --- timeouts and stale replies -------------------------------------------------


def test_timeout_closes_the_socket_and_the_next_request_reconnects_at_once():
    """Like evcc: a missing reply closes
    the connection; the next request opens a new one without any pause of
    our own (only the 60 ms delay before every request)."""
    clock = FakeClock()

    async def body(st, c):
        st.regs[0] = 3
        st.script = ["silent"]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(ModbusTimeoutError) as err:
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "no_answer"
        assert 0.18 <= loop.time() - t0 < 1.0
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 1
        assert c.last_close_monotonic == clock.t
        t1 = loop.time()
        assert await c.read_holding_registers(0, 1) == [3]  # same clock time: no pause of our own
        assert loop.time() - t1 < 0.15
        assert st.connections == 2 and c.connections == 2
        assert c.state == STATE_CONNECTED and c.consecutive_failures == 0

    run(body, timeout=0.2, clock=clock)


def test_reply_cut_off_mid_frame_closes_the_socket():
    async def body(st, c):
        st.script = ["partial"]
        with pytest.raises(ModbusTimeoutError):
            await c.read_holding_registers(0, 1)
        assert c.state == STATE_RECONNECTING

    run(body, timeout=0.1)


def test_late_reply_never_reaches_the_next_request():
    """The timeout closed the socket, so the late reply (value 11) is lost
    with it; the next read runs on a new connection."""

    async def body(st, c):
        st.regs.update({0: 11, 4: 44})
        st.script = [("late", 0.3)]
        with pytest.raises(ModbusTimeoutError):
            await c.read_holding_registers(0, 1)
        await asyncio.sleep(0.3)
        assert await c.read_holding_registers(4, 1) == [44]
        assert st.connections == 2 and c.consecutive_failures == 0

    run(body, timeout=0.15)


def test_foreign_transaction_id_is_out_of_step_and_closes():
    """No late reply can be pending (every error closes the socket), so a
    frame with a foreign transaction id means the stream is out of step:
    the socket is closed and a read is not retried (not a peer drop)."""

    async def body(st, c):
        st.regs[6] = 66
        st.script = [("stale", 1)]
        with pytest.raises(ModbusConnectionError) as err:
            await c.read_holding_registers(6, 1)
        assert err.value.kind == "out_of_step" and not err.value.reconnectable
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 1
        assert len(st.frames) == 1
        assert await c.read_holding_registers(6, 1) == [66]
        assert st.connections == 2

    run(body)


# --- broken connection: nothing is retried, no reconnect within 5 s ----------


def test_dropped_read_is_retried_once_on_a_fresh_connection():
    """evcc Connection.read: a read that fails with EOF / reset / broken pipe
    is sent once more at once on a fresh connection; only once."""

    async def body(st, c):
        st.regs[0] = 2
        st.script = ["drop"]
        assert await c.read_holding_registers(0, 1) == [2]
        assert st.connections == 2 and len(st.frames) == 2
        assert c.state == STATE_CONNECTED and c.consecutive_failures == 0
        st.script = ["drop", "drop"]
        with pytest.raises(ModbusConnectionError) as err:
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "closed_by_station" and err.value.reconnectable
        assert st.connections == 3 and len(st.frames) == 4  # one retry, not two
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 2
        assert await c.read_holding_registers(0, 1) == [2]
        assert st.connections == 4

    run(body)


def test_dropped_write_is_not_retried():
    """A write is never sent twice (it may already have executed); the next
    request opens a new connection at once."""

    async def body(st, c):
        st.script = ["drop"]
        with pytest.raises(ModbusConnectionError) as err:
            await c.write_register(95, 1)
        assert err.value.kind == "closed_by_station"
        await asyncio.sleep(0.05)
        assert len(st.frames) == 1 and st.connections == 1
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 1
        await c.write_register(91, 80)
        assert st.connections == 2 and st.regs[91] == 80

    run(body)


def test_idle_close_read_retried_write_fails():
    """The station closed the idle socket: like evcc the request is sent
    into it; a read hits EOF and is sent again on a fresh connection, a write
    fails (not retried, the station never got it) and the next request
    reconnects at once."""

    async def body(st, c):
        st.regs[0] = 7
        await c.read_holding_registers(0, 1)
        st.kick()
        await asyncio.sleep(0.05)
        assert c.state == STATE_DISCONNECTED  # EOF seen: no socket for the caller
        assert await c.read_holding_registers(0, 1) == [7]
        assert st.connections == 2 and c.consecutive_failures == 0
        st.kick()
        await asyncio.sleep(0.05)
        with pytest.raises(ModbusConnectionError) as err:
            await c.write_register(91, 80)
        assert err.value.kind == "closed_by_station"
        await asyncio.sleep(0.05)
        assert 91 not in st.regs and st.connections == 2
        await c.write_register(91, 80)
        assert st.connections == 3 and st.regs[91] == 80

    run(body)


def test_socket_timeout_is_a_broken_connection_not_a_missing_reply():
    async def body(st, c):
        await c.read_holding_registers(0, 1)
        st.script = ["silent"]
        pending = asyncio.ensure_future(c.read_holding_registers(0, 1))
        await asyncio.sleep(0.05)
        c._reader.set_exception(TimeoutError(110, "Connection timed out"))  # kernel ETIMEDOUT
        with pytest.raises(ModbusConnectionError):
            await pending
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 1

    run(body, timeout=1.0)


def test_failures_count_until_a_reply_each_on_a_new_connection():
    async def body(st, c):
        st.script = ["silent", "silent"]
        for n in (1, 2):
            with pytest.raises(ModbusTimeoutError):
                await c.read_holding_registers(0, 1)
            assert c.state == STATE_RECONNECTING and c.consecutive_failures == n
        assert st.connections == 2
        assert await c.read_holding_registers(0, 1) == [0]
        assert c.consecutive_failures == 0 and c.state == STATE_CONNECTED and st.connections == 3

    run(body, timeout=0.1)


def test_failure_is_counted_even_if_cancelled_while_closing():
    async def body(st, c):
        real_drop = c._drop

        async def slow_drop():
            await real_drop()
            await asyncio.sleep(0.5)  # stands for a slow wait_closed()

        c._drop = slow_drop
        st.script = ["drop"]
        pending = asyncio.ensure_future(c.write_register(95, 1))
        await asyncio.sleep(0.2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        c._drop = real_drop
        assert c.consecutive_failures == 1 and c.state == STATE_RECONNECTING

    run(body)


def test_refused_connect_counts_a_failure_and_is_tried_again_at_once():
    async def main():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        c = ModbusClient("127.0.0.1", port, delay=0.0)
        assert c.state == STATE_DISCONNECTED
        for n in (1, 2):
            with pytest.raises(ModbusConnectionError) as err:
                await c.read_holding_registers(0, 1)  # no cooldown: every request tries
            assert err.value.kind == "refused" and not err.value.reconnectable
            assert c.state == STATE_RECONNECTING and c.consecutive_failures == n
        await c.close()

    asyncio.run(main())


# --- reconnect pause ------------------------------------------------------------------


def test_no_backoff_while_running_every_request_connects():
    """Audit no 10/20/40/80/120 s ladder and no cooldown error any more;
    every request opens a connection if there is none."""

    async def body(st, c):
        st.refuse = True  # accepts, then closes at once (station not serving)
        for n in range(1, 4):
            with pytest.raises(ModbusConnectionError):
                await c.read_holding_registers(0, 1)
            assert st.connections == 2 * n  # each read: one try + one retry on a fresh connection
        with pytest.raises(ModbusConnectionError):
            await c.write_register(91, 80)
        assert st.connections == 7  # a write: one try, no retry
        st.refuse = False
        st.regs[0] = 1
        assert await c.read_holding_registers(0, 1) == [1]
        assert c.consecutive_failures == 0 and c.state == STATE_CONNECTED
        assert not hasattr(c, "seconds_until_retry") and not hasattr(c, "current_backoff")

    run(body)


def test_delay_60_ms_before_each_request_no_settle():
    """evcc EM2GO (TcpSettings Delay 60 ms, Connection.exec): a fixed 60 ms
    sleep before every request - the first one too, before its connect -
    and no pause of our own after a reply or after connecting."""

    async def body(st, c):
        loop = asyncio.get_running_loop()
        st.script = ["ok", ("exception", 2), "ok", "ok"]
        t0 = loop.time()
        await c.read_holding_registers(0, 1)
        with pytest.raises(ModbusExceptionError):
            await c.read_holding_registers(0, 1)
        await c.write_register(91, 80)
        await c.read_holding_registers(0, 2)
        times = [t for t, _raw in st.frames]
        assert st.accepted_at[0] - t0 >= 0.055  # the delay comes before the connect
        assert times[0] - st.accepted_at[0] < 0.05  # no settle pause after it
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert len(gaps) == 3 and min(gaps) >= 0.055 and max(gaps) < 0.2, gaps

    run(body, delay=0.06)


def test_default_delay_is_evcc_60_ms():
    from gnlt_charger.modbus import client as mc
    from gnlt_charger.modbus import const as C

    assert mc.DEFAULT_DELAY == C.DELAY_S == 0.06
    assert ModbusClient("192.0.2.1")._delay == 0.06
    assert not hasattr(C, "GAP_S") and not hasattr(C, "CONNECT_SETTLE_S")


# --- serialisation ------------------------------------------------------------------


def test_concurrent_callers_are_serialised():
    async def body(st, c):
        st.regs.update({a: 1000 + a for a in range(0, 20, 2)})
        st.script = [("late", 0.05)] * 10
        results = await asyncio.gather(*(c.read_holding_registers(a, 1) for a in range(0, 20, 2)))
        assert results == [[1000 + a] for a in range(0, 20, 2)]
        assert st.connections == 1 and len(st.frames) == 10
        # Every request reached the station only after the previous reply went out.
        for i in range(1, 10):
            prev_tid = st.request(i - 1)[0]
            assert st.frames[i][0] >= st.replied[prev_tid]

    run(body)


# --- close ------------------------------------------------------------------------------


def test_close_is_idempotent_and_final():
    async def body(st, c):
        await c.read_holding_registers(0, 1)
        await c.close()
        await c.close()
        assert c.state == STATE_CLOSED
        with pytest.raises(ModbusClosedError):
            await c.read_holding_registers(0, 1)
        with pytest.raises(ModbusClosedError):
            await c.write_register(91, 80)
        assert st.connections == 1

    run(body)


def test_close_while_a_request_waits_prevents_reconnect():
    async def body(st, c):
        st.script = [("late", 0.1)]
        first = asyncio.ensure_future(c.read_holding_registers(0, 1))
        await asyncio.sleep(0.02)
        queued = asyncio.ensure_future(c.read_holding_registers(2, 1))
        await asyncio.sleep(0)
        await c.close()
        assert await first == [0]
        with pytest.raises(ModbusClosedError):
            await queued
        assert st.connections == 1

    run(body)


def test_close_during_the_delay_opens_no_socket():
    """close() while a request sleeps its 60 ms delay (inside the
    lock) - the request must not connect afterwards."""

    async def body(st, c):
        pending = asyncio.ensure_future(c.read_holding_registers(0, 1))
        await asyncio.sleep(0.05)  # the request is in its delay
        await c.close()
        with pytest.raises(ModbusClosedError):
            await pending
        assert st.connections == 0 and c.connections == 0

    run(body, delay=0.3)


# --- malformed replies ------------------------------------------------------------


def test_every_timeout_closes_and_the_next_request_reconnects():
    async def body(st, c):
        st.script = ["silent", "silent", "ok", "silent", "silent", "ok"]
        for _ in range(6):
            try:
                await c.read_holding_registers(0, 1)
            except ModbusTimeoutError:
                pass
        assert st.connections == 5 and c.connections == 5 and c.state == STATE_CONNECTED

    run(body, timeout=0.1)


def test_short_read_is_a_protocol_error_and_closes():
    async def body(st, c):
        st.regs.update({0: 7, 1: 8})
        st.script = [("raw", bytes([0x03, 0])), ("raw", bytes([0x03, 2, 0, 7]))]
        for _ in range(2):
            with pytest.raises(ModbusProtocolError):
                await c.read_holding_registers(0, 2)
            # A whole frame: no link failure counted, but the socket is closed (evcc).
            assert c.state == STATE_DISCONNECTED and c.consecutive_failures == 0
        assert await c.read_holding_registers(0, 2) == [7, 8]
        assert st.connections == 3

    run(body)


def test_write_acknowledgement_must_echo_address_and_count():
    async def body(st, c):
        st.script = [("raw", bytes([0x10, 0, 92, 0, 1])), ("raw", bytes([0x10, 0, 91, 0, 2]))]
        for _ in range(2):
            with pytest.raises(ModbusProtocolError):
                await c.write_register(91, 80)

    run(body)


def test_exception_reply_without_code_is_a_protocol_error():
    async def body(st, c):
        st.script = [("raw", bytes([0x83]))]
        with pytest.raises(ModbusProtocolError):
            await c.read_holding_registers(0, 1)

    run(body)


def test_garbage_instead_of_a_reply_means_out_of_step():
    async def body(st, c):
        st.script = [("garbage", b"\xde\xad\xbe\xef\x00\x01\x02\x03")]
        with pytest.raises(ModbusConnectionError):
            await c.read_holding_registers(0, 1)
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 1

    run(body)


def test_garbage_while_idle_breaks_the_next_request():
    """Bytes the station sent unasked are read as the next reply: out of
    step, the socket is closed, a read is not retried."""

    async def body(st, c):
        await c.read_holding_registers(0, 1)
        st.inject(b"\xde\xad\xbe\xef\x00\x01\x02\x03")
        await asyncio.sleep(0.05)
        with pytest.raises(ModbusConnectionError) as err:
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "out_of_step"
        assert c.state == STATE_RECONNECTING
        assert await c.read_holding_registers(0, 1) == [0]
        assert st.connections == 2

    run(body)


# --- cancellation -------------------------------------------------------------------


def test_cancel_between_header_and_body_closes_the_socket():
    clock = FakeClock()

    async def body(st, c):
        st.regs.update({0: 5, 2: 9})
        st.script = [("split", 0.3)]
        pending = asyncio.ensure_future(c.read_holding_registers(0, 1))
        await asyncio.sleep(0.1)  # header consumed, body still on its way
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert c.state == STATE_DISCONNECTED and c.last_close_monotonic == clock.t
        assert await c.read_holding_registers(2, 1) == [9]  # a new connection at once
        assert st.connections == 2

    run(body, timeout=1.0, clock=clock)


def test_cancel_in_flight_closes_the_socket():
    """A cancelled request in flight leaves its reply unknown: the socket is
    closed (any error closes it) and the next request reconnects at once."""

    async def body(st, c):
        st.regs.update({0: 5, 2: 9})
        st.script = [("late", 0.2)]
        pending = asyncio.ensure_future(c.read_holding_registers(0, 1))
        await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert c.state == STATE_DISCONNECTED and c.consecutive_failures == 0
        assert await c.read_holding_registers(2, 1) == [9]
        assert st.connections == 2

    run(body, timeout=1.0)


def test_write_cancelled_while_queued_is_never_sent():
    async def body(st, c):
        st.script = [("late", 0.2)]
        first = asyncio.ensure_future(c.read_holding_registers(0, 1))
        await asyncio.sleep(0.02)
        queued = asyncio.ensure_future(c.write_register(95, 2))
        await asyncio.sleep(0.02)
        queued.cancel()
        assert await first == [0]
        await asyncio.sleep(0.05)
        assert len(st.frames) == 1 and 95 not in st.regs

    run(body)


# --- quiet window, connection count, failure kinds ---------------------------


def test_no_start_window_in_the_client():
    """no "quiet" start window in the
    client - a missing reply closes the socket also right after our 95 = 1."""

    async def body(st, c):
        assert not hasattr(c, "quiet_until")
        await c.read_holding_registers(0, 1)
        st.script = ["silent"]
        with pytest.raises(ModbusTimeoutError):
            await c.read_holding_registers(0, 1)
        assert c.state == STATE_RECONNECTING and c.consecutive_failures == 1

    run(body, timeout=0.15)


def test_no_own_reconnect_gap_like_evcc():
    """no delay of our own before a
    reconnect - the next request opens a new connection at once (only the
    60 ms delay before every request); a read dropped by the peer is sent
    once more at once."""

    async def body(st, c):
        st.regs[0] = 5
        st.script = ["silent"]
        with pytest.raises(ModbusTimeoutError):
            await c.read_holding_registers(0, 1)
        closed = asyncio.get_running_loop().time()
        assert await c.read_holding_registers(0, 1) == [5]
        assert st.accepted_at[1] - closed < 0.2
        st.script = ["drop"]
        t0 = asyncio.get_running_loop().time()
        assert await c.read_holding_registers(0, 1) == [5]  # retried at once
        assert st.accepted_at[2] - t0 < 0.3 and st.connections == 3

    run(body, timeout=0.1)


def test_no_min_reconnect_gap_setting():
    from gnlt_charger.modbus import client as mc
    from gnlt_charger.modbus import const as C

    assert not hasattr(mc, "DEFAULT_MIN_RECONNECT_GAP") and not hasattr(C, "MIN_RECONNECT_GAP_S")
    assert not hasattr(ModbusClient("192.0.2.1"), "_min_reconnect_gap")


def test_client_has_no_cooldown():
    """Audit no timeouts-in-a-row rule and no cooldown in the client."""
    c = ModbusClient("192.0.2.1")
    for name in ("seconds_until_retry", "current_backoff", "_reconnect_gap", "_max_timeouts"):
        assert not hasattr(c, name), name
    from gnlt_charger.modbus import client as mc

    for name in ("MAX_TIMEOUTS_IN_ROW", "DEFAULT_RECONNECT_GAP", "DEFAULT_BACKOFF", "MAX_BACKOFF", "MAX_STALE_REPLIES"):
        assert not hasattr(mc, name), name
    assert not hasattr(mc, "ModbusCooldownError")  # leftover removed


def test_entry_client_uses_the_const_timing():
    """Review (dead code): the constants of const.py are what an entry's
    client and the probe really use."""
    from gnlt_charger.modbus import const as C
    from gnlt_charger.modbus.runtime import new_client

    c = new_client("192.0.2.1", 502)
    assert (c._timeout, c._connect_timeout, c._delay, c._unit_id) == (
        C.REQUEST_TIMEOUT_S, C.CONNECT_TIMEOUT_S, C.DELAY_S, C.UNIT_ID
    )


def test_connections_counter():
    async def body(st, c):
        assert c.connections == 0
        await c.read_holding_registers(0, 1)
        await c.read_holding_registers(0, 1)
        assert c.connections == 1
        st.kick()
        await asyncio.sleep(0.05)
        await c.read_holding_registers(0, 1)  # EOF, then once more on a new socket
        assert c.connections == 2

    run(body)


def test_client_error_classes(monkeypatch):
    async def main():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        c = ModbusClient("127.0.0.1", port, delay=0.0)
        with pytest.raises(ModbusConnectionError) as err:
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "refused" and c.last_error_kind == "refused"
        await c.close()

        for raised, kind in (
            (TimeoutError(), "connect_timeout"),
            (OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
            (OSError(errno.EACCES, "denied"), "connect_failed"),
        ):
            async def fail(*_a, _raised=raised, **_k):
                raise _raised

            monkeypatch.setattr(asyncio, "open_connection", fail)
            c = ModbusClient("192.0.2.1", 502, delay=0.0)
            with pytest.raises(ModbusConnectionError) as err:
                await c.read_holding_registers(0, 1)
            assert err.value.kind == kind
            await c.close()
        monkeypatch.undo()

    asyncio.run(main())

    async def body(st, c):
        st.script = ["silent", ("exception", 2), "drop"]
        with pytest.raises(ModbusTimeoutError) as err:
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "no_answer"
        with pytest.raises(ModbusExceptionError) as err:
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "exception"
        with pytest.raises(ModbusConnectionError) as err:
            await c.write_register(91, 80)  # a write: not retried after the drop
        assert err.value.kind == "closed_by_station"
        await c.close()
        with pytest.raises(ModbusClosedError) as err:  # nothing sent
            await c.read_holding_registers(0, 1)
        assert err.value.kind == "closed"

    run(body, timeout=0.1)


# --- against the fake station of modbus_fake.py ----------------------------------------


def test_fake_fc06_no_reply():
    from modbus_fake import FakeStation, FakeTcpStation, SimClock

    async def main():
        model = FakeStation("A3C_2.7", clock=SimClock(realtime=True))
        server = FakeTcpStation(model)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(bytes.fromhex("0001 0000 0006 ff 06 005b 0050"))  # FC06 91 = 80
        await writer.drain()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.3)
        assert model.setpoint == 160 and model.fc06 and model.writes == []
        writer.close()
        await server.stop()

    asyncio.run(main())


def test_client_has_no_fc06():
    from pathlib import Path

    code = (Path(__file__).resolve().parents[1] / "custom_components/gnlt_charger/modbus/client.py").read_text(
        encoding="utf-8"
    )
    assert "0x06" not in code and "FC06 = " not in code


def test_start_stall_each_timeout_reconnects_client():
    # Scaled 1:10: a 1.2 s stall after 95 = 1 with a 0.1 s request timeout.
    # Like evcc every missing reply closes the socket and the next request
    # opens a new one at once; after the stall the station answers again.
    # (The fake's own reconnect rules are off here: min_gap_s 0, no churn, no burst.)
    from modbus_fake import Car, FakeStation, FakeTcpStation, SimClock

    async def main():
        model = FakeStation(
            "A3C_2.7", clock=SimClock(realtime=True), start_stall=True, stall_s=1.2, min_gap_s=0.0,
            churn_limit=None, burst_limit=10**6,
        )
        model.plug(Car())
        server = FakeTcpStation(model)
        await server.start()
        c = ModbusClient("127.0.0.1", server.port, delay=0.0, timeout=0.1)
        await c.write_register(95, 1)
        timeouts = 0
        loop = asyncio.get_running_loop()
        end = loop.time() + 1.6
        while loop.time() < end:
            try:
                await c.read_holding_registers(0, 1)
            except ModbusTimeoutError:
                timeouts += 1
        assert timeouts >= 8
        assert await c.read_holding_registers(0, 1) in ([3], [4])
        assert c.connections == timeouts + 1 and server.accepts == c.connections
        assert c.consecutive_failures == 0
        await c.close()
        await server.stop()

    asyncio.run(main())
