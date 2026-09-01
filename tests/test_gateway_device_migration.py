"""_migrate_gateway_device_id must reconcile the gateway device across any
prior identifier — historical format changes (host-based -> lowercase MAC ->
uppercase MAC) *and* host drift (DHCP reassigning the IP while MAC
resolution keeps failing, e.g. gateway and HA host on different subnets) —
without ever leaving an orphaned duplicate device behind.

It identifies the old device structurally (the one device for this config
entry with no via_device) rather than guessing specific old id strings, so
it isn't fooled by an id it's never seen before.
"""

from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import DOMAIN, _migrate_gateway_device_id

UNIQUE_ID = "uponorx265_test"
HOST_ID = "101683"  # "10.1.6.83".replace('.', '')
LOWER_MAC = "aabbccddeeff"
UPPER_MAC = "AABBCCDDEEFF"


def register_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)
    return entry


def register_device(hass, entry, gateway_id, **extra):
    dev_reg = dr.async_get(hass)
    return dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, gateway_id)},
        manufacturer="Uponor",
        name="Floda",
        **extra,
    )


def get_device(hass, entry, gateway_id):
    """Look up the device holding this gateway identifier, within this entry.

    async_get_device_by_identifier (HA 2026.8+) rather than async_get_device:
    the latter is deprecated because identifiers are no longer unique across
    config entries, and calling it from test code — where there is no
    integration frame on the stack — raises outright from HA 2026.9.
    """
    dev_reg = dr.async_get(hass)
    return dev_reg.async_get_device_by_identifier((UNIQUE_ID, gateway_id), entry.entry_id)


async def test_renames_host_based_device_in_place(hass):
    entry = register_entry(hass)
    old_device = register_device(hass, entry, HOST_ID)

    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)

    assert get_device(hass, entry, HOST_ID) is None
    migrated = get_device(hass, entry, UPPER_MAC)
    assert migrated is not None
    assert migrated.id == old_device.id, "should rename the existing device, not create a new one"


async def test_renames_lowercase_mac_device_in_place(hass):
    entry = register_entry(hass)
    old_device = register_device(hass, entry, LOWER_MAC)

    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)

    assert get_device(hass, entry, LOWER_MAC) is None
    migrated = get_device(hass, entry, UPPER_MAC)
    assert migrated is not None
    assert migrated.id == old_device.id


async def test_renames_device_with_an_id_never_guessed_at(hass):
    # The gateway's old identifier was based on an IP address from three
    # restarts ago (DHCP has reassigned it twice since, while MAC resolution
    # kept failing because the gateway is on a different subnet). Nothing in
    # the migration logic should need to know or guess that specific string.
    entry = register_entry(hass)
    stale_ip_based_id = "10842217"  # some previous host, long since reassigned
    old_device = register_device(hass, entry, stale_ip_based_id)

    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)

    assert get_device(hass, entry, stale_ip_based_id) is None, (
        "an id that drifted away from any known format must still be migrated, not orphaned"
    )
    migrated = get_device(hass, entry, UPPER_MAC)
    assert migrated is not None
    assert migrated.id == old_device.id


async def test_merges_duplicate_left_by_a_prior_restart(hass):
    entry = register_entry(hass)
    dev_reg = dr.async_get(hass)
    old_device = register_device(hass, entry, HOST_ID)
    old_device = dev_reg.async_update_device(old_device.id, name_by_user="Garage Gateway")

    ent_reg = er.async_get(hass)
    gateway_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{UNIQUE_ID}_{HOST_ID}_gateway_status",
        config_entry=entry,
        device_id=old_device.id,
        suggested_object_id="uponor_gateway_status",
    )
    gateway_entity = ent_reg.async_update_entity(
        gateway_entity.entity_id,
        name="Garage connection",
        icon="mdi:garage",
    )

    controller = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, "419524869")},
        manufacturer="Uponor",
        name="Controller 1",
        via_device_id=old_device.id,
    )
    new_device = register_device(hass, entry, UPPER_MAC)

    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    assert get_device(hass, entry, HOST_ID) is None, (
        "orphaned old-format device must be removed, not left behind"
    )
    remaining = get_device(hass, entry, UPPER_MAC)
    assert remaining is not None
    assert remaining.id == new_device.id
    assert remaining.name_by_user == "Garage Gateway", (
        "user customization from the old device must be carried over"
    )

    surviving_entity = ent_reg.async_get(gateway_entity.entity_id)
    assert surviving_entity is not None, (
        "removing the duplicate device must not delete its entity registry rows"
    )
    assert surviving_entity.device_id == new_device.id
    assert surviving_entity.name == "Garage connection"
    assert surviving_entity.icon == "mdi:garage"

    migrated_controller = dev_reg.async_get(controller.id)
    assert migrated_controller is not None
    assert migrated_controller.via_device_id == new_device.id, (
        "child devices must be reparented before the duplicate gateway is removed"
    )


async def test_no_op_when_no_old_device_exists(hass):
    entry = register_entry(hass)

    # Must not raise, and must not fabricate a device out of thin air —
    # creating devices is _register_gateway_devices's job, not this one's.
    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)

    assert get_device(hass, entry, UPPER_MAC) is None


async def test_no_op_when_already_on_current_format(hass):
    entry = register_entry(hass)
    device = register_device(hass, entry, UPPER_MAC)

    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)

    unchanged = get_device(hass, entry, UPPER_MAC)
    assert unchanged is not None
    assert unchanged.id == device.id


async def test_ignores_controller_devices_which_have_a_via_device(hass):
    # A controller device also belongs to this config entry, but it has a
    # via_device (pointing at the gateway) — it must never be mistaken for
    # the gateway device itself.
    entry = register_entry(hass)
    dev_reg = dr.async_get(hass)
    gateway_device = register_device(hass, entry, HOST_ID)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, "419524869")},
        manufacturer="Uponor",
        name="Controller 1",
        via_device_id=gateway_device.id,
    )

    _migrate_gateway_device_id(hass, entry, UNIQUE_ID, UPPER_MAC)

    migrated = get_device(hass, entry, UPPER_MAC)
    assert migrated is not None
    assert migrated.id == gateway_device.id
    # The controller device is untouched.
    assert get_device(hass, entry, "419524869") is not None
