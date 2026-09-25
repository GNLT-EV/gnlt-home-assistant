"""Yes/no states of the charger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .entity import GnltEntity


@dataclass(frozen=True, kw_only=True)
class GnltBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[Charger], bool]
    always_available: bool = False


# Warnings are shown as "Да / Нет" with their own icons, NOT with the "problem"
# device class: with it Home Assistant writes "OK" in the normal state, and
# "Машина не начала заряжаться: OK" reads like the car is fine (owner, 24.09.2026).
# They live under "Diagnostics": the device page sorts by name, and two "Нет"
# lines on top pushed power and status down. When one of them turns on, the
# person gets a notification with what to do (notifications.py).


BINARY_SENSORS: tuple[GnltBinaryDescription, ...] = (
    GnltBinaryDescription(
        key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        always_available=True,
        value=lambda c: c.connected,
    ),
    # Repeats the "Charging" switch and the status - kept for automations, but
    # hidden by default so the device page does not say the same thing thrice.
    GnltBinaryDescription(
        key="is_charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_registry_enabled_default=False,
        value=lambda c: c.status == "Charging",
    ),
    GnltBinaryDescription(
        key="cable",
        device_class=BinarySensorDeviceClass.PLUG,
        value=lambda c: c.cable_connected,
    ),
    # The charger charges, but no session number exists: a normal stop cannot
    # reach it (see the "Interrupt charging" button).
    GnltBinaryDescription(
        key="charging_without_session",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.charging_without_session,
    ),
    # A start the car did not take: the connector stayed in "charge finished".
    # Home Assistant closes such a session by itself; if the charger refuses,
    # the cable has to be unplugged and plugged in again.
    GnltBinaryDescription(
        key="empty_session",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.empty_session,
    ),
    GnltBinaryDescription(
        key="out_of_service",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.forced_inoperative_at is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    charger = entry.runtime_data
    async_add_entities(GnltBinarySensor(charger, d) for d in BINARY_SENSORS)


class GnltBinarySensor(GnltEntity, BinarySensorEntity):
    entity_description: GnltBinaryDescription

    def __init__(self, charger: Charger, description: GnltBinaryDescription) -> None:
        super().__init__(charger, description.key)
        self.entity_description = description
        self._always_available = description.always_available

    @property
    def is_on(self) -> bool:
        return self.entity_description.value(self.charger)
