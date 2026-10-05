"""A fake GNLT charger for the tests: talks OCPP 1.6J to the real server.

It answers every command the way the EVB11B does (Accepted, GetConfiguration
with the keys of the live charger) and lets a test send the charger's own
messages in the exact form the live charger uses.
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime
from typing import Any

import aiohttp

from gnlt_charger import charger as charger_mod
from gnlt_charger.charger import Charger, ChargerSettings
from gnlt_charger.server import OcppServer

LIVE_CONFIG = [
    {"key": "MeterValueSampleInterval", "readonly": False, "value": "10"},
    {"key": "HeartbeatInterval", "readonly": False, "value": "300"},
    {"key": "ChargeRate", "readonly": False, "value": "10"},
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fast(monkeypatch) -> None:
    """Pauses that matter for a live charger, not for a fake one."""
    monkeypatch.setattr(charger_mod, "WIRE_GAP_S", 0.0)
    monkeypatch.setattr(charger_mod, "COMMAND_GAP_S", 0.0)
    monkeypatch.setattr(charger_mod, "STATUS_AFTER_STOP_S", 0.2)
    monkeypatch.setattr(charger_mod, "EMPTY_SESSION_S", 3600)


def ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def meter(energy_wh: float, currents: dict[str, float], voltage: float = 230.0, power_w: Any = "sum") -> list[dict]:
    """sampledValue list like the live charger: total power without a phase,
    voltage and current per phase. ``power_w="phases"`` sends power per phase
    instead of the total (other firmware)."""
    samples: list[dict] = [
        {"value": str(energy_wh), "context": "Sample.Periodic", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}
    ]
    total = sum(voltage * a for a in currents.values())
    if power_w == "phases":
        for ph, a in currents.items():
            samples.append({"value": str(voltage * a), "measurand": "Power.Active.Import", "unit": "W", "phase": ph})
    else:
        samples.append({"value": str(total if power_w == "sum" else power_w), "measurand": "Power.Active.Import", "unit": "W"})
    for ph in ("L1", "L2", "L3"):
        samples.append({"value": str(voltage if ph in currents else 0), "measurand": "Voltage", "unit": "V", "phase": ph})
        samples.append({"value": str(currents.get(ph, 0.0)), "measurand": "Current.Import", "unit": "A", "phase": ph})
    return samples


class FakeStation:
    def __init__(self, port: int, identity: str = "102512111615") -> None:
        self.url = f"ws://127.0.0.1:{port}/ocpp/{identity}"
        self.received: list[list[Any]] = []
        self._answers: dict[str, asyncio.Future] = {}
        self._n = 0
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self.config = list(LIVE_CONFIG)

    async def connect(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url, protocols=("ocpp1.6",))
        self._reader = asyncio.ensure_future(self._read())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()

    async def _read(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            if frame[0] == 2:
                self.received.append(frame)
                await self.ws.send_str(json.dumps([3, frame[1], self.answer(frame[2], frame[3])]))
            elif frame[0] in (3, 4):
                fut = self._answers.pop(frame[1], None)
                if fut and not fut.done():
                    fut.set_result(frame)

    def answer(self, action: str, payload: dict) -> dict:
        if action == "GetConfiguration":
            return {"configurationKey": self.config}
        return {"status": "Accepted"}

    async def send(self, action: str, payload: dict) -> list[Any]:
        self._n += 1
        msg_id = f"st{self._n}"
        fut = asyncio.get_running_loop().create_future()
        self._answers[msg_id] = fut
        await self.ws.send_str(json.dumps([2, msg_id, action, payload]))
        return await asyncio.wait_for(fut, 5)

    async def status(self, status: str, error: str = "NoError", vendor: str | None = None) -> None:
        p = {"connectorId": 1, "errorCode": error, "status": status, "timestamp": ts()}
        if vendor:
            p["vendorErrorCode"] = vendor
        await self.send("StatusNotification", p)

    async def meter(self, samples: list[dict]) -> None:
        await self.send("MeterValues", {"connectorId": 1, "meterValue": [{"timestamp": ts(), "sampledValue": samples}]})

    def actions(self) -> list[str]:
        return [f[2] for f in self.received]


class Bench:
    """Server + one adopted charger + one fake station, as in Home Assistant."""

    def __init__(self, settings: ChargerSettings | None = None, stored: dict | None = None) -> None:
        self.port = free_port()
        self.saved: dict = {}
        self.charger: Charger | None = None
        self.settings = settings or ChargerSettings(max_current_a=16, phases=3)
        self.stored = stored

    def charger_for(self, identity: str) -> Charger:
        if self.charger is None:
            self.charger = Charger(identity, "Europe/Warsaw", self.stored, self.saved.update, "PL")
            self.charger.adopt(self.settings)
        return self.charger

    async def __aenter__(self) -> tuple[Charger, FakeStation]:
        self.server = OcppServer(self.port, self.charger_for, host="127.0.0.1")
        await self.server.start()
        self.station = FakeStation(self.port)
        await self.station.connect()
        await self.station.send(
            "BootNotification",
            {"chargePointModel": "Home OCPP", "chargePointVendor": "AEFA", "firmwareVersion": "SW:A3B_2.7-HW:B07_0.4"},
        )
        await asyncio.sleep(0.2)
        return self.charger, self.station

    async def __aexit__(self, *exc) -> None:
        await self.station.close()
        await self.charger.shutdown()
        await self.server.stop()


async def wait_for(cond, timeout: float = 3.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()
