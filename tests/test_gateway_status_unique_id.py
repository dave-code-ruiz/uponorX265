"""The gateway status sensor used to embed the resolved gateway id.

That id is volatile - the MAC when resolution works, the host with its dots
stripped when it does not - so the first successful MAC resolution silently
changed the sensor's unique_id. `_migrate_gateway_device_id` renames the
device in place in that case rather than removing it, so nothing cascades,
the old entity keeps holding `sensor.uponor_gateway_status`, and the new one
registers as `sensor.uponor_gateway_status_2`.

The unique_id is now free of the gateway id (there is one gateway per config
entry, so the instance id alone is unique), and the migration collapses every
historical form onto it.
"""

from homeassistant.helpers import entity_registry as er

from custom_components.uponorx265 import _migrate_entity_unique_ids
from custom_components.uponorx265.const import DOMAIN
from tests.helpers import make_state_proxy

UID = "uponorx265_test"
HOST_FORM = "1921681182"
MAC_FORM = "28F5374E5A24"
CANONICAL = f"{UID}_gateway_status"


def _register(hass, entry, unique_id, object_id="uponor_gateway_status", domain="sensor"):
    return er.async_get(hass).async_get_or_create(
        domain, DOMAIN, unique_id, config_entry=entry, suggested_object_id=object_id
    )


async def test_host_based_form_is_collapsed(hass):
    proxy = make_state_proxy(hass, unique_id=UID)
    entity = _register(hass, proxy._config_entry, f"{UID}_{HOST_FORM}_gateway_status")

    _migrate_entity_unique_ids(hass, proxy._config_entry, UID)

    assert er.async_get(hass).async_get(entity.entity_id).unique_id == CANONICAL


async def test_mac_based_form_is_collapsed(hass):
    proxy = make_state_proxy(hass, unique_id=UID)
    entity = _register(hass, proxy._config_entry, f"{UID}_{MAC_FORM}_gateway_status")

    _migrate_entity_unique_ids(hass, proxy._config_entry, UID)

    assert er.async_get(hass).async_get(entity.entity_id).unique_id == CANONICAL


async def test_pre_1_1_2_bare_form_composes_with_the_prefix_rule(hass):
    proxy = make_state_proxy(hass, unique_id=UID)
    entity = _register(hass, proxy._config_entry, f"{HOST_FORM}_gateway_status")

    _migrate_entity_unique_ids(hass, proxy._config_entry, UID)

    assert er.async_get(hass).async_get(entity.entity_id).unique_id == CANONICAL


async def test_already_canonical_is_left_alone(hass):
    proxy = make_state_proxy(hass, unique_id=UID)
    entity = _register(hass, proxy._config_entry, CANONICAL)

    _migrate_entity_unique_ids(hass, proxy._config_entry, UID)

    surviving = er.async_get(hass).async_get(entity.entity_id)
    assert surviving is not None and surviving.unique_id == CANONICAL


async def test_other_sensors_are_untouched(hass):
    proxy = make_state_proxy(hass, unique_id=UID)
    thermostat = _register(
        hass, proxy._config_entry, f"{UID}_285512345_status", object_id="kitchen_status"
    )
    controller = _register(
        hass, proxy._config_entry, f"{UID}_419524869_status", object_id="c1_status"
    )

    _migrate_entity_unique_ids(hass, proxy._config_entry, UID)

    reg = er.async_get(hass)
    assert reg.async_get(thermostat.entity_id).unique_id == f"{UID}_285512345_status"
    assert reg.async_get(controller.entity_id).unique_id == f"{UID}_419524869_status"


async def test_a_stranded_duplicate_is_removed(hass):
    """Both rows already exist - the run that produced the _2 in the first place."""
    proxy = make_state_proxy(hass, unique_id=UID)
    stale = _register(hass, proxy._config_entry, f"{UID}_{HOST_FORM}_gateway_status")
    current = _register(hass, proxy._config_entry, CANONICAL, object_id="uponor_gateway_status_2")

    _migrate_entity_unique_ids(hass, proxy._config_entry, UID)

    reg = er.async_get(hass)
    assert reg.async_get(stale.entity_id) is None, "stale duplicate was not removed"
    assert reg.async_get(current.entity_id).unique_id == CANONICAL


async def test_the_sensor_reclaims_its_entity_id_instead_of_becoming_2(hass):
    """The payoff: what the user actually sees after a MAC starts resolving."""
    proxy = make_state_proxy(hass, unique_id=UID)
    entry = proxy._config_entry

    # Boot 1: MAC would not resolve, so the sensor registered on the host form.
    boot1 = _register(hass, entry, f"{UID}_{HOST_FORM}_gateway_status")
    assert boot1.entity_id == "sensor.uponor_gateway_status"

    # Boot 2: the MAC resolves. Migration runs, then the platform registers.
    _migrate_entity_unique_ids(hass, entry, UID)
    boot2 = _register(hass, entry, CANONICAL)

    assert boot2.entity_id == "sensor.uponor_gateway_status", (
        f"gateway sensor came back as {boot2.entity_id} instead of reclaiming its entity_id"
    )
    assert boot2.entity_id == boot1.entity_id
