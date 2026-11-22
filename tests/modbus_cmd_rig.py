"""Test rig of the command layer: the station, its own poll loop, the
energy layer and a ``Commander`` on one virtual clock.

``VClock`` keeps the fake's rules (a transaction moves the clock by its
latency / timeout) but turns ``sleep`` into a timer: concurrent tasks (the
poll loop, a sequence, a deferred current write) wake in time order, and
``run(seconds)`` drives them like a real event loop would - minutes pass
instantly.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from datetime import UTC, datetime, timedelta

from gnlt_charger.modbus import const as C
from gnlt_charger.modbus.commands import Commander, CommandOptions
from gnlt_charger.modbus.energy import EnergyMeter, StationEnergy
from gnlt_charger.modbus.station import Station
from modbus_fake import Car, FakeClient, FakeStation, SimClock

SERIAL = "301234567890"
WALL0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SETTLE_YIELDS = 60


class VClock(SimClock):
    def __init__(self, start: float = 1000.0) -> None:
        super().__init__(start)
        self._heap: list = []
        self._n = itertools.count()

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self() + seconds, next(self._n), fut))
        await fut

    async def settle(self) -> None:
        for _ in range(SETTLE_YIELDS):
            await asyncio.sleep(0)

    async def run(self, seconds: float) -> None:
        end = self() + seconds
        while True:
            await self.settle()
            while self._heap and self._heap[0][2].done():
                heapq.heappop(self._heap)
            if self._heap and self._heap[0][0] <= end:
                t, _n, fut = heapq.heappop(self._heap)
                if t > self():
                    self._offset = t
                fut.set_result(None)
                continue
            if end > self():
                self._offset = end
            await self.settle()
            return


class MemoryStore:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0
        self.save_times: list[float] = []
        self.clock = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data
        self.saves += 1
        if self.clock is not None:
            self.save_times.append(self.clock())

    async def async_remove(self):
        self.data = None

    def async_delay_save(self, func, delay):
        pass


class Rig:
    def __init__(
        self,
        profile: str = "A3C_2.7",
        *,
        options: CommandOptions | None = None,
        energy: bool = True,
        realistic: bool = False,
        restored: dict | None = None,
        store: MemoryStore | None = None,
        clock: VClock | None = None,
        fake: FakeStation | None = None,
        decimal: str | None = "yes",
        **quirks,
    ) -> None:
        """``decimal``: the firmware class the station starts with -
        "yes" - as if the 6.1 A probe of
        the start-up had run; None leaves it open (``start_like_setup`` runs
        the probe like the entry's setup)."""
        self.clock = clock or VClock()
        self.fake = fake or FakeStation(profile, SERIAL, self.clock, **quirks)
        self.client = FakeClient(self.fake, realistic=realistic)
        self.store = store
        if store is not None:
            store.clock = self.clock
        self.st = Station(
            self.client, SERIAL, clock=self.clock, sleep=self.clock.sleep, restored=restored,
            store=store,
        )
        if decimal is not None:
            self.st.learned.set("decimal_current", decimal)
        self.meter = self.se = None
        if energy:
            self.meter = EnergyMeter(wall=self.wall)
            self.se = StationEnergy(self.st, self.meter)
        self.options = options or CommandOptions()
        self.cmd = Commander(self.st, energy=self.se, options=self.options, wall=self.wall, sleep=self.clock.sleep)
        self.notes: list[tuple[str, dict]] = []
        self.cmd.subscribe_notifications(lambda kind, data: self.notes.append((kind, data)))
        self.events: list[tuple[str, dict]] = []
        self.st.subscribe_events(lambda name, data: self.events.append((name, data)))

    def wall(self) -> datetime:
        return WALL0 + timedelta(seconds=self.clock() - 1000.0)

    async def start(self, seconds: float = 20.0) -> Rig:
        if self.store is not None:
            await self.st.async_load()
        await self.st.async_start()
        await self.run(seconds)
        return self

    async def start_like_setup(self, seconds: float = 20.0) -> Rig:
        """The entry's setup: first contact with evcc's initialize (the 6.1 A
        probe), reg 89 once, then polling."""
        if self.store is not None:
            await self.st.async_load()
        await self.st.async_first_contact(initialize=self.cmd.async_initialize)
        await self.st.async_read_comm_timeout()
        return await self.start(seconds)

    async def run(self, seconds: float) -> None:
        await self.clock.run(seconds)

    async def call(self, coro, budget: float = 120.0, step: float = 0.25):
        task = asyncio.ensure_future(coro)
        spent = 0.0
        while not task.done() and spent < budget:
            await self.clock.run(step)
            spent += step
        assert task.done(), "call did not finish in the budget"
        return task.result()

    async def idle(self, budget: float = 900.0) -> None:
        spent = 0.0
        while self.cmd._tasks and spent < budget:
            await self.clock.run(1.0)
            spent += 1.0
        assert not self.cmd._tasks, "command tasks still running"

    async def close(self) -> None:
        await self.cmd.async_close()
        task = self.st._task
        self.st._stopped = True
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.client.close()

    def writes(self) -> list[tuple[int, int]]:
        return [(reg, value) for _t, reg, value in self.fake.writes]

    def kinds(self) -> list[str]:
        return [k for k, _d in self.notes]

    def plug(self, car: Car | None = None) -> None:
        self.fake.plug(car or Car())


def go(main, *args) -> None:
    """Run ``async main(make_rig, *args)``; every rig is closed afterwards."""
    rigs: list[Rig] = []

    def make_rig(*a, **kw) -> Rig:
        rig = Rig(*a, **kw)
        rigs.append(rig)
        return rig

    async def runner():
        try:
            await main(make_rig, *args)
        finally:
            for rig in rigs:
                await rig.close()

    asyncio.run(runner())


__all__ = ["C", "Car", "MemoryStore", "Rig", "SERIAL", "VClock", "WALL0", "go"]
