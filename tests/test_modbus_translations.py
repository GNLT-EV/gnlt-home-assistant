"""Translations, texts and docs of the ModBus mode:; every key the ModBus code uses exists in en/ru/pl."""

import asyncio
import json
import os
import re

from gnlt_charger.modbus import commands as mb_commands
from gnlt_charger.modbus import config_flow as mbcf
from gnlt_charger.modbus import const as C
from gnlt_charger.modbus import entities as mb_entities
from gnlt_charger.modbus import repairs as mb_repairs
from gnlt_charger.modbus import runtime as mb_runtime
from modbus_ha import COMPONENT_DIR, make_hass, modbus_entry, stop, translations

LANGS = ("en", "ru", "pl")
REPO = os.path.dirname(os.path.dirname(COMPONENT_DIR))


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _placeholders(text):
    return set(re.findall(r"{(\w+)}", text))


def test_strings_equal_en_and_all_languages_have_every_key():
    assert translations("strings") == translations("en")
    flat = {lang: _flat(translations(lang)) for lang in LANGS}
    for lang in ("ru", "pl"):
        assert set(flat[lang]) == set(flat["en"]), set(flat[lang]) ^ set(flat["en"])
        for key, text in flat["en"].items():
            assert _placeholders(flat[lang][key]) == _placeholders(text), (lang, key)


def test_no_foreign_script_or_german():
    for lang in ("en", "pl"):
        for key, text in _flat(translations(lang)).items():
            assert not re.search(r"[а-яА-ЯёЁ]", text), (lang, key)
    for key, text in _flat(translations("pl")).items():
        assert not re.search(r"[äöüßÄÖÜ]", text), key


def _entities():
    async def main():
        hass = await make_hass()
        try:
            entry = modbus_entry(hass, 1)
            rt = mb_runtime.build_runtime(hass, entry, mb_runtime.ModbusClient("127.0.0.1", 1), ())
            entry.runtime_data = rt
            out = []
            for platform in ("sensor", "binary_sensor", "switch", "number", "select", "button", "time"):
                out += [(platform, e) for e in mb_entities.build_entities(hass, entry, platform)]
            await rt.commander.async_close()
            await rt.client.close()
            return out
        finally:
            await stop(hass)

    return asyncio.run(main())


def test_translation_keys_exist_all_langs():
    ents = _entities()
    for lang in LANGS:
        names = translations(lang)["entity"]
        for platform, entity in ents:
            key = entity.translation_key
            assert key in names[platform] and names[platform][key].get("name"), (lang, platform, key)
        sensor = names["sensor"]
        assert set(sensor["mb_status"]["state"]) == set(mb_entities.STATUS_OPTIONS)
        assert set(sensor["mb_error"]["state"]) == set(mb_entities.ERROR_OPTIONS)
        assert set(names["select"]["mb_phases"]["state"]) == set(mb_entities.PHASE_OPTIONS_AUTO)
    # New keys are mb_-prefixed; reused ones are the OCPP keys.
    ocpp = {"status", "error"}
    assert not any(e.translation_key in ocpp for _p, e in ents)


def test_exception_keys_and_placeholders():
    keys = set(mb_commands.REFUSAL_KEYS) | {
        v for k, v in vars(C).items() if k.startswith("REFUSE_")
    }
    for lang in LANGS:
        exc = translations(lang)["exceptions"]
        missing = keys - set(exc)
        assert not missing, (lang, missing)
        for key in keys:
            assert _placeholders(exc[key]["message"]) <= {"detail"}, (lang, key)


def test_config_options_selector_issue_keys():
    errors_flow = {"mb_host_empty", "mb_cannot_resolve", "mb_no_answer", "mb_refused", "mb_unreachable", "mb_busy",
                   "mb_not_ready", "mb_not_gnlt", "mb_exception", "mb_address_unknown"}
    for lang in LANGS:
        t = translations(lang)
        cfg = t["config"]
        assert "modbus_intro" in cfg["step"]["user"]["menu_options"]
        for step in ("modbus_intro", "modbus_address", "modbus_model"):
            assert cfg["step"][step]["title"], (lang, step)
        assert errors_flow <= set(cfg["error"]) and "preset_required" in cfg["error"]
        assert {"address_in_use", "already_configured"} <= set(cfg["abort"])
        assert cfg["create_entry"]["modbus_created"]
        opt = t["options"]
        assert errors_flow | {"mb_serial_mismatch", "address_in_use"} <= set(opt["error"])
        fields = set(opt["step"]["modbus"]["data"])
        assert {"energy_source", "host", "port"} <= fields
        # removed
        assert not {"allow_phase_with_car", "phase_3p_min_hold_s", "skip_10_16", "phase_switch_while_charging"} & fields
        assert "current_a3b_range" not in t["exceptions"]
        assert "mb_profile" not in t["selector"] and "profile" not in fields  # no firmware profiles
        assert set(t["config"]["step"]["modbus_model"]["data"]) == {"preset"}
        assert "2.7" in t["config"]["step"]["modbus_model"]["description"]  # the one static note
        assert "stop_refused_profile" not in t["exceptions"]
        # no fresh read / read-back
        assert not {"state_changed", "current_not_applied", "mode_not_applied"} & set(t["exceptions"])
        assert set(t["selector"]["mb_energy_source"]["options"]) == {"integral", "register"}
        issues = t["issues"]
        for key in ("station_not_responding", "identity_mismatch", "relay_welded"):
            assert issues[key]["title"] and issues[key]["description"]
        fix = issues["pnc_reset"]["fix_flow"]
        assert set(fix["error"]) == set(mb_repairs.FIX_ERRORS) | {"write_failed"}
        assert fix["step"]["confirm"]["description"] and "not_loaded" in fix["abort"]


def test_notice_file_header_and_manifest_unchanged():
    notice = open(os.path.join(COMPONENT_DIR, "modbus", "NOTICE"), encoding="utf-8").read()
    header = open(os.path.join(COMPONENT_DIR, "modbus", "client.py"), encoding="utf-8").read(2000)
    for text in (notice, header):
        assert "Copyright (c) 2026 Lutz Fiebach" in text and "MIT License" in text
        assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in text
    manifest = json.load(open(os.path.join(COMPONENT_DIR, "manifest.json"), encoding="utf-8"))
    assert manifest["requirements"] == []
    assert "dhcp" not in manifest and "zeroconf" not in manifest


def test_no_leftovers_of_removed_phase_rules():
    """no unused constants / imports; NOTICE says what the code does."""
    from gnlt_charger.modbus import commands as mb_cmd

    assert not hasattr(C, "PHASE_CURRENT_DROP_WAIT_S")
    assert not hasattr(mb_cmd, "STOP_VERIFIED") and not hasattr(mb_cmd, "STOP_INCONCLUSIVE")
    notice = open(os.path.join(COMPONENT_DIR, "modbus", "NOTICE"), encoding="utf-8").read()
    flat = " ".join(notice.split())
    assert "200 = 3 is written only if that write went out" in flat
    assert "whole-amp" in flat


def test_readme_sections_exist():
    for name, heading in (("README.md", "## ModBus mode"), ("README.ru.md", "## Режим ModBus"), ("README.pl.md", "## Tryb ModBus")):
        path = os.path.join(REPO, name)
        if not os.path.exists(path):  # the test copy holds only custom_components and tests
            continue
        text = open(path, encoding="utf-8").read()
        assert heading in text, name


def test_review6_refusal_texts_no_raw_keys_no_english_product_names():
    """pl/ru refusals and repairs carry no raw keys and no English
    product phrase; the class-unknown text promises no re-probe; the replug
    advice only for the vehicle faults (21/22)."""
    keys = [
        ("exceptions", "phase_refused_class_unknown"), ("exceptions", "site_member_pnc"),
        ("exceptions", "station_busy"), ("exceptions", "mb_faulted"), ("exceptions", "mb_faulted_replug"),
    ]
    for lang in ("pl", "ru"):
        t = translations(lang)
        for section, key in keys:
            text = t[section][key]["message"]
            assert "Plug & Charge" not in text, (lang, key)
            assert not re.search(r"\b[a-z]+_[a-z_]+\b", text), (lang, key, text)
        for key, issue in t["issues"].items():
            for text in issue.values() if isinstance(issue, dict) else ():
                if isinstance(text, str):
                    assert not re.search(r"\b[a-z]+_on\b", text), (lang, key, text)
    for lang in LANGS:
        exc = translations(lang)["exceptions"]
        assert exc["mb_faulted"]["message"] != exc["mb_faulted_replug"]["message"], lang
    en = translations("en")["exceptions"]
    assert "idle" not in en["phase_refused_class_unknown"]["message"]
    assert "plug it in again" in en["mb_faulted_replug"]["message"]
    assert "plug it in again" not in en["mb_faulted"]["message"]


def test_review6_station_busy_and_fault_details_in_the_user_language():
    from types import SimpleNamespace

    from gnlt_charger.modbus.commands import CommandError
    from gnlt_charger.modbus.entities import _detail

    busy = CommandError(C.REFUSE_STATION_BUSY, status=4)
    assert _detail(busy, "pl") == "ładowanie" and _detail(busy, "en") == "charging"
    assert _detail(busy, "ru") == "зарядка"
    assert _detail(CommandError(C.REFUSE_STATION_BUSY, status=99), "en") == "99"


def test_review6_fault_refusal_replug_only_for_vehicle_faults():
    import pytest

    from gnlt_charger.modbus.station import WriteRefused
    from modbus_cmd_rig import Car, go

    async def main(R):
        for code, key, detail in ((19, "mb_faulted", "E0020"), (21, "mb_faulted_replug", "21"), (22, "mb_faulted_replug", "22")):
            rig = await R().start()
            rig.plug(Car())
            await rig.run(10)
            rig.fake.set_error(code)
            await rig.run(20)
            with pytest.raises(WriteRefused) as err:
                await rig.call(rig.cmd.async_start())
            assert err.value.key == key, code
            from gnlt_charger.modbus.entities import _detail

            assert _detail(err.value) == detail

    go(main)


def test_dlm_site_marked_beta_in_all_languages():
    """Release 0.3.0: the GNLT site (DLM + PV) is beta - visible in the menu,
    every site step title, the new entry title and device model, and the site
    step says it is no replacement for the installation's breaker."""
    from gnlt_charger.site import config_flow as site_cf

    marks = {"en": "(beta)", "pl": "(beta)", "ru": "(бета)"}
    warn = {"en": "circuit breaker", "pl": "wyłącznika nadprądowego", "ru": "автоматический выключатель"}
    for lang, mark in marks.items():
        t = translations(lang)
        assert t["config"]["step"]["user"]["menu_options"]["site"].endswith(mark), lang
        for section in ("config", "options"):
            for step in ("site", "site_member", "site_pv"):
                assert t[section]["step"][step]["title"].endswith(mark), (lang, section, step)
            assert warn[lang] in t[section]["step"]["site"]["description"], (lang, section)
    assert site_cf.SITE_TITLE.endswith("(beta)")


def test_connection_options_name_their_protocol():
    """Release 0.3.0: every connection option names its
    protocol - the Bluetooth set-up and "already on the network" end in an
    OCPP entry, next to "ModBus TCP"."""
    for lang in LANGS:
        menu = translations(lang)["config"]["step"]["user"]["menu_options"]
        assert "OCPP" in menu["pick_device"] and menu["manual"].startswith("OCPP:"), lang
        assert "ModBus" in menu["modbus_intro"], lang


def test_one_firmware_2_7_statement_in_the_modbus_flow():
    """Release 0.3.0: the 2.7 rule (stop and phase switching while charging
    do not work over ModBus, A3B_2.7 and A3C_2.7) is stated once - in the
    model step; the intro no longer has the old EVB-only sentence."""
    for lang in LANGS:
        steps = translations(lang)["config"]["step"]
        assert "A3B_2.7" not in steps["modbus_intro"]["description"], lang
        model = steps["modbus_model"]["description"]
        assert "3.1" in model and "2.7" in model and "A3C_2.7" not in model, lang  # firmware note (release 0.3.0)


def test_release_version_0_3_0b1():
    """First a GitHub pre-release: 0.3.0b1 (PEP 440 beta form, valid for
    AwesomeVersion/HA); ModBus and the DLM site (beta)."""
    manifest = json.load(open(os.path.join(COMPONENT_DIR, "manifest.json"), encoding="utf-8"))
    assert manifest["version"] == "0.3.0b1"
