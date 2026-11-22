"""Injectable clock and timer sentinels (no Home Assistant imports, no sleeps).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

evcc injects ``github.com/benbjohnson/clock`` into the loadpoint; tests use
its mock clock, which starts at the Unix epoch. Here time is a float of
seconds since the epoch and durations are float seconds.

Go's zero ``time.Time`` (``IsZero``) is represented by:data:`ZERO_TIME`
(minus infinity, so "time since" it is infinite, like Go's saturated
duration), evcc's ``elapsed = time.Unix(0, 1)`` by:data:`ELAPSED`.
"""

from __future__ import annotations

import time
from typing import Protocol

ZERO_TIME = float("-inf")  # Go time.Time{}
ELAPSED = 1e-9  # evcc core/loadpoint.go: elapsed = time.Unix(0, 1)

SECOND = 1.0
MINUTE = 60.0
HOUR = 3600.0


class Clock(Protocol):
    """What the core needs from a clock."""

    def now(self) -> float:
        """Seconds since the epoch."""


MONOTONIC_OFFSET = 1e9  # s, keeps ELAPSED far in the past right after boot


class SystemClock:
    """Monotonic clock (evcc's clock.New() measures elapsed time monotonically).

    Not wall time: only differences are meaningful. The offset keeps
    data:`ELAPSED` (1 ns) far in the past even right after boot.
    """

    def now(self) -> float:
        return time.monotonic() + MONOTONIC_OFFSET


class MockClock:
    """benbjohnson/clock Mock: starts at the epoch, moves only when told."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def add(self, seconds: float) -> None:
        self._now += seconds

    def set(self, t: float) -> None:
        self._now = t


def is_zero(t: float) -> bool:
    """Go ``time.Time.IsZero``."""
    return t == ZERO_TIME


def since(clock: Clock, t: float) -> float:
    """Go ``clock.Since(t)`` in seconds."""
    return clock.now() - t
