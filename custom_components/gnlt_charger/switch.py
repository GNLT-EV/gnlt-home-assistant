"""Charging on/off and the owner's settings of the charger."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GnltConfigEntry
from .charger import Charger
from .const import CONF_AUTO_START, CONF_RESUME, CONF_SCHEDULE, CONF_SCHEDULE_STOP
from .entity import GnltEntity, run_command


async def async_setup_entry(
    hass: HomeAssistant, entry: GnltConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    charger = entry.runtime_data
    async_add_entities(
        [
            ChargingSwitch(charger),
            OptionSwitch(charger, entry, CONF_AUTO_START),
            OptionSwitch(charger, entry, CONF_RESUME),
            OptionSwitch(charger, entry, CONF_SCHEDULE),
            OptionSwitch(charger, entry, CONF_SCHEDULE_STOP),
            PlugAndChargeSwitch(charger),
        ]
    )


class ChargingSwitch(GnltEntity, SwitchEntity):
    """Start = RemoteStartTransaction, stop = RemoteStopTransaction.

    The switch shows what the charger reports, not what was asked: for power
    equipment "accepted" is not yet "done".
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, charger: Charger) -> None:
        super().__init__(charger, "charging")

    @property
    def is_on(self) -> bool:
        # An empty session (the car did not take the charge) stays open on the
        # charger until the cable is re-plugged, but nothing is charging: "on"
        # would read as "it is charging".
        c = self.charger
        return c.status == "Charging" or (c.tx_id is not None and not c.empty_session)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await run_command(self.charger.start())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await run_command(self.charger.stop())


class OptionSwitch(GnltEntity, SwitchEntity):
    """A setting kept in the config entry, so it survives restarts.

    * ``auto_start`` - Home Assistant starts the charge when the cable is
      connected, with an ordinary OCPP command, so the charge is visible and
      controllable (unlike the charger's own plug & charge on older units);
    * ``resume_after_power_loss`` - continue a charge that a power cut
      interrupted. Off by default and never switched on for the person: the
      car may be gone. Works from firmware 2.8; older units continue by
      themselves, and a switch that does nothing must not be offered.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, charger: Charger, entry: GnltConfigEntry, option: str) -> None:
        super().__init__(charger, option)
        if option in (CONF_SCHEDULE, CONF_SCHEDULE_STOP):
            # The schedule is used every day, not set once: it belongs to
            # "Controls" on the device page, next to the charging switch.
            self._attr_entity_category = None
        self._entry = entry
        self._option = option
        self._always_available = True

    @property
    def available(self) -> bool:
        if self._option == CONF_RESUME:
            return self.charger.resume_supported
        return True

    @property
    def is_on(self) -> bool:
        return bool({**self._entry.data, **self._entry.options}.get(self._option, False))

    async def _set(self, value: bool) -> None:
        options = {**self._entry.options, self._option: value}
        # "Start when the cable is connected" and the schedule exclude each other,
        # as on the platform: with both on, the cable would start the charge at
        # noon and the night window would mean nothing.
        if value and self._option == CONF_AUTO_START:
            options[CONF_SCHEDULE] = False
        if value and self._option == CONF_SCHEDULE:
            options[CONF_AUTO_START] = False
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class PlugAndChargeSwitch(GnltEntity, SwitchEntity):
    """The charger's own "plug & charge" (key ``FreeCharging``).

    Exists over OCPP only on units that report the key (firmware 2.9 and
    newer); elsewhere it is changed on the charger or during Bluetooth setup.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, charger: Charger) -> None:
        super().__init__(charger, "plug_and_charge")

    @property
    def available(self) -> bool:
        return self.charger.connected and self.charger.plug_and_charge_supported

    @property
    def is_on(self) -> bool | None:
        return self.charger.plug_and_charge

    async def async_turn_on(self, **kwargs: Any) -> None:
        await run_command(self.charger.set_plug_and_charge(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await run_command(self.charger.set_plug_and_charge(False))
