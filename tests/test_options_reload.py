"""Finding 07: an options change tore the entry down twice.

async_update_options() called async_unload() and then async_reload(), but
async_reload() already unloads a loaded entry first. The comment framed the
explicit unload as insurance for an entry whose initial setup failed, which
async_reload() also handles on its own.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import async_update_options
from custom_components.uponorx265.const import DOMAIN


def _entry(hass, state):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "10.0.0.1"}, unique_id="uponorx265_test")
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    return entry


async def test_loaded_entry_is_reloaded_once(hass):
    entry = _entry(hass, ConfigEntryState.LOADED)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload, \
         patch.object(hass.config_entries, "async_unload", AsyncMock()) as unload:
        await async_update_options(hass, entry)

    reload.assert_awaited_once_with(entry.entry_id)
    unload.assert_not_awaited()


async def test_entry_that_failed_setup_is_still_reloaded(hass):
    entry = _entry(hass, ConfigEntryState.SETUP_RETRY)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload, \
         patch.object(hass.config_entries, "async_unload", AsyncMock()) as unload:
        await async_update_options(hass, entry)

    reload.assert_awaited_once_with(entry.entry_id)
    unload.assert_not_awaited()
