"""One GNLT EVE/EVB charger talking OCPP 1.6J to Home Assistant.

This module is plain asyncio + aiohttp - no Home Assistant imports - so the
whole OCPP conversation can be exercised by a fake charger in the tests. Home
Assistant attaches to a :class:`Charger` through ``add_listener`` and the
command methods.

What differs from a textbook OCPP central system, and why (details in the
GNLT repository, ``CLAUDE.md`` -> "Нестандартная логика"):

* current is regulated with the vendor key ``ChargeRate`` (amps) through
  ``ChangeConfiguration`` - ``SetChargingProfile`` answers ``NotSupported``;
  it applies on the fly, in a running charge, within 6-8 s;
* ``"transactionId":,`` frames are repaired (``logic.parse_frame``);
* a charge the charger started by itself has no transaction id; it is shown,
  and stopped by the last known id or, explicitly, by taking the connector out
  of service for ten minutes;
* a start from ``Finishing`` is allowed, but a start the car did not take (the
  connector stays in ``Finishing``) is detected and cleaned up;
* the connection is dead only after 135 s of COMPLETE silence - dropping it on
  a missed pong made the firmware hang until unplugged;
* the clock is sent as local time with offset;
* the energy counter is blink-proof (``logic.EnergyCounter``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import WSMsgType, web

from .logic import (
    CABLE_STATUSES,
    CALL,
    CALLERROR,
    CALLRESULT,
    EnergyCounter,
    FrameError,
    Schedule,
    Tariff,
    Totals,
    extract_metrics,
    iso_in_zone,
    parse_frame,
    parse_ts,
    plugged_idle,
    station_timestamp,
    supports_resume_after_power_loss,
)

_LOGGER = logging.getLogger(__name__)

PING_INTERVAL_S = 45
# More than two ping cycles and less than the heartbeat interval (300 s): an
# idle charger sends nothing but a heartbeat.
DEAD_AFTER_S = 135
HEARTBEAT_INTERVAL_S = 300
CALL_TIMEOUT_S = 30
# Without a pause between ChangeConfiguration and RemoteStart the charger
# loses the second command (live case 26.07.2026).
COMMAND_GAP_S = 2
AUTO_START_COOLDOWN_S = 180
# The charger remembers its transaction through a reboot and accepts a stop by
# a number we already closed - but not for ever.
LAST_TX_MAX_AGE = timedelta(hours=3)
FORCED_RETURN_AFTER = timedelta(minutes=10)
# How long a fresh transaction may sit in Finishing before we call it empty.
# The car answers a start within seconds; three minutes is generous.
EMPTY_SESSION_S = 180
# A fresh transaction in one of these, with no energy, is a start the car did
# not take.
EMPTY_SESSION_STATUSES = frozenset({"Preparing", "Finishing"})
METER_STALE_S = 90
ID_TAG = "HomeAssistant"


class ChargerError(Exception):
    """A command the charger refused or could not take.

    ``code`` is a translation key (``exceptions`` section of strings.json).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class ChargerSettings:
    """What the owner chose in Home Assistant."""

    max_current_a: int = 16
    phases: int = 1
    auto_start: bool = False
    resume_after_power_loss: bool = False
    meter_interval_s: int = 10
    schedule: Schedule = field(default_factory=Schedule)
    tariff: Tariff = field(default_factory=Tariff)


def _now() -> datetime:
    return datetime.now(UTC)


class Charger:
    def __init__(
        self,
        identity: str,
        tz_name: str,
        stored: dict[str, Any] | None = None,
        save: Callable[[dict[str, Any]], None] | None = None,
        country: str | None = None,
    ) -> None:
        self.identity = identity
        self.tz_name = tz_name
        # Country of Home Assistant: the model letters differ between markets.
        self.country = country
        self.settings = ChargerSettings()
        self.adopted = False
        self._save_cb = save
        self._listeners: list[Callable[[], None]] = []

        stored = stored or {}
        self.energy = EnergyCounter.from_dict(stored.get("energy"))
        self.desired_current_a: int | None = stored.get("desired_current_a")
        self._tx_counter: int = int(stored.get("tx_counter", 0))
        self.last_tx_id: int | None = stored.get("last_tx_id")
        self.last_tx_at: datetime | None = parse_ts(stored.get("last_tx_at"))
        self.forced_inoperative_at: datetime | None = parse_ts(stored.get("forced_inoperative_at"))
        self.pending_resume: bool = bool(stored.get("pending_resume", False))
        self.tx_id: int | None = stored.get("tx_id")
        self.tx_meter_start: float | None = stored.get("tx_meter_start")
        self.tx_started_at: datetime | None = parse_ts(stored.get("tx_started_at"))

        # identity of the unit
        self.vendor: str | None = stored.get("vendor")
        self.model: str | None = stored.get("model")
        self.firmware: str | None = stored.get("firmware")

        # live state
        self._ws: web.WebSocketResponse | None = None
        self.connected_since: datetime | None = None
        self.last_seen: datetime | None = None
        self._last_rx_mono = 0.0
        self.status: str | None = None
        self.previous_status: str | None = None
        self.error_code: str | None = None
        self.vendor_error: str | None = None
        self.config: dict[str, str] = {}
        self.power_w: float | None = None
        self.current: dict[str, float] = {}
        self.voltage: dict[str, float] = {}
        self.temperature: float | None = None
        self.soc: float | None = None
        self.session_wh: float | None = stored.get("session_wh")
        # Money and statistics, in the currency of Home Assistant.
        self.session_cost: float = float(stored.get("session_cost") or 0.0)
        self.totals = Totals.from_dict(stored.get("totals"))
        self.last_session: dict[str, Any] | None = stored.get("last_session")
        # None until the first tick: a Home Assistant restart inside an open window
        # is not the window opening.
        self._schedule_was_open: bool | None = None
        self.last_meter_at: datetime | None = None

        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._call_lock = asyncio.Lock()
        # When the charger last answered one of our commands (monotonic clock).
        self._last_answer_mono = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_auto_start = 0.0
        # A start the car did not take: the connector stayed in Finishing.
        self.empty_session = False
        # The charger sends StopTransaction about a second BEFORE the status
        # Finishing (partner's bench, 25.09.2026). In that second the status is
        # still Charging and no session is open - "charging without a session"
        # would flash at every stop, with a false "problem" in the logbook.
        self._stop_pending_status = False
        # StopTransaction came with a number other than the open one; the
        # session is closed by the status that follows (see _on_StopTransaction).
        self._foreign_stop = False
        # The open session reached Charging at least once: it is not "empty",
        # even at 0 Wh (a short charge stays under the 10 Wh meter step).
        self._tx_charged = False
        # Reserved / Unavailable / Faulted say nothing about the cable: the
        # cable sensor keeps its last known state through them.
        self._cable = False
        # The number the charger is charging under, from its MeterValues. It is
        # there even in a charge the charger started by itself (partner's bench,
        # 25.09.2026: 20 and 22 while HA had no open session).
        self.station_tx_id: int | None = None

    # --- plumbing -------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def charging_without_session(self) -> bool:
        """The charger charges but has no transaction we could address."""
        return self.status == "Charging" and self.tx_id is None and not self._stop_pending_status

    @property
    def cable_connected(self) -> bool:
        return self._cable

    @property
    def plug_and_charge_supported(self) -> bool:
        """``FreeCharging`` over OCPP exists from 2.9 - check the key itself,
        not the version: the charger's answer is the only reliable proof."""
        return "FreeCharging" in self.config

    @property
    def plug_and_charge(self) -> bool | None:
        v = self.config.get("FreeCharging")
        return None if v is None else v.strip().lower() in ("true", "1", "on")

    @property
    def resume_supported(self) -> bool:
        return supports_resume_after_power_loss(self.firmware)

    @property
    def charge_rate_a(self) -> int | None:
        try:
            return int(float(self.config["ChargeRate"]))
        except (KeyError, ValueError):
            return None

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001 - a broken entity must not break OCPP
                _LOGGER.exception("listener failed")

    def _persist(self) -> None:
        if not self._save_cb:
            return
        self._save_cb(
            {
                "energy": self.energy.as_dict(),
                "desired_current_a": self.desired_current_a,
                "tx_counter": self._tx_counter,
                "last_tx_id": self.last_tx_id,
                "last_tx_at": self.last_tx_at.isoformat() if self.last_tx_at else None,
                "forced_inoperative_at": self.forced_inoperative_at.isoformat() if self.forced_inoperative_at else None,
                "pending_resume": self.pending_resume,
                "tx_id": self.tx_id,
                "tx_meter_start": self.tx_meter_start,
                "tx_started_at": self.tx_started_at.isoformat() if self.tx_started_at else None,
                "session_wh": self.session_wh,
                "session_cost": self.session_cost,
                "totals": self.totals.as_dict(),
                "last_session": self.last_session,
                "vendor": self.vendor,
                "model": self.model,
                "firmware": self.firmware,
            }
        )

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def adopt(self, settings: ChargerSettings) -> None:
        """Home Assistant took this charger under its care."""
        self.settings = settings
        self.adopted = True
        if self.connected:
            self._spawn(self._setup_after_boot())
        self._notify()

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    # --- connection -----------------------------------------------------------

    async def run(self, ws: web.WebSocketResponse) -> None:
        """Serve one WebSocket connection until it closes."""
        old = self._ws
        if old is not None and not old.closed:
            # On a home network a second connection means the charger lost the
            # first one (a half-dead TCP socket stays "open" for hours).
            _LOGGER.info("%s: new connection replaces the previous one", self.identity)
            await old.close(code=1000, message=b"replaced")
        self._ws = ws
        self.connected_since = _now()
        self._touch()
        self._notify()
        keepalive = asyncio.ensure_future(self._keepalive(ws))
        try:
            async for msg in ws:
                self._touch()
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type == WSMsgType.PING:
                    await ws.pong(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            keepalive.cancel()
            if self._ws is ws:
                self._ws = None
                self._on_disconnect()

    def _touch(self) -> None:
        self._last_rx_mono = time.monotonic()
        self.last_seen = _now()

    def _on_disconnect(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ChargerError("not_connected"))
        self._pending.clear()
        self._notify()

    async def _keepalive(self, ws: web.WebSocketResponse) -> None:
        """Ping every 45 s, but judge liveness by ANY incoming frame.

        The firmware does not always answer pings. Dropping the link on a
        missed pong cost a client two charges: after a few such drops the
        charger hung (Wi-Fi icon on, no OCPP) until unplugged.
        """
        try:
            while not ws.closed:
                await asyncio.sleep(PING_INTERVAL_S)
                silent = time.monotonic() - self._last_rx_mono
                if silent > DEAD_AFTER_S:
                    _LOGGER.info("%s: silent for %.0f s, closing", self.identity, silent)
                    await ws.close(code=1001, message=b"silent")
                    return
                try:
                    await ws.ping()
                except ConnectionError:
                    return
                await self._poll_meter_if_stale()
                await self._maybe_return_to_service()
        except asyncio.CancelledError:
            pass

    # --- incoming ---------------------------------------------------------------

    async def _on_text(self, raw: str) -> None:
        try:
            msg = parse_frame(raw)
        except FrameError:
            _LOGGER.warning("%s: unreadable frame %s", self.identity, raw[:200])
            return
        kind = msg[0]
        if kind == CALL and len(msg) >= 4:
            await self._on_call(msg[1], str(msg[2]), msg[3] if isinstance(msg[3], dict) else {})
        elif kind == CALLRESULT:
            fut = self._pending.pop(msg[1], None)
            if fut and not fut.done():
                fut.set_result(msg[2] if isinstance(msg[2], dict) else {})
        elif kind == CALLERROR:
            fut = self._pending.pop(msg[1], None)
            if fut and not fut.done():
                fut.set_exception(ChargerError("call_error", f"{msg[2]} {msg[3] if len(msg) > 3 else ''}"))

    async def _send(self, frame: list[Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ChargerError("not_connected")
        await ws.send_str(json.dumps(frame, separators=(",", ":")))

    async def _on_call(self, msg_id: str, action: str, payload: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{action}", None)
        if handler is None:
            await self._send([CALLERROR, msg_id, "NotImplemented", f"{action} is not supported", {}])
            return
        try:
            result, after = handler(payload)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("%s: %s handler failed", self.identity, action)
            await self._send([CALLERROR, msg_id, "InternalError", str(err)[:200], {}])
            return
        await self._send([CALLRESULT, msg_id, result])
        # Follow-up commands go AFTER the answer: the charger does not take a
        # request while it waits for our reply.
        if after is not None:
            self._spawn(after)
        self._notify()

    def _current_time(self) -> str:
        return iso_in_zone(_now(), self.tz_name)

    def _on_BootNotification(self, p: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        self.vendor = p.get("chargePointVendor") or self.vendor
        self.model = p.get("chargePointModel") or self.model
        self.firmware = p.get("firmwareVersion") or self.firmware
        if self.tx_id is not None:
            # A charge was open. After a reconnect it simply continues; after a
            # power cut it is gone. The next connector status tells which.
            self.pending_resume = True
        self._persist()
        return (
            {"status": "Accepted", "interval": HEARTBEAT_INTERVAL_S, "currentTime": self._current_time()},
            self._setup_after_boot(),
        )

    def _on_Heartbeat(self, _p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        return {"currentTime": self._current_time()}, None

    def _on_Authorize(self, _p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        # A home charger accepts any card - the factory cards from the box too.
        return {"idTagInfo": {"status": "Accepted"}}, None

    def _on_DataTransfer(self, _p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        return {"status": "UnknownVendorId"}, None

    def _on_FirmwareStatusNotification(self, _p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        return {}, None

    def _on_DiagnosticsStatusNotification(self, _p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        return {}, None

    def _on_StatusNotification(self, p: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        connector = p.get("connectorId", 1)
        status = p.get("status")
        error = p.get("errorCode")
        self.error_code = None if error in (None, "NoError") else error
        self.vendor_error = p.get("vendorErrorCode") or None
        if connector == 0 or not isinstance(status, str):
            return {}, None
        self._stop_pending_status = False
        if status in CABLE_STATUSES:
            self._cable = True
        elif status == "Available":
            self._cable = False
        if self._foreign_stop:
            self._foreign_stop = False
            if self.tx_id is not None and status not in ("Charging", "SuspendedEV", "SuspendedEVSE"):
                # The charger ended its only charge under another number: our
                # session is over (see _on_StopTransaction).
                self._finish_session(_now())
        if status == "Charging" and self.tx_id is not None:
            self._tx_charged = True
        if status != self.status:
            self.previous_status = self.status
            self.status = status
            if status == "Charging" and self.tx_id is None and self.previous_status != "Charging":
                # A charge the charger started by itself: no StartTransaction
                # will come, so this is where its money and count begin.
                self.session_cost = 0.0
                self.totals.add_session(self._local(_now()))
        if status in ("Available", "Charging", "SuspendedEV", "SuspendedEVSE"):
            # The cable was taken out, or the car took current after all: the
            # "car did not take the charge" state is over. NOT on Unavailable or
            # Preparing: those come from our own availability toggle while the
            # car still does not charge (the sensor went out after 1-5 s on the
            # partner's bench, 25.09.2026, and the person never saw it).
            self.empty_session = False
        if status != "Charging":
            # The charger stops sending readings outside a charge; stale power
            # on the dashboard would read as "still charging".
            self.power_w = 0.0
            self.current = {k: 0.0 for k in self.current}
        return {}, self._after_status(status)

    async def _after_status(self, status: str) -> None:
        if self.forced_inoperative_at is not None and status != "Unavailable":
            # The cable was unplugged - the state changed, the job is done.
            await self._return_to_service("status changed")
        if status in ("Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"):
            # Still charging, paused by the car or finished normally - the
            # charger simply reconnected (it sends BootNotification after every
            # Wi-Fi drop too). Nothing was interrupted.
            self.pending_resume = False
        elif status == "Available":
            if self.tx_id is not None:
                # The charger forgot the charge (no StopTransaction will come).
                self._close_tx()
            self.pending_resume = False
        elif status == "Preparing" and self.pending_resume:
            # Cable still in the car, no charge: from 2.8 this is how the
            # charger waits after the power came back.
            self.pending_resume = False
            if self.tx_id is not None:
                self._close_tx()
            if self.settings.resume_after_power_loss and self.resume_supported and self.adopted:
                _LOGGER.info("%s: resuming the charge interrupted by a power cut", self.identity)
                await self._auto_start()
                self._persist()
                return
        if self.adopted and self.tx_id is None and plugged_idle(status, self.previous_status):
            if self.settings.auto_start:
                await self._auto_start()
            elif self.settings.schedule.active(self._local(_now())):
                # The cable went in while the window is open: start now, not at
                # the next minute tick - a minute of waiting reads as "broken".
                await self._auto_start()
        self._persist()
        self._notify()

    def _on_StartTransaction(self, p: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        self._tx_counter += 1
        tx = self._tx_counter
        self.tx_id = tx
        self.tx_started_at = station_timestamp(p.get("timestamp"), _now())
        meter_start = p.get("meterStart")
        self.tx_meter_start = float(meter_start) if isinstance(meter_start, (int, float)) else None
        self.session_wh = 0.0
        self.session_cost = 0.0
        self.totals.add_session(self._local(_now()))
        self.energy.start_session(self.tx_meter_start)
        self.last_tx_id = tx
        self.last_tx_at = _now()
        self.pending_resume = False
        self.empty_session = False
        self._tx_charged = False
        self._foreign_stop = False
        self._persist()
        return {"idTagInfo": {"status": "Accepted"}, "transactionId": tx}, self._watch_start(tx)

    async def _watch_start(self, tx: int) -> None:
        """Catch a start the car did not take.

        After a finished charge, with the cable still in the car, the charger
        accepts a start and opens a transaction, and then it depends on the
        CAR. Often the current flows (the GNLT platform saw the opposite once,
        04.09.2026, and banned such starts - a test partner showed three out of
        four worked, 24.09.2026); sometimes - typically after a long idle, when
        the car has gone to sleep - no current flows.

        ‼️ The connector does NOT stay only in ``Finishing``. Live test of the
        partner, 25.09.2026: after two hours of idle the status was
        ``Preparing``, and a check for ``Finishing`` alone missed it. Car-side
        pauses (``SuspendedEV``) and station-side ones (``SuspendedEVSE``) are
        legitimate states and are left alone.
        We try the usual stop. If the charger refuses it (it usually does), the
        session is LEFT OPEN and the person is told to re-plug the cable - see
        _close_empty_session for why nothing more is done.
        """
        await asyncio.sleep(EMPTY_SESSION_S)
        if self.tx_id != tx or self.status not in EMPTY_SESSION_STATUSES or (self.session_wh or 0) > 0:
            return
        if self._tx_charged:
            # The charge went and ended (a short one may stay at 0 Wh): it is
            # not a start the car refused.
            return
        _LOGGER.warning("%s: the car did not take the charge (transaction %s)", self.identity, tx)
        self.empty_session = True
        self._notify()
        await self._close_empty_session(tx)

    async def _close_empty_session(self, tx: int) -> None:
        """Only the polite stop. 🔴 No availability toggle any more (0.2.3,
        owner's decision 25.09.2026).

        Until 0.2.2 a refused stop was followed by ChangeAvailability
        Inoperative -> Operative. The charger accepted it and closed the
        session, but the car still took no current, new starts were still
        rejected until the cable was re-plugged - and the partner's bench
        recorded eight times that the charger then STARTED CHARGING BY ITSELF,
        outside any transaction, as soon as the car woke up (25.09.2026). The
        GNLT platform dropped the same trick the same day.

        So the session stays open: if the car wakes up, the charge goes on under
        our number and is visible and stoppable; if the cable is taken out,
        ``Available`` closes it. Meanwhile the person sees "the car did not
        start charging - re-plug the cable".
        """
        try:
            res = await self.call("RemoteStopTransaction", {"transactionId": tx})
            if res.get("status") != "Accepted":
                _LOGGER.info("%s: empty session %s left open until the cable is re-plugged", self.identity, tx)
        except ChargerError as err:
            _LOGGER.warning("%s: could not stop the empty session %s: %s", self.identity, tx, err)
        self._notify()

    def _on_StopTransaction(self, p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        at = station_timestamp(p.get("timestamp"), _now())
        for mv in p.get("transactionData") or []:
            if isinstance(mv, dict):
                # "Transaction.Begin" is the reading at the START of the charge
                # (OCPP 1.6 ReadingContext) - this charger puts a 0 into every
                # StopTransaction. Taken as the current reading, it zeroed
                # "Energy of this charge" whenever no session was open (partner's
                # bench, 25.09.2026; the GNLT platform drew a drop to 0 at the
                # end of the curve for the same reason).
                samples = [
                    s
                    for s in mv.get("sampledValue") or []
                    if not (isinstance(s, dict) and s.get("context") == "Transaction.Begin")
                ]
                self._apply_meter(samples, station_timestamp(mv.get("timestamp"), _now()))
        meter_stop = p.get("meterStop")
        stop_wh = float(meter_stop) if isinstance(meter_stop, (int, float)) else None
        self._count(self.energy.finish(stop_wh, at), at)
        if stop_wh is not None and self.tx_meter_start is not None:
            self.session_wh = max(0.0, stop_wh - self.tx_meter_start)
        number = p.get("transactionId")
        if number in (None, self.tx_id):
            self._finish_session(at)
        elif self.tx_id is not None:
            # 🔴 The charger sometimes runs a charge under the number of an
            # EARLIER session, not the one we gave it (partner's bench,
            # 25.09.2026: after a quick card start-stop it lost StopTransaction
            # for 20 and ran the next card charge, given 21, as 20). Ignoring such
            # a stop left 21 open for ever: the switch stuck "on", a stop from HA
            # was rejected, and three minutes later the empty-session check
            # toggled the connector - after which the charger charged by itself.
            # The charger has one connector, so the stop is about its only
            # charge - but a late retry of an old stop must not end a live one:
            # the session is closed by the status that follows, unless it is
            # still a charge.
            _LOGGER.warning(
                "%s: StopTransaction for %s while %s is open - closing on the next status",
                self.identity, number, self.tx_id,
            )
            self._foreign_stop = True
        self._stop_pending_status = True
        self._persist()
        return {"idTagInfo": {"status": "Accepted"}}, None

    def _finish_session(self, at: datetime) -> None:
        started = self.tx_started_at
        self.last_session = {
            "energy_kwh": round((self.session_wh or 0.0) / 1000, 3),
            "cost": round(self.session_cost, 2),
            "started": started.isoformat() if started else None,
            "finished": at.isoformat(),
            "minutes": round((at - started).total_seconds() / 60) if started else None,
        }
        self._close_tx()

    def _local(self, at: datetime) -> datetime:
        return at.astimezone(ZoneInfo(self.tz_name))

    def _count(self, wh: float, at: datetime) -> None:
        """Energy that really went into the car, priced at the moment it did."""
        if wh <= 0:
            return
        local = self._local(at)
        cost = wh / 1000 * self.settings.tariff.price_at(local)
        self.session_cost += cost
        self.totals.add(wh, cost, local)

    def roll_totals(self) -> None:
        before = (self.totals.day, self.totals.month)
        self.totals.roll(self._local(_now()))
        if (self.totals.day, self.totals.month) != before:
            self._persist()
            self._notify()

    @property
    def tariff_set(self) -> bool:
        tariff = self.settings.tariff
        return tariff.price > 0 or bool(tariff.night_price)

    def price_now(self) -> float:
        return self.settings.tariff.price_at(self._local(_now()))

    def next_scheduled_start(self) -> datetime | None:
        return self.settings.schedule.next_start(self._local(_now()))

    async def schedule_tick(self, now: datetime | None = None) -> None:
        """Called every 30 seconds. Starts in an open window, stops when it closes.

        The stop is taken on the EDGE - the window was open at the previous tick
        and is closed now - not on "the minute equals the end": a tick is not
        guaranteed to land on that exact minute.
        """
        local = self._local(now or _now())
        schedule = self.settings.schedule
        is_open = schedule.active(local)
        was_open, self._schedule_was_open = self._schedule_was_open, is_open
        if not self.adopted or not self.connected:
            return
        charging = self.tx_id is not None or self.status == "Charging"
        if charging and was_open and not is_open and schedule.enabled and schedule.stop_at_end:
            _LOGGER.info("%s: schedule window closed, stopping", self.identity)
            try:
                await self.stop()
            except ChargerError as err:
                _LOGGER.info("%s: stop at window end failed: %s", self.identity, err)
            return
        if is_open and was_open is False and not charging and self.status == "Finishing" and self.cable_connected:
            # The window has just opened and the cable stayed in the car after an
            # earlier charge. plugged_idle() below refuses this Finishing (it is
            # meant for a cable plugged in), so the schedule never started while
            # the cable stayed in for days (partner's bench, 25.09.2026, 19:48).
            # A manual start from here worked 5 of 5 that day with the car awake;
            # a sleeping car (20:11) took no current - that start is caught by
            # _watch_start.
            await self._auto_start()
            return
        if is_open and not charging and plugged_idle(self.status, self.previous_status):
            await self._auto_start()

    def _close_tx(self) -> None:
        self.tx_id = None
        self.tx_meter_start = None
        self.tx_started_at = None

    def _on_MeterValues(self, p: dict[str, Any]) -> tuple[dict[str, Any], None]:
        for mv in p.get("meterValue") or []:
            if isinstance(mv, dict):
                self._apply_meter(mv.get("sampledValue"), station_timestamp(mv.get("timestamp"), _now()))
        if isinstance(p.get("transactionId"), int):
            self.station_tx_id = p["transactionId"]
        self.last_meter_at = _now()
        self._persist()
        return {}, None

    def _apply_meter(self, samples: Any, at: datetime) -> None:
        m = extract_metrics(samples)
        if m.power_w is not None:
            self.power_w = m.power_w
        if m.current:
            self.current.update(m.current)
        if m.voltage:
            self.voltage.update(m.voltage)
        if m.temperature is not None:
            self.temperature = m.temperature
        if m.soc is not None:
            self.soc = m.soc
        if m.energy_wh is not None:
            self._count(self.energy.add(m.energy_wh, at), at)
            base = self.tx_meter_start if self.tx_id is not None and self.tx_meter_start is not None else 0.0
            self.session_wh = max(0.0, m.energy_wh - base)

    # --- outgoing ---------------------------------------------------------------

    async def call(self, action: str, payload: dict[str, Any], timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        """Send a command and wait for the answer. One at a time: OCPP 1.6 does
        not allow a second request while the first is unanswered."""
        async with self._call_lock:
            if not self.connected:
                raise ChargerError("not_connected")
            # The charger drops a command that comes right after the answer to
            # the previous one (partner's bench: "Refresh readings" 3 of 3,
            # 24.09.2026; paired GetConfiguration after settings changes,
            # 25.09.2026). One rule for every command: 2 s after the last answer.
            wait = COMMAND_GAP_S - (time.monotonic() - self._last_answer_mono)
            if wait > 0:
                await asyncio.sleep(wait)
            msg_id = uuid.uuid4().hex[:20]
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[msg_id] = fut
            await self._send([CALL, msg_id, action, payload])
            try:
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError as err:
                raise ChargerError("no_answer", action) from err
            finally:
                self._pending.pop(msg_id, None)
                self._last_answer_mono = time.monotonic()

    async def _setup_after_boot(self) -> None:
        """Read the configuration and bring the charger to the owner's settings."""
        if not self.adopted:
            return
        await asyncio.sleep(COMMAND_GAP_S)
        try:
            res = await self.call("GetConfiguration", {})
            self.config = {
                str(k.get("key")): str(k.get("value", ""))
                for k in res.get("configurationKey") or []
                if isinstance(k, dict) and k.get("key")
            }
            wanted = str(self.settings.meter_interval_s)
            # Send only when it differs: re-sending after every boot filled the
            # charge log with lines people read as "10 amps".
            if self.config.get("MeterValueSampleInterval") not in (None, wanted):
                await self._change_config("MeterValueSampleInterval", wanted)
            if self.desired_current_a is not None and self.charge_rate_a != self.desired_current_a:
                await asyncio.sleep(COMMAND_GAP_S)
                await self._change_config("ChargeRate", str(self.desired_current_a))
        except ChargerError as err:
            _LOGGER.warning("%s: setup after boot incomplete: %s", self.identity, err)
        self._notify()

    async def _change_config(self, key: str, value: str) -> None:
        res = await self.call("ChangeConfiguration", {"key": key, "value": value})
        status = res.get("status")
        if status not in ("Accepted", "RebootRequired"):
            raise ChargerError("rejected", f"{key}={value}: {status}")
        self.config[key] = value

    async def _poll_meter_if_stale(self) -> None:
        if self.status != "Charging" or not self.adopted:
            return
        if self.last_meter_at and (_now() - self.last_meter_at).total_seconds() < METER_STALE_S:
            return
        try:
            await self.call("TriggerMessage", {"requestedMessage": "MeterValues", "connectorId": 1}, timeout=15)
        except ChargerError:
            pass

    # --- commands ---------------------------------------------------------------

    async def set_current(self, amps: int) -> None:
        """Only through ``ChargeRate``. Bounded by the passport of the unit."""
        if amps < 6 or amps > self.settings.max_current_a:
            raise ChargerError("current_out_of_range", f"6..{self.settings.max_current_a}")
        self.desired_current_a = amps
        self._persist()
        await self._change_config("ChargeRate", str(amps))
        self._notify()

    async def start(self) -> None:
        if self.status == "Charging" or (self.tx_id is not None and not self.empty_session):
            raise ChargerError("already_charging")
        if self.status in ("Unavailable", "Faulted"):
            raise ChargerError("unavailable")
        if self.empty_session:
            # Live test 25.09.2026: after an empty session the charger REJECTS
            # new starts, and toggling the connector does not wake the car.
            # Only re-plugging the cable helps - say so instead of trying.
            raise ChargerError("replug")
        # From a finished charge (cable still in the car) the start IS sent:
        # whether current flows is up to the car. A start it does not take is
        # caught by _watch_start.
        if self.desired_current_a is not None and self.charge_rate_a != self.desired_current_a:
            await self._change_config("ChargeRate", str(self.desired_current_a))
            await asyncio.sleep(COMMAND_GAP_S)
        res = await self.call("RemoteStartTransaction", {"connectorId": 1, "idTag": ID_TAG})
        if res.get("status") != "Accepted":
            raise ChargerError("rejected", str(res.get("status")))

    async def _auto_start(self) -> None:
        if time.monotonic() - self._last_auto_start < AUTO_START_COOLDOWN_S:
            return
        self._last_auto_start = time.monotonic()
        await asyncio.sleep(COMMAND_GAP_S)
        try:
            await self.start()
        except ChargerError as err:
            _LOGGER.info("%s: automatic start skipped: %s", self.identity, err)

    async def stop(self) -> None:
        """Polite stop: by the running transaction, or by the last number the
        charger was given (it remembers it through a reboot)."""
        if (self.tx_id is None or self.empty_session) and self.status not in ("Charging", "SuspendedEV", "SuspendedEVSE"):
            # Nothing is charging and no session is open (or only the empty one
            # the car did not take): nothing to stop. A schedule turning the
            # switch off must not fail with the text about a charge "started by
            # the charger itself" (partner's bench, 25.09.2026).
            return
        tx = self.tx_id
        if tx is None and self.last_tx_id is not None and self.last_tx_at and _now() - self.last_tx_at < LAST_TX_MAX_AGE:
            tx = self.last_tx_id
        if tx is None:
            raise ChargerError("no_session")
        res = await self.call("RemoteStopTransaction", {"transactionId": tx})
        if res.get("status") != "Accepted":
            raise ChargerError("no_session" if self.tx_id is None else "rejected", str(res.get("status")))

    async def force_stop(self) -> None:
        """Last resort for a charge without a session number: take the connector
        out of service. ``RemoteStart`` over it and ``ChargeRate = 0`` are both
        rejected - this is the only thing that works. It returns to service by
        itself in ten minutes or as soon as the status changes, whichever first:
        a charger silently left out of service is worse than the charge."""
        if not self.connected:
            raise ChargerError("not_connected")
        if not self.charging_without_session:
            # The button is always available (see ForceStopButton): a press
            # with no charge of the charger's own must not send anything.
            raise ChargerError("nothing_to_interrupt")
        # The polite stop first, by every number the charger may be using: on the
        # partner's bench (25.09.2026) all three charges the charger started by
        # itself stopped this way - twice even with a number other than its own.
        for tx in dict.fromkeys(n for n in (self.station_tx_id, self.tx_id, self.last_tx_id) if n is not None):
            try:
                res = await self.call("RemoteStopTransaction", {"transactionId": tx})
            except ChargerError:
                continue
            if res.get("status") == "Accepted":
                return
        res = await self.call("ChangeAvailability", {"connectorId": 1, "type": "Inoperative"})
        if res.get("status") not in ("Accepted", "Scheduled"):
            raise ChargerError("rejected", str(res.get("status")))
        self.forced_inoperative_at = _now()
        self._persist()
        self._notify()

    async def _maybe_return_to_service(self) -> None:
        if self.forced_inoperative_at and _now() - self.forced_inoperative_at >= FORCED_RETURN_AFTER:
            await self._return_to_service("timer")

    async def _return_to_service(self, why: str) -> None:
        try:
            res = await self.call("ChangeAvailability", {"connectorId": 1, "type": "Operative"})
        except ChargerError:
            return
        if res.get("status") in ("Accepted", "Scheduled"):
            _LOGGER.info("%s: back in service (%s)", self.identity, why)
            self.forced_inoperative_at = None
            self._persist()
            self._notify()

    async def set_plug_and_charge(self, on: bool) -> None:
        if not self.plug_and_charge_supported:
            raise ChargerError("not_supported")
        await self._change_config("FreeCharging", "true" if on else "false")
        self._notify()

    async def reset(self) -> None:
        res = await self.call("Reset", {"type": "Soft"})
        if res.get("status") != "Accepted":
            raise ChargerError("rejected", str(res.get("status")))

    async def refresh(self) -> None:
        await self.call("TriggerMessage", {"requestedMessage": "MeterValues", "connectorId": 1}, timeout=15)
        # Back to back, the charger drops the second request (3 of 3 on the
        # partner's bench, 24.09.2026); with a 2 s gap - 3 of 3 answered.
        await asyncio.sleep(COMMAND_GAP_S)
        await self.call("TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 1}, timeout=15)
