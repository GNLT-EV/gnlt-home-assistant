"""Which ModBus station entries are members of a GNLT site (Home Assistant side).

The station entities (``modbus/entities.py``) ask here whether their station
is controlled by a running site; while it is, the charging switch, the
current number and the phase select act on the station's loadpoint instead of
writing to the station (one automatic writer per station).

* ``hass.data["gnlt_charger_site"]["members"]``: station entry id -> the
  class:`~gnlt_charger.site.coordinator.SiteMember` of the running site;
* ``configured_site(hass, station_entry_id)``: the site entry that lists the
  station in its settings, loaded or not (used when the station's platforms
  are set up, to create the loadpoint mode select).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import CONF_KIND, DOMAIN, KIND_SITE
from .config import CONF_MEMBER_ENTRY, member_list

SITE_DATA = f"{DOMAIN}_site"


def site_data(hass: HomeAssistant) -> dict[str, Any]:
    data = hass.data.get(SITE_DATA)
    if data is None:
        data = {"members": {}, "sites": {}, "mode_entities": {}}
        hass.data[SITE_DATA] = data
    return data


def member_for(hass: HomeAssistant, station_entry_id: str) -> Any:
    """The running site's member object of this station, or None."""
    data = hass.data.get(SITE_DATA)
    return None if data is None else data["members"].get(station_entry_id)


def is_site_entry(entry: ConfigEntry) -> bool:
    data = getattr(entry, "data", None) or {}
    return data.get(CONF_KIND) == KIND_SITE


def site_options(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}


def no_automatic_features(options: Any) -> Any:
    """The station's own automatic writers are off while it is a site member
    (auto start, schedule, resume after power loss)."""
    return dataclasses.replace(
        options,
        auto_start=False,
        resume_after_power_loss=False,
        empty_session_stop=False,
        schedule=dataclasses.replace(options.schedule, enabled=False),
    )


def configured_site(hass: HomeAssistant, station_entry_id: str) -> ConfigEntry | None:
    """The enabled site entry that lists the station, loaded or not."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not is_site_entry(entry) or getattr(entry, "disabled_by", None) is not None:
            continue
        if any(m[CONF_MEMBER_ENTRY] == station_entry_id for m in member_list(site_options(entry))):
            return entry
    return None
