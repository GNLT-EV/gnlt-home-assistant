"""Adding a GNLT charger to Home Assistant.

Three ways in:

1. **Bluetooth** (recommended): Home Assistant writes the Wi-Fi network and its
   own address into the charger - the same steps the GNLT app does. The person
   then does the ONE thing no command can do: switches OCPP on, on the charger
   screen (Settings -> Wi-Fi -> OCPP). The command for it (0x21) is in the
   vendor document but exists in no firmware.
2. **Manual**: the charger is already on the network; the wizard shows the
   address to enter in the charger.
3. **Discovered**: a charger connected to our server by itself.

The passport (phases and power) is taken from the charger over Bluetooth when
possible - the unit reports its hardware current limit - and otherwise chosen
FROM THE LIST of versions sold, never typed as a number.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
from homeassistant.util import dt as dt_util

from . import async_ensure_server, charger_for
from .ble import BleError, discovered_chargers, looks_like_charger, provision
from .const import (
    CONF_AUTO_START,
    CONF_IDENTITY,
    CONF_METER_INTERVAL,
    CONF_NIGHT_END,
    CONF_NIGHT_PRICE,
    CONF_NIGHT_START,
    CONF_PORT,
    CONF_PRESET,
    CONF_PRICE,
    CONF_RESUME,
    CONF_SCHEDULE,
    DEFAULT_METER_INTERVAL,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_PORT,
    DEFAULT_PRESET,
    DOMAIN,
    OCPP_PATH,
)
from .protocol import (
    AC_PRESETS,
    MAX_URL_BYTES,
    MAX_WIFI_PASSWORD_BYTES,
    MAX_WIFI_SSID_BYTES,
    DeviceInfo,
    StationState,
    preset_from_station,
    preset_key,
)

_LOGGER = logging.getLogger(__name__)

CONF_ADDRESS = "address"
CONF_SSID = "ssid"
CONF_PASSWORD = "password"
CONF_HOST = "host"
CONF_PNC_OFF = "plug_and_charge_off"

WAIT_CONNECT_S = 600
PRESET_KEYS = [preset_key(ph, kw) for ph, kw, _a in AC_PRESETS]


def _preset_key(known: str | None) -> vol.Marker:
    """The charger version must be chosen by the person unless the charger told
    it itself: a preselected "1 phase 7 kW - 32 A" let a buyer who just clicked
    through get a 32 A slider on a 16 A unit (blind test, 25.09.2026).
    Optional on purpose: for a required field with no default the frontend
    preselects the first option itself; the step checks the choice instead."""
    return vol.Required(CONF_PRESET, default=known) if known else vol.Optional(CONF_PRESET)


def _preset_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=PRESET_KEYS, translation_key="preset", mode=SelectSelectorMode.LIST)
    )


def _ascii_error(value: str, max_len: int, prefix: str) -> str | None:
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        return f"{prefix}_ascii"
    return f"{prefix}_long" if len(raw) > max_len else None



async def _lan_address(hass: Any) -> str:
    """The address of Home Assistant in the home network, as the charger sees it.

    First the internal URL from the Home Assistant network settings - but only
    if it is a plain IP: the charger cannot resolve ``homeassistant.local``.
    Otherwise the address of the interface Home Assistant goes out through. In
    Docker without host networking that is the container's own 172.x address,
    which the charger cannot reach (partner's bench, 25.09.2026) - the internal
    URL is the way to fix it, and the wizard text says so.
    """
    url = hass.config.internal_url
    if url:
        host = urlparse(url).hostname or ""
        try:
            ipaddress.IPv4Address(host)
            return host
        except ValueError:
            pass
    return await async_get_source_ip(hass)

class GnltConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._ble_name: str | None = None
        self._wifi: dict[str, Any] = {}
        self._port = DEFAULT_PORT
        self._identity: str | None = None
        self._info: DeviceInfo | None = None
        self._state: StationState | None = None
        self._preset: str | None = None
        self._task: asyncio.Task[Any] | None = None
        self._error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GnltOptionsFlow()

    def _server_port(self) -> int | None:
        data = self.hass.data.get(DOMAIN)
        return data.server.port if data is not None and data.server is not None else None

    # --- start --------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["pick_device", "manual"])

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """A charger in Bluetooth mode is near a Home Assistant adapter or proxy."""
        if not looks_like_charger(discovery_info.name):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._ble_name = discovery_info.name
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_wifi()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": self._ble_name or ""}
        )

    async def async_step_pick_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        found = discovered_chargers(self.hass)
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._ble_name = found.get(self._address, self._address)
            return await self.async_step_wifi()
        if not found:
            return self.async_show_form(step_id="pick_device", errors={"base": "no_devices"}, data_schema=vol.Schema({}))
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=[{"value": a, "label": f"{n} ({a})"} for a, n in found.items()],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # --- Bluetooth: Wi-Fi and server address ----------------------------------

    async def async_step_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        running_port = self._server_port()
        if user_input is not None:
            ssid = user_input[CONF_SSID].strip()
            password = user_input[CONF_PASSWORD]
            host = user_input[CONF_HOST].strip()
            port = running_port or int(user_input.get(CONF_PORT, DEFAULT_PORT))
            url = f"ws://{host}:{port}{OCPP_PATH}"
            for key, err in (
                (CONF_SSID, _ascii_error(ssid, MAX_WIFI_SSID_BYTES, "ssid")),
                (CONF_PASSWORD, _ascii_error(password, MAX_WIFI_PASSWORD_BYTES, "password")),
                (CONF_HOST, _ascii_error(url, MAX_URL_BYTES, "host")),
            ):
                if err:
                    errors[key] = err
            if not host:
                errors[CONF_HOST] = "host_empty"
            if not errors:
                try:
                    await async_ensure_server(self.hass, port)
                except OSError:
                    errors[CONF_PORT] = "port_busy"
            if not errors:
                self._port = port
                self._wifi = {
                    "ssid": ssid,
                    "password": password,
                    "server_url": url,
                    "plug_and_charge_off": bool(user_input.get(CONF_PNC_OFF, True)),
                }
                self._error = None
                return await self.async_step_provision()
        if self._error:
            errors["base"] = self._error
            self._error = None

        default_host = (user_input or {}).get(CONF_HOST) or await _lan_address(self.hass)
        schema: dict[Any, Any] = {
            vol.Required(CONF_SSID, default=(user_input or {}).get(CONF_SSID, "")): str,
            vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(CONF_HOST, default=default_host): str,
        }
        if running_port is None:
            schema[vol.Required(CONF_PORT, default=DEFAULT_PORT)] = NumberSelector(
                NumberSelectorConfig(min=1024, max=65535, mode=NumberSelectorMode.BOX)
            )
        schema[vol.Required(CONF_PNC_OFF, default=True)] = BooleanSelector()
        return self.async_show_form(
            step_id="wifi",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"name": self._ble_name or ""},
        )

    async def async_step_provision(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(self._provision())
        if not self._task.done():
            return self.async_show_progress(step_id="provision", progress_action="provision", progress_task=self._task)
        task, self._task = self._task, None
        try:
            self._info, self._state = task.result()
        except BleError as err:
            _LOGGER.warning("Bluetooth setup failed: %s", err)
            self._error = err.code if err.code in BLE_ERRORS else "ble_failed"
            return self.async_show_progress_done(next_step_id="wifi")
        self._identity = self._info.serial
        await self.async_set_unique_id(self._identity, raise_on_progress=False)
        if self._state is not None:
            self._preset = preset_from_station(self._state.max_current_a, self._state.phases)
        return self.async_show_progress_done(next_step_id="passport")

    async def _provision(self) -> tuple[DeviceInfo, StationState | None]:
        assert self._address is not None
        return await provision(
            self.hass,
            self._address,
            server_url=self._wifi["server_url"],
            ssid=self._wifi["ssid"],
            password=self._wifi["password"],
            plug_and_charge_off=self._wifi["plug_and_charge_off"],
            local_now=dt_util.now(),
        )

    async def async_step_passport(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and not user_input.get(CONF_PRESET):
            errors[CONF_PRESET] = "preset_required"
        elif user_input is not None:
            self._preset = user_input[CONF_PRESET]
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, self._identity or "")
            if existing is not None:
                # The same charger set up again (new router, new password).
                self.hass.config_entries.async_update_entry(
                    existing, data={**existing.data, CONF_PORT: self._port, CONF_PRESET: self._preset}
                )
                self.hass.config_entries.async_schedule_reload(existing.entry_id)
                return self.async_abort(reason="reprovisioned")
            return await self.async_step_enable_ocpp()
        # Detected from the charger's own report -> pre-selected; the person
        # still confirms: the passport caps the current slider.
        return self.async_show_form(
            step_id="passport",
            data_schema=vol.Schema(
                {_preset_key(self._preset): _preset_selector()}
            ),
            errors=errors,
            description_placeholders={"serial": self._identity or ""},
        )

    async def async_step_enable_ocpp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_wait_connect()
        return self.async_show_form(step_id="enable_ocpp", description_placeholders={"serial": self._identity or ""})

    async def async_step_wait_connect(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(self._wait_for_charger())
        if not self._task.done():
            return self.async_show_progress(
                step_id="wait_connect", progress_action="wait_connect", progress_task=self._task
            )
        task, self._task = self._task, None
        return self.async_show_progress_done(next_step_id="done" if task.result() else "not_connected")

    async def _wait_for_charger(self) -> bool:
        for _ in range(WAIT_CONNECT_S // 2):
            charger = charger_for(self.hass, self._identity or "")
            if charger is not None and charger.connected:
                return True
            await asyncio.sleep(2)
        return False

    async def async_step_not_connected(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_done()
        host = self._wifi.get("server_url", "")
        return self.async_show_form(
            step_id="not_connected", description_placeholders={"serial": self._identity or "", "url": host}
        )

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self._create()

    def _create(self) -> ConfigFlowResult:
        assert self._identity is not None
        return self.async_create_entry(
            title=f"GNLT {self._identity}",
            data={
                CONF_IDENTITY: self._identity,
                CONF_PORT: self._port,
                CONF_PRESET: self._preset or DEFAULT_PRESET,
            },
        )

    # --- manual ------------------------------------------------------------------

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        running_port = self._server_port()
        if user_input is not None:
            identity = user_input[CONF_IDENTITY].strip()
            if not identity.isdigit() or len(identity) != 12:
                errors[CONF_IDENTITY] = "serial_invalid"
            elif not user_input.get(CONF_PRESET):
                errors[CONF_PRESET] = "preset_required"
            else:
                await self.async_set_unique_id(identity)
                self._abort_if_unique_id_configured()
                self._identity = identity
                self._preset = user_input[CONF_PRESET]
                self._port = running_port or int(user_input.get(CONF_PORT, DEFAULT_PORT))
                try:
                    await async_ensure_server(self.hass, self._port)
                except OSError:
                    errors[CONF_PORT] = "port_busy"
                else:
                    return await self.async_step_manual_address()
        schema: dict[Any, Any] = {
            vol.Required(CONF_IDENTITY): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            _preset_key(None): _preset_selector(),
        }
        if running_port is None:
            schema[vol.Required(CONF_PORT, default=DEFAULT_PORT)] = NumberSelector(
                NumberSelectorConfig(min=1024, max=65535, mode=NumberSelectorMode.BOX)
            )
        # Keep what the person typed when the form comes back with an error.
        data_schema = self.add_suggested_values_to_schema(vol.Schema(schema), user_input or {})
        return self.async_show_form(step_id="manual", data_schema=data_schema, errors=errors)

    async def async_step_manual_address(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        ip = await _lan_address(self.hass)
        return self.async_show_form(
            step_id="manual_address",
            description_placeholders={"url": f"ws://{ip}:{self._port}{OCPP_PATH}", "serial": self._identity or ""},
        )

    # --- a charger that came by itself ----------------------------------------------

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        identity = str(discovery_info[CONF_IDENTITY])
        await self.async_set_unique_id(identity)
        self._abort_if_unique_id_configured()
        self._identity = identity
        self._port = self._server_port() or DEFAULT_PORT
        self.context["title_placeholders"] = {"name": f"GNLT {identity}"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_PRESET):
            self._preset = user_input[CONF_PRESET]
            return self._create()
        if user_input is not None:
            errors[CONF_PRESET] = "preset_required"
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema({_preset_key(None): _preset_selector()}),
            errors=errors,
            description_placeholders={"serial": self._identity or ""},
        )


BLE_ERRORS = {
    "ble_not_found",
    "ble_connect_failed",
    "ble_not_charger",
    "ble_no_answer",
    "ble_rejected",
    "firmware_too_old",
}


class GnltOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {**self.config_entry.options, **user_input}
            # 0 means "a single tariff": an optional number box lost its label in
            # the Home Assistant form, so the field is required and zero is "none".
            if not user_input.get(CONF_NIGHT_PRICE):
                data.pop(CONF_NIGHT_PRICE, None)
            if user_input.get(CONF_AUTO_START):
                data[CONF_SCHEDULE] = False  # the two exclude each other
            return self.async_create_entry(data=data)
        current = {**self.config_entry.data, **self.config_entry.options}
        night = current.get(CONF_NIGHT_PRICE)
        price_selector = NumberSelector(NumberSelectorConfig(min=0, max=100, step="any", mode=NumberSelectorMode.BOX))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRESET, default=current.get(CONF_PRESET, DEFAULT_PRESET)): _preset_selector(),
                    vol.Required(CONF_AUTO_START, default=current.get(CONF_AUTO_START, False)): BooleanSelector(),
                    vol.Required(CONF_RESUME, default=current.get(CONF_RESUME, False)): BooleanSelector(),
                    vol.Required(CONF_PRICE, default=float(current.get(CONF_PRICE) or 0)): price_selector,
                    vol.Required(CONF_NIGHT_PRICE, default=float(night or 0)): price_selector,
                    vol.Required(
                        CONF_NIGHT_START, default=current.get(CONF_NIGHT_START, DEFAULT_NIGHT_START)
                    ): TimeSelector(),
                    vol.Required(CONF_NIGHT_END, default=current.get(CONF_NIGHT_END, DEFAULT_NIGHT_END)): TimeSelector(),
                    vol.Required(
                        CONF_METER_INTERVAL, default=current.get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL)
                    ): NumberSelector(NumberSelectorConfig(min=10, max=60, step=5, mode=NumberSelectorMode.SLIDER)),
                }
            ),
            description_placeholders={"currency": self.hass.config.currency},
        )
