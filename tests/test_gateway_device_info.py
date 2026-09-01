"""The gateway's own firmware comes from a different JNAP action.

`uponorsky/GetAttributes` describes the controllers and thermostats behind the
comm module; it carries nothing about the R-208 itself, which is why the
gateway device page had no firmware. `core/GetDeviceInfo` carries all of it.

Field response from the live gateway (a Wave Pulse on 2.0.6.6), trimmed to the
keys the integration reads:

    deviceID:        AA:BB:CC:DD:EE:FF
    serialNumber:    000000XX000000
    hardwareVersion: 1
    firmwareNumber:  20006006
    firmwareVersion: Sky_smatrixrelease_2_0_6_6_locked

20006006 is the value the Uponor app displays, owner-confirmed.
"""

from homeassistant.helpers import device_registry as dr

from custom_components.uponorx265 import _register_gateway_devices
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
GATEWAY_ID = "AABBCCDDEEFF"

DEVICE_INFO = {
    "deviceID": "AA:BB:CC:DD:EE:FF",
    "serialNumber": "000000XX000000",
    "hardwareVersion": "1",
    "firmwareNumber": 20006006,
    "firmwareVersion": "Sky_smatrixrelease_2_0_6_6_locked",
}


def _make_proxy(hass):
    proxy = make_state_proxy(
        hass,
        data={"sys_controller_1_presence": "1", "controller1_id": "419469805"},
        unique_id=UNIQUE_ID,
    )
    proxy._gateway_id = GATEWAY_ID
    return proxy


async def test_gateway_firmware_matches_the_uponor_app(hass):
    proxy = _make_proxy(hass)
    proxy._client.get_device_info.return_value = dict(DEVICE_INFO)

    await proxy.async_load_device_info()

    assert proxy.get_gateway_sw_version() == "20006006"
    assert proxy.get_gateway_hw_version() == "1"
    assert proxy.get_gateway_serial() == "000000XX000000"


async def test_gateway_device_carries_firmware_and_printed_serial(hass):
    proxy = _make_proxy(hass)
    proxy._client.get_device_info.return_value = dict(DEVICE_INFO)
    await proxy.async_load_device_info()

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)

    dev_reg = dr.async_get(hass)
    gateway = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, GATEWAY_ID), proxy._config_entry.entry_id
    )
    assert gateway.sw_version == "20006006"
    assert gateway.hw_version == "1"
    assert gateway.serial_number == "000000XX000000", (
        "the printed serial should win over the MAC-derived identifier"
    )


async def test_gateway_without_the_core_action_still_sets_up(hass):
    """Not every gateway need answer it; the attribute set is what matters."""
    proxy = _make_proxy(hass)
    proxy._client.get_device_info.side_effect = OSError("no such action")

    await proxy.async_load_device_info()

    assert proxy.get_gateway_sw_version() is None
    assert proxy.get_gateway_hw_version() is None
    # Falls back to the identifier rather than leaving the device serial blank.
    assert proxy.get_gateway_serial() == GATEWAY_ID

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)
    dev_reg = dr.async_get(hass)
    gateway = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, GATEWAY_ID), proxy._config_entry.entry_id
    )
    assert gateway is not None
    assert gateway.model == "R-208"


async def test_device_info_is_fetched_once(hass):
    proxy = _make_proxy(hass)
    proxy._client.get_device_info.return_value = dict(DEVICE_INFO)

    await proxy.async_load_device_info()
    await proxy.async_load_device_info()

    assert proxy._client.get_device_info.await_count == 1
