"""Floor temperature limits (`C?_T?_minimum_floor_setpoint` / `_maximum_floor_setpoint`).

The gateway reports both for every thermostat, floor probe or not: a unit
without one simply carries the defaults. So the values cannot say whether a
limit is in force, and the entities are gated on the probe instead - the same
`has_floor_temperature()` that gates the floor temperature sensor.

Like the other installer mode features they come in two forms sharing one
unique_id: read-only `sensor`s with the flag off, writable `number`s with it
on. The writable form carries Uponor's own rules, since the gateway reports no
range: 10-30 C for the minimum, 20-35 C for the maximum, and neither may
cross the other.

The raw values below are from a real Wave system: fifteen T-169s at the
680/788 defaults, and the one with a floor probe raised to 860 from the
Uponor app.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265.const import CONF_INSTALLER_SETTINGS, DOMAIN
from custom_components.uponorx265.number import FloorMaxLimitNumber, FloorMinLimitNumber
from tests.helpers import make_state_proxy, raw_for

UNIQUE_ID = "uponorx265_test"
HOST = "192.168.1.10"
PROBED = "C1_T1"
PROBED_ID = "286000001"
BARE = "C1_T2"
BARE_ID = "286000002"

LIVE_DATA = {
    "sys_controller_1_presence": "1",
    "controller1_id": "419000001",
    "cust_SW_version_update": "X265_121.hex",
    "C1_thermostat_1_presence": "1",
    "C1_thermostat1_id": PROBED_ID,
    f"{PROBED}_external_temperature": "808",
    f"{PROBED}_minimum_floor_setpoint": "680",
    f"{PROBED}_maximum_floor_setpoint": "860",
    "C1_thermostat_2_presence": "1",
    "C1_thermostat2_id": BARE_ID,
    f"{BARE}_external_temperature": "32767",
    f"{BARE}_minimum_floor_setpoint": "680",
    f"{BARE}_maximum_floor_setpoint": "788",
}


@pytest.fixture(autouse=True)
def _no_background_noise():
    """Keep the poll timer out of these tests."""
    with patch("custom_components.uponorx265.async_track_time_interval",
               return_value=lambda: None):
        yield


async def _setup(hass, installer_settings):
    """Set an entry up for real against a mocked gateway; returns its JNAP client."""
    data = {CONF_HOST: HOST, "name": "Uponor", CONF_INSTALLER_SETTINGS: installer_settings}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=data, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)

    client = AsyncMock()
    client.get_data.return_value = dict(LIVE_DATA)
    client.get_device_info.return_value = {}
    with patch("custom_components.uponorx265.UponorJnap", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    return client


def _state(hass, domain, thermostat_id, suffix):
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{UNIQUE_ID}_{thermostat_id}_{suffix}"
    )
    return hass.states.get(entity_id) if entity_id else None


def _limits(thermostat, min_c, max_c):
    return {
        f"{thermostat}_minimum_floor_setpoint": str(raw_for(min_c)),
        f"{thermostat}_maximum_floor_setpoint": str(raw_for(max_c)),
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

async def test_limits_are_decoded_like_every_other_temperature(hass):
    proxy = make_state_proxy(hass, data=LIVE_DATA)

    assert proxy.get_floor_min_limit(PROBED) == 20.0
    assert proxy.get_floor_max_limit(PROBED) == 30.0


async def test_a_thermostat_without_a_probe_still_reports_the_defaults(hass):
    """Why the values themselves cannot gate the entities."""
    proxy = make_state_proxy(hass, data=LIVE_DATA)

    assert proxy.has_floor_temperature(BARE) is False
    assert proxy.get_floor_min_limit(BARE) == 20.0
    assert proxy.get_floor_max_limit(BARE) == 26.0


async def test_missing_variables_are_none_rather_than_an_error(hass):
    proxy = make_state_proxy(hass, data={})

    assert proxy.get_floor_min_limit(PROBED) is None
    assert proxy.get_floor_max_limit(PROBED) is None


async def test_installer_mode_off_shows_the_limits_as_sensors(hass):
    await _setup(hass, installer_settings=False)

    assert _state(hass, "sensor", PROBED_ID, "floor_temp").state == "27.1"
    assert _state(hass, "sensor", PROBED_ID, "floor_min_limit").state == "20.0"
    assert _state(hass, "sensor", PROBED_ID, "floor_max_limit").state == "30.0"
    assert _state(hass, "number", PROBED_ID, "floor_min_limit") is None
    assert _state(hass, "number", PROBED_ID, "floor_max_limit") is None

    for domain in ("sensor", "number"):
        assert _state(hass, domain, BARE_ID, "floor_min_limit") is None
        assert _state(hass, domain, BARE_ID, "floor_max_limit") is None


async def test_installer_mode_on_shows_the_limits_as_numbers(hass):
    await _setup(hass, installer_settings=True)

    assert _state(hass, "number", PROBED_ID, "floor_min_limit").state == "20.0"
    assert _state(hass, "number", PROBED_ID, "floor_max_limit").state == "30.0"
    assert _state(hass, "sensor", PROBED_ID, "floor_min_limit") is None
    assert _state(hass, "sensor", PROBED_ID, "floor_max_limit") is None
    # The reading itself is not a setting, so it stays a sensor either way.
    assert _state(hass, "sensor", PROBED_ID, "floor_temp").state == "27.1"

    for domain in ("sensor", "number"):
        assert _state(hass, domain, BARE_ID, "floor_min_limit") is None
        assert _state(hass, domain, BARE_ID, "floor_max_limit") is None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

async def test_setting_a_limit_sends_the_raw_value(hass):
    proxy = make_state_proxy(hass, data=LIVE_DATA)

    await proxy.async_set_floor_max_limit(PROBED, 29.5)
    await proxy.async_set_floor_min_limit(PROBED, 18.0)

    sent = [call.args[0] for call in proxy._client.send_data.await_args_list]
    assert sent == [
        {f"{PROBED}_maximum_floor_setpoint": "851"},
        {f"{PROBED}_minimum_floor_setpoint": "644"},
    ]
    # Shown at once, without waiting for the next poll to read it back.
    assert proxy.get_floor_max_limit(PROBED) == 29.5
    assert proxy.get_floor_min_limit(PROBED) == 18.0


async def test_the_ranges_are_uponors(hass):
    proxy = make_state_proxy(hass, data={**LIVE_DATA, **_limits(PROBED, 20.0, 30.0)})
    minimum = FloorMinLimitNumber(UNIQUE_ID, proxy, PROBED)
    maximum = FloorMaxLimitNumber(UNIQUE_ID, proxy, PROBED)

    assert (minimum.native_min_value, minimum.native_max_value) == (10, 30)
    assert (maximum.native_min_value, maximum.native_max_value) == (20, 35)
    assert minimum.native_step == maximum.native_step == 0.5


async def test_neither_limit_may_cross_the_other(hass):
    proxy = make_state_proxy(hass, data={**LIVE_DATA, **_limits(PROBED, 22.0, 28.0)})
    minimum = FloorMinLimitNumber(UNIQUE_ID, proxy, PROBED)
    maximum = FloorMaxLimitNumber(UNIQUE_ID, proxy, PROBED)

    assert minimum.native_max_value == 28.0, "the minimum may not pass the maximum"
    assert maximum.native_min_value == 22.0, "the maximum may not drop below the minimum"


async def test_the_ranges_survive_a_gateway_that_has_not_answered_yet(hass):
    """A cached start builds the entities before any live data exists."""
    proxy = make_state_proxy(hass, data={})

    assert FloorMinLimitNumber(UNIQUE_ID, proxy, PROBED).native_max_value == 30
    assert FloorMaxLimitNumber(UNIQUE_ID, proxy, PROBED).native_min_value == 20


async def test_number_set_value_reaches_the_gateway(hass):
    client = await _setup(hass, installer_settings=True)
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{UNIQUE_ID}_{PROBED_ID}_floor_max_limit"
    )

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 28.5}, blocking=True
    )

    client.send_data.assert_awaited_once_with({f"{PROBED}_maximum_floor_setpoint": "833"})
    assert hass.states.get(entity_id).state == "28.5"


async def test_a_value_past_the_other_limit_is_refused_before_it_is_sent(hass):
    client = await _setup(hass, installer_settings=True)
    reg = er.async_get(hass)
    minimum = reg.async_get_entity_id("number", DOMAIN, f"{UNIQUE_ID}_{PROBED_ID}_floor_min_limit")
    maximum = reg.async_get_entity_id("number", DOMAIN, f"{UNIQUE_ID}_{PROBED_ID}_floor_max_limit")

    await hass.services.async_call(
        "number", "set_value", {"entity_id": minimum, "value": 24}, blocking=True
    )
    client.send_data.reset_mock()

    # 22 is inside Uponor's 20-35 range for a maximum, so only the minimum
    # that was just set stands in its way.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": maximum, "value": 22}, blocking=True
        )

    client.send_data.assert_not_awaited()
