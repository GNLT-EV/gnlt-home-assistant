"""Diagnostics download of a ModBus entry.

Versions, learned behaviour, options, link state and statistics,
the last poll's registers raw and decoded, records, both energy paths,
totals, desired values, problem flags, the write log and the transaction
ring. Built from memory only: it never opens a socket.

The station's host name / IP address is redacted everywhere, also inside
error texts; the serial number stays (it is the OCPP identity too and is in
the OCPP diagnostics as well).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from ..const import DOMAIN
from .runtime import IPV4, ModbusRuntime, async_resolve

REDACTED = "**REDACTED**"
TO_REDACT = {"host", "hostname", "address", "addresses", "ip", "password", "ssid"}


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    """Replace the host and its addresses inside every string (error texts
    carry "connect to <host>:<port>")."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return IPV4.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, secrets) for v in value]
    return value


def _commands(rt: ModbusRuntime) -> dict[str, Any]:
    cmd = rt.commander
    return {
        "sequence": cmd.sequence,
        "stop_not_working": cmd.stop_not_working,
        "charging_without_session": cmd.charging_without_session,
        "would_do": cmd.would_do,
        "phase_select_available": cmd.phase_select_available,
        "phase_pending": cmd.phase_pending,
        "options": asdict(cmd.options),
    }


async def async_get_modbus_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    rt: ModbusRuntime = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    meter = rt.meter
    data = {
        "integration_version": str(integration.version),
        "home_assistant_version": HA_VERSION,
        "transport": "modbus",
        "time_zone": meter.tz_name,
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "station": rt.station.diagnostics(),
        "commands": _commands(rt),
        "energy": {
            **meter.diagnostics(),
            "totals": meter.totals.as_dict(),
            "last_session": meter.last_session,
            "tariff": asdict(meter.tariff),
        },
    }
    # The live peer and the addresses the host resolves to now, not only at
    # setup; any other IPv4 address in a text too.
    now = await async_resolve(hass, rt.host, rt.port)
    secrets = tuple(sorted({*rt.secrets(), *now}, key=len, reverse=True))
    return _scrub(data, secrets)
