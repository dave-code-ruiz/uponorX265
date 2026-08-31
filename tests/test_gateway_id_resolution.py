"""Finding 02: the gateway id froze on the host-based fallback.

Two defects in async_resolve_gateway_id(). The first lookup ran
get_mac_address() straight on the event loop while the fallback five lines
below was correctly offloaded. And when both lookups failed, the host-based
form was written into the cache - which the whole method is guarded on - so
it never re-resolved. It is called once, at setup, so one failed lookup at
boot pinned the wrong identifier for the entire session.

That identifier is not cosmetic: it becomes the gateway device's identifier
and is baked into the gateway sensor's unique_id, so the tree gets registered
under something a later restart will not match.
"""

import threading
from unittest.mock import patch

from homeassistant.helpers import device_registry as dr

from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
HOST = "192.168.1.182"
HOST_FORM = "1921681182"
MAC = "28:F5:37:4E:5A:24"
MAC_FORM = "28F5374E5A24"


def _proxy(hass):
    proxy = make_state_proxy(hass, unique_id=UNIQUE_ID)
    proxy._host = HOST
    return proxy


async def test_mac_lookup_does_not_run_on_the_event_loop(hass):
    """getmac shells out to arp/ip neighbor - it must not block the loop."""
    proxy = _proxy(hass)
    loop_thread = threading.get_ident()
    ran_on = {}

    def fake_get_mac(**kwargs):
        ran_on["thread"] = threading.get_ident()
        return MAC

    with patch("custom_components.uponorx265.get_mac_address", fake_get_mac):
        assert await proxy.async_resolve_gateway_id() == MAC_FORM

    assert ran_on["thread"] != loop_thread, (
        "get_mac_address ran on the event loop instead of in an executor"
    )


async def test_failed_resolution_is_not_cached(hass):
    proxy = _proxy(hass)

    with patch("custom_components.uponorx265.get_mac_address", return_value=None), \
         patch("custom_components.uponorx265._get_mac_with_arp_refresh", return_value=None):
        assert await proxy.async_resolve_gateway_id() == HOST_FORM

    assert proxy._gateway_id is None, (
        "the host fallback was cached, so the session can never re-resolve"
    )


async def test_a_later_attempt_can_still_resolve(hass):
    """The cold-ARP case: fails at boot, succeeds once the cache warms."""
    proxy = _proxy(hass)

    with patch("custom_components.uponorx265.get_mac_address", return_value=None), \
         patch("custom_components.uponorx265._get_mac_with_arp_refresh", return_value=None):
        assert await proxy.async_resolve_gateway_id() == HOST_FORM

    with patch("custom_components.uponorx265.get_mac_address", return_value=MAC):
        assert await proxy.async_resolve_gateway_id() == MAC_FORM

    assert proxy._gateway_id == MAC_FORM


async def test_get_gateway_id_returns_the_host_form_while_unresolved(hass):
    proxy = _proxy(hass)
    assert proxy.get_gateway_id() == HOST_FORM


async def test_a_resolved_mac_is_cached_and_not_looked_up_again(hass):
    proxy = _proxy(hass)
    calls = []

    def counting(**kwargs):
        calls.append(1)
        return MAC

    with patch("custom_components.uponorx265.get_mac_address", counting):
        await proxy.async_resolve_gateway_id()
        await proxy.async_resolve_gateway_id()

    assert len(calls) == 1, "a resolved MAC should be cached, not re-looked-up every call"


async def test_retry_migrates_the_device_registered_under_the_fallback(hass):
    """The repair: the device registered at boot moves to the real identifier."""
    proxy = _proxy(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, HOST_FORM)},
    )

    with patch("custom_components.uponorx265.get_mac_address", return_value=MAC):
        await proxy._async_retry_gateway_id()

    migrated = dev_reg.async_get(device.id)
    assert (UNIQUE_ID, MAC_FORM) in migrated.identifiers, (
        "gateway device was left under the host-based identifier"
    )
    assert (UNIQUE_ID, HOST_FORM) not in migrated.identifiers


async def test_retry_is_a_no_op_while_the_mac_still_will_not_resolve(hass):
    proxy = _proxy(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, HOST_FORM)},
    )

    with patch("custom_components.uponorx265.get_mac_address", return_value=None), \
         patch("custom_components.uponorx265._get_mac_with_arp_refresh", return_value=None):
        await proxy._async_retry_gateway_id()

    assert (UNIQUE_ID, HOST_FORM) in dev_reg.async_get(device.id).identifiers
    assert proxy._gateway_id is None
