"""A real Home Assistant core for the ModBus entry tests.

``pytest-homeassistant-custom-component`` is not installed in the HA
container, and setting the integration up through the loader would also set
up its ``bluetooth`` dependency. So the tests build the
core the way HA's own test harness does (registries, config entries, states,
Store in a temporary config dir) and run the integration's code directly:
``async_forward_entry_setups`` / ``async_unload_platforms`` are replaced by
real ``EntityPlatform`` objects of each platform module, with the
integration's own translations loaded, so entity names, unique ids, device
registry entries and states are what HA would produce.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import tempfile
from datetime import timedelta
from types import MappingProxyType
from typing import Any

import gnlt_charger as integ
from homeassistant import bootstrap, config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform

DOMAIN = integ.DOMAIN
SERIAL = "301234567890"
COMPONENT_DIR = os.path.dirname(integ.__file__)
_LOGGER = logging.getLogger(__name__)


def translations(lang: str = "en") -> dict[str, Any]:
    path = os.path.join(COMPONENT_DIR, "strings.json" if lang == "strings" else f"translations/{lang}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _flat_entity_names(lang: str, platform: str) -> dict[str, str]:
    out = {}
    for key, value in translations(lang)["entity"].get(platform, {}).items():
        if "name" in value:
            out[f"component.{DOMAIN}.entity.{platform}.{key}.name"] = value["name"]
        for state, text in (value.get("state") or {}).items():
            out[f"component.{DOMAIN}.entity.{platform}.{key}.state.{state}"] = text
    return out


async def make_hass(config_dir: str | None = None, language: str = "en") -> HomeAssistant:
    hass = HomeAssistant(config_dir or tempfile.mkdtemp(prefix="gnlt_mb_"))
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await bootstrap.async_load_base_functionality(hass)
    await hass.config.async_set_time_zone("Europe/Warsaw")
    hass.config.currency = "PLN"
    hass.config.country = "PL"
    hass.config.language = language
    hass.data["_test_platforms"] = {}
    hass.config_entries.async_forward_entry_setups = lambda entry, platforms: _forward(hass, entry, platforms)
    hass.config_entries.async_unload_platforms = lambda entry, platforms: _unload(hass, entry, platforms)
    hass.data["_test_reloads"] = []
    hass.config_entries.async_schedule_reload = lambda entry_id: hass.data["_test_reloads"].append(entry_id)
    return hass


async def _forward(hass: HomeAssistant, entry: Any, platforms: list[str]) -> None:
    created = hass.data["_test_platforms"].setdefault(entry.entry_id, [])
    for name in platforms:
        module = importlib.import_module(f"gnlt_charger.{name}")
        platform = EntityPlatform(
            hass=hass,
            logger=_LOGGER,
            domain=name,
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        platform.config_entry = entry
        names = _flat_entity_names("en", name)
        platform.platform_translations = names
        platform.object_id_platform_translations = names
        platform.default_language_platform_translations = names
        collected: list[Any] = []
        state = {"done": False}

        def add(ents, update_before_add=False, _platform=platform, _state=state, _collected=collected):
            # Like HA's AddEntitiesCallback: later calls (a GNLT site adding the
            # mode select to a station that is set up already) add at once.
            if _state["done"]:
                hass.async_create_task(_platform.async_add_entities(list(ents)))
            else:
                _collected.extend(ents)

        await module.async_setup_entry(hass, entry, add)
        await platform.async_add_entities(collected)
        state["done"] = True
        created.append(platform)


async def _unload(hass: HomeAssistant, entry: Any, platforms: list[str]) -> bool:
    for platform in hass.data["_test_platforms"].pop(entry.entry_id, []):
        await platform.async_reset()
    return True


def add_entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any],
    options: dict[str, Any] | None = None,
    unique_id: str | None = None,
    title: str = "GNLT test",
    source: str = "user",
) -> config_entries.ConfigEntry:
    entry = config_entries.ConfigEntry(
        data=data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options=options or {},
        source=source,
        subentries_data=None,
        title=title,
        unique_id=unique_id,
        version=1,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


def modbus_entry(hass: HomeAssistant, port: int, *, serial: str = SERIAL, host: str = "127.0.0.1", **extra: Any):
    data = {
        "transport": "modbus",
        "serial": serial,
        "host": host,
        "port": port,
        "preset": "3_11",
        "profile": "A3C_2.7",
        **extra,
    }
    return add_entry(hass, data=data, unique_id=f"modbus_{serial}", title=f"GNLT {serial} ModBus")


async def setup(hass: HomeAssistant, entry: Any) -> bool:
    ok = await integ.async_setup_entry(hass, entry)
    object.__setattr__(entry, "state", config_entries.ConfigEntryState.LOADED)
    return ok


async def unload(hass: HomeAssistant, entry: Any) -> bool:
    ok = await integ.async_unload_entry(hass, entry)
    for remove in list(entry._on_unload or []):
        remove()
    entry._on_unload = None
    object.__setattr__(entry, "state", config_entries.ConfigEntryState.NOT_LOADED)
    return ok


async def stop(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


def entities(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """unique_id -> entity object of the entry."""
    out = {}
    for platform in hass.data["_test_platforms"].get(entry.entry_id, []):
        for entity in platform.entities.values():
            out[entity.unique_id] = entity
    return out


async def wait_for(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)
