"""Learned capabilities (modbus/profiles.py) and the removal of the firmware
profiles."""

import asyncio
import re
from pathlib import Path

import pytest

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import profiles
from gnlt_charger.modbus.profiles import LearnedCaps
from gnlt_charger.modbus.station import Station
from modbus_fake import Car, FakeClient, FakeStation, SimClock

MODBUS_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "gnlt_charger" / "modbus"


def test_no_firmware_profiles_stored_profile_ignored():
    """No A3B exception, no profiles; a Store written with a
    profile key (and learned values) still loads."""
    assert not {"PROFILES", "get_profile", "profile_from_serial", "profile_from_firmware"} & set(dir(profiles))
    assert not hasattr(C, "REFUSE_STOP_PROFILE")
    clock = SimClock()
    st = Station(
        FakeClient(FakeStation(clock=clock)), "101234567891", clock=clock,
        restored={"profile": "A3B_2.7", "learned": {"decimal_current": "yes", "stop_works": "no"}, "desired": {"current": 100}},
    )
    assert not hasattr(st, "profile") and not hasattr(st, "set_profile")
    assert st.desired["current"] == 100  # the rest of the Store loads
    assert st.learned.as_dict() == {"decimal_current": "unknown"}  # nothing learned is kept
    assert "profile" not in st.store_data() and "profile" not in st.diagnostics()


def test_a3b_serial_stops_like_any_station():
    """A portable EVB (serial 10...) gets 95 = 2 + 91 = 0 like
    every station (evcc sends its stop to any box)."""

    async def main():
        clock = SimClock()
        fake = FakeStation("A3B_2.7", "101234567891", clock)
        st = Station(FakeClient(fake), "101234567891", clock=clock, sleep=clock.sleep)
        fake.plug(Car())
        for _ in range(3):
            await st.async_tick()
            await clock.sleep(10)
        await st.async_write(95, 1, origin="user")
        for _ in range(6):
            await st.async_tick()
            await clock.sleep(10)
        res = await st.async_write(95, 2, origin="user")
        assert res.outcome != C.WRITE_NOT_NEEDED and (95, 2) in [(r, v) for _t, r, v in fake.writes]

    asyncio.run(main())


def test_learned_values_validated():
    caps = LearnedCaps({"stop_works": "yes", "decimal_current": "no", "junk": 1})
    assert caps.as_dict() == {"decimal_current": "no"}  # stop/phase values are not learned any more
    with pytest.raises(ValueError):
        caps.set("phase_write_with_car", "bad")  # removed with the chatter watchdog (item 7)
    assert caps.set("decimal_current", "yes") is True
    assert caps.set("decimal_current", "yes") is False


def test_no_phase_capability_without_reg_200():
    async def main():
        clock = SimClock()
        fake = FakeStation("unknown", "301234567890", clock, phases_readable=False)
        st = Station(FakeClient(fake), "301234567890", clock=clock, sleep=clock.sleep)
        fake.plug(Car())
        for _ in range(40):
            await st.async_tick()
            await clock.sleep(10)
        assert st.phase_capability is not True  # phase select only if reg 200 is readable

    asyncio.run(main())


def test_no_fw_number_gates():
    # EM2GO firmware numbers (1.1... 1.7) never gate behaviour.
    for path in MODBUS_DIR.glob("*.py"):
        code = "\n".join(
            line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
        )
        assert not re.search(r"""["']1\.[1-7]["']""", code), path.name
