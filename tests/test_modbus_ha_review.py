"""Step 5, round 2: review findings M1-M5, MINOR 1-9 and the nit
. Each test failed on 5867654."""

import asyncio
import os
import shutil
import tempfile

import gnlt_charger.diagnostics as diag
import pytest
from gnlt_charger.modbus import config_flow as mbcf
from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import entities as mb_entities
from gnlt_charger.modbus import runtime as mb_runtime
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import restore_state
from modbus_fake import FakeStation, FakeTcpStation, SimClock
from modbus_ha import (
    COMPONENT_DIR,
    DOMAIN,
    SERIAL,
    entities,
    make_hass,
    modbus_entry,
    setup,
    stop,
    unload,
    wait_for,
)
from test_modbus_config_flow import options_flow, through_address

REPO = os.path.dirname(os.path.dirname(COMPONENT_DIR))


def run(coro):
    return asyncio.run(coro)


async def start_fake(serial=SERIAL, **quirks):
    model = FakeStation("A3C_2.7", serial, SimClock(realtime=True), **quirks)
    server = FakeTcpStation(model)
    await server.start()
    return model, server


async def _loaded(hass, port, **extra):
    entry = modbus_entry(hass, port, **extra)
    assert await setup(hass, entry)
    rt = entry.runtime_data
    await wait_for(lambda: rt.station.snapshot.time is not None, 10)
    return entry, rt


async def _stop_runtime(rt):
    await rt.commander.async_close()
    rt.energy.close()
    await rt.station.async_stop()


# --- M1 + M2: total_energy floor through HA's RestoreSensor -------------------------------


def test_total_energy_floor_after_real_restart_with_wh_display_unit():
    """after an HA crash the Store may be older than
    what HA showed. The floor comes from HA's restore_state data (native kWh),
    never from the displayed state (here in Wh), and works after a real
    restart, not only a reload."""

    async def main():
        config_dir = tempfile.mkdtemp(prefix="gnlt_mb_restart_")
        model, server = await start_fake()
        model.overrides[0] = 0  # status 0 only: no sample, only the floor can carry the total
        hass = await make_hass(config_dir)
        entry = modbus_entry(hass, server.port)
        try:
            assert await setup(hass, entry)
            rt = entry.runtime_data
            ent = entities(hass, entry)[f"{SERIAL}_mb_total_energy"]
            er.async_get(hass).async_update_entity_options(ent.entity_id, "sensor", {"unit_of_measurement": "Wh"})
            rt.meter.total_wh = 12345.0
            rt.async_update()
            await hass.async_block_till_done()
            assert hass.states.get(ent.entity_id).state in ("12345", "12345.0")  # shown in Wh
            await restore_state.async_get(hass).async_dump_states()
            # "Crash": the runtime stops, the Store is older (here: gone).
            await _stop_runtime(rt)
            store = os.path.join(config_dir, ".storage", f"{DOMAIN}.modbus_{SERIAL}")
            if os.path.exists(store):
                os.remove(store)
        finally:
            await stop(hass)
        model.clock.advance(60)  # past the fake's silent window after a close (no own reconnect gap, evcc)
        hass2 = await make_hass(config_dir)
        try:
            entry2 = modbus_entry(hass2, server.port)
            assert await setup(hass2, entry2)
            rt2 = entry2.runtime_data
            assert rt2.station.snapshot.time is None  # restored before any sample
            assert rt2.meter.total_energy_wh == pytest.approx(12345.0)
            # Reload: the floor is not multiplied by the display unit either.
            await unload(hass2, entry2)
            model.clock.advance(60)
            assert await setup(hass2, entry2)
            assert entry2.runtime_data.meter.total_energy_wh == pytest.approx(12345.0)
            await unload(hass2, entry2)
        finally:
            await server.stop()
            await stop(hass2)
            shutil.rmtree(config_dir, ignore_errors=True)

    run(main())


# --- M3: an exception on an optional register leaves it unknown (single reads) -------


def test_probe_exception_on_optional_register_leaves_it_unknown():
    async def main():
        model, server = await start_fake(undocumented_exception=True)
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result.get("step_id") == "modbus_model", result
            regs = flow._modbus["registers"]
            assert regs[C.REG_MAX_CURRENT] == 160 and regs[C.REG_MIN_CURRENT] == 60
            assert 1 not in regs and 33 not in regs  # undocumented fillers never kept
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_probe_exception_on_serial_reads_has_own_text():
    async def main():
        model, server = await start_fake()
        model.set_quirks(undocumented_exception=True)
        import modbus_fake

        documented = modbus_fake.DOCUMENTED
        modbus_fake.DOCUMENTED = frozenset(documented - set(C.SERIAL_GROUP.used))
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result["errors"] == {"base": "mb_exception"}
        finally:
            modbus_fake.DOCUMENTED = documented
            await server.stop()
            await stop(hass)

    run(main())


# --- M4: never a second socket to a station an entry polls ---------------------


def test_no_second_socket_when_setup_dns_failed(monkeypatch):
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        real = mb_runtime.async_resolve
        try:
            async def no_dns(hass_, host, port):
                return ()

            monkeypatch.setattr(mb_runtime, "async_resolve", no_dns)
            entry, rt = await _loaded(hass, server.port, host="localhost")
            monkeypatch.setattr(mb_runtime, "async_resolve", real)
            before = server.accepts
            flow, result = await through_address(hass, server.port, host="127.0.0.1")
            assert result.get("type") == "abort" and result.get("reason") == "address_in_use", result
            assert server.accepts == before and server.open_connections == 1
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_unresolvable_entry_not_loaded_then_probe_goes_ahead(monkeypatch):
    """Start-up like evcc: an entry whose host does not resolve
    gets no answer, so its setup raises ConfigEntryNotReady (HA retries it)
    and it holds no socket; a probe of another address then goes ahead.
    (Was: a loaded entry with an unknown address blocked the probe, such
    an entry can no longer be loaded. The block itself stays for loaded
    entries, see test_no_second_socket_when_setup_dns_failed.)"""
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
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port, host="no-such-host.invalid", serial="302606014614")
            with pytest.raises(ConfigEntryNotReady):
                await setup(hass, entry)
            assert entry.entry_id not in mb_runtime.modbus_data(hass)["entries"]
            flow, result = await through_address(hass, server.port)
            assert result.get("step_id") == "modbus_model", result
            assert server.accepts == 1
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- M5: repair issues of an earlier runtime are cleared after a reload --------------------------


def test_repair_issue_cleared_after_reload():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            rt.station._emit(C.EVENT_LINK_LOST, hint="refused")
            await asyncio.sleep(0)
            issue_id = f"station_not_responding_{SERIAL}"
            assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
            await unload(hass, entry)
            model.clock.advance(60)
            assert await setup(hass, entry)
            rt2 = entry.runtime_data
            await wait_for(lambda: rt2.station.snapshot.time is not None, 10)
            rt2.station._emit(C.EVENT_LINK_RESTORED)
            await asyncio.sleep(0)
            assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- MINOR 1: an address change that fails unexpectedly still restores polling -------------------


def test_address_change_unexpected_error_reloads(monkeypatch):
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            schema = (await options_flow(hass, entry).async_step_init())["data_schema"].schema
            defaults = {str(k): k.default() for k in schema}

            async def boom(*args, **kwargs):
                raise RuntimeError("unexpected")

            async def no_wait(seconds):
                return None

            monkeypatch.setattr(mbcf, "async_probe", boom)
            monkeypatch.setattr(mbcf, "_sleep", no_wait)
            with pytest.raises(RuntimeError):
                await options_flow(hass, entry).async_step_modbus({**defaults, "host": "192.0.2.99"})
            assert hass.data["_test_reloads"] == [entry.entry_id]
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- MINOR 2: the adopted probe socket is closed when setup fails ------------------------------


# --- MINOR 3 + 4: live IP addresses never leak (diagnostics, entity attributes) -------------------


def test_live_ip_redacted_in_diagnostics_and_attributes(monkeypatch):
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            async def no_dns(hass_, host, port):
                return ()

            monkeypatch.setattr(mb_runtime, "async_resolve", no_dns)
            entry, rt = await _loaded(hass, server.port, host="localhost")
            rt.station.last_failure_text = "connect to localhost: refused by 127.0.0.1 (via 192.0.2.17)"
            data = await diag.async_get_config_entry_diagnostics(hass, entry)
            text = str(data)
            assert "127.0.0.1" not in text and "192.0.2.17" not in text and "localhost" not in text
            attrs = entities(hass, entry).get(f"{SERIAL}_mb_last_link_error")
            if attrs is None:  # disabled by default: build it directly
                attrs = next(e for e in mb_entities.build_entities(hass, entry, "sensor") if e.unique_id.endswith("_mb_last_link_error"))
            text = str(attrs.extra_state_attributes)
            assert "127.0.0.1" not in text and "localhost" not in text
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- MINOR 5: warnings under "Diagnostics" (OCPP parity) ----------------------------------------


def test_warning_binary_sensors_are_diagnostic():
    descriptions = {d.key: d for d in mb_entities.BINARY_SENSORS}
    for key in ("empty_session", "charging_without_session"):
        assert descriptions[key].entity_category == EntityCategory.DIAGNOSTIC, key


# --- MINOR 6 + 9: README ---------------------------------------------------------------------


def test_readme_device_wording_and_one_phase_mode():
    checks = {
        "README.md": ("1-phase mode", "two entries"),
        "README.ru.md": ("1-phase mode", "две записи"),
        "README.pl.md": ("1-phase mode", "dwa wpisy"),
    }
    for name, words in checks.items():
        path = os.path.join(REPO, name)
        if not os.path.exists(path):  # the container copy holds only custom_components and tests
            pytest.skip("README not in this copy")
        text = open(path, encoding="utf-8").read()
        section = text.split("ModBus", 1)[1]
        for word in words:
            assert word in section, (name, word)


# --- MINOR 7: Home Assistant stop closes the socket and saves -----------------------------------


def test_ha_stop_closes_socket_and_saves():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry, rt = await _loaded(hass, server.port)
            store = os.path.join(hass.config.config_dir, ".storage", f"{DOMAIN}.modbus_{SERIAL}")
            if os.path.exists(store):
                os.remove(store)
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await hass.async_block_till_done()
            await wait_for(lambda: server.open_connections == 0, 3)
            assert os.path.exists(store)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- MINOR 8: would_do is readable, in the HA language ---------------------------------------------


@pytest.mark.parametrize(
    ("lang", "word"),
    [("en", "another command is running"), ("ru", "выполняется другая команда"), ("pl", "trwa wykonywanie innego polecenia")],
)
def test_would_do_readable_translated(lang, word):
    async def main():
        model, server = await start_fake()
        hass = await make_hass(language=lang)
        try:
            entry, rt = await _loaded(hass, server.port)
            rt.commander.would_do = "schedule: busy"
            attrs = entities(hass, entry)[f"{SERIAL}_mb_status"].extra_state_attributes
            assert word in attrs["would_do"] and "busy" not in attrs["would_do"]
            assert attrs["would_do_key"] == "schedule: busy"
            rt.commander.would_do = "auto_start: daily cap"
            attrs = entities(hass, entry)[f"{SERIAL}_mb_status"].extra_state_attributes
            assert "daily cap" not in attrs["would_do"]
            rt.commander.would_do = "resume: something_new"
            attrs = entities(hass, entry)[f"{SERIAL}_mb_status"].extra_state_attributes
            assert attrs["would_do"]  # unknown reason: still a text, never empty
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- nit: split devices from the registry's own version -------------------------------------------


def test_split_devices_follows_registry_version():
    assert mb_entities.SPLIT_DEVICES == (dr.STORAGE_VERSION_MAJOR >= 3)
    assert "MAJOR_VERSION" not in vars(mb_entities)
