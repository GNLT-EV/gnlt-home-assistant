"""Set-up, unload and removal of a GNLT site config entry (Home Assistant side).

A site entry (``data["kind"] == "site"``) has no station of its own: it runs
one:class:`~gnlt_charger.site.coordinator.SiteCoordinator` over its member
ModBus entries and owns the site device with its sensors. Imported only for
site entries.

Options are applied by a reload (HA standard update listener). An unload
(also the first half of a reload) leaves every member at its minimum current
and raises the repair issue ``site_stopped``; a successful set-up clears it.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from .config import CONF_MEMBER_ENTRY, CONF_MEMBERS, member_list, site_config
from .coordinator import SiteCoordinator
from .members import site_data, site_options

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SITE_PLATFORMS = ["sensor"]
ISSUE_STOPPED = "site_stopped"
MODE_SUFFIX = "_mb_loadpoint_mode"


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.site.{entry_id}"


def issue_id(entry_id: str) -> str:
    return f"{ISSUE_STOPPED}_{entry_id}"


@callback
def _remove_stale_mode_entities(hass: HomeAssistant, keep: set[str]) -> None:
    """Mode selects of stations that are no longer members of any site."""
    registry = er.async_get(hass)
    for entity in list(registry.entities.values()):
        if (
            entity.platform == DOMAIN
            and entity.unique_id.endswith(MODE_SUFFIX)
            and entity.config_entry_id not in keep
        ):
            registry.async_remove(entity.entity_id)
            site_data(hass)["mode_entities"].pop(entity.config_entry_id, None)


def _all_members(hass: HomeAssistant, exclude: str | None = None) -> set[str]:
    from .members import is_site_entry

    out: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if is_site_entry(entry) and entry.entry_id != exclude:
            out |= {m[CONF_MEMBER_ENTRY] for m in member_list(site_options(entry))}
    return out


async def async_setup_site_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    config = site_config(site_options(entry))

    @callback
    def stopped(reason: str) -> None:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry.entry_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_STOPPED,
            translation_placeholders={"name": entry.title or "GNLT site", "reason": reason},
        )

    @callback
    def member_removed(station_entry_id: str) -> None:
        """A member's station entry was deleted: drop it from the site options
        (the update listener reloads the site)."""
        opts = site_options(entry)
        members = [m for m in member_list(opts) if m[CONF_MEMBER_ENTRY] != station_entry_id]
        if len(members) != len(member_list(opts)):
            hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MEMBERS: members})

    coord = SiteCoordinator(
        hass,
        entry.entry_id,
        config,
        store=Store(hass, STORE_VERSION, store_key(entry.entry_id)),
        on_stopped=stopped,
        on_member_removed=member_removed,
    )
    await coord.async_load()
    entry.runtime_data = coord
    site_data(hass)["sites"][entry.entry_id] = coord
    _remove_stale_mode_entities(hass, _all_members(hass))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, SITE_PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_options_updated))
    except BaseException:
        site_data(hass)["sites"].pop(entry.entry_id, None)
        raise
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
    coord.start()
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_site_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coord: SiteCoordinator = entry.runtime_data
    await coord.async_stop("unloaded")
    ok = await hass.config_entries.async_unload_platforms(entry, SITE_PLATFORMS)
    site_data(hass)["sites"].pop(entry.entry_id, None)
    return ok


async def async_remove_site_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await Store(hass, STORE_VERSION, store_key(entry.entry_id)).async_remove()
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
    _remove_stale_mode_entities(hass, _all_members(hass, exclude=entry.entry_id))
