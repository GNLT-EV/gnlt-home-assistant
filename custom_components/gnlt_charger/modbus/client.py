# MIT License
#
# Copyright (c) 2026 Lutz Fiebach
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# ---------------------------------------------------------------------------
# Modified by GNLT, 2026 (MIT, see the LICENSE file of this repository).
# Based on `custom_components/pvsc/modbus_client.py` of Lu-Fi's ha-em2go-pvsc
# (commit 1f8203b): one serialised persistent connection, MBAP framing with
# transaction ids, no reconnect after close(), FC16 for single writes, short
# replies rejected.
# GNLT changes: English text; RTU framing removed; FC16 multi-register write;
# connection handling as evcc's util/modbus (see below): a fixed delay before
# each request, any error closes the connection, the next request opens a new
# one at once, an idempotent read is retried once on a fresh connection after
# the peer closed the old one; typed errors with a failure kind; "reconnecting"
# state for callers; U16/U32 helpers; a count of opened connections.
#
# The connection rules are ported from evcc, util/modbus/connection.go
# (``exec``, ``read``, ``reconnectable``; master 82d37e9):
# Copyright (c) evcc.io (andig, naltatis, premultiply), MIT License - the same
# terms as the notice above.
# ---------------------------------------------------------------------------
"""Asyncio ModBus TCP client for GNLT/AEFA chargers (wall EVE, unit 255).

Plain standard library on purpose: ``pymodbus`` would clash with the version
pinned by Home Assistant's own ModBus integration.

Rules this client enforces:

* One persistent connection, one request at a time (the station serves a
  single ModBus session).
* FC03 for reads: the station layer reads like evcc, count 1 per 16-bit
  register and count 2 for a U32 (the register documents answer exception
  0xFF to more than 3). FC16 for every write, also of one register:
  FC06 is not in the documents and other users report it ignored.
* Unit id 255, MBAP framing, a fixed delay of 60 ms before every request,
  as evcc's EM2GO driver configures its connection (TcpSettings Delay);
  no pause of our own after a reply or after connecting.
* Any error - no reply in time, a broken or foreign frame, a ModBus
  exception reply, a cancelled request in flight - closes the connection
  (evcc ``Connection.exec``). The
  next request opens a new one at once, with no pause of our own.
* A read that fails because the peer dropped the socket (EOF, connection
  reset, broken pipe) is sent once more at once on a fresh connection (evcc
  ``Connection.read``). A write is never retried: it may already have
  executed.

Notes for the caller (station coordinator):

* ``connections`` grows by one with every socket opened: the caller re-checks
  the station identity first on every new connection (DHCP may have given
  the address to another unit). A read retried after a drop runs on a new
  connection, so its value belongs to a connection whose identity is not
  checked yet.
* ``consecutive_failures`` counts failed requests (and connects) since the
  last reply; an exception reply is a reply.
* Every ``ModbusError`` carries ``kind`` (``refused``, ``connect_timeout``,
  ``unreachable``, ``closed_by_station``, ``no_answer``...) so texts can tell
  "the host is up but refuses" from "the host is not reachable".
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import struct
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 255

MAX_READ_REGISTERS = 3
# FC16 limit of the ModBus specification; the station is only known to accept
# single-register writes.
MAX_WRITE_REGISTERS = 123

DEFAULT_TIMEOUT = 1.5
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_DELAY = 0.06  # evcc EM2GO: Delay 60 ms before each request

FC_READ_HOLDING_REGISTERS = 0x03
FC_WRITE_MULTIPLE_REGISTERS = 0x10

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_CLOSED = "closed"

_MBAP = struct.Struct(">HHHB")


# Failure kinds (``ModbusError.kind``).
KIND_REFUSED = "refused"  # ECONNREFUSED: the host is up, nothing serves the port
KIND_CONNECT_TIMEOUT = "connect_timeout"
KIND_UNREACHABLE = "unreachable"  # no route to the host / network down
KIND_CONNECT_FAILED = "connect_failed"  # any other connect error
KIND_CLOSED_BY_STATION = "closed_by_station"  # EOF, reset, broken pipe
KIND_OUT_OF_STEP = "out_of_step"
KIND_NO_ANSWER = "no_answer"  # TCP open, no reply within the timeout
KIND_CUT_OFF = "cut_off"  # a reply stopped in the middle of a frame
KIND_EXCEPTION = "exception"
KIND_PROTOCOL = "protocol"
KIND_CLOSED = "closed"

_UNREACHABLE_ERRNOS = {
    getattr(errno, name)
    for name in ("EHOSTUNREACH", "ENETUNREACH", "EHOSTDOWN", "ENETDOWN")
    if hasattr(errno, name)
}


class ModbusError(Exception):
    """Any failure of a ModBus request."""

    kind: str = KIND_CONNECT_FAILED


class ModbusExceptionError(ModbusError):
    """The station answered with a ModBus exception (a valid reply)."""

    kind = KIND_EXCEPTION

    def __init__(self, function: int, code: int, what: str) -> None:
        self.function = function
        self.code = code
        super().__init__(
            f"{what}: station answered exception code {code} (0x{code:02X}) "
            f"to function 0x{function:02X}"
        )


class ModbusTimeoutError(ModbusError):
    """No reply within the request timeout."""

    kind = KIND_NO_ANSWER


class ModbusConnectionError(ModbusError):
    """Connect failed, the connection broke or the stream got out of step.

    ``reconnectable``: the peer dropped the socket (EOF, reset, broken pipe),
    the case in which evcc sends a read once more on a fresh connection.
    """

    kind = KIND_CONNECT_FAILED
    reconnectable = False


class ModbusProtocolError(ModbusError):
    """A reply with the right transaction id but unexpected content."""

    kind = KIND_PROTOCOL


class ModbusClosedError(ModbusError):
    """The client was closed (entry unloaded); it never connects again.
    Nothing was sent: callers treat it as "not a new link error"."""

    kind = KIND_CLOSED


class _Dropped(Exception):
    """The connection broke during a request (EOF, reset, socket timeout)."""

    def __init__(self, text: str, reconnectable: bool) -> None:
        super().__init__(text)
        self.reconnectable = reconnectable


class _OutOfStep(Exception):
    """The byte stream no longer lines up with MBAP frames."""


class _NoReply(Exception):
    """Our own request timeout expired."""


def _reconnectable(err: BaseException) -> bool:
    """evcc ``reconnectable``: EPIPE, ECONNRESET or EOF - the peer dropped
    the socket, so the request never reached the device or got no reply."""
    return isinstance(err, (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError))


def _dropped(err: BaseException) -> _Dropped:
    return _Dropped(str(err) or type(err).__name__, _reconnectable(err))


# --- register value helpers -------------------------------------------------


def u16(value: int) -> int:
    """Check that ``value`` fits one register.

    Masking instead would turn a bug like -1 into 65535 A*10 on the wire.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        raise ValueError(f"register value out of range 0..65535: {value!r}")
    return value


def to_u32(words: Sequence[int], index: int = 0) -> int:
    """U32 from two registers, high word first.

    The words of one count-2 read, as evcc decodes them (on a stride-2 box
    the values of a and a+2):
    ``0x0000 0x2B20`` = 11040 W.
    """
    return (u16(words[index]) << 16) | u16(words[index + 1])


def from_u32(value: int) -> list[int]:
    """Two registers for a U32, high word first."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"U32 value out of range: {value!r}")
    return [value >> 16, value & 0xFFFF]


def _connect_kind(err: BaseException) -> str:
    if isinstance(err, ConnectionRefusedError):
        return KIND_REFUSED
    if isinstance(err, TimeoutError):
        return KIND_CONNECT_TIMEOUT
    if isinstance(err, OSError) and err.errno in _UNREACHABLE_ERRNOS:
        return KIND_UNREACHABLE
    return KIND_CONNECT_FAILED


def _check_span(address: int, count: int) -> None:
    if not isinstance(address, int) or address < 0 or address + count > 0x10000:
        raise ValueError(f"register range out of 0..65535: {address!r}+{count}")


# --- client ------------------------------------------------------------------


class ModbusClient:
    """Serialised ModBus TCP client holding one persistent connection.

    All public methods may be called concurrently: requests queue on one lock
    and go out one by one, each after a fixed ``delay`` (evcc: 60 ms).
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        unit_id: int = DEFAULT_UNIT_ID,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        delay: float = DEFAULT_DELAY,
        max_read_registers: int = MAX_READ_REGISTERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.port = port
        self._unit_id = unit_id
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._delay = max(delay, 0.0)
        self._max_read = max_read_registers
        # ``last_close_monotonic`` uses this clock (tests replace it).
        self._clock = clock

        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Set before close() waits for the lock, so a request already queued
        # cannot open a new socket after unload: the orphan connection would
        # lock the next instance out of the single-session station.
        self._closed = False
        self._closed_at: float | None = None
        # Last close caused by a failed request on an open socket (no reply,
        # dropped by the peer, out of step) - not by close() or a cancel.
        self._failed_close_at: float | None = None
        # True between reading a reply header and its body (a timeout there
        # is a reply cut off).
        self._partial = False
        self._tid = 0
        self._failures = 0
        self._connections = 0
        self.last_error: str | None = None
        self.last_error_kind: str | None = None

    # --- state for callers ---------------------------------------------------

    @property
    def state(self) -> str:
        """``connected`` / ``reconnecting`` / ``disconnected`` / ``closed``.

        ``reconnecting``: the last request failed and no reply came since -
        whether the next request finds a socket or opens a new one. A socket
        the peer has closed (EOF seen) counts as none.
        """
        if self._closed:
            return STATE_CLOSED
        if self._open():
            return STATE_RECONNECTING if self._failures else STATE_CONNECTED
        return STATE_RECONNECTING if self._failures else STATE_DISCONNECTED

    @property
    def consecutive_failures(self) -> int:
        """Failed requests and connects since the last reply."""
        return self._failures

    @property
    def connections(self) -> int:
        """Sockets opened by this client so far (a new one = a new session)."""
        return self._connections

    @property
    def last_close_monotonic(self) -> float | None:
        """Clock time (``time.monotonic`` by default) the socket was last closed."""
        return self._closed_at

    @property
    def last_failure_close_monotonic(self) -> float | None:
        """Clock time of the last close caused by a failed request on an open
        socket (peer close, no reply, broken frame); not close() or a cancel."""
        return self._failed_close_at

    def _open(self) -> bool:
        writer, reader = self._writer, self._reader
        return writer is not None and not writer.is_closing() and not (reader is not None and reader.at_eof())

    # --- public API ------------------------------------------------------------

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        """FC03 of 1..3 registers in one request; retried once on a fresh
        connection if the peer dropped the socket (evcc ``read``)."""
        if not isinstance(count, int) or not 1 <= count <= self._max_read:
            raise ValueError(f"one request reads 1..{self._max_read} registers, not {count!r}")
        _check_span(address, count)
        what = f"read {address}+{count}"
        pdu = struct.pack(">BHH", FC_READ_HOLDING_REGISTERS, address, count)

        def parse(body: bytes) -> list[int]:
            data = self._payload(body, FC_READ_HOLDING_REGISTERS, what)
            # A short reply (byte count 0 was seen elsewhere) is an error, never
            # a partial list: the caller would index it blindly mid-update.
            if len(data) != 1 + 2 * count or data[0] != 2 * count:
                raise ModbusProtocolError(f"{what}: expected {count} registers, got {body.hex(' ')}")
            return list(struct.unpack(f">{count}H", data[1:]))

        return await self._transact(pdu, what, parse, retry=True)

    async def write_register(self, address: int, value: int) -> None:
        """Write one register with FC16 (quantity 1), never FC06."""
        await self.write_registers(address, [value])

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        """FC16 write; never retried, the station may already have executed it."""
        words = [u16(v) for v in values]
        count = len(words)
        if not 1 <= count <= MAX_WRITE_REGISTERS:
            raise ValueError(f"one request writes 1..{MAX_WRITE_REGISTERS} registers, not {count}")
        _check_span(address, count)
        what = f"write {address}={words if count > 1 else words[0]}"
        pdu = struct.pack(
            f">BHHB{count}H", FC_WRITE_MULTIPLE_REGISTERS, address, count, 2 * count, *words
        )

        def parse(body: bytes) -> None:
            data = self._payload(body, FC_WRITE_MULTIPLE_REGISTERS, what)
            if data != struct.pack(">HH", address, count):
                raise ModbusProtocolError(f"{what}: unexpected acknowledgement {body.hex(' ')}")

        await self._transact(pdu, what, parse, retry=False)

    async def close(self) -> None:
        """Close for good; safe to call more than once."""
        self._closed = True
        async with self._lock:
            await self._drop()

    # --- transactions ------------------------------------------------------------

    @staticmethod
    def _payload(body: bytes, function: int, what: str) -> bytes:
        if body[0] == function | 0x80:
            if len(body) != 2:
                raise ModbusProtocolError(f"{what}: malformed exception reply {body.hex(' ')}")
            raise ModbusExceptionError(function, body[1], what)
        if body[0] != function:
            raise ModbusProtocolError(f"{what}: reply has function 0x{body[0]:02X}")
        return body[1:]

    async def _transact(self, pdu: bytes, what: str, parse: Callable[[bytes], _T], *, retry: bool) -> _T:
        async with self._lock:
            try:
                return await self._attempt(pdu, what, parse)
            except ModbusConnectionError as err:
                if not (retry and err.reconnectable):
                    raise
                _LOGGER.debug("ModBus %s:%s %s: read again on a fresh connection", self.host, self.port, what)
            return await self._attempt(pdu, what, parse)

    async def _attempt(self, pdu: bytes, what: str, parse: Callable[[bytes], _T]) -> _T:
        """One request: the delay, a connect if there is no socket, the exchange.
        Any error closes the socket (evcc ``exec``)."""
        if self._closed:
            raise ModbusClosedError(f"ModBus client for {self.host}:{self.port} is closed")
        if self._delay:
            # Before the request (and before a connect it may need), as evcc's
            # Connection.exec sleeps before each call.
            await asyncio.sleep(self._delay)
            if self._closed:
                # close() came during the delay: no socket after unload.
                raise ModbusClosedError(f"ModBus client for {self.host}:{self.port} is closed")
        await self._ensure_connected()
        try:
            body = await self._exchange(pdu)
        except _Dropped as err:
            raise await self._fail(
                ModbusConnectionError, f"{what}: connection broke ({err})", KIND_CLOSED_BY_STATION,
                reconnectable=err.reconnectable,
            ) from None
        except _OutOfStep as err:
            raise await self._fail(ModbusConnectionError, f"{what}: {err}", KIND_OUT_OF_STEP) from None
        except _NoReply:
            text = f"{what}: no reply within {self._timeout} s"
            if self._partial:
                raise await self._fail(ModbusTimeoutError, f"{text} (reply cut off)", KIND_CUT_OFF) from None
            raise await self._fail(ModbusTimeoutError, text) from None
        except asyncio.CancelledError:
            # The request may be out and its reply is unknown: this socket is
            # done (any error closes it).
            self._drop_now()
            raise
        self._record_success()
        try:
            return parse(body)
        except (ModbusExceptionError, ModbusProtocolError) as err:
            # A whole frame came back, so the link answered (no failure
            # counted); evcc still closes the connection on any error, an
            # exception reply included.
            self.last_error = str(err)
            self.last_error_kind = err.kind
            _LOGGER.debug("ModBus %s:%s %s; connection closed", self.host, self.port, err)
            await self._drop()
            raise

    async def _read_frame(self) -> tuple[int, bytes]:
        """One MBAP frame: (transaction id, PDU)."""
        assert self._reader is not None
        head = await self._reader.readexactly(_MBAP.size)
        tid, proto, length, _unit = _MBAP.unpack(head)
        if proto != 0 or not 2 <= length <= 254:
            raise _OutOfStep(f"bad MBAP header {head.hex(' ')}")
        self._partial = True
        body = await self._reader.readexactly(length - 1)
        self._partial = False
        return tid, body

    async def _exchange(self, pdu: bytes) -> bytes:
        assert self._writer is not None
        self._tid = self._tid % 0xFFFF + 1
        tid = self._tid
        try:
            self._writer.write(_MBAP.pack(tid, 0, len(pdu) + 1, self._unit_id) + pdu)
            await self._writer.drain()
        except (ConnectionError, OSError) as err:
            raise _dropped(err) from err

        timer = asyncio.timeout(self._timeout)
        try:
            async with timer:
                r_tid, body = await self._read_frame()
        # TimeoutError is an OSError, so it is sorted out first. Only our own
        # timer means "no reply"; a TimeoutError from the socket (ETIMEDOUT)
        # means the connection is dead.
        except TimeoutError as err:
            if timer.expired():
                raise _NoReply from None
            raise _dropped(err) from err
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as err:
            raise _dropped(err) from err
        if r_tid != tid:
            # Every error closes the socket, so no late reply can be pending:
            # a foreign id means the stream is out of step.
            raise _OutOfStep(f"reply with transaction id {r_tid}, expected {tid}")
        return body

    # --- connection --------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._writer is not None:
            # A socket the peer closed is used as it is: the request then
            # fails with EOF / reset and a read is sent again on a fresh
            # connection (evcc). Only one we closed ourselves is replaced.
            if not self._writer.is_closing():
                return
            await self._drop()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self._connect_timeout
            )
        except (TimeoutError, OSError) as err:
            detail = str(err) or type(err).__name__
            raise await self._fail(
                ModbusConnectionError,
                f"connect to {self.host}:{self.port}: {detail}",
                _connect_kind(err),
            ) from None
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        self._reader, self._writer = reader, writer
        self._connections += 1
        self._partial = False
        _LOGGER.debug("ModBus %s:%s: connected", self.host, self.port)

    def _drop_now(self) -> None:
        """Close the socket synchronously (safe inside a cancel)."""
        writer = self._writer
        self._reader = self._writer = None
        self._partial = False
        if writer is None:
            return
        self._closed_at = self._clock()
        writer.close()

    async def _drop(self) -> None:
        writer = self._writer
        self._drop_now()
        if writer is None:
            return
        try:
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        except (TimeoutError, OSError):
            pass

    async def _fail(
        self, error: type[ModbusError], text: str, kind: str | None = None, *, reconnectable: bool = False
    ) -> ModbusError:
        """Count the failure and close the socket; the next request reconnects at once."""
        # Count first: a cancel while waiting for the socket to close must
        # not lose the failure.
        self._failures += 1
        if self._writer is not None:
            self._failed_close_at = self._clock()
        self.last_error = text
        self.last_error_kind = kind or error.kind
        message = f"{text}; failure {self._failures} in a row, connection closed"
        _LOGGER.debug("ModBus %s:%s %s", self.host, self.port, message)
        await self._drop()
        result = error(message)
        result.kind = self.last_error_kind
        if reconnectable:
            result.reconnectable = True
        return result

    def _record_success(self) -> None:
        if self._failures:
            _LOGGER.info(
                "ModBus %s:%s: connection restored after %d failure(s)",
                self.host, self.port, self._failures,
            )
        self._failures = 0
