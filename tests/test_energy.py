"""Energy from the charger's own figures: every charge counted once, no debt."""

from datetime import UTC, datetime, timedelta

from gnlt_charger.logic import ChargeLedger

T0 = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)


def at(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


class Clock:
    def __init__(self) -> None:
        self.sec = 0.0

    def __call__(self, step: float = 31) -> datetime:
        self.sec += step
        return at(self.sec)


def charge(led, readings, meter_stop=None, meter_start=0.0, number=None, clock=None):
    clock = clock or Clock()
    led.start(meter_start, number, clock())
    for v in readings:
        led.reading(v, True, clock())
    deltas, rec = led.stop(meter_stop if meter_stop is not None else readings[-1], number, clock())
    return rec


def test_one_charge_is_the_chargers_final_figure():
    led = ChargeLedger()
    charge(led, [0, 1000, 2000], meter_stop=2010, number=1)
    assert led.total_wh == 2010
    assert led.session_wh == 2010


def test_two_charges_add_up():
    """Live 04.10.2026: 890 Wh, then 15 360 Wh."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 400, 880], meter_stop=890, number=1, clock=clock)
    charge(led, [0, 7000, 15340], meter_stop=15360, number=2, clock=clock)
    assert led.total_wh == 16250


def test_energy_of_the_charge_while_running():
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    for v in (0, 100, 200):
        led.reading(v, True, clock())
    assert led.session_wh == 200
    assert led.total_wh == 200


def test_idle_register_is_not_energy():
    """After a stop the register keeps the last value; refresh while idle."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 460], meter_stop=460, number=1, clock=clock)
    for _ in range(3):
        led.reading(460, False, clock())
    assert led.total_wh == 460


def test_final_figure_below_the_last_reading_is_what_is_recorded():
    """Owner 04.10.2026: the charger's final figure is the record; the
    difference is taken back once, in its own charge - never from the next."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 470], meter_stop=460, number=1, clock=clock)
    assert led.total_wh == 460
    assert led.session_wh == 460
    charge(led, [0, 100], meter_stop=100, number=2, clock=clock)
    assert led.total_wh == 560


def test_correction_is_published_as_a_negative_delta_at_the_last_reading():
    led = ChargeLedger()
    led.start(0, 1, at(0))
    led.reading(470, True, at(60))
    deltas, _ = led.stop(460, 1, at(90))
    assert [(d.wh, d.at) for d in deltas] == [(-10, at(60))]


def test_hundred_corrections_down_do_not_accumulate():
    """Platform review: 100 corrections in a row - the total is the sum of the
    charger's final figures, nothing carried over."""
    led = ChargeLedger()
    clock = Clock()
    for n in range(100):
        charge(led, [0, 470], meter_stop=460, number=n + 1, clock=clock)
    assert led.total_wh == 46000


def test_charge_the_charger_started_by_itself():
    """No StartTransaction: the first reading while charging opens it."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 300], meter_stop=300, number=1, clock=clock)
    for v in (20, 40, 60):
        led.reading(v, True, clock())
    led.stop(120, None, clock())
    assert led.total_wh == 420
    assert led.session_wh == 120


def test_own_start_with_first_reading_above_the_last_final_counts_from_zero():
    """Platform review (check C): the charger is known to count every charge from 0
    (its meterStart 0) - HA missed the start, first reading 500, final 700."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 200, 400], meter_stop=400, number=1, clock=clock)
    for v in (500, 700):
        led.reading(v, True, clock())
    led.stop(700, None, clock())
    assert led.total_wh == 1100
    assert led.session_wh == 700
    assert not led.last_record.approximate


def test_register_that_never_restarts():
    """Other firmware: meterStart is the lifetime register."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [50000, 50500], meter_stop=51000, meter_start=50000, number=1, clock=clock)
    for v in (51000, 51600):  # started by itself, register goes on
        led.reading(v, True, clock())
    led.stop(52000, None, clock())
    assert led.total_wh == 2000


def test_unknown_register_mode_is_marked_approximate():
    """Never saw a StartTransaction of this charger: the start of its own charge
    is a guess, and the charge says so."""
    led = ChargeLedger()
    clock = Clock()
    for v in (0, 300):
        led.reading(v, True, clock())
    led.stop(300, None, clock())
    assert led.total_wh == 300
    assert led.last_record.approximate


def test_one_contradicting_start_drops_the_mode_instead_of_switching_it():
    """Platform review: a wrong first choice must not silently turn the history."""
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 100], meter_stop=100, number=1, clock=clock)
    assert led.register_mode == "zero"
    charge(led, [5000, 5100], meter_stop=5100, meter_start=5000, number=2, clock=clock)
    assert led.register_mode == "unknown"
    assert led.total_wh == 200


def test_reset_in_the_middle_of_a_charge_with_readings_after():
    """Platform case: 65 530, reboot, register from 0 to 4 234, one stop."""
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    for v in (0, 20000, 40000, 65530, 0, 2000, 4234):
        led.reading(v, True, clock())
    led.stop(4234, 1, clock())
    assert led.total_wh == 69764
    assert led.session_wh == 69764
    assert led.last_record.resets == 1


def test_reset_with_only_the_final_figure_after_it():
    """The live charger sends no Transaction.End reading: the final figure
    alone shows the restart."""
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    for v in (0, 20000, 40000, 65530):
        led.reading(v, True, clock())
    led.stop(4234, 1, clock())
    assert led.total_wh == 69764
    assert led.session_wh == 69764


def test_next_charge_after_a_reset_is_counted_in_full():
    """Check A: the 0.2.9 ledger kept a 61 296 Wh debt and swallowed the next
    10 kWh charge."""
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    for v in (0, 30000, 65530):
        led.reading(v, True, clock())
    led.stop(4234, 1, clock())
    charge(led, [0, 5000, 10000], meter_stop=10000, number=2, clock=clock)
    assert led.total_wh == 79764


def test_new_charge_while_home_assistant_was_away():
    """Open charge at 5000, HA down, the charger finished it and began
    another; first reading after HA comes back: 300. The energy is right;
    both are one record (a restart of the register)."""
    led = ChargeLedger()
    clock = Clock()
    led.start(0, None, clock())
    led.reading(5000, True, clock())
    restored = ChargeLedger.from_dict(led.as_dict())
    restored.reading(300, True, clock())
    restored.reading(400, True, clock())
    assert restored.total_wh == 5400


def test_same_charge_after_a_home_assistant_restart():
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    led.reading(2000, True, clock())
    restored = ChargeLedger.from_dict(led.as_dict())
    restored.reading(2100, True, clock())
    restored.stop(2150, 1, clock())
    assert restored.total_wh == 2150


def test_stop_that_never_came():
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    led.reading(700, True, clock())
    charge(led, [0, 100], meter_stop=100, number=2, clock=clock)
    assert led.total_wh == 800
    assert led.records[0].approximate


def test_late_final_figure_replaces_the_estimate():
    """A charge closed without its stop; the stop comes later with its number."""
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    led.reading(700, True, clock())
    charge(led, [0, 100], meter_stop=100, number=2, clock=clock)
    led.stop(690, 1, clock())
    assert led.total_wh == 790
    assert not led.records[0].approximate


def test_stop_without_a_final_figure():
    led = ChargeLedger()
    clock = Clock()
    led.start(0, 1, clock())
    led.reading(700, True, clock())
    led.stop(None, 1, clock())
    assert led.total_wh == 700
    assert led.last_record.approximate


def test_duplicate_stop_is_not_counted_twice():
    led = ChargeLedger()
    clock = Clock()
    charge(led, [0, 700], meter_stop=700, number=1, clock=clock)
    led.stop(700, 1, clock())
    assert led.total_wh == 700


def test_duplicate_stop_without_number_after_a_restart_while_the_next_charge_runs():
    """Platform review: the same stop again (same figure, same moment) after a
    Home Assistant restart, while the next own charge is open."""
    led = ChargeLedger()
    led.start(0, None, at(0))
    led.reading(460, True, at(30))
    led.stop(460, None, at(60))
    restored = ChargeLedger.from_dict(led.as_dict())
    restored.reading(100, True, at(120))
    restored.reading(300, True, at(150))
    restored.stop(460, None, at(60))
    assert restored.total_wh == 760
    assert restored.open_record is not None


def test_late_stop_of_charge_a_without_number_while_b_runs():
    """Platform review: A (own, 470) ended by status, B runs (300), then A's stop
    without a number, 460, timed before B began: it settles A, B is untouched."""
    led = ChargeLedger(max_power_w=11000)
    led.reading(0, True, at(0))
    led.reading(470, True, at(30))
    led.close_open(at(40))
    for i, v in enumerate((0, 100, 200, 300)):
        led.reading(v, True, at(100 + 31 * i))
    _, rec = led.stop(460, None, at(35))
    assert rec is led.records[0]
    assert led.total_wh == 760
    assert led.open_record.energy_wh == 300


def test_late_stop_that_fits_two_charges_changes_nothing():
    """Same, but the stop carries no usable time: two charges fit, nothing is
    guessed - the stop is kept for the diagnostics file."""
    led = ChargeLedger(max_power_w=11000)
    led.reading(0, True, at(0))
    led.reading(470, True, at(30))
    led.close_open(at(40))
    for i, v in enumerate((0, 100, 200, 300)):
        led.reading(v, True, at(100 + 31 * i))
    _, rec = led.stop(460, None, at(300))
    assert rec is None
    assert led.total_wh == 770
    assert led.unresolved[-1]["meter_stop"] == 460


def test_several_charges_without_finals_settled_in_reverse_order():
    led = ChargeLedger()
    clock = Clock()
    for n, peak in ((1, 500), (2, 800), (3, 300)):
        led.start(0, n, clock())
        led.reading(peak, True, clock())
    led.close_open(clock())
    for n, final in ((3, 290), (2, 790), (1, 490)):
        led.stop(final, n, clock())
    assert led.total_wh == 1570
    assert all(not r.approximate for r in led.records)


def test_total_kept_from_an_older_version():
    led = ChargeLedger.from_dict({"total_wh": 2890.0, "peak_wh": 890.0})
    charge(led, [0, 15340], meter_stop=15360, number=1)
    assert led.total_wh == 18250


def test_open_charge_kept_from_the_first_0_2_9_format():
    led = ChargeLedger.from_dict(
        {"total_wh": 3000.0, "closed_wh": 2000.0, "open_base": 0.0, "open_wh": 1000.0, "open_last": 1000.0}
    )
    led.reading(1500, True, at(0))
    led.stop(1500, None, at(30))
    assert led.total_wh == 3500


def test_storage_round_trip_keeps_everything():
    led = ChargeLedger()
    charge(led, [0, 470], meter_stop=460, number=1)
    led.reading(100, True, at(999))
    again = ChargeLedger.from_dict(led.as_dict())
    assert again.as_dict() == led.as_dict()
