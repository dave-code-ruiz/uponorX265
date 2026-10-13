"""Toggling installer mode used to strand the entities it replaced.

`installer_settings` swaps its features between a read-only and a writable
form: relay config and pump management move between `sensor` and `select`,
bypass between `binary_sensor` and `switch`, the floor temperature limits
between `sensor` and `number`. Both forms deliberately share a
unique_id, but the entity registry is keyed by (domain, platform, unique_id) -
so the domain change registers a *second* row rather than renaming the first.

The side no longer being created was left in the registry as an unavailable
entity still holding its entity_id, which also pushed the replacement to a
`..._2` entity_id on the way back. `_remove_stale_installer_mode_entities`
deletes it before platform setup, and hands what the user had put on it to
`_restore_installer_mode_customizations`, which re-applies it to the
replacement once the platforms have registered one.
"""

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import (
    _remove_stale_installer_mode_entities,
    _restore_installer_mode_customizations,
)
from custom_components.uponorx265.const import CONF_INSTALLER_SETTINGS, DOMAIN

UNIQUE_ID = "uponorx265_test"

# The read-only side, as the platforms create it with installer mode off.
READ_ONLY = [
    ("sensor", f"{UNIQUE_ID}_c1id_relay_config"),
    ("sensor", f"{UNIQUE_ID}_pump_management"),
    ("binary_sensor", f"{UNIQUE_ID}_t1id_bypass_enable"),
    ("sensor", f"{UNIQUE_ID}_t1id_floor_min_limit"),
    ("sensor", f"{UNIQUE_ID}_t1id_floor_max_limit"),
]

# The writable side, same unique_ids, as installer mode creates them.
WRITABLE = [
    ("select", f"{UNIQUE_ID}_c1id_relay_config"),
    ("select", f"{UNIQUE_ID}_pump_management"),
    ("switch", f"{UNIQUE_ID}_t1id_bypass_enable"),
    ("number", f"{UNIQUE_ID}_t1id_floor_min_limit"),
    ("number", f"{UNIQUE_ID}_t1id_floor_max_limit"),
]

# Entities that have nothing to do with the flag and must survive either way.
UNRELATED = [
    ("sensor", f"{UNIQUE_ID}_gateway_status"),
    ("sensor", f"{UNIQUE_ID}_t1id_current_temp"),
    ("sensor", f"{UNIQUE_ID}_t1id_floor_temp"),
    ("switch", f"{UNIQUE_ID}_away"),
    ("switch", f"{UNIQUE_ID}_t1id_local_override"),
    ("binary_sensor", f"{UNIQUE_ID}_t1id_cb_actuator"),
    ("climate", f"{UNIQUE_ID}_t1id_climate"),
]


def _entry(hass, installer_settings):
    data = {"host": "10.0.0.1", "name": "Uponor", CONF_INSTALLER_SETTINGS: installer_settings}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=data, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)
    return entry


def _register(hass, entry, entities):
    reg = er.async_get(hass)
    return {
        (domain, unique_id): reg.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry
        ).entity_id
        for domain, unique_id in entities
    }


def _registered(hass):
    reg = er.async_get(hass)
    return {(e.domain, e.unique_id) for e in reg.entities.values()}


async def test_turning_installer_mode_on_removes_the_read_only_twins(hass):
    entry = _entry(hass, True)
    _register(hass, entry, READ_ONLY + WRITABLE + UNRELATED)

    _remove_stale_installer_mode_entities(hass, entry)

    assert _registered(hass) == set(WRITABLE) | set(UNRELATED)


async def test_turning_installer_mode_off_removes_the_writable_twins(hass):
    entry = _entry(hass, False)
    _register(hass, entry, READ_ONLY + WRITABLE + UNRELATED)

    _remove_stale_installer_mode_entities(hass, entry)

    assert _registered(hass) == set(READ_ONLY) | set(UNRELATED)


async def test_a_missing_flag_is_treated_as_installer_mode_off(hass):
    data = {"host": "10.0.0.1", "name": "Uponor"}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=data, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)
    _register(hass, entry, READ_ONLY + WRITABLE)

    _remove_stale_installer_mode_entities(hass, entry)

    assert _registered(hass) == set(READ_ONLY)


async def test_the_replacement_takes_over_the_removed_entity_id(hass):
    """The point of removing the twin first: its object_id is free to reclaim."""
    entry = _entry(hass, True)
    reg = er.async_get(hass)
    unique_id = f"{UNIQUE_ID}_t1id_bypass_enable"
    stale = reg.async_get_or_create(
        "binary_sensor", DOMAIN, unique_id,
        suggested_object_id="kitchen_bypass", config_entry=entry,
    )
    assert stale.entity_id == "binary_sensor.kitchen_bypass"

    carried = _remove_stale_installer_mode_entities(hass, entry)

    # Stands in for platform setup creating the writable side.
    replacement = reg.async_get_or_create(
        "switch", DOMAIN, unique_id,
        suggested_object_id="uponor_c1_t1_bypass", config_entry=entry,
    )
    _restore_installer_mode_customizations(hass, carried)

    assert reg.async_get(replacement.entity_id) is None
    assert reg.async_get_entity_id("switch", DOMAIN, unique_id) == "switch.kitchen_bypass"


async def test_another_entry_is_left_alone(hass):
    """The cleanup is scoped to its own config entry, not the whole registry."""
    mine = _entry(hass, True)
    other_data = {"host": "10.0.0.2", "name": "Uponor 2", CONF_INSTALLER_SETTINGS: False}
    other = MockConfigEntry(
        domain=DOMAIN, data=other_data, options=other_data, unique_id="uponorx265_other"
    )
    other.add_to_hass(hass)

    _register(hass, mine, READ_ONLY + WRITABLE)
    other_ids = [(d, u.replace(UNIQUE_ID, "uponorx265_other")) for d, u in READ_ONLY + WRITABLE]
    _register(hass, other, other_ids)

    _remove_stale_installer_mode_entities(hass, mine)

    assert _registered(hass) == set(WRITABLE) | set(other_ids)


async def test_customizations_follow_the_feature_across_the_toggle(hass):
    """Everything the user put on the read-only row lands on the writable one."""
    entry = _entry(hass, True)
    reg = er.async_get(hass)
    unique_id = f"{UNIQUE_ID}_c1id_relay_config"
    stale = reg.async_get_or_create(
        "sensor", DOMAIN, unique_id, suggested_object_id="cellar_relays", config_entry=entry
    )
    reg.async_update_entity(
        stale.entity_id,
        name="Cellar relays",
        icon="mdi:electric-switch",
        area_id="cellar",
        labels={"heating"},
        aliases={"the relays"},
        hidden_by=er.RegistryEntryHider.USER,
    )

    carried = _remove_stale_installer_mode_entities(hass, entry)

    replacement = reg.async_get_or_create(
        "select", DOMAIN, unique_id,
        suggested_object_id="uponor_c1_controller_relays", config_entry=entry,
    )
    _restore_installer_mode_customizations(hass, carried)

    restored = reg.async_get(reg.async_get_entity_id("select", DOMAIN, unique_id))
    assert restored.entity_id == "select.cellar_relays"
    assert restored.name == "Cellar relays"
    assert restored.icon == "mdi:electric-switch"
    assert restored.area_id == "cellar"
    assert restored.labels == {"heating"}
    assert restored.aliases == {"the relays"}
    assert restored.hidden_by is er.RegistryEntryHider.USER
    assert reg.async_get(replacement.entity_id) is None


async def test_an_uncustomized_row_leaves_the_replacement_untouched(hass):
    """Nothing to carry must not blank out what the platform just supplied."""
    entry = _entry(hass, True)
    reg = er.async_get(hass)
    unique_id = f"{UNIQUE_ID}_c1id_relay_config"
    reg.async_get_or_create(
        "sensor", DOMAIN, unique_id,
        suggested_object_id="uponor_c1_controller_relays", config_entry=entry,
    )

    carried = _remove_stale_installer_mode_entities(hass, entry)

    replacement = reg.async_get_or_create(
        "select", DOMAIN, unique_id,
        suggested_object_id="uponor_c1_controller_relays",
        original_name="Controller relays", original_icon="mdi:tune", config_entry=entry,
    )
    _restore_installer_mode_customizations(hass, carried)

    restored = reg.async_get(replacement.entity_id)
    assert restored is not None
    assert restored.name is None
    assert restored.icon is None
    assert restored.original_name == "Controller relays"
    assert restored.area_id is None
    assert restored.hidden_by is None


async def test_an_existing_replacement_keeps_its_own_customizations(hass):
    """A twice-toggled entry: the row the user lives with outranks the stale one."""
    entry = _entry(hass, True)
    reg = er.async_get(hass)
    unique_id = f"{UNIQUE_ID}_c1id_relay_config"
    stale = reg.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)
    reg.async_update_entity(stale.entity_id, name="Stale name", area_id="cellar")
    live = reg.async_get_or_create("select", DOMAIN, unique_id, config_entry=entry)
    reg.async_update_entity(live.entity_id, name="The name I actually use")

    carried = _remove_stale_installer_mode_entities(hass, entry)
    _restore_installer_mode_customizations(hass, carried)

    assert carried == {}
    kept = reg.async_get(live.entity_id)
    assert kept.name == "The name I actually use"
    assert kept.area_id is None


async def test_a_feature_that_does_not_come_back_is_skipped(hass):
    """A controller that disappeared leaves nothing to restore onto."""
    entry = _entry(hass, True)
    reg = er.async_get(hass)
    stale = reg.async_get_or_create(
        "sensor", DOMAIN, f"{UNIQUE_ID}_c4id_relay_config", config_entry=entry
    )
    reg.async_update_entity(stale.entity_id, name="Gone")

    carried = _remove_stale_installer_mode_entities(hass, entry)
    _restore_installer_mode_customizations(hass, carried)  # must not raise

    assert _registered(hass) == set()


async def test_a_taken_entity_id_is_not_stolen(hass):
    """An unrelated entity already holding the old object_id keeps it."""
    entry = _entry(hass, True)
    reg = er.async_get(hass)
    unique_id = f"{UNIQUE_ID}_c1id_relay_config"
    stale = reg.async_get_or_create(
        "sensor", DOMAIN, unique_id, suggested_object_id="cellar_relays", config_entry=entry
    )
    reg.async_update_entity(stale.entity_id, name="Cellar relays")

    carried = _remove_stale_installer_mode_entities(hass, entry)

    # Something else claims select.cellar_relays in the meantime.
    squatter = reg.async_get_or_create(
        "select", "other_integration", "squatter", suggested_object_id="cellar_relays"
    )
    assert squatter.entity_id == "select.cellar_relays"
    replacement = reg.async_get_or_create(
        "select", DOMAIN, unique_id,
        suggested_object_id="uponor_c1_controller_relays", config_entry=entry,
    )
    _restore_installer_mode_customizations(hass, carried)

    assert reg.async_get(squatter.entity_id).platform == "other_integration"
    # The rename is skipped, but the rest of the customizations still land.
    assert reg.async_get(replacement.entity_id).name == "Cellar relays"
