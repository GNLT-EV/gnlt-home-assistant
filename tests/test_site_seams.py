"""Seams between the site layer and the command/station layers (probes test_zz_probe_seams.py / test_zz_probe2.py made branch tests)."""

from gnlt_charger.modbus.commands import CommandOptions
from gnlt_charger.site.coordinator import no_automatic_features
from gnlt_charger.site.gnlt_charger_adapter import GnltChargerAdapter
from modbus_cmd_rig import Car, MemoryStore, go


def _restored(site: dict) -> dict:
    return {
        "control_active": True,
        "desired": {"current": 160},  # the user's value (number entity, persisted)
        "learned": {"decimal_current": "yes"},
        "commands": {"site": site},
    }


def test_h1_ha_restart_keeps_the_site_current_persisted():
    """Probe 1: HA restarts while the site held a charging member
    at 8.4 A; the user's desired current is 16 A. The membership and the site's
    current are in the Commander's store section."""

    async def main(R):
        rig = R(restored=_restored({"member": True, "current": 84}))
        rig.fake.setpoint = 84
        rig.plug()
        rig.fake.external_start()
        await rig.start(30)
        assert rig.cmd.site_member is True and rig.cmd.site_current == 84
        assert (91, 160) not in rig.writes(), "restore raised the DLM-limited member to the user's 16 A"

    go(main)


def test_h1_member_without_site_current_restores_at_most_the_read_back():
    async def main(R):
        rig = R(restored=_restored({"member": True, "current": None}))
        rig.fake.setpoint = 84
        rig.plug()
        rig.fake.external_start()
        await rig.start(30)
        assert rig.cmd.site_current is None
        assert (91, 160) not in rig.writes()
        # starts and the other command-layer restores use the station minimum
        assert rig.cmd._start_current_target() == rig.st.current_range_tenths()[0]

    go(main)


def test_h1_membership_and_site_current_are_saved_and_cleared_on_release():
    async def main(R):
        store = MemoryStore()
        rig = await R(store=store).start()
        rig.cmd.set_site_member(True)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(8.4)
        await rig.call(ad.async_drain())
        await rig.st.async_save()
        assert store.data["commands"]["site"] == {"member": True, "current": 84}
        rig.cmd.set_site_member(False)  # deliberate release
        await rig.st.async_save()
        assert store.data["commands"]["site"] == {"member": False, "current": None}
        assert rig.cmd.site_current is None

    go(main)


def test_h1_non_member_restore_unchanged():
    async def main(R):
        rig = R(restored=_restored({"member": False, "current": None}))
        rig.fake.setpoint = 84
        rig.plug()
        rig.fake.external_start()
        await rig.start(30)
        assert (91, 160) in rig.writes()  # the user's value, as before

    go(main)


def test_m1_site_decrease_during_the_start_is_written_at_once():
    """Probe 2: a DLM decrease while the start verifies."""

    async def main(R):
        rig = await R().start()
        rig.plug(Car(ramp_s=20.0))
        await rig.run(10)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(16.0)
        ad.enable(True)
        await rig.call(ad.async_drain())
        t0 = rig.clock()
        await rig.run(3)
        ad.max_current_millis(8.0)  # DLM decrease while the start verifies
        await rig.call(ad.async_drain())
        seen = None
        for _ in range(120):
            await rig.run(1)
            if rig.fake.setpoint == 80:
                seen = rig.clock() - t0
                break
        assert seen is not None and seen < 12, f"decrease written after {seen} s"
        await rig.run(60)
        assert rig.fake.setpoint == 80, "the start's own 91 check wrote the old target back"

    go(main)


def test_m1_site_decrease_right_after_95_1_car_draws_at_once():
    """Probe test_zz_probe2: no current above the decrease."""

    async def main(R):
        rig = await R().start()
        rig.plug(Car())
        await rig.run(10)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(16.0)
        ad.enable(True)
        await rig.call(ad.async_drain())
        t0 = rig.clock()
        await rig.run(1)
        ad.max_current_millis(8.0)
        await rig.call(ad.async_drain())
        log = []
        for _ in range(60):
            await rig.run(0.5)
            log.append((round(rig.clock() - t0, 1), rig.fake.status, rig.fake.setpoint, round(max(rig.fake.currents()), 1)))
            if rig.fake.setpoint == 80:
                break
        over = [x for x in log if x[3] > 8.5]
        assert not over, over[:3]

    go(main)


def test_m2_whole_amp_phase_switch_restarts_at_the_current_of_step_3():
    """Probe 3: a DLM decrease during the stop/pause of a
    whole-amp phase switch; the restart must not use the current captured
    before the stop."""

    async def main(R):
        rig = await R("A3C_2.7", decimal="no").start()
        rig.fake.phases = 3
        rig.plug(Car(phases=3))
        await rig.run(10)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current(16)
        ad.enable(True)
        await rig.call(ad.async_drain())
        await rig.run(150)
        assert rig.fake.status == 4, rig.fake.status
        ad.phases1p3p(1)  # 3 -> 1 whole-amp: stop, pause, restart
        await rig.call(ad.async_drain())
        await rig.run(2)
        ad.max_current(8)  # DLM decrease during the sequence
        await rig.call(ad.async_drain())
        log = []
        for i in range(200):
            await rig.run(1)
            log.append((i, rig.fake.status, rig.fake.setpoint))
            if rig.fake.status == 4 and rig.fake.setpoint == 80:
                break
        drawing_above = [x for x in log if x[1] == 4 and x[2] > 80]
        assert not drawing_above, f"charged at {drawing_above[0][2]} for {len(drawing_above)} s after the 8 A decrease"
        assert rig.fake.status == 4 and rig.fake.setpoint == 80

    go(main)


def test_m4_attach_clears_pending_automatic_actions():
    async def main(R):
        rig = await R().start()
        cmd = rig.cmd
        cmd._resume_pending = True
        cmd.set_site_member(True)
        assert cmd._resume_pending is False

    go(main)


