"""ModBus entry in Home Assistant: setup and unload beside an OCPP
entry of the same station, entities, availability, options without reload,
remove with the Store, lazy imports."""

import asyncio
import os
import subprocess
import sys
import textwrap

import gnlt_charger as integ
import pytest
from gnlt_charger.modbus import entities as mb_entities
from gnlt_charger.modbus import runtime as mb_runtime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from modbus_fake import FakeStation, FakeTcpStation, SimClock
from modbus_ha import (
    DOMAIN,
    SERIAL,
    add_entry,
    entities,
    make_hass,
    modbus_entry,
    setup,
    stop,
    unload,
    wait_for,
)

# Table 2.7.1: every key of the ModBus entity set (unique id <serial>_mb_<key>).
TABLE_KEYS = {
    "status", "error", "power", "apparent_power", "phases_in_use",
    "current_l1", "current_l2", "current_l3", "voltage_l1", "voltage_l2", "voltage_l3",
    "session_energy", "total_energy", "price_now", "session_cost", "cost_today", "cost_month",
    "energy_today", "energy_month", "sessions_month", "last_session", "next_start",
    "current_setpoint", "min_current", "max_current", "cable_max_current", "fallback_current", "comm_timeout",
    "station_meter", "charge_duration", "power_l1", "power_l2", "power_l3", "link_errors_24h", "last_link_error",
    "connected", "is_charging", "cable", "empty_session", "charging_without_session", "stop_not_working",
    "charging", "auto_start", "resume_after_power_loss", "schedule",
    "schedule_stop_at_end", "plug_and_charge", "current_limit", "phases", "schedule_days", "schedule_start",
    "schedule_end", "refresh", "pnc_off_and_stop",
}
NOT_CREATED = {"restart", "force_stop", "temperature", "out_of_service"}


async def start_fake(**quirks):
    model = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True), **quirks)
    server = FakeTcpStation(model)
    await server.start()
    return model, server


def run(coro):
    return asyncio.run(coro)


async def _loaded(hass, port, **extra):
    entry = modbus_entry(hass, port, **extra)
    assert await setup(hass, entry)
    rt = entry.runtime_data
    await wait_for(lambda: rt.station.snapshot.time is not None, 10)
    return entry, rt


def test_setup_removes_retired_entities_and_notifications(monkeypatch):
    """entities and notification kinds removed in 3.4.18 leave no
    orphans - the entity registry entries are removed and their
    notifications dismissed at setup (standard entity_registry API)."""
    from homeassistant.components import persistent_notification

    from gnlt_charger.const import DOMAIN

    dismissed = []
    real = persistent_notification.async_dismiss

    def dismiss(hass, notification_id):
        dismissed.append(notification_id)
        return real(hass, notification_id)

    monkeypatch.setattr(persistent_notification, "async_dismiss", dismiss)
    retired = {
        "switch": "control_active", "binary_sensor": "control_problem",
        "sensor": "station_session_energy", "button": "reset_learned",
    }

    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            reg = er.async_get(hass)
            for platform, key in retired.items():
                reg.async_get_or_create(platform, DOMAIN, f"{SERIAL}_mb_{key}")
            kept = reg.async_get_or_create("sensor", DOMAIN, f"{SERIAL}_mb_power")
            entry, rt = await _loaded(hass, server.port)
            for platform, key in retired.items():
                assert reg.async_get_entity_id(platform, DOMAIN, f"{SERIAL}_mb_{key}") is None, key
            assert reg.async_get(kept.entity_id) is not None
            for kind in ("write_breaker", "phase_chatter", "stop_inconclusive", "restore_dropped"):
                assert f"{DOMAIN}_{SERIAL}_mb_{kind}" in dismissed, kind
            assert f"{DOMAIN}_{SERIAL}_mb_faulted" not in dismissed
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- ---------------------------------------------------------------


def test_entity_set_matches_table():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            # From the registry: entities disabled by default are registered, not added.
            reg = er.async_get(hass)
            keys = {
                e.unique_id.split("_mb_", 1)[1] for e in reg.entities.values() if e.config_entry_id == entry.entry_id
            }
            assert keys == TABLE_KEYS
            assert not keys & NOT_CREATED
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_unique_ids_prefixed_and_same_device_as_ocpp():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            ocpp = add_entry(hass, data={"identity": SERIAL, "port": 9000, "preset": "3_11"}, unique_id=SERIAL)
            devreg = dr.async_get(hass)
            ocpp_dev = devreg.async_get_or_create(
                config_entry_id=ocpp.entry_id,
                identifiers={(DOMAIN, SERIAL)},
                name=f"GNLT {SERIAL}",
                manufacturer="GNLT",
                model="GNLT EVE",
                sw_version="SN:301234567890 A3C_2.7 HW:E22_2.0",
            )
            entry, rt = await _loaded(hass, server.port)
            reg = er.async_get(hass)
            ours = [e for e in reg.entities.values() if e.config_entry_id == entry.entry_id]
            assert ours and all(e.unique_id.startswith(f"{SERIAL}_mb_") for e in ours)
            device_ids = {e.device_id for e in ours}
            assert len(device_ids) == 1
            device = devreg.async_get(device_ids.pop())
            assert (DOMAIN, SERIAL) in device.identifiers
            # The OCPP device keeps its name, model and firmware.
            ocpp_after = devreg.async_get(ocpp_dev.id)
            assert ocpp_after.name == f"GNLT {SERIAL}"
            assert ocpp_after.model == "GNLT EVE"
            assert ocpp_after.sw_version == "SN:301234567890 A3C_2.7 HW:E22_2.0"
            if mb_entities.SPLIT_DEVICES:
                # HA >= 2026.8: one device record per config entry, same identifiers.
                assert device.id != ocpp_dev.id
                # a name of its own, so the two devices and their entity ids differ
                assert device.name == f"GNLT {SERIAL} ModBus" and device.sw_version is None
                assert all(e.entity_id.split(".")[1].startswith(f"gnlt_{SERIAL}_modbus") for e in ours)
            else:
                assert device.id == ocpp_dev.id
            # The OCPP entry, its count, store and server are untouched.
            assert DOMAIN not in hass.data
            assert "identity" not in entry.data and entry.data["serial"] == SERIAL
            await unload(hass, entry)
            assert DOMAIN not in hass.data
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_device_info_identifiers_only_when_ocpp_entry_configured(monkeypatch):
    async def main():
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, 1)
            rt = mb_runtime.build_runtime(hass, entry, mb_runtime.ModbusClient("127.0.0.1", 1), ())
            monkeypatch.setattr(mb_entities, "SPLIT_DEVICES", False)
            full = mb_entities.device_info(rt)
            assert full["name"] == f"GNLT {SERIAL} ModBus" and full["model"] == "GNLT EVE"  # PL: wall box = EVE
            assert "hw_version" not in full and "sw_version" not in full  # no firmware profile
            add_entry(hass, data={"identity": SERIAL}, unique_id=SERIAL)
            assert mb_entities.device_info(rt) == {"identifiers": {(DOMAIN, SERIAL)}}
            monkeypatch.setattr(mb_entities, "SPLIT_DEVICES", True)
            assert mb_entities.device_info(rt)["name"] == f"GNLT {SERIAL} ModBus"
            await rt.commander.async_close()
            await rt.client.close()
        finally:
            await stop(hass)

    run(main())


# --- --------------------------------------------------------------------


def test_setup_not_ready_when_station_does_not_answer_at_start(monkeypatch):
    """Start-up like evcc's NewEm2Go: the first contact is retried
    with a backoff and given up after 30 s (scaled down here); then the
    setup raises ConfigEntryNotReady - Home Assistant's own retry takes over.
    Nothing is forwarded and no socket is left."""
    from dataclasses import replace

    from gnlt_charger.modbus import station as mb_station
    from homeassistant.exceptions import ConfigEntryNotReady

    real = mb_station.Station.async_first_contact

    async def quick(self, **kwargs):
        self.timing = replace(self.timing, startup_initial_s=0.02, startup_max_s=0.05, startup_give_up_s=0.3)
        return await real(self, **kwargs)

    monkeypatch.setattr(mb_station.Station, "async_first_contact", quick)

    async def main():
        model, server = await start_fake()
        await server.close_port()  # ModBus off in the menu: refused
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port)
            with pytest.raises(ConfigEntryNotReady):
                await integ.async_setup_entry(hass, entry)
            data = mb_runtime.modbus_data(hass)
            assert entry.entry_id not in data["entries"]
            assert hass.data["_test_platforms"].get(entry.entry_id) in (None, [])
            assert server.open_connections == 0
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_setup_not_ready_when_reg_89_cannot_be_read():
    """evcc NewEm2Go: reg 89 once after the first contact; a failed read
    fails the setup (ConfigEntryNotReady, Home Assistant retries)."""
    from homeassistant.exceptions import ConfigEntryNotReady

    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            model.on_next("exception", fc=3, addr=89, code=2)
            entry = modbus_entry(hass, server.port)
            with pytest.raises(ConfigEntryNotReady):
                await integ.async_setup_entry(hass, entry)
            assert entry.entry_id not in mb_runtime.modbus_data(hass)["entries"]
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_poll_period_option_like_ocpp_meter_interval():
    """the options have "meter_interval" (default 10 s); it is applied
    without a reload."""

    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            assert rt.station.timing.poll_s == 10.0
            hass.config_entries.async_update_entry(entry, options={"meter_interval": 30})
            await hass.async_block_till_done()
            assert entry.runtime_data is rt and rt.station.next_delay() == 30.0
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_setup_not_ready_while_the_serial_reads_zeros(monkeypatch):
    """.11 (item 1): a serial that only reads zeros is not verified
     - a failed try of the start-up backoff; after the give-up the
    setup is not ready (Home Assistant retries), nothing is written."""
    from dataclasses import replace

    from gnlt_charger.modbus import station as mb_station
    from homeassistant.exceptions import ConfigEntryNotReady

    real = mb_station.Station.async_first_contact

    async def quick(self, **kwargs):
        self.timing = replace(self.timing, startup_initial_s=0.02, startup_max_s=0.05, startup_give_up_s=0.3)
        return await real(self, **kwargs)

    monkeypatch.setattr(mb_station.Station, "async_first_contact", quick)

    async def main():
        model, server = await start_fake()
        model._zero_reads = 10**9  # answers, but only zeros
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port)
            with pytest.raises(ConfigEntryNotReady):
                await setup(hass, entry)
            assert model.writes == []
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_setup_entry_succeeds_with_station_giving_no_data_and_availability():
    """a station that answers but gives no data yet (status 0,
    the poll is dropped) - the setup succeeds, entities show the state."""

    async def main():
        model, server = await start_fake()
        model.overrides[0] = 0  # identity fine, status 0: no sample
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port)
            assert await setup(hass, entry)
            ents = entities(hass, entry)
            rt = entry.runtime_data
            await asyncio.sleep(0.3)
            assert rt.station.snapshot.time is None
            for key in ("total_energy", "connected", "cost_today", "refresh"):
                assert ents[f"{SERIAL}_mb_{key}"].available, key
            for key in ("power", "current_l1", "status", "charging", "current_limit", "plug_and_charge"):
                assert not ents[f"{SERIAL}_mb_{key}"].available, key
            assert ents[f"{SERIAL}_mb_connected"].is_on is False
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_availability_after_poll():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            ents = entities(hass, entry)
            for key in ("power", "status", "charging", "current_limit", "plug_and_charge", "phases"):
                assert ents[f"{SERIAL}_mb_{key}"].available, key
            assert ents[f"{SERIAL}_mb_status"].native_value == "standby"
            assert ents[f"{SERIAL}_mb_connected"].is_on is True
            assert hass.states.get(ents[f"{SERIAL}_mb_status"].entity_id).state == "standby"
            # 93 = 1 (command mode): the P&C button is unavailable.
            assert not ents[f"{SERIAL}_mb_pnc_off_and_stop"].available
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- ------------------------------------------------------------------------------


def test_three_phase_entities_disabled_1p_and_phase_select_no_reload():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port, preset="1_7")
            reg = er.async_get(hass)
            ents = entities(hass, entry)
            for key in ("current_l2", "voltage_l3", "phases_in_use"):
                entity_id = reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_mb_{key}")
                assert reg.async_get(entity_id).disabled_by is not None, key
            entity_id = reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_mb_current_l1")
            assert reg.async_get(entity_id).disabled_by is None
            phases = ents[f"{SERIAL}_mb_phases"]
            assert not phases.available  # 1-phase preset
            hass.config_entries.async_update_entry(entry, options={"preset": "3_11"})
            await hass.async_block_till_done()  # the entry's update listener
            assert entry.runtime_data is rt  # no reload
            assert phases.available
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- -------------------------------------------------------------------------------


def test_options_change_no_reload_host_change_reloads():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            hass.config_entries.async_update_entry(
                entry,
                # a stale key of a removed option is ignored
                options={"auto_start": True, "energy_source": "register", "price": 1.2, "profile": "A3C_3.1",
                         "phase_switch_while_charging": True},
            )
            await hass.async_block_till_done()  # the entry's update listener
            assert entry.runtime_data is rt and hass.data["_test_reloads"] == []
            assert rt.commander.options.auto_start is True
            assert not hasattr(rt.commander.options, "phase_switch_while_charging")
            assert rt.meter.source == "register" and rt.meter.tariff.price == 1.2
            assert rt.client.connections == 1  # no reconnect
            hass.config_entries.async_update_entry(entry, data={**entry.data, "host": "localhost"})
            await hass.async_block_till_done()
            assert hass.data["_test_reloads"] == [entry.entry_id]
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- ---------------------------------------------------------------------------


def test_unload_leaves_no_socket_and_saves_then_remove_deletes_store():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            await unload(hass, entry)
            await wait_for(lambda: server.open_connections == 0, 3)
            path = os.path.join(hass.config.config_dir, ".storage", f"{DOMAIN}.modbus_{SERIAL}")
            assert os.path.exists(path)
            data = mb_runtime.modbus_data(hass)
            assert SERIAL in data["stores"]  # kept across a reload
            assert entry.entry_id not in data["entries"]
            assert "last_close" not in data  # no reload gap any more (evcc)
            # Remove: Store and cache go, a re-added entry starts clean.
            await integ.async_remove_entry(hass, entry)
            await hass.async_block_till_done()
            assert not os.path.exists(path)
            assert SERIAL not in data["stores"]
            hass.config_entries._entries.pop(entry.entry_id)
            model.clock.advance(60)  # the station's own pause after a close
            again, rt2 = await _loaded(hass, server.port)
            assert rt2.station.desired["current"] is None or rt2.station.desired["current"] == 160
            await unload(hass, again)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_store_shared_across_reload():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            rt.station.desired["current"] = 120
            store = mb_runtime.modbus_data(hass)["stores"][SERIAL]
            await unload(hass, entry)
            model.clock.advance(60)
            assert await setup(hass, entry)
            rt2 = entry.runtime_data
            assert rt2 is not rt and rt2.station._store is store
            assert rt2.station.desired["current"] == 120
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_restore_total_before_first_sample():
    async def main():
        model, server = await start_fake()
        model.overrides[0] = 0  # answers, but no sample (status 0 drops the poll)
        hass = await make_hass()
        try:
            # HA's restore data (native value + unit), as RestoreSensor stores it;
            # the shown state is never read.
            from datetime import UTC, datetime

            from homeassistant.components.sensor import SensorExtraStoredData
            from homeassistant.core import State
            from homeassistant.helpers import restore_state

            reg = er.async_get(hass)
            ent = reg.async_get_or_create("sensor", DOMAIN, f"{SERIAL}_mb_total_energy")
            restore_state.async_get(hass).last_states[ent.entity_id] = restore_state.StoredState(
                State(ent.entity_id, "12500"), SensorExtraStoredData(12.5, "kWh"), datetime.now(UTC)
            )
            entry = modbus_entry(hass, server.port)
            assert await setup(hass, entry)
            rt = entry.runtime_data
            assert rt.station.snapshot.time is None  # no sample booked yet
            assert rt.meter.total_energy_wh >= 12500
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_minute_tick_rolls_and_runs_schedule(monkeypatch):
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        ticks = []
        monkeypatch.setattr(mb_runtime, "async_track_time_change", lambda h, cb, second: ticks.append((cb, second)) or (lambda: None))
        try:
            entry, rt = await _loaded(hass, server.port)
            assert len(ticks) == 1 and ticks[0][1] == 0  # at second 0
            calls = []
            monkeypatch.setattr(rt.meter, "roll", lambda now=None: calls.append(("roll", now)))

            async def sched(now=None):
                calls.append(("schedule", now))

            monkeypatch.setattr(rt.commander, "async_schedule_tick", sched)
            await ticks[0][0]("NOW")
            assert calls == [("roll", "NOW"), ("schedule", "NOW")]
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- -------------------------------------------------------------------------


def test_refusals_translated_no_master_switch():
    """no "Control active" switch; refusals are translated errors."""

    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            ents = entities(hass, entry)
            assert f"{SERIAL}_mb_control_active" not in ents
            with pytest.raises(HomeAssistantError) as err:
                await ents[f"{SERIAL}_mb_charging"].async_turn_on()
            assert err.value.translation_key == "no_car"
            assert err.value.translation_placeholders == {"detail": ""}
            n = len(model.writes)
            with pytest.raises(HomeAssistantError) as err:
                await ents[f"{SERIAL}_mb_current_limit"].async_set_native_value(40.0)
            assert err.value.translation_key == "current_out_of_range"
            assert err.value.translation_placeholders["detail"] == "6.0–16.0"
            assert len(model.writes) == n
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- ---------------------------------------------------------------------------------


def _python(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, env=env, timeout=120)


def test_ocpp_entry_never_imports_modbus():
    result = _python(
        """
        import sys
        import gnlt_charger, gnlt_charger.config_flow, gnlt_charger.diagnostics
        for p in ("sensor", "binary_sensor", "switch", "number", "select", "button", "time"):
            __import__(f"gnlt_charger.{p}")
        loaded = [m for m in sys.modules if m.startswith("gnlt_charger.modbus")]
        print("LOADED", loaded)
        assert not loaded, loaded
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ocpp_flow_works_when_modbus_import_fails():
    result = _python(
        """
        import asyncio, sys
        from types import SimpleNamespace
        sys.modules["gnlt_charger.modbus"] = None  # any import of it fails
        from gnlt_charger.config_flow import GnltConfigFlow
        flow = GnltConfigFlow()
        flow.hass = SimpleNamespace(data={})
        flow.handler = "gnlt_charger"
        flow.context = {"source": "user"}
        flow.flow_id = "x"
        result = asyncio.run(flow.async_step_user())
        assert result["menu_options"][:2] == ["pick_device", "manual"], result
        try:
            asyncio.run(flow.async_step_modbus_intro())
        except ImportError:
            print("modbus step fails alone")
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "modbus step fails alone" in result.stdout
