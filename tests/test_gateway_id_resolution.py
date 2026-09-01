"""The gateway id comes from the gateway, not from the network.

Originally finding 02: async_resolve_gateway_id() ran get_mac_address() on the
event loop, and cached the host-based fallback into the very field the method
is guarded on, so one failed lookup at boot pinned the wrong identifier for
the whole session.

The lookup itself is now gone. `core/GetDeviceInfo` reports the gateway's own
MAC as `deviceID`, so resolution is a read from the device rather than an
inference from the local ARP cache - no executor hop, no cold-cache failure,
and it works across subnets, which ARCHITECTURE.md had written off as a
topology limit.

The normalisation is deliberately unchanged ("AA:BB:CC:DD:EE:FF" -> "AABBCCDDEEFF"),
so an install that already resolved a MAC through getmac keeps the exact
identifier it registered. The fallback behaviour is unchanged too, and is
still what these tests pin: that identifier anchors the gateway device and the
controller/thermostat hierarchy under it, so churn orphans registry devices.
"""

from homeassistant.helpers import device_registry as dr

from custom_components.uponorx265 import _migrate_gateway_device_id
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
HOST = "192.168.1.10"
HOST_FORM = "192168110"
MAC = "AA:BB:CC:DD:EE:FF"
MAC_FORM = "AABBCCDDEEFF"


def _proxy(hass, *, reports_mac=True):
    proxy = make_state_proxy(hass, unique_id=UNIQUE_ID)
    proxy._host = HOST
    _set_gateway_answer(proxy, reports_mac=reports_mac)
    return proxy


def _set_gateway_answer(proxy, *, reports_mac):
    """Control what core/GetDeviceInfo reports, and reset any cached answer."""
    proxy._device_info = {}
    proxy._client.get_device_info.return_value = (
        {"deviceID": MAC, "serialNumber": "000000XX000000"} if reports_mac else {}
    )


async def test_mac_is_read_from_the_gateway_and_normalised(hass):
    proxy = _proxy(hass)

    assert await proxy.async_resolve_gateway_id() == MAC_FORM
    assert proxy._gateway_id == MAC_FORM, (
        "the identifier format must match what getmac-era installs registered"
    )


async def test_failed_resolution_is_not_cached(hass):
    proxy = _proxy(hass, reports_mac=False)

    assert await proxy.async_resolve_gateway_id() == HOST_FORM

    assert proxy._gateway_id is None, (
        "the host fallback was cached, so the session can never re-resolve"
    )


async def test_failed_resolution_does_not_downgrade_existing_mac_device(hass):
    """A gateway that won't answer must not move a stable device to its host id."""
    proxy = _proxy(hass, reports_mac=False)
    dev_reg = dr.async_get(hass)
    stable_device = dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, MAC_FORM)},
    )

    resolved = await proxy.async_resolve_gateway_id()

    assert resolved == MAC_FORM
    assert proxy.get_gateway_id() == MAC_FORM
    assert proxy._gateway_id is None, (
        "a registry fallback must not be mistaken for a confirmed lookup"
    )

    # This mirrors the setup step. A host fallback here would rename the
    # existing MAC device back to the IP-derived identifier.
    _migrate_gateway_device_id(hass, proxy._config_entry, UNIQUE_ID, resolved)

    unchanged = dev_reg.async_get(stable_device.id)
    assert unchanged is not None
    assert unchanged.identifiers == {(UNIQUE_ID, MAC_FORM)}
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, HOST_FORM), proxy._config_entry.entry_id
    ) is None

    # The retained registry id is only a fallback; a later poll must still
    # attempt resolution and cache a confirmed answer once the gateway replies.
    _set_gateway_answer(proxy, reports_mac=True)
    assert await proxy.async_resolve_gateway_id() == MAC_FORM
    assert proxy._gateway_id == MAC_FORM


async def test_a_later_attempt_can_still_resolve(hass):
    """Fails while the gateway is unreachable, succeeds once it answers."""
    proxy = _proxy(hass, reports_mac=False)
    assert await proxy.async_resolve_gateway_id() == HOST_FORM

    _set_gateway_answer(proxy, reports_mac=True)
    assert await proxy.async_resolve_gateway_id() == MAC_FORM

    assert proxy._gateway_id == MAC_FORM


async def test_a_raising_gateway_is_retried_rather_than_remembered(hass):
    """A transient JNAP error must not poison device info for the session."""
    proxy = _proxy(hass)
    proxy._client.get_device_info.side_effect = OSError("connection reset")

    assert await proxy.async_resolve_gateway_id() == HOST_FORM
    assert proxy._gateway_id is None

    proxy._client.get_device_info.side_effect = None
    _set_gateway_answer(proxy, reports_mac=True)
    assert await proxy.async_resolve_gateway_id() == MAC_FORM


async def test_get_gateway_id_returns_the_host_form_while_unresolved(hass):
    proxy = _proxy(hass)
    assert proxy.get_gateway_id() == HOST_FORM


async def test_a_resolved_mac_is_cached_and_not_looked_up_again(hass):
    proxy = _proxy(hass)

    await proxy.async_resolve_gateway_id()
    await proxy.async_resolve_gateway_id()

    assert proxy._client.get_device_info.await_count == 1, (
        "a resolved MAC should be cached, not re-fetched on every call"
    )


async def test_retry_migrates_the_device_registered_under_the_fallback(hass):
    """The repair: the device registered at boot moves to the real identifier."""
    proxy = _proxy(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, HOST_FORM)},
    )

    await proxy._async_retry_gateway_id()

    migrated = dev_reg.async_get(device.id)
    assert (UNIQUE_ID, MAC_FORM) in migrated.identifiers, (
        "gateway device was left under the host-based identifier"
    )
    assert (UNIQUE_ID, HOST_FORM) not in migrated.identifiers


async def test_retry_is_a_no_op_while_the_gateway_will_not_answer(hass):
    proxy = _proxy(hass, reports_mac=False)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, HOST_FORM)},
    )

    await proxy._async_retry_gateway_id()

    assert (UNIQUE_ID, HOST_FORM) in dev_reg.async_get(device.id).identifiers
    assert proxy._gateway_id is None
