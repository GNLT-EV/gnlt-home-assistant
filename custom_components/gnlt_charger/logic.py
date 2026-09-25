"""Pure rules for GNLT EVE/EVB chargers, shared by the OCPP server and entities.

Every rule here was paid for on the live GNLT platform; the history lives in
``CLAUDE.md`` and ``PROJECT_LOG.md`` of the main repository. Nothing in this
module imports Home Assistant, so it is covered by plain pytest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# --- OCPP-J frames ---------------------------------------------------------

CALL = 2
CALLRESULT = 3
CALLERROR = 4

_EMPTY_TX = re.compile(r'("transactionId"\s*:)\s*(?=[,}])')


class FrameError(ValueError):
    """Frame that cannot be understood at all."""


def parse_frame(raw: str) -> list[Any]:
    """Parse an OCPP-J text frame.

    The firmware sends ``"transactionId":,`` - a key WITHOUT a value - for
    charges it started itself (plug & charge, resume after a power cut, offline).
    That is not JSON, and a strict parser drops the whole frame together with
    the energy readings. The one known pattern is repaired to ``null``;
    anything else stays an error.
    """
    try:
        msg = json.loads(raw)
    except ValueError:
        if not re.search(r'"transactionId"\s*:\s*[,}]', raw):
            raise FrameError("not JSON") from None
        try:
            msg = json.loads(_EMPTY_TX.sub(r"\1null", raw))
        except ValueError as err:
            raise FrameError("not JSON") from err
    if not isinstance(msg, list) or len(msg) < 3 or not isinstance(msg[1], str):
        raise FrameError("not an OCPP-J array")
    return msg


# --- time --------------------------------------------------------------------


def iso_in_zone(at: datetime, tz_name: str) -> str:
    """Local time with offset, e.g. ``2026-09-24T18:23:37.000+03:00``.

    The charger shows on its screen exactly the clock we send. With UTC ("Z")
    it showed UTC and its history was shifted by the zone offset; with a local
    time plus offset it shows the wall clock and echoes the offset back, so the
    timestamps it sends stay unambiguous. The offset is rounded to whole
    minutes - a raw float once produced "+02:60".
    """
    local = at.astimezone(ZoneInfo(tz_name))
    offset = local.utcoffset() or timedelta()
    minutes = round(offset.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    hh, mm = divmod(abs(minutes), 60)
    return local.strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}{sign}{hh:02d}:{mm:02d}"


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


FUTURE_TOLERANCE = timedelta(minutes=1)


def station_timestamp(value: Any, now: datetime) -> datetime:
    """A reading from the future does not exist.

    Once the firmware stamped readings exactly one hour ahead (summer time that
    Belarus has not had since 2011). A timestamp more than a minute ahead of us
    is replaced with ours; one in the past is kept - that is how legitimate
    offline back-fill looks.
    """
    ts = parse_ts(value)
    if ts is None or ts - now > FUTURE_TOLERANCE:
        return now
    return ts


# --- meter -------------------------------------------------------------------

RESET_CONFIRM = timedelta(minutes=5)


@dataclass
class EnergyCounter:
    """Lifetime energy (Wh) built from the charger's register, blink-proof.

    Streaming form of ``recovery.ts::meterIncrements`` of the GNLT platform -
    ONE rule for what counts as energy. The charger's
    ``Energy.Active.Import.Register`` restarts with every session and sometimes
    BLINKS: a sample far below the peak, then back to normal. Taking every drop
    as a reset counts the whole charge twice - that exact bug doubled a client's
    bill on the platform (22.09.2026).

    Rules:
      * the reference is the PEAK of the current count, not the previous
        sample, so a single dip does not shift the count;
      * a drop is a reset only when the register stays below half of the peak
        for five minutes - after a real reset even 22 kW cannot climb that far,
        while a blink comes back with the very next sample;
      * after a confirmed reset everything the new count gained is energy.

    ``total_wh`` only grows, so it is safe for the Home Assistant energy
    dashboard (``total_increasing``).
    """

    total_wh: float = 0.0
    peak_wh: float | None = None
    dip_since: datetime | None = None
    dip_peak_wh: float = 0.0

    def start_session(self, meter_start_wh: float | None) -> None:
        """StartTransaction gives an explicit new baseline."""
        self.peak_wh = meter_start_wh
        self.dip_since = None
        self.dip_peak_wh = 0.0

    def add(self, value_wh: float, at: datetime) -> float:
        """Feed a register reading; returns the increment counted, Wh."""
        if self.peak_wh is None:
            self.peak_wh = value_wh
            return 0.0
        confirmed = 0.0
        if self.dip_since is not None and at - self.dip_since > RESET_CONFIRM:
            # The five-minute window closed with the register still low: the
            # reset is real. This sample already belongs to the new count.
            confirmed = self._confirm_reset()
        peak = self.peak_wh

        if value_wh >= peak:
            gained = value_wh - peak
            self.total_wh += gained  # `confirmed` is already in the total
            self.peak_wh = value_wh
            self.dip_since = None
            self.dip_peak_wh = 0.0
            return confirmed + gained

        if value_wh >= peak / 2:
            # Near the peak (the final reading of a session is often a few
            # hundred Wh below the last sample), or back after a blink.
            self.dip_since = None
            self.dip_peak_wh = 0.0
            return confirmed

        # Far below the peak: a blink or a real reset - undecided until a
        # sample outside the five-minute window (or the final reading) decides.
        if self.dip_since is None:
            self.dip_since = at
            self.dip_peak_wh = value_wh
        else:
            self.dip_peak_wh = max(self.dip_peak_wh, value_wh)
        return confirmed

    def finish(self, value_wh: float | None, at: datetime) -> float:
        """StopTransaction: the final reading is the LAST sample.

        A dip still pending at the end has nothing after it to come back to -
        the platform treats that as a confirmed reset, and so do we.
        """
        gained = self.add(value_wh, at) if value_wh is not None else 0.0
        if self.dip_since is not None:
            gained += self._confirm_reset()
        return gained

    def _confirm_reset(self) -> float:
        gained = self.dip_peak_wh
        self.total_wh += gained
        self.peak_wh = self.dip_peak_wh
        self.dip_since = None
        self.dip_peak_wh = 0.0
        return gained

    def as_dict(self) -> dict[str, Any]:
        return {"total_wh": self.total_wh}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EnergyCounter:
        return cls(total_wh=float((data or {}).get("total_wh", 0.0)))


def energy_wh(samples: Any) -> float | None:
    """Energy register from a sampledValue list, in Wh."""
    for s in samples if isinstance(samples, list) else []:
        if not isinstance(s, dict):
            continue
        if s.get("measurand", "Energy.Active.Import.Register") != "Energy.Active.Import.Register":
            continue
        try:
            v = float(s.get("value"))
        except (TypeError, ValueError):
            continue
        return v * 1000 if s.get("unit", "Wh") == "kWh" else v
    return None


@dataclass
class Metrics:
    power_w: float | None = None
    current: dict[str, float] = field(default_factory=dict)
    voltage: dict[str, float] = field(default_factory=dict)
    temperature: float | None = None
    soc: float | None = None
    energy_wh: float | None = None


def extract_metrics(samples: Any) -> Metrics:
    """Readings the charger sends: energy, power, per-phase current and voltage,
    body temperature. ``SoC = 0`` means "no data" (DC chargers send zero before
    the car talks to them), never "empty battery"."""
    m = Metrics()
    for s in samples if isinstance(samples, list) else []:
        if not isinstance(s, dict):
            continue
        measurand = s.get("measurand", "Energy.Active.Import.Register")
        try:
            v = float(s.get("value"))
        except (TypeError, ValueError):
            continue
        unit = s.get("unit")
        phase = str(s.get("phase") or "L1")[:2]
        if measurand == "Energy.Active.Import.Register":
            m.energy_wh = v * 1000 if unit == "kWh" else v
        elif measurand == "Power.Active.Import":
            m.power_w = v * 1000 if unit == "kW" else v
        elif measurand == "Current.Import":
            m.current[phase] = v
        elif measurand == "Voltage":
            m.voltage[phase] = v
        elif measurand == "Temperature":
            m.temperature = v
        elif measurand == "SoC" and v > 0:
            m.soc = v
    return m


# --- connector status ----------------------------------------------------------


def is_real_finishing(status: str | None, previous: str | None) -> bool:
    """``Finishing`` means TWO opposite things on this firmware.

    After ``Charging`` - the charge is over, the cable is still in the car.
    After ``Available`` - the cable was pulled out and plugged in again, and the
    firmware said Finishing instead of Preparing. Unknown history -> real.

    Used for the automatic start only: "cable plugged in" is the second case,
    never the first. A MANUAL start is allowed in both - whether current flows
    after a finished charge depends on the car, and a start it does not take is
    caught by ``Charger._watch_start`` (changed 24.09.2026 after a partner's
    test: three of four such starts worked).
    """
    if status != "Finishing":
        return False
    return previous != "Available"


CABLE_STATUSES = frozenset({"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"})


def firmware_version(raw: str | None) -> float | None:
    """``SW:A1B_2.7-HW:B07_0.4`` -> 2.07 (so that 2.10 > 2.9)."""
    m = re.search(r"SW:[A-Za-z0-9]+_(\d+)\.(\d+)", raw or "")
    if not m:
        return None
    return int(m.group(1)) + int(m.group(2)) / 100


def supports_resume_after_power_loss(raw: str | None) -> bool:
    """From 2.8 the charger waits for a server command after power returns; 2.7
    continues by itself outside the protocol, so the setting would do nothing.
    Unknown format -> allowed (not our charger, behaviour unknown, not bad)."""
    v = firmware_version(raw)
    return v is None or v >= 2.08


def model_name(serial: str | None, firmware: str | None, country: str | None = None) -> str | None:
    """GNLT EVE (portable) / GNLT EVB (wall box). The charger calls itself
    "Home OCPP" in both cases; the serial prefix and the board code tell them
    apart. If the two disagree - say nothing rather than a wrong model.

    ‼️ In Poland (gnlt.pl) the letters are the other way round: the portable
    unit is sold as GNLT EVB (EVB03B, EVB11B), the wall box as GNLT EVE. A buyer
    there saw "GNLT EVE" on his portable EVB11B and took it for a wrong device
    (partner's bench, 25.09.2026). The country comes from Home Assistant."""
    portable, wall = ("GNLT EVB", "GNLT EVE") if (country or "").upper() == "PL" else ("GNLT EVE", "GNLT EVB")
    by_serial = portable if (serial or "").startswith("10") else wall if (serial or "").startswith("30") else None
    m = re.search(r"HW:([A-Za-z0-9]+)_", firmware or "")
    board = m.group(1) if m else None
    by_board = portable if board == "B07" else wall if board == "E22" else None
    if by_serial and by_board and by_serial != by_board:
        return None
    return by_serial or by_board


def clamp_current(amps: float, max_a: int) -> int:
    return max(6, min(int(round(amps)), max_a))


# --- schedule, tariff, statistics ------------------------------------------------
#
# Same rules as the GNLT platform (``scheduler.ts``, ``billing.ts::priceAt``):
#   * a window is given in minutes of the local day; ``end <= start`` means it
#     crosses midnight and ends the next day; the day filter applies to the day
#     the window STARTS;
#   * a scheduled start needs the cable in the car and no charge running, and is
#     repeated at most every 3 minutes;
#   * every portion of energy is priced at the moment it was delivered - a charge
#     that runs from the day into the night tariff is paid at both prices.

DAY_SCOPES = ("all", "weekdays", "weekends")


def _day_ok(scope: str, weekday: int) -> bool:
    """weekday: 0 = Monday ... 6 = Sunday."""
    if scope == "weekdays":
        return weekday < 5
    if scope == "weekends":
        return weekday >= 5
    return True


def in_window(start_min: int, end_min: int, scope: str, local: datetime) -> bool:
    minute = local.hour * 60 + local.minute
    weekday = local.weekday()
    if start_min == end_min:
        return _day_ok(scope, weekday)  # the whole day
    if start_min < end_min:
        return start_min <= minute < end_min and _day_ok(scope, weekday)
    # crosses midnight: evening part belongs to today, morning part to yesterday's window
    if minute >= start_min:
        return _day_ok(scope, weekday)
    if minute < end_min:
        return _day_ok(scope, (weekday - 1) % 7)
    return False


@dataclass
class Schedule:
    enabled: bool = False
    start_min: int = 23 * 60
    end_min: int = 7 * 60
    days: str = "all"
    stop_at_end: bool = False

    def active(self, local: datetime) -> bool:
        return self.enabled and in_window(self.start_min, self.end_min, self.days, local)

    def next_start(self, local: datetime) -> datetime | None:
        """When the next window opens (``local`` itself if one is open now)."""
        if not self.enabled:
            return None
        if self.active(local):
            return local.replace(second=0, microsecond=0)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        for day in range(8):
            candidate = midnight + timedelta(days=day, minutes=self.start_min)
            if candidate > local and _day_ok(self.days, candidate.weekday()):
                return candidate
        return None


# Statuses in which a scheduled or automatic start makes sense: cable in the
# car, no current. "Finishing" only after "Available" - the cable was re-plugged;
# after "Charging" it means the car is done, and restarting it every 3 minutes
# would produce a stream of empty sessions.
def plugged_idle(status: str | None, previous: str | None) -> bool:
    if status in ("Preparing", "SuspendedEVSE"):
        return True
    return status == "Finishing" and not is_real_finishing(status, previous)


@dataclass
class Tariff:
    """Price of 1 kWh: the base price, optionally a second (night) price in a window."""

    price: float = 0.0
    night_price: float | None = None
    night_start_min: int = 23 * 60
    night_end_min: int = 7 * 60

    def price_at(self, local: datetime) -> float:
        if self.night_price is not None and in_window(self.night_start_min, self.night_end_min, "all", local):
            return self.night_price
        return self.price


@dataclass
class Totals:
    """Energy and money by calendar day and month, local time."""

    day: str = ""
    day_wh: float = 0.0
    day_cost: float = 0.0
    month: str = ""
    month_wh: float = 0.0
    month_cost: float = 0.0
    month_sessions: int = 0

    def roll(self, local: datetime) -> None:
        day, month = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")
        if day != self.day:
            self.day, self.day_wh, self.day_cost = day, 0.0, 0.0
        if month != self.month:
            self.month, self.month_wh, self.month_cost, self.month_sessions = month, 0.0, 0.0, 0

    def add(self, wh: float, cost: float, local: datetime) -> None:
        self.roll(local)
        self.day_wh += wh
        self.day_cost += cost
        self.month_wh += wh
        self.month_cost += cost

    def add_session(self, local: datetime) -> None:
        self.roll(local)
        self.month_sessions += 1

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Totals:
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)
