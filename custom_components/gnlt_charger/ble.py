"""Bluetooth link to a GNLT charger through Home Assistant's Bluetooth stack.

Works with the built-in adapter and with ESPHome Bluetooth proxies alike.

Rules carried over from the GNLT app, each learned on a live charger:

* the charger's Bluetooth is ON ONLY WHILE OCPP IS OFF on its screen
  (Settings -> Wi-Fi -> OCPP). Wi-Fi does not matter;
* the characteristics are chosen by their PROPERTIES (write + notify), not by
  UUID - the UUIDs differ between revisions of the radio module;
* frames go in 20-byte pieces 300 ms apart;
* the charger greets first (``A5 02 00 A7``) and expects ``AA 02 80 2C``;
* the serial number is checked against what the charger REPORTS - it
  advertises either its serial or ``BL602-BLE-DEV``, and a neighbour's charger
  may be closer;
* the order of provisioning matters: the DHCP command (0x17) is a START, after
  it the charger goes to Wi-Fi and switches Bluetooth off - so it is last.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .protocol import (
    CHUNK_DELAY_S,
    HELLO_ID,
    MODULE_NAME_PREFIX,
    Cmd,
    DeviceInfo,
    Reply,
    ReplyBuffer,
    StationState,
    chunk_frame,
    frame_charge_id,
    frame_close_session,
    frame_dhcp,
    frame_hello_ack,
    frame_ocpp_url,
    frame_plug_and_charge,
    frame_read_device_info,
    frame_read_state,
    frame_set_time,
    frame_wifi_password,
    frame_wifi_ssid,
    parse_device_info,
    parse_station_state,
)

_LOGGER = logging.getLogger(__name__)

REPLY_TIMEOUT_S = 6.0
DATA_SERVICE_UUID = "55e405d2-af9f-a98f-e54a-7dfe43535355"
_SERIAL_NAME = re.compile(r"^\d{12}$")


class BleError(Exception):
    """``code`` is a translation key of the config flow errors."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def looks_like_charger(name: str | None) -> bool:
    return bool(name) and (name.startswith(MODULE_NAME_PREFIX) or bool(_SERIAL_NAME.match(name)))


def discovered_chargers(hass: HomeAssistant) -> dict[str, str]:
    """address -> name of chargers currently advertising."""
    found: dict[str, str] = {}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if looks_like_charger(info.name):
            found[info.address] = info.name
    return found


class ChargerLink:
    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        self.address = address
        self._client: BleakClientWithServiceCache | None = None
        self._write: BleakGATTCharacteristic | None = None
        self._notify: BleakGATTCharacteristic | None = None
        self._with_response = True
        self._buffer = ReplyBuffer()
        self._waiters: list[tuple[int, asyncio.Future[Reply]]] = []

    async def connect(self) -> None:
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise BleError("ble_not_found")
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache, device, device.name or self.address, max_attempts=3
            )
        except Exception as err:  # noqa: BLE001 - bleak raises many types
            raise BleError("ble_connect_failed", str(err)) from err

        # The known data service first: the charger also has a second service
        # with the same properties that only echoes what is written into it,
        # and BlueZ lists that one first (stand, 27.09.2026). The platform
        # app uses the same UUID; properties remain the fallback for other
        # revisions of the radio module.
        services = sorted(self._client.services, key=lambda s: s.uuid.lower() != DATA_SERVICE_UUID)
        for service in services:
            write = next((c for c in service.characteristics if "write" in c.properties), None)
            no_ack = next((c for c in service.characteristics if "write-without-response" in c.properties), None)
            notify = next(
                (c for c in service.characteristics if "notify" in c.properties or "indicate" in c.properties), None
            )
            if (write or no_ack) and notify:
                self._write = write or no_ack
                self._with_response = write is not None
                self._notify = notify
                break
        if self._write is None or self._notify is None:
            await self.disconnect()
            raise BleError("ble_not_charger")
        await self._client.start_notify(self._notify, self._on_notify)

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        for reply in self._buffer.push(bytes(data)):
            if reply.cmd_id == HELLO_ID and reply.status == "unsupported":
                # The charger's greeting: answer it, otherwise it may decide
                # nobody is there.
                self.hass.async_create_task(self._write_frame(frame_hello_ack()))
                continue
            for i, (cmd_id, fut) in enumerate(self._waiters):
                if cmd_id == reply.cmd_id and not fut.done():
                    fut.set_result(reply)
                    del self._waiters[i]
                    break

    async def _write_frame(self, frame: bytes) -> None:
        if self._client is None or self._write is None:
            raise BleError("ble_connect_failed")
        try:
            for i, chunk in enumerate(chunk_frame(frame)):
                if i:
                    await asyncio.sleep(CHUNK_DELAY_S)
                await self._client.write_gatt_char(self._write, chunk, response=self._with_response)
        except BleakError as err:
            # The link dropped mid-write, e.g. the charger switched Bluetooth
            # off on its way to Wi-Fi.
            raise BleError("ble_connect_failed", str(err)) from err

    async def send(self, frame: bytes, cmd_id: int, timeout: float = REPLY_TIMEOUT_S) -> Reply:
        fut: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._waiters.append((cmd_id, fut))
        try:
            await self._write_frame(frame)
            reply = await asyncio.wait_for(fut, timeout)
        except TimeoutError as err:
            raise BleError("ble_no_answer", f"0x{cmd_id:02X}") from err
        finally:
            self._waiters = [(c, f) for c, f in self._waiters if f is not fut]
        if reply.status != "ok":
            raise BleError("ble_rejected", f"0x{cmd_id:02X} {reply.status}")
        return reply

    async def device_info(self) -> DeviceInfo:
        info = parse_device_info((await self.send(frame_read_device_info(), Cmd.READ_DEVICE_INFO)).payload)
        if info is None:
            raise BleError("ble_not_charger")
        return info

    async def state(self) -> StationState | None:
        try:
            return parse_station_state((await self.send(frame_read_state(), Cmd.READ_STATE)).payload)
        except BleError:
            return None

    async def close(self) -> None:
        """Always say goodbye (0x1A): while the charger thinks the link is alive
        it neither advertises nor goes back to Wi-Fi."""
        if self._client is not None and self._client.is_connected:
            try:
                await self.send(frame_close_session(), Cmd.CLOSE, timeout=2)
            except Exception:  # noqa: BLE001 - goodbye is best effort, never masks the result
                pass
        await self.disconnect()

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


async def provision(
    hass: HomeAssistant,
    address: str,
    *,
    server_url: str,
    ssid: str,
    password: str,
    plug_and_charge_off: bool,
    local_now: datetime,
    progress: Callable[[str], None] | None = None,
) -> tuple[DeviceInfo, StationState | None]:
    """Write Wi-Fi and the Home Assistant address into the charger.

    Returns what the charger said about itself. Raises :class:`BleError`.
    """
    say = progress or (lambda _s: None)
    link = ChargerLink(hass, address)
    await link.connect()
    try:
        info = await link.device_info()
        if not info.ocpp_supported:
            raise BleError("firmware_too_old")
        # Read the unit's own limits while Bluetooth is still up - after the
        # DHCP command the charger switches it off.
        state = await link.state()
        say("clock")
        try:
            await link.send(frame_set_time(local_now), Cmd.SET_TIME)
        except BleError:
            _LOGGER.info("charger %s did not take the clock over Bluetooth", info.serial)
        say("server")
        await link.send(frame_charge_id(info.serial), Cmd.OCPP_CHARGE_ID)
        await link.send(frame_ocpp_url(server_url), Cmd.OCPP_URL)
        say("wifi")
        await link.send(frame_wifi_ssid(ssid), Cmd.WIFI_SSID)
        await link.send(frame_wifi_password(password), Cmd.WIFI_PASSWORD)
        if plug_and_charge_off:
            # Second to last on purpose: its previous position cannot be read
            # back reliably, so the window where it is changed but the rest
            # failed is kept to a single command.
            await link.send(frame_plug_and_charge(False), Cmd.PLUG_AND_CHARGE)
        say("network")
        try:
            await link.send(frame_dhcp(), Cmd.WIFI_IP, timeout=4)
        except BleError as err:
            # The charger may drop Bluetooth before answering - it is already
            # on its way to Wi-Fi. Only a refusal is a failure.
            if err.code == "ble_rejected":
                raise
        return info, state
    finally:
        await link.close()
