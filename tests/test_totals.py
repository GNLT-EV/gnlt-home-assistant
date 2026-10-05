"""The start of a period announced to Home Assistant (last_reset).

Home Assistant counts the whole value of a NEW cycle as consumed since its
start. Announcing the running day's start at an update from 0.2.8 - when that
day's energy was already counted - added it again (stand, 04.10.2026:
"Energy today" +19.99 kWh in the long-term statistics)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from gnlt_charger.logic import Totals

TZ = "Europe/Warsaw"
WAW = ZoneInfo(TZ)


def test_day_carried_over_from_an_older_version_is_not_announced():
    t = Totals.from_dict({"day": "2026-10-04", "day_wh": 19990.0, "month": "2026-10", "month_wh": 19990.0})
    assert t.start_of("day", TZ, announced_only=True) is None
    assert t.start_of("month", TZ, announced_only=True) is None


def test_next_day_is_announced_from_its_midnight():
    t = Totals.from_dict({"day": "2026-10-04", "day_wh": 19990.0, "month": "2026-10", "month_wh": 19990.0})
    t.roll(datetime(2026, 10, 5, 0, 0, 5, tzinfo=WAW))
    assert t.start_of("day", TZ, announced_only=True) == datetime(2026, 10, 5, tzinfo=WAW)
    assert t.day_wh == 0
    # the month did not begin from zero under this version yet
    assert t.start_of("month", TZ, announced_only=True) is None
    t.roll(datetime(2026, 11, 1, 0, 0, 5, tzinfo=WAW))
    assert t.start_of("month", TZ, announced_only=True) == datetime(2026, 11, 1, tzinfo=WAW)


def test_new_installation_announces_its_first_day():
    t = Totals()
    t.add(100, 0.1, datetime(2026, 10, 4, 15, 0, tzinfo=WAW))
    assert t.start_of("day", TZ, announced_only=True) == datetime(2026, 10, 4, tzinfo=WAW)


def test_start_is_the_day_of_the_counters_not_of_the_clock():
    """At 00:00:30, before the counters roll, the value is still yesterday's:
    its start is yesterday's midnight (was: "now" -> today's midnight, and
    yesterday's value read as a new cycle)."""
    t = Totals()
    t.add(5000, 5.0, datetime(2026, 10, 4, 23, 50, tzinfo=WAW))
    assert t.start_of("day", TZ, announced_only=True) == datetime(2026, 10, 4, tzinfo=WAW)
    assert t.start_of("day", TZ, announced_only=False) == datetime(2026, 10, 4, tzinfo=WAW)


def test_flags_survive_storage():
    t = Totals()
    t.add(100, 0.1, datetime(2026, 10, 4, 15, 0, tzinfo=WAW))
    again = Totals.from_dict(t.as_dict())
    assert again.day_from_zero and again.month_from_zero
