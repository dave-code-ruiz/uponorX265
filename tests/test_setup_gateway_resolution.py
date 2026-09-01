"""A cached start must not wait on the gateway to resolve its id.

Caching thermostats exists so entities come back after a restart while the
gateway is still booting - `async_setup_entry` backgrounds the first poll and
goes straight to platform setup. Resolving the gateway id is a JNAP round
trip, so awaiting it there put the network back in front of platform setup on
exactly the path built to avoid it: against an unreachable gateway the client
spends ~8s (three attempts at a 2s connect timeout, plus backoff) before
giving up, and every entity is late by that much.

It is also unnecessary. A cached start means the entry has been set up before,
so `_get_registered_gateway_id()` has already read the identifier the gateway
device is registered under, and `get_gateway_id()` returns exactly that. The
backgrounded poll resolves the real MAC and `_async_retry_gateway_id()`
migrates the registry if it ever differs.

A first setup has no registered id to fall back on and already awaits the
first poll, so resolution stays there.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import async_setup_entry
from custom_components.uponorx265.const import DOMAIN, STORAGE_KEY, STORAGE_VERSION

UNIQUE_ID = "uponorx265_test"
HOST = "192.168.1.10"
HOST_FORM = "192168110"
MAC = "AA:BB:CC:DD:EE:FF"
MAC_FORM = "AABBCCDDEEFF"
CONTROLLER_ID = "419524869"
THERMOSTAT_ID = "285805788"

LIVE_DATA = {
    "sys_controller_1_presence": "1",
    "controller1_id": CONTROLLER_ID,
    "C1_thermostat1_id": THERMOSTAT_ID,
    "C1_T1_thermostat_type": "2",
    "C1_T1_hw_type": "7",
    "cust_SW_version_update": "X265_121.hex",
}
DEVICE_INFO = {"deviceID": MAC, "serialNumber": "000000XX000000"}

CACHED_META = {
    "thermostats": ["C1_T1"],
    "ids": {"C1_T1": THERMOSTAT_ID},
    "controllers": ["C1"],
    "controller_ids": {"C1": CONTROLLER_ID},
    "models": {"C1_T1": "T-169"},
    "rooms": {"C1_T1": "Kitchen"},
}


@pytest.fixture(autouse=True)
def _no_background_noise():
    """Keep the poll timer out of these tests."""
    with patch("custom_components.uponorx265.async_track_time_interval",
               return_value=lambda: None):
        yield


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: HOST, "name": "Uponor"},
        options={CONF_HOST: HOST, "name": "Uponor"},
        unique_id=UNIQUE_ID,
    )
    entry.add_to_hass(hass)
    return entry


def _seed_cache(hass_storage):
    hass_storage[f"{STORAGE_KEY}_{UNIQUE_ID}"] = {
        "version": STORAGE_VERSION,
        "key": f"{STORAGE_KEY}_{UNIQUE_ID}",
        "data": {"_meta": CACHED_META},
    }


async def _setup(hass, entry, *, device_info=DEVICE_INFO):
    """Run async_setup_entry with a mocked gateway, returning the client."""
    client = AsyncMock()
    client.get_data.return_value = dict(LIVE_DATA)
    client.get_device_info.return_value = dict(device_info)
    with patch("custom_components.uponorx265.UponorJnap", return_value=client), \
         patch.object(hass.config_entries, "async_forward_entry_setups",
                      AsyncMock(return_value=True)):
        assert await async_setup_entry(hass, entry) is True
    return client


async def test_cached_start_does_not_wait_on_an_unresponsive_gateway(hass, hass_storage):
    """A gateway that never answers must not hold up platform setup.

    Asserting on call counts cannot distinguish setup awaiting the gateway
    from the backgrounded poll reaching it first, so the gateway is made to
    hang instead: if setup awaits anything from it, this never returns.
    """
    _seed_cache(hass_storage)
    entry = _entry(hass)

    answer = asyncio.Event()

    async def only_once_it_answers(*args, **kwargs):
        await answer.wait()
        return dict(DEVICE_INFO)

    async def data_only_once_it_answers(*args, **kwargs):
        await answer.wait()
        return dict(LIVE_DATA)

    client = AsyncMock()
    client.get_device_info.side_effect = only_once_it_answers
    client.get_data.side_effect = data_only_once_it_answers

    with patch("custom_components.uponorx265.UponorJnap", return_value=client), \
         patch.object(hass.config_entries, "async_forward_entry_setups",
                      AsyncMock(return_value=True)):
        # Generous next to the client's ~8s give-up time, tight next to
        # "returned without waiting at all".
        assert await asyncio.wait_for(
            async_setup_entry(hass, entry), timeout=1
        ) is True

        # Let the backgrounded poll finish so nothing is left pending.
        answer.set()
        await hass.async_block_till_done()


async def test_cached_start_registers_under_the_existing_identifier(hass, hass_storage):
    """Deferring resolution must not fall back to the host-derived id."""
    _seed_cache(hass_storage)
    entry = _entry(hass)

    # What a previous successful setup left behind: the gateway device already
    # carries the MAC-derived identifier.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, MAC_FORM)},
    )

    await _setup(hass, entry)

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, MAC_FORM), entry.entry_id
    ) is not None, "the gateway device was not kept under its registered identifier"
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, HOST_FORM), entry.entry_id
    ) is None, "a second gateway device was created under the host-derived form"


async def test_first_setup_still_resolves_before_platform_setup(hass, hass_storage):
    """With no cache there is no registered id to fall back on."""
    entry = _entry(hass)

    client = await _setup(hass, entry)

    assert client.get_device_info.await_count >= 1, (
        "a first setup must resolve the MAC before device_info is evaluated"
    )
    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, MAC_FORM), entry.entry_id
    ) is not None, "first setup registered the gateway under the wrong identifier"


async def test_first_setup_falls_back_when_the_gateway_reports_no_mac(hass, hass_storage):
    entry = _entry(hass)

    await _setup(hass, entry, device_info={})

    assert dr.async_get(hass).async_get_device_by_identifier(
        (UNIQUE_ID, HOST_FORM), entry.entry_id
    ) is not None, "with no MAC available the host form is the expected identifier"


async def test_cached_start_converges_once_the_poll_resolves(hass, hass_storage):
    """The backgrounded poll is what carries resolution on a cached start."""
    _seed_cache(hass_storage)
    entry = _entry(hass)

    # A previous run never resolved a MAC, so the device sits on the host form.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, HOST_FORM)},
    )

    await _setup(hass, entry)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, MAC_FORM), entry.entry_id
    ) is not None, (
        "the backgrounded poll did not migrate the gateway onto its real MAC"
    )
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, HOST_FORM), entry.entry_id
    ) is None
