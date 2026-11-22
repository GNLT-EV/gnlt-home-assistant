"""Phase current <-> power conversion (no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Source: evcc ``util/grid/grid.go``. The operation order is kept so float
results equal evcc's bit for bit.
"""

from __future__ import annotations

import math

VOLTAGE = 230.0  # nominal phase voltage, line-to-line for IT grids
_MIN_ACTIVE_CURRENT = 1.0

_no_neutral = False


def no_neutral() -> bool:
    """evcc grid.NoNeutral: three-wire grid without neutral (IT grid)."""
    return _no_neutral


def set_no_neutral(value: bool) -> None:
    """evcc grid.SetNoNeutral."""
    global _no_neutral  # noqa: PLW0603 - evcc keeps this as process-wide state
    _no_neutral = value


def factor(phases: int) -> float:
    """evcc grid.Factor."""
    if no_neutral() and phases == 3:
        return math.sqrt(3)
    return float(phases)


def current_to_power(current: float, phases: int) -> float:
    """evcc grid.CurrentToPower: per-phase current to total power."""
    return current * factor(phases) * VOLTAGE


def power_to_current(power: float, phases: int) -> float:
    """evcc grid.PowerToCurrent: total power to per-phase current."""
    return power / (factor(phases) * VOLTAGE)
