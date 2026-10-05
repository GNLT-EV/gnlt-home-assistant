"""Diagnostics download: Settings -> Devices & services -> GNLT EV Charger ->
the charger -> "Download diagnostics".

One file the owner can send to support: settings, what the charger reports,
the counters and the last OCPP frames in both directions - what a debug log
would show, without turning debug logging on and waiting for the problem
again. Wi-Fi name and password are never stored by the integration; they are
redacted here anyway in case an older entry carries them.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.loader import async_get_integration

from . import GnltConfigEntry
from .const import DOMAIN

TO_REDACT = {"password", "ssid", "address"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GnltConfigEntry) -> dict[str, Any]:
    charger = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    settings = asdict(charger.settings)
    return {
        "integration_version": str(integration.version),
        "home_assistant_version": HA_VERSION,
        "time_zone": charger.tz_name,
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "settings": settings,
        "charger": {
            "identity": charger.identity,
            "vendor": charger.vendor,
            "model": charger.model,
            "firmware": charger.firmware,
            "connected": charger.connected,
            "connected_since": _iso(charger.connected_since),
            "last_seen": _iso(charger.last_seen),
            "status": charger.status,
            "previous_status": charger.previous_status,
            "error_code": charger.error_code,
            "vendor_error": charger.vendor_error,
            "configuration": {k: ("**REDACTED**" if k in SECRET_KEYS else v) for k, v in charger.config.items()},
            "desired_current_a": charger.desired_current_a,
            "power_w": charger.power_w,
            "current_a": charger.current,
            "voltage_v": charger.voltage,
            "phases_in_use": charger.phases_in_use,
            "power_reported_w": charger.power_reported_w,
            "power_mismatch": charger.power_mismatch,
            "temperature": charger.temperature,
            "last_meter_at": _iso(charger.last_meter_at),
        },
        "session": {
            "tx_id": charger.tx_id,
            "station_tx_id": charger.station_tx_id,
            "tx_meter_start": charger.tx_meter_start,
            "tx_started_at": _iso(charger.tx_started_at),
            "session_wh": charger.session_wh,
            "empty_session": charger.empty_session,
            "charging_without_session": charger.charging_without_session,
            "last_session": charger.last_session,
        },
        "energy": charger.energy.as_dict(),
        "totals": charger.totals.as_dict(),
        "frames": [{"at": at, "dir": direction, "frame": _redact_frame(raw)} for at, direction, raw in charger.frames],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: GnltConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    return await async_get_config_entry_diagnostics(hass, entry)


# Configuration keys whose value is a secret (OCPP 1.6 security profile).
SECRET_KEYS = ("AuthorizationKey",)


def _redact_frame(raw: str) -> str:
    # RFID card numbers (idTag); the integration's own tag stays readable.
    raw = re.sub(r'("idTag":")(?!HomeAssistant")[^"]+(")', r"\1**REDACTED**\2", raw)
    for key in SECRET_KEYS:
        raw = re.sub(rf'("key":"{key}"[^}}]*?"value":")[^"]*(")', r"\1**REDACTED**\2", raw)
        raw = re.sub(rf'("{key}","value":")[^"]*(")', r"\1**REDACTED**\2", raw)
    return raw


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
