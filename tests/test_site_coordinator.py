"""Site coordinator (site/coordinator.py): two fake stations (real Station +
Commander on one virtual clock) and fake grid meter sensor states in a real
Home Assistant state machine.

DLM cap applied at once on overload; meter stale -> failure action; station
offline -> reserved, not commanded; PV starts after the enable delay and stops
after the disable delay; no automatic features of a member; stop of the site
leaves members at their minimum current.
"""

from __future__ import annotations

import asyncio

from datetime import timedelta
from types import SimpleNamespace

from gnlt_charger.site.config import MemberConfig, SiteConfig
from gnlt_charger.site.coordinator import SiteCoordinator, no_automatic_features
from gnlt_charger.site.dlm import MeterFailureAction, MeterState, Reason
from gnlt_charger.logic import Schedule
from gnlt_charger.modbus.commands import ORIGIN_SITE, CommandOptions
from modbus_cmd_rig import WALL0, Car, VClock, go
from modbus_ha import make_hass, stop

GRID = ("sensor.grid_l1", "sensor.grid_l2", "sensor.grid_l3")


class FakeRuntime:
    """What the coordinator uses of a ModbusRuntime."""

    def __init__(self, rig, entry_id: str) -> None:
        self.rig = rig
        self.station = rig.st
        self.commander = rig.cmd
        self.serial = f"serial-{entry_id}"
        self.entry = SimpleNamespace(entry_id=entry_id)
        self.site_options_filter = None
        self.platform_adders: dict = {}
        self.base_options = rig.options
        self.updates = 0

    def command_options(self):
        opts = self.base_options
        return self.site_options_filter(opts) if self.site_options_filter else opts

    def async_update(self) -> None:
        self.updates += 1


class Site:
    def __init__(self, hass, clock, rigs, coord, rts) -> None:
        self.hass, self.clock, self.rigs, self.coord, self.rts = hass, clock, rigs, coord, rts
        self.base = [0.0, 0.0, 0.0]
        self.meter_live = True
        self.tick = 0
        self.deleted: set[str] = set()  # station config entries deleted in HA

    def wall(self):
        return WALL0 + timedelta(seconds=self.clock() - 1000.0)

    def grid(self) -> list[float]:
        out = list(self.base)
        for rig in self.rigs:
            for p, c in enumerate(rig.fake.currents()):
                out[p] += c
        return out

    def set_meter(self) -> None:
        if not self.meter_live:
            return
        ts = self.wall().timestamp()
        self.tick += 1
        noise = 0.02 if self.tick % 2 else -0.02  # a real meter is never exactly constant
        for entity_id, value in zip(GRID, (v + noise for v in self.grid())):
            self.hass.states.async_set(entity_id, f"{value:.2f}", {"unit_of_measurement": "A"}, timestamp=ts)

    async def cycle(self, seconds: float = 10.0) -> None:
        self.set_meter()
        await self.coord.async_cycle()
        print(f"t={self.clock():.0f} " + self.dump(short=True))
        await self.clock.run(seconds)

    async def cycles(self, n: int, seconds: float = 10.0) -> None:
        for _ in range(n):
            await self.cycle(seconds)

    def writes(self, i: int) -> list[tuple[int, int]]:
        return self.rigs[i].writes()

    def dump(self, short: bool = False) -> str:
        out = [f"grid={self.grid()} meter={self.coord.result.meter_state if self.coord.result else None}"]
        for rig, m in zip(self.rigs, self.coord.members):
            lp = m.loadpoint
            if short:
                a = m.allocation
                out.append(
                    f"{m.entry_id}: st={rig.st.snapshot.status} cur={rig.st.snapshot.currents} sp={m.setpoint()} "
                    f"allowed={a.allowed_a if a else None} {a.reason.value if a else None} req={m.requested} "
                    f"en={lp.enabled if lp else None} off={lp.offered_current if lp else None} w={rig.writes()[-3:]}"
                )
                continue
            out.append(
                f"{m.entry_id}: status={rig.st.snapshot.status} writes={rig.writes()} alloc={m.allocation} "
                f"req={m.requested} err={m.adapter.last_error if m.adapter else None} "
                f"lp_en={lp.enabled if lp else None} offered={lp.offered_current if lp else None} "
                f"lp_status={lp.status if lp else None} mode={lp.mode if lp else None} "
                f"sent={m.adapter.sent if m.adapter else None} notes={rig.kinds()}"
            )
        return " | ".join(out)


async def make_site(R, hass, *, profiles=("A3C_2.7", "A3C_2.7"), options=None, cars=(None, None), **cfg) -> Site:
    clock = VClock()
    rigs = []
    for profile in profiles:
        rig = R(profile, clock=clock, options=options or CommandOptions())
        await rig.start(5)
        rigs.append(rig)
    for rig, car in zip(rigs, cars):
        if car is not None:
            rig.plug(car)
    await clock.run(15)
    rts = {f"st{i}": FakeRuntime(rig, f"st{i}") for i, rig in enumerate(rigs)}
    config = SiteConfig(
        grid_entities=GRID,
        members=tuple(MemberConfig(entry_id=f"st{i}", priority=1) for i in range(len(rigs))),
        **{"fuse_limit_a": 25.0, **cfg},
    )
    site = Site(hass, clock, rigs, None, rts)
    site.coord = SiteCoordinator(
        hass, "site1", config, clock=clock, utcnow=site.wall, resolve=rts.get, sleep=clock.sleep,
        entry_exists=lambda entry_id: entry_id not in site.deleted,
    )
    return site


def car3() -> Car:
    return Car(phases=3, min_a=6.0, factor=1.0)


def run(main):
    async def outer(R):
        hass = await make_hass()
        try:
            await main(R, hass)
        finally:
            await stop(hass)

    go(outer)


# --- attach -------------------------------------------------------------------------------


def test_member_options_without_automatic_features():
    opts = CommandOptions(auto_start=True, resume_after_power_loss=True, schedule=Schedule(enabled=True))
    out = no_automatic_features(opts)
    assert not out.auto_start and not out.resume_after_power_loss and not out.schedule.enabled
    assert opts.auto_start  # the entry's own options are untouched


def test_attach_filters_automatic_features_and_new_member_is_off():
    async def main(R, hass):
        site = await make_site(R, hass, options=CommandOptions(auto_start=True))
        await site.cycles(3)
        rt = site.rts["st0"]
        assert rt.site_options_filter is not None
        assert rt.commander.options.auto_start is False
        site.rigs[0].plug(car3())  # plugged in once the site runs: no auto start of the station's own
        await site.cycles(3)
        m = site.coord.members[0]
        assert m.mode == "off" and m.loadpoint is not None
        assert site.writes(0) == [] and site.writes(1) == []  # nothing starts by surprise
        assert site.coord.result.meter_state is MeterState.OK

    run(main)


# --- DLM ----------------------------------------------------------------------------------


def test_dlm_cap_applied_at_once_on_overload_both_members():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        for m in site.coord.members:
            m.set_mode("now")
        await site.cycles(12)
        assert all(r.st.snapshot.status == 4 for r in site.rigs), site.dump()
        # 2 x 12.5 A fit the 25 A fuse with no base load
        currents = [r.fake.currents()[0] for r in site.rigs]
        assert sum(currents) <= 25.0 + 0.01 and min(currents) >= 6.0
        n0, n1 = len(site.writes(0)), len(site.writes(1))
        # base load jumps by 10 A per phase: both stations are cut in the next cycle
        site.base = [10.0, 10.0, 10.0]
        await site.cycle(12)
        new = [site.writes(0)[n0:], site.writes(1)[n1:]]
        assert all(any(reg == 91 for reg, _v in w) for w in new), new
        assert all(not any(w == (95, 2) for w in ws) for ws in new)
        await site.cycles(3)
        assert max(site.grid()) <= 25.0 + 0.3, site.grid()
        assert {w["origin"] for r in site.rigs for w in r.st.diagnostics()["write_log"]} == {ORIGIN_SITE}

    run(main)


def test_dlm_overload_below_minimum_disables_lowest_priority():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        site.coord.members[0].cfg = MemberConfig("st0", priority=1)
        site.coord.members[1].cfg = MemberConfig("st1", priority=2)
        for m in site.coord.members:
            m.set_mode("now")
        await site.cycles(12)
        site.base = [15.0, 15.0, 15.0]  # 10 A left: only one minimum fits
        await site.cycles(4)
        assert (95, 2) in site.writes(1) and (95, 2) not in site.writes(0)
        assert site.coord.members[1].allocation.allowed_a == 0.0

    run(main)


def test_meter_stale_runs_failure_action_stop():
    async def main(R, hass):
        site = await make_site(
            R, hass, cars=(car3(), car3()), meter_failure_action=MeterFailureAction.STOP
        )
        for m in site.coord.members:
            m.set_mode("now")
        await site.cycles(12)
        assert all(r.st.snapshot.status == 4 for r in site.rigs)
        site.meter_live = False  # the sensors stop updating (last_reported ages)
        await site.cycles(3)
        assert site.coord.meter_error is None  # 30 s old: still within 3 x the meter period
        assert site.coord.result.meter_state is MeterState.NOT_NEW  # read, not new: no increase
        assert (95, 2) not in site.writes(0)
        await site.cycles(2)
        assert site.coord.meter_error is not None and "no update" in site.coord.meter_error
        assert site.coord.result.meter_state is MeterState.FAILED_STALE  # value older than 3 x period
        assert (95, 2) in site.writes(0) and (95, 2) in site.writes(1)

    run(main)


def test_meter_unavailable_is_an_error():
    async def main(R, hass):
        site = await make_site(R, hass)
        await site.cycle()
        assert site.coord.reading is not None and not site.coord.reading.error
        site.meter_live = False
        hass.states.async_set(GRID[1], "unavailable", timestamp=site.wall().timestamp())
        await site.coord.async_cycle()
        assert site.coord.reading.error and "unavailable" in site.coord.meter_error

    run(main)


def test_station_offline_reserved_not_commanded():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        for m in site.coord.members:
            m.set_mode("now")
        await site.cycles(12)
        lost = site.rigs[1]
        lost.fake.kill_sessions()
        lost.fake.host_down = True
        for _ in range(30):
            await site.cycle()
            if lost.st.snapshot.link != "connected":
                break
        assert lost.st.snapshot.link != "connected"
        before = len(lost.fake.writes)
        site.base = [8.0, 8.0, 8.0]
        await site.cycles(10)
        alloc = site.coord.members[1].allocation
        assert alloc.allowed_a is None and alloc.reason is Reason.RESERVED_OFFLINE
        assert len(lost.fake.writes) == before
        # the reservation is taken from the other station
        assert site.coord.members[0].allocation.allowed_a <= 25.0 - 8.0 - 6.0 + 0.01

    run(main)


# --- PV -----------------------------------------------------------------------------------


def test_pv_starts_after_enable_delay_and_stops_after_disable_delay():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        site.coord.members[0].set_mode("pv")
        site.base = [2.0, 2.0, 2.0]  # import: the car connects without surplus (evcc starts at once on connect)
        await site.cycles(4)
        assert (95, 1) not in site.writes(0)
        site.base = [-12.0, -12.0, -12.0]  # 8.3 kW export
        await site.cycle()
        started_at = site.clock()
        while (95, 1) not in site.writes(0) and site.clock() - started_at < 200:
            await site.cycle()
        assert (95, 1) in site.writes(0)
        waited = site.clock() - started_at
        # evcc enable delay 1 min, + the evcc loop (30 s) round-robin of 2 loadpoints (60 s each)
        assert 60 <= waited <= 130, waited
        await site.cycles(6)
        assert site.rigs[0].st.snapshot.status == 4
        # surplus gone: import of 5 A per phase on top of the charge
        site.base = [5.0, 5.0, 5.0]
        gone_at = site.clock()
        while (95, 2) not in site.writes(0) and site.clock() - gone_at < 400:
            await site.cycle()
        assert (95, 2) in site.writes(0)
        assert 180 <= site.clock() - gone_at <= 250, site.clock() - gone_at  # evcc disable delay 3 min

    run(main)


# --- stop ---------------------------------------------------------------------------------


def test_stop_leaves_members_at_minimum_no_stop_no_start():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        stopped = []
        site.coord.on_stopped = stopped.append
        site.coord.members[0].set_mode("now")
        await site.cycles(12)
        assert site.rigs[0].st.snapshot.status == 4 and site.rigs[1].st.snapshot.status == 2
        n0, n1 = len(site.writes(0)), len(site.writes(1))
        task = __import__("asyncio").ensure_future(site.coord.async_stop())
        await site.clock.run(30)
        await task
        assert stopped == ["unloaded"]
        assert site.writes(0)[n0:] == [(91, 60)]
        assert site.writes(1)[n1:] == []  # not charging: nothing, no start
        assert all(rt.site_options_filter is None for rt in site.rts.values())

    run(main)


def test_crash_leaves_members_at_minimum_and_reports():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        stopped = []
        site.coord.on_stopped = stopped.append
        site.coord.members[0].set_mode("now")
        await site.cycles(12)
        n0 = len(site.writes(0))

        async def broken():
            raise RuntimeError("bug")

        site.coord.async_cycle = broken
        site.set_meter()
        site.coord.running = True
        task = __import__("asyncio").ensure_future(site.coord._run())
        await site.clock.run(30)
        await task
        assert stopped == ["crashed"] and site.coord.running is False
        assert site.writes(0)[n0:] == [(91, 60)]
        # a crash is not a release: the station's own automatic
        # features stay off and every member is held at its minimum by the command layer
        for rt in site.rts.values():
            assert rt.site_options_filter is not None
            assert rt.commander.site_member is True
            assert rt.commander.site_current == 60
            assert rt.commander.options.auto_start is False
        # the user unloads/reloads the site: now the members are released
        await site.coord.async_stop()
        for rt in site.rts.values():
            assert rt.site_options_filter is None and rt.commander.site_member is False
            assert rt.commander.site_current is None

    run(main)


def test_dlm_cap_increase_within_dead_band_writes_nothing():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        site.coord.members[0].set_mode("now")
        site.base = [11.15, 11.15, 11.15]  # ~13.8 A left
        await site.cycles(12)
        n = len(site.writes(0))
        site.base = [11.0, 11.0, 11.0]  # the cap rises by 0.1-0.2 A
        await site.cycles(4)
        assert site.writes(0)[n:] == []

    run(main)


def test_l1_any_decrease_is_enforced_exactly():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        site.coord.members[0].set_mode("now")
        site.base = [8.0, 8.0, 8.0]  # 17 A left: the car is at its 16 A
        await site.cycles(12)
        assert site.rigs[0].fake.setpoint == 160
        site.base = [9.15, 9.15, 9.15]  # the cap falls by 0.1-0.2 A
        await site.cycles(4)
        allowed = site.coord.members[0].allocation.allowed_a
        assert allowed < 16.0 and site.rigs[0].fake.setpoint == round(allowed * 10), (allowed, site.writes(0))

    run(main)


def test_phase_auto_only_with_the_phase_switch_option():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        await site.cycles(2)
        m = site.coord.members[0]
        assert m.can_auto and m.phase_option == "auto"  # evcc default for a 1p/3p charger: automatic
        await m.async_select_phases("3")
        assert m.loadpoint.phases_configured == 3 and m.settings["phases"] == 3
        await m.async_select_phases("auto")
        assert m.loadpoint.phases_configured == 0

    run(main)


def test_phase_select_writes_and_fixes_loadpoint_phases():
    """No "phase switching while charging" option any more: the loadpoint has phase switching once the class is known."""

    async def main(R, hass):
        site = await make_site(R, hass)
        await site.cycles(2)
        m = site.coord.members[0]
        assert m.loadpoint.has_phase_switching() and m.can_auto
        await m.async_select_phases("1")  # the loadpoint's configured phases (evcc SetPhasesConfigured)
        assert m.loadpoint.phases_configured == 1
        await site.cycles(2)
        assert (200, 1) not in site.writes(0)  # evcc: no car -> setLimit(0), no phase scaling
        site.rigs[0].plug(car3())
        await site.cycles(8)
        assert (200, 1) in site.writes(0)  # connected: scalePhasesRequired -> Phases1p3p
        assert m.loadpoint.get_phases() == 1

    run(main)


# ---.11 ---------------------------------------


def test_h1_pnc_on_below_minimum_is_set_to_its_minimum():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        m = site.coord.members[0]
        m.set_mode("now")
        await site.cycles(12)
        assert site.rigs[0].st.snapshot.status == 4
        site.rigs[0].fake.mode = 0  # Plug & Charge switched on while charging: a stop is refused
        await site.cycles(2)
        n = len(site.writes(0))
        site.base = [21.0, 21.0, 21.0]
        await site.cycles(6)
        assert (91, 60) in site.writes(0)[n:], "stop refused (P&C): it must at least be at its minimum"

    run(main)


def test_h1_minimum_first_then_stop_when_the_stop_works():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        site.coord.members[0].set_mode("now")
        await site.cycles(12)
        n = len(site.writes(0))
        site.base = [21.0, 21.0, 21.0]
        await site.cycles(3)
        new = site.writes(0)[n:]
        assert (91, 60) in new and (95, 2) in new and new.index((91, 60)) < new.index((95, 2)), new

    run(main)


async def _stop_site(site):
    task = asyncio.ensure_future(site.coord.async_stop())
    await site.clock.run(31)
    await task


def test_h3_unload_with_deferred_increase_leaves_minimum():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        m = site.coord.members[0]
        m.set_max_current(6.0)
        m.set_mode("now")
        rig = site.rigs[0]
        for _ in range(60):
            await site.cycle(1)
            if (95, 1) in rig.writes():
                break
        m.set_max_current(16.0)
        await site.cycle(1)  # an increase deferred by or still queued
        await _stop_site(site)
        await site.clock.run(30)
        assert rig.fake.setpoint <= 60, f"station at {rig.fake.setpoint / 10} A after the site stopped"

    run(main)


def test_h3_unload_after_settled_increase_leaves_minimum():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        m = site.coord.members[0]
        m.set_max_current(6.0)
        m.set_mode("now")
        rig = site.rigs[0]
        for _ in range(40):
            await site.cycle(2)
            if rig.st.snapshot.status == 4 and rig.st.snapshot.delivering:
                break
        m.set_max_current(16.0)
        await site.cycle(1)
        await site.cycle(1)
        await _stop_site(site)
        await site.clock.run(30)
        assert rig.fake.setpoint <= 60, f"station at {rig.fake.setpoint / 10} A after the site stopped"

    run(main)


def test_m1_start_near_the_limit_does_not_flap():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        site.base = [15.0, 15.0, 15.0]  # 10 A left on a 25 A fuse: one car at 6-10 A fits
        await site.cycles(2)
        site.coord.members[0].set_mode("now")
        await site.cycles(30)
        w = site.writes(0)
        assert w.count((95, 2)) == 0, f"start/stop flapping: {w.count((95, 1))} starts, {w.count((95, 2))} stops"
        assert site.rigs[0].st.snapshot.status == 4

    run(main)


def test_m2_adding_a_charging_member_keeps_the_charge():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        rig = site.rigs[0]
        await rig.call(rig.cmd.async_start())  # the user started the charge before the site existed
        await site.clock.run(30)
        assert rig.st.snapshot.status == 4
        n = len(rig.writes())
        await site.cycles(4)
        m = site.coord.members[0]
        assert m.mode == "now" and m.settings["mode"] == "now"
        new = rig.writes()[n:]
        assert (95, 2) not in new, "new member stopped a running charge"
        assert (91, 60) not in new, "attaching dropped a running charge to the minimum"
        assert rig.st.snapshot.status == 4

    run(main)


def test_m2_new_member_not_charging_starts_off_and_stored_mode_wins():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7", "A3C_2.7"), cars=(car3(), None))
        site.coord.members[0].settings["mode"] = "pv"
        site.coord.members[0].stored = True  # restored from the site Store
        await site.cycles(2)
        assert site.coord.members[0].mode == "pv"
        assert site.coord.members[1].mode == "off"

    run(main)


def test_m3_decrease_lost_after_a_link_glitch_is_sent_again():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        m = site.coord.members[0]
        m.set_mode("now")
        await site.cycles(12)
        rig = site.rigs[0]
        assert rig.st.snapshot.status == 4
        site.base = [12.0, 12.0, 12.0]
        await site.cycle(4)  # cut to ~13 A, 91 written now
        site.base = [15.0, 15.0, 15.0]
        await site.cycle(1)  # cut to ~10 A: deferred by (91 at most every 10 s)
        rig.fake.kill_sessions()
        rig.fake.host_down = True  # Wi-Fi glitch while the deferred write waits
        await site.clock.run(15)
        rig.fake.host_down = False
        for _ in range(12):
            await site.cycle()
        allowed = m.allocation.allowed_a
        assert rig.fake.setpoint <= allowed * 10 + 2, f"station still at {rig.fake.setpoint / 10} A, allowed {allowed}"

    run(main)


def test_m3_allocator_gets_the_read_back_setpoint_when_it_differs():
    async def main(R, hass):
        site = await make_site(R, hass, profiles=("A3C_2.7",), cars=(car3(),))
        m = site.coord.members[0]
        m.set_mode("now")
        await site.cycles(12)
        rig = site.rigs[0]
        rig.st.desired["current"] = 80  # the command layer believes 8 A; the station holds 16 A
        inp = site.coord._station_input(m, site.clock())
        assert inp.setpoint_a == rig.st.snapshot.setpoint_a == 16.0

    run(main)


def test_md2_unloaded_station_entry_keeps_its_reservation():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        for m in site.coord.members:
            m.set_mode("now")
        await site.cycles(12)
        # the station entry st1 is unloaded/reloaded in HA (runtime gone); the station keeps charging
        site.rts.pop("st1")
        await site.cycles(2)
        a = site.coord.members[1].allocation
        assert a.allowed_a is None and a.reason is Reason.RESERVED_OFFLINE
        assert max(a.booked_a) >= 6.0, f"a station that was charging reserves nothing once its entry is gone: {a}"

    run(main)


def test_md3_meter_value_age_is_its_last_report_not_our_read():
    async def main(R, hass):
        site = await make_site(R, hass)
        await site.cycle()
        assert site.coord.result.meter_state is MeterState.OK
        first = site.coord.reading.updated_at
        site.meter_live = False  # the sensors keep their value and their last_reported
        await site.cycle()
        assert abs(site.coord.reading.updated_at - first) < 1e-3  # now - age of last_reported
        assert not site.coord.reading.error
        assert site.coord.result.meter_state is MeterState.NOT_NEW  # read every cycle, no increase
        site.meter_live = True
        await site.cycle()
        assert site.coord.result.meter_state is MeterState.OK and site.coord.reading.updated_at > first

    run(main)


def test_site_member_refused_repair_removed_with_its_only_cause():
    """the repair "site_member_refused" had one cause - the 93 = 1
    gate refusing a site start under Plug & Charge. Nothing
    raises that refusal any more, so the repair, its hook and its texts are
    gone; under Plug & Charge the site's 95 = 1 goes out."""
    import inspect

    from gnlt_charger.site import coordinator as co
    from gnlt_charger.site import ha as site_ha
    from modbus_ha import translations

    assert not hasattr(co, "USER_FIXABLE_REFUSALS") and not hasattr(site_ha, "ISSUE_MEMBER_REFUSED")
    assert "on_member_problem" not in inspect.signature(co.SiteCoordinator).parameters
    for lang in ("en", "pl", "ru"):
        assert "site_member_refused" not in translations(lang)["issues"], lang

    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        site.rigs[0].fake.menu_set_mode(0)  # Plug & Charge switched on in the station menu
        await site.cycles(2)
        site.coord.members[0].set_mode("now")
        await site.cycles(4)
        m = site.coord.members[0]
        assert not hasattr(m, "write_problem")
        assert (95, 1) in site.writes(0)

    run(main)


def test_l2_hardware_max_passed_to_the_allocator():
    from gnlt_charger.modbus import commands as mc
    from gnlt_charger.modbus import const as MC
    from gnlt_charger.site import coordinator as co
    from gnlt_charger.site.dlm import DlmConfig

    # one definition each
    assert not hasattr(co, "_STATION_FIELDS")  # StationInput always has hardware_max_a now
    assert mc.ORIGIN_SITE is MC.ORIGIN_SITE and MC.SITE_ORIGINS == frozenset({MC.ORIGIN_SITE})
    assert co.DEADBAND_A == DlmConfig.__dataclass_fields__["overdraw_tolerance_a"].default

    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        await site.cycles(2)
        m = site.coord.members[0]
        inp = site.coord._station_input(m, site.clock())
        assert inp.hardware_max_a == site.rigs[0].st.current_range_tenths()[1] / 10 == 16.0
        site.rts.pop("st1")  # also for an unloaded member
        await site.cycle()
        assert site.coord._station_input(site.coord.members[1], site.clock()).hardware_max_a == 16.0

    run(main)


def test_l3_rebuilding_the_site_clears_queued_update_requests():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        st1 = site.rts.pop("st1")  # the second station entry loads later
        await site.cycles(2)
        lp0 = site.coord.members[0].loadpoint
        old = site.coord.site
        lp0.set_mode("now")  # queued in the old site's update queue
        assert lp0._lp_queued
        site.rts["st1"] = st1  # its loadpoint is built: the site is rebuilt in this cycle
        await site.cycle()
        assert site.coord.site is not old
        assert not lp0._lp_queued
        lp0.set_mode("off")
        assert lp0._lp_queued and lp0.lp_chan is site.coord.site.lp_update_chan

    run(main)


def test_l6_settings_saved_at_unload():
    from modbus_cmd_rig import MemoryStore

    async def main(R, hass):
        site = await make_site(R, hass)
        store = MemoryStore()  # async_delay_save does nothing: only a flush saves
        site.coord._store = store
        site.coord.members[0].set_mode("pv")
        site.coord.members[1].set_max_current(10.0)
        assert store.data is None
        await _stop_site(site)
        assert store.data["members"]["st0"]["mode"] == "pv"
        assert store.data["members"]["st1"]["max_current"] == 10.0

    run(main)


def test_c6_circuit_power_follows_the_allowed_current_for_phase_decisions():
    from gnlt_charger.site.clock import is_zero

    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), None))
        await site.cycles(2)
        m = site.coord.members[0]
        lp = m.loadpoint
        assert lp.has_phase_switching()
        lp.enabled, lp.phases, lp.phases_configured = True, 1, 0
        m.circuit.allowed = 7.0  # DLM allows 7 A per phase on the station's phases
        site.coord.spare, m.spare_granted = None, 0.0  # no spare headroom this cycle
        # evcc circuitAllowsPhases: 3p at 6 A needs 4140 W; the DLM allows 7 A x 1p x 230 V = 1610 W
        assert lp.circuit_allows_phases(1, 6.0) is True
        assert lp.circuit_allows_phases(3, 6.0) is False
        assert m.circuit.validate_power(0.0, 5000.0) == 7.0 * 1 * 230.0
        assert m.circuit.get_max_power() == 7.0 * 1 * 230.0
        # evcc pvScalePhases: big surplus on 1p, but the DLM cap forbids 3p -> no scale-up timer
        lp.charge_power = 1610.0
        lp.reset_phase_timer()
        assert lp.pv_scale_phases(-6000.0, 6.0, 16.0, True) == 0
        assert is_zero(lp.phase_timer)
        # allowed 0: a power limit of (almost) zero, never "unlimited"
        m.circuit.allowed = 0.0
        assert 0 < m.circuit.get_max_power() < 1.0

    run(main)


def test_c13_evcc_tick_every_30_s_dlm_every_meter_period():
    from gnlt_charger.site import coordinator as co

    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        await site.cycles(2)  # the loadpoints are built and updated once
        ticks = []
        allocations = []
        real_tick = site.coord.site.tick
        site.coord.site.tick = lambda: (ticks.append(site.clock()), real_tick())[1]
        real_allocate = site.coord.allocator.allocate
        site.coord.allocator.allocate = lambda *a: (allocations.append(site.clock()), real_allocate(*a))[1]
        await site.cycles(9)  # 90 s
        assert co.EVCC_INTERVAL_S == 30.0
        assert len(allocations) == 9
        assert len(ticks) == 3, ticks
        assert all(b - a >= 30.0 - 1e-6 for a, b in zip(ticks, ticks[1:]))

    run(main)


def test_d_release_restores_the_users_current_and_leaves_the_minimum():
    async def main(R, hass):
        clock_rig_desired = 120  # the user's value before the station joined the site
        site = await make_site(R, hass, cars=(car3(), None))
        rig = site.rigs[0]
        rig.st.desired["current"] = clock_rig_desired
        await site.cycle()
        site.coord.members[0].set_mode("now")
        await site.cycles(12)
        assert rig.st.snapshot.status == 4
        assert rig.st.desired["current"] == clock_rig_desired  # site writes keep the user's value
        assert rig.cmd.site_current == rig.fake.setpoint
        await _stop_site(site)
        assert rig.fake.setpoint == 60  # left at its minimum (no stop)
        assert rig.cmd.site_current is None  # released: the user's value controls again
        assert (95, 2) not in rig.writes()
        assert rig.st.desired["current"] == clock_rig_desired  # the user's value is back

    run(main)


def test_l4_member_whose_entry_was_deleted_is_dropped_from_the_reservation():
    async def main(R, hass):
        site = await make_site(R, hass, cars=(car3(), car3()))
        removed = []
        site.coord.on_member_removed = removed.append
        for m in site.coord.members:
            m.set_mode("now")
        await site.cycles(12)
        site.rts.pop("st1")
        site.deleted.add("st1")  # the user deleted the station's entry
        site.rigs[1].fake.unplug()  # and that station no longer draws
        await site.cycles(3)
        assert site.coord.members[1].allocation is None
        assert "st1" not in site.coord.result.stations
        assert removed == ["st1"]
        # the remaining member gets the whole fuse again
        assert site.coord.members[0].allocation.allowed_a >= 16.0 - 1e-6

    run(main)




def test_phase_refusals_are_translated_not_english_value_errors():
    """Step 2: a refusal reached a Polish HA in English - the
    member raised a plain ValueError ("this station cannot switch phases
    automatically", or the loadpoint's "cannot configure more phases...")
    which run_command passes through untranslated. Every refusal is a
    translated key now (en/pl/ru)."""
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    from gnlt_charger.modbus.entities import run_command
    from gnlt_charger.modbus.station import WriteRefused
    from gnlt_charger.site import api
    from modbus_ha import translations

    async def main(R, hass):
        site = await make_site(R, hass)
        await site.cycles(2)
        m = site.coord.members[0]
        m.adapter.capabilities = m.adapter.capabilities - {api.PhaseSwitcher}  # cannot switch on its own
        assert not m.can_auto
        with pytest.raises(WriteRefused) as err:
            await m.async_select_phases("auto")
        with pytest.raises(HomeAssistantError) as ha_err:
            await run_command(m.async_select_phases("auto"))
        assert ha_err.value.translation_key == err.value.key
        m.adapter.capabilities = m.adapter.capabilities | {api.PhaseSwitcher}
        m.loadpoint.get_charger_physical_phases = lambda: 1  # the loadpoint refuses 3 > 1
        with pytest.raises(WriteRefused) as err3:
            await m.async_select_phases("3")
        for lang in ("en", "pl", "ru"):
            exc = translations(lang)["exceptions"]
            assert err.value.key in exc and err3.value.key in exc, lang

    run(main)
