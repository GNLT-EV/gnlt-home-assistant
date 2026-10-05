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

# Rules shared with the GNLT platform (agreed 04.10.2026; common scenarios in
# tests/vectors.json). They are a chosen
# heuristic, not a proof - an ambiguous choice is marked on the charge.
# Only a drop below half of the peak can be a restart of the register; a smaller
# one is a correction downwards and the peak stays.
RESET_BELOW = 0.5
# A low reading followed by a return above half of the peak within this time
# is a false dip, not a restart.
CONFIRM_WINDOW = timedelta(minutes=5)
# "The register could have restarted and grown to this value in the time" -
# rated power x (time + slack) x margin.
POWER_MARGIN = 1.1
TIME_SLACK_S = 60
# Closed charges kept for late or repeated StopTransaction; older ones are
# folded into the archived sum.
KEEP_CHARGES = 5
KEEP_UNRESOLVED = 10
NO_FINAL = "ended without its final figure (yet)"


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None


@dataclass
class ChargeRecord:
    """One charge as the charger counted it: register segments, its final
    figure, and what of it was already published in the totals.

    A segment is a stretch of one register count; a restart of the register in
    the middle of the charge opens the next one. Energy = sum of the segments;
    when no restart happened and the final figure came, it is the charger's own
    ``meterStop - meterStart`` (owner's decision 04.10.2026: the charger's final
    figure is what is recorded)."""

    rid: int
    number: int | None = None
    started: str | None = None
    open: bool = True
    peak: float = 0.0
    inc_wh: float = 0.0
    correction_wh: float = 0.0
    resets: int = 0
    ambiguous: int = 0
    reasons: list[str] = field(default_factory=list)
    pending: float | None = None
    pending_at: str | None = None
    last_good_at: str | None = None
    last_at: str | None = None
    final: float | None = None
    stopped_at: str | None = None
    stop_raw: str | None = None
    published_wh: float = 0.0

    @property
    def energy_wh(self) -> float:
        return max(0.0, self.inc_wh - self.correction_wh)

    @property
    def approximate(self) -> bool:
        return bool(self.reasons) or (not self.open and self.final is None)

    def note(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChargeRecord:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class Delta:
    """Energy to add to (or, for a correction, take from) the totals, and the
    moment it belongs to - the reading it came with."""

    wh: float
    at: datetime | None
    rid: int


@dataclass
class ChargeLedger:
    """Energy (Wh) from the charger's own numbers.

    * Every charge is a :class:`ChargeRecord`; ``total_wh`` is the sum of what
      each record published - there is no carried-over "debt": a correction
      belongs to its own charge and is taken back from the totals once.
    * Readings grow the open charge live (estimate); the final figure of
      ``StopTransaction`` goes through the same drop rules as a reading (the
      GNLT platform feeds it as the last sample).
    * Drops (shared with the platform): above half of the peak - a correction,
      the peak stays; below half - a candidate restart. The next reading decides:
      not below the peak -> false dip (ambiguous when a restart could have
      grown to that value in the time); back above half within 5 minutes ->
      false dip; still low and growing, or later -> the register restarted: a
      new segment of the same charge.
    * A charge the charger started by itself begins at 0 when the charger is
      known to count every charge from zero (its own ``meterStart`` = 0), at the
      idle register when it is known to count on; unknown -> the old guess,
      marked approximate.
    * A StopTransaction is matched to its charge by number; without a number by
      the readings (and, to break a tie, by time). Two candidates that both fit
      -> nothing is changed, the stop is kept for the diagnostics file.
    """

    archived_wh: float = 0.0
    records: list[ChargeRecord] = field(default_factory=list)
    last_register: float | None = None
    zero_starts: int = 0
    nonzero_starts: int = 0
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    next_rid: int = 1
    # Runtime only: how the last StopTransaction was taken - "matched",
    # "duplicate", "unresolved" (fits two charges or a lost one: nothing
    # changed) or "none" (no charge at all).
    last_stop: str = "none"
    # Runtime only: rated power of the charger (A x phases x 230 V), for the
    # "could the register have restarted" test. None -> every such case is
    # ambiguous.
    max_power_w: float | None = None

    # --- views ----------------------------------------------------------------

    @property
    def total_wh(self) -> float:
        return self.archived_wh + sum(r.published_wh for r in self.records)

    @property
    def open_record(self) -> ChargeRecord | None:
        return next((r for r in reversed(self.records) if r.open), None)

    @property
    def last_record(self) -> ChargeRecord | None:
        return self.records[-1] if self.records else None

    @property
    def is_open(self) -> bool:
        return self.open_record is not None

    @property
    def session_wh(self) -> float | None:
        r = self.last_record
        return None if r is None else r.energy_wh

    @property
    def register_mode(self) -> str:
        """"zero" - counts every charge from 0; "cumulative"; "unknown"."""
        if self.zero_starts and not self.nonzero_starts:
            return "zero"
        if self.nonzero_starts and not self.zero_starts:
            return "cumulative"
        return "unknown"

    # --- events ---------------------------------------------------------------

    def start(self, meter_start: float | None, number: int | None, at: datetime) -> list[Delta]:
        """StartTransaction: a new charge from ``meterStart``."""
        deltas = self.close_open(at)
        base = meter_start if meter_start is not None else 0.0
        if meter_start is not None:
            if meter_start == 0:
                self.zero_starts += 1
            else:
                self.nonzero_starts += 1
        self._open(number, base, at)
        return deltas

    def reading(self, value: float, charging: bool, at: datetime) -> list[Delta]:
        """A register reading."""
        r = self.open_record
        if r is None:
            if not charging or self._idle_register(value):
                # Idle: the register holds the last charge's value - also while
                # the status still says Charging after StopTransaction (a second
                # normally, minutes after an emergency stop; review 5).
                self.last_register = value
                return []
            r = self._open(None, self._own_start_base(value), at)
            if self.register_mode == "unknown":
                r.note("register mode unknown: start of a charge the charger began by itself guessed")
        self.last_register = value
        self._point(r, value, at, final=False)
        r.last_at = _iso(at)
        return self._publish(r, at)

    def stop(
        self, meter_stop: float | None, number: int | None, at: datetime, raw: str | None = None
    ) -> tuple[list[Delta], ChargeRecord | None]:
        """StopTransaction. Returns the deltas and the charge it closed (None
        when it could not be matched - then nothing changed); how it was taken
        is in ``last_stop``. ``raw`` is the timestamp string of the frame: a
        retry carries the same string even when the time was replaced (clock
        ahead)."""
        r = self._match_stop(meter_stop, number, at, raw)
        if r is None:
            return [], None
        if r.final is not None and not r.open:
            self.last_stop = "duplicate"
            return [], r  # a repeated stop: already settled
        self.last_stop = "matched"
        if meter_stop is not None:
            self.last_register = meter_stop
            self._point(r, meter_stop, at, final=True)
            r.final = meter_stop
            r.stopped_at = _iso(at)
            r.stop_raw = raw
            if NO_FINAL in r.reasons:
                r.reasons.remove(NO_FINAL)
        elif r.open:
            r.note(NO_FINAL)
        r.open = False
        r.pending = None
        # A correction downwards belongs to the last reading (a chosen policy:
        # the final figure does not say when the difference arose); energy
        # after it - to the moment of the final figure.
        when = (parse_ts(r.last_at) or at) if r.energy_wh < r.published_wh else at
        deltas = self._publish(r, when)
        self._prune()
        return deltas, r

    def close_open(self, at: datetime) -> list[Delta]:
        """The open charge ended without its final figure (yet): its estimate
        stands until a late StopTransaction replaces it."""
        r = self.open_record
        if r is None:
            return []
        r.open = False
        r.pending = None
        r.note(NO_FINAL)
        self._prune()
        return []

    # --- internals ------------------------------------------------------------

    def _open(self, number: int | None, base: float, at: datetime) -> ChargeRecord:
        r = ChargeRecord(rid=self.next_rid, number=number, started=_iso(at), peak=base)
        self.next_rid += 1
        self.records.append(r)
        return r

    def _idle_register(self, value: float) -> bool:
        """The value is the register of the charge that just ended: between its
        final figure and its peak (a final below the last reading), with 1 Wh
        for kWh rounding."""
        last = self.last_record
        if last is None or last.open or last.final is None:
            return False
        low, high = sorted((last.final, last.peak))
        return low - 1 <= value <= high + 1

    def _own_start_base(self, value: float) -> float:
        mode = self.register_mode
        last = self.last_register
        if mode == "zero" or last is None:
            return 0.0
        if mode == "cumulative":
            return last if value >= last else 0.0
        return last if value >= last else 0.0

    def _could_restart_to(self, value: float, since: datetime | None, at: datetime) -> bool:
        """Could a restarted register have grown to ``value`` by ``at``?"""
        if self.max_power_w is None or since is None:
            return True
        seconds = max(0.0, (at - since).total_seconds()) + TIME_SLACK_S
        return value <= self.max_power_w * seconds / 3600 * POWER_MARGIN

    def _point(self, r: ChargeRecord, v: float, at: datetime, final: bool) -> None:
        """One value of the register, a reading or the final figure."""
        half = RESET_BELOW * r.peak
        if r.pending is None:
            if v >= r.peak:
                r.inc_wh += v - r.peak
                r.peak = v
                r.last_good_at = _iso(at)
            elif v >= half:
                if final:
                    r.correction_wh = r.peak - v
            elif final:
                self._restart(r, v)
            else:
                r.pending, r.pending_at = v, _iso(at)
            return
        dropped_at = parse_ts(r.pending_at) or at
        if v >= r.peak:
            if self._could_restart_to(v, parse_ts(r.last_good_at), at):
                r.ambiguous += 1
                r.note("register dip: a restart was possible too")
            r.inc_wh += v - r.peak
            r.peak = v
            r.last_good_at = _iso(at)
            r.pending = None
        elif v >= half:
            if at - dropped_at <= CONFIRM_WINDOW:
                if final:
                    r.correction_wh = r.peak - v
                r.pending = None
            else:
                self._restart(r, v)
        elif v > r.pending or final or at - dropped_at > CONFIRM_WINDOW:
            self._restart(r, v)
        # else: still low and not growing - keep waiting

    def _restart(self, r: ChargeRecord, v: float) -> None:
        """The register restarted: a new segment, counted from 0."""
        r.resets += 1
        r.inc_wh += v
        r.peak = v
        r.correction_wh = 0.0
        r.pending = None

    def _publish(self, r: ChargeRecord, at: datetime) -> list[Delta]:
        delta = r.energy_wh - r.published_wh
        if abs(delta) < 1e-9:
            return []
        r.published_wh += delta
        return [Delta(delta, at, r.rid)]

    def _fits(self, r: ChargeRecord, v: float, at: datetime) -> bool:
        """The final figure fits the charge without assuming a restart."""
        if v < RESET_BELOW * r.peak:
            return False
        if v <= r.peak:
            return True
        return self._could_restart_to(v - r.peak, parse_ts(r.last_at), at)

    def _match_stop(
        self, v: float | None, number: int | None, at: datetime, raw: str | None
    ) -> ChargeRecord | None:
        for r in reversed(self.records):
            # The same StopTransaction again (a retry after a reconnect): same
            # figure and the same timestamp string (or moment).
            same_time = r.stop_raw == raw if raw is not None and r.stop_raw is not None else r.stopped_at == _iso(at)
            if r.final is not None and r.final == v and same_time and number in (None, r.number):
                return r
        open_r = self.open_record
        # An unknown start (a charge carried over from the old storage format
        # without its session) proves nothing either way (platform review,
        # 04.10.2026).
        started = parse_ts(open_r.started) if open_r is not None else None
        open_began_before = started is not None and at >= started - timedelta(seconds=5)
        open_began_after = started is not None and at < started - timedelta(seconds=5)
        if number is not None:
            found = next((r for r in reversed(self.records) if r.number == number), None)
            if found is not None and found is open_r:
                return found
            if open_r is not None and open_began_before:
                # The charger runs its only charge under the number of an
                # EARLIER session (partner's bench 25.09.2026), and the stop is
                # stamped inside the open charge: it is the open one.
                return open_r
            if found is not None:
                return found  # a late stop of an earlier charge
            if open_r is not None:
                return self._unresolved(v, number, at)
            # A number HA never gave (the charger's own charge carries its own
            # number in MeterValues): matched like a stop without a number.
        waiting = [r for r in reversed(self.records) if not r.open and r.final is None]
        candidates = ([open_r] if open_r else []) + waiting
        if not candidates:
            self.last_stop = "none"
            return None
        if len(candidates) == 1:
            return candidates[0]
        if v is not None:
            fitting = [r for r in candidates if self._fits(r, v, at)]
            if len(fitting) > 1 and open_r in fitting and open_began_after:
                # Time as a tie-breaker only: it ended before the open one began.
                fitting.remove(open_r)
            if len(fitting) == 1:
                return fitting[0]
        return self._unresolved(v, number, at)

    def _unresolved(self, v: float | None, number: int | None, at: datetime) -> None:
        self.last_stop = "unresolved"
        self.unresolved.append({"at": _iso(at), "number": number, "meter_stop": v})
        del self.unresolved[:-KEEP_UNRESOLVED]
        return None

    def _prune(self) -> None:
        closed = [r for r in self.records if not r.open]
        for r in closed[:-KEEP_CHARGES]:
            self.archived_wh += r.published_wh
            self.records.remove(r)

    # --- storage --------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "archived_wh": self.archived_wh,
            "records": [r.as_dict() for r in self.records],
            "last_register": self.last_register,
            "zero_starts": self.zero_starts,
            "nonzero_starts": self.nonzero_starts,
            "unresolved": list(self.unresolved),
            "next_rid": self.next_rid,
            "total_wh": self.total_wh,
            "register_mode": self.register_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ChargeLedger:
        data = data or {}

        def num(key: str) -> float | None:
            v = data.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        if data.get("version") == 2:
            return cls(
                archived_wh=num("archived_wh") or 0.0,
                records=[ChargeRecord.from_dict(r) for r in data.get("records") or [] if isinstance(r, dict)],
                last_register=num("last_register"),
                zero_starts=int(data.get("zero_starts") or 0),
                nonzero_starts=int(data.get("nonzero_starts") or 0),
                unresolved=[u for u in data.get("unresolved") or [] if isinstance(u, dict)],
                next_rid=int(data.get("next_rid") or 1),
            )
        # 0.2.8 / early 0.2.9 kept a total (and an open charge): nothing is
        # lost or counted again.
        total = num("total_wh") or 0.0
        led = cls(archived_wh=total, last_register=num("last_register"))
        base = num("open_base")
        if base is not None:
            open_wh = num("open_wh") or 0.0
            last = num("open_last")
            r = led._open(None, base, datetime.now(UTC))
            r.started = None  # unknown - a late stop must still find it
            r.inc_wh = open_wh
            r.published_wh = open_wh
            r.peak = last if last is not None else base + open_wh
            led.archived_wh = max(0.0, total - open_wh)
        return led


ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"


def _phase(raw: Any) -> str | None:
    """OCPP phase -> "L1" / "L2" / "L3"; None for a line-to-line or neutral value.

    No phase means L1 for current and voltage (one-phase chargers omit it).
    "L1-N" is L1; "L1-L2" (400 V between phases) and "N" are not a phase of
    their own and must not overwrite L1.
    """
    if raw in (None, ""):
        return "L1"
    text = str(raw)
    if text in ("L1", "L2", "L3"):
        return text
    if len(text) == 4 and text[:2] in ("L1", "L2", "L3") and text[2:] == "-N":
        return text[:2]
    return None


def _value(s: dict[str, Any]) -> float | None:
    try:
        return float(s.get("value"))
    except (TypeError, ValueError):
        return None


def _total(whole: float | None, phases: dict[str, float]) -> float | None:
    """A value without a phase is the whole; without it the phases add up.

    Three-phase firmware may send power and energy per phase only: taking the
    last sample showed one third of the real power (04.10.2026).
    """
    if whole is not None:
        return whole
    return sum(phases.values()) if phases else None


def energy_wh(samples: Any) -> float | None:
    """Energy register from a sampledValue list, in Wh."""
    return extract_metrics(samples).energy_wh


@dataclass
class Metrics:
    power_w: float | None = None
    # Power came per phase (summed into power_w), and the power factor if sent.
    power_per_phase: bool = False
    power_factor: float | None = None
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
    power: float | None = None
    power_ph: dict[str, float] = {}
    energy: float | None = None
    energy_ph: dict[str, float] = {}
    for s in samples if isinstance(samples, list) else []:
        if not isinstance(s, dict):
            continue
        v = _value(s)
        if v is None:
            continue
        measurand = s.get("measurand", ENERGY)
        unit = s.get("unit")
        tagged = s.get("phase") not in (None, "")
        phase = _phase(s.get("phase"))
        if measurand == ENERGY:
            wh = v * 1000 if unit == "kWh" else v
            if not tagged:
                energy = wh
            elif phase:
                energy_ph[phase] = wh
        elif measurand == POWER:
            w = v * 1000 if unit == "kW" else v
            if not tagged:
                power = w
            elif phase:
                power_ph[phase] = w
        elif measurand == "Power.Factor":
            m.power_factor = v
        elif measurand == "Current.Import" and phase:
            m.current[phase] = v
        elif measurand == "Voltage" and phase:
            m.voltage[phase] = v
        elif measurand == "Temperature":
            m.temperature = v
        elif measurand == "SoC" and v > 0:
            m.soc = v
    m.power_w = _total(power, power_ph)
    m.power_per_phase = power is None and bool(power_ph)
    m.energy_wh = _total(energy, energy_ph)
    return m


# Firmware whose phase-less power is, on three phases, the power of ONE phase:
# the register agrees with the integral of the sum of U x I (1.00) and not with
# the integral of the reported power (0.33) - GNLT platform data, 450 of 584
# three-phase readings, 04.10.2026. SW:A3B_2.7-HW:B07_0.4 - live three-phase
# charge, 06.10.2026: reported 3400-3500 W at a sum of U x I of 10.2-10.4 kW
# (0.33-0.34), register 0.99 of it, the charger's own screen 10.5 kW. Only an
# exact match; other firmware is not assumed to behave the same.
ONE_PHASE_POWER_PROFILES = frozenset({"SW:A3B_3.1-HW:B07_0.5", "SW:A3B_2.7-HW:B07_0.4"})


def apparent_power_va(voltage: dict[str, float], current: dict[str, float]) -> float:
    """Sum of U x I of the phases (VA). A charger that sends one voltage (no
    phase = L1) for all phases: that voltage is used for each."""
    common = voltage.get("L1", 0.0)
    return sum((voltage.get(ph) or common) * a for ph, a in current.items())


def one_phase_power_fix(
    power_w: float,
    voltage: dict[str, float],
    current: dict[str, float],
    per_phase_power: bool,
    power_factor: float | None,
    profile_confirmed: bool,
) -> float:
    """Power to show: the charger's figure, except for a confirmed profile that
    reports one phase of several - then the sum of U x I of the phases with
    current (a calculated estimate, the same rule as the GNLT platform).

    Never without the profile: an unknown power factor is not 1 (6 000 W of
    11 040 VA is a valid reading)."""
    if not profile_confirmed or per_phase_power or power_factor is not None:
        return power_w
    loaded = {
        ph: voltage.get(ph, 0.0) * a
        for ph, a in current.items()
        if a > 1.0 and voltage.get(ph, 0.0) > 100.0
    }
    if len(loaded) < 2:
        return power_w
    total = sum(loaded.values())
    if total < 500 or power_w >= 0.75 * total:
        return power_w
    if not any(abs(power_w - va) <= 0.1 * va for va in loaded.values()):
        return power_w
    return total


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
# Same rules as the GNLT platform:
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
    # The current day / month began from zero while this version counted it.
    # Only such a period is announced to Home Assistant as a cycle with its
    # start (last_reset): Home Assistant counts the whole value of a NEW cycle
    # as consumed since that start (sensor/recorder.py, compile_statistics), so
    # announcing the running day's start in the middle of the day - at an
    # update from 0.2.8, when the day's energy was already counted - counted it
    # twice (stand, 04.10.2026: +19.99 kWh). A period not seen from zero goes on
    # as the cycle Home Assistant already has.
    day_from_zero: bool = False
    month_from_zero: bool = False

    def roll(self, local: datetime) -> None:
        day, month = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")
        if self.day and day < self.day:
            # Never back: a reading stamped up to a minute ahead may roll the
            # day first, and Home Assistant's own clock comes a moment later
            # (review 5, M1: the new day was zeroed back to yesterday).
            return
        if day != self.day:
            self.day, self.day_wh, self.day_cost = day, 0.0, 0.0
            self.day_from_zero = True
        if month != self.month:
            self.month, self.month_wh, self.month_cost, self.month_sessions = month, 0.0, 0.0, 0
            self.month_from_zero = True

    def start_of(self, what: str, tz_name: str, announced_only: bool) -> datetime | None:
        """Local start of the day / month the counters belong to - not of
        "now": in the first moments after midnight, before the counters roll,
        the value still belongs to yesterday. ``announced_only``: None for a
        period not seen from zero (see day_from_zero)."""
        if what == "day":
            text, fmt, seen = self.day, "%Y-%m-%d", self.day_from_zero
        else:
            text, fmt, seen = self.month, "%Y-%m", self.month_from_zero
        if not text or (announced_only and not seen):
            return None
        return datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo(tz_name))

    def add(self, wh: float, cost: float, local: datetime) -> None:
        """Energy and money at a local moment. A moment of a day already rolled
        over (a late correction, a back-filled reading) counts in its month if
        that month is still the current one - the totals never roll back."""
        day, month = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")
        if self.day and day < self.day:
            if month == self.month:
                self.month_wh += wh
                self.month_cost += cost
            return
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
