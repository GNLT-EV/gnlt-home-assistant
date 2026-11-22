"""Two charger entries set up at the same time (Home Assistant starts all
entries of one integration together): one shared OCPP server and one
DomainData, never a second bind of the same port."""

import asyncio
import socket
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

import gnlt_charger as integ


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SlowStore:
    """Store whose first load yields to the loop, like the real disk read."""

    def __init__(self, hass, version, key):
        pass

    async def async_load(self):
        await asyncio.sleep(0.01)
        return {}


def run_many(monkeypatch, n):
    monkeypatch.setattr(integ, "Store", SlowStore)
    hass = SimpleNamespace(data={}, config=SimpleNamespace(time_zone="Europe/Warsaw", country="PL"))
    port = free_port()

    async def main():
        results = await asyncio.gather(
            *(integ.async_ensure_server(hass, port) for _ in range(n)),
            return_exceptions=True,
        )
        data = hass.data.get(integ.DOMAIN)
        if data is not None and data.server is not None:
            await data.server.stop()
        for r in results:
            if isinstance(r, integ.DomainData) and r is not data and r.server is not None:
                await r.server.stop()
        return results, data

    return asyncio.run(main())


def check(results, data):
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"a setup failed: {errors!r}"
    assert all(r is data for r in results)
    assert data.server is not None


def test_two_setups_share_one_server(monkeypatch):
    check(*run_many(monkeypatch, 2))


def test_ten_setups_share_one_server(monkeypatch):
    check(*run_many(monkeypatch, 10))


class DiskStore:
    """Store over one shared "disk" dict; load and save yield to the loop."""

    disk: dict = {}

    def __init__(self, hass, version, key):
        pass

    async def async_load(self):
        await asyncio.sleep(0.01)
        return dict(DiskStore.disk)

    async def async_save(self, data):
        await asyncio.sleep(0)
        DiskStore.disk = dict(data)

    def async_delay_save(self, data_func, delay):
        DiskStore.disk = dict(data_func())


def run_remove_with_setup(monkeypatch, remove_first):
    """The last entry was unloaded and removed (shared data already dropped)
    while another entry sets up, e.g. a removal during a restart."""
    monkeypatch.setattr(integ, "Store", DiskStore)
    DiskStore.disk = {"A": {"kwh": 1}, "B": {"kwh": 2}}
    hass = SimpleNamespace(data={}, config=SimpleNamespace(time_zone="Europe/Warsaw", country="PL"))
    port = free_port()
    entry = SimpleNamespace(data={integ.CONF_IDENTITY: "A"})

    async def main():
        remove = integ.async_remove_entry(hass, entry)
        setup = integ.async_ensure_server(hass, port)
        await asyncio.gather(*((remove, setup) if remove_first else (setup, remove)))
        data = hass.data[integ.DOMAIN]
        await data.server.stop()
        return data

    return asyncio.run(main())


def check_removed(data):
    # The live copy is saved again on the next counter change: if it still
    # holds the removed charger, its counters come back.
    assert "A" not in data.stored, "removed charger is back in the shared data"
    assert "A" not in DiskStore.disk
    assert "B" in data.stored


def test_remove_while_setup_starts(monkeypatch):
    check_removed(run_remove_with_setup(monkeypatch, remove_first=True))


def test_setup_while_remove_starts(monkeypatch):
    check_removed(run_remove_with_setup(monkeypatch, remove_first=False))


class FakeServer:
    port = 9000
    stopped = False

    async def stop(self):
        self.stopped = True


def test_unload_last_entry_while_a_new_charger_connects():
    """A charger connects while the last entry is unloading: the server still
    listens while the known chargers close their sockets one by one."""
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(time_zone="Europe/Warsaw", country="PL"),
    )

    async def unload_platforms(entry, platforms):
        return True

    hass.config_entries = SimpleNamespace(async_unload_platforms=unload_platforms)
    DiskStore.disk = {}
    server = FakeServer()
    data = integ.DomainData(store=DiskStore(hass, 1, "k"), stored={}, server=server, entries=1)
    hass.data[integ.DOMAIN] = data
    connect = integ._charger_factory(hass, data)

    class FakeWs:
        closed = False

        async def close(self, **kwargs):
            self.closed = True

    late_ws = FakeWs()

    class ClosingCharger:
        connected = True

        async def shutdown(self):
            await asyncio.sleep(0)  # ws.close() waits for the charger's answer
            late = connect("NEW-CHARGER")  # what OcppServer._handle does on a connect
            late._ws = late_ws  # ...and Charger.run() takes the socket: it is live
            self.connected = False

    data.chargers["A"] = ClosingCharger()
    data.chargers["B"] = ClosingCharger()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(adopted=True))

    async def main():
        return await integ.async_unload_entry(hass, entry)

    assert asyncio.run(main()) is True
    assert late_ws.closed, "the charger that connected during the loop was left open"
    assert server.stopped
    assert integ.DOMAIN not in hass.data


def ha_stub(**config_entries):
    """Just enough of hass for the shared server: every identity is known,
    so a connect offers no discovery."""
    return SimpleNamespace(
        data={},
        config=SimpleNamespace(time_zone="Europe/Warsaw", country="PL"),
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [SimpleNamespace(unique_id=i) for i in ("A", "B", "NEW")],
            **config_entries,
        ),
    )


@pytest.mark.parametrize("late", ["new charger", "same charger reconnects"])
def test_unload_last_entry_with_late_connection_real_server(monkeypatch, late):
    """Real OcppServer: a socket opened while the last entry unloads must not
    keep the server's cleanup waiting (aiohttp waits 60 s + 60 s for open
    handlers, with the setup lock held)."""
    monkeypatch.setattr(integ, "Store", DiskStore)
    DiskStore.disk = {}

    async def unload_platforms(entry, platforms):
        return True

    hass = ha_stub(async_unload_platforms=unload_platforms)
    port = free_port()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(adopted=True))

    async def main():
        data = await integ.async_ensure_server(hass, port)
        data.entries = 1
        session = aiohttp.ClientSession()
        drains = []

        async def connect(identity):
            try:
                ws = await session.ws_connect(f"http://127.0.0.1:{port}/ocpp/{identity}", protocols=("ocpp1.6",))
            except aiohttp.ClientError:
                return False  # refused: the server no longer listens
            drains.append(asyncio.ensure_future(ws.receive()))
            for _ in range(200):
                if identity in data.chargers and data.chargers[identity].connected:
                    return True
                await asyncio.sleep(0.01)
            return True

        assert await connect("A")
        a = data.chargers["A"]
        own_shutdown = a.shutdown

        async def shutdown():
            if late == "new charger":
                await connect("NEW")
                await own_shutdown()
            else:
                await own_shutdown()
                await connect("A")

        a.shutdown = shutdown
        started = time.monotonic()
        try:
            ok = await asyncio.wait_for(integ.async_unload_entry(hass, entry), 10)
        finally:
            for d in drains:
                d.cancel()
            await session.close()
        return ok, time.monotonic() - started

    ok, took = asyncio.run(main())
    assert ok is True
    assert took < 5, f"unload took {took:.1f} s"
    assert integ.DOMAIN not in hass.data


def test_removed_charger_still_online_does_not_save_back(monkeypatch):
    """Entries A and B loaded; A unloaded and removed while its charger is
    still online. Its next message must not bring the old counters back."""
    monkeypatch.setattr(integ, "Store", DiskStore)
    DiskStore.disk = {"A": {"tx_counter": 7}, "B": {"tx_counter": 3}}
    hass = ha_stub()

    async def main():
        data = await integ._domain_data(hass)
        data.entries = 2
        connect = integ._charger_factory(hass, data)
        a = connect("A")
        connect("B")
        data.entries -= 1  # entry A unloaded, B keeps the server
        a.adopted = False
        await integ.async_remove_entry(hass, SimpleNamespace(data={integ.CONF_IDENTITY: "A"}))
        a._persist()  # next StatusNotification / MeterValues of charger A
        return data

    data = asyncio.run(main())
    assert "A" not in DiskStore.disk, f"removed charger saved back: {DiskStore.disk.get('A')}"
    assert "A" not in data.stored
    assert "B" in DiskStore.disk


def setup_stubs(monkeypatch):
    monkeypatch.setattr(integ, "Store", DiskStore)
    monkeypatch.setattr(integ.notifications, "watch", lambda *a, **k: (lambda: None))
    monkeypatch.setattr(integ, "async_track_time_change", lambda *a, **k: (lambda: None))

    async def forward(entry, platforms):
        return None

    return ha_stub(async_forward_entry_setups=forward)


def gnlt_entry(identity, port):
    return SimpleNamespace(
        data={integ.CONF_IDENTITY: identity, integ.CONF_PORT: port},
        options={},
        title=identity,
        runtime_data=None,
        async_on_unload=lambda f: None,
        add_update_listener=lambda f: (lambda: None),
    )


def test_failed_setup_does_not_keep_the_count(monkeypatch):
    """A stored record the charger cannot read fails setup; Home Assistant
    never unloads a failed entry, so it must not stay counted."""
    hass = setup_stubs(monkeypatch)
    DiskStore.disk = {"A": {"tx_counter": "broken"}}
    port = free_port()

    async def main():
        try:
            await integ.async_setup_entry(hass, gnlt_entry("A", port))
        except ValueError:
            pass
        else:
            raise AssertionError("setup with a broken record did not fail")
        data = hass.data[integ.DOMAIN]
        await data.server.stop()
        return data

    data = asyncio.run(main())
    assert data.entries == 0, f"failed setup left the count at {data.entries}"


def test_two_setup_entries_at_once(monkeypatch):
    """async_setup_entry itself, two at once as at Home Assistant start."""
    hass = setup_stubs(monkeypatch)
    DiskStore.disk = {}
    port = free_port()
    entries = [gnlt_entry("A", port), gnlt_entry("B", port)]

    async def main():
        results = await asyncio.gather(*(integ.async_setup_entry(hass, e) for e in entries), return_exceptions=True)
        data = hass.data.get(integ.DOMAIN)
        servers = {id(r.server): r.server for r in [data] if r and r.server}
        for s in servers.values():
            await s.stop()
        return results, data

    results, data = asyncio.run(main())
    assert results == [True, True], f"a setup failed: {results!r}"
    assert data.entries == 2
    assert all(e.runtime_data is data.chargers[e.data[integ.CONF_IDENTITY]] for e in entries)


def unload_stub():
    async def unload_platforms(entry, platforms):
        return True

    return ha_stub(async_unload_platforms=unload_platforms)


def run_unload_with_slow_handshake(monkeypatch, delay):
    """The last entry unloads while a request is still in prepare() for
    `delay` seconds when the listener closes."""
    monkeypatch.setattr(integ, "Store", DiskStore)
    DiskStore.disk = {}
    own_prepare = web.WebSocketResponse.prepare
    slow = {"on": False}

    async def prepare(self, request):
        if slow["on"]:
            await asyncio.sleep(delay)
        return await own_prepare(self, request)

    monkeypatch.setattr(web.WebSocketResponse, "prepare", prepare)
    hass = unload_stub()
    port = free_port()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(adopted=True))

    async def main():
        data = await integ.async_ensure_server(hass, port)
        data.entries = 1
        session = aiohttp.ClientSession()
        slow["on"] = True

        async def late():
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ocpp/NEW", protocols=("ocpp1.6",))
            await ws.receive()

        client = asyncio.ensure_future(late())
        await asyncio.sleep(0.1)  # the request is in the handler, prepare() sleeps
        started = time.monotonic()
        try:
            ok = await asyncio.wait_for(integ.async_unload_entry(hass, entry), 20)
        finally:
            client.cancel()
            await session.close()
        return ok, time.monotonic() - started

    return asyncio.run(main())


def test_unload_last_entry_with_late_handshake_real_server(monkeypatch):
    """The request reaches Charger.run() after the last scan for open
    sockets: it must still be closed, not left to aiohttp's shutdown timeout
    (60 s + 60 s by default) with the setup lock held."""
    ok, took = run_unload_with_slow_handshake(monkeypatch, 0.3)
    assert ok is True
    assert took < 3, f"unload took {took:.1f} s"


def test_unload_last_entry_with_stuck_handshake_real_server(monkeypatch):
    """A handler that never reaches a charger cannot be closed by us: only
    the server's shutdown timeout bounds the unload (two phases of it)."""
    ok, took = run_unload_with_slow_handshake(monkeypatch, 60)
    assert ok is True
    assert took < 12, f"unload took {took:.1f} s"


class FailingSaveStore(DiskStore):
    async def async_save(self, data):
        raise OSError("disk full")


def test_failed_last_unload_leaves_no_dead_server(monkeypatch):
    """The save at the end of the last unload fails: the next setup must
    start a new server, not reuse the stopped one."""
    monkeypatch.setattr(integ, "Store", FailingSaveStore)
    DiskStore.disk = {}
    hass = unload_stub()
    port = free_port()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(adopted=True))

    async def main():
        old = await integ.async_ensure_server(hass, port)
        old.entries = 1
        stopped = old.server
        try:
            await integ.async_unload_entry(hass, entry)
        except OSError:
            pass
        monkeypatch.setattr(integ, "Store", DiskStore)
        new = await integ.async_ensure_server(hass, port)
        server = new.server
        await server.stop()
        return stopped, server

    stopped, server = asyncio.run(main())
    assert server is not stopped, "the next setup reused the stopped server"


def test_setup_failing_after_the_count_undoes_it(monkeypatch):
    """Platforms fail to set up after the entry was counted: Home Assistant
    never unloads a failed entry, so the count and the adopt are undone."""
    hass = setup_stubs(monkeypatch)
    DiskStore.disk = {}

    async def forward(entry, platforms):
        raise RuntimeError("platform failed")

    hass.config_entries.async_forward_entry_setups = forward
    port = free_port()

    async def main():
        try:
            await integ.async_setup_entry(hass, gnlt_entry("A", port))
        except RuntimeError:
            pass
        else:
            raise AssertionError("setup did not fail")
        data = hass.data[integ.DOMAIN]
        await data.server.stop()
        return data

    data = asyncio.run(main())
    assert data.entries == 0, f"failed setup left the count at {data.entries}"
    assert data.chargers["A"].adopted is False
