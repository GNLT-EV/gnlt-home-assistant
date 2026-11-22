"""Adding a GNLT site (DLM + PV for several ModBus stations), and its options.

Imported only inside the site steps of the integration's flow. Steps (the
options flow has the same three, prefilled):

1. ``site``: grid meter (three phase-current sensors, import positive, with
   "invert sign"), optional grid power and PV power sensors, meter update
   period, fuse limit per phase, optional total power limit, DLM action on a
   meter failure, member stations (GNLT ModBus entries);
2. ``site_member``: once per member - priority (1 = served first) and the
   grid phase its L1 is wired to;
3. ``site_pv``: PV thresholds and delays (evcc defaults).

One site per Home Assistant (unique id ``gnlt_site``).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import CONF_KIND, CONF_TRANSPORT, DOMAIN, KIND_SITE, TRANSPORT_MODBUS
from .config import (
    CONF_DISABLE_DELAY,
    CONF_DISABLE_THRESHOLD,
    CONF_ENABLE_DELAY,
    CONF_ENABLE_THRESHOLD,
    CONF_FAILURE_ACTION,
    CONF_FUSE,
    CONF_GRID_INVERT,
    CONF_GRID_L1,
    CONF_GRID_L2,
    CONF_GRID_L3,
    CONF_GRID_POWER,
    CONF_MAX_POWER,
    CONF_MEMBER_ENTRY,
    CONF_MEMBERS,
    CONF_METER_PERIOD,
    CONF_PHASE_1,
    CONF_PRIORITY,
    CONF_PV_POWER,
    DEFAULT_DISABLE_DELAY,
    DEFAULT_ENABLE_DELAY,
    DEFAULT_FUSE,
    DEFAULT_METER_PERIOD,
    DEFAULT_PRIORITY,
    FAILURE_ACTIONS,
    GRID_KEYS,
    member_list,
)

SITE_UNIQUE_ID = "gnlt_site"
SITE_TITLE = "GNLT site (beta)"  # DLM + PV is beta in 0.3.0
CONF_STATIONS = "stations"  # the multi-select of the first step
PHASE_1_OPTIONS = ["1", "2", "3"]


def _number(low: float, high: float, step: float | str, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _sensor(device_class: str) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor", device_class=device_class))


def modbus_stations(hass: Any) -> dict[str, str]:
    """entry id -> title of every GNLT ModBus entry."""
    return {
        e.entry_id: e.title or e.entry_id
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS
    }


def _site_schema(hass: Any, current: dict[str, Any]) -> vol.Schema:
    stations = modbus_stations(hass)
    chosen = [m[CONF_MEMBER_ENTRY] for m in member_list(current) if m[CONF_MEMBER_ENTRY] in stations]
    fields: dict[Any, Any] = {}
    for key in GRID_KEYS:
        marker = vol.Required(key, default=current[key]) if current.get(key) else vol.Required(key)
        fields[marker] = _sensor("current")
    fields[vol.Required(CONF_GRID_INVERT, default=bool(current.get(CONF_GRID_INVERT, False)))] = BooleanSelector()
    for key in (CONF_GRID_POWER, CONF_PV_POWER):
        marker = vol.Optional(key, default=current[key]) if current.get(key) else vol.Optional(key)
        fields[marker] = _sensor("power")
    fields[vol.Required(CONF_METER_PERIOD, default=float(current.get(CONF_METER_PERIOD) or DEFAULT_METER_PERIOD))] = (
        _number(5, 60, 1, "s")
    )
    fields[vol.Required(CONF_FUSE, default=float(current.get(CONF_FUSE) or DEFAULT_FUSE))] = _number(6, 250, 1, "A")
    fields[vol.Required(CONF_MAX_POWER, default=float(current.get(CONF_MAX_POWER) or 0))] = _number(0, 500000, 100, "W")
    action = current.get(CONF_FAILURE_ACTION)
    fields[vol.Required(CONF_FAILURE_ACTION, default=action if action in FAILURE_ACTIONS else "openwb")] = SelectSelector(
        SelectSelectorConfig(options=FAILURE_ACTIONS, translation_key="site_failure_action", mode=SelectSelectorMode.LIST)
    )
    fields[vol.Required(CONF_STATIONS, default=chosen)] = SelectSelector(
        SelectSelectorConfig(
            options=[{"value": k, "label": v} for k, v in stations.items()],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )
    return vol.Schema(fields)


def _member_schema(current: dict[str, Any]) -> vol.Schema:
    phase_1 = str(current.get(CONF_PHASE_1) or 1)
    return vol.Schema(
        {
            vol.Required(CONF_PRIORITY, default=int(current.get(CONF_PRIORITY) or DEFAULT_PRIORITY)): _number(1, 10, 1),
            vol.Required(CONF_PHASE_1, default=phase_1 if phase_1 in PHASE_1_OPTIONS else "1"): SelectSelector(
                SelectSelectorConfig(options=PHASE_1_OPTIONS, translation_key="site_phase_1", mode=SelectSelectorMode.LIST)
            ),
        }
    )


def _pv_schema(current: dict[str, Any]) -> vol.Schema:
    def num(key: str, default: float) -> float:
        value = current.get(key)
        return float(value) if isinstance(value, (int, float)) else default

    return vol.Schema(
        {
            vol.Required(CONF_ENABLE_THRESHOLD, default=num(CONF_ENABLE_THRESHOLD, 0.0)): _number(-50000, 50000, 10, "W"),
            vol.Required(CONF_DISABLE_THRESHOLD, default=num(CONF_DISABLE_THRESHOLD, 0.0)): _number(-50000, 50000, 10, "W"),
            vol.Required(CONF_ENABLE_DELAY, default=num(CONF_ENABLE_DELAY, DEFAULT_ENABLE_DELAY)): _number(0, 3600, 1, "s"),
            vol.Required(CONF_DISABLE_DELAY, default=num(CONF_DISABLE_DELAY, DEFAULT_DISABLE_DELAY)): _number(0, 3600, 1, "s"),
        }
    )


class SiteSteps:
    """The three steps on a flow object (config flow or options flow).

    ``_site_values`` collects the answers; ``_site_finish`` creates the entry.
    """

    def _site_init(self, current: dict[str, Any]) -> None:
        self._site_values: dict[str, Any] = dict(current)
        self._site_pending: list[str] = []
        self._site_members: list[dict[str, Any]] = []

    async def _step_site(self, flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        values = self._site_values
        if user_input is not None:
            stations = [s for s in user_input.get(CONF_STATIONS) or [] if s in modbus_stations(flow.hass)]
            if not stations:
                errors[CONF_STATIONS] = "site_no_members"
            seen: set[str] = set()
            for key in GRID_KEYS:
                entity = user_input.get(key)
                if not entity:
                    errors[key] = "site_grid_missing"
                elif entity in seen:
                    errors[key] = "site_grid_same"  # three different phase sensors
                seen.add(entity)
            power, pv = user_input.get(CONF_GRID_POWER), user_input.get(CONF_PV_POWER)
            if power and pv and power == pv:
                errors[CONF_PV_POWER] = "site_power_same"
            if not errors:
                old = {m[CONF_MEMBER_ENTRY]: m for m in member_list(values)}
                data = {k: v for k, v in user_input.items() if k != CONF_STATIONS}
                for key in (CONF_GRID_POWER, CONF_PV_POWER):
                    data.setdefault(key, None)
                values.update(data)
                self._site_pending = list(stations)
                self._site_members = []
                self._site_old = old
                return await flow.async_step_site_member()
            values = {**values, **user_input}
        return flow.async_show_form(step_id="site", data_schema=_site_schema(flow.hass, values), errors=errors)

    async def _step_member(self, flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        entry_id = self._site_pending[0]
        if user_input is not None:
            self._site_members.append(
                {
                    CONF_MEMBER_ENTRY: entry_id,
                    CONF_PRIORITY: int(user_input.get(CONF_PRIORITY) or DEFAULT_PRIORITY),
                    CONF_PHASE_1: int(user_input.get(CONF_PHASE_1) or 1),
                }
            )
            self._site_pending.pop(0)
            if self._site_pending:
                return await flow.async_step_site_member()
            self._site_values[CONF_MEMBERS] = self._site_members
            return await flow.async_step_site_pv()
        name = modbus_stations(flow.hass).get(entry_id, entry_id)
        return flow.async_show_form(
            step_id="site_member",
            data_schema=_member_schema(self._site_old.get(entry_id, {})),
            description_placeholders={"name": name},
        )

    async def _step_pv(self, flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if user_input is not None:
            self._site_values.update(user_input)
            return self._site_finish(flow)
        return flow.async_show_form(step_id="site_pv", data_schema=_pv_schema(self._site_values))

    def _site_finish(self, flow: Any) -> ConfigFlowResult:
        raise NotImplementedError


# --- config flow (called from config_flow.GnltConfigFlow) ---------------------------------


class _ConfigSteps(SiteSteps):
    def _site_finish(self, flow: Any) -> ConfigFlowResult:
        data = {CONF_KIND: KIND_SITE, **self._site_values}
        return flow.async_create_entry(title=SITE_TITLE, data=data)


def _state(flow: Any) -> _ConfigSteps:
    steps = getattr(flow, "_site_steps", None)
    if steps is None:
        steps = _ConfigSteps()
        steps._site_init({})
        flow._site_steps = steps
    return steps


async def async_step_site(flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
    if user_input is None:
        if flow.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, SITE_UNIQUE_ID) is not None:
            return flow.async_abort(reason="already_configured")
        await flow.async_set_unique_id(SITE_UNIQUE_ID, raise_on_progress=False)
        if not modbus_stations(flow.hass):
            return flow.async_abort(reason="site_no_modbus")
    return await _state(flow)._step_site(flow, user_input)


async def async_step_member(flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
    return await _state(flow)._step_member(flow, user_input)


async def async_step_pv(flow: Any, user_input: dict[str, Any] | None) -> ConfigFlowResult:
    return await _state(flow)._step_pv(flow, user_input)


# --- options ----------------------------------------------------------------------------


class SiteOptionsFlow(OptionsFlow, SiteSteps):
    def _ensure(self) -> None:
        if not hasattr(self, "_site_values"):
            self._site_init({**self.config_entry.data, **self.config_entry.options})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_site()

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure()
        return await self._step_site(self, user_input)

    async def async_step_site_member(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure()
        return await self._step_member(self, user_input)

    async def async_step_site_pv(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure()
        return await self._step_pv(self, user_input)

    def _site_finish(self, flow: Any) -> ConfigFlowResult:
        options = {k: v for k, v in self._site_values.items() if k != CONF_KIND}
        return self.async_create_entry(data=options)
