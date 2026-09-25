"""Start and end of the charging schedule window.

Kept in the config entry, so they survive restarts and can be changed from
the dashboard and by automations. An end earlier than the start means the
window goes through midnight (23:00 - 07:00), as on the GNLT platform.
"""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .const import CONF_SCHEDULE_END, CONF_SCHEDULE_START, DEFAULT_SCHEDULE_END, DEFAULT_SCHEDULE_START
from .entity import GnltEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    charger = entry.runtime_data
    async_add_entities(
        [
            ScheduleTime(charger, entry, CONF_SCHEDULE_START, DEFAULT_SCHEDULE_START),
            ScheduleTime(charger, entry, CONF_SCHEDULE_END, DEFAULT_SCHEDULE_END),
        ]
    )


class ScheduleTime(GnltEntity, TimeEntity):
    def __init__(self, charger: Charger, entry: GnltConfigEntry, option: str, default: str) -> None:
        super().__init__(charger, option)
        self._entry = entry
        self._option = option
        self._default = default
        self._always_available = True

    @property
    def native_value(self) -> time:
        text = str({**self._entry.data, **self._entry.options}.get(self._option) or self._default)
        hh, mm = (int(x) for x in text.split(":")[:2])
        return time(hh % 24, mm % 60)

    async def async_set_value(self, value: time) -> None:
        options = {**self._entry.options, self._option: value.strftime("%H:%M")}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
