"""Repairs with a fix flow.

Only the ModBus mode raises fixable issues (Plug & Charge switched back on by
the station after a power cut: one click writes it off again). The ModBus
code is imported here only when such a fix flow is opened.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    from .modbus.repairs import async_create_modbus_fix_flow

    return await async_create_modbus_fix_flow(hass, issue_id, data)
