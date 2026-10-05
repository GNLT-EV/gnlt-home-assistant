"""Review 5 (independent agent, 04.10.2026): regressions and gaps found in the
ledger rework against 5577f19. Each failed before the fix."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sim import Bench, FakeStation, fast, meter, ts

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


# --- R1: stop under the number of an EARLIER charge (partner's bench 25.09) ----


def test_r1a_charge_run_under_old_number_whose_stop_was_lost():
    """Start 1 (quick, its stop lost), start 2; the charger runs 2 as 1 and
    sends StopTransaction(1, 1000). One charge of 1000 Wh happened."""

    async def go():
        async with Bench() as (c, st):
            tx1 = await start_tx(st)
            await st.status("Charging")
            await st.meter(meter(0, THREE))
            tx2 = await start_tx(st)  # stop of tx1 never came
            assert tx2 != tx1
            for wh in (0, 500, 1000):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 1000, "transactionId": tx1, "reason": "Local", "timestamp": ts()})
            await st.status("Finishing")
            await asyncio.sleep(0.1)
            assert c.energy.total_wh == 1000, c.energy.total_wh
            assert c.totals.day_wh == 1000, c.totals.day_wh
            assert c.tx_id is None, "session of tx2 must be closed by the status after the stop"

    run(go())


def test_r1b_charge_run_under_old_number_whose_stop_did_come():
    """Start 1 + its stop (0 Wh), start 2; the charger runs 2 as 1. The old
    code closed 2 on the next status (_foreign_stop)."""

    async def go():
        async with Bench() as (c, st):
            tx1 = await start_tx(st)
            await st.send("StopTransaction", {"meterStop": 0, "transactionId": tx1, "reason": "Local", "timestamp": ts()})
            await st.status("Finishing")
            await start_tx(st)
            await st.status("Charging")
            for wh in (0, 500, 990):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 1000, "transactionId": tx1, "reason": "Local", "timestamp": ts()})
            await st.status("Finishing")
            await asyncio.sleep(0.1)
            assert c.tx_id is None, "tx2 left open in Finishing (switch stuck 'on')"
            assert c.energy.total_wh == 1000, c.energy.total_wh

    run(go())


# --- R2: a reading after StopTransaction, before the status changes -----------


def test_r2_reading_between_stop_and_finishing_is_not_a_new_charge():
    """Charger known to count from 0 (meterStart 0). StopTransaction comes ~1 s
    before Finishing; a MeterValues in that second (or Refresh readings / the
    stale-meter poll while HA still shows Charging after an emergency stop)."""

    async def go():
        async with Bench() as (c, st):
            tx = await start_tx(st)
            await st.status("Charging")
            for wh in (0, 1000, 2000):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 2010, "transactionId": tx, "reason": "Local", "timestamp": ts()})
            await st.meter(meter(2010, THREE))  # idle register = final figure, status still Charging
            await st.status("Finishing")
            await asyncio.sleep(0.1)
            assert c.energy.total_wh == 2010, c.energy.total_wh
            assert c.totals.day_wh == 2010, c.totals.day_wh

    run(go())


def test_r2_ledger_level():
    led = ChargeLedger(max_power_w=11040)
    led.start(0, 1, at(0))
    for i, v in enumerate((0, 1000, 2000)):
        led.reading(v, True, at(10 + 10 * i))
    led.stop(2010, 1, at(40))
    led.reading(2010, True, at(41))  # status still "Charging"
    assert led.total_wh == 2010, led.total_wh


# --- R3: ledger matched the running charge, charger calls the stop "late" ----


def test_r3_own_charge_stop_with_clock_behind_updates_session_and_last_charge():
    """test_stop_of_own_charge_with_a_clock_behind_is_not_thrown_away checks only
    the total. The ledger closes the charge at 69 764, but the charger takes the
    stop for a late one: "Energy of this charge" and "Last charge" are not
    updated."""
    from test_energy_sim import reboot

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 20000, 65530):
                await st.meter(meter(wh, THREE))
            st = await reboot(bench)
            await st.send("StopTransaction", {"meterStop": 4234, "reason": "Local", "timestamp": "2026-10-04T03:00:00+02:00"})
            await st.status("Finishing")
            await asyncio.sleep(0.2)
            assert c.energy.total_wh == 69764
            assert c.session_wh == 69764, c.session_wh
            assert c.last_session is not None and c.last_session["energy_kwh"] == 69.764, c.last_session
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_r3b_ha_charge_stop_without_number_with_clock_behind():
    """HA charge; the charger's StopTransaction comes without a number and
    with its clock behind the start. The ledger closes the HA charge with its
    final figure; the charger treats the stop as late and keeps the session."""

    async def go():
        async with Bench() as (c, st):
            await start_tx(st)
            await st.status("Charging")
            for wh in (0, 500, 1000):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 1010, "reason": "Local", "timestamp": "2026-10-04T03:00:00+02:00"})
            await st.status("Finishing")
            await asyncio.sleep(0.1)
            assert c.energy.total_wh == 1010
            assert not c.energy.is_open
            assert c.tx_id is None, "ledger closed the charge, HA session left open"

    run(go())


# --- R4: repeated stop whose time was replaced (clock ahead) -----------------


def test_r4_repeated_stop_with_clock_ahead_is_not_applied_to_the_next_charge():
    """Own charge A, stop without number stamped 1 h ahead (station_timestamp
    replaces it with now), own charge B runs, the SAME stop frame again."""

    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 470):
                await st.meter(meter(wh, THREE))
            ahead = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            frame = {"meterStop": 460, "reason": "Local", "timestamp": ahead}
            await st.send("StopTransaction", frame)
            await st.status("Finishing")
            await st.status("Charging")
            for wh in (0, 100, 300):
                await st.meter(meter(wh, THREE))
            await asyncio.sleep(0.05)
            await st.send("StopTransaction", frame)  # retry after a reconnect
            for wh in (400, 500):
                await st.meter(meter(wh, THREE))
            assert c.energy.total_wh == 960, c.energy.total_wh

    run(go())


# --- R5: number of a pruned charge -------------------------------------------


def test_r5_retry_of_a_stop_of_a_pruned_charge_does_not_close_the_running_one():
    led = ChargeLedger(max_power_w=11040)
    t = 0
    for n in range(1, 8):
        led.start(0, n, at(t)); t += 10
        led.reading(460, True, at(t)); t += 10
        led.stop(460, n, at(t)); t += 10
    led.start(0, 8, at(t)); t += 10
    led.reading(300, True, at(t)); t += 10
    _, rec = led.stop(460, 1, at(25))  # repeated stop of charge 1, long pruned
    assert rec is None or rec.number == 1
    assert led.open_record is not None and led.open_record.number == 8
    assert led.total_wh == 7 * 460 + 300, led.total_wh


# --- R6: unresolved stop without number ends the running HA session ----------


def test_r6_unresolved_stop_does_not_end_the_running_session():
    """Own A (470) closed by Finishing without its stop; HA charge B runs (300);
    A's stop comes without a number, stamped after B began (clocks differ):
    the ledger cannot tell -> unresolved, "nothing changes". The charger ends
    session B anyway."""

    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await start_tx(st)
            await st.status("Charging")
            for wh in (0, 100, 300):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 460, "reason": "Local", "timestamp": ts()})
            assert c.energy.unresolved, "expected an unresolved stop"
            assert c.tx_id is not None, "running HA session was ended by an unresolved stop"

    run(go())
