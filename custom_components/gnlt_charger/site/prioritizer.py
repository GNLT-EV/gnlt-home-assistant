"""Loadpoint priorities: flexible power of lower-priority loadpoints (no Home Assistant imports).

Portions ported from evcc (https://github.com/evcc-io/evcc), commit 82d37e9,
MIT License, Copyright (c) evcc.io (andig, naltatis, premultiply)

Source: evcc ``core/prioritizer/prioritizer.go``. Each site cycle records
every loadpoint's flexible charge power; a surplus-flexible loadpoint may
count the flexible power of all loadpoints with a lower priority as
available.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


class Prioritizer:
    """evcc core/prioritizer/prioritizer.go: Prioritizer."""

    def __init__(self) -> None:
        self.demand: dict[Any, float] = {}

    def update_charge_power_flexibility(self, lp: Any) -> None:
        """evcc core/prioritizer/prioritizer.go: UpdateChargePowerFlexibility (tariff rates excluded)."""
        power = lp.get_charge_power_flexibility()
        if power >= 0:
            self.demand[lp] = power

    def get_charge_power_flexibility(self, lp: Any) -> float:
        """evcc core/prioritizer/prioritizer.go: GetChargePowerFlexibility.

        Sum of the flexible power of all loadpoints with a lower priority.
        """
        prio = lp.effective_priority()
        reduce_by = 0.0
        parts: list[str] = []

        for other, power in self.demand.items():
            if other.effective_priority() < prio and power > 0:
                reduce_by += power
                parts.append(f"{power:.0f}W from {other.get_title()} at prio {other.effective_priority()}")

        if reduce_by > 0:
            _LOGGER.debug(
                "lp %s at prio %d gets additional %s, total %.0fW", lp.get_title(), prio, ", ".join(parts), reduce_by
            )
        return reduce_by
