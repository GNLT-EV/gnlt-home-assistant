"""Entities of a GNLT site (Home Assistant side).

* On each member station's own device (created by the station entry, so the
  device stays one record per config entry): the select ``loadpoint_mode``
  (off / now / minpv / pv, evcc loadpoint modes), unique id
  ``<serial>_mb_loadpoint_mode``. Added when the station's select platform is
  set up while the station is listed in a site, or later through the
  platform's add callback when the site attaches the station.
* On the site device (identifiers ``(gnlt_charger, site_<entry_id>)``):
  grid current per phase as used by the DLM, headroom per phase after the
  allocation, meter state, site (grid) power, PV surplus, and one sensor per
  member with its allowed current (reason, required, booked in attributes).
  Unique ids ``site_<entry_id>_<key>``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from ..const import DOMAIN
from ..modbus.entities import AVAIL_ALWAYS, ModbusEntity
from .coordinator import MODES, SiteCoordinator, SiteMember
from .dlm import MeterState
from .members import configured_site, member_for, site_data

MODE_KEY = "loadpoint_mode"
METER_STATES = [s.value for s in MeterState]


# --- on the member station's device ------------------------------------------------------


class LoadpointModeSelect(ModbusEntity, SelectEntity):
    """evcc loadpoint mode of a member station; persisted by the site."""

    availability = AVAIL_ALWAYS
    _attr_options = MODES

    def __init__(self, rt: Any) -> None:
        super().__init__(rt, MODE_KEY, f"mb_{MODE_KEY}")

    @property
    def _member(self) -> SiteMember | None:
        return member_for(self.hass, self.rt.entry.entry_id) if self.hass else None

    @property
    def available(self) -> bool:
        return self._member is not None

    @property
    def current_option(self) -> str | None:
        member = self._member
        return member.mode if member is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        member = self._member
        if member is None or member.allocation is None:
            return None
        return {"allowed_current": member.allocation.allowed_a, "reason": member.allocation.reason.value}

    async def async_select_option(self, option: str) -> None:
        member = self._member
        if member is None:
            return
        member.set_mode(option)


def _register(hass: HomeAssistant, rt: Any, entity: LoadpointModeSelect) -> None:
    site_data(hass)["mode_entities"][rt.entry.entry_id] = (rt, entity)


def member_entities(hass: HomeAssistant, rt: Any) -> list[Entity]:
    """Select-platform setup of a station entry: the mode select if a site lists it."""
    if configured_site(hass, rt.entry.entry_id) is None:
        return []
    entity = LoadpointModeSelect(rt)
    _register(hass, rt, entity)
    # a running site takes the station over now, not at its next cycle:
    # its own automatic features are off before its polling starts
    for coord in list(site_data(hass)["sites"].values()):
        if coord.running and coord.member(rt.entry.entry_id) is not None:
            coord.attach_runtime(rt.entry.entry_id, rt)
    return [entity]


@callback
def async_ensure_member_entities(hass: HomeAssistant, rt: Any) -> None:
    """The site attached the station: add the mode select if its platform has not."""
    known = site_data(hass)["mode_entities"].get(rt.entry.entry_id)
    if known is not None and known[0] is rt:
        return
    adder = rt.platform_adders.get("select")
    if adder is None:
        return  # the select platform is not set up yet: it creates the entity itself
    entity = LoadpointModeSelect(rt)
    _register(hass, rt, entity)
    adder([entity])


# --- on the site device -------------------------------------------------------------------


def site_device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"site_{entry.entry_id}")},
        name=entry.title or "GNLT site",
        manufacturer="GNLT",
        model="DLM + PV (beta)",
    )


@dataclass(frozen=True, kw_only=True)
class SiteSensorDescription(SensorEntityDescription):
    value: Callable[[SiteCoordinator], Any]
    attributes: Callable[[SiteCoordinator], dict[str, Any] | None] | None = None


def _phase(values: Any, i: int) -> float | None:
    return None if values is None else round(float(values[i]), 2)


def _site_power(c: SiteCoordinator) -> float | None:
    return None if c.site is None else round(c.site.state().grid_power, 0)


SITE_SENSORS: tuple[SiteSensorDescription, ...] = (
    *(
        SiteSensorDescription(
            key=f"grid_current_l{i + 1}",
            translation_key=f"site_grid_current_l{i + 1}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=1,
            value=lambda c, i=i: _phase(c.reading.currents_a if c.reading is not None else None, i),
        )
        for i in range(3)
    ),
    *(
        SiteSensorDescription(
            key=f"headroom_l{i + 1}",
            translation_key=f"site_headroom_l{i + 1}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=1,
            value=lambda c, i=i: _phase(c.result.headroom_left_a if c.result is not None else None, i),
            attributes=lambda c, i=i: {
                "before_allocation": _phase(c.result.headroom_start_a if c.result is not None else None, i)
            },
        )
        for i in range(3)
    ),
    SiteSensorDescription(
        key="meter_state",
        translation_key="site_meter_state",
        device_class=SensorDeviceClass.ENUM,
        options=METER_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda c: c.result.meter_state.value if c.result is not None else None,
        attributes=lambda c: {"error": c.meter_error},
    ),
    SiteSensorDescription(
        key="site_power",
        translation_key="site_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=_site_power,
    ),
    SiteSensorDescription(
        key="pv_surplus",
        translation_key="site_pv_surplus",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda c: None if c.pv_surplus_w is None else round(c.pv_surplus_w, 0),
    ),
)


class _SiteEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coord: SiteCoordinator, entry: ConfigEntry, key: str) -> None:
        self.coord = coord
        self._attr_unique_id = f"site_{entry.entry_id}_{key}"
        self._attr_device_info = site_device(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coord.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self.coord.running


class SiteSensor(_SiteEntity, SensorEntity):
    entity_description: SiteSensorDescription

    def __init__(self, coord: SiteCoordinator, entry: ConfigEntry, description: SiteSensorDescription) -> None:
        super().__init__(coord, entry, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coord)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attributes
        return fn(self.coord) if fn else None


class MemberAllowedSensor(_SiteEntity, SensorEntity):
    """Allowed current of one member this cycle; reason and bookings as attributes."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "site_allowed_current"

    def __init__(self, coord: SiteCoordinator, entry: ConfigEntry, member: SiteMember, name: str) -> None:
        super().__init__(coord, entry, f"allowed_{member.entry_id}")
        self.member = member
        self._attr_translation_placeholders = {"station": name}

    @property
    def native_value(self) -> float | None:
        alloc = self.member.allocation
        return None if alloc is None else alloc.allowed_a

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alloc = self.member.allocation
        lp = self.member.loadpoint
        return {
            "station_entry_id": self.member.entry_id,
            "reason": None if alloc is None else alloc.reason.value,
            "required": None if alloc is None else alloc.required_a,
            "booked": None if alloc is None else list(alloc.booked_a),
            "mode": self.member.mode,
            "requested": self.member.requested,
            "offered": None if lp is None else lp.offered_current,
        }


def build_site_entities(hass: HomeAssistant, entry: ConfigEntry, platform: str) -> list[Entity]:
    coord: SiteCoordinator = entry.runtime_data
    if platform != "sensor":
        return []
    out: list[Entity] = [SiteSensor(coord, entry, d) for d in SITE_SENSORS]
    for m in coord.members:
        station = hass.config_entries.async_get_entry(m.entry_id)
        name = station.title if station is not None else m.entry_id
        out.append(MemberAllowedSensor(coord, entry, m, name))
    return out


@callback
def async_setup_site_platform(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Any, platform: str) -> None:
    async_add_entities(build_site_entities(hass, entry, platform))
