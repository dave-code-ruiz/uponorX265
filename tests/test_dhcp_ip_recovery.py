"""The entry follows the gateway when DHCP moves it to a new IP.

The MAC was originally resolved only to give the gateway device a stable
registry key, so that a DHCP move renamed nothing. It never made the
integration able to *reach* a moved gateway - the host stayed whatever string
was typed into the config flow.

Recording the MAC as a device connection lets Home Assistant match a DHCP
lease to this entry and hand the config flow the new address. That is only
possible because the MAC now comes from the gateway itself (JNAP
core/GetDeviceInfo); the previous ARP lookup could not see a gateway on
another subnet and returned nothing on a cold cache, so the connection would
have been missing in exactly the cases that matter.
"""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_DHCP
from homeassistant.const import CONF_HOST
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import _register_gateway_devices
from custom_components.uponorx265.const import DOMAIN
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_uponor"
MAC = "AA:BB:CC:DD:EE:FF"
MAC_FORM = "AABBCCDDEEFF"
OLD_HOST = "192.168.1.10"
NEW_HOST = "192.168.1.55"

DEVICE_INFO = {"deviceID": MAC, "serialNumber": "000000XX000000"}


def _discovery(ip=NEW_HOST, macaddress="aabbccddeeff"):
    return DhcpServiceInfo(ip=ip, hostname="uponor", macaddress=macaddress)


async def _entry_with_registered_gateway(hass, host=OLD_HOST):
    """Set up the registry state a configured gateway leaves behind."""
    proxy = make_state_proxy(
        hass,
        data={"sys_controller_1_presence": "1", "controller1_id": "419524869"},
        entry_data={CONF_HOST: host},
        unique_id=UNIQUE_ID,
    )
    proxy._client.get_device_info.return_value = dict(DEVICE_INFO)
    await proxy.async_load_device_info()
    proxy._gateway_id = MAC_FORM

    entry = proxy._config_entry
    hass.config_entries.async_update_entry(
        entry, data={CONF_HOST: host}, options={CONF_HOST: host}
    )
    _register_gateway_devices(hass, entry, UNIQUE_ID, proxy)
    return entry, proxy


async def test_gateway_device_records_the_mac_as_a_connection(hass):
    entry, _ = await _entry_with_registered_gateway(hass)

    device = dr.async_get(hass).async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, dr.format_mac(MAC)), entry.entry_id
    )
    assert device is not None, (
        "without a MAC connection HA can never match a DHCP lease to this entry"
    )
    assert (UNIQUE_ID, MAC_FORM) in device.identifiers


async def test_a_new_lease_moves_the_entry_to_the_new_ip(hass):
    entry, _ = await _entry_with_registered_gateway(hass)

    with patch("custom_components.uponorx265.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=_discovery()
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == NEW_HOST
    assert entry.options[CONF_HOST] == NEW_HOST, (
        "options must move too: setup merges data and options with options "
        "winning, so a data-only update is reverted on the next setup"
    )


async def test_a_lease_for_the_current_ip_changes_nothing(hass):
    entry, _ = await _entry_with_registered_gateway(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=_discovery(ip=OLD_HOST)
    )
    await hass.async_block_till_done()

    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == OLD_HOST


async def test_an_unknown_mac_is_ignored(hass):
    await _entry_with_registered_gateway(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=_discovery(macaddress="001122334455"),
    )
    await hass.async_block_till_done()

    assert result["reason"] == "not_uponor_device", (
        "DHCP discovery must never adopt a device this integration does not own"
    )


async def test_a_device_belonging_to_another_integration_is_ignored(hass):
    """The MAC is registered, but not by us - leave it alone."""
    other = MockConfigEntry(domain="other_integration", data={})
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(MAC))},
        identifiers={("other_integration", "whatever")},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=_discovery()
    )
    await hass.async_block_till_done()

    assert result["reason"] == "not_uponor_device"


async def test_no_connection_recorded_when_the_gateway_never_reported_a_mac(hass):
    """A gateway on the host-form fallback must not claim a bogus connection."""
    proxy = make_state_proxy(
        hass,
        data={"sys_controller_1_presence": "1", "controller1_id": "419524869"},
        entry_data={CONF_HOST: OLD_HOST},
        unique_id=UNIQUE_ID,
    )
    proxy._client.get_device_info.return_value = {}
    await proxy.async_load_device_info()

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)

    assert proxy.get_gateway_mac() is None
    device = dr.async_get(hass).async_get_device_by_identifier(
        (UNIQUE_ID, proxy.get_gateway_id()), proxy._config_entry.entry_id
    )
    assert device is not None
    assert device.connections == set()
