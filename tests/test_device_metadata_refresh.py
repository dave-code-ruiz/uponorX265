"""Device model/firmware must survive a cached startup.

On a cached startup async_setup_entry dispatches the first update as a
background task and proceeds straight to platform setup, so every device_info
is evaluated against an empty _data. The thermostat model survives that (it is
persisted in the discovery metadata and has a cached fallback); the controller
model and every firmware version do not, and because device_info is only read
when an entity is added, they stay blank for the rest of the session.

Observed on the live install: dump_hardware_info reported controller X-265 /
sw_version 1.21 and sixteen T-169s, all read from the same getters the device
pages use. The registry meanwhile held the thermostat models (cached) but
model None for every controller, and sw_version None for everything.
"""

from homeassistant.helpers import device_registry as dr

from custom_components.uponorx265 import (
    _refresh_device_metadata,
    _register_gateway_devices,
)
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
GATEWAY_ID = "AABBCCDDEEFF"
CONTROLLER_ID = "419524869"
THERMOSTAT_ID = "285805788"

# What the gateway reports once a poll has actually completed: a Wave system
# on X265_121.hex, one controller, one T-169 (hw_type 7).
LIVE_DATA = {
    "sys_controller_1_presence": "1",
    "controller1_id": CONTROLLER_ID,
    "cust_SW_version_update": "X265_121.hex",
    "C1_hardware_type": "1",
    "C1_sw_version": "289",
    "C1_thermostat1_id": THERMOSTAT_ID,
    "C1_T1_thermostat_type": "2",
    "C1_T1_hw_type": "7",
    "C1_T1_sw_version": "12",   # owner-confirmed: renders as "C"
}


def _make_proxy(hass, data):
    proxy = make_state_proxy(hass, data=data, unique_id=UNIQUE_ID)
    proxy._gateway_id = GATEWAY_ID  # pretend MAC resolution already ran
    return proxy


def _register_thermostat_device(hass, proxy):
    """Stand in for the device row an entity's device_info creates at setup."""
    dev_reg = dr.async_get(hass)
    return dev_reg.async_get_or_create(
        config_entry_id=proxy._config_entry.entry_id,
        identifiers={(UNIQUE_ID, proxy.get_thermostat_id("C1_T1"))},
        model=proxy.get_thermostat_model("C1_T1"),
        sw_version=proxy.get_version("C1_T1"),
    )


async def test_cached_startup_leaves_metadata_blank(hass):
    """Characterises the bug: registration against empty _data writes nothing."""
    proxy = _make_proxy(hass, data={})
    proxy._storage_metadata = {
        "controllers": ["C1"],
        "controller_ids": {"C1": CONTROLLER_ID},
        "thermostats": ["C1_T1"],
        "ids": {"C1_T1": THERMOSTAT_ID},
        "models": {"C1_T1": "T-169"},
    }

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)
    thermostat_device = _register_thermostat_device(hass, proxy)

    dev_reg = dr.async_get(hass)
    controller_device = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, CONTROLLER_ID), proxy._config_entry.entry_id
    )

    # The cached model comes through, which is why this went unnoticed.
    assert thermostat_device.model == "T-169"
    # Everything read straight from _data does not.
    assert thermostat_device.sw_version is None
    assert controller_device.model is None
    assert controller_device.sw_version is None


async def test_refresh_fills_in_metadata_once_live_data_arrives(hass):
    proxy = _make_proxy(hass, data={})
    proxy._storage_metadata = {
        "controllers": ["C1"],
        "controller_ids": {"C1": CONTROLLER_ID},
        "thermostats": ["C1_T1"],
        "ids": {"C1_T1": THERMOSTAT_ID},
        "models": {"C1_T1": "T-169"},
    }

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)
    _register_thermostat_device(hass, proxy)

    # The background poll lands.
    proxy._data.update(LIVE_DATA)
    _refresh_device_metadata(
        hass, proxy._config_entry, UNIQUE_ID, proxy, ["C1_T1"]
    )

    dev_reg = dr.async_get(hass)
    controller_device = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, CONTROLLER_ID), proxy._config_entry.entry_id
    )
    thermostat_device = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, THERMOSTAT_ID), proxy._config_entry.entry_id
    )

    assert controller_device.model == "X-265"
    assert controller_device.sw_version == "1.21", (
        "controller firmware should match the X265_121.hex image name"
    )
    assert thermostat_device.model == "T-169"
    assert thermostat_device.sw_version is not None, (
        "thermostat firmware is still blank after live data arrived"
    )


async def test_refresh_does_not_blank_a_value_it_cannot_resolve(hass):
    """A later poll missing a variable must not erase what is already correct."""
    proxy = _make_proxy(hass, data=dict(LIVE_DATA))

    _register_gateway_devices(hass, proxy._config_entry, UNIQUE_ID, proxy)
    _register_thermostat_device(hass, proxy)

    # A degraded response: the thermostat rows are gone from _data, but the
    # cached id keeps the device addressable.
    proxy._storage_metadata = {"ids": {"C1_T1": THERMOSTAT_ID}, "models": {}}
    for key in ("C1_T1_thermostat_type", "C1_T1_hw_type", "C1_T1_sw_version"):
        proxy._data.pop(key)

    _refresh_device_metadata(
        hass, proxy._config_entry, UNIQUE_ID, proxy, ["C1_T1"]
    )

    dev_reg = dr.async_get(hass)
    thermostat_device = dev_reg.async_get_device_by_identifier(
        (UNIQUE_ID, THERMOSTAT_ID), proxy._config_entry.entry_id
    )
    assert thermostat_device.model == "T-169"
    assert thermostat_device.sw_version is not None


async def test_refresh_runs_once_per_setup_not_once_per_poll(hass, monkeypatch):
    """The hook is guarded so the 30-second poll does not rewrite the registry."""
    proxy = _make_proxy(hass, data={})
    proxy._client.get_data.return_value = dict(LIVE_DATA)

    calls = []
    monkeypatch.setattr(
        "custom_components.uponorx265._refresh_device_metadata",
        lambda *args, **kwargs: calls.append(args),
    )

    await proxy.async_update()
    assert len(calls) == 1, "first update after setup must reconcile the registry"

    await proxy.async_update()
    await proxy.async_update()
    assert len(calls) == 1, "refresh repeated on a later poll"


async def test_thermostat_firmware_renders_as_uppercase_hex(hass):
    """Owner-confirmed: these thermostats report revision "C", not "c".

    The raw value is hex-encoded the same way the controller's is; only the
    case was wrong, which stayed invisible while controller versions happened
    to be all digits.
    """
    proxy = _make_proxy(hass, data=dict(LIVE_DATA))
    assert proxy.get_version("C1_T1") == "C"


async def test_controller_firmware_still_matches_the_image_name(hass):
    proxy = _make_proxy(hass, data=dict(LIVE_DATA))
    assert proxy.get_controller_version("C1") == "1.21"
