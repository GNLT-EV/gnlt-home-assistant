"""GNLT site entry in Home Assistant: set-up over two ModBus entries (fake
stations over TCP) next to a third one that is not a member; member entities
(mode select, charging switch -> mode, current number -> loadpoint max, phase
select), site sensors, unload -> repair issue and members released; the site
config flow and options flow."""

from __future__ import annotations

import asyncio

import gnlt_charger.config_flow as cf
import voluptuous as vol
from gnlt_charger.modbus.commands import ORIGIN_USER
from gnlt_charger.modbus.entities import PHASE_OPTIONS
from gnlt_charger.site import ha as site_ha
from gnlt_charger.site.config_flow import SiteOptionsFlow
from gnlt_charger.site.members import member_for, site_data
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from modbus_fake import Car, FakeStation, FakeTcpStation, SimClock
from modbus_ha import DOMAIN, add_entry, entities, make_hass, modbus_entry, setup, stop, translations, unload, wait_for

SERIALS = ("301234567890", "302606014614", "302606014615")
GRID = ("sensor.grid_l1", "sensor.grid_l2", "sensor.grid_l3")


def run(coro):
    return asyncio.run(coro)


async def start_fakes():
    out = []
    for serial in SERIALS:
        model = FakeStation("A3C_2.7", serial, SimClock(realtime=True))
        server = FakeTcpStation(model)
        await server.start()
        out.append((model, server))
    return out


def set_grid(hass, values=(1.0, 1.0, 1.0)):
    for entity_id, value in zip(GRID, values):
        hass.states.async_set(entity_id, str(value), {"unit_of_measurement": "A"})


def site_data_dict(members, **extra):
    return {
        "kind": "site",
        "grid_current_l1": GRID[0],
        "grid_current_l2": GRID[1],
        "grid_current_l3": GRID[2],
        "grid_invert": False,
        "meter_period_s": 5,
        "fuse_limit_a": 25,
        "max_total_power_w": 0,
        "meter_failure_action": "openwb",
        "members": [{"entry_id": e.entry_id, "priority": 1, "phase_1": 1} for e in members],
        "enable_threshold_w": 0,
        "disable_threshold_w": 0,
        "enable_delay_s": 60,
        "disable_delay_s": 180,
        **extra,
    }


async def stations(hass, fakes, *, auto_start_third=False):
    out = []
    for i, (_model, server) in enumerate(fakes):
        entry = modbus_entry(hass, server.port, serial=SERIALS[i])
        if i == 2 and auto_start_third:
            hass.config_entries.async_update_entry(entry, options={"auto_start": True})
        assert await setup(hass, entry)
        out.append(entry)
    for entry in out:
        await wait_for(lambda e=entry: e.runtime_data.station.snapshot.time is not None, 15)
    return out


async def site_entry(hass, members):
    entry = add_entry(hass, data=site_data_dict(members), unique_id="gnlt_site", title="GNLT site")
    assert await setup(hass, entry)
    return entry


def test_site_setup_entities_and_member_controls():
    async def main():
        fakes = await start_fakes()
        hass = await make_hass()
        try:
            set_grid(hass)
            a, b, c = await stations(hass, fakes, auto_start_third=True)
            site = await site_entry(hass, [a, b])
            coord = site.runtime_data
            await wait_for(lambda: coord.cycles >= 1 and all(m.loadpoint is not None for m in coord.members), 15)
            await hass.async_block_till_done()
            # members attached; the non-member is untouched
            assert member_for(hass, a.entry_id) is not None and member_for(hass, b.entry_id) is not None
            assert member_for(hass, c.entry_id) is None
            assert c.runtime_data.site_options_filter is None and c.runtime_data.commander.options.auto_start is True
            # mode select on the members' devices only (added after their set-up)
            reg = er.async_get(hass)
            mode_ids = {
                e.config_entry_id for e in reg.entities.values() if e.unique_id.endswith("_mb_loadpoint_mode")
            }
            assert mode_ids == {a.entry_id, b.entry_id}
            ea = entities(hass, a)
            select = ea[f"{SERIALS[0]}_mb_loadpoint_mode"]
            assert select.available and select.current_option == "off" and select.options == ["off", "now", "minpv", "pv"]
            assert select.device_info["identifiers"] == {(DOMAIN, SERIALS[0])}
            # site sensors on the site device
            se = entities(hass, site)
            keys = {uid.split(f"site_{site.entry_id}_", 1)[1] for uid in se}
            assert {"grid_current_l1", "headroom_l3", "meter_state", "site_power", "pv_surplus"} <= keys
            assert f"allowed_{a.entry_id}" in keys and f"allowed_{c.entry_id}" not in keys
            state = hass.states.get(se[f"site_{site.entry_id}_grid_current_l1"].entity_id)
            assert state is not None and float(state.state) == 1.0
            assert hass.states.get(se[f"site_{site.entry_id}_meter_state"].entity_id).state == "ok"
            # charging switch of a member -> loadpoint mode, no write of its own
            n = len(fakes[0][0].writes)
            await ea[f"{SERIALS[0]}_mb_charging"].async_turn_on()
            m = coord.member(a.entry_id)
            assert m.mode == "now" and m.loadpoint.get_mode() == "now"
            await ea[f"{SERIALS[0]}_mb_charging"].async_turn_off()
            assert m.mode == "off"
            # current number of a member -> loadpoint max current
            await ea[f"{SERIALS[0]}_mb_current_limit"].async_set_native_value(10.0)
            assert m.max_current == 10.0 and m.loadpoint.max_current == 10.0
            assert ea[f"{SERIALS[0]}_mb_current_limit"].native_value == 10.0
            assert len(fakes[0][0].writes) == n
            assert all(w["origin"] != ORIGIN_USER for w in a.runtime_data.station.diagnostics()["write_log"])
            # Plug & Charge cannot be switched on for a member (the station would start by itself)
            from homeassistant.exceptions import HomeAssistantError

            try:
                await ea[f"{SERIALS[0]}_mb_plug_and_charge"].async_turn_on()
            except HomeAssistantError as err:
                assert err.translation_key == "site_member_pnc"
            else:
                raise AssertionError("Plug & Charge switched on for a site member")
            # phase select: "auto" - the 6.1 A probe of the setup (evcc initialize)
            # knows the firmware class, so the loadpoint can switch phases
            assert ea[f"{SERIALS[0]}_mb_phases"].options == [*PHASE_OPTIONS, "auto"]
            # the mode select changes the loadpoint and is kept by the site Store
            await select.async_select_option("pv")
            assert m.mode == "pv" and coord._dump()["members"][a.entry_id]["mode"] == "pv"
            # the non-member's switch still writes by itself (no car: refused as today)
            ec = entities(hass, c)
            try:
                await ec[f"{SERIALS[2]}_mb_charging"].async_turn_on()
            except Exception as err:  # noqa: BLE001
                assert "no_car" in repr(err) or getattr(err, "translation_key", "") == "no_car"
            # no 93 gate on a start any more - under Plug & Charge the site's
            # 95 = 1 goes out, nothing is refused, no "site_member_refused" issue
            fakes[0][0].plug(Car(phases=3, min_a=6.0))
            fakes[0][0].menu_set_mode(0)
            m.set_mode("now")

            async def feed():
                while True:
                    set_grid(hass)
                    await asyncio.sleep(1)

            feeder = asyncio.ensure_future(feed())
            await wait_for(lambda: (95, 1) in [(r, v) for _t, r, v in fakes[0][0].writes], 45)
            assert ir.async_get(hass).async_get_issue(DOMAIN, f"site_member_refused_{site.entry_id}_{a.entry_id}") is None
            feeder.cancel()
            m.set_mode("off")
            # unload: members released, repair issue raised
            await unload(hass, site)
            assert member_for(hass, a.entry_id) is None
            assert a.runtime_data.site_options_filter is None
            assert not select.available
            issue = ir.async_get(hass).async_get_issue(DOMAIN, f"site_stopped_{site.entry_id}")
            assert issue is not None and issue.translation_key == "site_stopped"
            # set-up again clears the issue
            assert await setup(hass, site)
            assert ir.async_get(hass).async_get_issue(DOMAIN, f"site_stopped_{site.entry_id}") is None
            await unload(hass, site)
            for e in (a, b, c):
                await unload(hass, e)
        finally:
            for _model, server in fakes:
                await server.stop()
            await stop(hass)

    run(main())


def test_station_set_up_after_site_gets_mode_select_and_removed_member_loses_it():
    async def main():
        fakes = await start_fakes()
        hass = await make_hass()
        try:
            set_grid(hass)
            a = modbus_entry(hass, fakes[0][1].port, serial=SERIALS[0])
            site = await site_entry(hass, [a])
            hass.config_entries.async_update_entry(a, options={"auto_start": True})
            coord = site.runtime_data
            cycles = coord.cycles
            assert await setup(hass, a)  # the station comes after the site
            # attached at once, before the next site cycle: its auto start is off already
            assert coord.cycles == cycles
            assert member_for(hass, a.entry_id) is coord.member(a.entry_id)
            assert a.runtime_data.site_options_filter is not None
            assert a.runtime_data.commander.options.auto_start is False
            assert f"{SERIALS[0]}_mb_loadpoint_mode" in entities(hass, a)
            coord = site.runtime_data
            await wait_for(lambda: coord.member(a.entry_id).loadpoint is not None, 15)
            await unload(hass, site)
            # the site no longer lists the station: its mode select is removed from the registry
            hass.config_entries.async_update_entry(site, data={**site.data, "members": []})
            assert await setup(hass, site)
            reg = er.async_get(hass)
            assert not [e for e in reg.entities.values() if e.unique_id.endswith("_mb_loadpoint_mode")]
            await unload(hass, site)
            await unload(hass, a)
        finally:
            for _model, server in fakes:
                await server.stop()
            await stop(hass)

    run(main())


def test_site_starts_in_now_mode():
    async def main():
        fakes = await start_fakes()
        hass = await make_hass()
        try:
            set_grid(hass, (0.0, 0.0, 0.0))
            (a,) = [await _one(hass, fakes)]
            a.runtime_data.station.learned.set("decimal_current", "yes")
            fakes[0][0].plug(Car(phases=3, min_a=6.0))
            site = await site_entry(hass, [a])
            coord = site.runtime_data
            await wait_for(lambda: coord.member(a.entry_id).loadpoint is not None, 15)
            coord.member(a.entry_id).set_mode("now")

            async def fed():
                while True:
                    model = fakes[0][0]
                    set_grid(hass, tuple(model.currents()))
                    await asyncio.sleep(0.5)

            feeder = asyncio.ensure_future(fed())
            try:
                await wait_for(lambda: (95, 1) in [(r, v) for _t, r, v in fakes[0][0].writes], 40)
            finally:
                feeder.cancel()
            log = a.runtime_data.station.diagnostics()["write_log"]
            # the 6.1 A probe of this start (evcc initialize) and the site's writes only
            assert {w["origin"] for w in log} - {"firmware_probe"} == {"site"}
            await unload(hass, site)
            await unload(hass, a)
        finally:
            for _model, server in fakes:
                await server.stop()
            await stop(hass)

    run(main())


async def _one(hass, fakes):
    entry = modbus_entry(hass, fakes[0][1].port, serial=SERIALS[0])
    assert await setup(hass, entry)
    await wait_for(lambda: entry.runtime_data.station.snapshot.time is not None, 15)
    return entry


# --- config flow ---------------------------------------------------------------------------


def new_flow(hass):
    flow = cf.GnltConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    flow.flow_id = "test"
    return flow


def _modbus_entries(hass, n=2):
    return [modbus_entry(hass, 1000 + i, serial=SERIALS[i]) for i in range(n)]


def test_site_flow_creates_entry():
    async def main():
        hass = await make_hass()
        try:
            a, b = _modbus_entries(hass)
            flow = new_flow(hass)
            menu = await flow.async_step_user()
            assert "site" in menu["menu_options"]
            result = await flow.async_step_site()
            assert result["type"] == "form" and result["step_id"] == "site"
            fields = {str(k) for k in result["data_schema"].schema}
            assert {"grid_current_l1", "grid_current_l2", "grid_current_l3", "grid_invert", "grid_power", "pv_power",
                    "meter_period_s", "fuse_limit_a", "max_total_power_w", "meter_failure_action", "stations"} == fields
            base = {k: (GRID[i] if i < 3 else None) for i, k in enumerate(("grid_current_l1", "grid_current_l2", "grid_current_l3"))}
            answer = {**base, "grid_invert": True, "meter_period_s": 10, "fuse_limit_a": 32, "max_total_power_w": 0,
                      "meter_failure_action": "minimum", "stations": []}
            result = await flow.async_step_site(answer)
            assert result["errors"] == {"stations": "site_no_members"}
            result = await flow.async_step_site({**answer, "stations": [a.entry_id, b.entry_id]})
            assert result["step_id"] == "site_member" and result["description_placeholders"]["name"] == a.title
            result = await flow.async_step_site_member({"priority": 1, "phase_1": "1"})
            assert result["step_id"] == "site_member" and result["description_placeholders"]["name"] == b.title
            result = await flow.async_step_site_member({"priority": 2, "phase_1": "2"})
            assert result["step_id"] == "site_pv"
            defaults = {str(k): k.default() for k in result["data_schema"].schema}
            assert defaults == {"enable_threshold_w": 0.0, "disable_threshold_w": 0.0, "enable_delay_s": 60.0,
                                "disable_delay_s": 180.0}
            result = await flow.async_step_site_pv(defaults)
            assert result["type"] == "create_entry" and flow.unique_id == "gnlt_site"
            data = result["data"]
            assert data["kind"] == "site" and data["grid_invert"] is True and data["fuse_limit_a"] == 32
            assert data["meter_failure_action"] == "minimum" and data["grid_power"] is None
            assert data["members"] == [
                {"entry_id": a.entry_id, "priority": 1, "phase_1": 1},
                {"entry_id": b.entry_id, "priority": 2, "phase_1": 2},
            ]
        finally:
            await stop(hass)

    run(main())


def test_site_flow_aborts_without_modbus_and_when_configured():
    async def main():
        hass = await make_hass()
        try:
            result = await new_flow(hass).async_step_site()
            assert result["type"] == "abort" and result["reason"] == "site_no_modbus"
            a, = _modbus_entries(hass, 1)
            add_entry(hass, data=site_data_dict([a]), unique_id="gnlt_site", title="GNLT site")
            result = await new_flow(hass).async_step_site()
            assert result["type"] == "abort" and result["reason"] == "already_configured"
        finally:
            await stop(hass)

    run(main())


def test_site_options_flow_prefilled_and_saved():
    async def main():
        hass = await make_hass()
        try:
            a, b = _modbus_entries(hass)
            site = add_entry(hass, data=site_data_dict([a]), unique_id="gnlt_site", title="GNLT site")
            flow = cf.GnltConfigFlow.async_get_options_flow(site)
            assert isinstance(flow, SiteOptionsFlow)
            flow.hass = hass
            flow.handler = site.entry_id
            flow.flow_id = "opt"
            result = await flow.async_step_init()
            defaults = {
                str(k): k.default() for k in result["data_schema"].schema if k.default is not vol.UNDEFINED
            }
            assert defaults["stations"] == [a.entry_id] and defaults["fuse_limit_a"] == 25.0
            result = await flow.async_step_site({**{k: v for k, v in defaults.items() if v is not None},
                                                 "fuse_limit_a": 40, "stations": [a.entry_id, b.entry_id]})
            assert result["step_id"] == "site_member"
            member_defaults = {str(k): k.default() for k in result["data_schema"].schema}
            assert member_defaults == {"priority": 1, "phase_1": "1"}  # the existing member keeps its values
            result = await flow.async_step_site_member({"priority": 1, "phase_1": "3"})
            result = await flow.async_step_site_member({"priority": 3, "phase_1": "1"})
            result = await flow.async_step_site_pv({"enable_threshold_w": -500, "disable_threshold_w": 200,
                                                    "enable_delay_s": 30, "disable_delay_s": 120})
            assert result["type"] == "create_entry"
            data = result["data"]
            assert "kind" not in data and data["fuse_limit_a"] == 40 and data["enable_threshold_w"] == -500
            assert [m["phase_1"] for m in data["members"]] == [3, 1]
        finally:
            await stop(hass)

    run(main())


def test_site_translation_keys_all_languages():
    from gnlt_charger.site.entities import METER_STATES, SITE_SENSORS

    for lang in ("en", "pl", "ru"):
        t = translations(lang)
        sensor = t["entity"]["sensor"]
        for d in SITE_SENSORS:
            assert sensor[d.translation_key]["name"], (lang, d.key)
        assert set(sensor["site_meter_state"]["state"]) == set(METER_STATES)
        assert "{station}" in sensor["site_allowed_current"]["name"]
        assert set(t["entity"]["select"]["mb_loadpoint_mode"]["state"]) == {"off", "now", "minpv", "pv"}
        assert "auto" in t["entity"]["select"]["mb_phases"]["state"]
        for step in ("site", "site_member", "site_pv"):
            assert t["config"]["step"][step]["title"] and t["options"]["step"][step]["title"]
        assert set(t["selector"]["site_failure_action"]["options"]) == {"openwb", "minimum", "stop"}
        assert t["issues"]["site_stopped"]["title"] and "{reason}" in t["issues"]["site_stopped"]["description"]
        assert t["config"]["abort"]["site_no_modbus"]
    assert site_ha.ISSUE_STOPPED == "site_stopped"
    assert site_data  # imported for the registry tests


def test_md4_flow_rejects_same_sensor_twice():
    async def main():
        hass = await make_hass()
        try:
            a, _b = _modbus_entries(hass)
            flow = new_flow(hass)
            await flow.async_step_site()
            answer = {"grid_current_l1": GRID[0], "grid_current_l2": GRID[0], "grid_current_l3": GRID[2],
                      "grid_invert": False, "meter_period_s": 10, "fuse_limit_a": 25, "max_total_power_w": 0,
                      "meter_failure_action": "openwb", "stations": [a.entry_id]}
            result = await flow.async_step_site(answer)
            assert result["errors"] == {"grid_current_l2": "site_grid_same"}
            result = await flow.async_step_site(
                {**answer, "grid_current_l2": GRID[1], "grid_power": "sensor.p", "pv_power": "sensor.p"}
            )
            assert result["errors"] == {"pv_power": "site_power_same"}
            result = await flow.async_step_site({**answer, "grid_current_l2": GRID[1], "grid_power": "sensor.p",
                                                 "pv_power": "sensor.pv"})
            assert result["step_id"] == "site_member"
            for lang in ("en", "pl", "ru"):
                t = translations(lang)
                for root in ("config", "options"):
                    assert t[root]["error"]["site_grid_same"] and t[root]["error"]["site_power_same"]
        finally:
            await stop(hass)

    run(main())


def test_h1_station_listed_in_a_site_is_filtered_from_build_time():
    async def main():
        fakes = await start_fakes()
        hass = await make_hass()
        try:
            a = modbus_entry(hass, fakes[0][1].port, serial=SERIALS[0])
            hass.config_entries.async_update_entry(a, options={"auto_start": True})
            add_entry(hass, data=site_data_dict([a]), unique_id="gnlt_site", title="GNLT site")  # not loaded
            assert await setup(hass, a)
            assert a.runtime_data.site_options_filter is not None
            assert a.runtime_data.commander.options.auto_start is False
            await unload(hass, a)
        finally:
            for _model, server in fakes:
                await server.stop()
            await stop(hass)

    run(main())


def test_l4_deleted_station_entry_is_dropped_from_the_site_options():
    async def main():
        fakes = await start_fakes()
        hass = await make_hass()
        try:
            set_grid(hass)
            a, b, c = await stations(hass, fakes)
            site = await site_entry(hass, [a, b])
            coord = site.runtime_data
            await wait_for(lambda: coord.cycles >= 1, 15)
            await unload(hass, b)
            hass.config_entries._entries.pop(b.entry_id)  # the user deleted the station
            await wait_for(lambda: [m["entry_id"] for m in {**site.data, **site.options}["members"]] == [a.entry_id], 15)
            assert site.entry_id in hass.data["_test_reloads"]  # the site reloads with its new members
            await unload(hass, site)
            await unload(hass, a)
            await unload(hass, c)
        finally:
            for _model, server in fakes:
                await server.stop()
            await stop(hass)

    run(main())


def test_n1_membership_reconciled_with_the_site_entries_at_station_load():
    """Probe 4: the station's store says "member" but no enabled site
    lists it any more (site removed while the station entry was not loaded)."""
    from gnlt_charger.modbus.station import STORE_VERSION, store_key
    from homeassistant.helpers.storage import Store

    async def main():
        fakes = await start_fakes()
        hass = await make_hass()
        try:
            stored = {
                "version": STORE_VERSION, "serial": SERIALS[0], "control_active": True,
                "desired": {"current": 160}, "learned": {"decimal_current": "yes"},
                "commands": {"site": {"member": True, "current": 84}},
            }
            await Store(hass, STORE_VERSION, store_key(SERIALS[0])).async_save(stored)
            a = modbus_entry(hass, fakes[0][1].port, serial=SERIALS[0])
            assert await setup(hass, a)
            cmd = a.runtime_data.commander
            assert cmd.site_member is False and cmd.site_current is None
            assert cmd._start_current_target() == 160  # the user's current controls again
            await unload(hass, a)
            # listed by a site, nothing stored: a member before its first poll
            await Store(hass, STORE_VERSION, store_key(SERIALS[1])).async_save(
                {**stored, "serial": SERIALS[1], "commands": {}}
            )
            b = modbus_entry(hass, fakes[1][1].port, serial=SERIALS[1])
            add_entry(hass, data=site_data_dict([b]), unique_id="gnlt_site", title="GNLT site")  # not loaded
            assert await setup(hass, b)
            assert b.runtime_data.commander.site_member is True
            assert b.runtime_data.commander.site_current is None
            await unload(hass, b)
        finally:
            for _model, server in fakes:
                await server.stop()
            await stop(hass)

    run(main())
