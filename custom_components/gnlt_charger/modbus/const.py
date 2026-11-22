"""Registers, scales, code tables and timing constants of the ModBus mode.

One source of truth for every number of the ModBus mode. Each value carries the
reason it has this size; measured values come from EVE (A3C) and EVB (A3B) chargers.
Nothing here imports Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- transport ---------------------------------------------------------------

PORT = 502
UNIT_ID = 255  # every request; not configurable (register documents)
DELAY_S = 0.06  # before every request, as evcc's EM2GO driver (TcpSettings Delay); no settle after connect
# evcc 82d37e9 uses the fork evcc-io/modbus,
# whose tcpTimeout of 3 s is the request deadline and the dial timeout.
CONNECT_TIMEOUT_S = 3.0
REQUEST_TIMEOUT_S = 3.0
# Reconnect exactly like evcc, no own gap: any error closes the connection
# and the next request opens a new one at once; a read dropped by the peer
# is retried once at once on a fresh connection. No backoff while running.
#
# Start-up like evcc's NewEm2Go: the first contact is retried with an
# exponential backoff starting at 2 s, at most 10 s apart, given up after
# 30 s; then Home Assistant retries the setup (ConfigEntryNotReady).
# Multiplier 1.5 and randomization 0.5 are the defaults of the backoff
# library evcc uses (cenkalti/backoff v4; not read in its source).
STARTUP_BACKOFF_INITIAL_S = 2.0
STARTUP_BACKOFF_MAX_S = 10.0
STARTUP_BACKOFF_GIVE_UP_S = 30.0
STARTUP_BACKOFF_MULTIPLIER = 1.5
STARTUP_BACKOFF_RANDOMIZATION = 0.5

# Measured on an EVE (A3C_2.7): repeated close/open gave timeouts and refused
# connects even with 10 s gaps, so a single refused/timeout proves nothing.
# The "check the menu / IP" hints are shown only after a persistent run of
# failures: at least this many failed attempts spanning at least this long
# .
LINK_HINT_AFTER_ATTEMPTS = 3
LINK_HINT_AFTER_S = 180.0

# --- polling -----------------------------------------------------------------

# every 10 s always, the period an option
# like the OCPP "meter_interval" (10-60 s); no faster rate while charging.
POLL_S = 10.0  # tier A
MIN_POLL_S = 2.0  # polling ~1/s caused timeouts elsewhere (E3-016 reworded)
SLOW_POLL_S = 60.0  # tier B
IDENTITY_RETRIES = 3  # serial read tries within ~2 s before "not ready"
IDENTITY_RETRY_DELAY_S = 0.7  # poll only; a stop re-reads at once (it must not wait)
REG89_WARN_BELOW_S = 30  # comm timeout shorter than 2 polls + margin: someone set it short
REFRESH_MIN_S = 5.0

# --- link health -------------------------------------------------------------

LINK_DOWN_POLLS = 3  # "connected" goes off after this many failed polls in a row
UNAVAILABLE_AFTER_S = 60.0  # measurements keep their last value this long
LOST_NOTIFY_AFTER_S = 180.0  # then: link lost (notification + repair issue)

# --- commands / sequences (used by the command layer) -----------------

# A stop still charging this long after it went out is shown as "stop not
# working".
STOP_VERIFY_S = 30.0
STOP_LATE_S = 300.0
START_CURRENT_S = 90.0
EMPTY_SESSION_S = 180.0
SETTLE_S = 60.0
CHARGE_GRACE_S = 60.0
ENERGY_GAP_MAX_S = 60.0
REG28_LATE_S = 900.0

# evcc EM2GO driver Enable(false): every stop is 95 = 2 followed by
# 91 = 0, for both firmware classes (the only working AEFA stop used this
# pair, measured on A3C_3.1). The box stores the 0 as 50 (5.0 A,
# measured on A3B). Either value is the stop marker: allowed by the write
# gate for that stop step only, never treated as a foreign change to
# restore while the station is stopped in command mode, and overwritten by
# the next start, which writes 91 before 95 = 1.
STOP_SETPOINT = 0
STOP_SETPOINT_STORED = frozenset({0, 50})

# Origins of user commands (entities, services) and HA automations; every
# other origin is automatic/internal.
USER_ORIGINS = frozenset({"user", "automation"})
# evcc initialize: the 6.1 A probe of the start-up writes before the first poll.
ORIGIN_PROBE = "firmware_probe"
# Site control (the DLM + PV coordinator) writes the charger every cycle like
# evcc's loadpoint: its 91 is not coalesced (evcc has no coalescing).
ORIGIN_SITE = "site"  # the site coordinator (DLM + PV, site/coordinator.py)
SITE_ORIGINS = frozenset({ORIGIN_SITE})



# --- thresholds of the state model --------------------------------------------

DELIVERING_ON_A = 1.0  # status 4 counts as "delivering" from this phase current
DELIVERING_OFF_A = 0.7
PHASE_ACTIVE_A = 1.0  # same thresholds as the OCPP phase count (charger.py)
PHASE_OFF_A = 0.7
CAPPED_6A_LOW = 5.5
CAPPED_6A_HIGH = 6.5
DEFAULT_MIN_TENTHS = 60  # 6.0 A: IEC minimum; never written below
DEFAULT_MAX_TENTHS = 160  # used while reg 32 is unknown
CURRENT_FLOOR_TENTHS = 60
POWER_ON_SETPOINT = 160  # 91 after a power-on: part of the restart signature

# --- registers -----------------------------------------------------------------

REG_STATUS = 0
REG_CABLE = 2  # reg 1 is undocumented and never read
REG_ERROR = 4
REG_CURRENT_L1 = 6
REG_CURRENT_L2 = 8
REG_CURRENT_L3 = 10
REG_POWER = 12  # U32: one count-2 read (as evcc); on a stride-2 box the words of 12 and 14, meaning assumed
# U32 16/20/24 (one count-2 read each): power per phase in 10 W (measured on
# A3C_2.7 under load: 204 -> 2040 W at 8.9 A, U x I 2047 W); sensors power L1/L2/L3.
REG_POWER_L1 = 16
REG_POWER_L2 = 20
REG_POWER_L3 = 24
REG_METER = 28  # U32 (one count-2 read), 0.1 kWh
REG_MAX_CURRENT = 32
REG_MIN_CURRENT = 34
REG_CABLE_MAX = 36
REG_SERIAL = 38  # 38, 40, 42 (three single reads): BCD words -> 12 digits
REG_DURATION = 78  # U32 (one count-2 read), s (60 s steps)
REG_FALLBACK_CURRENT = 87
REG_COMM_TIMEOUT = 89
REG_CURRENT_SETPOINT = 91
REG_MODE = 93  # 0 = Plug & Charge, 1 = start/stop by command
REG_COMMAND = 95  # 1 start, 2 stop; an echo of the last command only
REG_VOLTAGE_L1 = 109
REG_VOLTAGE_L2 = 111
REG_VOLTAGE_L3 = 113
REG_PHASES = 200  # 1 or 3

# The only registers the integration may ever write, each as exactly
# one register with FC16. 87/89 are read-only until the car test T6.
WRITE_ALLOW_LIST = frozenset({REG_CURRENT_SETPOINT, REG_MODE, REG_COMMAND, REG_PHASES})
ALLOWED_WRITE_VALUES = {
    REG_MODE: frozenset({0, 1}),
    REG_COMMAND: frozenset({1, 2}),
    REG_PHASES: frozenset({1, 3}),
}
COMMAND_START = 1
COMMAND_STOP = 2
MODE_PLUG_AND_CHARGE = 0
MODE_COMMAND = 1

# Registers a DaheimLaden (Injet) charger uses and an AEFA box does not; never
# accessed.
DAHEIMLADEN_ONLY_REGS = frozenset({54, 184, 185, 186, 187, 188})

# Scales: register value x scale = unit value.
SCALE_CURRENT = 0.1  # A: 6/8/10, 32/34/36, 87, 91
SCALE_VOLTAGE = 0.1  # V: 109/111/113
SCALE_ENERGY = 0.1  # kWh: 28
SCALE_POWER = 1.0  # W: 12
SCALE_PHASE_POWER = 10.0  # W: 16/20/24


# Request shape like evcc's EM2GO driver: every 16-bit register is
# its own FC03 request of count 1, a U32 value one request of count 2 at its
# first register. No block reads, so the block-read layout of the box (measured
# on A3C_2.7: a block of n at a answers a, a+2, a+4...) never matters for 16-bit
# values.


@dataclass(frozen=True)
class ReadGroup:
    """Registers read together for one purpose (one poll tier entry).

    ``used`` are the keys of ``raw`` the group fills. A 16-bit group reads
    each register with its own count-1 request. ``u32``: one count-2 request
    at ``used[0]``; like evcc the two words are decoded as (w0 << 16) | w1
    and kept under the keys a and a + 1 (``decode_u32``). On a stride-2 box
    the second word is the one of a + 2; its U32 meaning is assumed until tests
    with a car confirm it (plausible power for evcc users).
    ``required``: an exception reply fails the poll; for an optional group
    the values are just unknown.
    """

    used: tuple[int, ...]
    required: bool = True
    u32: bool = False

    @property
    def address(self) -> int:
        return self.used[0]

    @property
    def requests(self) -> tuple[tuple[int, int], ...]:
        """The FC03 requests of this group: (address, count)."""
        if self.u32:
            return ((self.used[0], 2),)
        return tuple((reg, 1) for reg in self.used)


def u32_group(address: int, *, required: bool = True) -> ReadGroup:
    return ReadGroup((address, address + 1), required=required, u32=True)


GROUP_STATUS = ReadGroup((REG_STATUS, REG_CABLE, REG_ERROR))
GROUP_CURRENTS = ReadGroup((REG_CURRENT_L1, REG_CURRENT_L2, REG_CURRENT_L3))
GROUP_POWER = u32_group(REG_POWER)
GROUP_DURATION = u32_group(REG_DURATION, required=False)
GROUP_SETPOINT_MODE = ReadGroup((REG_CURRENT_SETPOINT, REG_MODE))
GROUP_VOLTAGES = ReadGroup((REG_VOLTAGE_L1, REG_VOLTAGE_L2, REG_VOLTAGE_L3))
GROUP_SETPOINT = ReadGroup((REG_CURRENT_SETPOINT,))
GROUP_MODE = ReadGroup((REG_MODE,))
GROUP_LIMITS = ReadGroup((REG_MAX_CURRENT, REG_MIN_CURRENT, REG_CABLE_MAX), required=False)

GROUP_COMMAND = ReadGroup((REG_COMMAND,), required=False)

# Tier A, every poll. 200 only while the phase capability is known.
# 95 every poll: evcc Enabled(). Reg 72 is not read (evcc
# removed it as unreliable).
TIER_A = (
    GROUP_STATUS,
    GROUP_CURRENTS,
    GROUP_POWER,
    GROUP_DURATION,
    GROUP_SETPOINT_MODE,
    GROUP_COMMAND,
    GROUP_VOLTAGES,
)
PHASES_GROUP = ReadGroup((REG_PHASES,), required=False)

# Tier B, every 60 s and on the first poll of every connection.
TIER_B = (
    u32_group(REG_METER, required=False),
    u32_group(REG_POWER_L1, required=False),
    u32_group(REG_POWER_L2, required=False),
    u32_group(REG_POWER_L3, required=False),
)

# Once per connection (no 60 s read):
# the station's limits for the current range.
CONNECTION_GROUPS = (GROUP_LIMITS,)

# Read outside the poll like evcc's EM2GO driver: 89 once at setup, 87
# by the heartbeat every 89 / 2 s. Their values feed the sensors.
HEARTBEAT_GROUP = ReadGroup((REG_FALLBACK_CURRENT,))
COMM_TIMEOUT_GROUP = ReadGroup((REG_COMM_TIMEOUT,))

# Registers whose value is kept between their reads.
STICKY_REGS = frozenset(
    {r for g in TIER_B for r in g.used} | {r for g in CONNECTION_GROUPS for r in g.used}
    | {REG_FALLBACK_CURRENT, REG_COMM_TIMEOUT}
)

# Tier C, first read of every connection (identity): 38, 40, 42, three
# single reads (the V0.1 document was right; "contiguous 38-40" was a misread
# block read).
SERIAL_GROUP = ReadGroup((REG_SERIAL, REG_SERIAL + 2, REG_SERIAL + 4))


# --- reg 0 status and reg 4 error tables (V1 document) --------------------------

STATUS_UNKNOWN = "unknown"
STATUS_KEYS = {
    0: STATUS_UNKNOWN,
    1: "standby",  # no car
    2: "connected",
    3: "starting",
    4: "charging",
    5: "start_failed",
    6: "finished",
    7: "fault",  # a valid state, not a read error
    8: "scheduled",  # waiting for the station's own reservation
    9: "firmware_update",
    10: "booting",
}
STATUS_STANDBY = 1
STATUS_CONNECTED = 2
STATUS_STARTING = 3
STATUS_CHARGING = 4
STATUS_START_FAILED = 5
STATUS_FINISHED = 6
STATUS_FAULT = 7
STATUS_FIRMWARE_UPDATE = 9
STATUS_BOOTING = 10
CABLE_STATUSES = frozenset({2, 3, 4, 6})  # these imply a plugged cable
# The statuses evcc's EM2GO driver maps to A/B/C; any other code makes the
# loadpoint skip its cycle - here: no control write.
CONTROL_STATUSES = frozenset({1, 2, 3, 4, 6})
NO_WRITE_STATUSES = frozenset({STATUS_FIRMWARE_UPDATE, STATUS_BOOTING})

ERROR_UNKNOWN = "unknown"
ERROR_KEYS = {
    0: "no_error",
    11: "cp_fault",
    12: "emergency_stop",
    13: "under_voltage",
    14: "over_voltage",
    15: "over_temperature",
    16: "meter_fault",
    17: "leakage_fault",
    18: "output_short",
    19: "over_current",
    21: "vehicle_fault",
    22: "vehicle_timeout",
    23: "relay_welded",  # relay adhesion: safety-critical
    24: "rcd_fault",
    25: "pe_fault",
    26: "startup_fault",
}
ERROR_RELAY_WELDED = 23
# Faults of the car side that a re-plug clears (vehicle_fault, vehicle_timeout):
# only these get the "unplug and plug in again" advice.
REPLUG_FAULTS = frozenset({21, 22})

# An own 200 write: the new phase count is expected until the settle window
# ends plus this margin (two polls must agree), then a change is logged again.
PHASE_EXPECT_EXTRA_S = 60.0

# Reg 4 code -> code on the charger screen (manual's bitmask), for the person
# who reads the screen.
SCREEN_CODE = {
    25: "E0001",
    24: "E0002",
    14: "E0004",
    13: "E0008",
    17: "E0010",
    19: "E0020",
    11: "E0040",
    18: "E0080",
    15: "E0100",
    12: "E8000",
}

# Display codes of the manual (bitmask), cross-reference for diagnostics only.
DISPLAY_FAULT_BITS = {
    0x0001: "ground",
    0x0002: "rcmu",
    0x0004: "over_voltage",
    0x0008: "under_voltage",
    0x0010: "leakage",
    0x0020: "over_current",  # E0020 seen with a car that does not follow 11-15 A
    0x0040: "cp_voltage",
    0x0080: "short_circuit",
    0x0100: "over_temperature",
    0x8000: "emergency_stop",
}

# --- link -------------------------------------------------------------------

LINK_CONNECTED = "connected"
LINK_RECONNECTING = "reconnecting"
LINK_LOST = "lost"

IDENTITY_UNVERIFIED = "unverified"
IDENTITY_OK = "ok"
IDENTITY_MISMATCH = "mismatch"

# Failure classes for UI texts and logs; the TCP ones are the
# client's ``ModbusError.kind`` values.
FAIL_REFUSED = "refused"
FAIL_CONNECT_TIMEOUT = "connect_timeout"
FAIL_UNREACHABLE = "unreachable"
FAIL_CLOSED_BY_STATION = "closed_by_station"
FAIL_NO_ANSWER = "no_answer"
FAIL_EXCEPTION = "exception"
FAIL_NOT_READY = "not_ready"
FAIL_IDENTITY_MISMATCH = "identity_mismatch"
FAIL_OTHER = "other"

# --- write refusals (translation keys of HomeAssistantError) --------

REFUSE_NOT_CONNECTED = "not_connected"
REFUSE_NOT_READY = "not_ready"
REFUSE_IDENTITY_MISMATCH = "identity_mismatch"
REFUSE_STATION_BUSY = "station_busy"
REFUSE_NOT_ALLOWED = "register_not_allowed"
REFUSE_CURRENT_RANGE = "current_out_of_range"
REFUSE_BUSY = "busy"

# Write outcomes in the write log.
WRITE_OK = "ok"  # acknowledged (no read-back)
WRITE_UNKNOWN = "unknown"  # no acknowledgement
WRITE_REJECTED = "rejected"  # exception reply
WRITE_NOT_SENT = "not_sent"  # the connection failed before sending
WRITE_NOT_NEEDED = "not_needed"  # stop of an idle station: nothing written

# --- problems (attribute ``reason`` of ``control_problem``) ---------------------

PROBLEM_STATION_FAULT_5_7 = "station_fault_5_7"
PROBLEM_IDENTITY_MISMATCH = "identity_mismatch"
PROBLEM_RELAY_WELDED = "relay_welded"
PROBLEM_STATION_LIMIT_LOWER = "station_limit_lower"
PROBLEM_CAPPED_6A = "capped_6a"
# 93 back to 0 (Plug & Charge) while the user's choice is 1: repair, no write.
PROBLEM_PNC_RESET = "plug_and_charge_reset"

# --- events published by the station layer ---------------------------------------

EVENT_PLUGGED = "plugged"
EVENT_UNPLUGGED = "unplugged"
EVENT_STATUS = "status_changed"
EVENT_FAULT = "fault"
EVENT_FAULT_CLEARED = "fault_cleared"
EVENT_RESTARTED = "station_restarted"
EVENT_FOREIGN_CHANGE = "foreign_change"
EVENT_LINK_LOST = "link_lost"
EVENT_LINK_RESTORED = "link_restored"
EVENT_IDENTITY_MISMATCH = "identity_mismatch"
EVENT_IDENTITY_RESTORED = "identity_restored"
EVENT_WRITE = "write"

# Notification kinds, sent by the HA layer.
NOTIFY_KINDS = (
    "faulted",
    "empty_session",
    "link_lost",
    "stop_not_working",
    "start_not_taken",
    "station_restarted",
    "identity_mismatch",
    "sequence_interrupted",
    "self_start",
    "finished_replug",
)

# --- learned capabilities --------------------------------------------------

# nothing is learned across restarts; the firmware
# class of the 6.1 A probe of this start is the only value (not stored).
LEARNED_DEFAULTS = {
    "decimal_current": "unknown",  # unknown / yes / no
}

# Size of the diagnostic rings.
RING_SIZE = 200
