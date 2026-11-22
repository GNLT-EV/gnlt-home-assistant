"""Diagnostics download of a ModBus entry:."""

import asyncio
import json

import gnlt_charger.diagnostics as diag
from homeassistant.helpers import device_registry as dr
from modbus_fake import FakeStation, FakeTcpStation, SimClock
from modbus_ha import SERIAL, make_hass, modbus_entry, setup, stop, unload, wait_for


def test_diagnostics_content_redacts_host_never_opens_socket():
    async def main():
        model = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True))
        server = FakeTcpStation(model)
        await server.start()
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, server.port, host="localhost")
            assert await setup(hass, entry)
            rt = entry.runtime_data
            await wait_for(lambda: rt.station.snapshot.time is not None, 10)
            rt.station.last_failure_text = f"connect to localhost:{server.port}: refused (127.0.0.1)"
            accepts, requests = server.accepts, len(model.requests)
            data = await diag.async_get_config_entry_diagnostics(hass, entry)
            device = next(iter(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)))
            assert await diag.async_get_device_diagnostics(hass, entry, device) == data
            assert server.accepts == accepts and len(model.requests) == requests  # no socket, no request
            text = json.dumps(data, default=str)
            assert "localhost" not in text and "127.0.0.1" not in text
            assert data["entry"]["data"]["host"] == "**REDACTED**"
            assert SERIAL in text and data["station"]["serial"] == SERIAL  # serial kept
            assert data["transport"] == "modbus"
            st = data["station"]
            for key in ("link", "registers_raw", "decoded", "learned", "desired", "problems", "write_log", "transactions"):
                assert key in st, key
            assert {"consecutive_failures", "errors_24h", "last_failure"} <= set(st["link"])
            assert st["registers_raw"]  # last poll, raw
            assert {"source", "record", "total_wh", "totals", "tariff"} <= set(data["energy"])
            assert {"options", "would_do"} <= set(data["commands"]) and "rate_limits" not in data["commands"]
            assert "profile" not in data  # no firmware profiles
            await unload(hass, entry)
        finally:
            await server.stop()
            await stop(hass)

    asyncio.run(main())
