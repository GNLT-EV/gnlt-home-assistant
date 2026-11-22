"""Sequences of the ModBus mode (no Home Assistant imports).

the station is driven like evcc
drives an EM2GO box - a start is phases -> current -> 95 = 1, a stop is
95 = 2 -> 91 = 0, each write one request; no fresh read, no read-back, no
hold, no verification by sampling, no persisted step. What is left over time
is the whole-amp class phase switch (the EM2GO driver's workaround, 10 s
pause), kept as the one running sequence of the station (single flight): a user stop cancels it at once.

* ``Sequence``, ``StartResult``, ``StopResult``: shared by the command layer;
* ``SequenceMixin._run_phase_switch``: whole-amp class only (stop, 10 s,
  start, 200; behaviour of em2go.go, nothing copied). The new class writes
  200 directly (``Commander._run_phase_direct``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from . import const as C
from .station import WriteRefused, WriteResult

_LOGGER = logging.getLogger(__name__)

G_SETPOINT = C.GROUP_SETPOINT  # (91,1)
G_PHASES = C.PHASES_GROUP  # (200,1)
# The EM2GO workaround's pause between its stop and its start (em2go.go
# phases1p3p sleeps 10 s; behaviour only).
WORKAROUND_PAUSE_S = 10.0
G_COMMAND = C.ReadGroup((C.REG_COMMAND,))  # evcc Enabled(): reg 95 == 1

# Sequence kinds and results.
KIND_START = "start"
KIND_PHASE = "phase"

START_SENT = "sent"
START_ALREADY = "already_charging"

STOP_NOT_NEEDED = "not_needed"
STOP_SENT = "sent"
STOP_FAILED = "failed"  # the write did not go out


class SequenceCancelled(Exception):
    """A user stop or the unload cancelled the sequence."""


@dataclass
class Sequence:
    """The one running sequence of a station (single flight)."""

    kind: str
    origin: str
    started: float
    step: str = "precheck"
    cancelled: bool = False
    cancel_reason: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    committed: asyncio.Future | None = None

    def commit(self, result: Any = None, error: BaseException | None = None) -> None:
        fut = self.committed
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)


@dataclass(frozen=True)
class StartResult:
    outcome: str
    write: WriteResult | None = None


@dataclass(frozen=True)
class StopResult:
    outcome: str
    write: WriteResult | None = None


def _sent(res: WriteResult) -> bool:
    return res.outcome not in (C.WRITE_NOT_SENT, C.WRITE_REJECTED)


def _refusal(res: WriteResult) -> WriteRefused:
    return WriteRefused(C.REFUSE_NOT_CONNECTED if res.outcome == C.WRITE_NOT_SENT else C.REFUSE_STATION_BUSY)


class SequenceMixin:
    """The time-dependent step of ``Commander`` (the EM2GO workaround)."""

    def _check(self, seq: Sequence | None) -> None:
        if seq is not None and seq.cancelled:
            raise SequenceCancelled(seq.cancel_reason or "cancelled")

    async def _wait(self, seq: Sequence | None, seconds: float) -> None:
        """A wait that ends at once when the sequence is cancelled."""
        self._check(seq)
        if seconds <= 0:
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        if seq is None:
            await sleeper
            return
        waiter = asyncio.ensure_future(seq.event.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (sleeper, waiter):
                if not fut.done():
                    fut.cancel()
        self._check(seq)

    # --- phase switch, whole-amp class (the EM2GO driver's workaround) -------------------

    async def _run_phase_switch(self, seq: Sequence, target: int) -> WriteResult:
        """Whole-amp firmware class (evcc's 6.1 A probe read 6.0 back): the
        EM2GO driver's workaround - behaviour of em2go.go phases1p3p and
        Enable, nothing copied (not MIT). If the station is enabled (reg 95
        reads 1, evcc Enabled): stop (95 = 2, 91 = 0), wait 10 s, start
        (95 = 1; 200 = 1 again if the station was on one phase; the current),
        then 200 = target. Not enabled: only 200. Before 1 -> 3 the current
        drops to the minimum first, as evcc core scalePhases does for every
        charger (core/loadpoint.go, MIT, see NOTICE); the user's current comes
        back afterwards (``Commander._phase_current_back``). No verification,
        no learning: the next polls show what the station does."""
        st = self.station
        origin = seq.origin
        lowered = target == 3 and st.snapshot.phases != 3  # unknown counts as "not 3"
        was_one = st.snapshot.phases == 1
        current = self._start_current_target()
        restarted = False
        stopped = False
        try:
            if lowered:
                seq.step = "current"
                current = st.current_range_tenths()[0]
                drop = await self._write_current(current, origin, force=True)
                if drop is not None and not _sent(drop):
                    raise _refusal(drop)  # evcc scalePhases returns when setLimit fails
            seq.step = "enabled"
            self._check(seq)
            try:
                enabled = (await st.async_read(G_COMMAND)).get(C.REG_COMMAND) == C.COMMAND_START
            except Exception as err:
                raise WriteRefused(C.REFUSE_NOT_CONNECTED) from err
            if enabled:
                self._in_phase_switch = True
                if self.energy is not None:
                    self.energy.begin_phase_switch()
                seq.step = "stop"
                self._check(seq)
                stop = await st.async_write(C.REG_COMMAND, C.COMMAND_STOP, origin=origin)
                if stop.outcome == C.WRITE_NOT_SENT:
                    raise WriteRefused(C.REFUSE_NOT_CONNECTED)
                stopped = True
                self._stop_bookkeeping(stop, origin, note_energy=False)
                await self._write_stop_zero(stop, origin)
                seq.step = "pause"
                await self._wait(seq, WORKAROUND_PAUSE_S)
                seq.step = "start"
                r95 = await st.async_write(C.REG_COMMAND, C.COMMAND_START, origin=origin)
                if not _sent(r95):
                    raise _refusal(r95)
                restarted = True
                self._after_start_sent(r95)
                if not lowered:
                    # taken now: a change during the pause (a site decrease) counts
                    current = self._start_current_target()
                if was_one:
                    await st.async_write(C.REG_PHASES, 1, origin=origin)
                await self._write_current(current, origin, force=True)
            seq.step = "phases"
            self._check(seq)
            return await st.async_write(C.REG_PHASES, target, origin=origin)
        finally:
            self._in_phase_switch = False
            if lowered:
                self._phase_current_back(seq)
            if self.energy is not None:
                # The record stays open through the switch; a stop that cancelled
                # it decides the record's reason itself.
                self.energy.end_phase_switch(restarted or not stopped or seq.cancelled)


__all__ = [
    "Sequence",
    "SequenceCancelled",
    "SequenceMixin",
    "StartResult",
    "StopResult",
]
