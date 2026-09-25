"""Readings of the charger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .const import OCPP_ERRORS, OCPP_STATUSES
from .entity import GnltEntity


@dataclass(frozen=True, kw_only=True)
class GnltSensorDescription(SensorEntityDescription):
    value: Callable[[Charger], Any]
    three_phase_only: bool = False
    always_available: bool = False
    # Unit is the currency of Home Assistant ("money") or currency per kWh ("price").
    money: str | None = None
    last_reset: Callable[[Charger], datetime | None] | None = None
    attributes: Callable[[Charger], dict[str, Any] | None] | None = None


def _lower(v: str | None) -> str | None:
    return v.lower() if v else None


def _kwh(wh: float | None) -> float | None:
    return None if wh is None else round(wh / 1000, 3)


def _money(c: Charger, value: float) -> float | None:
    # No tariff set - "unknown", not "0,00": zero reads as "charging is free".
    return round(value, 2) if c.tariff_set else None


def _start_of(c: Charger, what: str) -> datetime:
    local = c._local(datetime.now().astimezone())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.replace(day=1) if what == "month" else start


def _last_session(c: Charger) -> dict[str, Any] | None:
    s = c.last_session
    if not s:
        return None
    return {"started": s.get("started"), "finished": s.get("finished"), "minutes": s.get("minutes"), "cost": s.get("cost")}


SENSORS: tuple[GnltSensorDescription, ...] = (
    GnltSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=OCPP_STATUSES,
        value=lambda c: _lower(c.status),
    ),
    GnltSensorDescription(
        key="error",
        device_class=SensorDeviceClass.ENUM,
        options=OCPP_ERRORS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: _lower(c.error_code) or "noerror",
    ),
    GnltSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        # Native kW, not a "suggested" unit: a suggestion is remembered by the
        # entity registry, and an entity created by an older version would keep
        # showing "3 656,00 W". "3,66 kW" reads at a glance.
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value=lambda c: None if c.power_w is None else round(c.power_w / 1000, 3),
    ),
    *(
        GnltSensorDescription(
            key=f"current_{ph.lower()}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=1,
            three_phase_only=ph != "L1",
            value=lambda c, ph=ph: c.current.get(ph),
        )
        for ph in ("L1", "L2", "L3")
    ),
    *(
        GnltSensorDescription(
            key=f"voltage_{ph.lower()}",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=0,
            three_phase_only=ph != "L1",
            value=lambda c, ph=ph: c.voltage.get(ph),
        )
        for ph in ("L1", "L2", "L3")
    ),
    GnltSensorDescription(
        key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        # Starts from zero with every charge; Home Assistant reads a drop of a
        # total_increasing sensor as a new cycle, which is exactly that.
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: None if c.session_wh is None else round(c.session_wh / 1000, 3),
    ),
    GnltSensorDescription(
        key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        # For the energy dashboard: only grows, blinks of the charger's meter
        # are filtered out (logic.EnergyCounter).
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        always_available=True,
        value=lambda c: round(c.energy.total_wh / 1000, 3),
    ),
    # --- money -------------------------------------------------------------
    GnltSensorDescription(
        key="price_now",
        money="price",
        suggested_display_precision=4,
        value=lambda c: c.price_now() if c.tariff_set else None,
        always_available=True,
    ),
    GnltSensorDescription(
        key="session_cost",
        device_class=SensorDeviceClass.MONETARY,
        money="money",
        suggested_display_precision=2,
        value=lambda c: _money(c, c.session_cost),
        always_available=True,
    ),
    GnltSensorDescription(
        key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        money="money",
        suggested_display_precision=2,
        value=lambda c: _money(c, c.totals.day_cost),
        last_reset=lambda c: _start_of(c, "day"),
        always_available=True,
    ),
    GnltSensorDescription(
        key="cost_month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        money="money",
        suggested_display_precision=2,
        value=lambda c: _money(c, c.totals.month_cost),
        last_reset=lambda c: _start_of(c, "month"),
        always_available=True,
    ),
    # --- statistics ----------------------------------------------------------
    GnltSensorDescription(
        key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: _kwh(c.totals.day_wh),
        always_available=True,
    ),
    GnltSensorDescription(
        key="energy_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: _kwh(c.totals.month_wh),
        always_available=True,
    ),
    GnltSensorDescription(
        key="sessions_month",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda c: c.totals.month_sessions,
        always_available=True,
    ),
    GnltSensorDescription(
        key="last_session",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: (c.last_session or {}).get("energy_kwh"),
        attributes=_last_session,
        always_available=True,
    ),
    GnltSensorDescription(
        key="next_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda c: c.next_scheduled_start(),
        always_available=True,
    ),
    GnltSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.temperature,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    charger = entry.runtime_data
    currency = hass.config.currency
    async_add_entities(GnltSensor(charger, d, currency) for d in SENSORS)


class GnltSensor(GnltEntity, SensorEntity):
    entity_description: GnltSensorDescription

    def __init__(self, charger: Charger, description: GnltSensorDescription, currency: str) -> None:
        super().__init__(charger, description.key)
        self.entity_description = description
        if description.money == "money":
            self._attr_native_unit_of_measurement = currency
        elif description.money == "price":
            self._attr_native_unit_of_measurement = f"{currency}/kWh"
        self._always_available = description.always_available
        if description.three_phase_only and charger.settings.phases == 1:
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.charger)

    @property
    def last_reset(self) -> datetime | None:
        fn = self.entity_description.last_reset
        return fn(self.charger) if fn else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes:
            return self.entity_description.attributes(self.charger)
        if self.entity_description.key == "error" and self.charger.vendor_error:
            return {"vendor_error_code": self.charger.vendor_error}
        return None
