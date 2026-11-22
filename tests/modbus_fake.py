"""Fake GNLT/AEFA ModBus station for tests.

Three pieces:

* ``FakeStation`` - the station model: registers, status machine, car, energy,
  per-profile behaviour and every measured quirk, plus the connection
  rules of the box (single session, reconnect silence, churn, half-open
  session). It runs on an injected clock (``SimClock``), so minute-scale
  rules are tested without waiting.
* ``FakeTcpStation`` - an in-process asyncio TCP server speaking MBAP byte for
  byte on 127.0.0.1, for client-level tests (framing, sockets, short timing).
  It logs accepts, open connections, frames and overlapping requests.
* ``FakeClient`` - an in-memory stand-in for ``ModbusClient`` (same API and
  the same rules: any error closes the session, the next request reconnects
  at once, a dropped read is retried once, error kinds) talking to a
  ``FakeStation`` directly. The station layer runs
  against it with the simulated clock: a missing reply moves the clock by the
  request timeout instead of waiting.

Behaviour not confirmed on a station is switchable; defaults follow the
measured behaviour.
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field, fields, replace

from gnlt_charger.modbus import client as mc

UNIT = 255
REALISTIC_LATENCY_S = 0.03  # request + reply on the LAN (with the 60 ms delay: ~0.09 s per transaction)
DOCUMENTED = frozenset(
    {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42,
     72, 78, 80, 87, 89, 91, 93, 95, 109, 111, 113, 200}
)  # stride 2: U32 words at a and a + 2, serial at 38/40/42


class SimClock:
    """Monotonic clock for the fake and the station layer.

    ``realtime=False`` (default): time moves only by ``advance``/``sleep``.
    ``realtime=True``: follows ``time.monotonic`` plus any ``advance`` offset
    (TCP tests that need real sockets but want to skip minutes).
    """

    def __init__(self, start: float = 1000.0, realtime: bool = False) -> None:
        self._offset = start
        self.realtime = realtime
        self._base = time.monotonic()

    def __call__(self) -> float:
        if self.realtime:
            return time.monotonic() - self._base + self._offset
        return self._offset

    def advance(self, seconds: float) -> None:
        self._offset += max(seconds, 0.0)

    async def sleep(self, seconds: float) -> None:
        if self.realtime:
            await asyncio.sleep(max(seconds, 0.0))
        else:
            self._offset += max(seconds, 0.0)
            await asyncio.sleep(0)


@dataclass
class Car:
    phases: int = 1
    min_a: float = 7.5  # a car that takes nothing below 7.5 A
    max_a: float = 32.0
    factor: float = 0.92  # drawn = offered x factor (10 A -> 9.2)
    ramp_s: float = 0.0  # status 4 with 0 A this long after the charge starts
    asleep: bool = False  # never draws


@dataclass
class Quirks:
    # framing
    max_regs_per_read: int = 3
    # Measured on a station: a block read of n at a returns a, a+2...;
    # odd addresses hold other bytes. 1 = the contiguous model of the documents.
    stride: int = 2
    over_read_exception: int = 0xFF
    undocumented_exception: bool = False  # undocumented regs -> exception 2
    wrong_unit: str = "silent"  # or "close"
    # commands
    stop: str = "works"  # "works" | "ignored" (B07: current to car min, stuck until replug)
    restart_after_stop: float | str | None = None  # None: at once; seconds; "replug"
    start_from_6: bool = True
    pnc_95_acts_anyway: bool = False  # 95 acts while 93 = 0 (open check N18/T8)
    starting_s: float = 2.0
    whole_amps: bool = False  # 61 -> 60
    phase_effect: str = "none"  # "none" | "pause" (4 -> 2 for phase_pause_s -> 4)
    phase_pause_s: float = 10.0
    phase_reverts_to_3_at_start: bool = False  # F8
    phases_readable: bool = True  # False: reg 200 answers exception 2 (V5 type)
    chatter_on_200_with_car: bool = False  # E1-140; ON in start and tests
    chatter_even_before_start: bool = False  # chatter also when 95 = 1 follows within the grace
    chatter_grace_s: float = 1.0
    chatter_flap_s: float = 2.0
    chatter_burst_a: float = 0.7
    chatter_silence_after_s: float = 25.0
    start_stall: bool = False  # no replies for stall_s after 95 = 1 is accepted
    stall_s: float = 12.0
    # ASSUMED: a write that arrives inside the stall is still executed, only
    # its reply never comes (the request sits in the TCP buffer of a busy
    # box); False: the stalled box loses it.
    stall_executes_writes: bool = True
    # meters and measurements
    reg28: str = "live"  # "live" | "lazy" (B07: 0.3 kWh about every 10 min) | "zero"
    reg28_step: int = 3
    reg28_every_s: float = 600.0
    power_factor: float = 1.0  # reg 12 = round_100(factor x sum U*I)
    per_phase_unit: int = 1  # W per unit of regs 16/20/24 (1 or 10, C-42)
    per_phase_zero: bool = False
    idle_l23_v: tuple[float, float] = (230.0, 230.0)
    reset_72_on_pause: bool = True
    status6_on_car_end: bool = True
    # power loss
    power_loss_93_to_0: bool = True
    power_loss_keeps_200: bool = True
    power_loss_status10_s: float = 0.0
    power_loss_zero_reads: int = 0
    boot_s: float = 20.0
    # station-side limits
    fallback_works: bool = False  # 87 applied after 89 s without requests
    menu_limit_a: float | None = None  # E5-001: min(menu, 91) is applied
    reg32_follows_menu: bool = False
    dry_contact: str = "off"  # "off" | "cap_6a" | "start_stop" | "suspend"
    reset_91_on_unplug: bool = False  # DaheimLaden type, off for AEFA
    reset_91_on_restart: bool = False
    a3b_11_15_to_16: bool = False
    a3b_160_draws_9_1: bool = False
    e31_80_reads_100_once: bool = False
    # connection level
    min_gap_s: float = 5.0  # a connect sooner after a close is refused / silent
    gap_mode: str = "refuse"  # "refuse" | "silent"
    silences: list[float] = field(default_factory=list)  # silent seconds per new connection
    close_fresh_after_s: float | None = None  # the station closes a new connection after this
    churn_window_s: float = 60.0  # measured: repeated open/close -> refused / timeouts
    churn_limit: int | None = None  # connects inside the window after which new ones fail
    churn_mode: str = "refuse"  # "refuse" | "timeout" | "silent"
    burst_limit: int = 6  # >= 6 connects in 10 s -> silent 60-90 s
    burst_window_s: float = 10.0
    burst_silence_s: float = 75.0
    hang_s: float = 120.0  # second client: hang lasts this long after both closed
    half_open_timeout_s: float = 120.0
    half_open_mode: str = "silent"  # "silent" | "refuse"
    # Measured: connects after a close fail as on
    # a station - within 2 s a connect timeout, within 5 s accepted but
    # silent, and once 3 connects were accepted within 60 s the next one is
    # refused, whatever the gap (measured: "10 s: ok, ok, refused").
    n3_connects: bool = False
    n3_timeout_within_s: float = 2.0
    n3_silent_within_s: float = 5.0
    n3_silent_s: float = 10.0
    n3_accepted_limit: int = 3


PROFILE_QUIRKS: dict[str, dict] = {
    "A3B_2.7": dict(
        stop="ignored", restart_after_stop="replug", reg28="lazy", power_factor=0.91,
        idle_l23_v=(0.0, 0.0), starting_s=3.0,
    ),
    # Measured: a reconnect after 0 s timed out / stayed empty
    # (silent, not refused); repeated connects failed even with 10 s gaps
    # ("10 s: ok, ok, refused"). How long the refusals last while connects go
    # on is NOT measured; the churn rule (churn_limit=4 per 60 s, refusals
    # while attempts continue) extrapolates it. With the evcc transport (a
    # connect attempt on every poll, no backoff)
    # that rule never lets the box recover - also with the 5 s minimum
    # reconnect gap (25 tests stay refused) - so
    # it is off by default and tested explicitly
    # (test_modbus_station.py:test_n3_churn_rule_vs_evcc_reconnect).
    "A3C_2.7": dict(
        max_regs_per_read=4, reg28="zero", power_factor=0.97, idle_l23_v=(2.0, 1.0),
        gap_mode="silent", churn_limit=None, churn_window_s=60.0, churn_mode="refuse",
    ),
    "A3C_3.1": dict(reg28="zero", power_factor=0.97, restart_after_stop=77.0),
    "unknown": dict(),
}


@dataclass
class Session:
    sid: int
    opened_at: float
    alive: bool = True
    never_answer: bool = False  # a second concurrent client
    silent_until: float = 0.0
    close_at: float | None = None
    killed: bool = False  # closed by the station (power loss, IP moved)


class _Exc(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def serial_words(serial: str) -> list[int]:
    """12 decimal digits as three BCD words (``301234567890`` -> 0x3012 0x3456 0x7890)."""
    return [int(serial[i : i + 4], 16) for i in (0, 4, 8)]


def _round100(watts: float) -> int:
    return int(round(watts / 100.0)) * 100


class FakeStation:
    """The station model. Every request goes through ``request``; every
    connection through ``connect``/``close``."""

    def __init__(
        self,
        profile: str = "A3C_2.7",
        serial: str = "301234567890",
        clock: SimClock | None = None,
        **quirks,
    ) -> None:
        self.clock = clock or SimClock()
        self.profile = profile
        base = PROFILE_QUIRKS.get(profile, {})
        self.q = Quirks(**{**base, **quirks})
        self.serial = serial
        # registers holding settings
        self.setpoint = 160
        self.mode = 1
        self.command = 2
        self.phases = 3
        self.max_tenths = 160
        self.min_tenths = 60
        self.cable_max = 160
        self.fallback = 160
        self.comm_timeout = 3600
        # state
        self.status = 1
        self.cable = 0
        self.error = 0
        self.car: Car | None = None
        self.forced_status: int | None = None
        self.forced_mode: int | None = None
        # Raw register values that win over the model (reg 1 filler, odd codes...).
        self.overrides: dict[int, int] = {}
        self.meter_wh = 0.0
        self.reg28 = 0
        self._lazy_base_wh = 0.0
        self._lazy_next: float | None = None
        self.session_wh = 0.0
        self.charge_started: float | None = None
        self._starting_until: float | None = None
        self._pause_until: float | None = None
        self._paused = False
        self._stuck = False
        self._stop_at: float | None = None
        self._stopped_since_plug = False
        self._last_drawn: float | None = None
        self._chatter_pending_at: float | None = None
        self._chatter_since: float | None = None
        self._stall_until = 0.0
        self._boot_until = 0.0
        self._status10_until = 0.0
        self._pnc_start_at: float | None = None
        self._zero_reads = 0
        self._read_100_once = False
        self.last_request_at = self.clock()
        self._last_update = self.clock()
        # connection state
        self.sessions: dict[int, Session] = {}
        self._next_sid = 1
        self.last_close_at: float | None = None
        self._connect_times: list[float] = []
        self._accepted_times: list[float] = []
        self._burst_silent_until = 0.0
        self._hung = False
        self._hang_until = 0.0
        self._half_open_until = 0.0
        self._silent_connects_until = 0.0
        self.port_closed = False  # ModBus off in the menu / OCPP mode: ECONNREFUSED
        self.host_down = False  # connect timeout
        self.stale_session = False  # refused until power_cycle()
        # logs for assertions
        self.requests: list[tuple[float, int, int, int, int]] = []  # (t, sid, fc, addr, count)
        self.writes: list[tuple[float, int, int]] = []  # (t, register, value)
        self.fc06: list[tuple[float, int, int]] = []
        self.connect_log: list[tuple[float, str]] = []
        self.script: list[dict] = []

    # --- test controls ---------------------------------------------------------

    def set_quirks(self, **changes) -> None:
        self.q = replace(self.q, **changes)

    def on_next(self, kind: str, *, fc: int | None = None, addr: int | None = None, **params) -> None:
        """One-shot behaviour for the next request matching ``fc``/``addr``.

        Kinds: ``silent``, ``late`` (delay), ``cut``, ``drop``, ``exception``
        (code), ``ack_not_stored``, ``drop_after_write`` (ack, silence_s).
        """
        self.script.append({"kind": kind, "fc": fc, "addr": addr, **params})

    def plug(self, car: Car | None = None) -> None:
        self._update()
        self.car = car or Car()
        self.cable = 1
        self.command = 2  # 95 returns to 2 after a re-plug
        self._stuck = False
        self._stopped_since_plug = False
        if self.mode == 0:
            self._begin_start()  # Plug & Charge: starts by itself at 91
        else:
            self.status = 2

    def unplug(self) -> None:
        self._update()
        self.car = None
        self.cable = 0
        self.status = 1
        self.charge_started = None
        self._starting_until = None
        self._paused = False
        self._stuck = False
        self._stopped_since_plug = False
        self._chatter_pending_at = None
        self._chatter_since = None
        self._last_drawn = None
        if self.q.reset_91_on_unplug:
            self.setpoint = 0

    def car_finish(self) -> None:
        """The car ends the charge by itself (battery full)."""
        self._update()
        self.status = 6 if self.q.status6_on_car_end else 2
        self._last_drawn = None

    def car_pause(self) -> None:
        """Car-side pause: 4 -> 2 with 0 A, no command from us."""
        self._update()
        if self.status == 4:
            self.status = 2
            self._paused = True

    def car_resume(self) -> None:
        self._update()
        if self._paused:
            self._paused = False
            self._enter_charging(self.clock(), reset_72=self.q.reset_72_on_pause)

    def external_start(self) -> None:
        """Station buttons / dry contact start without a ModBus command."""
        self._update()
        if self.cable and self.status in (2, 6):
            self._begin_start()

    def set_error(self, code: int) -> None:
        self.error = code

    def force_status(self, code: int | None) -> None:
        """Status read from reg 0 regardless of the model (5, 7-10, 31...)."""
        self.forced_status = code

    def menu_set_mode(self, value: int) -> None:
        """93 changed from the station menu."""
        self.mode = value

    def foreign_set_current(self, tenths: int) -> None:
        """91 changed by someone else while our link stays up."""
        self.setpoint = tenths

    def power_loss(self) -> None:
        """Mains off and on: sessions die, the box boots, settings reset."""
        self._update()
        now = self.clock()
        self.kill_sessions()
        self._boot_until = now + self.q.boot_s
        self._status10_until = self._boot_until + self.q.power_loss_status10_s
        if self.q.power_loss_93_to_0:
            self.mode = 0
        self.setpoint = 0 if self.q.reset_91_on_restart else 160
        if not self.q.power_loss_keeps_200:
            self.phases = 3
        self.session_wh = 0.0
        self.charge_started = None
        self._starting_until = None
        self._stuck = False
        self._paused = False
        self._chatter_pending_at = self._chatter_since = None
        self._stall_until = 0.0
        self._zero_reads = self.q.power_loss_zero_reads
        self.command = 2
        self.status = 2 if self.cable else 1
        if self.cable and self.mode == 0:
            self._pnc_start_at = self._boot_until

    def power_cycle(self) -> None:
        """Clears a stale session that made the box refuse connections."""
        self.stale_session = False

    def kill_sessions(self) -> None:
        for s in self.sessions.values():
            if s.alive:
                s.alive = False
                s.killed = True
        self._hung = False

    # --- model ------------------------------------------------------------------

    def _begin_start(self) -> None:
        now = self.clock()
        self.status = 3
        self._starting_until = now + self.q.starting_s
        if self.q.start_stall:
            self._stall_until = now + self.q.stall_s

    def _enter_charging(self, now: float, reset_72: bool = True) -> None:
        self.status = 4
        self._starting_until = None
        if self.charge_started is None or reset_72:
            self.charge_started = now
            self.session_wh = 0.0
        if self.q.phase_reverts_to_3_at_start:
            self.phases = 3

    def _status(self) -> int:
        now = self.clock()
        if self.forced_status is not None:
            return self.forced_status
        if now < self._status10_until and now >= self._boot_until - self.q.boot_s:
            if self.q.power_loss_status10_s and now >= self._boot_until:
                return 10
        if self._chatter_since is not None:
            phase = int((now - self._chatter_since) // self.q.chatter_flap_s)
            return 4 if phase % 2 else 2
        return self.status

    def _limit_a(self) -> float:
        limit = self.setpoint / 10.0
        if self.q.menu_limit_a is not None:
            limit = min(limit, self.q.menu_limit_a)
        if self.q.fallback_works and self.comm_timeout and (
            self.clock() - self.last_request_at >= self.comm_timeout
        ):
            limit = self.fallback / 10.0
        if self.q.dry_contact == "cap_6a":
            limit = 6.0
        if self.q.a3b_11_15_to_16 and 11.0 <= limit < 16.0:
            limit = 16.0
        return limit

    def currents(self) -> list[float]:
        now = self.clock()
        if self._chatter_since is not None:
            phase = int((now - self._chatter_since) // self.q.chatter_flap_s)
            return [self.q.chatter_burst_a if phase % 2 else 0.0, 0.0, 0.0]
        car = self.car
        if self.status != 4 or car is None or car.asleep or self._paused:
            return [0.0, 0.0, 0.0]
        if self.charge_started is not None and now < self.charge_started + car.ramp_s:
            return [0.0, 0.0, 0.0]
        if self._stuck:
            per = car.min_a
        elif self.q.a3b_160_draws_9_1 and self.setpoint == 160:
            per = 9.1
        else:
            limit = min(self._limit_a(), car.max_a)
            if limit < car.min_a:
                # The car does not go below its minimum: the current stays.
                per = self._last_drawn if self._last_drawn is not None else car.min_a
            else:
                per = round(limit * car.factor, 1)
        self._last_drawn = per
        n = min(car.phases, self.phases)
        return [per if i < n else 0.0 for i in range(3)]

    def voltages(self, amps: list[float] | None = None) -> list[float]:
        amps = self.currents() if amps is None else amps
        l2, l3 = self.q.idle_l23_v
        return [
            round(235.0 - 0.3 * amps[0], 1),
            233.0 if amps[1] > 0 else l2,
            233.0 if amps[2] > 0 else l3,
        ]

    def power_w(self, amps: list[float] | None = None) -> int:
        amps = self.currents() if amps is None else amps
        volts = self.voltages(amps)
        return _round100(self.q.power_factor * sum(u * i for u, i in zip(volts, amps)))

    def _update(self) -> None:
        now = self.clock()
        dt = now - self._last_update
        if dt > 0 and self.status == 4:
            wh = self.power_w() * dt / 3600.0
            self.meter_wh += wh
            self.session_wh += wh
        self._last_update = now
        if self.status == 3 and self._starting_until is not None and now >= self._starting_until:
            self._enter_charging(now)
        if self._pause_until is not None and now >= self._pause_until:
            self._pause_until = None
            if self.cable and self.status == 2:
                self._enter_charging(now, reset_72=True)
        if self._pnc_start_at is not None and now >= self._pnc_start_at:
            self._pnc_start_at = None
            if self.cable and self.mode == 0:
                self._begin_start()
        if (
            self._chatter_pending_at is not None
            and self._chatter_since is None
            and now - self._chatter_pending_at >= self.q.chatter_grace_s
        ):
            self._chatter_since = self._chatter_pending_at + self.q.chatter_grace_s
        # reg 28
        if self.q.reg28 == "live":
            self.reg28 = int(self.meter_wh // 100)
        elif self.q.reg28 == "lazy":
            if self.status == 4:
                if self._lazy_next is None:
                    self._lazy_next = now + self.q.reg28_every_s
                while now >= self._lazy_next:
                    steps = int((self.meter_wh - self._lazy_base_wh) // (self.q.reg28_step * 100))
                    self.reg28 += steps * self.q.reg28_step
                    self._lazy_base_wh += steps * self.q.reg28_step * 100
                    self._lazy_next += self.q.reg28_every_s
            else:
                self._lazy_next = None
        else:
            self.reg28 = 0
        # Ghost of a half-open session expires.
        if self._half_open_until and now >= self._half_open_until:
            self._half_open_until = 0.0
            self.last_close_at = now

    def _duration(self) -> int:
        if self.charge_started is None or not self.cable:
            return 0
        return int((self.clock() - self.charge_started) // 60) * 60

    def register(self, addr: int) -> int:
        """Current value of one register (0 for undocumented ones)."""
        return self._registers().get(addr, 0) if addr not in self.overrides else self.overrides[addr]

    def _registers(self) -> dict[int, int]:
        amps = self.currents()
        volts = self.voltages(amps)
        power = self.power_w(amps)
        pp = [0, 0, 0]
        if not self.q.per_phase_zero:
            pp = [int(u * i / self.q.per_phase_unit) for u, i in zip(volts, amps)]
        mode = self.forced_mode if self.forced_mode is not None else self.mode
        s = self.q.stride
        regs = {
            0: self._status(),
            2: self.cable,
            4: self.error,
            6: int(round(amps[0] * 10)),
            8: int(round(amps[1] * 10)),
            10: int(round(amps[2] * 10)),
            # U32: high word at a, low word at a + stride (a count-2 read returns
            # both, decoded (w0 << 16) | w1 as evcc does; ASSUMED until T1/T14).
            12: power >> 16,
            12 + s: power & 0xFFFF,
            16: pp[0] >> 16, 16 + s: pp[0] & 0xFFFF,
            20: pp[1] >> 16, 20 + s: pp[1] & 0xFFFF,
            24: pp[2] >> 16, 24 + s: pp[2] & 0xFFFF,
            28: self.reg28 >> 16,
            28 + s: self.reg28 & 0xFFFF,
            32: int(self.q.menu_limit_a * 10)
            if self.q.reg32_follows_menu and self.q.menu_limit_a is not None
            else self.max_tenths,
            34: self.min_tenths,
            36: self.cable_max,
            72: int(self.session_wh // 100),
            78: self._duration() >> 16,
            78 + s: self._duration() & 0xFFFF,
            87: self.fallback,
            89: self.comm_timeout,
            91: self.setpoint,
            93: mode,
            95: self.command,
            109: int(round(volts[0] * 10)),
            111: int(round(volts[1] * 10)),
            113: int(round(volts[2] * 10)),
            200: self.phases,
        }
        words = serial_words(self.serial)
        regs.update({38: words[0], 40: words[1], 42: words[2]})  # registers are even; stride is the block-read layout
        regs.update(self.overrides)
        return regs

    def _read(self, addr: int, count: int) -> list[int]:
        if count > self.q.max_regs_per_read:
            raise _Exc(self.q.over_read_exception)
        span = [addr + self.q.stride * i for i in range(count)]
        if not self.q.phases_readable and 200 in span:
            raise _Exc(2)
        if self.q.undocumented_exception and any(r not in DOCUMENTED for r in span):
            raise _Exc(2)
        if self._zero_reads > 0:
            self._zero_reads -= 1
            return [0] * count
        regs = self._registers()
        if 91 in span and self._read_100_once and 91 not in self.overrides:
            self._read_100_once = False
            regs[91] = 100  # measured anomaly: 91 = 80 then 95 = 1 read 100 once
        return [regs.get(r, self._odd_garbage(r)) for r in span]

    def _odd_garbage(self, addr: int) -> int:
        """Measured: an address between the registers (33, 35, 110...) answers
        other bytes (33 read 0xA000); 0 in the contiguous model."""
        if self.q.stride == 2:
            return (0xA000 | (addr * 37)) & 0xFFFF
        return 0

    def _write(self, addr: int, value: int) -> None:
        now = self.clock()
        self.writes.append((now, addr, value))
        if addr == 91:
            if value == 0:
                value = 50  # measured: 0 is stored as 50, the car keeps charging
            if self.q.whole_amps:
                value = value // 10 * 10
            self.setpoint = value
        elif addr == 93:
            self.mode = value  # no effect on a running charge
        elif addr == 95:
            self.command = value
            if value == 1:
                self._start_command(now)
            elif value == 2:
                self._stop_command(now)
        elif addr == 200:
            self.phases = value
            self._phase_written(now)
        elif addr == 87:
            self.fallback = value
        elif addr == 89:
            self.comm_timeout = value
        elif addr == 32:
            self.max_tenths = value

    def _start_command(self, now: float) -> None:
        if self._chatter_pending_at is not None and self._chatter_since is None:
            if not self.q.chatter_even_before_start and now - self._chatter_pending_at <= self.q.chatter_grace_s:
                self._chatter_pending_at = None  # written right before the start: no chatter
        if self.mode != 1 and not self.q.pnc_95_acts_anyway:
            return  # documents: 95 acts only with 93 = 1
        if self._stuck or not self.cable or self.car is None:
            return
        if self.car.asleep:
            return  # armed (95 echoes 1), the status stays 2 until the car wakes
        if self.status == 6 and not self.q.start_from_6:
            return
        if self.status not in (2, 6):
            return
        rule = self.q.restart_after_stop
        if self._stopped_since_plug:
            if rule == "replug":
                return
            if isinstance(rule, (int, float)) and self._stop_at is not None and now - self._stop_at < rule:
                return  # measured: an earlier 95 = 1 is ignored
        if self.q.e31_80_reads_100_once and self.setpoint == 80:
            self._read_100_once = True
        self._begin_start()

    def _stop_command(self, now: float) -> None:
        if self.mode != 1 and not self.q.pnc_95_acts_anyway:
            return
        if self.status not in (3, 4):
            return
        if self.q.stop == "ignored":
            self._stuck = True  # B07: current to car minimum, status stays 4
            self._stopped_since_plug = True
            return
        self.status = 2
        self._starting_until = None
        self._stop_at = now
        self._stopped_since_plug = True
        self._last_drawn = None

    def _phase_written(self, now: float) -> None:
        if self.car is not None and self.cable and self.status in (2, 6) and self.q.chatter_on_200_with_car:
            self._chatter_pending_at = now
            if self.q.chatter_even_before_start:
                self._chatter_since = now
        if self.status == 4 and self.q.phase_effect == "pause":
            self.status = 2
            self._pause_until = now + self.q.phase_pause_s

    # --- connections ----------------------------------------------------------------

    def connect(self) -> tuple[str, Session | None]:
        """``("accept", session)``, ``("refuse", None)`` or ``("timeout", None)``."""
        self._update()
        now = self.clock()
        recent = [t for t in self._connect_times if now - t < self.q.churn_window_s]
        burst = [t for t in self._connect_times if now - t < self.q.burst_window_s]
        self._connect_times.append(now)
        verdict, silent_until = "accept", 0.0
        if self.host_down:
            verdict = "timeout"
        elif self.port_closed or self.stale_session or now < self._boot_until:
            verdict = "refuse"
        elif self.q.churn_limit is not None and len(recent) >= self.q.churn_limit:
            if self.q.churn_mode == "silent":
                silent_until = now + self.q.churn_window_s
            else:
                verdict = self.q.churn_mode
        elif self._half_open_until and now < self._half_open_until:
            if self.q.half_open_mode == "refuse":
                verdict = "refuse"
            else:
                silent_until = self._half_open_until
        elif self.q.n3_connects and self.last_close_at is not None and (
            now - self.last_close_at < self.q.n3_timeout_within_s
        ):
            verdict = "timeout"
        elif self.q.n3_connects and len(
            [t for t in self._accepted_times if now - t < 60.0]
        ) >= self.q.n3_accepted_limit:
            verdict = "refuse"
        elif self.q.n3_connects and self.last_close_at is not None and (
            now - self.last_close_at < self.q.n3_silent_within_s
        ):
            silent_until = now + self.q.n3_silent_s
        elif self.last_close_at is not None and now - self.last_close_at < self.q.min_gap_s:
            if self.q.gap_mode == "refuse":
                verdict = "refuse"
            else:
                silent_until = self.last_close_at + self.q.min_gap_s
        if len(burst) + 1 >= self.q.burst_limit:
            self._burst_silent_until = now + self.q.burst_silence_s
        self.connect_log.append((now, verdict))
        if verdict != "accept":
            return verdict, None
        s = Session(self._next_sid, now)
        self._next_sid += 1
        self._accepted_times.append(now)
        if any(o.alive for o in self.sessions.values()):
            s.never_answer = True
            self._hung = True
        if self.q.silences:
            silent_until = max(silent_until, now + self.q.silences.pop(0))
        silent_until = max(silent_until, self._burst_silent_until, self._silent_connects_until)
        s.silent_until = silent_until
        if self.q.close_fresh_after_s is not None:
            s.close_at = now + self.q.close_fresh_after_s
        self.sessions[s.sid] = s
        return "accept", s

    def close(self, sid: int, fin: bool = True) -> None:
        """The client closed its socket; ``fin=False``: the FIN never arrived
        (HA crash, Wi-Fi loss) and the station keeps the session half-open."""
        s = self.sessions.get(sid)
        if s is None or not s.alive:
            return
        now = self.clock()
        s.alive = False
        if not fin:
            self._half_open_until = now + self.q.half_open_timeout_s
            return
        self.last_close_at = now
        if self._hung and not any(o.alive for o in self.sessions.values()):
            self._hung = False
            self._hang_until = now + self.q.hang_s

    def open_sessions(self) -> int:
        return sum(1 for s in self.sessions.values() if s.alive)

    # --- requests ----------------------------------------------------------------

    def _take_script(self, fc: int, addr: int) -> dict | None:
        for i, action in enumerate(self.script):
            if (action["fc"] is None or action["fc"] == fc) and (
                action["addr"] is None or action["addr"] == addr
            ):
                return self.script.pop(i)
        return None

    def request(self, sid: int, unit: int, pdu: bytes) -> tuple:
        """Handle one request PDU. Returns one of ``("reply", pdu)``,
        ``("silent")``, ``("late", delay, pdu)``, ``("cut", pdu)``,
        ``("drop", pdu_or_None)``."""
        self._update()
        now = self.clock()
        s = self.sessions.get(sid)
        if s is None or not s.alive:
            return ("drop", None)
        if s.close_at is not None and now >= s.close_at:
            self.close(sid)
            s.killed = True
            return ("drop", None)
        fc = pdu[0]
        addr = struct.unpack(">H", pdu[1:3])[0] if len(pdu) >= 3 else 0
        count = struct.unpack(">H", pdu[3:5])[0] if len(pdu) >= 5 else 0
        self.requests.append((now, sid, fc, addr, count))
        if s.never_answer or self._hung or now < self._hang_until or now < s.silent_until:
            return ("silent",)
        if unit != UNIT:
            return ("drop", None) if self.q.wrong_unit == "close" else ("silent",)
        if fc == 0x06:
            self.fc06.append((now, addr, count))
            return ("silent",)  # E5-009: FC06 gets no reply at all
        if fc not in (0x03, 0x10):
            return ("reply", bytes([fc | 0x80, 0x01]))
        if now < self._stall_until:
            if fc == 0x10 and self.q.stall_executes_writes:
                try:
                    self._process(fc, pdu)
                except _Exc:
                    pass
            return ("silent",)
        if self._chatter_since is not None and now - self._chatter_since >= self.q.chatter_silence_after_s:
            return ("silent",)
        self.last_request_at = now
        action = self._take_script(fc, addr)
        kind = action["kind"] if action else "ok"
        if kind == "silent":
            return ("silent",)
        if kind == "drop":
            self.close(sid)
            s.killed = True
            return ("drop", None)
        if kind == "exception":
            return ("reply", bytes([fc | 0x80, action.get("code", 2)]))
        try:
            reply = self._process(fc, pdu, store=kind != "ack_not_stored")
        except _Exc as err:
            reply = bytes([fc | 0x80, err.code])
        if kind == "late":
            return ("late", action.get("delay", 2.0), reply)
        if kind == "cut":
            return ("cut", reply)
        if kind == "drop_after_write":
            self.close(sid)
            s.killed = True
            self._silent_connects_until = now + action.get("silence_s", 90.0)
            return ("drop", reply if action.get("ack", True) else None)
        return ("reply", reply)

    def _process(self, fc: int, pdu: bytes, store: bool = True) -> bytes:
        if fc == 0x03:
            addr, count = struct.unpack(">HH", pdu[1:5])
            words = self._read(addr, count)
            return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *words)
        addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
        values = struct.unpack(f">{count}H", pdu[6 : 6 + 2 * count])
        if nbytes != 2 * count:
            raise _Exc(3)
        if store:
            for i, v in enumerate(values):
                self._write(addr + i, v)
        else:
            self.writes.append((self.clock(), addr, values[0]))
        return pdu[:5]


# --- TCP server -------------------------------------------------------------------


class FakeTcpStation:
    """MBAP server on 127.0.0.1 in front of a ``FakeStation``.

    The model's clock should be ``SimClock(realtime=True)`` here.
    """

    def __init__(self, model: FakeStation) -> None:
        self.model = model
        self.accepts = 0
        self.max_open = 0
        self.frames: list[tuple[float, bytes]] = []  # (loop time received, raw request)
        self.replies: list[tuple[float, int]] = []  # (loop time sent, tid)
        self.overlaps = 0  # requests sent before the previous reply was received
        self.detect_overlap = False  # costs 1 ms per request
        self._writers: dict[int, asyncio.StreamWriter] = {}
        self._tasks: set[asyncio.Task] = set()
        self._half_open_next = False
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    @property
    def open_connections(self) -> int:
        return sum(1 for w in self._writers.values() if not w.is_closing())

    async def start(self, port: int = 0) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port, reuse_address=True)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self.kick()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def close_port(self) -> None:
        """Stop listening: connects get ECONNREFUSED (ModBus off / OCPP mode)."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def reopen_port(self) -> None:
        await self.start(self.port)

    def kick(self) -> None:
        """Close every open connection from the station side."""
        for writer in self._writers.values():
            writer.close()

    def half_open_next_close(self) -> None:
        """The next close by the client is not seen by the station (no FIN)."""
        self._half_open_next = True

    @staticmethod
    def swap_addresses(a: FakeTcpStation, b: FakeTcpStation) -> None:
        """DHCP swapped the two stations' addresses; their TCP sessions die."""
        a.kick()
        b.kick()
        a.model, b.model = b.model, a.model

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _later(self, delay: float, writer, data: bytes, tid: int | None, close: bool = False) -> None:
        await asyncio.sleep(delay)
        if not writer.is_closing():
            writer.write(data)
            if tid is not None:
                self.replies.append((asyncio.get_running_loop().time(), tid))
            if close:
                writer.close()

    async def _handle(self, reader, writer) -> None:
        loop = asyncio.get_running_loop()
        self.accepts += 1
        model = self.model
        verdict, session = model.connect()
        if verdict != "accept":
            # A refused or timed-out connect cannot be produced after accept on
            # localhost: "refuse" closes at once, "timeout" never answers.
            if verdict == "refuse":
                writer.close()
                return
            session = None
        sid = session.sid if session else -1
        self._writers[id(writer)] = writer
        self.max_open = max(self.max_open, self.open_connections)
        if session is not None and session.close_at is not None:
            self._spawn(self._later(session.close_at - model.clock(), writer, b"", None, close=True))
        fin = True
        try:
            while True:
                head = await reader.readexactly(7)
                tid, _proto, length, unit = struct.unpack(">HHHB", head)
                body = await reader.readexactly(length - 1)
                now = loop.time()
                if self.detect_overlap:
                    # A client that waits for each reply has nothing more in
                    # flight while we hold its request unanswered.
                    await asyncio.sleep(0.001)
                    if reader._buffer:  # noqa: SLF001 - test helper
                        self.overlaps += 1
                self.frames.append((now, head + body))
                if session is None or self.model is not model:
                    continue
                result = model.request(sid, unit, body)
                kind = result[0]
                if kind == "reply":
                    writer.write(self._frame(tid, unit, result[1]))
                    self.replies.append((loop.time(), tid))
                elif kind == "late":
                    self._spawn(self._later(result[1], writer, self._frame(tid, unit, result[2]), tid))
                elif kind == "cut":
                    writer.write(self._frame(tid, unit, result[1])[:9])
                elif kind == "drop":
                    if result[1] is not None:
                        writer.write(self._frame(tid, unit, result[1]))
                    await writer.drain()
                    writer.close()
                    return
                # "silent": nothing
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            if self._half_open_next:
                self._half_open_next = False
                fin = False
        finally:
            if session is not None:
                model.close(sid, fin=fin)
            writer.close()

    @staticmethod
    def _frame(tid: int, unit: int, pdu: bytes) -> bytes:
        return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


# --- in-memory client double --------------------------------------------------------


class FakeClient:
    """``ModbusClient`` stand-in bound to a ``FakeStation`` on its clock.

    Mirrors the rules of ``modbus/client.py`` (transport like evcc): one
    session; ``delay`` before every request; any error - a missing or cut
    reply, a drop, an exception reply - closes the session and the next
    request opens a new one at once (evcc); a read that finds the session dropped
    by the station is sent once more on a fresh one, a write never. A missing
    reply moves the simulated clock by ``timeout``.
    """

    def __init__(
        self,
        station: FakeStation,
        *,
        timeout: float = mc.DEFAULT_TIMEOUT,
        connect_timeout: float = mc.DEFAULT_CONNECT_TIMEOUT,
        latency: float = 0.0,
        delay: float = 0.0,
        realistic: bool = False,
    ) -> None:
        self.station = station
        self.host, self.port = "fake", 502
        self._clock = station.clock
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        if realistic:
            # Real timing of one transaction: the client's 60 ms delay before
            # it + ~30 ms round trip = ~0.09 s; no settle after a connect.
            latency, delay = REALISTIC_LATENCY_S, mc.DEFAULT_DELAY
        self._latency = latency
        self._delay = delay
        self._session: Session | None = None
        self._closed = False
        self._closed_at: float | None = None
        self.failed_close_at: float | None = None
        self._failures = 0
        self._connections = 0
        self.last_error: str | None = None
        self.last_error_kind: str | None = None
        self.in_flight = 0
        self.max_in_flight = 0
        self.sent: list[tuple[float, int, int, int | list[int]]] = []  # (t, fc, addr, count/values)

    # same read-only state as ModbusClient
    @property
    def state(self) -> str:
        if self._closed:
            return mc.STATE_CLOSED
        if self._session is not None and self._session.alive:
            return mc.STATE_RECONNECTING if self._failures else mc.STATE_CONNECTED
        return mc.STATE_RECONNECTING if self._failures else mc.STATE_DISCONNECTED

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    @property
    def connections(self) -> int:
        return self._connections

    @property
    def last_close_monotonic(self) -> float | None:
        return self._closed_at

    @property
    def last_failure_close_monotonic(self) -> float | None:
        return self.failed_close_at

    def retarget(self, station: FakeStation) -> None:
        """The address now belongs to another station (DHCP); the session dies."""
        if self._session is not None:
            self.station.close(self._session.sid)
            self._session.alive = False
            self._session.killed = True
        self.station = station

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        if not isinstance(count, int) or not 1 <= count <= mc.MAX_READ_REGISTERS:
            raise ValueError(f"one request reads 1..3 registers, not {count!r}")
        pdu = struct.pack(">BHH", 0x03, address, count)
        body = await self._transact(pdu, f"read {address}+{count}", (0x03, address, count), retry=True)
        data = self._payload(body, 0x03, f"read {address}+{count}")
        if len(data) != 1 + 2 * count or data[0] != 2 * count:
            raise mc.ModbusProtocolError(f"read {address}+{count}: short reply")
        return list(struct.unpack(f">{count}H", data[1:]))

    async def write_register(self, address: int, value: int) -> None:
        await self.write_registers(address, [value])

    async def write_registers(self, address: int, values) -> None:
        words = [mc.u16(v) for v in values]
        pdu = struct.pack(f">BHHB{len(words)}H", 0x10, address, len(words), 2 * len(words), *words)
        body = await self._transact(pdu, f"write {address}={words}", (0x10, address, words), retry=False)
        data = self._payload(body, 0x10, f"write {address}")
        if data != struct.pack(">HH", address, len(words)):
            raise mc.ModbusProtocolError(f"write {address}: unexpected acknowledgement")

    async def close(self) -> None:
        self._closed = True
        self._drop()

    @staticmethod
    def _payload(body: bytes, fc: int, what: str) -> bytes:
        if body[0] == fc | 0x80:
            raise mc.ModbusExceptionError(fc, body[1], what)
        if body[0] != fc:
            raise mc.ModbusProtocolError(f"{what}: reply has function 0x{body[0]:02X}")
        return body[1:]

    def _drop(self) -> None:
        if self._session is not None:
            if self._session.alive:
                self.station.close(self._session.sid)
            self._session = None
            self._closed_at = self._clock()

    def _fail(self, error, text: str, kind: str, reconnectable: bool = False):
        self._failures += 1
        if self._session is not None:
            self.failed_close_at = self._clock()  # like client.last_failure_close_monotonic
        self.last_error, self.last_error_kind = text, kind
        self._drop()
        err = error(text)
        err.kind = kind
        if reconnectable:
            err.reconnectable = True
        return err

    async def _transact(self, pdu: bytes, what: str, log, retry: bool) -> bytes:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # let concurrent callers interleave like real I/O
            try:
                return self._attempt(pdu, what, log)
            except mc.ModbusConnectionError as err:
                if not (retry and getattr(err, "reconnectable", False)):
                    raise
            return self._attempt(pdu, what, log)  # evcc: a read once more on a fresh connection
        finally:
            self.in_flight -= 1

    def _attempt(self, pdu: bytes, what: str, log) -> bytes:
        if self._closed:
            raise mc.ModbusClosedError("fake client closed")
        if self._delay:
            self._clock.advance(self._delay)  # before the request and its connect, like client.py
        if self._session is not None and not self._session.alive:
            # Closed by the station while idle: the request runs into EOF.
            raise self._fail(
                mc.ModbusConnectionError, f"{what}: connection broke (EOF)", mc.KIND_CLOSED_BY_STATION, True
            )
        if self._session is None:
            verdict, session = self.station.connect()
            if verdict == "refuse":
                raise self._fail(mc.ModbusConnectionError, "connect: refused", mc.KIND_REFUSED)
            if verdict == "timeout":
                self._clock.advance(self._connect_timeout)
                raise self._fail(mc.ModbusConnectionError, "connect: timeout", mc.KIND_CONNECT_TIMEOUT)
            self._session = session
            self._connections += 1
        self.sent.append((self._clock(), *log))
        return self._exchange(pdu, what)

    def _exchange(self, pdu: bytes, what: str) -> bytes:
        if self._latency:
            self._clock.advance(self._latency)
        result = self.station.request(self._session.sid, UNIT, pdu)
        kind = result[0]
        if kind == "reply" or (kind == "drop" and result[1] is not None):
            # A reply. "drop" with a reply: the acknowledgement arrived, then
            # the station closed the socket; the next request finds it closed.
            self._failures = 0
            body = result[1]
            if body[0] & 0x80:
                self._drop()  # evcc closes the connection on any error, an exception reply too
            return body
        if kind == "drop":
            raise self._fail(
                mc.ModbusConnectionError, f"{what}: connection broke", mc.KIND_CLOSED_BY_STATION, True
            )
        self._clock.advance(self._timeout)
        if kind == "cut":
            raise self._fail(mc.ModbusTimeoutError, f"{what}: reply cut off", mc.KIND_CUT_OFF)
        # silent or late: no reply in time closes the session
        raise self._fail(mc.ModbusTimeoutError, f"{what}: no reply", mc.KIND_NO_ANSWER)


def quirk_names() -> list[str]:
    return [f.name for f in fields(Quirks)]
