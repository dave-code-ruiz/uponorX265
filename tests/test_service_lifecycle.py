"""Finding 06: the domain services outlive the last config entry.

`set_variable`, `dump_hardware_info` and `dump_raw_data` are registered on the
domain behind a `has_service` guard, so they survive `async_unload_entry`.
Once the last entry is gone their handlers resolve to no state proxies and
return silently - callable from an automation, with no error and no effect.
They have to be removed with the last entry, and kept while any other entry
is still loaded.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import async_unload_entry
from custom_components.uponorx265.const import DOMAIN

# Spelled out rather than imported, so this pins the behaviour the services
# are expected to have and not whatever list the module happens to define.
DOMAIN_SERVICES = ("set_variable", "dump_hardware_info", "dump_raw_data")


def _loaded_entry(hass, unique_id):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "10.0.0.1"}, unique_id=unique_id)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


def _register_all(hass):
    for service in DOMAIN_SERVICES:
        hass.services.async_register(DOMAIN, service, AsyncMock())


async def test_services_removed_when_last_entry_unloads(hass):
    entry = _loaded_entry(hass, "uponorx265_only")
    _register_all(hass)

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, entry) is True

    for service in DOMAIN_SERVICES:
        assert not hass.services.has_service(DOMAIN, service), (
            f"{service} outlived the last config entry"
        )


async def test_services_kept_while_another_entry_is_loaded(hass):
    first = _loaded_entry(hass, "uponorx265_first")
    _loaded_entry(hass, "uponorx265_second")
    _register_all(hass)

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, first) is True

    for service in DOMAIN_SERVICES:
        assert hass.services.has_service(DOMAIN, service), (
            f"{service} was removed while a second gateway is still loaded"
        )


async def test_services_kept_when_platform_unload_fails(hass):
    entry = _loaded_entry(hass, "uponorx265_only")
    _register_all(hass)

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        assert await async_unload_entry(hass, entry) is False

    for service in DOMAIN_SERVICES:
        assert hass.services.has_service(DOMAIN, service), (
            f"{service} was removed even though the platform unload failed"
        )
