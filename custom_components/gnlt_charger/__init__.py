"""GNLT EV Charger - GNLT EVE / EVB chargers in Home Assistant over OCPP 1.6J.

One config entry = one charger. All chargers share one OCPP server (port 9000
by default), started with the first entry and stopped with the last one.

A charger that connects without an entry is not turned away: it gets an
answer to everything it sends and shows up in "Discovered", so a charger set up
by hand or with another app can be added with one click.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store

from . import notifications
from .charger import Charger, ChargerSettings
from .const import (
    CONF_AUTO_START,
    CONF_IDENTITY,
    CONF_KIND,
    CONF_METER_INTERVAL,
    CONF_NIGHT_END,
    CONF_NIGHT_PRICE,
    CONF_NIGHT_START,
    CONF_PORT,
    CONF_PRESET,
    CONF_PRICE,
    CONF_RESUME,
    CONF_SCHEDULE,
    CONF_SCHEDULE_DAYS,
    CONF_SCHEDULE_END,
    CONF_SCHEDULE_START,
    CONF_SCHEDULE_STOP,
    CONF_TRANSPORT,
    DEFAULT_METER_INTERVAL,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_PORT,
    DEFAULT_PRESET,
    DEFAULT_SCHEDULE_END,
    DEFAULT_SCHEDULE_START,
    DOMAIN,
    KIND_SITE,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TRANSPORT_MODBUS,
)
from .logic import Schedule, Tariff, model_name
from .protocol import preset_max_current, preset_phases
from .server import OcppServer

_LOGGER = logging.getLogger(__name__)


type GnltConfigEntry = ConfigEntry[Charger]


@dataclass
class DomainData:
    store: Store[dict[str, Any]]
    stored: dict[str, Any]
    server: OcppServer | None = None
    chargers: dict[str, Charger] = field(default_factory=dict)
    entries: int = 0


def is_modbus(entry: ConfigEntry) -> bool:
    """A ModBus TCP entry (modbus/). Its code is imported only for such
    entries: an import error there must not break OCPP chargers."""
    # getattr: the OCPP unload path never read entry.data, and must not start now.
    data = getattr(entry, "data", None) or {}
    return data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS


def is_site(entry: ConfigEntry) -> bool:
    """A GNLT site entry (site/): DLM + PV over several ModBus entries; no station."""
    data = getattr(entry, "data", None) or {}
    return data.get(CONF_KIND) == KIND_SITE


def minutes(value: Any, default: str) -> int:
    """"23:00" or "23:00:00" -> 1380."""
    text = str(value or default)
    try:
        hh, mm = (int(x) for x in text.split(":")[:2])
    except ValueError:
        hh, mm = (int(x) for x in default.split(":"))
    return (hh % 24) * 60 + mm % 60


def settings_from_entry(entry: ConfigEntry) -> ChargerSettings:
    opts = {**entry.data, **entry.options}
    preset = str(opts.get(CONF_PRESET, DEFAULT_PRESET))
    night_price = opts.get(CONF_NIGHT_PRICE)
    return ChargerSettings(
        schedule=Schedule(
            enabled=bool(opts.get(CONF_SCHEDULE, False)),
            start_min=minutes(opts.get(CONF_SCHEDULE_START), DEFAULT_SCHEDULE_START),
            end_min=minutes(opts.get(CONF_SCHEDULE_END), DEFAULT_SCHEDULE_END),
            days=str(opts.get(CONF_SCHEDULE_DAYS, "all")),
            stop_at_end=bool(opts.get(CONF_SCHEDULE_STOP, False)),
        ),
        tariff=Tariff(
            price=float(opts.get(CONF_PRICE) or 0.0),
            night_price=None if night_price in (None, "") else float(night_price),
            night_start_min=minutes(opts.get(CONF_NIGHT_START), DEFAULT_NIGHT_START),
            night_end_min=minutes(opts.get(CONF_NIGHT_END), DEFAULT_NIGHT_END),
        ),
        max_current_a=preset_max_current(preset) or 16,
        phases=preset_phases(preset),
        auto_start=bool(opts.get(CONF_AUTO_START, False)),
        resume_after_power_loss=bool(opts.get(CONF_RESUME, False)),
        meter_interval_s=int(opts.get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL)),
    )


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    """One lock for creating and tearing down the shared data and server.

    Home Assistant sets up all entries of an integration at the same time.
    Without it, two entries both saw "no server yet" across the awaits and
    both bound the port: the second got "address in use" and retried every
    10 minutes forever. Kept
    under its own key: hass.data[DOMAIN] is dropped with the last entry."""
    return hass.data.setdefault(f"{DOMAIN}_lock", asyncio.Lock())


async def _domain_data(hass: HomeAssistant) -> DomainData:
    data: DomainData | None = hass.data.get(DOMAIN)
    if data is None:
        store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        data = DomainData(store=store, stored=(await store.async_load()) or {})
        hass.data[DOMAIN] = data
    return data


def _charger_factory(hass: HomeAssistant, data: DomainData):
    @callback
    def charger_for(identity: str) -> Charger:
        charger = data.chargers.get(identity)
        if charger is None:

            def save(state: dict[str, Any], identity: str = identity) -> None:
                # Only the charger registered now: one whose entry was removed
                # may still be online and wrote its old counters back on its
                # next message.
                if data.chargers.get(identity) is not charger:
                    return
                data.stored[identity] = state
                data.store.async_delay_save(lambda: data.stored, 5)

            charger = Charger(identity, hass.config.time_zone, data.stored.get(identity), save, hass.config.country)
            data.chargers[identity] = charger
        return charger

    return charger_for


@callback
def async_offer_discovery(hass: HomeAssistant, identity: str) -> None:
    """A charger without an entry is online: offer it in "Discovered"."""
    if any(e.unique_id == identity for e in hass.config_entries.async_entries(DOMAIN)):
        return
    if any(
        f["context"].get("unique_id") == identity and f["context"].get("source") == SOURCE_INTEGRATION_DISCOVERY
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    ):
        return
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY}, data={CONF_IDENTITY: identity}
        )
    )


def _announce_factory(hass: HomeAssistant, data: DomainData):
    @callback
    def on_connect(identity: str) -> None:
        async_offer_discovery(hass, identity)

    return on_connect


async def async_ensure_server(hass: HomeAssistant, port: int) -> DomainData:
    """Start the shared OCPP server if it is not running yet.

    The setup wizard needs it BEFORE the first entry exists: it waits for the
    freshly configured charger to connect.
    """
    async with _lock(hass):
        return await _ensure_server_locked(hass, port)


async def _ensure_server_locked(hass: HomeAssistant, port: int) -> DomainData:
    data = await _domain_data(hass)
    if data.server is None:
        server = OcppServer(port, _charger_factory(hass, data), _announce_factory(hass, data))
        await server.start()
        data.server = server
    return data


def charger_for(hass: HomeAssistant, identity: str) -> Charger | None:
    data: DomainData | None = hass.data.get(DOMAIN)
    return data.chargers.get(identity) if data else None


async def async_setup_entry(hass: HomeAssistant, entry: GnltConfigEntry) -> bool:
    if is_site(entry):
        from .site.ha import async_setup_site_entry

        return await async_setup_site_entry(hass, entry)
    if is_modbus(entry):
        from .modbus.runtime import async_setup_modbus_entry

        return await async_setup_modbus_entry(hass, entry)
    port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
    # Read before the count below: a bad value fails setup, and Home Assistant
    # never unloads an entry whose setup failed, so the count would stay up.
    identity = entry.data[CONF_IDENTITY]
    settings = settings_from_entry(entry)
    async with _lock(hass):
        try:
            data = await _ensure_server_locked(hass, port)
        except OSError as err:
            raise ConfigEntryNotReady(f"port {port} is busy: {err}") from err
        # Before the count: a stored record the charger cannot read fails setup
        # here, and a failed entry is never unloaded, so it must not be counted.
        charger = _charger_factory(hass, data)(identity)
        charger.adopt(settings)
        # Counted under the lock: an unload of the last other entry must not
        # stop the server between our start check and this count.
        data.entries += 1
    if data.server is not None and data.server.port != port:
        _LOGGER.warning(
            "charger %s is set to port %s, but the server already listens on %s",
            identity,
            port,
            data.server.port,
        )

    entry.runtime_data = charger

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_options_updated))
        entry.async_on_unload(notifications.watch(hass, charger, entry.title))
        entry.async_on_unload(charger.add_listener(_device_info_updater(hass, entry, charger)))

        async def _tick(_now: Any) -> None:
            # Schedule, and the day/month counters rolling over at local midnight
            # even when no energy flows.
            await charger.schedule_tick()
            charger.roll_totals()

        # At second 0 of every minute: the schedule is set in minutes, so the charge
        # starts and stops within a second of the window's edge (every 30 s from
        # Home Assistant's start it came up to 30 s late - blind test).
        entry.async_on_unload(async_track_time_change(hass, _tick, second=0))
    except BaseException:
        # Home Assistant never unloads an entry whose setup failed: undo the
        # count and the adopt, or the count stays up for good.
        charger.adopted = False
        async with _lock(hass):
            data.entries -= 1
        raise
    return True


def _device_info_updater(hass: HomeAssistant, entry: ConfigEntry, charger: Charger) -> Any:
    """Keep firmware and model of the device up to date. The device is created
    with whatever is known at setup; on a new install the charger has not said
    hello yet, so the firmware stayed empty until a reload. Written only when it changes."""

    @callback
    def update() -> None:
        if not charger.firmware:
            return
        registry = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        if not devices:
            return
        device = devices[0]
        model = model_name(charger.identity, charger.firmware, charger.country) or charger.model
        if device.sw_version != charger.firmware or (model and device.model != model):
            registry.async_update_device(device.id, sw_version=charger.firmware, model=model or device.model)

    return update


async def _options_updated(hass: HomeAssistant, entry: GnltConfigEntry) -> None:
    entry.runtime_data.adopt(settings_from_entry(entry))


async def async_unload_entry(hass: HomeAssistant, entry: GnltConfigEntry) -> bool:
    if is_site(entry):
        from .site.ha import async_unload_site_entry

        return await async_unload_site_entry(hass, entry)
    if is_modbus(entry):
        from .modbus.runtime import async_unload_modbus_entry

        return await async_unload_modbus_entry(hass, entry)
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not ok:
        return False
    entry.runtime_data.adopted = False
    async with _lock(hass):
        data: DomainData = hass.data[DOMAIN]
        data.entries -= 1
        if data.entries <= 0 and data.server is not None:
            # Stop listening first: its first step closes the listener. A
            # charger that connected or reconnected while we closed sockets
            # kept aiohttp's cleanup waiting 60 s + 60 s with the lock held
            # . Then close every socket still open, a copy
            # each pass: a late handshake adds itself to the dict.
            try:
                stopping = asyncio.ensure_future(data.server.stop())
                await asyncio.sleep(0)
                # Until the server has stopped: a handshake already in flight
                # when the listener closed reaches its charger only later.
                while True:
                    live = [c for c in data.chargers.values() if c.connected]
                    if not live and stopping.done():
                        break
                    for charger in live:
                        await charger.shutdown()
                    if not live:
                        await asyncio.wait({stopping}, timeout=0.5)
                await stopping
                await data.store.async_save(data.stored)
            finally:
                # Also on a failure or a cancel: a stopped server left in the
                # shared data was reused by the next setup, and no charger
                # could connect until Home Assistant restarted.
                data.server = None
                hass.data.pop(DOMAIN, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the stored counters of a removed charger."""
    if is_site(entry):
        from .site.ha import async_remove_site_entry

        await async_remove_site_entry(hass, entry)
        return
    if is_modbus(entry):
        from .modbus.runtime import async_remove_modbus_entry

        await async_remove_modbus_entry(hass, entry)
        return
    # Under the lock: without it, an entry setting up meanwhile loaded the file
    # before our save, kept the removed charger in its copy and wrote it back.
    async with _lock(hass):
        data: DomainData | None = hass.data.get(DOMAIN)
        store: Store[dict[str, Any]] = data.store if data else Store(hass, STORAGE_VERSION, STORAGE_KEY)
        stored = data.stored if data else ((await store.async_load()) or {})
        identity = entry.data.get(CONF_IDENTITY)
        stored.pop(identity, None)
        # Other entries keep the server: a charger still online is dropped and
        # its socket closed, so it comes back as a new, discovered charger.
        charger = data.chargers.pop(identity, None) if data else None
        if charger is not None:
            await charger.shutdown()
        await store.async_save(stored)
