"""The pre-HA-2026.8 fallbacks in the device-registry compat shims must work.

Two registry APIs changed in HA 2026.8/2026.9: `async_get_device` gave way to
`async_get_device_by_identifier`, and `async_get_or_create`'s `via_device`
(a parent identifier tuple) gave way to `via_device_id` (the parent's registry
id). Both old forms break in HA 2027.8.

The integration prefers the new APIs and falls back to the old ones on cores
older than 2026.8. Nothing else covers those fallback branches — on a current
core they are dead code until an old-HA user hits them — so these tests force
each fallback and assert it resolves the same device / builds the same
hierarchy as the modern path.

Both old APIs still function (deprecated) on the pinned 2026.8.3, so the
fallbacks stay exercisable. They are reached here through integration code
rather than called directly from the test, which also matches how HA reports
the deprecation: a custom-integration frame downgrades it to a log line,
whereas a direct call from test code raises outright from 2026.9.
"""

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.uponorx265 as uponor
from custom_components.uponorx265 import (
    DOMAIN,
    _async_get_device_by_identifier,
    _register_gateway_devices,
)
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
GATEWAY_ID = "AABBCCDDEEFF"
CONTROLLER_ID = "419524869"


async def test_lookup_falls_back_to_async_get_device(hass, monkeypatch):
    """Without async_get_device_by_identifier, the old lookup finds the device."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, GATEWAY_ID)},
    )

    # Simulate a core older than 2026.8, where the method does not exist.
    monkeypatch.delattr(
        type(dev_reg), "async_get_device_by_identifier", raising=False
    )

    found = _async_get_device_by_identifier(
        dev_reg, (UNIQUE_ID, GATEWAY_ID), entry.entry_id
    )
    assert found is not None, "fallback lookup failed to find a registered device"
    assert found.id == device.id

    missing = _async_get_device_by_identifier(
        dev_reg, (UNIQUE_ID, "does-not-exist"), entry.entry_id
    )
    assert missing is None, "fallback lookup must report an absent device as None"


async def test_via_device_fallback_still_builds_the_hierarchy(hass, monkeypatch):
    """With via_device_id unavailable, via_device must still parent the controller."""
    monkeypatch.setattr(uponor, "_SUPPORTS_VIA_DEVICE_ID", False)

    proxy = make_state_proxy(
        hass,
        data={
            "sys_controller_1_presence": "1",
            "controller1_id": CONTROLLER_ID,
        },
        unique_id=UNIQUE_ID,
    )
    proxy._gateway_id = GATEWAY_ID

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)

    dev_reg = dr.async_get(hass)
    entry_id = proxy._config_entry.entry_id
    gateway = dev_reg.async_get_device_by_identifier((UNIQUE_ID, GATEWAY_ID), entry_id)
    controller = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, CONTROLLER_ID), entry_id
    )

    assert gateway is not None
    assert controller is not None
    assert controller.via_device_id == gateway.id, (
        "the deprecated via_device identifier must still resolve to the gateway"
    )
