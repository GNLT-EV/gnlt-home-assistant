"""Review 5, round 3: long-term statistics of the sum sensors as Home
Assistant compiles them. ``Recorder`` repeats the loop of
homeassistant/components/sensor/recorder.py (compile_statistics, the part
with last_reset / old_state / new_state / _sum, HA 2026.9.3, lines ~723-822);
the states come from the real sensor descriptions and a real Charger driven
through its OCPP handlers with a fake clock."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gnlt_charger import charger as charger_mod
from gnlt_charger.charger import Charger, ChargerSettings
from gnlt_charger.logic import Tariff, Totals
from gnlt_charger.sensor import SENSORS

TZ = "Europe/Warsaw"
WAW = ZoneInfo(TZ)
KEYS = ("energy_today", "energy_month", "session_energy", "total_energy", "cost_today", "cost_month")
DESC = {d.key: d for d in SENSORS}


class Recorder:
    """sum statistics of one 'total' sensor; one state per compile period."""

    def __init__(self, state=None, sum_=0.0, last_reset=None):
        self.stat = None if state is None else {"state": state, "sum": sum_, "last_reset": last_reset}

    def feed(self, fstate, lr):
        lr = None if lr is None else lr.astimezone(UTC).isoformat()
        if self.stat:
            old_last_reset = self.stat["last_reset"]
            new_state = old_state = self.stat["state"]
            _sum = self.stat["sum"]
        else:
            old_last_reset = None
            new_state = old_state = None
            _sum = 0.0
        reset = False
        if lr != old_last_reset and lr is not None:
            reset = True
        elif old_state is None and lr is None:
            reset = True
        if reset:
            if old_state is not None and new_state is not None:
                _sum += new_state - old_state
            new_state = fstate
            old_last_reset = lr
            old_state = 0.0 if old_state is not None else new_state
        else:
            new_state = fstate
        _sum += new_state - old_state
        self.stat = {"state": new_state, "sum": _sum, "last_reset": lr}

    @property
    def sum(self):
        return self.stat["sum"] if self.stat else 0.0


class World:
    def __init__(self, stored=None, start=datetime(2026, 10, 4, 12, 0, tzinfo=WAW), recorders=None):
        self.t = start
        charger_mod._now = lambda: self.t
        self.saved = {}
        self.c = Charger("102512111615", TZ, stored, self.saved.update, "PL")
        self.c.adopt(ChargerSettings(max_current_a=16, phases=3, tariff=Tariff(price=1.0)))
        self.rec = recorders or {k: Recorder() for k in KEYS}
        self.snap()

    def snap(self):
        for k in KEYS:
            d = DESC[k]
            v = d.value(self.c)
            if v is None:
                continue
            lr = d.last_reset(self.c) if d.last_reset else None
            self.rec[k].feed(float(v), lr)

    def tick(self, sec):
        self.t += timedelta(seconds=sec)

    def h(self, action, payload):
        _, after = getattr(self.c, f"_on_{action}")(payload)
        if after is not None:
            if action == "StatusNotification":
                asyncio.run(after)
            else:
                after.close()
        self.snap()

    def iso(self):
        return self.t.isoformat()

    def status(self, s):
        self.h("StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": s, "timestamp": self.iso()})

    def meter(self, wh):
        self.h("MeterValues", {"connectorId": 1, "meterValue": [{"timestamp": self.iso(), "sampledValue": [
            {"value": str(wh), "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]}]})

    def minute(self):
        self.c.roll_totals()
        self.snap()

    def charge(self, readings, final, step=600):
        r, _ = None, None
        _, after = self.c._on_StartTransaction({"connectorId": 1, "idTag": "HomeAssistant", "meterStart": 0, "timestamp": self.iso()})
        after.close()
        tx = self.c.tx_id
        self.snap()
        self.status("Charging")
        for wh in readings:
            self.tick(step)
            self.meter(wh)
            self.minute()
        self.tick(30)
        self.h("StopTransaction", {"meterStop": final, "transactionId": tx, "reason": "Local", "timestamp": self.iso()})
        self.status("Finishing")
        self.status("Available")


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    monkeypatch.setattr(charger_mod, "_now", charger_mod._now)


def test_upgrade_mid_day_then_midnight_then_month_end():
    """0.2.8 storage: 20 kWh today / 50 kWh this month already in HA's sums."""
    stored = {"energy": {"total_wh": 50000.0}, "totals": {"day": "2026-10-31", "day_wh": 20000.0, "day_cost": 20.0,
              "month": "2026-10", "month_wh": 50000.0, "month_cost": 50.0}}
    recs = {k: Recorder() for k in KEYS}
    recs["energy_today"] = Recorder(20.0, 1000.0, None)
    recs["energy_month"] = Recorder(50.0, 1000.0, None)
    recs["total_energy"] = Recorder(50.0, 1000.0, None)
    w = World(stored, datetime(2026, 10, 31, 20, 0, tzinfo=WAW), recs)
    w.charge([0, 1000, 2000], 2000)          # 2 kWh on 31.10
    w.t = datetime(2026, 11, 1, 0, 0, 0, tzinfo=WAW)
    w.minute()                                # midnight + new month
    w.tick(3600)
    w.charge([0, 1500], 1500)                 # 1.5 kWh on 01.11
    assert recs["total_energy"].sum == pytest.approx(1003.5)
    assert recs["energy_today"].sum == pytest.approx(1003.5), recs["energy_today"].stat
    assert recs["energy_month"].sum == pytest.approx(1003.5), recs["energy_month"].stat


def test_correction_down_in_the_day_and_charge_over_midnight():
    w = World(None, datetime(2026, 10, 4, 23, 0, tzinfo=WAW))
    w.charge([0, 470], 460)                                   # same day, -10 Wh
    w.tick(600)                                                # 23:30
    w.charge([0, 1000, 2000, 3000, 4000], 3990, step=600)      # over midnight
    total = w.c.energy.total_wh / 1000
    assert total == pytest.approx(4.45)
    assert w.rec["total_energy"].sum == pytest.approx(total - 0.0)  # first state is the zero point (0)
    assert w.rec["energy_today"].sum == pytest.approx(total), w.rec["energy_today"].stat
    assert w.rec["energy_month"].sum == pytest.approx(total)
    assert w.rec["session_energy"].sum == pytest.approx(total), w.rec["session_energy"].stat


def test_restart_keeps_the_cycles():
    w = World(None, datetime(2026, 10, 4, 10, 0, tzinfo=WAW))
    w.charge([0, 700], 700)
    w2 = World(dict(w.saved), w.t + timedelta(minutes=5), w.rec)
    w2.charge([0, 300], 300)
    for k in ("energy_today", "energy_month", "session_energy", "total_energy"):
        assert w2.rec[k].sum == pytest.approx(1.0), (k, w2.rec[k].stat)


def test_totals_never_roll_back_by_an_earlier_moment():
    """A reading stamped up to a minute ahead (station_timestamp keeps it)
    rolls the day at 23:59:30; a start at 23:59:45 by HA's clock must not
    roll it back and wipe the new day."""
    t = Totals()
    t.add(1000, 1.0, datetime(2026, 10, 4, 23, 0, tzinfo=WAW))
    t.add(100, 0.1, datetime(2026, 10, 5, 0, 0, 20, tzinfo=WAW))
    t.add_session(datetime(2026, 10, 4, 23, 59, 45, tzinfo=WAW))
    assert t.day == "2026-10-05" and t.day_wh == 100, t
