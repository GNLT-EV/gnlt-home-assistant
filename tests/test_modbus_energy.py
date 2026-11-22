"""Energy accounting of the ModBus mode (modbus/energy.py) -....

Two kinds of tests: the pure ``EnergyMeter`` fed with hand-made samples (exact
arithmetic of both paths, prices, persistence), and the whole chain - fake
station -> ``Station`` -> ``StationEnergy`` -> ``EnergyMeter`` - on the
simulated clock, for the record lifecycle and the behaviour of real profiles
(B07 lazy reg 28, reg 28 = 0 on A3C, 1-phase B07 power).
"""

import asyncio
import copy
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gnlt_charger import logic
from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import energy as E
from gnlt_charger.modbus.energy import (
    EnergyMeter,
    Sample,
    StationEnergy,
)
from gnlt_charger.modbus.station import Station
from modbus_fake import Car, FakeClient, FakeStation, SimClock

SERIAL = "301234567890"  # A3C_2.7: reg 28 always 0
B07 = "101234567891"  # A3B_2.7: reg 28 in lazy 0.3 kWh steps, reg 12 = 0.91 x U*I
OTHER = "302606014614"
BASE = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
LIFETIME_TENTHS = 4860
ENERGY_PY = Path(__file__).resolve().parents[1] / "custom_components" / "gnlt_charger" / "modbus" / "energy.py"


def run(coro):
    return asyncio.run(coro)


# --- pure meter helpers ------------------------------------------------------------------


class Wall:
    def __init__(self, start=BASE):
        self.now = start

    def __call__(self):
        return self.now


def meter(source=E.DEFAULT_SOURCE, **kw):
    wall = Wall()
    return EnergyMeter(source=source, wall=wall, **kw), wall


def put(m, wall, t, power, status=4, *, reg28=None, plugged=True, current=None, voltage=230.0, poll=True):
    """One sample at t seconds after BASE (both clocks move together)."""
    wall.now = BASE + timedelta(seconds=t)
    amps = round(power / voltage + 0.05, 2) if current is None else current
    m.sample(
        Sample(
            t=1000.0 + t,
            status=status,
            power_w=power,
            currents=(amps, 0.0, 0.0),
            voltages=(voltage, 0.0, 0.0),
            meter_tenths=reg28,
            plugged=plugged,
            delivering=status == 4 and amps >= 1.0,
            poll=poll,
        )
    )


# --- station chain helpers ---------------------------------------------------------------


def chain(profile="A3C_2.7", serial=SERIAL, *, source=E.DEFAULT_SOURCE, restored=None, store=None, clock=None,
          tariff=None, fake=None, **quirks):
    clock = clock or SimClock()
    if fake is None:
        fake = FakeStation(profile, serial, clock, **quirks)
        # A used box has a non-zero lifetime meter (486 kWh); only A3C
        # read 0, and there it stays 0. A first non-zero value after zeros is
        # a baseline, never energy.
        if fake.q.reg28 == "lazy":
            fake.reg28 = LIFETIME_TENTHS
        elif fake.q.reg28 == "live":
            fake.meter_wh = LIFETIME_TENTHS * 100.0
    client = FakeClient(fake)
    st = Station(client, serial, restored=restored, store=store, clock=clock, sleep=clock.sleep)
    m = EnergyMeter(source=source, tariff=tariff, wall=lambda: BASE + timedelta(seconds=clock() - 1000.0), name=serial)
    se = StationEnergy(st, m)
    return fake, st, m, se, clock


async def drive(station, clock, seconds):
    """What the poll loop does, on the simulated clock."""
    end = clock() + seconds
    while clock() < end:
        started = clock()
        await station.async_tick()
        elapsed = clock() - started
        await clock.sleep(max(station.next_delay() - elapsed, station.timing.min_poll_s - elapsed, 0.0))


async def start_charge(fake, st, clock, car=None, setpoint=100):
    """A charge started at the station (buttons): adopted, no write."""
    fake.setpoint = setpoint
    fake.plug(car or Car())
    await drive(st, clock, 30)
    fake.external_start()
    await drive(st, clock, 30)
    assert st.snapshot.status == C.STATUS_CHARGING


class MemoryStore:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.saves += 1

    async def async_remove(self):
        self.data = None

    def async_delay_save(self, func, delay):
        pass


# --- shared functions -------------------------------------------------------


def test_energy_imports_shared_functions():
    assert E.Tariff is logic.Tariff
    assert E.Totals is logic.Totals
    assert E.apparent_power_va is logic.apparent_power_va
    assert isinstance(EnergyMeter().totals, logic.Totals)


def test_totals_shared_classes():
    m, wall = meter()
    put(m, wall, 0, 0)
    put(m, wall, 60, 3600)  # trapezoid: 30 Wh
    assert type(m.totals) is logic.Totals
    assert m.totals.day_wh == pytest.approx(30.0)
    assert m.totals.month_wh == pytest.approx(30.0)
    assert m.total_wh == pytest.approx(30.0)
    # Totals roll at a new day and never back (logic.Totals rules).
    m.roll(BASE + timedelta(days=1))
    assert m.totals.day_wh == 0.0 and m.totals.month_wh == pytest.approx(30.0)
    m.roll(BASE)
    assert m.totals.day == (BASE + timedelta(days=1)).strftime("%Y-%m-%d")


# --- ------------------------------------------------------------------------------


def test_default_integral():
    assert E.DEFAULT_SOURCE == "integral"
    assert EnergyMeter().source == "integral"
    assert EnergyMeter(source="bogus").source == "integral"


def test_both_paths_computed():
    """B07, 25 min at 10 A: the integral and the lazy reg 28 both count."""

    async def main():
        fake, st, m, _se, clock = chain("A3B_2.7", B07, reg28_every_s=600.0)
        await start_charge(fake, st, clock)
        await drive(st, clock, 25 * 60)
        rec = m.record
        assert rec.source == "integral"
        assert rec.integral_wh > 700
        assert rec.register_wh >= 600 and rec.register_wh % 300 == 0
        attrs = m.sensor_values()["session_energy"].attributes
        assert attrs["energy_integral_kwh"] == pytest.approx(rec.integral_wh / 1000, abs=0.001)
        assert attrs["energy_register_kwh"] == pytest.approx(rec.register_wh / 1000, abs=0.001)
        # Totals follow the integral only (the option), never both.
        assert m.total_wh == pytest.approx(rec.integral_wh)

    run(main())


def test_source_option_switch_applies_next_record():
    m, wall = meter("integral")
    put(m, wall, 0, 2000, reg28=100)
    m.set_source("register")
    put(m, wall, 10, 2000, reg28=100)
    assert m.record.source == "integral"  # never changes mid-record
    put(m, wall, 20, 0, status=2, plugged=False)
    assert m.record is None and m.last.source == "integral"
    put(m, wall, 2000, 2000, reg28=100)
    assert m.record.source == "register"


@pytest.mark.parametrize("source", ["integral", "register"])
def test_paths_consistent_20min_10a(source):
    """Simulated 20-min 10 A charge (fake 'unknown' profile: reg 12 = U*I,
    live reg 28): both paths agree within reg 28's 0.1 kWh resolution and
    its 60 s read interval."""

    async def main():
        fake, st, m, _se, clock = chain("unknown", SERIAL, source=source)
        await start_charge(fake, st, clock)
        await drive(st, clock, 20 * 60)
        fake.unplug()
        await drive(st, clock, 180)  # unplug confirmed + late reg-28 reads
        rec = m.last
        assert rec.reason == "unplug"
        delivered = fake.meter_wh - LIFETIME_TENTHS * 100.0
        assert delivered == pytest.approx(rec.integral_wh, rel=0.03)
        assert abs(rec.register_wh - rec.integral_wh) <= 150.0
        assert rec.ui_wh == pytest.approx(rec.integral_wh, rel=0.03)
        assert m.total_wh == pytest.approx(rec.energy_wh)
        assert rec.approximate is False

    run(main())


# --- ------------------------------------------------------------------------------


def test_integral_trapezoid():
    m, wall = meter()
    put(m, wall, 0, 0)
    put(m, wall, 10, 1000)
    assert m.record.integral_wh == pytest.approx(1000 / 2 * 10 / 3600)
    put(m, wall, 20, 1000)
    assert m.record.integral_wh == pytest.approx(1000 / 2 * 10 / 3600 + 1000 * 10 / 3600)
    # Outside {3, 4} nothing flows (the station reports 0 anyway).
    before = m.record.integral_wh
    put(m, wall, 30, 1000, status=2)
    assert m.record.integral_wh == pytest.approx(before + 1000 / 2 * 10 / 3600)


def test_integral_counts_sub_1a_tail():
    m, wall = meter()
    put(m, wall, 0, 2000)
    put(m, wall, 5, 200, current=0.8)  # below the 1 A "delivering" threshold
    put(m, wall, 10, 200, current=0.8)
    assert m.record.integral_wh == pytest.approx((2000 + 200) / 2 * 5 / 3600 + 200 * 5 / 3600)


def test_integral_gap_not_integrated():
    m, wall = meter()
    put(m, wall, 0, 3000)
    put(m, wall, 60, 3000)  # exactly the limit: integrated
    assert m.record.integral_wh == pytest.approx(50.0)
    assert not m.record.approximate
    put(m, wall, 121, 3000)  # 61 s: link loss, not integrated
    assert m.record.integral_wh == pytest.approx(50.0)
    assert m.record.approximate and "gap" in m.record.notes
    put(m, wall, 126, 3000)
    assert m.record.integral_wh == pytest.approx(50.0 + 3000 * 5 / 3600)
    assert m.diag["gaps"] == 1


def test_ui_integral_attribute():
    m, wall = meter()
    put(m, wall, 0, 1800, current=10.0, voltage=230.0)  # reg 12 below U*I (B07 style)
    put(m, wall, 36, 1800, current=10.0, voltage=230.0)
    assert m.record.integral_wh == pytest.approx(18.0)
    assert m.record.ui_wh == pytest.approx(23.0)
    assert m.sensor_values()["session_energy"].attributes["energy_ui_kwh"] == pytest.approx(0.023)


def test_b07_one_phase_power_integral():
    """B07 with a 1-phase car: reg 12 is integrated as read (0.91 x U*I,
    100 W steps), the U*I integral is only an attribute, and does not
    take the low reading for a glitch."""

    async def main():
        fake, st, m, _se, clock = chain("A3B_2.7", B07)
        await start_charge(fake, st, clock, Car(phases=1), setpoint=100)
        assert st.snapshot.power_w == 1900  # round100(0.91 x 232.2 V x 9.2 A)
        t0 = m.record.integral_wh
        await drive(st, clock, 600)
        rec = m.record
        assert rec.integral_wh - t0 == pytest.approx(1900 * 600 / 3600, rel=0.02)
        assert rec.integral_wh / rec.ui_wh == pytest.approx(0.89, abs=0.02)
        assert m.diag["power_dropped"] == 0
        assert m.total_wh == pytest.approx(rec.integral_wh)

    run(main())


# --- ------------------------------------------------------------------------------


def test_reg28_delta_steps():
    """B07 lazy steps of 0.3 kWh are the register energy; with source
    ``register`` they are the session, totals and money."""

    async def main():
        tariff = logic.Tariff(price=1.0)
        fake, st, m, _se, clock = chain("A3B_2.7", B07, source="register", tariff=tariff)
        await start_charge(fake, st, clock)
        await drive(st, clock, 25 * 60)
        rec = m.record
        assert rec.source == "register"
        assert rec.register_wh in (600.0, 900.0)
        assert m.total_wh == rec.register_wh
        assert m.sensor_values()["session_energy"].value == pytest.approx(rec.register_wh / 1000)
        assert m.sensor_values()["session_cost"].value == pytest.approx(rec.register_wh / 1000)

    run(main())


def test_reg28_drop_rebase():
    m, wall = meter("register")
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 600, 2000, reg28=1003)
    put(m, wall, 605, 2000, reg28=990)  # drop: rebased, no negative delta
    put(m, wall, 1200, 2000, reg28=993)
    assert m.record.register_wh == pytest.approx(600.0)
    assert m.record.approximate and "reg28_rebase" in m.record.notes
    assert m.diag["reg28_rebases"] == 1
    assert m.total_wh == pytest.approx(600.0)


def test_reg28_jump_rejected():
    m, wall = meter("register", p_max_w=E.p_max_w("3_11"))
    assert E.p_max_w("3_11") == 11000.0
    assert E.p_max_w(None) == 22000.0
    put(m, wall, 0, 2000, reg28=1000)
    # 60 s at 11 kW is 183 Wh + 300 Wh slack: +500 kWh (factor-10 bug) is not energy.
    put(m, wall, 60, 2000, reg28=6000)  # held until a second read confirms the new level
    assert m.record.register_wh == 0.0 and m.diag["reg28_rebases"] == 0
    put(m, wall, 660, 2000, reg28=6003)
    assert m.record.register_wh == pytest.approx(300.0) and m.diag["reg28_rebases"] == 1
    # A big but plausible lazy step (10 min at up to 11 kW, + REG28_LATE_S) is accepted.
    put(m, wall, 1260, 2000, reg28=6018)
    assert m.record.register_wh == pytest.approx(1800.0)


def test_reg28_zero_after_nonzero_ignored():
    m, wall = meter("register")
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 60, 2000, reg28=0)  # not a reading of a lifetime meter
    put(m, wall, 600, 2000, reg28=1003)
    assert m.record.register_wh == pytest.approx(300.0)
    assert m.diag["reg28_zero_ignored"] == 1 and m.diag["reg28_rebases"] == 0
    assert "reg28_rebase" not in m.record.notes


def test_reg28_zero_station_unavailable():
    async def main():
        fake, st, m, _se, clock = chain("A3C_2.7", SERIAL)  # reg 28 always 0
        await start_charge(fake, st, clock)
        await drive(st, clock, 300)
        fake.unplug()
        await drive(st, clock, 30)
        rec = m.last
        assert rec.register_wh == 0.0 and rec.integral_wh > 100
        assert m.register_available is False
        assert m.sensor_values()["session_energy"].attributes["register_path"] == "unavailable"

    run(main())


# --- ------------------------------------------------------------------------------


def _closed_record(source):
    m, wall = meter(source, tariff=logic.Tariff(price=1.0))
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 600, 2000, reg28=1003)
    put(m, wall, 610, 0, status=2, reg28=1003, plugged=False)
    assert m.record is None and m.last.reason == "unplug"
    return m, wall


def test_reg28_late_step_attributed_register_only():
    m, wall = _closed_record("register")
    total = m.total_wh
    put(m, wall, 1400, 0, status=1, reg28=1006, plugged=False)  # 790 s after close
    assert m.last.register_wh == pytest.approx(600.0)
    assert m.total_wh == pytest.approx(total + 300.0)
    assert m.last_session["energy_kwh"] == pytest.approx(0.6)
    assert m.last_session["cost"] == pytest.approx(0.6)
    # Later than REG28_LATE_S: totals only (register mode), not the record.
    put(m, wall, 1600, 0, status=1, reg28=1009, plugged=False)
    assert m.last.register_wh == pytest.approx(600.0)
    assert m.total_wh == pytest.approx(total + 600.0)

    m, wall = _closed_record("integral")
    total, reg = m.total_wh, m.last.register_wh
    put(m, wall, 1400, 0, status=1, reg28=1006, plugged=False)
    assert m.last.register_wh == reg and m.total_wh == total  # integral record: ignored
    assert m.diag["late_steps_ignored_wh"] == pytest.approx(300.0)


def test_late_step_with_new_record_by_timing():
    m, wall = _closed_record("register")
    put(m, wall, 700, 0, reg28=1003)  # a new record: status 4, not delivering yet
    new = m.record
    assert new is not None and not new.delivered
    put(m, wall, 710, 0, reg28=1006)  # still the closed one's step
    assert m.last.register_wh == pytest.approx(600.0) and new.register_wh == 0.0
    put(m, wall, 715, 2000, reg28=1006)  # the new record delivers
    put(m, wall, 720, 2000, reg28=1009)
    assert new.register_wh == pytest.approx(300.0) and m.last.register_wh == pytest.approx(600.0)


def test_source_switch_no_double_count():
    """Record 1 counted by its integral; the option changes to register; the
    late reg-28 step of record 1 must not be counted again."""

    async def main():
        fake, st, m, _se, clock = chain("A3B_2.7", B07, source="integral")
        await start_charge(fake, st, clock)
        await drive(st, clock, 12 * 60)
        fake.unplug()
        await drive(st, clock, 30)
        first = m.last
        assert first.source == "integral" and first.reason == "unplug"
        total = m.total_wh
        assert total == pytest.approx(first.integral_wh)
        m.set_source("register")
        fake.reg28 += 3  # the box adds the rest of the charge late
        await drive(st, clock, 120)
        assert m.total_wh == pytest.approx(total)
        assert first.register_wh == 300.0  # only the step seen while open
        # Record 2 is register-sourced; its late step goes to it and to totals.
        await start_charge(fake, st, clock)
        await drive(st, clock, 12 * 60)
        fake.unplug()
        await drive(st, clock, 30)
        second = m.last
        assert second.source == "register" and second.rid == first.rid + 1
        before = m.total_wh
        m.set_source("integral")
        fake.reg28 += 3
        await drive(st, clock, 120)
        assert second.register_wh == pytest.approx(600.0)
        assert m.total_wh == pytest.approx(before + 300.0)
        assert m.total_wh == pytest.approx(first.integral_wh + second.register_wh)

    run(main())


# ---, C-42, C-37 -----------------------------------------------------------------


def test_reg72_not_in_totals():
    async def main():
        results = []
        for override in (False, True):
            fake, st, m, _se, clock = chain("unknown", SERIAL)
            if override:
                # Reg 72 nonsense, per-phase power x10 (C-42) and reg 78 do not matter.
                fake.overrides.update({72: 9999, 16: 0x7, 18: 0, 78: 0, 80: 7})  # U32 words at a and a+2 (stride 2)
            await start_charge(fake, st, clock)
            await drive(st, clock, 300)
            results.append((m.total_wh, m.record.integral_wh, m.record.register_wh))
        assert results[0] == pytest.approx(results[1])

    run(main())
    source = ENERGY_PY.read_text(encoding="utf-8")
    for name in ("REG_SESSION_ENERGY", "REG_POWER_L1", "REG_POWER_L2", "REG_POWER_L3", "REG_DURATION",
                 "session_energy_kwh", "per_phase_power_raw", "duration_s"):
        assert name not in source


# --- ------------------------------------------------------------------------------


def test_deltas_priced_at_time():
    """A night price from 23:00: the energy before is priced at the day price,
    the energy after at the night price - each delta at its own moment."""
    tariff = logic.Tariff(price=1.0, night_price=0.5, night_start_min=23 * 60, night_end_min=7 * 60)
    wall = Wall(datetime(2026, 11, 19, 22, 58, tzinfo=UTC))
    m = EnergyMeter(tariff=tariff, wall=wall)

    def at(minute_offset, t):
        wall.now = datetime(2026, 11, 19, 22, 58, tzinfo=UTC) + timedelta(minutes=minute_offset)
        m.sample(Sample(t=t, status=4, power_w=3600, currents=(16.0, 0, 0), voltages=(230.0, 0, 0), delivering=True))

    at(0, 0.0)
    at(1, 60.0)  # 22:59: 60 Wh at 1.0
    at(2, 120.0)  # 23:00: 60 Wh at 0.5
    at(3, 180.0)  # 23:01: 60 Wh at 0.5
    assert m.record.cost == pytest.approx(0.06 + 0.03 + 0.03)
    assert m.totals.day_cost == pytest.approx(0.12)
    assert m.sensor_values()["cost_today"].value == pytest.approx(0.12)
    # Without a tariff money is unknown, not 0 (OCPP parity).
    assert EnergyMeter().sensor_values()["session_cost"].value is None


# --- ------------------------------------------------------------------------------


def test_record_opens_on_4():
    async def main():
        fake, st, m, _se, clock = chain()
        fake.plug(Car())
        await drive(st, clock, 30)
        assert m.record is None and st.record_open is False
        fake.external_start()
        await clock.sleep(1)
        await st.async_tick()
        assert st.snapshot.status == C.STATUS_STARTING and m.record is None
        await drive(st, clock, 20)
        assert m.record is not None and m.record.rid == 1
        assert st.record_open is True

    run(main())


def test_record_not_split_by_car_pause():
    """Car pause 4 -> 2 for 3 min with the cable in: one record, counted once."""

    async def main():
        fake, st, m, _se, clock = chain()
        await start_charge(fake, st, clock)
        await drive(st, clock, 300)
        rid = m.record.rid
        fake.car_pause()
        await drive(st, clock, 180)
        assert st.snapshot.status == C.STATUS_CONNECTED
        assert m.record is not None and m.record.rid == rid and st.record_open
        paused = m.record.integral_wh
        fake.car_resume()
        await drive(st, clock, 300)
        assert m.record.rid == rid and m.last is None
        assert m.record.integral_wh > paused + 100
        assert m.totals.month_sessions == 1
        assert not m.record.approximate

    run(main())


def test_record_counted_once():
    m, wall = meter()
    put(m, wall, 0, 2000)
    put(m, wall, 10, 0, status=2)  # car pause
    put(m, wall, 20, 2000)
    assert m.totals.month_sessions == 1
    put(m, wall, 30, 0, status=2, plugged=False)
    # A record that never delivered (car asleep) is not a charge for the count.
    put(m, wall, 100, 0, status=4)
    put(m, wall, 110, 0, status=2, plugged=False)
    assert m.last.rid == 2 and m.totals.month_sessions == 1
    # A delivered one at 0 Wh is (W23/W24).
    put(m, wall, 200, 0, status=4, current=6.0)
    assert m.record.delivered and m.totals.month_sessions == 2


def test_record_closes_on_own_stop_any_outcome():
    async def main():
        # The stop works: closes as soon as the station is out of 4.
        fake, st, m, se, clock = chain()
        await start_charge(fake, st, clock)
        se.note_stop("user_stop")
        await drive(st, clock, 10)
        assert m.record is not None  # still 4: open
        fake._write(C.REG_COMMAND, C.COMMAND_STOP)
        await drive(st, clock, 10)
        assert m.record is None and m.last.reason == "user_stop" and not st.record_open
        # B07 ignores the stop: the record stays open while it is 4...
        fake, st, m, se, clock = chain("A3B_2.7", B07)
        await start_charge(fake, st, clock)
        se.note_stop("schedule_end")
        fake._write(C.REG_COMMAND, C.COMMAND_STOP)
        await drive(st, clock, 120)
        assert st.snapshot.status == C.STATUS_CHARGING and m.record is not None
        # ... and closes with the stop's reason once it leaves 4.
        fake.car_pause()
        await drive(st, clock, 10)
        assert m.last.reason == "schedule_end"
        # Our start after a stop: the old stop no longer decides.
        m2, wall = meter()
        put(m2, wall, 0, 2000)
        m2.note_stop()
        m2.note_start()
        put(m2, wall, 10, 0, status=2)
        assert m2.record is not None

    run(main())


def test_record_unplug_closes_at_the_first_poll():
    """the unplug as reg 2 shows it, at once (no 2-poll vote)."""

    async def main():
        fake, st, m, _se, clock = chain()
        await start_charge(fake, st, clock)
        await drive(st, clock, 60)
        fake.unplug()
        await clock.sleep(5)
        await st.async_tick()
        assert st.snapshot.cable == 0 and m.record is None and m.last.reason == "unplug"

    run(main())


def test_record_closes_on_status6():
    async def main():
        fake, st, m, _se, clock = chain()
        await start_charge(fake, st, clock)
        fake.car_finish()
        await drive(st, clock, 10)
        assert m.last.reason == "station" and m.record is None

    run(main())


def test_record_closes_power_loss_on_restart():
    """ restart (93 -> 0 with 91 -> 160 after power-on) closes the record
    as power_loss; the charge the box resumes by itself (P&C after boot) is a
    new record."""

    async def main():
        fake, st, m, _se, clock = chain()
        await start_charge(fake, st, clock, setpoint=100)
        await drive(st, clock, 120)
        assert m.record.rid == 1
        fake.power_loss()
        await drive(st, clock, 150)
        assert m.last.rid == 1 and m.last.reason == "power_loss"
        assert m.record is not None and m.record.rid == 2
        assert m.totals.month_sessions == 2

    run(main())


def test_record_reason_phase_switch():
    m, wall = meter()
    put(m, wall, 0, 2000)
    put(m, wall, 10, 0, status=2)  # inside the station is out of 4
    assert m.record is not None  # an own phase sequence does not close it
    m.end_phase_switch(restarted=True)
    put(m, wall, 20, 2000)
    assert m.record is not None
    put(m, wall, 30, 0, status=2)
    m.end_phase_switch(restarted=False)
    put(m, wall, 40, 0, status=2)
    assert m.record is None and m.last.reason == "phase_switch"


# --- ------------------------------------------------------------------------------


def test_energy_store_section_roundtrip():
    async def main():
        fake, st, m, _se, clock = chain("A3B_2.7", B07, source="register")
        await start_charge(fake, st, clock)
        await drive(st, clock, 15 * 60)
        data = copy.deepcopy(st.store_data())
        assert data["energy"]["record"]["rid"] == 1
        assert data["serial"] == B07  # the station's own keys are untouched
        st2 = Station(FakeClient(fake), B07, restored=data, clock=clock, sleep=clock.sleep)
        m2 = EnergyMeter()
        se2 = StationEnergy(st2, m2)
        assert m2.record.as_dict() == m.record.as_dict()
        assert m2.total_wh == m.total_wh and m2.totals == m.totals
        assert m2.as_dict()["reg28"] == m.as_dict()["reg28"]
        assert st2.record_open is True
        se2.close()
        # Registered before the Store is loaded: loaded by async_load.
        st3 = Station(FakeClient(fake), B07, store=MemoryStore(data), clock=clock, sleep=clock.sleep)
        m3 = EnergyMeter()
        StationEnergy(st3, m3)
        assert m3.record is None
        await st3.async_load()
        assert m3.record.rid == 1

    run(main())


def test_restart_mid_charge_continues_record():
    """HA crash mid-charge: the Store is up to 60 s old. The record goes on
    (same rid), is approximate (HA downtime is not integrated), total_energy
    never steps back below HA's last state, and reg 28 continues from its
    stored baseline."""

    async def main():
        clock = SimClock()
        fake, st, m, _se, _ = chain("A3B_2.7", B07, clock=clock)
        await start_charge(fake, st, clock)
        await drive(st, clock, 11 * 60)
        saved = copy.deepcopy(st.store_data())  # last debounced save
        await drive(st, clock, 50)
        last_state = m.sensor_values()["total_energy"].value
        rid, register = m.record.rid, m.record.register_wh
        # crash: nothing more is saved; the box drops the dead socket
        fake.kill_sessions()
        await clock.sleep(90)
        _f, st2, m2, _se2, _ = chain("A3B_2.7", B07, clock=clock, fake=fake, restored=saved)
        m2.restore_total(last_state)
        assert m2.sensor_values()["total_energy"].value == pytest.approx(last_state)
        await drive(st2, clock, 15 * 60)
        rec = m2.record
        assert rec is not None and rec.rid == rid
        assert rec.approximate and "ha_restart" in rec.notes
        assert m2.sensor_values()["total_energy"].value > last_state
        assert rec.register_wh >= register + 300  # the next lazy step is counted
        assert m2.totals.month_sessions == 1  # not counted again
        assert not m2.diag["reg28_rebases"]

    run(main())


def test_total_energy_never_steps_back_after_crash():
    m, wall = meter()
    put(m, wall, 0, 3600)
    put(m, wall, 60, 3600)
    data = m.as_dict()
    m2 = EnergyMeter()
    m2.load(data)
    m2.restore_total(0.5)  # HA showed more than the Store has
    assert m2.total_energy_wh == pytest.approx(500.0)
    m2.restore_total(0.01)  # less: never back
    assert m2.total_energy_wh == pytest.approx(500.0)
    m2.restore_total(None)
    assert m2.total_energy_wh == pytest.approx(500.0)


def test_save_on_record_close():
    async def main():
        store = MemoryStore()
        fake, st, m, _se, clock = chain(store=store)
        await start_charge(fake, st, clock)
        await drive(st, clock, 60)
        assert store.saves == 0
        fake.unplug()
        await drive(st, clock, 30)
        assert store.saves >= 1
        assert store.data["energy"]["last"]["reason"] == "unplug"
        assert store.data["energy"]["record"] is None

    run(main())


# --- -------------------------------------------------------------------------


def test_energy_sensor_classes():
    assert E.SENSOR_CLASSES["session_energy"] == ("energy", "total", True)
    assert E.SENSOR_CLASSES["total_energy"] == ("energy", "total", False)
    assert E.SENSOR_CLASSES["sessions_month"] == (None, "total_increasing", False)
    for key in ("energy_today", "energy_month", "cost_today", "cost_month"):
        assert E.SENSOR_CLASSES[key][1:] == ("total", True)
    m, wall = meter(tariff=logic.Tariff(price=1.0))
    values = m.sensor_values()
    assert set(values) == set(E.SENSOR_CLASSES)
    assert values["total_energy"] == E.SensorValue(0.0)
    assert values["session_energy"].value is None
    put(m, wall, 0, 3600)
    put(m, wall, 60, 3600)
    values = m.sensor_values()
    assert values["session_energy"].last_reset == BASE  # the record start
    assert values["total_energy"].last_reset is None
    assert values["energy_today"].last_reset is not None  # day seen from zero
    assert values["cost_today"].value == pytest.approx(0.06)


def test_last_session_attributes():
    m, wall = meter(tariff=logic.Tariff(price=2.0))
    put(m, wall, 0, 3600)
    put(m, wall, 60, 3600)
    put(m, wall, 120, 0, status=2)
    put(m, wall, 180, 0, status=1, plugged=False)
    last = m.sensor_values()["last_session"]
    assert last.value == pytest.approx(0.09)
    assert set(last.attributes) == {"started", "finished", "minutes", "cost", "approximate", "reason", "energy_source"}
    assert last.attributes["minutes"] == 3
    assert last.attributes["reason"] == "unplug"
    assert last.attributes["energy_source"] == "integral"
    assert last.attributes["cost"] == pytest.approx(0.18)
    assert last.attributes["approximate"] is False


# --- ------------------------------------------------------------------------------


def test_power_implausible_dropped(caplog):
    m, wall = meter()
    put(m, wall, 0, 2000, current=9.0)
    with caplog.at_level(logging.WARNING, logger="gnlt_charger.modbus.energy"):
        put(m, wall, 5, 262144, current=9.0)  # firmware glitch: previous value kept
        put(m, wall, 10, 262144, current=9.0)
    assert m.record.integral_wh == pytest.approx(2000 * 10 / 3600)
    assert m.diag["power_dropped"] == 2
    assert len([r for r in caplog.records if "dropped" in r.getMessage()]) == 1  # once per charge
    # Within 1.2 x U*I + 200 W: kept.
    put(m, wall, 15, int(1.2 * 230 * 9.0 + 199), current=9.0)
    assert m.diag["power_dropped"] == 2


# --- review round 1 of step 4 ------------------------------


class HaDelayStore(MemoryStore):
    """``async_delay_save`` with Home Assistant's semantics on the simulated
    clock: every call cancels the pending write and schedules a new one
    ``delay`` seconds later (homeassistant.helpers.storage.Store)."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.due = None
        self.func = None

    def async_delay_save(self, func, delay):
        self.func, self.due = func, self.clock() + delay

    async def fire_due(self):
        if self.due is not None and self.clock() >= self.due:
            self.due = None
            await self.async_save(self.func())


def test_store_written_while_polling_with_ha_delay_semantics():
    """C1: a poll every 5 s must not push the debounced save forever; during
    a 10-min charge the open record reaches the Store."""

    async def main():
        clock = SimClock()
        store = HaDelayStore(clock)
        fake, st, m, _se, _ = chain(store=store, clock=clock)
        fake.setpoint = 100
        fake.plug(Car())
        fake.external_start()
        end = clock() + 600
        while clock() < end:
            started = clock()
            await st.async_tick()
            await store.fire_due()
            elapsed = clock() - started
            await clock.sleep(max(st.next_delay() - elapsed, st.timing.min_poll_s - elapsed, 0.0))
        assert m.record is not None
        assert store.saves >= 1, "nothing written while polling"
        assert store.data["energy"]["record"]["rid"] == m.record.rid

    run(main())


def test_m1_register_mode_crash_no_double_count():
    m, wall = meter("register")
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 600, 2000, reg28=1003)
    saved = copy.deepcopy(m.as_dict())  # last save
    put(m, wall, 650, 2000, reg28=1006)  # a step after the save
    last_state = m.sensor_values()["total_energy"].value
    assert last_state == pytest.approx(0.6)
    m2 = EnergyMeter(source="register", wall=wall)
    m2.load(saved)
    m2.restore_total(last_state)
    put(m2, wall, 700, 2000, reg28=1006)  # the same value again: recovered, not counted twice
    assert m2.sensor_values()["total_energy"].value == pytest.approx(0.6)
    put(m2, wall, 1300, 2000, reg28=1009)
    assert m2.sensor_values()["total_energy"].value == pytest.approx(0.9)
    # what is saved next is what was shown
    assert m2.as_dict()["total_wh"] == pytest.approx(900.0)


def test_m1_integral_crash_floor_plus_new():
    m, wall = meter("integral")
    put(m, wall, 0, 3600)
    put(m, wall, 60, 3600)  # 60 Wh
    saved = copy.deepcopy(m.as_dict())
    put(m, wall, 120, 3600)  # 60 Wh more, lost with the crash
    last_state = m.sensor_values()["total_energy"].value
    m2 = EnergyMeter(wall=wall)
    m2.load(saved)
    m2.restore_total(last_state)
    put(m2, wall, 200, 3600)
    put(m2, wall, 260, 3600)  # 60 Wh after the restart
    assert m2.sensor_values()["total_energy"].value == pytest.approx(0.18)


def test_register_mode_crash_chain():
    """HA crash in register mode on B07: after the restart total_energy equals
    the register energy of the charge - never stepped back, never doubled."""

    async def main():
        clock = SimClock()
        fake, st, m, _se, _ = chain("A3B_2.7", B07, source="register", clock=clock)
        await start_charge(fake, st, clock)
        await drive(st, clock, 9 * 60)
        saved = copy.deepcopy(st.store_data())
        await drive(st, clock, 3 * 60)  # the 10-min lazy step lands after the save
        last_state = m.sensor_values()["total_energy"].value
        assert last_state == pytest.approx(m.record.register_wh / 1000)
        assert last_state > 0
        fake.kill_sessions()
        await clock.sleep(30)
        _f, st2, m2, _se2, _ = chain("A3B_2.7", B07, source="register", clock=clock, fake=fake, restored=saved)
        m2.restore_total(last_state)
        await drive(st2, clock, 60)
        assert m2.sensor_values()["total_energy"].value == pytest.approx(last_state)
        await drive(st2, clock, 12 * 60)
        rec = m2.record
        assert rec.register_wh >= 600
        assert m2.sensor_values()["total_energy"].value == pytest.approx(rec.register_wh / 1000)

    run(main())


def test_m2_reg28_bound_not_grown_by_idle_time():
    m, wall = meter("register", tariff=logic.Tariff(price=1.0))
    put(m, wall, 0, 0, status=1, reg28=0, plugged=False)
    put(m, wall, 10, 0, status=1, reg28=0, plugged=False)
    ten_days = 10 * 24 * 3600
    put(m, wall, ten_days, 0, status=1, reg28=48600, plugged=False)  # lifetime value appears
    put(m, wall, ten_days + 10, 0, status=1, reg28=48600, plugged=False)
    assert m.total_wh == 0.0 and m.totals.month_cost == 0.0
    # factor-10 glitch after 3 idle days (persisting over two reads): rebased, not energy
    m, wall = meter("register", p_max_w=E.p_max_w("3_11"))
    put(m, wall, 0, 0, status=1, reg28=10000, plugged=False)
    put(m, wall, 3 * 24 * 3600, 0, status=1, reg28=100000, plugged=False)
    put(m, wall, 3 * 24 * 3600 + 60, 0, status=1, reg28=100000, plugged=False)
    assert m.total_wh == 0.0 and m.diag["reg28_rebases"] == 1


def test_m3_stale_stop_does_not_split_car_pause():
    m, wall = meter()
    put(m, wall, 0, 2000)
    m.note_stop("user_stop")  # B07 ignores it: stays 4
    for t in range(5, 3600, 5):
        put(m, wall, t, 2000)
    put(m, wall, 3600, 0, status=2)  # a car pause an hour later
    put(m, wall, 3605, 0, status=2)
    put(m, wall, 3700, 2000)
    assert m.record.rid == 1 and m.last is None and m.totals.month_sessions == 1
    # a slow stop inside STOP_VERIFY_S + STOP_LATE_S still closes as ours
    m, wall = meter()
    put(m, wall, 0, 2000)
    m.note_stop("user_stop")
    for t in range(5, 300, 5):
        put(m, wall, t, 2000)
    put(m, wall, 300, 0, status=2)
    assert m.last.reason == "user_stop"


def test_m4_sensor_classes_match_ocpp():
    from gnlt_charger.sensor import SENSORS

    def value(x):
        return None if x is None else str(getattr(x, "value", x))

    ocpp = {d.key: (value(d.device_class), value(d.state_class), d.last_reset is not None) for d in SENSORS}
    for key, ours in E.SENSOR_CLASSES.items():
        assert key in ocpp, key
        assert ours == ocpp[key], key


def test_m1_minor_single_glitch_keeps_energy():
    m, wall = meter("register", p_max_w=E.p_max_w("3_11"))
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 60, 2000, reg28=10010)  # one glitched read
    put(m, wall, 120, 2000, reg28=1003)  # back to normal
    put(m, wall, 180, 2000, reg28=1003)
    assert m.record.register_wh == pytest.approx(300.0)
    assert m.diag["reg28_rebases"] == 0 and "reg28_rebase" not in m.record.notes


def test_m2_minor_late_step_after_window_follows_last_source():
    m, wall = meter("integral")
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 30, 2000, reg28=1000)
    put(m, wall, 40, 0, status=2, reg28=1000, plugged=False)
    total = m.total_wh
    m.set_source("register")
    put(m, wall, 40 + 901, 0, status=1, reg28=1003, plugged=False)
    assert m.total_wh == pytest.approx(total)


def test_m3_minor_empty_record_does_not_take_late_step():
    m, wall = meter("register", tariff=logic.Tariff(price=1.0))
    put(m, wall, 0, 2000, reg28=1000)
    for t in range(30, 601, 30):
        put(m, wall, t, 2000, reg28=1000 if t < 600 else 1003)
    put(m, wall, 610, 0, status=2, reg28=1003, plugged=False)
    first = m.last
    put(m, wall, 700, 0, status=4, reg28=1003, current=0.0)  # car never takes current
    put(m, wall, 710, 0, status=2, reg28=1003, plugged=False, current=0.0)
    empty = m.last
    assert empty.rid == 2 and not empty.delivered
    put(m, wall, 900, 0, status=1, reg28=1006, plugged=False, current=0.0)  # record 1's late step
    assert first.register_wh == pytest.approx(600.0)
    assert empty.register_wh == 0.0 and empty.cost == 0.0
    assert m.total_wh == pytest.approx(600.0)


def test_m4_minor_status3_energy_in_session():
    m, wall = meter(tariff=logic.Tariff(price=1.0))
    put(m, wall, 0, 1500, status=3)
    put(m, wall, 30, 1500, status=3)
    put(m, wall, 60, 1500, status=4)
    assert m.record.integral_wh == pytest.approx(1500 * 60 / 3600)
    assert m.total_wh == pytest.approx(m.record.integral_wh)
    assert m.record.cost == pytest.approx(m.record.integral_wh / 1000)
    assert m.diag["unattributed_wh"] == 0.0
    # a start that fails in 3: the energy still reaches the totals
    m, wall = meter()
    put(m, wall, 0, 1500, status=3)
    put(m, wall, 30, 1500, status=3)
    put(m, wall, 40, 0, status=2)
    assert m.record is None and m.total_wh == pytest.approx(1500 * 30 / 3600 + 1500 / 2 * 10 / 3600)


@pytest.mark.parametrize(
    "bad",
    [
        {"record": {"rid": 1}},
        {"record": {"rid": 1, "source": "integral", "started": "x", "integral_wh": None}},
        {"record": {"rid": 1, "source": "integral", "started": BASE.isoformat(), "integral_wh": None}},
        {"totals": {"day_wh": None, "month_sessions": "x"}},
        {"record": "x", "last": 5, "reg28": "a", "total_wh": "b", "next_rid": None, "diag": 3},
        {"version": 99, "total_wh": 5000.0},
    ],
)
def test_m5_bad_store_data_never_breaks_setup(bad):
    clock = SimClock()
    fake = FakeStation("A3C_2.7", SERIAL, clock)
    st = Station(FakeClient(fake), SERIAL, restored={"energy": bad}, clock=clock, sleep=clock.sleep)
    m = EnergyMeter()
    StationEnergy(st, m)
    m.sample(Sample(t=1.0, status=4, power_w=1000))
    m.sample(Sample(t=2.0, status=4, power_w=1000))
    m.sensor_values()
    assert m.as_dict()["version"] == E.ENERGY_STORE_VERSION
    if "version" in bad:
        assert m.total_wh < 5000.0  # a newer format is not read as ours
    rec = bad.get("record")
    if isinstance(rec, dict) and rec.get("started") == "x":
        assert m.record.rid == 1 and m.record.started != "x"  # unreadable: dropped, a new record
    elif isinstance(rec, dict) and "started" in rec:
        assert m.record.rid == 1 and m.record.integral_wh >= 0.0 and m.record.approximate


def test_m6_out_of_order_poll_still_closes():
    m, wall = meter()
    put(m, wall, 0, 2000)
    put(m, wall, 20, 2000, poll=False)  # a verification read stamped after the poll start
    put(m, wall, 15, 0, status=1, plugged=False)  # poll that started at t=15
    assert m.record is None and m.last.reason == "unplug"
    assert m.record_open is False


def test_m7_status6_inside_phase_switch():
    m, wall = meter()
    put(m, wall, 0, 2000)
    m.begin_phase_switch()
    put(m, wall, 10, 0, status=6)  # inside not the car ending
    assert m.record is not None
    m.end_phase_switch(restarted=True)
    put(m, wall, 20, 2000)
    assert m.record is not None
    put(m, wall, 30, 0, status=6)  # outside a sequence: the car ended
    assert m.last.reason == "station"
    m, wall = meter()
    put(m, wall, 0, 2000)
    m.begin_phase_switch()
    put(m, wall, 10, 0, status=6)
    m.end_phase_switch(restarted=False)
    put(m, wall, 20, 0, status=6)
    assert m.last.reason == "phase_switch"


def test_m8_late_step_priced_at_charge_end():
    tariff = logic.Tariff(price=1.0, night_price=0.5, night_start_min=23 * 60, night_end_min=7 * 60)
    wall = Wall(datetime(2026, 11, 19, 22, 40, tzinfo=UTC))
    m = EnergyMeter(source="register", tariff=tariff, wall=wall)

    def at(seconds, status, reg28, plugged=True):
        wall.now = datetime(2026, 11, 19, 22, 40, tzinfo=UTC) + timedelta(seconds=seconds)
        m.sample(Sample(t=1000.0 + seconds, status=status, power_w=2000 if status == 4 else 0,
                        currents=(9.0 if status == 4 else 0.0, 0, 0), voltages=(230.0, 0, 0),
                        meter_tenths=reg28, plugged=plugged, delivering=status == 4))

    at(0, 4, 1000)
    at(600, 4, 1003)
    at(900, 2, 1003, plugged=False)  # closed 22:55, day price
    at(1500, 1, 1006, plugged=False)  # late step arrives 23:05 (night)
    assert m.last.cost == pytest.approx(0.6)


def test_persist_stop_reason_stop_time_and_next_rid():
    m, wall = meter()
    put(m, wall, 0, 2000)
    m.note_stop("schedule_end")
    m2 = EnergyMeter(wall=wall)
    m2.load(copy.deepcopy(m.as_dict()))
    put(m2, wall, 10, 0, status=2)
    assert m2.last.reason == "schedule_end"
    # the stop's time survives a restart: 405 s after the stop it is forgotten
    m, wall = meter()
    put(m, wall, 0, 2000)
    m.note_stop("user_stop")
    for t in range(5, 205, 5):
        put(m, wall, t, 2000)
    m3 = EnergyMeter(wall=wall)
    m3.load(copy.deepcopy(m.as_dict()))  # HA restarts 200 s after the stop
    put(m3, wall, 400, 2000)
    put(m3, wall, 405, 0, status=2)
    assert m3.record is not None
    # next_rid survives without any record kept
    m4 = EnergyMeter(wall=wall)
    m4.load({"version": 1, "next_rid": 7})
    put(m4, wall, 500, 2000)
    assert m4.record.rid == 7
    # ... and in a round trip whose records were dropped as unreadable
    for t in (510, 520):
        put(m4, wall, t, 0, status=2, plugged=False)
        put(m4, wall, t + 5, 2000)
    data = copy.deepcopy(m4.as_dict())
    data.update(record=None, last=None, last_charge=None, last_charge_is_last=False)
    m5 = EnergyMeter(wall=wall)
    m5.load(data)
    put(m5, wall, 600, 2000)
    assert m5.record.rid == 10


def test_m2_zero_baseline_and_charge_time_bound():
    """A first value after zeros is a baseline even when small enough to look
    like a step; a long charge widens the bound by its own charging time."""
    m, wall = meter("register")
    put(m, wall, 0, 2000, reg28=0)
    put(m, wall, 30, 2000, reg28=0)
    put(m, wall, 60, 2000, reg28=3)  # A3C-style zeros, then a value
    assert m.record.register_wh == 0.0 and m.diag["reg28_rebases"] == 0
    assert "reg28_rebase" not in m.record.notes
    # 30 min at 11 kW: one lazy block of 5.5 kWh is real energy, more than
    # P_max x REG28_LATE_S + 0.3 kWh would allow without the charging time
    m, wall = meter("register", p_max_w=E.p_max_w("3_11"))
    for t in range(0, 1800, 30):
        put(m, wall, t, 11000, reg28=1000)
    put(m, wall, 1800, 11000, reg28=1055)
    put(m, wall, 1830, 11000, reg28=1055)
    assert m.record.register_wh == pytest.approx(5500.0)
    assert m.diag["reg28_rebases"] == 0


def test_m5_unexpected_load_error_starts_clean(monkeypatch):
    def broken(_data):
        raise RuntimeError("format nobody foresaw")

    m, wall = meter()
    put(m, wall, 0, 2000)
    data = copy.deepcopy(m.as_dict())
    monkeypatch.setattr(E.EnergyRecord, "from_dict", staticmethod(broken))
    m2 = EnergyMeter(wall=wall)
    m2.load(data)  # never raises into Station._restore
    assert m2.record is None and m2.total_wh == 0.0


# --- energy accounting, edge cases ---------------------


def test_e1_register_keeps_energy_charged_while_ha_down():
    """HA down 30 min while the car charges at 7.4 kW: reg 28 holds the 3.7 kWh;
    the downtime counts as charging time for the plausibility bound."""
    m, wall = meter("register", p_max_w=E.p_max_w("1_7"))
    put(m, wall, 0, 7400, reg28=10000)
    for t in range(60, 601, 60):
        put(m, wall, t, 7400, reg28=10000 + (t // 600) * 7)
    saved = copy.deepcopy(m.as_dict())
    before = m.total_wh
    m2 = EnergyMeter(source="register", wall=wall, p_max_w=E.p_max_w("1_7"))
    m2.load(saved)
    for t in (2400, 2405, 2410):
        put(m2, wall, t, 7400, reg28=10007 + 37)
    assert m2.total_wh - before == pytest.approx(3700.0)
    assert m2.diag["reg28_rebases"] == 0
    # idle before the restart: the downtime does not widen the bound
    m, wall = meter("register")
    put(m, wall, 0, 0, status=1, reg28=10000, plugged=False)
    m3 = EnergyMeter(source="register", wall=wall)
    m3.load(copy.deepcopy(m.as_dict()))
    # +1000 kWh after 5 idle days: below 22 kW x 5 days, so only the idle rule rejects it
    put(m3, wall, 5 * 24 * 3600, 0, status=1, reg28=20000, plugged=False)
    put(m3, wall, 5 * 24 * 3600 + 10, 0, status=1, reg28=20000, plugged=False)
    assert m3.total_wh == 0.0 and m3.diag["reg28_rebases"] == 1


def test_e1_link_gap_counts_as_charging_time():
    """A link loss mid-charge (no samples, statuses unknown in between) widens
    the bound by its length; the step reg 28 shows afterwards is energy."""
    m, wall = meter("register", p_max_w=E.p_max_w("1_7"))
    put(m, wall, 0, 7400, reg28=10000)
    put(m, wall, 10, 0, status=2, reg28=10000)  # car pause
    put(m, wall, 1810, 0, status=2, reg28=10037)  # 30 min without a reply: it charged meanwhile
    put(m, wall, 1820, 0, status=2, reg28=10037)
    assert m.record.register_wh == pytest.approx(3700.0)


def test_e2_register_crash_saved_before_first_reg28_read():
    m, wall = meter("register")
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 600, 2000, reg28=1003)
    saved = copy.deepcopy(m.as_dict())
    put(m, wall, 650, 2000, reg28=1006)
    last_state = m.sensor_values()["total_energy"].value  # 0.6
    m2 = EnergyMeter(source="register", wall=wall)
    m2.load(saved)
    m2.restore_total(last_state)
    assert m2.sensor_values()["total_energy"].value == pytest.approx(0.6)
    saved2 = copy.deepcopy(m2.as_dict())  # unload before the station answered
    m3 = EnergyMeter(source="register", wall=wall)
    m3.load(saved2)
    m3.restore_total(m2.sensor_values()["total_energy"].value)
    put(m3, wall, 900, 2000, reg28=1006)
    assert m3.sensor_values()["total_energy"].value == pytest.approx(0.6)
    put(m3, wall, 1500, 2000, reg28=1009)
    assert m3.sensor_values()["total_energy"].value == pytest.approx(0.9)
    # The pending floor is saved too: without HA's last state on the next
    # start, and with a reg-28 read that recovers nothing (0 is not a
    # reading), total_energy still never steps back below 0.6.
    m4 = EnergyMeter(source="register", wall=wall)
    m4.load(copy.deepcopy(saved2))
    put(m4, wall, 900, 2000, reg28=0)
    assert m4.sensor_values()["total_energy"].value == pytest.approx(0.6)
    assert m4.as_dict()["total_wh"] == pytest.approx(600.0)


def test_e3_integral_floor_does_not_hide_later_register_energy():
    m, wall = meter("integral")
    put(m, wall, 0, 3600)
    put(m, wall, 60, 3600)
    saved = copy.deepcopy(m.as_dict())
    put(m, wall, 120, 3600)  # lost with the crash; HA showed 0.12
    last_state = m.sensor_values()["total_energy"].value
    m2 = EnergyMeter(wall=wall)
    m2.load(saved)
    m2.restore_total(last_state)
    assert m2.sensor_values()["total_energy"].value == pytest.approx(0.12)
    put(m2, wall, 200, 0, status=1, plugged=False, reg28=5000)
    m2.set_source("register")
    shown0 = m2.sensor_values()["total_energy"].value
    put(m2, wall, 300, 2000, reg28=5000)
    put(m2, wall, 900, 2000, reg28=5003)
    assert m2.sensor_values()["total_energy"].value - shown0 == pytest.approx(0.3)


def test_e4_phase_switch_flag_not_restored():
    """A sequence does not survive an HA restart: a 6 after it is the car ending."""
    m, wall = meter()
    put(m, wall, 0, 2000)
    put(m, wall, 5, 2000)
    m.begin_phase_switch()
    m2 = EnergyMeter(wall=wall)
    m2.load(copy.deepcopy(m.as_dict()))
    put(m2, wall, 100, 2000)
    put(m2, wall, 3605, 0, status=6)
    assert m2.record is None and m2.last.reason == "station"


def test_e2_integral_booked_while_fold_pending():
    """The floor waits for reg 28 (the last charge was register-sourced), but
    integral energy booked meanwhile (option integral, a failed start in 3)
    shows at once on top of the floor and survives the fold."""
    m, wall = meter("register")
    put(m, wall, 0, 2000, reg28=1000)
    put(m, wall, 600, 2000, reg28=1003)
    put(m, wall, 610, 0, status=2, reg28=1003, plugged=False)
    m.set_source("integral")
    saved = copy.deepcopy(m.as_dict())
    m2 = EnergyMeter(source="integral", wall=wall)
    m2.load(saved)
    m2.restore_total(0.5)  # HA showed more than the Store has
    assert m2.sensor_values()["total_energy"].value == pytest.approx(0.5)
    put(m2, wall, 700, 3600, status=3)  # no reg-28 read yet
    put(m2, wall, 760, 3600, status=3)
    put(m2, wall, 770, 0, status=2)  # start failed: its energy goes to the totals
    booked = 3600 * 60 / 3600 + 3600 / 2 * 10 / 3600
    assert m2.sensor_values()["total_energy"].value == pytest.approx((500 + booked) / 1000, abs=0.001)
    put(m2, wall, 780, 0, status=2, reg28=1003)  # first reg-28 read: the floor is folded
    assert m2.sensor_values()["total_energy"].value == pytest.approx((500 + booked) / 1000, abs=0.001)
