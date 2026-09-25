"""Which days the schedule window works on."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .const import CONF_SCHEDULE_DAYS
from .entity import GnltEntity
from .logic import DAY_SCOPES


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ScheduleDays(entry.runtime_data, entry)])


class ScheduleDays(GnltEntity, SelectEntity):
    """Every day / weekdays / weekends. The day is the one the window STARTS on:
    Friday 23:00 - Saturday 07:00 is a weekday window."""

    _attr_options = list(DAY_SCOPES)

    def __init__(self, charger: Charger, entry: GnltConfigEntry) -> None:
        super().__init__(charger, CONF_SCHEDULE_DAYS)
        self._entry = entry
        self._always_available = True

    @property
    def current_option(self) -> str:
        value = {**self._entry.data, **self._entry.options}.get(CONF_SCHEDULE_DAYS, "all")
        return value if value in DAY_SCOPES else "all"

    async def async_select_option(self, option: str) -> None:
        options = {**self._entry.options, CONF_SCHEDULE_DAYS: option}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
