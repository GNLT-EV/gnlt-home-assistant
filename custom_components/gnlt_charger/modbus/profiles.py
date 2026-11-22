"""Learned capabilities of a ModBus station.

No firmware profiles any more: every station is driven like evcc drives an EM2GO box - stop and phases
for every box, behaviour from the 6.1 A probe (read back as 60 the whole-amp
"workaround" class). The config flow shows one static note: on firmware 2.7
stop and phase switching while charging do not work over ModBus, OCPP is
recommended. A stored profile key is ignored.
"""

from __future__ import annotations

from typing import Any

from .const import LEARNED_DEFAULTS

_LEARNED_VALUES = {
    "decimal_current": {"unknown", "yes", "no"},
}


class LearnedCaps:
    """The firmware class of this start (the 6.1 A probe); not stored, no
    reset button."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._values = dict(LEARNED_DEFAULTS)
        for key, value in (data or {}).items():
            if key in _LEARNED_VALUES and value in _LEARNED_VALUES[key]:
                self._values[key] = value

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def set(self, key: str, value: str) -> bool:
        """Store a learned value; True if it changed."""
        if key not in _LEARNED_VALUES or value not in _LEARNED_VALUES[key]:
            raise ValueError(f"learned capability {key}={value!r} is not known")
        changed = self._values[key] != value
        self._values[key] = value
        return changed

    def reset(self) -> None:
        self._values = dict(LEARNED_DEFAULTS)

    def as_dict(self) -> dict[str, str]:
        return dict(self._values)
