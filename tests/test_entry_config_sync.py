"""Finding 03: setup wiped the registries whenever data and options differed.

`async_setup_entry` opened with `async_clear_config_entry()` on both the device
and entity registries, guarded only by `data != options`. The entity call
deletes registry rows outright - custom names, entity_ids, areas and icons go
with them - and it ran *before* both migrations, so
`_migrate_gateway_device_id()` got an empty device list and returned early.

Finding 04 is the trigger rather than a defect of its own: entries predating
the 1.1.5 refactor carry no feature-flag keys at all, so the first completed
options-flow run guarantees the mismatch that arms the wipe.

The replacement keeps the sync - platforms read entry.data, the options flow
writes entry.options - and touches no registry.
"""

from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import _sync_entry_config
from custom_components.uponorx265.const import (
    CONF_BINARY_SENSOR_VALVE,
    CONF_CONTROLLER_IO,
    CONF_CREATE_CONTROLLERS,
    CONF_INSTALLER_SETTINGS,
    CONF_SENSOR_TEMP,
    CONF_SWITCH_SENSOR_AVG,
    DOMAIN,
    FLAG_DEFAULTS,
)

UNIQUE_ID = "uponorx265_test"

# What a config entry created before the 1.1.5 refactor looks like: host, name
# and the room names, and not one feature flag.
LEGACY_DATA = {"host": "10.0.0.1", "name": "Uponor", "c1_t1": "Kitchen", "c1_t2": "Hall"}


def _entry(hass, data, options):
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)
    return entry


async def test_legacy_entry_gains_the_documented_defaults(hass):
    entry = _entry(hass, LEGACY_DATA, LEGACY_DATA)
    _sync_entry_config(hass, entry)

    assert entry.data[CONF_SENSOR_TEMP] is True
    assert entry.data[CONF_CREATE_CONTROLLERS] is True
    assert entry.data[CONF_BINARY_SENSOR_VALVE] is False
    assert entry.data[CONF_SWITCH_SENSOR_AVG] is False
    assert entry.data[CONF_CONTROLLER_IO] is False
    assert entry.data[CONF_INSTALLER_SETTINGS] is False


async def test_data_and_options_end_up_equal(hass):
    entry = _entry(hass, LEGACY_DATA, LEGACY_DATA)
    _sync_entry_config(hass, entry)

    assert dict(entry.data) == dict(entry.options), (
        "data and options must match, or the next setup sees a mismatch again"
    )


async def test_options_win_over_data(hass):
    entry = _entry(
        hass,
        {**LEGACY_DATA, CONF_BINARY_SENSOR_VALVE: False},
        {**LEGACY_DATA, CONF_BINARY_SENSOR_VALVE: True},
    )
    _sync_entry_config(hass, entry)

    assert entry.data[CONF_BINARY_SENSOR_VALVE] is True


async def test_a_key_only_data_holds_is_not_dropped(hass):
    # The old code replaced data with options wholesale, so anything options
    # did not carry was lost.
    entry = _entry(hass, LEGACY_DATA, {"host": "10.0.0.1", CONF_SENSOR_TEMP: False})
    _sync_entry_config(hass, entry)

    assert entry.data["c1_t1"] == "Kitchen"
    assert entry.data["c1_t2"] == "Hall"
    assert entry.data["name"] == "Uponor"
    assert entry.data[CONF_SENSOR_TEMP] is False


async def test_sync_is_idempotent(hass):
    entry = _entry(hass, LEGACY_DATA, LEGACY_DATA)
    _sync_entry_config(hass, entry)
    settled = dict(entry.data)

    _sync_entry_config(hass, entry)
    assert dict(entry.data) == settled
    assert dict(entry.options) == settled


async def test_an_entry_already_in_step_is_left_alone(hass):
    complete = {**LEGACY_DATA, **FLAG_DEFAULTS}
    entry = _entry(hass, complete, complete)
    _sync_entry_config(hass, entry)

    assert dict(entry.data) == complete
    assert dict(entry.options) == complete


async def test_registries_survive_a_data_options_mismatch(hass):
    """The regression that matters: a mismatch must not cost you the registry."""
    entry = _entry(hass, LEGACY_DATA, {**LEGACY_DATA, CONF_INSTALLER_SETTINGS: True})

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, "AABBCCDDEEFF")},
    )
    registry_entry = ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{UNIQUE_ID}_AABBCCDDEEFF_gateway_status",
        config_entry=entry,
        suggested_object_id="uponor_gateway_status",
    )
    ent_reg.async_update_entity(registry_entry.entity_id, name="Boiler room gateway")

    _sync_entry_config(hass, entry)

    surviving = ent_reg.async_get(registry_entry.entity_id)
    assert surviving is not None, "entity registry row was deleted by the config sync"
    assert surviving.name == "Boiler room gateway", "the user's custom name was lost"
    assert entry.entry_id in dev_reg.async_get(device.id).config_entries, (
        "device lost its config entry association"
    )
