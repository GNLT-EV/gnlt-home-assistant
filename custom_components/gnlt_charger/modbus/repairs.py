"""Fix flow of the ``pnc_reset`` repair.

After a power cut the station may switch Plug & Charge back on (reg 93 -> 0)
while the user's stored choice is "off" (93 = 1). Nothing is written by
itself: the repair offers one click that writes 93 = 1 through the normal
write gate (link, identity, fresh read, read-back). The
issue disappears once 93 reads 1.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .client import ModbusError
from .runtime import modbus_data
from .station import WriteRefused


class PncResetRepairFlow(RepairsFlow):
    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        runtime = modbus_data(self.hass)["entries"].get(self._entry_id or "")
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await runtime.commander.async_pnc_restore()
            except WriteRefused as err:
                errors["base"] = err.key if err.key in FIX_ERRORS else "write_failed"
            except ModbusError:
                errors["base"] = "not_connected"
            else:
                runtime.async_update()
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"name": runtime.title},
        )


# Refusals shown in the fix flow; any other is "write_failed".
FIX_ERRORS = frozenset({"not_connected", "not_ready", "identity_mismatch", "busy", "station_busy"})


async def async_create_modbus_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    return PncResetRepairFlow((data or {}).get("entry_id"))
