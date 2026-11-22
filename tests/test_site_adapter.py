"""The evcc charger adapter of one GNLT ModBus station (site/gnlt_charger_adapter.py).

Contract of ``site/api.py``: explicit capabilities per profile and option,
status only A/B/C (anything else raises: the loadpoint skips the cycle),
writes queued in order through the Commander with the automatic origin
``site``, the A3B stop refusal raised at once.
"""

import pytest
from gnlt_charger.modbus import const as C

from gnlt_charger.modbus.commands import ORIGIN_SITE, CommandError, CommandOptions
from gnlt_charger.modbus.station import is_automatic
from gnlt_charger.site import api
from gnlt_charger.site.api import ChargeStatus
from gnlt_charger.site.gnlt_charger_adapter import GnltChargerAdapter
from modbus_cmd_rig import Car, go


def caps(adapter):
    return set(adapter.capabilities)


def origins(rig):
    return [w["origin"] for w in rig.st.diagnostics()["write_log"]]


def test_origin_site_is_automatic():
    assert ORIGIN_SITE == "site" and is_automatic(ORIGIN_SITE)


def test_capabilities_a3c_decimal_phase_switcher_without_option():
    async def main(R):
        rig = await R("A3C_2.7").start()
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        assert rig.st.phase_capability is True and rig.cmd.phase_select_available
        # no "phase switching while charging" option any more: automatic 1p/3p like evcc
        assert api.has_cap(ad, api.ChargerEx) and api.has_cap(ad, api.PhaseSwitcher)
        assert api.has_cap(ad, api.PhaseVoltages)

    go(main)


def test_capabilities_declare_phase_switcher_when_class_known():
    """PhaseSwitcher whenever the station has the phase capability, the
    profile allows a phase select and the firmware class is known - no
    option any more."""

    async def main(R):
        rig = await R("A3C_2.7").start()
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        assert caps(ad) == {
            api.ChargerEx, api.PhaseSwitcher, api.PhaseGetter, api.Meter, api.PhaseCurrents, api.PhaseVoltages,
        }

    go(main)


def test_capabilities_whole_amp_class_and_unknown_class_no_charger_ex():
    async def main(R):
        rig = await R("A3C_2.7", decimal="no").start()
        assert api.ChargerEx not in caps(GnltChargerAdapter(rig.st, rig.cmd))

    go(main)


def test_capabilities_a3b_like_any_station():
    """No A3B exception - a portable EVB whose reg 200 reads
    gets the phase capabilities like evcc's initialize gives them."""

    async def main(R):
        rig = await R("A3B_2.7").start()
        c = caps(GnltChargerAdapter(rig.st, rig.cmd))
        assert ({api.PhaseSwitcher, api.PhaseGetter} <= c) == (rig.st.phase_capability is True)
        assert {api.Meter, api.PhaseCurrents} <= c

    go(main)


def test_capabilities_one_phase_preset_no_phase_caps():
    async def main(R):
        rig = await R("A3C_2.7", options=CommandOptions(phases_available=False)).start()
        c = caps(GnltChargerAdapter(rig.st, rig.cmd))
        assert api.PhaseSwitcher not in c and api.PhaseGetter not in c

    go(main)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(1, ChargeStatus.A), (2, ChargeStatus.B), (3, ChargeStatus.B), (6, ChargeStatus.B), (4, ChargeStatus.C)],
)
def test_status_mapping(code, expected):
    async def main(R):
        rig = await R().start()
        rig.fake.force_status(code)
        await rig.run(15)
        assert rig.st.snapshot.status == code
        assert GnltChargerAdapter(rig.st, rig.cmd).status() == expected

    go(main)


@pytest.mark.parametrize("code", [5, 7, 9, 10, 31])
def test_status_other_codes_raise(code):
    async def main(R):
        rig = await R().start()
        rig.fake.force_status(code)
        await rig.run(15)
        assert rig.st.snapshot.status == code
        with pytest.raises(Exception):
            GnltChargerAdapter(rig.st, rig.cmd).status()

    go(main)


def test_meter_and_currents_from_snapshot():
    async def main(R):
        rig = await R().start()
        rig.plug(Car(phases=3))
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.idle()
        await rig.run(40)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        snap = rig.st.snapshot
        assert ad.current_power() == snap.power_w and ad.current_power() > 0
        assert ad.currents() == tuple(snap.currents)
        assert ad.enabled() is True and ad.status() == ChargeStatus.C
        assert ad.get_phases() == 3

    go(main)


def test_writes_go_in_order_through_commander_with_site_origin():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        assert ad.enabled() is False
        # what the loadpoint does on enable: current first, then enable (evcc setLimit)
        ad.max_current_millis(8.4)
        ad.enable(True)
        assert ad.enabled() is True  # the queued intent is reported until the station shows it
        await rig.call(ad.async_drain())
        await rig.idle()
        await rig.run(30)
        writes = rig.writes()
        assert writes.index((91, 84)) < writes.index((95, 1))
        assert set(origins(rig)) == {ORIGIN_SITE}
        assert rig.st.snapshot.status == 4 and ad.enabled() is True
        ad.enable(False)
        await rig.call(ad.async_drain())
        await rig.idle()
        assert (95, 2) in rig.writes()
        assert set(origins(rig)) == {ORIGIN_SITE}

    go(main)


def test_whole_amp_charger_gets_whole_amps():
    async def main(R):
        rig = await R(decimal="no").start()
        rig.plug()
        await rig.run(20)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current(9)
        await rig.call(ad.async_drain())
        assert (91, 90) in rig.writes()

    go(main)


def test_current_out_of_range_raises_at_once():
    async def main(R):
        rig = await R().start()
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        with pytest.raises(CommandError):
            ad.max_current_millis(40.0)
        assert ad.pending == 0

    go(main)


def test_refused_write_is_logged_not_raised_and_queue_goes_on():
    async def main(R):
        rig = await R().start()
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.enable(True)  # no car: the start is refused in the task
        await rig.call(ad.async_drain())
        assert ad.last_error == "no_car" and ad.pending == 0
        assert ad.enabled() is False

    go(main)


def test_cancel_pending_drops_queued_writes():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(7.0)
        ad.enable(True)
        ad.cancel_pending()
        await rig.run(30)
        assert (95, 1) not in rig.writes()

    go(main)


def test_md1_empty_session_stop_off_for_site_members():
    from gnlt_charger.site.coordinator import no_automatic_features

    assert no_automatic_features(CommandOptions()).empty_session_stop is False

    async def main(R):
        rig = await R(options=no_automatic_features(CommandOptions())).start()
        rig.plug(Car(asleep=True))  # the car does not take the start: status stays 2 (OCPP rule)
        await rig.run(20)
        await rig.call(rig.cmd.async_start())
        await rig.run(220)
        await rig.idle()
        assert rig.kinds().count("empty_session") == 1  # still reported
        assert (95, 2) not in rig.writes()  # but the site decides, not

    go(main)


def test_site_current_writes_keep_the_users_desired_current():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        rig.st.desired["current"] = 160  # the user's value (number entity, persisted)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(8.4)
        ad.enable(True)
        await rig.call(ad.async_drain())
        await rig.idle()
        await rig.run(30)
        assert rig.st.desired["current"] == 160
        assert rig.cmd.site_current == 84
        # the start sequence's own 91 is the site's value, never the user's 16 A
        assert (91, 160) not in rig.writes() and rig.fake.setpoint == 84
        ad.max_current_millis(7.0)
        await rig.call(ad.async_drain())
        await rig.run(10)
        assert rig.fake.setpoint == 70 and rig.st.desired["current"] == 160

    go(main)


def test_station_restore_after_power_loss_uses_the_site_current():
    async def main(R):
        rig = await R().start()
        rig.st.desired["current"] = 160  # the user's value
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(8.4)
        await rig.call(ad.async_drain())
        await rig.run(15)
        assert rig.fake.setpoint == 84 and rig.cmd.site_current == 84
        n = len(rig.writes())
        rig.fake.power_loss()  # the station restarts: restores 91
        await rig.run(200)
        restores = [w for w in rig.st.diagnostics()["write_log"] if w["origin"] == "restore"]
        assert restores, rig.writes()[n:]
        assert (91, 160) not in rig.writes()[n:], rig.writes()[n:]
        assert rig.fake.setpoint == 84
        # no site control: the user's value is restored again
        rig.cmd.site_current = None
        rig.fake.power_loss()
        await rig.run(200)
        assert rig.fake.setpoint == 160

    go(main)


def test_phase_switcher_whenever_reg_200_reads_also_with_the_class_unknown():
    """ / brief item 6: evcc declares the phase switcher when the
    200 read succeeds at initialize - not tied to the firmware class (the
    command layer still refuses a switch under current while it is
    unknown)."""

    async def main(R):
        rig = await R("A3C_2.7", decimal=None).start(3)
        rig.st.learned.set("decimal_current", "unknown")
        assert rig.st.phase_capability is True
        assert api.PhaseSwitcher in GnltChargerAdapter(rig.st, rig.cmd).compute_capabilities()

    go(main)


def test_l2_phases1p3p_refusal_raises_at_once():
    async def main(R):
        rig = await R("A3C_2.7").start()
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        rig.st.identity = C.IDENTITY_MISMATCH  # another station answers: the gate refuses
        with pytest.raises(api.NotAvailableError):
            ad.phases1p3p(1)
        assert ad.pending == 0

    go(main)


def test_l3_current_floored_to_0_1_a_never_rounded_up():
    async def main(R):
        rig = await R().start()
        rig.plug()
        await rig.run(20)
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        ad.max_current_millis(8.46)
        await rig.call(ad.async_drain())
        assert (91, 84) in rig.writes() and (91, 85) not in rig.writes()

    go(main)


def test_phase_voltages_declared_and_read_from_the_polled_registers():
    """Like the EM2GO driver the adapter gives the
    voltages L1..L3 (regs 109/111/113) so evcc core can count the phases."""

    async def main(R):
        rig = await R("A3C_2.7").start()
        ad = GnltChargerAdapter(rig.st, rig.cmd)
        assert api.PhaseVoltages in caps(ad)
        u = ad.voltages()
        assert len(u) == 3 and all(isinstance(x, float) for x in u)
        assert u == tuple(float(x) if x is not None else 0.0 for x in rig.st.snapshot.voltages)

    go(main)
