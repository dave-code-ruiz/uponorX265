"""Bug A: concurrent async_turn_off() calls must not lose each other's storage writes.

Before the fix, async_load_storage() replaced self._storage_data with a fresh
dict on every call, and async_turn_off() did load -> mutate -> save with no
locking. When several thermostats are turned off in the same service call,
those coroutines interleave and each save only carries its own key ("last
writer wins"), silently discarding every other room's memo.
"""

import asyncio

from tests.helpers import make_state_proxy, thermostat_data

THERMOSTATS = ["C1_T1", "C1_T2", "C1_T3", "C1_T4", "C1_T5", "C1_T6"]


async def test_concurrent_turn_off_keeps_every_thermostats_memo(hass):
    data = {}
    for t in THERMOSTATS:
        data.update(thermostat_data(t, setpoint_c=19.5))
    proxy = make_state_proxy(hass, data=data)

    # The test harness's mocked Store.async_save never actually suspends (no
    # executor read, no disk write), so without a forced yield point the six
    # turn_off coroutines below would just run to completion one after
    # another even with no lock at all — passing regardless of the fix. A
    # real Store does suspend (that's what lets them interleave in
    # production), so inject an equivalent yield point here to make this a
    # genuine regression test rather than an accidental pass.
    real_save = proxy._store.async_save

    async def yielding_save(payload):
        await asyncio.sleep(0)
        await real_save(payload)

    proxy._store.async_save = yielding_save

    # Mirrors HA turning off several thermostats from one service call: the
    # coroutines run concurrently, not sequentially.
    await asyncio.gather(*(proxy.async_turn_off(t) for t in THERMOSTATS))

    await proxy.async_load_storage()
    for t in THERMOSTATS:
        assert proxy._storage_data.get(t) == 19.5, (
            f"{t}'s setpoint memo was lost to a concurrent write from another thermostat"
        )
