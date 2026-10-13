"""The integration must not read the deprecated `DeviceEntry.config_entries`.

HA 2026.8 restricted a device to a single config entry, `config_entry_id`.
HA 2026.10 deprecated the set-valued `config_entries` it replaces, and the
field breaks in HA 2027.10:

    Detected that custom integration 'uponorx265' accesses
    `DeviceEntry.config_entries`, which is deprecated because a device belongs
    to a single config entry; use `DeviceEntry.config_entry_id` instead at
    custom_components/uponorx265/config_flow.py, line 74

Two places read it, both to get from a device back to the gateway that owns
it: DHCP IP-recovery, which runs whenever a lease for a known gateway is seen
and is what logged the line above, and `uponorx265.set_variable` when it is
given a `device_id`. Both now go through `_device_config_entry_ids()`.

The read is caught by replacing the property and recording who asks for it,
rather than by watching the log. That works on every core, including the
ones older than 2026.10 where the read is still silent, and it does not
depend on `report_usage` recognising the caller: it softens to a log line
only for a frame it can place inside a custom integration, and raises
otherwise.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_DHCP
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.uponorx265 as uponorx265
from custom_components.uponorx265 import _resolve_target_proxies
from custom_components.uponorx265.const import DOMAIN
from custom_components.uponorx265.helper import _device_config_entry_ids
from tests.helpers import make_state_proxy

PACKAGE_DIR = Path(uponorx265.__file__).parent

UNIQUE_ID = "uponorx265_uponor"
MAC = "AA:BB:CC:DD:EE:FF"
MAC_FORM = "AABBCCDDEEFF"
OLD_HOST = "192.168.1.10"
NEW_HOST = "192.168.1.55"


@pytest.fixture
def deprecated_reads(monkeypatch):
    """Where the integration read `DeviceEntry.config_entries` from, in order.

    Only reads made from the integration's own modules are recorded; core is
    free to use its own field.
    """
    reads = []

    def config_entries(self):
        caller = sys._getframe(1).f_code
        if Path(caller.co_filename).parent == PACKAGE_DIR:
            reads.append(f"{Path(caller.co_filename).name}:{caller.co_name}")
        return {self.config_entry_id}

    monkeypatch.setattr(dr.DeviceEntry, "config_entries", property(config_entries))
    return reads


def _gateway(hass, unique_id):
    """A loaded gateway: its entry, its state proxy and its gateway device."""
    proxy = make_state_proxy(hass, unique_id=unique_id)
    entry = proxy._config_entry
    hass.data[unique_id] = {"state_proxy": proxy}
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(unique_id, "gateway")},
    )
    return proxy, device


def _call(*device_ids):
    return SimpleNamespace(data={ATTR_DEVICE_ID: list(device_ids)})


async def test_dhcp_recovery_does_not_read_the_deprecated_field(hass, deprecated_reads):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: OLD_HOST},
        options={CONF_HOST: OLD_HOST},
        unique_id=UNIQUE_ID,
    )
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(UNIQUE_ID, MAC_FORM)},
        connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(MAC))},
    )

    with patch("custom_components.uponorx265.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data=DhcpServiceInfo(
                ip=NEW_HOST, hostname="uponor", macaddress="aabbccddeeff"
            ),
        )
        await hass.async_block_till_done()

    # The recovery itself still has to work, or an empty list proves nothing.
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == NEW_HOST
    assert deprecated_reads == []


async def test_a_device_id_targets_the_gateway_it_belongs_to(hass, deprecated_reads):
    first_proxy, first_device = _gateway(hass, "uponorx265_first")
    second_proxy, second_device = _gateway(hass, "uponorx265_second")

    assert _resolve_target_proxies(hass, _call(second_device.id)) == [second_proxy]
    assert _resolve_target_proxies(hass, _call(first_device.id, second_device.id)) == [
        first_proxy,
        second_proxy,
    ]
    assert deprecated_reads == []


async def test_a_device_of_another_integration_targets_nothing(hass, deprecated_reads):
    _gateway(hass, "uponorx265_first")
    other = MockConfigEntry(domain="other_integration", data={})
    other.add_to_hass(hass)
    foreign_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other_integration", "whatever")},
    )

    assert _resolve_target_proxies(hass, _call(foreign_device.id)) == []
    assert deprecated_reads == []


async def test_a_plain_device_reports_its_single_entry(hass, deprecated_reads):
    _, device = _gateway(hass, "uponorx265_first")

    assert _device_config_entry_ids(device) == {device.config_entry_id}
    assert deprecated_reads == []


# The two cases below cannot be produced from a current registry: one needs a
# core older than 2026.8, the other a device id that predates the split. Each
# stand-in carries exactly the fields core gives such a device.


def test_a_core_without_config_entry_id_falls_back_to_config_entries():
    device = SimpleNamespace(config_entries={"first", "second"})

    assert _device_config_entry_ids(device) == {"first", "second"}


def test_a_composite_device_keeps_every_entry_it_spans():
    """`config_entry_id` alone would name only the composite's former primary."""
    device = SimpleNamespace(
        config_entry_id="first",
        is_composite_device=True,
        config_entries={"first", "second"},
    )

    assert _device_config_entry_ids(device) == {"first", "second"}
