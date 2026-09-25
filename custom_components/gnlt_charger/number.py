"""Charging current limit.

Goes to the charger as the ``ChargeRate`` key and applies within a running
charge in 6-8 seconds. The ceiling is the passport of the unit (chosen from
the list of versions sold, never computed from kW), the floor is 6 A - below
that AC charging does not run at all (IEC 61851).
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .entity import GnltEntity, run_command


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([CurrentLimit(entry.runtime_data)])


class CurrentLimit(GnltEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = 6
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, charger: Charger) -> None:
        super().__init__(charger, "current_limit")

    @property
    def native_max_value(self) -> float:
        return self.charger.settings.max_current_a

    @property
    def native_value(self) -> float | None:
        value = self.charger.charge_rate_a or self.charger.desired_current_a
        return None if value is None else min(value, self.charger.settings.max_current_a)

    async def async_set_native_value(self, value: float) -> None:
        await run_command(self.charger.set_current(int(round(value))))
