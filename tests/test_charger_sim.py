"""The whole OCPP conversation with a fake charger: three-phase readings,
faults, charges the charger starts by itself, Home Assistant restarts."""

import asyncio
import logging

import pytest
from sim import Bench, meter, wait_for

from gnlt_charger.charger import ChargerError

from sim import fast  # noqa: E402

THREE = {"L1": 16.0, "L2": 16.0, "L3": 16.0}
ONE = {"L1": 16.0}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    fast(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def test_three_phase_power_total():
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(500, THREE))
            assert round(c.power_w) == 11040
            assert c.current == THREE
            assert c.phases_in_use == 3

    run(go())


def test_three_phase_power_per_phase_firmware():
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(500, THREE, power_w="phases"))
            assert round(c.power_w) == 11040

    run(go())


def test_phase_drop_is_logged(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(500, THREE))
            await st.meter(meter(510, THREE))
            await st.meter(meter(600, ONE))
            assert c.phases_in_use == 3  # one reading is not a change yet
            await st.meter(meter(610, ONE))
            assert c.phases_in_use == 1

    with caplog.at_level(logging.WARNING):
        run(go())
    assert any("on 1 phase(s) instead of 3" in r.getMessage() for r in caplog.records)


def test_power_of_one_phase_in_three_phase_charge_is_recorded(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.send("BootNotification", {"chargePointModel": "Home OCPP", "chargePointVendor": "AEFA", "firmwareVersion": "SW:A1B_2.7-HW:B07_0.4"})
            await st.status("Charging")
            await st.meter(meter(500, THREE, power_w=3680))
            assert c.power_mismatch is None
            await st.meter(meter(600, THREE, power_w=3680))
            assert c.power_mismatch["active_power_w"] == 3680
            assert c.power_mismatch["apparent_power_va"] == 11040

    with caplog.at_level(logging.WARNING):
        run(go())
    assert any("cause not established" in r.getMessage() for r in caplog.records)


def test_live_one_phase_frame_has_no_mismatch():
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            for wh in (190, 210, 220):
                await st.meter(meter(wh, {"L1": 9.1}, voltage=236, power_w=2100))
            assert c.power_mismatch is None
            assert c.phases_in_use == 1

    run(go())


def test_emergency_stop_without_status_asks_for_status():
    """Live 04.10.2026 01:00: over current -> StopTransaction EmergencyStop and
    no StatusNotification. Home Assistant must ask for the status."""

    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(440, {"L1": 9.0}, voltage=237, power_w=2100))
            await st.send(
                "StopTransaction",
                {"idTag": "", "meterStop": 460, "reason": "EmergencyStop", "timestamp": "2026-10-04T01:00:38.664+02:00",
                 "transactionData": [{"timestamp": "2026-10-04T01:00:38.664+02:00", "sampledValue": [
                     {"context": "Transaction.Begin", "measurand": "Energy.Active.Import.Register", "unit": "Wh", "value": "0"}]}]},
            )
            assert await wait_for(lambda: any(
                f[2] == "TriggerMessage" and f[3].get("requestedMessage") == "StatusNotification" for f in st.received
            ))
            await st.status("Faulted", "OverCurrentFailure", "E0020")
            assert c.status == "Faulted"
            with pytest.raises(ChargerError) as err:
                await c.start()
            assert err.value.code == "faulted"
            assert err.value.detail == "E0020"

    run(go())


def test_normal_stop_does_not_ask_for_status():
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.send("StopTransaction", {"meterStop": 100, "reason": "Remote", "timestamp": "2026-10-04T01:00:00+02:00"})
            await st.status("Finishing")
            await asyncio.sleep(0.5)
            assert not any(f[2] == "TriggerMessage" and f[3].get("requestedMessage") == "StatusNotification" for f in st.received)

    run(go())


def test_charge_started_by_the_charger_counts_previous_energy_once():
    """A short charge, then the charger starts again by itself (no
    StartTransaction): the register restarts at 0. Total = both charges."""

    async def go():
        async with Bench() as (c, st):
            r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": "2026-10-04T01:00:00+02:00"})
            tx = r[2]["transactionId"]
            await st.status("Charging")
            for wh in (0, 150, 300):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 300, "transactionId": tx, "reason": "Remote", "timestamp": "2026-10-04T01:02:00+02:00"})
            await st.status("Finishing")
            await st.status("Available")
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 92, 183, 275, 367, 458, 550):
                await st.meter(meter(wh, THREE))
            assert c.energy.total_wh == 850, c.energy.total_wh
            assert c.totals.month_sessions == 2
            assert c.totals.day_wh == 850

    run(go())


def test_home_assistant_restart_in_a_charge_is_not_a_new_charge():
    async def go():
        bench = Bench()
        async with bench as (c, st):
            await st.status("Charging")
            for wh in (0, 1000, 2000):
                await st.meter(meter(wh, THREE))
            sessions = c.totals.month_sessions
        stored = dict(bench.saved)
        bench2 = Bench(stored=stored)
        async with bench2 as (c2, st2):
            await st2.status("Charging")
            await st2.meter(meter(2100, THREE))
            await st2.meter(meter(2200, THREE))
            assert c2.energy.total_wh == 2200, c2.energy.total_wh
            assert c2.totals.month_sessions == sessions

    run(go())


def test_frames_are_kept_for_diagnostics():
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            dirs = {d for _at, d, _raw in c.frames}
            assert dirs == {"<-", "->"}
            assert any("BootNotification" in raw for _at, _d, raw in c.frames)

    run(go())


def test_frames_are_in_the_debug_log(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")

    with caplog.at_level(logging.DEBUG, logger="gnlt_charger.charger"):
        run(go())
    lines = [r.getMessage() for r in caplog.records if r.name == "gnlt_charger.charger"]
    assert any("<- [2," in m and "StatusNotification" in m for m in lines)
    assert any("-> [3," in m for m in lines)


def test_charge_started_by_the_charger_through_suspended_ev():
    """Preparing -> SuspendedEV (car waking up) -> Charging, no transaction
    (independent review)."""

    async def go():
        async with Bench() as (c, st):
            r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": "2026-10-04T01:00:00+02:00"})
            tx = r[2]["transactionId"]
            await st.status("Charging")
            for wh in (0, 150, 300):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 300, "transactionId": tx, "reason": "Remote", "timestamp": "2026-10-04T01:02:00+02:00"})
            for s in ("Finishing", "Available", "Preparing", "SuspendedEV", "Charging"):
                await st.status(s)
            for wh in (0, 92, 183, 275, 367, 458, 550):
                await st.meter(meter(wh, THREE))
            assert c.energy.total_wh == 850, c.energy.total_wh
            assert c.totals.month_sessions == 2

    run(go())


def test_late_stop_of_a_charge_started_by_the_charger():
    """Charge A without a transaction peaks at 470; the charger starts B by
    itself; A's StopTransaction (meterStop 440) arrives after that."""

    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 200, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await st.status("Charging")
            await st.send("StopTransaction", {"meterStop": 440, "reason": "Local", "timestamp": "2026-10-04T01:02:00+02:00"})
            for wh in (0, 100):
                await st.meter(meter(wh, THREE))
            # A is recorded at the charger's final figure, 440 (owner
            # 04.10.2026; was its last reading, 470), + B 100.
            assert c.energy.total_wh == 540, c.energy.total_wh

    run(go())


def test_one_phase_after_a_pause_is_logged(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(500, THREE))
            await st.meter(meter(510, THREE))
            await st.status("SuspendedEV")
            await st.status("Charging")
            await st.meter(meter(600, ONE))
            await st.meter(meter(610, ONE))
            assert c.phases_in_use == 1

    with caplog.at_level(logging.WARNING):
        run(go())
    assert sum("phase(s) instead of" in r.getMessage() for r in caplog.records) == 1


def test_phase_hovering_around_one_amp_does_not_flood_the_log(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            for i in range(8):
                await st.meter(meter(500 + i, {"L1": 6.0, "L2": 6.0, "L3": 0.9 if i % 2 else 1.1}))

    with caplog.at_level(logging.WARNING):
        run(go())
    assert sum("phase(s) instead of" in r.getMessage() for r in caplog.records) <= 1


def test_faulted_without_code_has_readable_texts():
    from gnlt_charger.notifications import error_text

    async def go():
        async with Bench() as (c, st):
            await st.status("Faulted")
            assert error_text("pl", c) == ("błąd", "")
            with pytest.raises(ChargerError) as err:
                await c.start()
            assert err.value.detail == "-"

    run(go())


def test_diagnostics_file_is_json(monkeypatch):
    import json

    from homeassistant.helpers.json import ExtendedJSONEncoder

    import gnlt_charger.diagnostics as diag

    class Entry:
        pass

    async def integration(_hass, _domain):
        class I:
            version = "0.2.9"

        return I()

    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(500, THREE))
            entry = Entry()
            entry.runtime_data = c
            entry.data = {"identity": "102512111615", "port": 9000, "preset": "3_11", "password": "x", "ssid": "y"}
            entry.options = {}
            monkeypatch.setattr(diag, "async_get_integration", integration)
            out = await diag.async_get_config_entry_diagnostics(None, entry)
            text = json.dumps(out, cls=ExtendedJSONEncoder)
            assert '"password": "**REDACTED**"' in text and '"x"' not in text
            assert out["frames"] and out["charger"]["phases_in_use"] == 3

    run(go())


def test_late_stop_of_an_earlier_transaction_after_the_next_start():
    """Partner's bench 25.09: StopTransaction of the OLD number after the next
    StartTransaction (second review). It ended before the new one began."""

    async def go():
        async with Bench() as (c, st):
            r = await st.send("StartTransaction", {"connectorId": 1, "idTag": "x", "meterStart": 0, "timestamp": "2026-10-04T01:00:00+02:00"})
            tx1 = r[2]["transactionId"]
            await st.status("Charging")
            for wh in (0, 200, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await st.send("StartTransaction", {"connectorId": 1, "idTag": "x", "meterStart": 0, "timestamp": "2026-10-04T01:10:00+02:00"})
            await st.status("Charging")
            await st.send("StopTransaction", {"meterStop": 460, "transactionId": tx1, "reason": "Local", "timestamp": "2026-10-04T01:05:00+02:00"})
            for wh in (0, 100):
                await st.meter(meter(wh, THREE))
            # A at its final figure 460 (was its last reading 470) + B 100.
            assert (c.energy.total_wh, c.totals.day_wh) == (560, 560)
            assert c.tx_id is not None

    run(go())


def test_suspended_evse_without_a_charge_is_not_a_charge():
    async def go():
        async with Bench() as (c, st):
            for s in ("Available", "Preparing", "SuspendedEVSE"):
                await st.status(s)
            assert c.totals.month_sessions == 0

    run(go())


def test_status_of_the_whole_charger_keeps_the_connector_error():
    async def go():
        async with Bench() as (c, st):
            await st.status("Faulted", "OverCurrentFailure", "E0020")
            await st.send("StatusNotification", {"connectorId": 0, "errorCode": "NoError", "status": "Available"})
            assert c.status == "Faulted"
            assert (c.error_code, c.vendor_error) == ("OverCurrentFailure", "E0020")

    run(go())


def test_one_voltage_for_three_phases_is_not_a_power_mismatch():
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            samples = [
                {"value": "11040", "measurand": "Power.Active.Import", "unit": "W"},
                {"value": "230", "measurand": "Voltage", "unit": "V"},
                *({"value": "16", "measurand": "Current.Import", "unit": "A", "phase": p} for p in ("L1", "L2", "L3")),
            ]
            await st.meter(samples)
            await st.meter(samples)
            assert c.power_mismatch is None
            assert c.phases_in_use == 3

    run(go())


def test_secret_configuration_is_redacted_in_diagnostics():
    from gnlt_charger.diagnostics import _redact_frame

    raw = '[3,"a",{"configurationKey":[{"key":"AuthorizationKey","readonly":false,"value":"s3cret"},{"key":"ChargeRate","readonly":false,"value":"10"}]}]'
    out = _redact_frame(raw)
    assert "s3cret" not in out and '"value":"10"' in out


def test_late_stop_without_a_number_after_a_charge_from_home_assistant():
    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 200, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": "2026-10-04T01:10:00+02:00"})
            await st.status("Charging")
            await st.meter(meter(100, THREE))
            await st.send("StopTransaction", {"meterStop": 470, "reason": "Local", "timestamp": "2026-10-04T01:05:00+02:00"})
            assert c.energy.total_wh == 570, c.energy.total_wh
            assert c.tx_id is not None

    run(go())


def test_car_flapping_between_preparing_and_suspended_ev_is_one_charge_at_most():
    async def go():
        async with Bench() as (c, st):
            for _ in range(3):
                await st.status("Preparing")
                await st.status("SuspendedEV")
            assert c.totals.month_sessions == 0
            await st.status("Charging")
            await st.status("SuspendedEV")
            await st.status("Charging")
            assert c.totals.month_sessions == 1

    run(go())


def test_phases_closing_one_after_another_at_the_start_are_not_a_change(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await st.meter(meter(0, ONE))
            await st.meter(meter(10, THREE))
            await st.meter(meter(100, THREE))
            assert c.phases_in_use == 3

    with caplog.at_level(logging.WARNING):
        run(go())
    assert not any("phase(s) instead of" in r.getMessage() for r in caplog.records)


def test_fault_of_the_whole_charger_is_shown():
    async def go():
        async with Bench() as (c, st):
            await st.status("Available")
            await st.send("StatusNotification", {"connectorId": 0, "errorCode": "GroundFailure", "status": "Faulted"})
            assert c.error_code == "GroundFailure"

    run(go())


def test_rfid_card_numbers_are_redacted_in_diagnostics():
    from gnlt_charger.diagnostics import _redact_frame

    out = _redact_frame('[2,"a","StartTransaction",{"connectorId":1,"idTag":"04A1B2C3","meterStart":0}]')
    assert "04A1B2C3" not in out
    own = _redact_frame('[2,"a","StartTransaction",{"idTag":"HomeAssistant"}]')
    assert "HomeAssistant" in own


def test_late_stop_of_the_chargers_own_charge_after_the_next_ones_readings():
    """Fourth review: own charge A (470 Wh), own charge B already read 0..300,
    then A's StopTransaction (no number, ended before B began)."""

    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 200, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await asyncio.sleep(0.05)
            ended_a = ts()
            await asyncio.sleep(6)
            await st.status("Charging")
            for wh in (0, 100, 200, 300):
                await st.meter(meter(wh, THREE))
            await st.send("StopTransaction", {"meterStop": 460, "reason": "Local", "timestamp": ended_a})
            # A at its final figure 460 (was its last reading 470) + B 300.
            assert c.energy.total_wh == 760, c.energy.total_wh

    from sim import ts

    run(go())


def test_three_to_one_phase_with_residual_current_on_unused_phases(caplog):
    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            for i in range(3):
                await st.meter(meter(100 + i, THREE))
            for i in range(3):
                await st.meter(meter(200 + i, {"L1": 16.0, "L2": 0.6, "L3": 0.6}))
            assert c.phases_in_use == 1

    with caplog.at_level(logging.WARNING):
        run(go())
    assert sum("phase(s) instead of" in r.getMessage() for r in caplog.records) == 1


def test_power_cut_in_a_charge_from_home_assistant_register_restarts():
    """Fourth review: BootNotification with the charge open, the charger goes
    on charging and its register starts again from 0."""

    async def go():
        async with Bench() as (c, st):
            await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": "2026-10-04T01:00:00+02:00"})
            await st.status("Charging")
            for wh in (0, 150, 300):
                await st.meter(meter(wh, THREE))
            await st.send("BootNotification", {"chargePointModel": "Home OCPP", "chargePointVendor": "AEFA", "firmwareVersion": "SW:A3B_2.7-HW:B07_0.4"})
            await st.status("Charging")
            for wh in (0, 92, 183, 275, 367, 458, 550):
                await st.meter(meter(wh, THREE))
            assert c.energy.total_wh == 850, c.energy.total_wh

    run(go())


def test_reconnect_in_a_charge_register_goes_on():
    async def go():
        async with Bench() as (c, st):
            await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": "2026-10-04T01:00:00+02:00"})
            await st.status("Charging")
            for wh in (0, 150, 300):
                await st.meter(meter(wh, THREE))
            await st.send("BootNotification", {"chargePointModel": "Home OCPP", "chargePointVendor": "AEFA", "firmwareVersion": "SW:A3B_2.7-HW:B07_0.4"})
            await st.status("Charging")
            for wh in (310, 400, 500):
                await st.meter(meter(wh, THREE))
            assert c.energy.total_wh == 500, c.energy.total_wh

    run(go())


def test_energy_of_this_charge_follows_the_chargers_register():
    """Live: the register never went down inside a charge (781 readings);
    the final figure of the charge is the charger's meterStop."""

    async def go():
        async with Bench() as (c, st):
            await st.send("StartTransaction", {"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": "2026-10-04T01:00:00+02:00"})
            await st.status("Charging")
            seen = []
            for wh in (0, 1000, 2000, 2050, 2100):
                await st.meter(meter(wh, THREE))
                seen.append(c.session_wh)
            assert seen == [0, 1000, 2000, 2050, 2100], seen
            await st.send("StopTransaction", {"meterStop": 2110, "transactionId": 1, "reason": "Remote", "timestamp": "2026-10-04T02:00:00+02:00"})
            assert c.session_wh == 2110
            assert c.last_session["energy_kwh"] == 2.11
            assert c.energy.total_wh == 2110

    run(go())


def test_energy_of_a_charge_started_by_the_charger_counts_from_its_start():
    async def go():
        async with Bench() as (c, st):
            await st.status("Preparing")
            await st.status("Charging")
            for wh in (0, 200, 470):
                await st.meter(meter(wh, THREE))
            await st.status("Finishing")
            await st.status("Preparing")
            await st.status("Charging")
            seen = []
            for wh in (0, 100, 200):
                await st.meter(meter(wh, THREE))
                seen.append(c.session_wh)
            assert seen[-1] == 200, seen
            assert seen == sorted(seen), seen

    run(go())


def test_changing_a_setting_in_a_charge_sends_nothing_to_the_charger():
    """Time audit: every switch flick re-read the configuration and could put
    the charge current back in the middle of a charge."""
    from gnlt_charger.charger import ChargerSettings

    async def go():
        async with Bench() as (c, st):
            await st.status("Charging")
            await asyncio.sleep(0.2)
            before = len(st.received)
            new = ChargerSettings(max_current_a=16, phases=3, auto_start=True)
            c.adopt(new)
            await asyncio.sleep(0.3)
            assert st.received[before:] == []
            c.adopt(ChargerSettings(max_current_a=16, phases=3, auto_start=True, meter_interval_s=30))
            await asyncio.sleep(0.3)
            assert [f[2:] for f in st.received[before:]] == [["ChangeConfiguration", {"key": "MeterValueSampleInterval", "value": "30"}]]

    run(go())


def test_no_start_into_the_chargers_own_paused_charge():
    """Time audit: own charge (no transaction) paused by the charger
    (SuspendedEVSE) with auto start on - no RemoteStartTransaction."""
    from gnlt_charger.charger import ChargerSettings

    async def go():
        async with Bench(settings=ChargerSettings(max_current_a=16, phases=3, auto_start=True)) as (c, st):
            await st.status("Preparing")
            await asyncio.sleep(0.1)
            st.received.clear()
            await st.status("Charging")
            await st.meter(meter(100, THREE))
            await st.status("SuspendedEVSE")
            await asyncio.sleep(0.3)
            await c.schedule_tick()
            assert "RemoteStartTransaction" not in st.actions()

    run(go())
