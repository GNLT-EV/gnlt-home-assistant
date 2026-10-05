"""Readings in every form OCPP 1.6 allows, one phase and three."""

from gnlt_charger.logic import extract_metrics


def sv(measurand, value, unit=None, phase=None, **extra):
    s = {"measurand": measurand, "value": str(value), "context": "Sample.Periodic"}
    if unit:
        s["unit"] = unit
    if phase:
        s["phase"] = phase
    s.update(extra)
    return s


# The frame our EVB11B sends (live, 04.10.2026 00:53): total power without a
# phase, voltage and current per phase.
LIVE_ONE_PHASE = [
    sv("Energy.Active.Import.Register", 190, "Wh", location="Outlet"),
    sv("Power.Active.Import", 2100, "W", location="Outlet"),
    sv("Voltage", 236, "V", "L1"),
    sv("Current.Import", 9.1, "A", "L1"),
    sv("Voltage", 0, "V", "L2"),
    sv("Current.Import", 0.0, "A", "L2"),
    sv("Voltage", 0, "V", "L3"),
    sv("Current.Import", 0.0, "A", "L3"),
    sv("Temperature", 31, "Celsius", location="Body"),
]


def test_live_frame_one_phase():
    m = extract_metrics(LIVE_ONE_PHASE)
    assert m.power_w == 2100
    assert m.energy_wh == 190
    assert m.current == {"L1": 9.1, "L2": 0.0, "L3": 0.0}
    assert m.voltage == {"L1": 236, "L2": 0, "L3": 0}
    assert m.temperature == 31


def test_three_phase_total_power():
    m = extract_metrics(
        [
            sv("Power.Active.Import", 11000, "W"),
            *(sv("Current.Import", 16, "A", p) for p in ("L1", "L2", "L3")),
            *(sv("Voltage", 230, "V", p) for p in ("L1", "L2", "L3")),
        ]
    )
    assert m.power_w == 11000
    assert m.current == {"L1": 16, "L2": 16, "L3": 16}


def test_power_per_phase_is_summed():
    m = extract_metrics([sv("Power.Active.Import", 3.6, "kW", p) for p in ("L1", "L2", "L3")])
    assert round(m.power_w) == 10800


def test_total_power_wins_over_phases():
    m = extract_metrics(
        [
            sv("Power.Active.Import", 3600, "W", "L1"),
            sv("Power.Active.Import", 3600, "W", "L2"),
            sv("Power.Active.Import", 3600, "W", "L3"),
            sv("Power.Active.Import", 10800, "W"),
        ]
    )
    assert m.power_w == 10800


def test_energy_per_phase_is_summed():
    m = extract_metrics([sv("Energy.Active.Import.Register", 1.5, "kWh", p) for p in ("L1", "L2", "L3")])
    assert round(m.energy_wh) == 4500


def test_energy_total_wins_over_phases():
    m = extract_metrics(
        [
            sv("Energy.Active.Import.Register", 1500, "Wh", "L1"),
            sv("Energy.Active.Import.Register", 4500, "Wh"),
            sv("Energy.Active.Import.Register", 1500, "Wh", "L2"),
        ]
    )
    assert m.energy_wh == 4500


def test_phase_to_neutral_names():
    m = extract_metrics([sv("Voltage", 231, "V", "L1-N"), sv("Voltage", 232, "V", "L2-N"), sv("Voltage", 233, "V", "L3-N")])
    assert m.voltage == {"L1": 231, "L2": 232, "L3": 233}


def test_line_to_line_voltage_does_not_overwrite_l1():
    m = extract_metrics([sv("Voltage", 230, "V", "L1-N"), sv("Voltage", 400, "V", "L1-L2")])
    assert m.voltage == {"L1": 230}


def test_neutral_current_is_not_l1():
    m = extract_metrics([sv("Current.Import", 16, "A", "L1"), sv("Current.Import", 0.4, "A", "N")])
    assert m.current == {"L1": 16}


def test_current_without_phase_is_l1():
    m = extract_metrics([sv("Current.Import", 10, "A")])
    assert m.current == {"L1": 10}


def test_offered_current_is_not_the_current():
    m = extract_metrics([sv("Current.Offered", 16, "A"), sv("Current.Import", 9.5, "A", "L1")])
    assert m.current == {"L1": 9.5}


def test_energy_of_other_kind_is_ignored():
    m = extract_metrics(
        [sv("Energy.Active.Import.Register", 1000, "Wh"), sv("Energy.Reactive.Import.Register", 99999, "varh")]
    )
    assert m.energy_wh == 1000


def test_defaults_without_measurand_and_unit():
    m = extract_metrics([{"value": "1234"}])
    assert m.energy_wh == 1234


def test_bad_values_are_skipped():
    m = extract_metrics([sv("Power.Active.Import", "x", "W"), "junk", None, sv("Voltage", 230, "V", "L1")])
    assert m.power_w is None
    assert m.voltage == {"L1": 230}
