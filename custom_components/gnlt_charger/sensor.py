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
    UnitOfApparentPower,
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
from .logic import apparent_power_va
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


def _start_of(c: Charger, what: str) -> datetime | None:
    """Start of the period the money counters belong to."""
    return c.totals.start_of(what, c.tz_name, announced_only=False)


def _cycle_start(c: Charger, what: str) -> datetime | None:
    """Start of the period the energy counters belong to - only for a period
    counted from zero by this version (logic.Totals.day_from_zero)."""
    return c.totals.start_of(what, c.tz_name, announced_only=True)


def _last_session(c: Charger) -> dict[str, Any] | None:
    s = c.last_session
    if not s:
        return None
    return {
        "started": s.get("started"),
        "finished": s.get("finished"),
        "minutes": s.get("minutes"),
        "cost": s.get("cost"),
        "approximate": s.get("approximate", False),
        "reason": s.get("reason"),
    }


def _power_attributes(c: Charger) -> dict[str, Any] | None:
    if c.power_reported_w is None:
        return None
    # Shown power is calculated (U x I): this firmware reports the power of one
    # phase on three phases. The charger's own figure stays visible.
    return {"calculated": "U x I", "reported_power_kw": round(c.power_reported_w / 1000, 3)}


def _apparent_kva(c: Charger) -> float | None:
    if not c.current:
        return None
    return round(apparent_power_va(c.voltage, c.current) / 1000, 3)


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
        attributes=_power_attributes,
    ),
    GnltSensorDescription(
        # Sum of U x I of the phases - apparent power, calculated. Next to the
        # charger's active power: a charger that reports one phase of three, or
        # a reading we parse wrongly, is seen at once.
        key="apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        suggested_display_precision=2,
        value=_apparent_kva,
    ),
    GnltSensorDescription(
        # How many phases carry the charge (current above 1 A); unknown outside
        # a charge. Shows a car or charger that charges on one phase.
        key="phases_in_use",
        state_class=SensorStateClass.MEASUREMENT,
        three_phase_only=True,
        value=lambda c: c.phases_in_use,
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
        # Starts from zero with every charge (last_reset = its start) and may
        # go down a little when the charger's final figure is below the last
        # reading: "total", not "total_increasing" - a drop of more than 10 % of
        # a total_increasing sensor is read as a new cycle and counted again.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: None if c.session_wh is None else round(c.session_wh / 1000, 3),
        last_reset=lambda c: c.session_started,
    ),
    GnltSensorDescription(
        key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        # For the energy dashboard: built from the charger's own figures
        # (logic.ChargeLedger) - grows with the readings, and goes down once by
        # the difference when a charge's final figure is below its last
        # reading (owner 04.10.2026: the final figure is what is recorded).
        # "total" without last_reset: Home Assistant subtracts such a drop in
        # its hour; total_increasing would read a drop of over 10 % (a young
        # installation) as a new meter cycle.
        state_class=SensorStateClass.TOTAL,
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
        # Same as the money of the day: "total" with the start of the day, so a
        # correction by a final figure is a small minus, not a new cycle.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: _kwh(c.totals.day_wh),
        last_reset=lambda c: _cycle_start(c, "day"),
        always_available=True,
    ),
    GnltSensorDescription(
        key="energy_month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: _kwh(c.totals.month_wh),
        last_reset=lambda c: _cycle_start(c, "month"),
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
