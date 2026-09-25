"""Bluetooth protocol of GNLT EVE/EVB (AEFA) chargers.

Port of ``apps/user-web/src/ble/protocol.ts`` - the same frames, the same
checks. The TypeScript file is the source of truth and carries the history of
every rule below; this module must stay pure (no Home Assistant, no bleak) so
it can be tested without hardware.

Frame layout (both directions)::

    request:  AA | LEN | ID   | payload... | SUM
    reply:    A5 | LEN | code | payload... | SUM

``LEN`` counts the bytes AFTER itself (ID + payload + SUM); ``SUM`` is the sum
of every previous byte modulo 256. Reply code = ID + 0x80 on success,
ID + 0x50 on error, the bare ID when the firmware does not know the command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

REQ_HEADER = 0xAA
RES_HEADER = 0xA5


class Cmd:
    READ_STATE = 0x01
    READ_DEVICE_INFO = 0x02
    READ_STORED_RECORDS = 0x04
    READ_SCHEDULES = 0x06
    SET_CURRENT = 0x10
    EARTH_CHECK = 0x11
    PLUG_AND_CHARGE = 0x13
    CHARGE_SWITCH = 0x14
    WIFI_SSID = 0x15
    WIFI_PASSWORD = 0x16
    WIFI_IP = 0x17
    PHASES = 0x19
    CLOSE = 0x1A
    SET_TIME = 0x1D
    OCPP_CHARGE_ID = 0x1E
    OCPP_URL = 0x1F
    # 0x21 "enable OCPP" is in the vendor document but does NOT exist in any
    # firmware (vendor answer 17.09.2026). OCPP is switched on by hand, on the
    # charger screen. Never send it.


HELLO_ID = 0x00

MAX_CHARGE_ID_BYTES = 20
MAX_URL_BYTES = 128
MAX_WIFI_SSID_BYTES = 32
MAX_WIFI_PASSWORD_BYTES = 32

# The radio module shows up under two names: the serial number (fresh / after a
# power cycle) and "BL602-BLE-DEV" (after the first connection). Search by both
# and trust only the serial the charger reports itself.
MODULE_NAME_PREFIX = "BL602"

# The vendor app never negotiates MTU: 20 useful bytes per write, 300 ms apart,
# otherwise the firmware glues the pieces together before parsing them.
CHUNK_SIZE = 20
CHUNK_DELAY_S = 0.3

# Below this byte the vendor app refuses OCPP setup ("device not supported").
OCPP_MIN_VERSION = 32

# No AC charge below this (IEC 61851).
MIN_CURRENT_A = 6


class ProtocolError(ValueError):
    """A value that must not be sent to the charger."""


def checksum(data: bytes | bytearray) -> int:
    return sum(data) & 0xFF


def build_frame(cmd_id: int, payload: bytes | bytearray = b"") -> bytes:
    length = 1 + len(payload) + 1
    if length > 0xFF:
        raise ProtocolError(f"frame 0x{cmd_id:02X}: payload too long")
    body = bytes([REQ_HEADER, length, cmd_id]) + bytes(payload)
    return body + bytes([checksum(body)])


def ascii_field(text: str, field_name: str, max_bytes: int) -> bytes:
    """ASCII only - the firmware (and the vendor app) reject anything else.

    Cyrillic Wi-Fi names exist, so the caller must explain the error to the
    person instead of silently mangling the name.
    """
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError as err:
        raise ProtocolError(f"{field_name}: only latin letters, digits and ASCII symbols") from err
    if len(data) > max_bytes:
        raise ProtocolError(f"{field_name}: at most {max_bytes} characters")
    return data


def frame_charge_id(charge_id: str) -> bytes:
    return build_frame(Cmd.OCPP_CHARGE_ID, ascii_field(charge_id, "ChargeID", MAX_CHARGE_ID_BYTES))


def frame_ocpp_url(url: str) -> bytes:
    """Server address; the charger appends the ChargeID itself."""
    return build_frame(Cmd.OCPP_URL, ascii_field(url, "server address", MAX_URL_BYTES))


def frame_wifi_ssid(ssid: str) -> bytes:
    return build_frame(Cmd.WIFI_SSID, ascii_field(ssid, "network name", MAX_WIFI_SSID_BYTES))


def frame_wifi_password(password: str) -> bytes:
    return build_frame(Cmd.WIFI_PASSWORD, ascii_field(password, "network password", MAX_WIFI_PASSWORD_BYTES))


def frame_dhcp() -> bytes:
    """Mandatory third Wi-Fi step, and a START, not a setting.

    Without it the charger accepts name and password but never joins the
    network. On receiving it the charger goes to Wi-Fi and switches Bluetooth
    off - so it must be the LAST command of the session.
    """
    payload = bytearray(13)
    payload[0] = 0x01  # 01 = obtain the address via DHCP
    return build_frame(Cmd.WIFI_IP, payload)


def frame_plug_and_charge(enabled: bool) -> bytes:
    return build_frame(Cmd.PLUG_AND_CHARGE, bytes([0x01 if enabled else 0x00]))


def frame_close_session() -> bytes:
    """Without it the charger keeps the radio busy and does not go back to Wi-Fi."""
    return build_frame(Cmd.CLOSE, bytes([0x55]))


def frame_hello_ack() -> bytes:
    """The charger greets first with ``A5 02 00 A7`` and waits for ``AA 02 80 2C``."""
    return build_frame(HELLO_ID + 0x80)


def frame_read_device_info() -> bytes:
    return build_frame(Cmd.READ_DEVICE_INFO)


def frame_read_state() -> bytes:
    return build_frame(Cmd.READ_STATE)


def frame_set_time(clock: datetime) -> bytes:
    """Local wall-clock time of the charger (plain binary numbers, not BCD)."""
    return build_frame(
        Cmd.SET_TIME,
        bytes([clock.year % 100, clock.month, clock.day, clock.hour, clock.minute, clock.second]),
    )


def frame_set_current(amps: int, max_a: int) -> bytes:
    if not isinstance(amps, int) or amps < MIN_CURRENT_A or amps > max_a or amps > 0xFF:
        raise ProtocolError(f"current {amps} A outside {MIN_CURRENT_A}..{max_a} A")
    return build_frame(Cmd.SET_CURRENT, bytes([amps]))


def chunk_frame(frame: bytes, size: int = CHUNK_SIZE) -> list[bytes]:
    return [frame[i : i + size] for i in range(0, len(frame), size)]


# --- replies ---------------------------------------------------------------


@dataclass
class Reply:
    cmd_id: int
    status: str  # "ok" | "error" | "unsupported"
    payload: bytes


def _decode_status(code: int) -> tuple[int, str]:
    # Addition, not OR: the error reply to 0x0F is 0x5F, while 0x0F | 0x60 = 0x6F.
    if code >= 0x80:
        return code - 0x80, "ok"
    if code >= 0x50:
        return code - 0x50, "error"
    return code, "unsupported"


@dataclass
class ReplyBuffer:
    """Reassembles replies that arrive in 20-byte notifications.

    Garbage before a header is dropped byte by byte; a frame with a wrong
    checksum is dropped silently - better lost than misread when the frame
    drives power equipment.
    """

    _buf: bytearray = field(default_factory=bytearray)

    def push(self, chunk: bytes | bytearray) -> list[Reply]:
        self._buf.extend(chunk)
        out: list[Reply] = []
        while len(self._buf) >= 3:
            if self._buf[0] != RES_HEADER:
                del self._buf[0]
                continue
            total = 2 + self._buf[1]
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            if checksum(frame[:-1]) == frame[-1]:
                cmd_id, status = _decode_status(frame[2])
                out.append(Reply(cmd_id, status, frame[3:-1]))
        return out

    def reset(self) -> None:
        self._buf.clear()


@dataclass
class DeviceInfo:
    model: str
    serial: str
    version: int

    @property
    def ocpp_supported(self) -> bool:
        return self.version >= OCPP_MIN_VERSION


def parse_device_info(payload: bytes) -> DeviceInfo | None:
    """Layout decoded from a live charger on 31.08.2026.

    First three bytes - the series in letters (``EVB``); the last six - the
    serial number where every byte READS AS TWO DECIMAL DIGITS in hex
    (``0x10`` -> "10"); byte 9 - the version.
    """
    if len(payload) < 18:
        return None
    model = "".join(chr(b) for b in payload[0:3] if 0x20 <= b < 0x7F)
    serial = "".join(f"{b:02x}" for b in payload[12:18])
    return DeviceInfo(model=model, serial=serial, version=payload[9])


@dataclass
class StationState:
    status: int
    max_current_a: int
    set_current_a: int
    voltage: tuple[int, int, int]
    current: tuple[float, float, float]
    power_kw: float
    kwh: float
    fault_code: int
    plug_and_charge: bool
    phases: int

    @property
    def charging(self) -> bool:
        return self.status == 7


def parse_station_state(payload: bytes) -> StationState | None:
    """Reply to READ_STATE. ``max_current_a`` is the HARDWARE limit of the unit
    (vendor, 17.09.2026), so it is what the passport is filled from."""
    if len(payload) < 29:
        return None

    def u16(i: int) -> int:
        return (payload[i] << 8) | payload[i + 1]

    def has(i: int) -> bool:
        return len(payload) > i

    return StationState(
        status=payload[2],
        max_current_a=payload[0],
        set_current_a=payload[1],
        voltage=(u16(3), u16(7), u16(11)),
        current=(u16(5) / 10, u16(9) / 10, u16(13) / 10),
        power_kw=u16(17) / 10,
        kwh=u16(19) / 100,
        fault_code=u16(25) if has(26) else 0,
        plug_and_charge=payload[36] == 0x01 if has(36) else False,
        phases=payload[37] if has(37) else 0,
    )


# --- passport ---------------------------------------------------------------

# AC versions sold. Chosen FROM A LIST, never computed as P / (U x phases):
# power on the label is rounded down (16 A x 230 V = 3.68 kW, sold as "3.5").
AC_PRESETS: tuple[tuple[int, float, int], ...] = (
    # (phases, kW, max current per phase)
    (1, 3.5, 16),
    (1, 7.0, 32),
    (1, 11.0, 48),
    (3, 11.0, 16),
    (3, 22.0, 32),
)


def preset_key(phases: int, power_kw: float) -> str:
    """``1_7`` = one phase 7 kW, ``1_3_5`` = one phase 3.5 kW (usable as a
    translation key, which a colon or a dot is not)."""
    return f"{phases}_{power_kw:g}".replace(".", "_")


def preset_phases(key: str) -> int:
    return 3 if key.startswith("3_") else 1


def preset_max_current(key: str) -> int | None:
    for phases, kw, amps in AC_PRESETS:
        if preset_key(phases, kw) == key:
            return amps
    return None


def preset_from_station(max_current_a: int, phases: int) -> str | None:
    """The charger reports its hardware limit and phase count over Bluetooth."""
    for ph, kw, amps in AC_PRESETS:
        if ph == phases and amps == max_current_a:
            return preset_key(ph, kw)
    return None
