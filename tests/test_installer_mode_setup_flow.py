"""The customization carry-over has to survive real platform setup.

`_restore_installer_mode_customizations` runs after
`async_forward_entry_setups()` because the replacement rows do not exist until
their platform registers them - which relies on `async_add_entities()` having
finished registering by the time that await returns. The rest of the suite
stubs the forward out, so nothing there would notice if that stopped holding.

This runs the real forward against a mocked gateway and asserts on what the
entity registry looks like afterwards.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import async_setup_entry
from custom_components.uponorx265.const import CONF_INSTALLER_SETTINGS, DOMAIN

UNIQUE_ID = "uponorx265_test"
CONTROLLER_ID = "419524869"
THERMOSTAT_ID = "285805788"

# One controller, one thermostat with a floor probe, and the variables
# installer mode switches between read-only and writable.
LIVE_DATA = {
    "sys_controller_1_presence": "1",
    "C1_thermostat_1_presence": "1",
    "controller1_id": CONTROLLER_ID,
    "C1_thermostat1_id": THERMOSTAT_ID,
    "C1_T1_thermostat_type": "2",
    "C1_T1_hw_type": "7",
    "C1_controller_relays_config": "3",
    "sys_pump_management": "0",
    "C1_T1_bypass_enable": "0",
    "C1_T1_external_temperature": "808",
    "C1_T1_minimum_floor_setpoint": "680",
    "C1_T1_maximum_floor_setpoint": "860",
}

# (unique_id suffix, domain when installer mode is off, domain when it is on)
PAIRS = (
    (f"{UNIQUE_ID}_{CONTROLLER_ID}_relay_config", "sensor", "select"),
    (f"{UNIQUE_ID}_pump_management", "sensor", "select"),
    (f"{UNIQUE_ID}_{THERMOSTAT_ID}_bypass_enable", "binary_sensor", "switch"),
    (f"{UNIQUE_ID}_{THERMOSTAT_ID}_floor_min_limit", "sensor", "number"),
    (f"{UNIQUE_ID}_{THERMOSTAT_ID}_floor_max_limit", "sensor", "number"),
)


@pytest.fixture(autouse=True)
def _no_background_noise():
    """Keep the poll timer out of these tests."""
    with patch("custom_components.uponorx265.async_track_time_interval",
               return_value=lambda: None):
        yield


def _entry(hass, installer_settings):
    data = {CONF_HOST: "10.0.0.1", "name": "Uponor",
            CONF_INSTALLER_SETTINGS: installer_settings}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=data, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)
    return entry


def _gateway():
    """Patch in a mocked JNAP client for the duration of a setup or reload."""
    client = AsyncMock()
    client.get_data.return_value = dict(LIVE_DATA)
    client.get_device_info.return_value = {"deviceID": "AA:BB:CC:DD:EE:FF"}
    return patch("custom_components.uponorx265.UponorJnap", return_value=client)


async def _setup(hass, entry):
    """Set the entry up through HA, so platform setup really runs."""
    with _gateway():
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()


async def _set_installer_mode(hass, entry, value):
    """Flip the flag the way the options flow does, and let the reload finish."""
    with _gateway():
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_INSTALLER_SETTINGS: value}
        )
        await hass.async_block_till_done()


async def test_toggling_leaves_exactly_one_row_per_feature(hass):
    entry = _entry(hass, False)
    await _setup(hass, entry)

    reg = er.async_get(hass)
    for unique_id, read_only, writable in PAIRS:
        assert reg.async_get_entity_id(read_only, DOMAIN, unique_id) is not None, unique_id
        assert reg.async_get_entity_id(writable, DOMAIN, unique_id) is None, unique_id

    await _set_installer_mode(hass, entry, True)

    for unique_id, read_only, writable in PAIRS:
        assert reg.async_get_entity_id(writable, DOMAIN, unique_id) is not None, unique_id
        assert reg.async_get_entity_id(read_only, DOMAIN, unique_id) is None, (
            f"the read-only {read_only} twin of {unique_id} was left behind"
        )

    # And back, which is where the stranded rows used to push the returning
    # entity to a `..._2` entity_id.
    await _set_installer_mode(hass, entry, False)

    for unique_id, read_only, writable in PAIRS:
        assert reg.async_get_entity_id(read_only, DOMAIN, unique_id) is not None, unique_id
        assert reg.async_get_entity_id(writable, DOMAIN, unique_id) is None, unique_id


async def test_a_renamed_entity_keeps_its_name_and_id_across_the_toggle(hass):
    entry = _entry(hass, False)
    await _setup(hass, entry)

    reg = er.async_get(hass)
    unique_id, read_only, writable = PAIRS[2]
    reg.async_update_entity(
        reg.async_get_entity_id(read_only, DOMAIN, unique_id),
        new_entity_id=f"{read_only}.kitchen_bypass",
        name="Kitchen bypass",
        icon="mdi:pipe-valve",
    )

    await _set_installer_mode(hass, entry, True)

    moved = reg.async_get(reg.async_get_entity_id(writable, DOMAIN, unique_id))
    assert moved.entity_id == f"{writable}.kitchen_bypass"
    assert moved.name == "Kitchen bypass"
    assert moved.icon == "mdi:pipe-valve"
    assert hass.states.get(moved.entity_id) is not None, (
        "the restored entity_id has no state, so the rename outran platform setup"
    )
