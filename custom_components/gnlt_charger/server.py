"""OCPP 1.6J WebSocket server for GNLT chargers inside Home Assistant.

Its own small aiohttp server on a separate port (9000 by default) rather than
a view on Home Assistant's port: the charger speaks only plain ``ws://`` (its
firmware carries no root certificates), while Home Assistant often runs behind
HTTPS, and a view there would sit under Home Assistant's authentication.

The charger joins the address it was given with its ChargeID, so any path
works: ``ws://<ha>:9000/ocpp/302511017394`` and ``ws://<ha>:9000/302511017394``
both reach the charger ``302511017394``.

‼️ This port must stay inside the home network. OCPP 1.6 chargers have no
password here, and anybody who reaches the port can pose as a charger.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohttp import web

from .charger import Charger

_LOGGER = logging.getLogger(__name__)

SUBPROTOCOL = "ocpp1.6"


class OcppServer:
    def __init__(
        self,
        port: int,
        charger_for: Callable[[str], Charger],
        on_connect: Callable[[str], None] | None = None,
        host: str = "0.0.0.0",
    ) -> None:
        self.port = port
        self.host = host
        self._charger_for = charger_for
        self._on_connect = on_connect
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        _LOGGER.info("GNLT OCPP server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @staticmethod
    def identity_from_path(path: str) -> str | None:
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        identity = self.identity_from_path(request.path)
        if not identity or len(identity) > 48:
            return web.Response(status=404, text="charger identity missing in the path")
        ws = web.WebSocketResponse(
            protocols=(SUBPROTOCOL,),
            # Liveness is judged by the charger module (any incoming frame),
            # never by a single missed pong - see Charger._keepalive.
            autoping=False,
            heartbeat=None,
            max_msg_size=1024 * 1024,
        )
        if not ws.can_prepare(request).ok:
            return web.Response(status=426, text="WebSocket (OCPP 1.6J) expected")
        await ws.prepare(request)
        _LOGGER.info("charger %s connected from %s", identity, request.remote)
        charger = self._charger_for(identity)
        if self._on_connect:
            self._on_connect(identity)
        try:
            await charger.run(ws)
        finally:
            _LOGGER.info("charger %s disconnected", identity)
        return ws
