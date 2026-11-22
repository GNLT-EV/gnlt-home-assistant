"""Entities of a ModBus TCP entry (table 2.7.1). Each platform file of the integration hands a ModBus entry over
to ``async_setup_platform``; OCPP entities are not touched.

* Unique ids are ``<serial>_mb_<key>``: they never collide with the OCPP
  entities of the same station (``<serial>_<key>``), so the long-term
  statistics of the two transports never mix.
* Device: identifiers ``(gnlt_charger, <serial>)`` as the OCPP entry of the
  same station. Up to Home Assistant 2026.7 a device was shared by config
  entries: then, if an OCPP entry of this serial exists, only the identifiers
  are passed, so the OCPP name, model and firmware are not overwritten
. From 2026.8 every config entry owns its own device record
  (device registry version 3): the ModBus
  entry gets its own device with the same identifiers and therefore needs a
  name and model of its own.
* Translation keys reuse the OCPP ones where the meaning is the same; new or
  different ones start with ``mb_``.
* Registers are shown as read and scaled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_AUTO_START,
    CONF_RESUME,
    CONF_SCHEDULE,
    CONF_SCHEDULE_DAYS,
    CONF_SCHEDULE_END,
    CONF_SCHEDULE_START,
    CONF_SCHEDULE_STOP,
    CONF_TRANSPORT,
    DEFAULT_SCHEDULE_END,
    DEFAULT_SCHEDULE_START,
    DOMAIN,
    TRANSPORT_MODBUS,
)
from ..logic import DAY_SCOPES, apparent_power_va, model_name, one_phase_power_fix
from ..notifications import language_of
from . import const as C
from .client import ModbusError
from .energy import SENSOR_CLASSES
from .notify import STATUS_NAMES, would_do_text
from .runtime import ModbusRuntime, entry_options
from .station import WriteRefused

# Device registry version 3 (Home Assistant 2026.8): one device record per
# config entry. Before it, entries with the same identifiers shared one device.
# Taken from the registry itself, not from the HA version.
SPLIT_DEVICES = getattr(dr, "STORAGE_VERSION_MAJOR", 1) >= 3

AVAIL_ALWAYS = "always"  # settings, totals, the link sensor
AVAIL_MEASUREMENT = "measurement"  # last value kept 60 s without a valid poll
AVAIL_CONTROL = "control"  # link connected and identity verified

STATUS_OPTIONS = list(dict.fromkeys([*C.STATUS_KEYS.values(), C.STATUS_UNKNOWN]))
ERROR_OPTIONS = list(dict.fromkeys([*C.ERROR_KEYS.values(), C.ERROR_UNKNOWN]))
PHASE_OPTIONS = ["1", "3"]
PHASE_OPTIONS_AUTO = [*PHASE_OPTIONS, "auto"]  # site member that can switch phases (evcc "auto")



# --- shared helpers ------------------------------------------------------------------


def _ocpp_entry_configured(hass: HomeAssistant, serial: str) -> bool:
    return any(
        e.unique_id == serial and e.source != SOURCE_IGNORE and e.data.get(CONF_TRANSPORT) != TRANSPORT_MODBUS
        for e in hass.config_entries.async_entries(DOMAIN)
    )


def device_info(rt: ModbusRuntime) -> DeviceInfo:
    """same identifiers as the OCPP entry of this station."""
    serial = rt.serial
    if not SPLIT_DEVICES and _ocpp_entry_configured(rt.hass, serial):
        return DeviceInfo(identifiers={(DOMAIN, serial)})
    info = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        # Not the OCPP device's name: one station in both modes shows two
        # devices with different names and entity ids (release 0.3.0).
        name=f"GNLT {serial} ModBus",
        manufacturer="GNLT",
        serial_number=serial,
    )
    model = model_name(serial, None, rt.hass.config.country)
    if model:
        info["model"] = model
    return info


def _detail(err: WriteRefused, lang: str = "en") -> str:
    p = err.placeholders
    if "min" in p and "max" in p:
        return f"{float(p['min']):.1f}–{float(p['max']):.1f}"
    status = p.get("status")
    if isinstance(status, int) and status in C.STATUS_KEYS:
        # the status name in the user's language, not the raw number
        return STATUS_NAMES[lang].get(C.STATUS_KEYS[status], str(status))
    for key in ("detail", "time", "status", "reason"):
        if p.get(key) is not None:
            return str(p[key])
    return ""


async def run_command(awaitable: Any, hass: HomeAssistant | None = None) -> Any:
    """A refusal of the command or station layer becomes a translated error;
    a broken link while sending says "not connected". ``hass``
    gives the language of the placeholder text (a status name)."""
    try:
        return await awaitable
    except WriteRefused as err:
        lang = language_of(hass.config.language) if hass is not None else "en"
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders={"detail": _detail(err, lang)},
        ) from err
    except (ModbusError, ConnectionError, OSError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="not_connected", translation_placeholders={"detail": ""}
        ) from err


def _phase_dict(values: tuple[float | None, ...]) -> dict[str, float]:
    return {ph: v for ph, v in zip(("L1", "L2", "L3"), values) if v is not None}


def _kw(watts: float | None) -> float | None:
    return None if watts is None else round(watts / 1000, 3)


def _power_attributes(rt: ModbusRuntime) -> dict[str, Any] | None:
    snap = rt.station.snapshot
    if snap.power_w is None:
        return None
    attrs: dict[str, Any] = {"calculated": False, "reported_power_kw": _kw(snap.power_w)}
    if rt.serial.startswith("10"):
        # Board B07 reports the power of one phase on several (OCPP fix,
        # 0.2.11): not verified over ModBus, so only shown next to reg 12.
        current, voltage = _phase_dict(snap.currents), _phase_dict(snap.voltages)
        attrs["ui_power_kw"] = _kw(one_phase_power_fix(float(snap.power_w), voltage, current, False, None, True))
    return attrs


def _apparent_kva(rt: ModbusRuntime) -> float | None:
    snap = rt.station.snapshot
    current = _phase_dict(snap.currents)
    if not current:
        return None
    return round(apparent_power_va(_phase_dict(snap.voltages), current) / 1000, 3)


def _price_now(rt: ModbusRuntime) -> float | None:
    meter = rt.meter
    return meter.tariff.price_at(dt_util.now()) if meter.tariff_set else None


def _next_start(rt: ModbusRuntime) -> datetime | None:
    return rt.commander.options.schedule.next_start(dt_util.now())


def _status_attributes(rt: ModbusRuntime) -> dict[str, Any]:
    snap = rt.station.snapshot
    return {
        "raw_code": snap.status,
        "waiting_for_start": snap.waiting_for_start,
        # readable, in the language of HA; the raw key for automations.
        "would_do": would_do_text(language_of(rt.hass.config.language), rt.commander.would_do),
        "would_do_key": rt.commander.would_do,
    }


def _link_errors(rt: ModbusRuntime) -> int:
    return int(rt.station.diagnostics()["link"]["errors_24h"])


def _site_member(rt: ModbusRuntime) -> Any:
    """The running GNLT site's member object of this station (site/members.py), or None.
    While it exists the manual controls act on the station's loadpoint."""
    from ..site.members import member_for

    return member_for(rt.hass, rt.entry.entry_id)


# --- base entity --------------------------------------------------------------------


class ModbusEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    availability = AVAIL_MEASUREMENT

    def __init__(self, rt: ModbusRuntime, key: str, translation_key: str | None = None) -> None:
        self.rt = rt
        self._attr_unique_id = f"{rt.serial}_mb_{key}"
        self._attr_translation_key = translation_key or key
        self._attr_device_info = device_info(rt)

    @property
    def available(self) -> bool:
        snap = self.rt.station.snapshot
        if self.availability == AVAIL_ALWAYS:
            return True
        if self.availability == AVAIL_CONTROL:
            return snap.link == C.LINK_CONNECTED and snap.identity == C.IDENTITY_OK
        return snap.measurements_available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.rt.add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


# --- sensors ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MbSensorDescription(SensorEntityDescription):
    value: Callable[[ModbusRuntime], Any] | None = None
    availability: str = AVAIL_MEASUREMENT
    three_phase_only: bool = False
    money: str | None = None  # "money": HA currency; "price": currency per kWh
    attributes: Callable[[ModbusRuntime], dict[str, Any] | None] | None = None
    energy: bool = False  # value, last_reset and attributes from the energy meter


def _energy_description(key: str, **kwargs: Any) -> MbSensorDescription:
    device_class, state_class, _has_reset = SENSOR_CLASSES[key]
    return MbSensorDescription(
        key=key,
        device_class=SensorDeviceClass(device_class) if device_class else None,
        state_class=SensorStateClass(state_class) if state_class else None,
        energy=True,
        availability=AVAIL_ALWAYS,
        **kwargs,
    )


def _snap(attr: str) -> Callable[[ModbusRuntime], Any]:
    return lambda rt: getattr(rt.station.snapshot, attr)


SENSORS: tuple[MbSensorDescription, ...] = (
    MbSensorDescription(
        key="status",
        translation_key="mb_status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        value=_snap("status_key"),
        attributes=_status_attributes,
    ),
    MbSensorDescription(
        key="error",
        translation_key="mb_error",
        device_class=SensorDeviceClass.ENUM,
        options=ERROR_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda rt: rt.station.snapshot.error_key,
        attributes=lambda rt: {"raw_code": rt.station.snapshot.error},
    ),
    MbSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value=lambda rt: _kw(rt.station.snapshot.power_w),
        attributes=_power_attributes,
    ),
    MbSensorDescription(
        key="apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfApparentPower.KILO_VOLT_AMPERE,
        suggested_display_precision=2,
        value=_apparent_kva,
    ),
    MbSensorDescription(
        key="phases_in_use",
        state_class=SensorStateClass.MEASUREMENT,
        three_phase_only=True,
        value=_snap("phases_in_use"),
    ),
    *(
        MbSensorDescription(
            key=f"current_l{i + 1}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=1,
            three_phase_only=i > 0,
            value=lambda rt, i=i: rt.station.snapshot.currents[i],
        )
        for i in range(3)
    ),
    *(
        # Regs 16/20/24 x 10 W
        MbSensorDescription(
            key=f"power_l{i + 1}",
            translation_key=f"mb_power_l{i + 1}",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.KILO_WATT,
            suggested_display_precision=2,
            three_phase_only=i > 0,
            value=lambda rt, i=i: _kw(rt.station.snapshot.phase_power_w[i]),
        )
        for i in range(3)
    ),
    *(
        MbSensorDescription(
            key=f"voltage_l{i + 1}",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=0,
            three_phase_only=i > 0,
            value=lambda rt, i=i: rt.station.snapshot.voltages[i],
        )
        for i in range(3)
    ),
    _energy_description(
        "session_energy", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2
    ),
    _energy_description(
        "total_energy", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2
    ),
    MbSensorDescription(
        key="price_now", money="price", suggested_display_precision=4, value=_price_now, availability=AVAIL_ALWAYS
    ),
    _energy_description("session_cost", money="money", suggested_display_precision=2),
    _energy_description("cost_today", money="money", suggested_display_precision=2),
    _energy_description("cost_month", money="money", suggested_display_precision=2),
    _energy_description(
        "energy_today", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2
    ),
    _energy_description(
        "energy_month", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2
    ),
    _energy_description("sessions_month"),
    _energy_description(
        "last_session", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2
    ),
    MbSensorDescription(
        key="next_start", device_class=SensorDeviceClass.TIMESTAMP, value=_next_start, availability=AVAIL_ALWAYS
    ),
    # --- ModBus only (diagnostics) ---------------------------------------------------
    MbSensorDescription(
        key="current_setpoint",
        translation_key="mb_current_setpoint",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_snap("setpoint_a"),
    ),
    *(
        MbSensorDescription(
            key=key,
            translation_key=f"mb_{key}",
            device_class=SensorDeviceClass.CURRENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value=_snap(attr),
        )
        for key, attr in (
            ("min_current", "min_current_a"),
            ("max_current", "max_current_a"),
            ("cable_max_current", "cable_max_a"),
            ("fallback_current", "fallback_current_a"),
        )
    ),
    MbSensorDescription(
        key="comm_timeout",
        translation_key="mb_comm_timeout",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=_snap("comm_timeout_s"),
    ),
    MbSensorDescription(
        # As read, also 0 after a non-zero value: no state class, so no
        # long-term statistics and no meter-reset artefacts.
        key="station_meter",
        translation_key="mb_station_meter",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=_snap("meter_kwh"),
    ),
    MbSensorDescription(
        key="charge_duration",
        translation_key="mb_charge_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_snap("duration_s"),
    ),
    MbSensorDescription(
        key="link_errors_24h",
        translation_key="mb_link_errors_24h",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        availability=AVAIL_ALWAYS,
        value=_link_errors,
    ),
    MbSensorDescription(
        key="last_link_error",
        translation_key="mb_last_link_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        availability=AVAIL_ALWAYS,
        value=lambda rt: rt.station.last_failure,
        # Attributes go to the recorder: no host name or IP address.
        attributes=lambda rt: {"text": rt.redact(rt.station.last_failure_text), "hint": rt.station.link_hint},
    ),
)


class MbSensor(ModbusEntity, SensorEntity):
    entity_description: MbSensorDescription

    def __init__(self, rt: ModbusRuntime, description: MbSensorDescription, currency: str) -> None:
        super().__init__(rt, description.key, description.translation_key)
        self.entity_description = description
        self.availability = description.availability
        if description.money == "money":
            self._attr_native_unit_of_measurement = currency
        elif description.money == "price":
            self._attr_native_unit_of_measurement = f"{currency}/kWh"
        if description.three_phase_only and not rt.commander.options.phases_available:
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> Any:
        d = self.entity_description
        if d.energy:
            return self.rt.meter.sensor_values()[d.key].value
        return d.value(self.rt) if d.value else None

    @property
    def last_reset(self) -> datetime | None:
        d = self.entity_description
        if d.energy and SENSOR_CLASSES[d.key][2]:
            return self.rt.meter.sensor_values()[d.key].last_reset
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        d = self.entity_description
        if d.energy:
            return self.rt.meter.sensor_values()[d.key].attributes
        return d.attributes(self.rt) if d.attributes else None


class MbTotalEnergySensor(MbSensor, RestoreSensor):
    """``total_energy`` restores HA's last native value (kWh) on start.

    The Store may be up to 60 s older than what HA showed (crash): the
    total never steps back below it. Read from
    HA's restore data, in the sensor's native unit - never from the state,
    which may be shown in Wh or MWh. Runs while the
    platforms are set up, before the first poll books a sample."""

    _TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is None or last.native_value is None:
            return
        factor = self._TO_KWH.get(str(last.native_unit_of_measurement or "kWh"))
        try:
            value = float(last.native_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if factor is not None:
            self.rt.meter.restore_total(value * factor)


# --- binary sensors -------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MbBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[ModbusRuntime], bool | None]
    availability: str = AVAIL_MEASUREMENT
    attributes: Callable[[ModbusRuntime], dict[str, Any] | None] | None = None


def _cable(rt: ModbusRuntime) -> bool | None:
    cable = rt.station.snapshot.cable
    return None if cable is None else cable == 1


# Warnings are "Yes / No" with no "problem" device class, as the OCPP ones
# .
BINARY_SENSORS: tuple[MbBinaryDescription, ...] = (
    MbBinaryDescription(
        key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        availability=AVAIL_ALWAYS,
        value=lambda rt: rt.station.snapshot.link == C.LINK_CONNECTED,
        attributes=lambda rt: {
            "link": rt.station.snapshot.link,
            "identity": rt.station.snapshot.identity,
            "hint": rt.station.link_hint,
        },
    ),
    MbBinaryDescription(
        key="is_charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_registry_enabled_default=False,
        value=lambda rt: rt.station.snapshot.status == C.STATUS_CHARGING,
    ),
    MbBinaryDescription(key="cable", device_class=BinarySensorDeviceClass.PLUG, value=_cable),
    MbBinaryDescription(
        key="empty_session",
        entity_category=EntityCategory.DIAGNOSTIC,  # OCPP parity
        availability=AVAIL_ALWAYS,
        value=lambda rt: "empty_session" in rt.station.snapshot.problems,
    ),
    MbBinaryDescription(
        key="charging_without_session",
        entity_category=EntityCategory.DIAGNOSTIC,
        availability=AVAIL_ALWAYS,
        value=lambda rt: rt.commander.charging_without_session,
    ),
    MbBinaryDescription(
        key="stop_not_working",
        translation_key="mb_stop_not_working",
        entity_category=EntityCategory.DIAGNOSTIC,
        availability=AVAIL_ALWAYS,
        value=lambda rt: rt.commander.stop_not_working,
    ),
)


class MbBinarySensor(ModbusEntity, BinarySensorEntity):
    entity_description: MbBinaryDescription

    def __init__(self, rt: ModbusRuntime, description: MbBinaryDescription) -> None:
        super().__init__(rt, description.key, description.translation_key)
        self.entity_description = description
        self.availability = description.availability

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.rt)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attributes
        return fn(self.rt) if fn else None


# --- settings kept in the config entry (OCPP parity) -------------------------------------


class _OptionEntity(ModbusEntity):
    availability = AVAIL_ALWAYS

    def __init__(self, rt: ModbusRuntime, entry: ConfigEntry, option: str) -> None:
        super().__init__(rt, option)
        self._entry = entry
        self._option = option

    def _value(self, default: Any = None) -> Any:
        return entry_options(self._entry).get(self._option, default)

    def _store(self, value: Any) -> None:
        options = {**self._entry.options, self._option: value}
        # Auto start and the schedule exclude each other, as for OCPP.
        if value is True and self._option == CONF_AUTO_START:
            options[CONF_SCHEDULE] = False
        if value is True and self._option == CONF_SCHEDULE:
            options[CONF_AUTO_START] = False
        # Applied without a reload by the update listener.
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()


class OptionSwitch(_OptionEntity, SwitchEntity):
    def __init__(self, rt: ModbusRuntime, entry: ConfigEntry, option: str) -> None:
        super().__init__(rt, entry, option)
        if option in (CONF_AUTO_START, CONF_RESUME):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return bool(self._value(False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._store(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._store(False)


class ScheduleDays(_OptionEntity, SelectEntity):
    # The schedule is used every day: "Controls", next to the charging
    # switch, as the OCPP entities (not "Configuration").
    _attr_options = list(DAY_SCOPES)

    def __init__(self, rt: ModbusRuntime, entry: ConfigEntry) -> None:
        super().__init__(rt, entry, CONF_SCHEDULE_DAYS)

    @property
    def current_option(self) -> str:
        value = self._value("all")
        return value if value in DAY_SCOPES else "all"

    async def async_select_option(self, option: str) -> None:
        self._store(option)


class ScheduleTime(_OptionEntity, TimeEntity):
    def __init__(self, rt: ModbusRuntime, entry: ConfigEntry, option: str, default: str) -> None:
        super().__init__(rt, entry, option)
        self._default = default

    @property
    def native_value(self) -> time:
        text = str(self._value() or self._default)
        hh, mm = (int(x) for x in text.split(":")[:2])
        return time(hh % 24, mm % 60)

    async def async_set_value(self, value: time) -> None:
        self._store(value.strftime("%H:%M"))


# --- controls -----------------------------------------------------------------------------


class ChargingSwitch(ModbusEntity, SwitchEntity):
    """Start and stop like evcc (one path each); ON like the OCPP switch:
    status 4 or an open charge record that is not an empty session."""

    availability = AVAIL_CONTROL
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, rt: ModbusRuntime) -> None:
        super().__init__(rt, "charging")

    @property
    def is_on(self) -> bool:
        return self.rt.commander.charging_switch_state

    async def async_turn_on(self, **kwargs: Any) -> None:
        member = _site_member(self.rt)
        if member is not None:
            member.set_mode("now")  # site member: loadpoint mode Now (DLM still applies)
            return
        await run_command(self.rt.commander.async_start(), hass=self.hass)
        self.rt.async_update()

    async def async_turn_off(self, **kwargs: Any) -> None:
        member = _site_member(self.rt)
        if member is not None:
            member.set_mode("off")
            return
        await run_command(self.rt.commander.async_stop(), hass=self.hass)
        self.rt.async_update()


class PlugAndChargeSwitch(ModbusEntity, SwitchEntity):
    """Reg 93: ON = Plug & Charge (93 = 0), OFF = start/stop from HA (93 = 1).
    Other values (e.g. 2 on related hardware) are shown raw, state unknown."""

    availability = AVAIL_CONTROL
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, rt: ModbusRuntime) -> None:
        super().__init__(rt, "plug_and_charge")

    @property
    def is_on(self) -> bool | None:
        mode = self.rt.station.snapshot.mode
        if mode == C.MODE_PLUG_AND_CHARGE:
            return True
        if mode == C.MODE_COMMAND:
            return False
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"raw_value": self.rt.station.snapshot.mode, "stored_choice": self.rt.station.desired.get("mode")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        if _site_member(self.rt) is not None:
            # A GNLT site member: with Plug & Charge the station starts by itself
            # and refuses a stop, so the site could no longer limit it.
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="site_member_pnc", translation_placeholders={"detail": ""}
            )
        await run_command(self.rt.commander.async_set_plug_and_charge(True), hass=self.hass)
        self.rt.async_update()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await run_command(self.rt.commander.async_set_plug_and_charge(False), hass=self.hass)
        self.rt.async_update()


class CurrentLimit(ModbusEntity, NumberEntity):
    """Reg 91 in 0.1 A steps within [reg 34, min(reg 32, reg 36, preset)]."""

    availability = AVAIL_CONTROL
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX

    def __init__(self, rt: ModbusRuntime) -> None:
        super().__init__(rt, "current_limit")

    @property
    def native_min_value(self) -> float:
        return self.rt.station.current_range_tenths()[0] / 10

    @property
    def native_max_value(self) -> float:
        return self.rt.station.current_range_tenths()[1] / 10

    @property
    def native_value(self) -> float | None:
        member = _site_member(self.rt)
        if member is not None:
            return member.max_current  # site member: the loadpoint's max current
        desired = self.rt.station.desired.get("current")
        if isinstance(desired, int):
            return desired / 10
        return self.rt.station.snapshot.setpoint_a

    async def async_set_native_value(self, value: float) -> None:
        member = _site_member(self.rt)
        if member is not None:
            member.set_max_current(round(value, 1))
            return
        await run_command(self.rt.commander.async_set_current(round(value, 1)), hass=self.hass)
        self.rt.async_update()


class PhaseSelect(ModbusEntity, SelectEntity):
    """Reg 200: state = the user's choice; always created,
    unavailable where it does not apply (1-phase preset, no capability)."""

    availability = AVAIL_CONTROL
    _attr_options = PHASE_OPTIONS

    def __init__(self, rt: ModbusRuntime) -> None:
        super().__init__(rt, "phases", "mb_phases")

    @property
    def available(self) -> bool:
        return super().available and self.rt.commander.phase_select_available

    @property
    def options(self) -> list[str]:
        member = _site_member(self.rt)
        return PHASE_OPTIONS_AUTO if member is not None and member.can_auto else PHASE_OPTIONS

    @property
    def current_option(self) -> str | None:
        member = _site_member(self.rt)
        if member is not None and member.phase_option is not None:
            return member.phase_option  # site member: the loadpoint's configured phases
        desired = self.rt.station.desired.get("phases")
        if desired in (1, 3):
            return str(desired)
        station = self.rt.station.snapshot.phases
        return str(station) if station in (1, 3) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.rt.station
        return {
            "station_phases": st.snapshot.phases,
            "pending": self.rt.commander.phase_pending,
        }

    async def async_select_option(self, option: str) -> None:
        member = _site_member(self.rt)
        if member is not None:
            await run_command(member.async_select_phases(option), hass=self.hass)
            self.rt.async_update()
            return
        await run_command(self.rt.commander.async_set_phases(int(option)), hass=self.hass)
        self.rt.async_update()


class RefreshButton(ModbusEntity, ButtonEntity):
    """An immediate poll on the existing connection, at most every 5 s."""

    availability = AVAIL_ALWAYS  # no logbook "Pressed" on every reconnect (OCPP parity)
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, rt: ModbusRuntime) -> None:
        super().__init__(rt, "refresh")

    async def async_press(self) -> None:
        await self.rt.station.async_refresh()


class PncOffAndStopButton(ModbusEntity, ButtonEntity):
    """93:= 1, read back, then the stop sequence: a stop is
    impossible while Plug & Charge is on. Available only while 93 = 0."""

    availability = AVAIL_CONTROL

    def __init__(self, rt: ModbusRuntime) -> None:
        super().__init__(rt, "pnc_off_and_stop", "mb_pnc_off_and_stop")

    @property
    def available(self) -> bool:
        return super().available and self.rt.station.snapshot.mode == C.MODE_PLUG_AND_CHARGE

    async def async_press(self) -> None:
        await run_command(self.rt.commander.async_pnc_off_and_stop(), hass=self.hass)
        self.rt.async_update()


# --- platform setup ------------------------------------------------------------------------


def build_entities(hass: HomeAssistant, entry: ConfigEntry, platform: str) -> list[Entity]:
    rt: ModbusRuntime = entry.runtime_data
    if platform == "sensor":
        currency = hass.config.currency
        return [
            (MbTotalEnergySensor if d.key == "total_energy" else MbSensor)(rt, d, currency) for d in SENSORS
        ]
    if platform == "binary_sensor":
        return [MbBinarySensor(rt, d) for d in BINARY_SENSORS]
    if platform == "switch":
        return [
            ChargingSwitch(rt),
            OptionSwitch(rt, entry, CONF_AUTO_START),
            OptionSwitch(rt, entry, CONF_RESUME),
            OptionSwitch(rt, entry, CONF_SCHEDULE),
            OptionSwitch(rt, entry, CONF_SCHEDULE_STOP),
            PlugAndChargeSwitch(rt),
        ]
    if platform == "number":
        return [CurrentLimit(rt)]
    if platform == "select":
        return [PhaseSelect(rt), ScheduleDays(rt, entry)]
    if platform == "time":
        return [
            ScheduleTime(rt, entry, CONF_SCHEDULE_START, DEFAULT_SCHEDULE_START),
            ScheduleTime(rt, entry, CONF_SCHEDULE_END, DEFAULT_SCHEDULE_END),
        ]
    if platform == "button":
        return [RefreshButton(rt), PncOffAndStopButton(rt)]
    return []


@callback
def async_setup_platform(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Any, platform: str) -> None:
    rt: ModbusRuntime = entry.runtime_data
    rt.platform_adders[platform] = async_add_entities  # a GNLT site adds its entities later
    entities = build_entities(hass, entry, platform)
    if platform == "select":
        from ..site.entities import member_entities

        entities += member_entities(hass, rt)
    async_add_entities(entities)
