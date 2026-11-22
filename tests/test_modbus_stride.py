"""Measured on a station: a block read of
n registers at a returns a, a+2, a+4... The probe reads one register per
request like evcc, so a stride-2 box gives every value at its own address.
The device hw_version is neutral text (no words in one language)."""

import asyncio

from gnlt_charger.modbus import entities as mb_entities
from gnlt_charger.modbus import runtime as mb_runtime
from modbus_fake import FakeStation, FakeTcpStation, SimClock
from modbus_ha import SERIAL, make_hass, modbus_entry, stop
from test_modbus_config_flow import through_address


def test_probe_on_stride_2_box_maps_every_register():
    async def main():
        # The fake answers block reads with stride 2 and garbage between registers (35a3e49).
        model = FakeStation("A3C_2.7", SERIAL, SimClock(realtime=True))
        assert model.q.stride == 2
        model.min_tenths = 70
        model.cable_max = 130
        model.plug()
        model.set_error(19)
        server = FakeTcpStation(model)
        await server.start()
        hass = await make_hass()
        try:
            flow, result = await through_address(hass, server.port)
            assert result.get("step_id") == "modbus_model", result
            assert flow._modbus["serial"] == SERIAL
            regs = flow._modbus["registers"]
            assert regs[32] == 160 and regs[34] == 70 and regs[36] == 130, regs  # max / min / cable
            assert regs[2] == 1 and regs[4] == 19, regs  # cable and error, one read each
            assert regs[93] == 1 and regs[91] == 160 and regs[200] == 3
            assert not {1, 3, 33, 35, 37, 39, 41} & set(regs), regs  # no filler (garbage) address kept
            schema = {str(k): k for k in result["data_schema"].schema}
            assert schema["preset"].default() == "3_11"
        finally:
            await server.stop()
            await stop(hass)

    asyncio.run(main())


