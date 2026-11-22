"""Home Assistant side of a ModBus TCP entry.

Transport like evcc: a station that does not answer at start-up fails the
setup after 30 s (ConfigEntryNotReady, HA retries) instead of loading
without data; no reload gap across entries; the probe socket of the setup
is closed and the entry opens its own.

One config entry = one station: a ``ModbusClient`` (one persistent socket), a
``Station`` (polling, state model, write gate), an ``EnergyMeter`` fed through
``StationEnergy``, and a ``Commander`` (commands, sequences, automatic
features). Imported only for entries with ``transport: modbus``.

Everything ModBus keeps between entries lives in ``hass.data["gnlt_charger_modbus"]``
and never in the OCPP ``hass.data["gnlt_charger"]``: a ModBus entry
does not start, stop or count towards the shared OCPP server and never reads
or writes the OCPP store.

* ``entries``: entry_id -> ``ModbusRuntime``;
* ``stores``: serial -> the HA ``Store`` of that station, one per serial,
  kept across reloads so a pending delayed save is never lost;
* ``addresses``: entry_id -> (resolved IPs, port) of every set-up entry, for
  the duplicate checks of the flow;
* ``probe_failures``: "ip:port" -> times of failed probes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .. import settings_from_entry
from ..const import (
    CONF_METER_INTERVAL,
    CONF_PORT,
    CONF_PRESET,
    CONF_SERIAL,
    DEFAULT_METER_INTERVAL,
    DEFAULT_PRESET,
    DOMAIN,
    PLATFORMS,
)
from ..protocol import preset_max_current, preset_phases
from . import const as C
from .client import ModbusClient, ModbusError
from .commands import Commander, CommandOptions
from .energy import DEFAULT_SOURCE, SOURCES, EnergyMeter, StationEnergy, p_max_w
from .station import STORE_VERSION, Station, StationTiming, store_key

_LOGGER = logging.getLogger(__name__)

MODBUS_DATA = f"{DOMAIN}_modbus"
REDACTED = "**REDACTED**"
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

# Entry data / options of a ModBus entry (besides the shared OCPP option keys:
# preset, auto start, resume, prices, schedule).
CONF_HOST = "host"
CONF_ENERGY_SOURCE = "energy_source"


def modbus_data(hass: HomeAssistant) -> dict[str, Any]:
    data = hass.data.get(MODBUS_DATA)
    if data is None:
        data = {
            "entries": {},
            "stores": {},
            "addresses": {},
            "probe_failures": {},
        }
        hass.data[MODBUS_DATA] = data
    return data


def entry_options(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}


def entry_port(entry: ConfigEntry) -> int:
    return int(entry.data.get(CONF_PORT) or C.PORT)


def command_options(entry: ConfigEntry) -> CommandOptions:
    """Options of the command layer from the entry. Schedule, auto
    start and resume use the same keys and the same parser as OCPP."""
    opts = entry_options(entry)
    settings = settings_from_entry(entry)
    preset = str(opts.get(CONF_PRESET, DEFAULT_PRESET))
    return CommandOptions(
        phases_available=preset_phases(preset) == 3,
        auto_start=settings.auto_start,
        resume_after_power_loss=settings.resume_after_power_loss,
        schedule=settings.schedule,
    )


def poll_interval(entry: ConfigEntry) -> float:
    """The poll period like the OCPP option ``meter_interval``: default 10 s, 10-60 s."""
    try:
        value = float(entry_options(entry).get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL))
    except (TypeError, ValueError):
        value = float(DEFAULT_METER_INTERVAL)
    return min(max(value, 10.0), 60.0)


def energy_source(entry: ConfigEntry) -> str:
    source = entry_options(entry).get(CONF_ENERGY_SOURCE)
    return source if source in SOURCES else DEFAULT_SOURCE


def new_client(host: str, port: int) -> ModbusClient:
    """The client of an entry or a probe with the timing constants of const.py."""
    return ModbusClient(
        host,
        port,
        unit_id=C.UNIT_ID,
        timeout=C.REQUEST_TIMEOUT_S,
        connect_timeout=C.CONNECT_TIMEOUT_S,
        delay=C.DELAY_S,
    )


async def async_resolve(hass: HomeAssistant, host: str, port: int) -> tuple[str, ...]:
    """IP addresses of ``host`` (an IP gives itself). Empty if it does not
    resolve: setup never fails on it, the flow says so."""
    try:
        infos = await hass.loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return ()
    return tuple(sorted({str(info[4][0]) for info in infos}))


# --- the runtime of one entry -----------------------------------------------------------


@dataclass
class ModbusRuntime:
    """Everything one ModBus entry runs; ``entry.runtime_data`` of the entry."""

    hass: HomeAssistant
    entry: ConfigEntry
    serial: str
    host: str
    port: int
    addresses: tuple[str, ...]
    client: ModbusClient
    station: Station
    meter: EnergyMeter
    energy: StationEnergy
    commander: Commander
    _listeners: list[Callable[[], None]] = field(default_factory=list)
    _unsubscribe: list[Callable[[], Any]] = field(default_factory=list)
    # Site membership (site/coordinator.py): while the station is a member of a
    # GNLT site its own automatic features are filtered out of the options, and
    # the site adds its entities through the platforms' add callbacks.
    site_options_filter: Callable[[CommandOptions], CommandOptions] | None = None
    platform_adders: dict[str, Callable[..., Any]] = field(default_factory=dict)

    _connection: int = -1

    def __post_init__(self) -> None:
        self._unsubscribe = [
            self.station.subscribe(self._on_snapshot),
            self.commander.subscribe_notifications(lambda _kind, _data: self.async_update()),
        ]

    def _on_snapshot(self, _snap: Any) -> None:
        # Every new socket: remember the address it really reached, so the
        # flow compares with the live peer, not with a name resolved at
        # setup.
        if self.client.connections != self._connection:
            self._connection = self.client.connections
            peer = self.peer()
            if peer and peer not in self.addresses:
                self.addresses = (*self.addresses, peer)
            modbus_data(self.hass)["addresses"][self.entry.entry_id] = (self.addresses, self.port)
        self.async_update()

    def peer(self) -> str | None:
        """IP address of the open socket, if any. The client keeps no public
        accessor; read-only use of its stream, never a request."""
        writer = getattr(self.client, "_writer", None)
        if writer is None or writer.is_closing():
            return None
        peer = writer.get_extra_info("peername")
        return str(peer[0]) if peer else None

    def secrets(self) -> tuple[str, ...]:
        """Host name and every address of the station (for redaction)."""
        values = {self.host, *self.addresses}
        peer = self.peer()
        if peer:
            values.add(peer)
        return tuple(sorted((v for v in values if v), key=len, reverse=True))

    def redact(self, text: Any) -> Any:
        """The station's host and addresses out of a text (error texts carry them)."""
        if not isinstance(text, str):
            return text
        for secret in self.secrets():
            text = text.replace(secret, REDACTED)
        return IPV4.sub(REDACTED, text)

    @property
    def title(self) -> str:
        return self.entry.title or f"GNLT {self.serial}"

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    @callback
    def async_update(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # an entity must never break the poll
                _LOGGER.exception("%s: entity update failed", self.serial)

    def apply_options(self) -> None:
        """Options take effect without a reload, like OCPP ``adopt``."""
        opts = entry_options(self.entry)
        preset = str(opts.get(CONF_PRESET, DEFAULT_PRESET))
        self.station.preset_max_a = preset_max_current(preset)
        self.station.timing.poll_s = poll_interval(self.entry)
        self.commander.options = self.command_options()
        self.meter.set_source(energy_source(self.entry))
        self.meter.tariff = settings_from_entry(self.entry).tariff
        self.meter.p_max_w = p_max_w(preset)
        self.async_update()

    def command_options(self) -> CommandOptions:
        options = command_options(self.entry)
        if self.site_options_filter is not None:
            options = self.site_options_filter(options)
        return options

    def detach(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        self._listeners.clear()


def build_runtime(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: ModbusClient,
    addresses: tuple[str, ...],
) -> ModbusRuntime:
    data = modbus_data(hass)
    opts = entry_options(entry)
    serial = str(entry.data[CONF_SERIAL])
    preset = str(opts.get(CONF_PRESET, DEFAULT_PRESET))
    store = data["stores"].get(serial)
    if store is None:
        store = Store(hass, STORE_VERSION, store_key(serial))
        data["stores"][serial] = store
    station = Station(
        client,
        serial,
        store=store,
        preset_max_a=preset_max_current(preset),
        timing=StationTiming(poll_s=poll_interval(entry)),
    )
    meter = EnergyMeter(
        source=energy_source(entry),
        tariff=settings_from_entry(entry).tariff,
        tz_name=str(hass.config.time_zone),
        p_max_w=p_max_w(preset),
        name=serial,
    )
    energy = StationEnergy(station, meter)
    # Created after the energy link: the record state is updated before the
    # command layer sees the same poll.
    commander = Commander(station, energy=energy, options=command_options(entry), wall=dt_util.now)
    runtime = ModbusRuntime(
        hass=hass,
        entry=entry,
        serial=serial,
        host=str(entry.data[CONF_HOST]),
        port=entry_port(entry),
        addresses=addresses,
        client=client,
        station=station,
        meter=meter,
        energy=energy,
        commander=commander,
    )
    from ..site.members import configured_site, no_automatic_features

    if configured_site(hass, entry.entry_id) is not None:
        # A GNLT site lists this station: its own automatic features are off from
        # the start, before the site attaches it (nothing unfiltered runs).
        runtime.site_options_filter = no_automatic_features
        commander.options = runtime.command_options()
    return runtime


# Entities and notification kinds of removed features: no orphans after an
# update.
RETIRED_ENTITIES = (
    ("switch", "control_active"),
    ("binary_sensor", "control_problem"),
    ("sensor", "station_session_energy"),
    ("button", "reset_learned"),
)
RETIRED_NOTIFICATIONS = (
    "auto_start_skipped", "current_not_applied", "long_stall", "mode_not_applied", "pair_broken_unplug",
    "phase_chatter", "phase_not_applied", "phase_restart_not_taken", "phase_reverted", "phase_switch_failed",
    "pnc_reset_restored", "restore_dropped", "schedule_stop_skipped", "sequence_cancelled",
    "sequence_interrupted", "slow_stop", "start_not_taken", "station_fault", "stop_inconclusive",
    "stop_not_working", "write_breaker",
)


@callback
def remove_retired(hass: HomeAssistant, serial: str) -> None:
    """Remove the registry entries of retired entities and dismiss the
    notifications of retired kinds (standard registry / notification API)."""
    reg = er.async_get(hass)
    for platform, key in RETIRED_ENTITIES:
        entity_id = reg.async_get_entity_id(platform, DOMAIN, f"{serial}_mb_{key}")
        if entity_id is not None:
            _LOGGER.info("%s: removing retired entity %s", serial, entity_id)
            reg.async_remove(entity_id)
    for kind in RETIRED_NOTIFICATIONS:
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_{serial}_mb_{kind}")


async def async_setup_modbus_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = modbus_data(hass)
    serial = str(entry.data[CONF_SERIAL])
    host = str(entry.data[CONF_HOST])
    port = entry_port(entry)
    remove_retired(hass, serial)  # before anything else: also when the station does not answer yet
    addresses = await async_resolve(hass, host, port)
    # Like evcc the entry opens its own connection: the
    # setup's probe socket was closed by the flow.
    client = new_client(host, port)
    try:
        runtime = build_runtime(hass, entry, client, addresses)
    except BaseException:
        # Never leave the adopted socket to the garbage collector.
        await client.close()
        raise
    station = runtime.station
    try:
        # Store first; HA's last total_energy comes from the RestoreSensor
        # while the platforms are set up; the first poll only after both
        # .
        await station.async_load()
        # The stored site membership is reconciled with the site entries now, before
        # the first poll: a station no enabled site lists gets its
        # user's current back; a listed one is a member (restores stay limited).
        from ..site.members import configured_site

        runtime.commander.set_site_member(configured_site(hass, entry.entry_id) is not None)
        # Start-up like evcc's NewEm2Go: the first contact is retried with a
        # backoff (2 s -> 10 s) for up to 30 s; then Home Assistant retries
        # the whole setup (ConfigEntryNotReady), nothing is forwarded yet.
        try:
            await station.async_first_contact(initialize=runtime.commander.async_initialize)
            # evcc NewEm2Go: reg 89 once; a failed read fails the setup.
            await station.async_read_comm_timeout()
        except ModbusError as err:
            raise ConfigEntryNotReady(
                f"GNLT {serial}: no ModBus answer at start-up ({getattr(err, 'kind', 'error')})"
            ) from err
        entry.runtime_data = runtime
        data["entries"][entry.entry_id] = runtime
        data["addresses"][entry.entry_id] = (addresses, port)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_options_updated))

        from .notify import watch

        entry.async_on_unload(watch(hass, runtime))

        async def _tick(now: Any) -> None:
            # Day/month roll and the schedule at second 0 of every minute.
            try:
                runtime.meter.roll(now)
                await runtime.commander.async_schedule_tick(now)
            except Exception:
                _LOGGER.exception("%s: minute tick failed", serial)
            runtime.async_update()

        entry.async_on_unload(async_track_time_change(hass, _tick, second=0))

        async def _on_stop(_event: Event) -> None:
            # HA stops without unloading entries: close the socket and save
            # now, so the next start finds no orphan session.
            await runtime.commander.async_close()
            runtime.energy.close()
            await station.async_stop()

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
        # Polling starts at once; a station that stops answering later is
        # shown by the entities, polling goes on.
        await station.async_start()
    except BaseException:
        data["entries"].pop(entry.entry_id, None)
        data["addresses"].pop(entry.entry_id, None)
        runtime.detach()
        await runtime.commander.async_close()
        runtime.energy.close()
        await station.async_stop()
        raise
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    runtime: ModbusRuntime = entry.runtime_data
    if str(entry.data.get(CONF_HOST)) != runtime.host or entry_port(entry) != runtime.port:
        # A new address is a new connection: the only option that reloads.
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    runtime.apply_options()


async def async_unload_modbus_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not ok:
        return False
    data = modbus_data(hass)
    runtime: ModbusRuntime = entry.runtime_data
    runtime.detach()
    await runtime.commander.async_close()
    runtime.energy.close()
    # Closes the socket within ~1 s and awaits the Store save.
    await runtime.station.async_stop()
    data["entries"].pop(entry.entry_id, None)
    data["addresses"].pop(entry.entry_id, None)
    return True


async def async_remove_modbus_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A re-added entry starts clean: Control OFF, nothing learned."""
    data = modbus_data(hass)
    serial = str(entry.data.get(CONF_SERIAL) or "")
    if not serial:
        return
    store = data["stores"].pop(serial, None) or Store(hass, STORE_VERSION, store_key(serial))
    await store.async_remove()
    data["entries"].pop(entry.entry_id, None)
    data["addresses"].pop(entry.entry_id, None)
    from .notify import async_delete_issues

    async_delete_issues(hass, serial)


async def async_wait(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)
