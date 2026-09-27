"""Base entity: pushed by the charger, no polling."""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .charger import Charger, ChargerError
from .const import DOMAIN
from .logic import model_name


class GnltEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    # Available only while the charger is connected, unless a subclass knows
    # better (the connection sensor, the lifetime energy counter).
    _always_available = False

    def __init__(self, charger: Charger, key: str) -> None:
        self.charger = charger
        self._attr_translation_key = key
        self._attr_unique_id = f"{charger.identity}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger.identity)},
            name=f"GNLT {charger.identity}",
            manufacturer="GNLT",
            model=model_name(charger.identity, charger.firmware, charger.country) or charger.model,
            sw_version=charger.firmware,
            serial_number=charger.identity,
        )

    @property
    def available(self) -> bool:
        return self._always_available or self.charger.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.charger.add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


async def run_command(awaitable: Awaitable[None]) -> None:
    """Turn a charger refusal into a message the person can read."""
    try:
        await awaitable
    except ChargerError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=err.code,
            translation_placeholders={"detail": err.detail},
        ) from err
    except (ConnectionError, OSError) as err:
        # The link to the charger broke while sending.
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="not_connected", translation_placeholders={"detail": ""}
        ) from err
