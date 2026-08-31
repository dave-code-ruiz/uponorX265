"""Finding 05: devices the gateway no longer reports could not be removed.

`async_remove_config_entry_device` was absent, so the config entry reported
`supports_remove_device: false`. Home Assistant only renders the Delete action
on a device page when an integration opts in, and the same gate makes
programmatic config-entry device removal refuse - which is what turned a
stale gateway device into a hand-edit of the registry.

Removable means "no identifier of this device matches anything the entry
currently knows about". Cached controllers and thermostats count as known, so
a unit absent from one transient JNAP response is not offered for deletion.
"""

from homeassistant.helpers import device_registry as dr

from custom_components.uponorx265 import async_remove_config_entry_device
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
GATEWAY_ID = "AABBCCDDEEFF"
CONTROLLER_ID = "419524869"
THERMOSTAT_ID = "285512345"
CACHED_THERMOSTAT_ID = "285567890"


def _proxy(hass):
    """A proxy reporting one controller, one active thermostat, one cached-only."""
    proxy = make_state_proxy(
        hass,
        data={
            "sys_controller_1_presence": "1",
            "controller1_id": CONTROLLER_ID,
            "C1_thermostat_1_presence": "1",
            "C1_thermostat1_id": THERMOSTAT_ID,
        },
        unique_id=UNIQUE_ID,
    )
    proxy._gateway_id = GATEWAY_ID
    # C1_T2 is known only from storage - it dropped out of this poll.
    proxy._storage_metadata = {
        "thermostats": ["C1_T1", "C1_T2"],
        "ids": {"C1_T1": THERMOSTAT_ID, "C1_T2": CACHED_THERMOSTAT_ID},
        "controllers": ["C1"],
        "controller_ids": {"C1": CONTROLLER_ID},
    }
    return proxy


def _device(hass, proxy, identifier):
    dev_reg = dr.async_get(hass)
    return dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, identifier)},
    )


async def _removable(hass, proxy, identifier, loaded=True):
    if loaded:
        hass.data[UNIQUE_ID] = {"state_proxy": proxy, "thermostats": ["C1_T1"]}
    device = _device(hass, proxy, identifier)
    return await async_remove_config_entry_device(hass, proxy._config_entry, device)


async def test_stale_gateway_device_is_removable(hass):
    proxy = _proxy(hass)
    # The pre-MAC identifier scheme: host IP with the dots stripped.
    assert await _removable(hass, proxy, "1921681182") is True


async def test_live_gateway_device_is_not_removable(hass):
    proxy = _proxy(hass)
    assert await _removable(hass, proxy, GATEWAY_ID) is False


async def test_live_controller_device_is_not_removable(hass):
    proxy = _proxy(hass)
    assert await _removable(hass, proxy, CONTROLLER_ID) is False


async def test_active_thermostat_device_is_not_removable(hass):
    proxy = _proxy(hass)
    assert await _removable(hass, proxy, THERMOSTAT_ID) is False


async def test_cached_thermostat_missing_from_this_poll_is_not_removable(hass):
    proxy = _proxy(hass)
    assert await _removable(hass, proxy, CACHED_THERMOSTAT_ID) is False, (
        "a thermostat absent from one JNAP response must not be offered for deletion"
    )


async def test_unknown_thermostat_device_is_removable(hass):
    proxy = _proxy(hass)
    assert await _removable(hass, proxy, "285599999") is True


async def test_nothing_is_removable_while_the_entry_is_unloaded(hass):
    proxy = _proxy(hass)
    # hass.data never populated: the live set is unknown, so refuse.
    assert await _removable(hass, proxy, "1921681182", loaded=False) is False


async def test_home_assistant_reports_the_integration_supports_device_removal(hass):
    """The registry gate itself: HA renders Delete only when this returns True."""
    from homeassistant.config_entries import support_remove_from_device

    from custom_components.uponorx265.const import DOMAIN

    assert await support_remove_from_device(hass, DOMAIN) is True
