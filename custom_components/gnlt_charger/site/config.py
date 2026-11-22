"""Settings of a GNLT site config entry (no Home Assistant imports).

One site entry owns several GNLT ModBus station entries ("members"). The
entry data/options keys are defined here and parsed into:class:`SiteConfig`;
the config flow (``site/config_flow.py``) writes them, the coordinator
(``site/coordinator.py``) reads them.

Defaults: DLM settings as ``site/dlm.py`` (openWB 2.x, meter period 10 s =
openWB control interval), PV thresholds and delays as evcc
(``core/loadpoint.go`` NewLoadpoint: thresholds 0 W, enable delay 1 min,
disable delay 3 min). Only what a customer has to set is exposed; every other
allocator / loadpoint switch keeps its default.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .dlm import MeterFailureAction

CONF_GRID_L1 = "grid_current_l1"
CONF_GRID_L2 = "grid_current_l2"
CONF_GRID_L3 = "grid_current_l3"
CONF_GRID_INVERT = "grid_invert"
CONF_GRID_POWER = "grid_power"
CONF_PV_POWER = "pv_power"
CONF_METER_PERIOD = "meter_period_s"
CONF_FUSE = "fuse_limit_a"
CONF_MAX_POWER = "max_total_power_w"
CONF_FAILURE_ACTION = "meter_failure_action"
CONF_MEMBERS = "members"
CONF_MEMBER_ENTRY = "entry_id"
CONF_PRIORITY = "priority"
CONF_PHASE_1 = "phase_1"
CONF_ENABLE_THRESHOLD = "enable_threshold_w"
CONF_DISABLE_THRESHOLD = "disable_threshold_w"
CONF_ENABLE_DELAY = "enable_delay_s"
CONF_DISABLE_DELAY = "disable_delay_s"

GRID_KEYS = (CONF_GRID_L1, CONF_GRID_L2, CONF_GRID_L3)

DEFAULT_METER_PERIOD = 10.0  # s, openWB control interval (dlm.py meter_period_s)
DEFAULT_FUSE = 25.0  # A, prefill only: the user confirms the fuse
DEFAULT_PRIORITY = 1
DEFAULT_ENABLE_DELAY = 60.0  # s, evcc NewLoadpoint
DEFAULT_DISABLE_DELAY = 180.0  # s, evcc NewLoadpoint
FAILURE_ACTIONS = [a.value for a in MeterFailureAction]


@dataclass(frozen=True)
class MemberConfig:
    entry_id: str
    priority: int = DEFAULT_PRIORITY  # 1 = served first (DLM ascending order); equal = one group
    phase_1: int = 1  # grid phase that station L1 is wired to


@dataclass(frozen=True)
class SiteConfig:
    grid_entities: tuple[str, str, str]
    grid_invert: bool = False
    grid_power_entity: str | None = None
    pv_power_entity: str | None = None
    meter_period_s: float = DEFAULT_METER_PERIOD
    fuse_limit_a: float = DEFAULT_FUSE
    max_total_power_w: float | None = None
    meter_failure_action: MeterFailureAction = MeterFailureAction.OPENWB
    members: tuple[MemberConfig, ...] = ()
    enable_threshold_w: float = 0.0
    disable_threshold_w: float = 0.0
    enable_delay_s: float = DEFAULT_ENABLE_DELAY
    disable_delay_s: float = DEFAULT_DISABLE_DELAY


def _num(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _entity(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def member_list(opts: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = opts.get(CONF_MEMBERS) or []
    return [dict(m) for m in raw if isinstance(m, Mapping) and m.get(CONF_MEMBER_ENTRY)]


def site_config(opts: Mapping[str, Any]) -> SiteConfig:
    """Entry data merged with options -> settings."""
    members = []
    for m in member_list(opts):
        phase_1 = int(_num(m.get(CONF_PHASE_1), 1))
        members.append(
            MemberConfig(
                entry_id=str(m[CONF_MEMBER_ENTRY]),
                priority=int(_num(m.get(CONF_PRIORITY), DEFAULT_PRIORITY)),
                phase_1=phase_1 if phase_1 in (1, 2, 3) else 1,
            )
        )
    max_power = _num(opts.get(CONF_MAX_POWER), 0.0)
    action = opts.get(CONF_FAILURE_ACTION)
    period = _num(opts.get(CONF_METER_PERIOD), DEFAULT_METER_PERIOD)
    return SiteConfig(
        grid_entities=tuple(str(opts.get(k) or "") for k in GRID_KEYS),  # type: ignore[arg-type]
        grid_invert=bool(opts.get(CONF_GRID_INVERT, False)),
        grid_power_entity=_entity(opts.get(CONF_GRID_POWER)),
        pv_power_entity=_entity(opts.get(CONF_PV_POWER)),
        meter_period_s=period if period > 0 else DEFAULT_METER_PERIOD,
        fuse_limit_a=max(_num(opts.get(CONF_FUSE), DEFAULT_FUSE), 1.0),
        max_total_power_w=max_power if max_power > 0 else None,
        meter_failure_action=MeterFailureAction(action) if action in FAILURE_ACTIONS else MeterFailureAction.OPENWB,
        members=tuple(members),
        enable_threshold_w=_num(opts.get(CONF_ENABLE_THRESHOLD), 0.0),
        disable_threshold_w=_num(opts.get(CONF_DISABLE_THRESHOLD), 0.0),
        enable_delay_s=max(_num(opts.get(CONF_ENABLE_DELAY), DEFAULT_ENABLE_DELAY), 0.0),
        disable_delay_s=max(_num(opts.get(CONF_DISABLE_DELAY), DEFAULT_DISABLE_DELAY), 0.0),
    )
