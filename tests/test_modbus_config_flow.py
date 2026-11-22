"""ModBus config flow and options:... texts, probe hand-over, options."""

import asyncio
import time

import gnlt_charger.config_flow as cf
import pytest
from gnlt_charger.modbus import config_flow as mbcf
from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import runtime as mb_runtime
from modbus_fake import FakeStation, FakeTcpStation, SimClock
from modbus_ha import (
    DOMAIN,
    SERIAL,
    make_hass,
    modbus_entry,
    setup,
    stop,
    unload,
    wait_for,
)

OTHER = "302606014614"


def run(coro):
    return asyncio.run(coro)


async def start_fake(serial=SERIAL, **quirks):
    model = FakeStation("A3C_2.7", serial, SimClock(realtime=True), **quirks)
    server = FakeTcpStation(model)
    await server.start()
    return model, server


def new_flow(hass):
    flow = cf.GnltConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    flow.flow_id = "test"
    return flow


def options_flow(hass, entry):
    flow = cf.GnltConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "opt"
    return flow


async def through_address(hass, port, host="127.0.0.1"):
    flow = new_flow(hass)
    result = await flow.async_step_modbus_intro()
    assert result["step_id"] == "modbus_intro"
    result = await flow.async_step_modbus_intro({})
    assert result["step_id"] == "modbus_address"
    result = await flow.async_step_modbus_address({"host": host, "port": port})
    return flow, result


def test_menu_has_modbus_item_and_intro_step():
    async def main():
        hass = await make_hass()
        try:
            flow = new_flow(hass)
            result = await flow.async_step_user()
            # existing items unchanged; the GNLT site (DLM + PV) comes last
            assert result["menu_options"] == ["pick_device", "manual", "modbus_intro", "site"]
            result = await flow.async_step_modbus_intro()
            assert result["type"] == "form" and result["step_id"] == "modbus_intro"
            assert dict(result["data_schema"].schema) == {}
        finally:
            await stop(hass)

    run(main())


def test_full_flow_creates_entry_probe_single_socket_le_3_regs(monkeypatch):
    def no_ble(*args, **kwargs):
        raise AssertionError("Bluetooth used in the ModBus flow")

    monkeypatch.setattr(cf, "discovered_chargers", no_ble)
    monkeypatch.setattr(cf, "provision", no_ble)

    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result["step_id"] == "modbus_model", result
            assert server.accepts == 1
            reads = [r for r in model.requests if r[2] == 3]
            assert reads[0][3] == 38  # identity first
            assert all(r[4] <= 3 for r in reads)
            assert model.writes == []
            # Prefill: reg 32 = 16 A and a readable reg 200 -> three-phase 11 kW.
            schema = {str(k): k for k in result["data_schema"].schema}
            assert schema["preset"].default() == "3_11"
            assert set(schema) == {"preset"}  # no profile select, no firmware field
            result = await flow.async_step_modbus_model({"preset": "3_11"})
            assert result["type"] == "create_entry"
            assert result["title"] == f"GNLT {SERIAL} ModBus"  # the device name's form (notification titles)
            assert result["data"] == {
                "transport": "modbus", "serial": SERIAL, "host": "127.0.0.1", "port": server.port,
                "preset": "3_11",
            }
            assert "identity" not in result["data"]
            assert result["description"] == "modbus_created"
            assert flow.unique_id == f"modbus_{SERIAL}"
            # no hand-over - the probe socket is closed, the entry connects by itself.
            await wait_for(lambda: server.open_connections == 0, 3)
            assert "probe" not in mb_runtime.modbus_data(hass)
            # The fake keeps a reconnect within 5 s of a close silent (A3C_2.7 rule,
            # measured): a user takes longer than that on the model step. Right
            # away, the first setup may fail (ConfigEntryNotReady) and HA retries.
            model.clock.advance(6)
            entry = modbus_entry(hass, server.port, profile="A3C_2.7")
            assert await setup(hass, entry)
            rt = entry.runtime_data
            await wait_for(lambda: rt.station.snapshot.time is not None, 10)
            assert server.accepts == 2 and rt.client.connections >= 1
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_probe_no_socket_when_entry_polls_hostname_vs_ip():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port)
            assert await setup(hass, entry)
            rt = entry.runtime_data
            await wait_for(lambda: rt.station.snapshot.time is not None, 10)
            accepts = server.accepts
            flow, result = await through_address(hass, server.port, host="localhost")
            assert result == {**result, "type": "abort", "reason": "address_in_use"}
            assert server.accepts == accepts
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_duplicate_modbus_entry_refused_and_socket_closed():
    async def main():
        model, server = await start_fake()
        hass = await make_hass()
        try:
            modbus_entry(hass, 1, host="192.0.2.1")  # same serial, another (old) address, not loaded
            flow, result = await through_address(hass, server.port)
            assert result["type"] == "abort" and result["reason"] == "already_configured"
            await wait_for(lambda: server.open_connections == 0, 3)
            assert "probe" not in mb_runtime.modbus_data(hass)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_probe_errors_single_failure_generic_then_cause_texts():
    async def main():
        model, server = await start_fake()
        await server.close_port()
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result["errors"] == {"base": "mb_no_answer"}  # one refusal proves nothing
            assert server.accepts == 0
            key = f"127.0.0.1:{server.port}"
            now = time.monotonic()
            mb_runtime.modbus_data(hass)["probe_failures"][key] = [now - 400, now - 200]
            result = await flow.async_step_modbus_address({"host": "127.0.0.1", "port": server.port})
            assert result["errors"] == {"base": "mb_refused"}
            result = await flow.async_step_modbus_address({"host": "", "port": server.port})
            assert result["errors"] == {"host": "mb_host_empty"}
            result = await flow.async_step_modbus_address({"host": "no-such-host.invalid", "port": 502})
            assert result["errors"] == {"base": "mb_cannot_resolve"}
        finally:
            await server.stop()
            await stop(hass)

    run(main())


def test_probe_partly_zero_serial_is_not_ready():
    """zeros straddling the three serial reads are "not ready",
    not a serial (7 zero replies: [0,0,0], [0,0,0], [0,x,x])."""

    async def main():
        model, server = await start_fake()
        model._zero_reads = 7
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result["errors"] == {"base": "mb_not_ready"}, result
        finally:
            await server.stop()
            await stop(hass)

    run(main())


@pytest.mark.parametrize(("words", "error"), [([0, 0, 0], "mb_not_ready"), ([0xABCD, 0x3456, 0x7891], "mb_not_gnlt")])
def test_probe_not_ready_and_not_gnlt(words, error):
    async def main():
        model, server = await start_fake()
        model.overrides.update(dict(zip(C.SERIAL_GROUP.used, words)))  # 38/40/42
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result["errors"] == {"base": error}
            assert server.accepts == 1
            await wait_for(lambda: server.open_connections == 0, 3)
        finally:
            await server.stop()
            await stop(hass)

    run(main())


# --- options ------------------------------------------------------------------


def test_options_flow_fields():
    async def main():
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, 502, host="192.0.2.10")
            flow = options_flow(hass, entry)
            assert isinstance(flow, mbcf.ModbusOptionsFlow)
            result = await flow.async_step_init()
            assert result["step_id"] == "modbus"
            fields = {str(k) for k in result["data_schema"].schema}
            assert fields == {
                "preset", "host", "port", "auto_start", "resume_after_power_loss", "price", "night_price",
                "night_start", "night_end", "energy_source", "meter_interval",
            }
            defaults = {str(k): k.default() for k in result["data_schema"].schema}
            assert defaults["energy_source"] == "integral"
            base = {k: v for k, v in defaults.items()}
            # No "phase switching while charging" option, no profile.
            result = await flow.async_step_modbus(base)
            assert result["type"] == "create_entry"
            assert "profile" not in result["data"] and "phase_switch_while_charging" not in result["data"]
            assert "host" not in result["data"] and "port" not in result["data"]
            # The same fields again ("skip 10.1-15.9 A" removed).
            hass.config_entries.async_update_entry(entry, options=result["data"])
            result = await options_flow(hass, entry).async_step_init()
            defaults = {str(k): k.default() for k in result["data_schema"].schema}
            assert "skip_10_16" not in defaults and "phase_switch_while_charging" not in defaults
        finally:
            await stop(hass)

    run(main())


def test_reconfigure_same_peer_no_reload_other_serial_refused():
    async def main():
        model, server = await start_fake()
        other_model, other = await start_fake(serial=OTHER)
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port)
            assert await setup(hass, entry)
            rt = entry.runtime_data
            await wait_for(lambda: rt.station.snapshot.time is not None, 10)
            defaults = {str(k): k.default() for k in (await options_flow(hass, entry).async_step_init())["data_schema"].schema}
            # Same station by name: kept on the running socket, no reload.
            result = await options_flow(hass, entry).async_step_modbus({**defaults, "host": "localhost"})
            assert result["type"] == "create_entry"
            await hass.async_block_till_done()
            assert entry.data["host"] == "localhost" and entry.runtime_data is rt
            assert hass.data["_test_reloads"] == [] and server.accepts == 1
            # Another station answers at the new address: refused; the runtime was closed first.
            started = time.monotonic()
            result = await options_flow(hass, entry).async_step_modbus({**defaults, "host": "127.0.0.1", "port": other.port})
            assert time.monotonic() - started < 5.0  # no reload gap after the close any more (evcc)
            assert result["errors"] == {"base": "mb_serial_mismatch"}
            assert server.open_connections == 0  # closed before the probe: never two sockets
            assert hass.data["_test_reloads"] == [entry.entry_id]  # back to the old address
            assert entry.data["port"] == server.port
            await unload(hass, entry)
        finally:
            await server.stop()
            await other.stop()
            await stop(hass)

    run(main())
