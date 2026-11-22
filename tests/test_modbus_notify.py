"""Notifications, repair issues and the pnc_reset fix flow of a ModBus entry
,."""

import asyncio
import re
from dataclasses import replace

import gnlt_charger.repairs as root_repairs
import pytest
from gnlt_charger.modbus import commands as mb_commands
from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import notify as mb_notify
from gnlt_charger.modbus import runtime as mb_runtime
from gnlt_charger.modbus.station import Snapshot
from homeassistant.helpers import issue_registry as ir
from modbus_ha import DOMAIN, SERIAL, make_hass, modbus_entry, stop

LANGS = ("en", "ru", "pl")
STATION_KINDS = ("faulted", "link_lost", "identity_mismatch", "relay_welded", "pnc_reset")
# 's list (authoritative) - every kind has its own text.
R801 = (
    "faulted", "empty_session", "link_lost", "station_restarted", "identity_mismatch", "self_start",
    "finished_replug", "relay_welded", "pnc_reset", "current_clamped",
)
# Kinds of the whole-amp Variant B sequence and its chatter watchdog: gone with
# them (the EM2GO workaround instead).
REMOVED_KINDS = (
    "phase_switch_failed", "phase_chatter", "phase_reverted", "phase_restart_not_taken", "long_stall",
    "pair_broken_unplug", "station_fault",
    "sequence_cancelled",  # only from "Control active" off
    "write_breaker",  # the write breaker
    # no verification, read-back or persisted step any more
    "sequence_interrupted", "stop_not_working", "start_not_taken", "stop_inconclusive", "slow_stop",
    "schedule_stop_skipped", "current_not_applied", "phase_not_applied", "mode_not_applied", "pnc_reset_restored",
    "auto_start_skipped",  # the start quota and its deferral
    "restore_dropped",  # the 91 restore limits
)
SAMPLE = {
    "code": 19, "hint": "refused", "serial_seen": "101234567891", "log": "91=160 (restore)", "value": 16.0,
    "reason": "link", "kind": "phase", "step": "stop", "observed": {"status": 4, "current_a": 6.0, "phases": 1, "setpoint_a": 6.0},
    "desired_a": 32.0, "applied_a": 16.0, "current_a": 10.0, "phases": 3, "station_phases": 1, "resume": True,
    "start": "10:05", "end": "11:40",
}


def run(coro):
    return asyncio.run(coro)


def test_every_kind_has_its_own_text_in_every_language():
    kinds = set(R801) | set(mb_commands.NOTIFY_KINDS) | set(STATION_KINDS)
    for lang in LANGS:
        assert set(mb_notify.TEXTS[lang]) == set(mb_notify.TEXTS["en"]), lang
        missing = kinds - set(mb_notify.TEXTS[lang])
        assert not missing, (lang, missing)
        assert not set(REMOVED_KINDS) & set(mb_notify.TEXTS[lang]), lang
    assert not set(REMOVED_KINDS) & set(mb_commands.NOTIFY_KINDS)
    for table in (mb_notify.STATUS_NAMES, mb_notify.ERROR_NAMES, mb_notify.RECOVERY, mb_notify.LINK_HINTS,
                  mb_notify.OBSERVED, mb_notify.REASONS, mb_notify.RESUME):
        assert all(set(table[lang]) == set(table["en"]) for lang in LANGS)
    assert set(mb_notify.STATUS_NAMES["en"]) == set(C.STATUS_KEYS.values())
    assert set(mb_notify.ERROR_NAMES["en"]) == set(C.ERROR_KEYS.values()) | {C.ERROR_UNKNOWN}


@pytest.mark.parametrize("lang", LANGS)
def test_render_all_kinds_no_missing_placeholders(lang):
    for kind in mb_notify.TEXTS[lang]:
        title, message = mb_notify.render(lang, kind, SAMPLE, "GNLT 301234567890 ModBus")
        assert "{" not in title + message and "}" not in title + message, kind
        assert "?" not in title + message, (kind, message)
        assert title.startswith("GNLT 301234567890 ModBus:")
    # Same placeholders in every language.
    for kind, (title, message) in mb_notify.TEXTS["en"].items():
        want = set(re.findall(r"{(\w+)}", title + message))
        got = set(re.findall(r"{(\w+)}", "".join(mb_notify.TEXTS[lang][kind])))
        assert got == want, (lang, kind)


def test_fault_hint_mapping_and_recovery_texts():
    t = mb_notify.fault_texts("en", 19)
    assert "E0020" in t["error"] and "current" in t["cause"]  # OCPP OverCurrentFailure cause
    assert "mains power" in mb_notify.fault_texts("en", 17)["recovery"]  # leakage: power off
    assert "emergency" in mb_notify.fault_texts("en", 12)["recovery"]
    assert "CP mode" in mb_notify.fault_texts("en", 11)["recovery"]
    assert "Grounding selection" in mb_notify.fault_texts("en", 25)["recovery"]
    assert "service" in mb_notify.fault_texts("en", 23)["recovery"]
    assert "earth" in mb_notify.fault_texts("en", 24)["cause"]
    assert mb_notify.fault_texts("en", 99)["error"] == "error 99"
    assert mb_notify.fault_texts("ru", 19)["cause"].startswith("Обычно")
    assert mb_notify.fault_texts("pl", 13)["cause"].startswith("Ładowanie będzie możliwe")


def test_link_hints_only_after_persistent_failure_class():
    en = mb_notify.render("en", "link_lost", {"hint": None}, "X")[1]
    assert "Last error" not in en
    assert "refused" in mb_notify.render("en", "link_lost", {"hint": "refused"}, "X")[1]
    assert "not reachable" in mb_notify.render("en", "link_lost", {"hint": "connect_timeout"}, "X")[1]


async def _watched(monkeypatch):
    hass = await make_hass()
    entry = modbus_entry(hass, 1, host="192.0.2.7")
    rt = mb_runtime.build_runtime(hass, entry, mb_runtime.ModbusClient("192.0.2.7", 1), ())
    entry.runtime_data = rt
    mb_runtime.modbus_data(hass)["entries"][entry.entry_id] = rt
    shown, dismissed = {}, []
    monkeypatch.setattr(
        mb_notify.persistent_notification, "async_create",
        lambda hass, message, title=None, notification_id=None: shown.__setitem__(notification_id, (title, message)),
    )
    monkeypatch.setattr(
        mb_notify.persistent_notification, "async_dismiss", lambda hass, notification_id: dismissed.append(notification_id)
    )
    stop_watch = mb_notify.watch(hass, rt)
    return hass, entry, rt, shown, dismissed, stop_watch


def _nid(kind):
    return f"{DOMAIN}_{SERIAL}_mb_{kind}"


def _snap(rt, t, **kw):
    snap = replace(Snapshot(), time=t, **kw)
    rt.station._snap = snap  # as Station.async_tick: the snapshot is current when listeners run
    for cb in list(rt.station._listeners):
        cb(snap)


def test_link_lost_notification_and_repair_then_restored(monkeypatch):
    async def main():
        hass, entry, rt, shown, dismissed, stop_watch = await _watched(monkeypatch)
        try:
            rt.station._emit(C.EVENT_LINK_RESTORED)
            await asyncio.sleep(0)
            assert dismissed == []  # dismissed only if it was shown
            rt.station._emit(C.EVENT_LINK_LOST, hint="refused")
            await asyncio.sleep(0)
            assert _nid("link_lost") in shown and "refused" in shown[_nid("link_lost")][1]
            issue = ir.async_get(hass).async_get_issue(DOMAIN, f"station_not_responding_{SERIAL}")
            assert issue is not None and issue.translation_placeholders["name"] == entry.title
            rt.station._emit(C.EVENT_LINK_RESTORED)
            await asyncio.sleep(0)
            assert _nid("link_lost") in dismissed
            assert ir.async_get(hass).async_get_issue(DOMAIN, f"station_not_responding_{SERIAL}") is None
            rt.station._emit(C.EVENT_IDENTITY_MISMATCH, serial_seen="101234567891")
            await asyncio.sleep(0)
            assert "101234567891" in shown[_nid("identity_mismatch")][1]
            issue = ir.async_get(hass).async_get_issue(DOMAIN, f"identity_mismatch_{SERIAL}")
            assert issue.severity == ir.IssueSeverity.ERROR
        finally:
            stop_watch()
            await rt.commander.async_close()
            await stop(hass)

    run(main())


def test_fault_shown_also_on_first_poll_and_again_on_new_code(monkeypatch):
    """like OCPP, a fault already there when HA starts is shown too."""

    async def main():
        hass, entry, rt, shown, dismissed, stop_watch = await _watched(monkeypatch)
        try:
            rt.station._emit(C.EVENT_FAULT, code=19, key="over_current", first_poll=True)
            await asyncio.sleep(0)
            assert "E0020" in shown[_nid("faulted")][1]
            shown.clear()
            rt.station._emit(C.EVENT_FAULT, code=19, key="over_current", first_poll=False)
            await asyncio.sleep(0)
            assert "E0020" in shown[_nid("faulted")][1]
            rt.station._emit(C.EVENT_FAULT, code=13, key="under_voltage", first_poll=False)
            await asyncio.sleep(0)
            assert "E0008" in shown[_nid("faulted")][1]
            rt.station._emit(C.EVENT_FAULT_CLEARED, code=13, first_poll=False)
            await asyncio.sleep(0)
            assert _nid("faulted") in dismissed
        finally:
            stop_watch()
            await rt.commander.async_close()
            await stop(hass)

    run(main())


def test_notification_on_first_poll_too_and_state_dismissed(monkeypatch):
    """nothing is held back on start-up (OCPP shows a flag that is on
    when HA starts)."""

    async def main():
        hass, entry, rt, shown, dismissed, stop_watch = await _watched(monkeypatch)
        try:
            rt.commander._notify("empty_session", startup=True)
            rt.commander._notify("station_restarted", startup=True, resume=False, record_open_before=True)
            await asyncio.sleep(0)
            assert _nid("empty_session") in shown and _nid("station_restarted") in shown
            # State-type: dismissed only by a later poll without the flag.
            _snap(rt, 100.0, problems=frozenset({"empty_session"}))
            rt.commander._notify("empty_session", startup=False)
            await asyncio.sleep(0)
            assert _nid("empty_session") in shown
            _snap(rt, 100.0, problems=frozenset())  # the same poll the notification came with
            await asyncio.sleep(0)
            assert _nid("empty_session") not in dismissed
            _snap(rt, 110.0, problems=frozenset())
            await asyncio.sleep(0)
            assert _nid("empty_session") in dismissed
        finally:
            stop_watch()
            await rt.commander.async_close()
            await stop(hass)

    run(main())


def test_relay_welded_and_pnc_reset_repairs_with_fix_flow(monkeypatch):
    async def main():
        hass, entry, rt, shown, dismissed, stop_watch = await _watched(monkeypatch)
        try:
            _snap(rt, 10.0, error=C.ERROR_RELAY_WELDED, problems=frozenset({C.PROBLEM_PNC_RESET}))
            await asyncio.sleep(0)
            reg = ir.async_get(hass)
            relay = reg.async_get_issue(DOMAIN, f"relay_welded_{SERIAL}")
            assert relay is not None and relay.severity == ir.IssueSeverity.ERROR and not relay.is_fixable
            pnc = reg.async_get_issue(DOMAIN, f"pnc_reset_{SERIAL}")
            assert pnc is not None and pnc.is_fixable and pnc.data["entry_id"] == entry.entry_id
            assert _nid("relay_welded") in shown and _nid("pnc_reset") in shown
            # The fix flow writes 93 = 1 through the command layer (one click).
            calls = []

            async def restore():
                calls.append("93=1")

            monkeypatch.setattr(rt.commander, "async_pnc_restore", restore)
            flow = await root_repairs.async_create_fix_flow(hass, f"pnc_reset_{SERIAL}", dict(pnc.data))
            flow.hass = hass
            result = await flow.async_step_init()
            assert result["type"] == "form" and result["step_id"] == "confirm"
            assert calls == []  # nothing written before the click
            result = await flow.async_step_confirm({})
            assert result["type"] == "create_entry" and calls == ["93=1"]
            # Refused write: shown in the form, translated key.
            async def refused():
                raise mb_commands.CommandError(C.REFUSE_NOT_CONNECTED)

            monkeypatch.setattr(rt.commander, "async_pnc_restore", refused)
            result = await flow.async_step_confirm({})
            assert result["errors"] == {"base": "not_connected"}
            # Flags gone on a later poll: issues removed.
            _snap(rt, 20.0, error=0, problems=frozenset())
            assert reg.async_get_issue(DOMAIN, f"relay_welded_{SERIAL}") is None
            assert reg.async_get_issue(DOMAIN, f"pnc_reset_{SERIAL}") is None
        finally:
            stop_watch()
            await rt.commander.async_close()
            await stop(hass)

    run(main())


def test_self_start_shown_while_it_lasts_then_summary_with_times(monkeypatch):
    """Like OCPP charging_without_session: shown while the charge
    without our command lasts, then replaced (same id) by a summary with the
    start and end time."""

    async def main():
        hass, entry, rt, shown, dismissed, stop_watch = await _watched(monkeypatch)
        try:
            rt.commander._charging_without_session = True
            _snap(rt, 100.0, status=4)
            await asyncio.sleep(0)
            assert "Plug & Charge" in shown[_nid("self_start")][1]
            rt.commander._charging_without_session = False
            _snap(rt, 110.0, status=2)
            await asyncio.sleep(0)
            title, message = shown[_nid("self_start")]
            assert "charged by itself" in title and " to " in message and _nid("self_start") not in dismissed
            assert "self_start" not in mb_commands.NOTIFY_KINDS
        finally:
            stop_watch()
            await rt.commander.async_close()
            await stop(hass)

    run(main())


def test_fault_recovery_replug_advice_only_for_vehicle_faults():
    """the default recovery text told the user to unplug and
    plug in again for every unlisted code; only 21/22 (REPLUG_FAULTS) get
    that advice, other codes a neutral text."""
    from gnlt_charger.modbus import notify as mb_notify

    replug = {"en": "plug it in again", "pl": "podłącz go ponownie", "ru": "вставьте снова"}
    for lang, phrase in replug.items():
        for code in sorted(C.REPLUG_FAULTS):
            assert phrase in mb_notify.fault_texts(lang, code)["recovery"], (lang, code)
        for code in (13, 14, 19, 24, 99, None):
            assert phrase not in mb_notify.fault_texts(lang, code)["recovery"], (lang, code)
