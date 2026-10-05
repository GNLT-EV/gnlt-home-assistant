"""Energy and power over the whole OCPP conversation with a fake charger:
the cases found with the GNLT platform (review
04.10.2026). Each failed on 5577f19."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sim import Bench, FakeStation, fast, meter, ts

from gnlt_charger import charger as charger_mod
from gnlt_charger.charger import ChargerSettings
from gnlt_charger.logic import Tariff

THREE = {"L1": 16.0, "L2": 16.0, "L3": 16.0}
BOOT = {"chargePointModel": "Home OCPP", "chargePointVendor": "AEFA", "firmwareVersion": "SW:A3B_2.7-HW:B07_0.4"}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    fast(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def stop_frame(meter_stop, at, tx=None, end=False):
    """As the live charger sends it: Transaction.Begin (0) only; ``end`` adds a
    Transaction.End reading (other firmware)."""
    samples = [{"value": "0", "context": "Transaction.Begin", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]
    if end:
        samples.append({"value": str(meter_stop), "context": "Transaction.End", "measurand": "Energy.Active.Import.Register", "unit": "Wh"})
    p = {"meterStop": meter_stop, "reason": "Local", "timestamp": at, "transactionData": [{"timestamp": at, "sampledValue": samples}]}
    if tx is not None:
        p["transactionId"] = tx
    return p


async def meter_at(st, wh, at):
    await st.send("MeterValues", {"connectorId": 1, "meterValue": [{"timestamp": at, "sampledValue": meter(wh, THREE)}]})


async def ha_charge(st, at_start, readings, stop_wh, at_stop):
    r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": at_start})
    tx = r[2]["transactionId"]
    await st.status("Charging")
    for wh, at in readings:
        await meter_at(st, wh, at)
    await st.send("StopTransaction", stop_frame(stop_wh, at_stop, tx))
    await st.status("Finishing")
    await st.status("Available")


async def reboot(bench):
    await bench.station.close()
    await asyncio.sleep(0.2)
    st = FakeStation(bench.port)
    bench.station = st
    await st.connect()
    await st.send("BootNotification", BOOT)
    await st.status("Charging")
    return st


@pytest.mark.parametrize("end", [False, True], ids=["stop_like_live", "stop_with_end_reading"])
@pytest.mark.parametrize("own", [False, True], ids=["ha_start", "station_own"])
@pytest.mark.parametrize("after", [True, False], ids=["readings_after_reboot", "no_readings_after_reboot"])
def test_reset_in_a_charge_is_one_charge(after, own, end):
    """Platform case: 65 530, reboot, register from 0 to 4 234, ONE stop
    with meterStop 4 234. Truth 69 764 - in the totals and in the last charge
    (owner 04.10.2026). 5577f19: 65 530 without readings after the reboot, and
    the last charge 4,234 always."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            tx = None
            if own:
                await st.status("Preparing")
            else:
                r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": ts()})
                tx = r[2]["transactionId"]
            await st.status("Charging")
            for wh in (0, 20000, 40000, 65530):
                await st.meter(meter(wh, THREE))
            st = await reboot(bench)
            if after:
                for wh in (0, 2000, 4234):
                    await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", stop_frame(4234, ts(), tx, end))
            await st.status("Finishing")
            await asyncio.sleep(0.2)
            assert c.energy.total_wh == 69764
            assert c.totals.day_wh == 69764
            assert c.last_session["energy_kwh"] == 69.764
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_next_charge_after_a_reset_is_counted():
    """Check A (platform review): 5577f19 swallowed the next 10 kWh charge."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": ts()})
            tx = r[2]["transactionId"]
            await st.status("Charging")
            for wh in (0, 30000, 65530):
                await st.meter(meter(wh, THREE))
            st = await reboot(bench)
            await st.send("StopTransaction", stop_frame(4234, ts(), tx))
            await st.status("Finishing")
            await st.status("Available")
            await ha_charge(st, ts(), [(0, ts()), (5000, ts()), (10000, ts())], 10000, ts())
            await asyncio.sleep(0.2)
            assert c.energy.total_wh == 79764
            assert c.totals.day_wh == 79764
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


@pytest.mark.parametrize("edge", ["midnight", "night_tariff"])
def test_correction_belongs_to_the_day_and_price_of_its_reading(edge, monkeypatch):
    """Check B: readings to 470, final 460 just after midnight / the start of
    the night price, then 1 000 Wh. 5577f19: the 10 Wh moved into the next
    day / price (990 Wh on 05.10)."""
    if edge == "midnight":
        last_at, stop_at, nxt = "2026-10-04T23:59:50+02:00", "2026-10-05T00:00:10+02:00", "2026-10-05T00:10:00+02:00"
    else:
        last_at, stop_at, nxt = "2026-10-04T22:59:50+02:00", "2026-10-04T23:00:10+02:00", "2026-10-04T23:10:00+02:00"
    start_at = last_at.replace(":59:50", ":30:00")
    clock = {"t": datetime.fromisoformat(last_at)}
    monkeypatch.setattr(charger_mod, "_now", lambda: clock["t"])

    async def go():
        settings = ChargerSettings(max_current_a=16, phases=3, tariff=Tariff(price=1.0, night_price=0.5))
        bench = Bench(settings=settings)
        c, st = await bench.__aenter__()
        try:
            clock["t"] = datetime.fromisoformat(stop_at)
            await ha_charge(st, start_at, [(0, start_at), (470, last_at)], 460, stop_at)
            assert c.energy.total_wh == 460
            assert c.last_session["energy_kwh"] == 0.46
            clock["t"] = datetime.fromisoformat(nxt)
            await ha_charge(st, nxt, [(0, nxt), (1000, nxt)], 1000, nxt)
            assert c.energy.total_wh == 1460
            if edge == "midnight":
                assert c.totals.day == "2026-10-05"
                assert c.totals.day_wh == 1000
                assert c.totals.month_wh == 1460
            else:
                assert c.totals.day_wh == 1460
                # 460 at the day price, 1 000 at the night price
                assert c.totals.day_cost == pytest.approx(0.46 + 0.5)
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_own_charge_with_first_reading_above_the_last_final():
    """Check C: HA charge ends at 400; the charger starts the next by itself
    and HA first sees 500. This charger counts every charge from 0 (its
    meterStart 0): the charge is 700, the total 1 100. 5577f19: 700 / 0,3."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await ha_charge(st, ts(), [(0, ts()), (200, ts()), (400, ts())], 400, ts())
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (500, 700):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", stop_frame(700, ts()))
            await st.status("Finishing")
            await asyncio.sleep(0.2)
            assert c.energy.total_wh == 1100
            assert c.last_session["energy_kwh"] == 0.7
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_stop_of_own_charge_with_a_clock_behind_is_not_thrown_away():
    """First run of 04.10.2026: a stop of the charger's own charge stamped
    before the charge began was dropped as a late one - 4 234 Wh lost. Now the
    ledger matches it by the readings (the only charge)."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 20000, 65530):
                await st.meter(meter(wh, THREE))
            st = await reboot(bench)
            await st.send("StopTransaction", stop_frame(4234, "2026-10-04T03:00:00+02:00"))
            await st.status("Finishing")
            await asyncio.sleep(0.2)
            assert c.energy.total_wh == 69764
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_late_stop_of_own_charge_a_while_b_runs():
    """Platform review: own charge A (470) ends by status, own charge B runs (300),
    then A's stop without a number (460), timed before B began. A is settled
    at 460; B goes on, untouched."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 470):
                await st.meter(meter(wh, THREE))
            ended_a = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
            await st.status("Finishing")
            await st.status("Charging")
            for wh in (0, 100, 200, 300):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", stop_frame(460, ended_a))
            assert c.energy.total_wh == 760
            assert c.status == "Charging"
            assert c.energy.open_record.energy_wh == 300
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_three_phase_firmware_reporting_one_phase_shows_u_x_i():
    """Platform data: SW:A3B_3.1-HW:B07_0.5 on three phases sends
    the power of one phase. Shown: sum of U x I; the charger's figure kept."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.send("BootNotification", {**BOOT, "firmwareVersion": "SW:A3B_3.1-HW:B07_0.5"})
            await st.status("Charging")
            await st.meter(meter(500, {"L1": 9.1, "L2": 9.1, "L3": 9.1}, power_w=2100))
            assert round(c.power_w) == 6279
            assert c.power_reported_w == 2100
            assert c.power_mismatch is None
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_a3b_2_7_on_three_phases_shows_u_x_i():
    """Live bench 06.10.2026, SW:A3B_2.7-HW:B07_0.4 on three phases: the charger
    sends 3500 W (its own screen 10.5 kW), the register grows at the sum of U x I.
    Shown: the sum of U x I; the charger's figure kept."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.status("Charging")
            await st.meter(meter(500, {"L1": 14.6, "L2": 14.5, "L3": 14.5}, voltage=236.0, power_w=3500))
            assert round(c.power_w) == 10290
            assert c.power_reported_w == 3500
            assert c.power_mismatch is None
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_charger_figure_cleared_when_the_charge_ends():
    """Cloud review 06.10.2026: after the charge the power is 0, so the
    charger's own figure must not stay in the power sensor's attributes."""

    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.status("Charging")
            await st.meter(meter(500, {"L1": 14.6, "L2": 14.5, "L3": 14.5}, voltage=236.0, power_w=3500))
            assert c.power_reported_w == 3500
            await st.status("Finishing")
            assert c.power_w == 0.0
            assert c.power_reported_w is None
        finally:
            await bench.__aexit__(None, None, None)

    run(go())


def test_other_firmware_keeps_the_chargers_power_and_records_the_difference(caplog):
    async def go():
        bench = Bench()
        c, st = await bench.__aenter__()
        try:
            await st.send("BootNotification", {**BOOT, "firmwareVersion": "SW:A1B_2.7-HW:B07_0.4"})
            await st.status("Charging")
            for wh in (500, 600):
                await st.meter(meter(wh, {"L1": 9.1, "L2": 9.1, "L3": 9.1}, power_w=2100))
            assert c.power_w == 2100
            assert c.power_reported_w is None
            assert c.power_mismatch["active_power_w"] == 2100
            assert c.power_mismatch["apparent_power_va"] == 6279
        finally:
            await bench.__aexit__(None, None, None)

    run(go())
    assert any("cause not established" in r.getMessage() for r in caplog.records)


def test_old_stop_after_the_upgrade_does_not_touch_the_open_charge():
    """Platform review: upgrade from the
    old storage with charge 7 open since an hour ago; a retry of charge 6's stop
    (meterStop 400, stamped two hours ago) must change nothing; charge 7's own
    stop then settles it. 4c36c21: total 3400, charge 7 closed by the old stop."""
    now = datetime.now(UTC)
    stored = {
        "energy": {"total_wh": 3000.0, "closed_wh": 2000.0, "open_base": 0.0, "open_wh": 1000.0,
                   "open_last": 1000.0, "last_register": 1000.0},
        "tx_id": 7, "tx_counter": 7, "last_tx_id": 7,
        "tx_started_at": (now - timedelta(hours=1)).isoformat(), "tx_meter_start": 0.0,
    }

    async def go():
        bench = Bench(stored=stored)
        c, st = await bench.__aenter__()
        try:
            await st.status("Charging")
            await st.send("StopTransaction", stop_frame(400, (now - timedelta(hours=2)).isoformat(), 6))
            assert c.energy.total_wh == 3000
            assert c.energy.is_open
            assert c.energy.last_stop == "unresolved"
            assert c.tx_id == 7
            await st.send("StopTransaction", stop_frame(1200, ts(), 7))
            await st.status("Finishing")
            await asyncio.sleep(0.1)
            assert c.energy.total_wh == 3200
            assert not c.energy.is_open
            assert c.tx_id is None
        finally:
            await bench.__aexit__(None, None, None)

    run(go())
