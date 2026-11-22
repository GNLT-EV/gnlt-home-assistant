"""Adding a station in ModBus TCP mode, and its options.

Imported only inside the ModBus steps of the integration's flow: an
import error here must not break adding OCPP chargers.

Steps: ``modbus_intro`` (what must be set on the station first) ->
``modbus_address`` (IP or host name, port; one probe) -> ``modbus_model``
(charger version; one static note for firmware 2.7) -> entry.
No Bluetooth step: the station does not advertise in ModBus mode.

The probe obeys the single-connection rule: the entered host is
resolved first and compared with the addresses of every set-up entry - a
match opens no socket at all. Otherwise one client is used (one socket at a
time; like evcc any error closes the socket and the next read opens a new
one at once), the serial is read first (up to 3 tries), then a few
registers, one per request (as evcc). The connection is closed after the
probe; the new entry opens its own, like evcc.

No reload-gap wait before the probe (a missing reply closes the socket, the
next read reconnects); registers are read one per request, so no block is
split after an exception.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
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
    TimeSelector,
)

from ..const import (
    CONF_AUTO_START,
    CONF_METER_INTERVAL,
    CONF_NIGHT_END,
    CONF_NIGHT_PRICE,
    CONF_NIGHT_START,
    CONF_PORT,
    CONF_PRESET,
    CONF_PRICE,
    CONF_RESUME,
    CONF_SCHEDULE,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_METER_INTERVAL,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_PRESET,
    DOMAIN,
    TRANSPORT_MODBUS,
)
from ..protocol import AC_PRESETS, preset_from_station, preset_key
from . import const as C
from .client import ModbusClient, ModbusError, ModbusExceptionError, ModbusTimeoutError
from .energy import DEFAULT_SOURCE, SOURCES
from .runtime import (
    CONF_ENERGY_SOURCE,
    CONF_HOST,
    async_resolve,
    entry_options,
    entry_port,
    modbus_data,
    new_client,
)
from .station import decode_serial

_LOGGER = logging.getLogger(__name__)

PRESET_KEYS = [preset_key(ph, kw) for ph, kw, _a in AC_PRESETS]



# Registers read by the probe after the serial, one request each like evcc
# status, cable, error; max, min, cable max; 91; 93; 200 (phase
# capability; an exception means "none"). All are optional - only the
# serial is required; they only prefill.
PROBE_REGISTERS = (
    C.REG_STATUS, C.REG_CABLE, C.REG_ERROR,
    C.REG_MAX_CURRENT, C.REG_MIN_CURRENT, C.REG_CABLE_MAX,
    C.REG_CURRENT_SETPOINT, C.REG_MODE, C.REG_PHASES,
)
_sleep = asyncio.sleep  # the waits of the flow (tests replace it)


@dataclass
class ProbeResult:
    serial: str
    registers: dict[int, int]
    client: ModbusClient
    ips: tuple[str, ...]


class ProbeError(Exception):
    """The probe failed; ``key`` is the form error (base)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class ProbeAbort(Exception):
    """The address belongs to an entry already set up: no socket was opened."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- the probe ------------------------------------------------------------------------


def _failure_key(hass: HomeAssistant, ips: tuple[str, ...], port: int, kind: str) -> str:
    """one failure proves nothing (connect churn alone gives refusals
    and timeouts) -> "wait 3 minutes and retry". The cause texts
    appear only after >= 3 failed attempts over >= 3 minutes."""
    failures = modbus_data(hass)["probe_failures"]
    now = time.monotonic()
    key = f"{ips[0] if ips else '?'}:{port}"
    times = [t for t in failures.get(key, []) if now - t < 3600] + [now]
    failures[key] = times
    if len(times) >= C.LINK_HINT_AFTER_ATTEMPTS and now - times[0] >= C.LINK_HINT_AFTER_S:
        if kind == C.FAIL_REFUSED:
            return "mb_refused"
        if kind in (C.FAIL_CONNECT_TIMEOUT, C.FAIL_UNREACHABLE, "connect_failed"):
            return "mb_unreachable"
        return "mb_busy"
    if kind == C.FAIL_EXCEPTION:
        return "mb_exception"  # a real answer: no need to wait
    return "mb_no_answer"


IN_USE = "address_in_use"
UNKNOWN = "mb_address_unknown"


async def _in_use(hass: HomeAssistant, host: str, ips: tuple[str, ...], port: int, exclude: str | None) -> str | None:
    """Does a set-up entry already talk to this address?

    Compared by IP, never by the host string: the live peer of every
    runtime socket, the addresses it reached before, and every configured
    host resolved now. A loaded entry whose address cannot be known at all
    (no socket, the name does not resolve) blocks the probe: it might be
    this very station."""
    data = modbus_data(hass)
    wanted = set(ips)
    for entry_id, runtime in data["entries"].items():
        if entry_id == exclude or runtime.port != port:
            continue
        known = set(runtime.addresses) | set(await async_resolve(hass, runtime.host, port))
        peer = runtime.peer()
        if peer:
            known.add(peer)
        if runtime.host == host or known & wanted:
            return IN_USE
        if not known:
            return UNKNOWN
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude or entry.entry_id in data["entries"]:
            continue
        if entry.data.get(CONF_TRANSPORT) != TRANSPORT_MODBUS or entry_port(entry) != port:
            continue
        other = str(entry.data.get(CONF_HOST))
        if other == host or set(await async_resolve(hass, other, port)) & wanted:
            return IN_USE
    return None


async def _read(client: ModbusClient, address: int, count: int) -> list[int]:
    return await client.read_holding_registers(address, count)


async def _read_serial(client: ModbusClient) -> list[int]:
    """The three serial words, one request each (38, 40, 42). An exception
    reply is final: no GNLT serial here."""
    return [(await _read(client, reg, 1))[0] for reg in C.SERIAL_GROUP.used]


async def _read_optional(client: ModbusClient, registers: tuple[int, ...]) -> dict[int, int]:
    """One request per register; an exception reply leaves that register
    unknown. Only timeouts and link errors fail the probe."""
    out: dict[int, int] = {}
    for reg in registers:
        try:
            out[reg] = (await _read(client, reg, 1))[0]
        except ModbusExceptionError:
            continue
    return out


async def async_probe(hass: HomeAssistant, host: str, port: int, *, exclude: str | None = None) -> ProbeResult:
    ips = await async_resolve(hass, host, port)
    if not ips:
        raise ProbeError("mb_cannot_resolve")
    in_use = await _in_use(hass, host, ips, port, exclude)
    if in_use == IN_USE:
        raise ProbeAbort(IN_USE)
    if in_use == UNKNOWN:
        raise ProbeError(UNKNOWN)
    data = modbus_data(hass)
    client = new_client(host, port)
    serial: str | None = None
    try:
        words: list[int] | None = None
        previous: list[int] | None = None
        stable = False
        for attempt in range(C.IDENTITY_RETRIES):
            try:
                words = await _read_serial(client)
            except ModbusTimeoutError:
                words = None  # the socket was closed; the next try reconnects (evcc)
            if words and all(words):  # zeros from a box not ready yet: read again
                break
            if words and any(words) and words == previous:
                stable = True  # the same partly zero words twice: a real 0000 word
                break
            previous = words
            if attempt < C.IDENTITY_RETRIES - 1:
                await _sleep(C.IDENTITY_RETRY_DELAY_S)
        if words is None:
            raise ModbusTimeoutError("no answer to the serial read")
        if not all(words) and not stable:
            # All zero, or zeros straddling the three single reads: not ready,
            # never another serial.
            raise ProbeError("mb_not_ready")
        serial = decode_serial(words)
        if serial is None:
            raise ProbeError("mb_not_gnlt")
        # Identity first, then the probe's registers.
        registers = await _read_optional(client, PROBE_REGISTERS)
    except ProbeError:
        await _close(hass, client, ips, serial)
        raise
    except ModbusError as err:
        await _close(hass, client, ips, serial)
        raise ProbeError(_failure_key(hass, ips, port, getattr(err, "kind", C.FAIL_OTHER))) from err
    except BaseException:
        await _close(hass, client, ips, serial)
        raise
    data["probe_failures"].pop(f"{ips[0]}:{port}", None)
    return ProbeResult(serial, registers, client, ips)


async def _close(hass: HomeAssistant, client: ModbusClient, ips: tuple[str, ...], serial: str | None) -> None:
    await client.close()


def _prefill_preset(registers: dict[int, int]) -> str | None:
    """Preset from reg 32 (station maximum) and the phase register; the person confirms."""
    max_tenths = registers.get(C.REG_MAX_CURRENT)
    if not max_tenths:
        return None
    phases = 3 if registers.get(C.REG_PHASES) in (1, 3) else 1
    return preset_from_station(int(round(max_tenths / 10)), phases)


def _preset_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=PRESET_KEYS, translation_key="preset", mode=SelectSelectorMode.LIST)
    )


def _port_selector() -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX))


# --- config flow steps (called from config_flow.GnltConfigFlow) ---------------------------


def _state(flow: Any) -> dict[str, Any]:
    state = getattr(flow, "_modbus", None)
    if state is None:
        state = {}
        flow._modbus = state
    return state


async def async_step_intro(flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
    """Prerequisites in order; nothing is sent to the station."""
    if user_input is not None:
        return await flow.async_step_modbus_address()
    return flow.async_show_form(step_id="modbus_intro", data_schema=vol.Schema({}))


async def async_step_address(flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
    state = _state(flow)
    errors: dict[str, str] = {}
    if user_input is not None:
        host = str(user_input.get(CONF_HOST) or "").strip()
        port = int(user_input.get(CONF_PORT) or C.PORT)
        if not host:
            errors[CONF_HOST] = "mb_host_empty"
        else:
            try:
                result = await async_probe(flow.hass, host, port)
            except ProbeAbort as err:
                return flow.async_abort(reason=err.reason)
            except ProbeError as err:
                errors["base"] = err.key
            else:
                unique_id = f"modbus_{result.serial}"
                if flow.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id) is not None:
                    # A second ModBus entry of this station: close the socket first.
                    await _close(flow.hass, result.client, result.ips, result.serial)
                    return flow.async_abort(reason="already_configured")
                await flow.async_set_unique_id(unique_id, raise_on_progress=False)
                # Like evcc there is no hand-over: the entry
                # connects by itself; its start-up backoff covers a box that
                # keeps a fresh reconnect silent.
                await _close(flow.hass, result.client, result.ips, result.serial)
                state.update(host=host, port=port, serial=result.serial, registers=result.registers)
                return await flow.async_step_modbus_model()
    suggested = {CONF_HOST: state.get(CONF_HOST, ""), CONF_PORT: state.get(CONF_PORT, C.PORT)}
    if user_input is not None:
        suggested = {**suggested, **user_input}
    schema = vol.Schema(
        {
            vol.Required(CONF_HOST): TextSelector(TextSelectorConfig(autocomplete="off")),
            vol.Required(CONF_PORT, default=C.PORT): _port_selector(),
        }
    )
    return flow.async_show_form(
        step_id="modbus_address",
        data_schema=flow.add_suggested_values_to_schema(schema, suggested),
        errors=errors,
    )


async def async_step_model(flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
    state = _state(flow)
    serial = state["serial"]
    errors: dict[str, str] = {}
    if user_input is not None:
        preset = user_input.get(CONF_PRESET)
        if not preset:
            errors[CONF_PRESET] = "preset_required"
        else:
            return flow.async_create_entry(
                title=f"GNLT {serial} ModBus",  # same form as the device name
                data={
                    CONF_TRANSPORT: TRANSPORT_MODBUS,
                    CONF_SERIAL: serial,
                    CONF_HOST: state[CONF_HOST],
                    CONF_PORT: state[CONF_PORT],
                    CONF_PRESET: preset,
                },
                description="modbus_created",
            )
    known = _prefill_preset(state.get("registers") or {})
    preset_marker = vol.Required(CONF_PRESET, default=known) if known else vol.Optional(CONF_PRESET)
    schema = vol.Schema(
        {
            preset_marker: _preset_selector(),
        }
    )
    return flow.async_show_form(
        step_id="modbus_model",
        data_schema=schema,
        errors=errors,
        description_placeholders={"serial": serial},
    )


# --- options -------------------------------------------------------------


class ModbusOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_modbus()

    async def async_step_modbus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry: ConfigEntry = self.config_entry
        errors: dict[str, str] = {}
        current = entry_options(entry)
        if user_input is not None:
            values = dict(user_input)
            host = str(values.pop(CONF_HOST, current.get(CONF_HOST)) or "").strip()
            port = int(values.pop(CONF_PORT, entry_port(entry)) or C.PORT)
            if not host:
                errors[CONF_HOST] = "mb_host_empty"
            if not errors:
                options = {**entry.options, **values}
                if not values.get(CONF_NIGHT_PRICE):
                    options.pop(CONF_NIGHT_PRICE, None)  # 0 = a single tariff (OCPP)
                if values.get(CONF_AUTO_START):
                    options[CONF_SCHEDULE] = False  # the two exclude each other
                if (host, port) != (str(entry.data.get(CONF_HOST)), entry_port(entry)):
                    error = await self._change_address(entry, host, port, options)
                    if error is None:
                        return self.async_create_entry(data=options)
                    errors["base"] = error
                else:
                    return self.async_create_entry(data=options)
            current = {**current, **user_input}
        return self.async_show_form(
            step_id="modbus",
            data_schema=self._schema(current),
            errors=errors,
            description_placeholders={"currency": self.hass.config.currency},
        )

    async def _change_address(self, entry: ConfigEntry, host: str, port: int, options: dict[str, Any]) -> str | None:
        """the entry, device and entities stay; the new address must
        answer with the entry's serial. Never two sockets: the same peer is
        kept on the running connection; another peer is probed only after the
        running client is closed."""
        hass = self.hass
        serial = str(entry.data[CONF_SERIAL])
        runtime = modbus_data(hass)["entries"].get(entry.entry_id)
        ips = await async_resolve(hass, host, port)
        if not ips:
            return "mb_cannot_resolve"
        if runtime is not None and port == runtime.port and set(ips) & set(runtime.addresses):
            # The same station under another name: the running connection
            # keeps checking its serial; no reload, no new socket.
            runtime.host = host
            runtime.client.host = host
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: host}, options=options)
            return None
        in_use = await _in_use(hass, host, ips, port, entry.entry_id)
        if in_use is not None:
            return in_use
        if runtime is not None:
            await runtime.station.async_stop()  # closes first: never two sockets
        try:
            try:
                result = await async_probe(hass, host, port, exclude=entry.entry_id)
            except ProbeAbort as err:
                hass.config_entries.async_schedule_reload(entry.entry_id)
                return err.reason
            except ProbeError as err:
                hass.config_entries.async_schedule_reload(entry.entry_id)  # back to the old address
                return err.key
        except BaseException:
            # Whatever went wrong after the stop: polling of the old address
            # must come back.
            hass.config_entries.async_schedule_reload(entry.entry_id)
            raise
        if result.serial != serial:
            await _close(hass, result.client, result.ips, result.serial)
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return "mb_serial_mismatch"
        await _close(hass, result.client, result.ips, result.serial)
        # One update for data and options: the update listener sees the new
        # address once and reloads once (the only reloading option).
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port}, options=options
        )
        return None

    def _schema(self, current: dict[str, Any]) -> vol.Schema:
        night = current.get(CONF_NIGHT_PRICE)
        price_selector = NumberSelector(NumberSelectorConfig(min=0, max=100, step="any", mode=NumberSelectorMode.BOX))

        source = current.get(CONF_ENERGY_SOURCE)
        fields: dict[Any, Any] = {
            vol.Required(CONF_PRESET, default=current.get(CONF_PRESET, DEFAULT_PRESET)): _preset_selector(),
            vol.Required(CONF_HOST, default=str(current.get(CONF_HOST) or "")): TextSelector(
                TextSelectorConfig(autocomplete="off")
            ),
            vol.Required(CONF_PORT, default=int(current.get(CONF_PORT) or C.PORT)): _port_selector(),
            vol.Required(CONF_AUTO_START, default=bool(current.get(CONF_AUTO_START, False))): BooleanSelector(),
            vol.Required(CONF_RESUME, default=bool(current.get(CONF_RESUME, False))): BooleanSelector(),
            vol.Required(CONF_PRICE, default=float(current.get(CONF_PRICE) or 0)): price_selector,
            vol.Required(CONF_NIGHT_PRICE, default=float(night or 0)): price_selector,
            vol.Required(CONF_NIGHT_START, default=current.get(CONF_NIGHT_START, DEFAULT_NIGHT_START)): TimeSelector(),
            vol.Required(CONF_NIGHT_END, default=current.get(CONF_NIGHT_END, DEFAULT_NIGHT_END)): TimeSelector(),
            vol.Required(
                CONF_ENERGY_SOURCE, default=source if source in SOURCES else DEFAULT_SOURCE
            ): SelectSelector(
                SelectSelectorConfig(options=list(SOURCES), translation_key="mb_energy_source", mode=SelectSelectorMode.LIST)
            ),
            # Like the OCPP option: how often the charger is read.
            vol.Required(
                CONF_METER_INTERVAL, default=current.get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL)
            ): NumberSelector(NumberSelectorConfig(min=10, max=60, step=5, mode=NumberSelectorMode.SLIDER)),
        }
        return vol.Schema(fields)
