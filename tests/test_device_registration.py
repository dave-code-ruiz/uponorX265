"""Bug D: gateway/controller devices must exist before platforms build entities.

Thermostat and controller entities declare a `via_device` pointing at their
parent (controller, then gateway), but the parent device was previously only
created as a side effect of a specific entity (a controller status sensor,
gated behind CONF_CREATE_CONTROLLERS, living in a platform that loads after
CLIMATE/SWITCH). _register_gateway_devices() must create both devices up
front, regardless of platform order or which optional entities are enabled.
"""

from homeassistant.helpers import device_registry as dr

from custom_components.uponorx265 import _register_gateway_devices
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
GATEWAY_ID = "AABBCCDDEEFF"
CONTROLLER_ID = "419524869"


async def test_gateway_and_controller_devices_exist_before_platform_setup(hass):
    proxy = make_state_proxy(
        hass,
        data={
            "sys_controller_1_presence": "1",
            "controller1_id": CONTROLLER_ID,
        },
        unique_id=UNIQUE_ID,
    )
    proxy._gateway_id = GATEWAY_ID  # pretend MAC resolution already ran

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)

    dev_reg = dr.async_get(hass)
    gateway_device = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, GATEWAY_ID), proxy._config_entry.entry_id
    )
    controller_device = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, CONTROLLER_ID), proxy._config_entry.entry_id
    )

    assert gateway_device is not None, "gateway device was not registered before platform setup"
    assert controller_device is not None, "controller device was not registered before platform setup"
    assert controller_device.via_device_id == gateway_device.id, (
        "controller device's via_device does not point at the (now-existing) gateway device"
    )


async def test_controller_device_registered_even_when_controller_sensor_disabled(hass):
    # CONF_CREATE_CONTROLLERS is not in entry_data at all here — the optional
    # controller status sensor that used to be the only thing creating this
    # device would never run.
    proxy = make_state_proxy(
        hass,
        data={
            "sys_controller_1_presence": "1",
            "controller1_id": CONTROLLER_ID,
        },
        entry_data={},
        unique_id=UNIQUE_ID,
    )
    proxy._gateway_id = GATEWAY_ID

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, CONTROLLER_ID), proxy._config_entry.entry_id
    ) is not None
