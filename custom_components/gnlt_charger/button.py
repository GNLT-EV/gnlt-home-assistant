"""One-shot actions."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .entity import GnltEntity, run_command


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    charger = entry.runtime_data
    async_add_entities([RestartButton(charger), RefreshButton(charger), ForceStopButton(charger)])


class RestartButton(GnltEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    # Stays available while the charger is offline: Home Assistant writes every
    # "unavailable -> available" flip of a button to the logbook as "Pressed" -
    # six false "Restart - Pressed" on the partner's bench, 25.09.2026, one per
    # reconnect. A press without a connection says "not connected".
    _always_available = True

    def __init__(self, charger: Charger) -> None:
        super().__init__(charger, "restart")

    async def async_press(self) -> None:
        await run_command(self.charger.reset())


class RefreshButton(GnltEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _always_available = True  # same reason as RestartButton

    def __init__(self, charger: Charger) -> None:
        super().__init__(charger, "refresh")

    async def async_press(self) -> None:
        await run_command(self.charger.refresh())


class ForceStopButton(GnltEntity, ButtonEntity):
    """For a charge without a session number only - a deliberate action.

    First the polite stop by every number the charger may be using; only if it
    refuses them all, the connector is taken out of service.
    """

    # Available all the time, like RestartButton: it used to appear only while
    # the charger charged by itself, and every "unavailable -> available" flip
    # was written to the logbook as "Pressed" (partner's bench, 25.09.2026,
    # 19:38:51). A press with nothing to interrupt says so and sends nothing.
    _always_available = True

    def __init__(self, charger: Charger) -> None:
        super().__init__(charger, "force_stop")

    async def async_press(self) -> None:
        await run_command(self.charger.force_stop())
