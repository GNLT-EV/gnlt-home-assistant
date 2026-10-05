"""Scenarios shared with the GNLT platform (tests/vectors.json, hand-derived
expectations confirmed by the platform). Same inputs,
same outputs - "same rules, same numbers". A difference is a question for the
partners, never a reason to edit the expectations here.

One difference is declared (owner's decision 04.10.2026, agreed with the
platform): the energy of a charge is the charger's final figure when the
register did not restart; the platform keeps the sum of increments when that
sum is more than 100 Wh above it.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gnlt_charger.logic import ChargeLedger, one_phase_power_fix

VECTORS = json.loads((Path(__file__).parent / "vectors.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)

# id -> energy of the charge in Home Assistant where it differs from the platform.
HA_SESSION_ENERGY = {
    "final-slightly-below-peak": 22450,
    "double-dip-at-end": 22450,
    "correction-down-340wh": 40380,
}


def run(s):
    led = ChargeLedger(max_power_w=s["max_power_w"])
    led.start(s["start_wh"], 1, T0)
    for p in s["points"]:
        if p.get("final"):
            sec = s["final_at_sec"] if s["final_at_sec"] is not None else p["sec"]
            led.stop(p["wh"], 1, T0 + timedelta(seconds=sec))
        else:
            led.reading(p["wh"], True, T0 + timedelta(seconds=p["sec"]))
    return led.last_record


@pytest.mark.parametrize("s", VECTORS["scenarios"], ids=lambda s: s["id"])
def test_energy_scenario(s):
    r = run(s)
    exp = s["expected"]
    assert r.inc_wh == exp["total_wh"], "sum of increments"
    assert r.ambiguous == exp["ambiguous"], "ambiguous choices"
    if exp["session_energy_wh"] is not None:
        want = HA_SESSION_ENERGY.get(s["id"], exp["session_energy_wh"])
        assert r.energy_wh == want, "energy of the charge"


def test_declared_differences_are_still_differences():
    """If the platform changes, this list must shrink - not stay stale."""
    by_id = {s["id"]: s for s in VECTORS["scenarios"]}
    for sid, ours in HA_SESSION_ENERGY.items():
        assert by_id[sid]["expected"]["session_energy_wh"] != ours, sid


@pytest.mark.parametrize("s", VECTORS["power_scenarios"], ids=lambda s: s["id"])
def test_power_scenario(s):
    voltage = dict(zip(("L1", "L2", "L3"), s["voltage_v"]))
    current = dict(zip(("L1", "L2", "L3"), s["current_a"]))
    shown = one_phase_power_fix(
        s["power_w"],
        voltage,
        current,
        per_phase_power=s.get("explicit_phase_power", False),
        power_factor=s.get("power_factor"),
        profile_confirmed=s.get("profile_confirmed", True),
    )
    assert shown == pytest.approx(s["expected"]["active_power_w"], abs=1)
