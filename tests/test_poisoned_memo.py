"""Bug B: a setpoint already at the off value must not be memorised as the restore target.

If something sets the setpoint to min_temp before calling turn_off, the old
code recorded min_temp as "the value to restore" — so turn_on would write
min_temp right back, the room would compute as still off, and it could never
be turned back on via hvac_mode.
"""

from tests.helpers import make_state_proxy, thermostat_data

THERMOSTAT = "C1_T1"


async def test_turn_off_does_not_memorise_the_off_value(hass):
    # Setpoint is already at min_temp (15.0) when turn_off is called.
    data = thermostat_data(THERMOSTAT, setpoint_c=15.0, min_c=15.0, max_c=25.0)
    proxy = make_state_proxy(hass, data=data)

    await proxy.async_turn_off(THERMOSTAT)

    await proxy.async_load_storage()
    assert THERMOSTAT not in proxy._storage_data, (
        "the off value (min_temp) must not be recorded as the restore target"
    )


async def test_turn_on_recovers_from_a_previously_poisoned_memo(hass):
    # Simulate a memo poisoned by the pre-fix bug: the store already holds
    # the off value as this room's "restore" target.
    data = thermostat_data(THERMOSTAT, setpoint_c=15.0, min_c=15.0, max_c=25.0)
    proxy = make_state_proxy(hass, data=data)
    proxy._storage_data[THERMOSTAT] = 15.0
    await proxy._store.async_save(proxy._compose_storage_payload())

    await proxy.async_turn_on(THERMOSTAT)

    sent_var, sent_raw = proxy._client.send_data.call_args[0][0].popitem()
    assert sent_var == f"{THERMOSTAT}_setpoint"
    off_raw = round(15.0 * 18 + 320)
    default_raw = round(20.0 * 18 + 320)  # DEFAULT_TEMP fallback
    assert sent_raw != off_raw, (
        "turn_on restored the poisoned off-value instead of falling back to a safe default"
    )
    assert sent_raw == default_raw
