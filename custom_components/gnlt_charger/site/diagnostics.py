"""Diagnostics of a GNLT site entry: settings, last DLM result, members (no addresses)."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import SiteCoordinator


def _alloc(a: Any) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "allowed_a": a.allowed_a,
        "reason": a.reason.value,
        "required_a": a.required_a,
        "booked_a": list(a.booked_a),
        "headroom_a": list(a.headroom_a),
    }


async def async_get_site_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coord: SiteCoordinator = entry.runtime_data
    res = coord.result
    config = dataclasses.asdict(coord.config)
    return {
        "config": config,
        "running": coord.running,
        "stopped_reason": coord.stopped_reason,
        "cycles": coord.cycles,
        "meter_error": coord.meter_error,
        "grid_reading": None if coord.reading is None else dataclasses.asdict(coord.reading),
        "result": None
        if res is None
        else {
            "meter_state": res.meter_state.value,
            "headroom_start_a": list(res.headroom_start_a),
            "headroom_left_a": list(res.headroom_left_a),
            "power_left_w": res.power_left_w,
            "overload_a": list(res.overload_a),
        },
        "members": [
            {
                "entry_id": m.entry_id,
                "serial": m.runtime.serial if m.runtime is not None else None,
                "attached": m.runtime is not None,
                "settings": dict(m.settings),
                "capabilities": sorted(c.__name__ for c in m.adapter.capabilities) if m.adapter else None,
                "requested_a": m.requested,
                "allocation": _alloc(m.allocation),
                "last_site_error": m.adapter.last_error if m.adapter else None,
                "offered_a": m.loadpoint.offered_current if m.loadpoint else None,
                "loadpoint_enabled": m.loadpoint.enabled if m.loadpoint else None,
            }
            for m in coord.members
        ],
    }
