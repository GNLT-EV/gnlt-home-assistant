"""Review 5, round 2: the fixes of the coordinator. Expected = correct
behaviour; a failure = a finding."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sim import Bench, fast, meter, ts

from gnlt_charger.logic import ChargeLedger

THREE = {"L1": 16.0, "L2": 16.0, "L3": 16.0}
T0 = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)


def at(sec):
    return T0 + timedelta(seconds=sec)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    fast(monkeypatch)


def run(coro):
    return asyncio.run(coro)


async def start_tx(st, meter_start=0):
    r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": meter_start, "timestamp": ts()})
    return r[2]["transactionId"]


def kwh_meter(kwh):
    return [{"value": f"{kwh}", "context": "Sample.Periodic", "measurand": "Energy.Active.Import.Register", "unit": "kWh"}]


# N1: upgrade from the old storage format with a charge open; the charge ended
# while Home Assistant was restarting - the queued stop is stamped before the
# restart (= the "started" the migration gave the open charge).


def test_n1_stop_queued_during_the_upgrade_restart():
    ended = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    stored = {
        "energy": {"total_wh": 3000.0, "closed_wh": 2000.0, "open_base": 0.0, "open_wh": 1000.0, "open_last": 1000.0, "last_register": 1000.0},
        "tx_id": 7,
        "tx_counter": 7,
        "last_tx_id": 7,
        "tx_started_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "tx_meter_start": 0.0,
    }

    async def go():
        bench = Bench(stored=stored)
        c, st = await bench.__aenter__()
        try:
            await st.send("StopTransaction", {"meterStop": 1200, "transactionId": 7, "reason": "EVDisconnected", "timestamp": ended})
            await st.status("Finishing")
            await asyncio.sleep(0.1)
            assert c.energy.total_wh == 3200, (c.energy.total_wh, c.energy.last_stop, c.energy.unresolved)
            assert c.tx_id is None
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


# N2: the idle-register rule only knows "value == final". Final below the
# last reading (the owner's rule-2 case) and the idle register shows the last
# reading - is it a new charge from 0?


def test_n2_idle_register_equal_to_last_reading_not_final():
    led = ChargeLedger(max_power_w=11040)
    led.start(0, 1, at(0))
    led.reading(0, True, at(10))
    led.reading(470, True, at(20))
    led.stop(460, 1, at(30))
    led.reading(470, True, at(31))  # status still Charging
    assert led.total_wh == 460, led.total_wh


# N3: the same with a kWh register (other firmware): 2.01 kWh * 1000 is
# 2009.9999999999998, not 2010.


def test_n3_idle_register_in_kwh():
    async def go():
        async with Bench() as (c, st):
            tx = await start_tx(st)
            await st.status("Charging")
            for kwh in (0, 1, 2):
                await st.meter(kwh_meter(kwh))
            await st.send("StopTransaction", {"meterStop": 2010, "transactionId": tx, "reason": "Local", "timestamp": ts()})
            await st.meter(kwh_meter(2.01))
            await st.status("Finishing")
            assert c.energy.total_wh == pytest.approx(2010), c.energy.total_wh

    run(go())


# N4: own charge closed by Finishing before its stop; the stop carries a
# number Home Assistant never gave. 5577f19-review code matched it (only
# waiting charge); now?


def test_n4_own_charge_stop_with_unknown_number_after_finishing():
    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 200, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await st.send("StopTransaction", {"meterStop": 480, "transactionId": 22, "reason": "Local", "timestamp": ts()})
            assert c.energy.total_wh == 480, (c.energy.total_wh, c.energy.last_stop)

    run(go())


# N5: a repeated stop of the last charge while the charger's own next charge
# has begun (no reading yet) must not hide "charging without a session".


def test_n5_repeated_stop_does_not_hide_charging_without_session():
    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 470):
                await st.meter(meter(wh, THREE))
            frame = {"meterStop": 470, "reason": "Local", "timestamp": ts()}
            await st.send("StopTransaction", frame)
            await st.status("Finishing")
            await st.status("Charging")
            await st.send("StopTransaction", frame)
            assert c.energy.last_stop == "duplicate"
            assert c.charging_without_session, "duplicate stop set _stop_pending_status"
            assert c.energy.total_wh == 470

    run(go())


# N6: late stop of an EARLIER HA charge with its number while the next HA
# charge runs - the existing guarantee, now decided by time.


def test_n6_late_stop_of_earlier_number_stamped_before_the_open_start():
    led = ChargeLedger(max_power_w=11040)
    led.start(0, 1, at(0))
    led.reading(470, True, at(60))
    led.start(0, 2, at(600))  # stop of 1 lost so far
    led.reading(300, True, at(660))
    _, rec = led.stop(460, 1, at(300))
    assert rec.number == 1
    assert led.open_record.number == 2
    assert led.total_wh == 760


# N7: the same, but the HA charge 2 is the charger's own (no number) and its
# record began at its FIRST READING seen (HA was away / readings every 60 s):
# the stop of A is stamped after B's actual start but before B's first reading.


def test_n7_number_stop_vs_own_record_started_at_first_reading():
    led = ChargeLedger(max_power_w=11040)
    led.start(0, 1, at(0))
    led.reading(470, True, at(60))
    led.close_open(at(100))  # Available without a stop
    led.reading(0, True, at(400))  # B, first reading seen
    led.reading(300, True, at(460))
    _, rec = led.stop(460, 1, at(120))
    assert rec.number == 1
    assert led.open_record is not None
    assert led.total_wh == 760
