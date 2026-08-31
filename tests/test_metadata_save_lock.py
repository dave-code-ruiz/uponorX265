"""The discovery-metadata save must take the same storage lock as the memo writes.

_async_persist_discovery_metadata() composes its payload from self._storage_data
and writes the whole file. It runs on every poll, concurrently with whatever
climate service calls are in flight. Without the storage lock it is a third
writer to the same file, so its (older) snapshot of _storage_data can land on
top of a turn_off memo that was written while its own async_save was suspended
- the same lost-update shape as bug A, just from the other side.
"""

import asyncio

from tests.helpers import make_state_proxy, thermostat_data

THERMOSTAT = "C1_T1"


def _discoverable_data():
    data = {
        "sys_controller_1_presence": "1",
        "C1_thermostat_1_presence": "1",
        "controller1_id": "419524869",
        "thermostat1_id": "285803812",
        "cust_C1_T1_name": "Master Bedroom",
        "cust_ip_device": "10.0.0.1",
    }
    data.update(thermostat_data(THERMOSTAT, setpoint_c=19.5))
    return data


async def test_metadata_refresh_does_not_clobber_a_concurrent_turn_off_memo(hass):
    proxy = make_state_proxy(hass, data=_discoverable_data())

    # Same yield injection as test_turn_off_storage_race: the mocked Store
    # never suspends, so without it the two coroutines below run to completion
    # one after the other and no interleaving is possible, with or without the
    # lock.
    real_save = proxy._store.async_save

    async def yielding_save(payload):
        await asyncio.sleep(0)
        await real_save(payload)

    proxy._store.async_save = yielding_save

    # A poll's metadata refresh racing a climate.turn_off, as happens on any
    # first poll after a rename/discovery change.
    await asyncio.gather(
        proxy._async_persist_discovery_metadata(),
        proxy.async_turn_off(THERMOSTAT),
    )

    await proxy.async_load_storage()
    assert proxy._storage_data.get(THERMOSTAT) == 19.5, (
        "the metadata refresh overwrote the file with a snapshot taken before "
        "the turn_off memo was written"
    )
    assert proxy._storage_metadata.get("rooms", {}).get(THERMOSTAT) == "Master Bedroom", (
        "discovery metadata was lost"
    )


async def test_metadata_refresh_completes_while_memo_writes_are_in_flight(hass):
    """Deadlock guard: the newly locked save must not be reachable from a caller
    that already holds the storage lock."""
    proxy = make_state_proxy(hass, data=_discoverable_data())

    await asyncio.wait_for(
        asyncio.gather(
            proxy.async_remember_setpoint(THERMOSTAT, 21.0),
            proxy._async_persist_discovery_metadata(),
            proxy.async_turn_off(THERMOSTAT),
        ),
        timeout=5,
    )

    await proxy.async_load_storage()
    assert proxy._storage_metadata.get("thermostats") == [THERMOSTAT]
