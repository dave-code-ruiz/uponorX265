"""BypassEnableSwitch must enforce Uponor's own limit of max 2 active bypass
zones per controller (confirmed against the official app's own documented
limit, see ARCHITECTURE.md), and must count per-controller, not globally.
"""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.uponorx265.switch import BypassEnableSwitch

from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
C1_THERMOSTATS = ["C1_T1", "C1_T2", "C1_T3"]
C2_THERMOSTATS = ["C2_T1"]


def make_switch(hass, proxy, thermostat, all_thermostats):
    hass.data[UNIQUE_ID] = {"thermostats": all_thermostats}
    entity = BypassEnableSwitch(UNIQUE_ID, proxy, thermostat)
    entity.hass = hass
    entity.async_write_ha_state = lambda: None  # not added to a platform in this test
    return entity


async def test_third_bypass_on_same_controller_is_rejected(hass):
    proxy = make_state_proxy(hass, unique_id=UNIQUE_ID)
    proxy._data["C1_T1_bypass_enable"] = "1"
    proxy._data["C1_T2_bypass_enable"] = "1"

    entity = make_switch(hass, proxy, "C1_T3", C1_THERMOSTATS + C2_THERMOSTATS)

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    proxy._client.send_data.assert_not_called()


async def test_second_bypass_on_same_controller_is_allowed(hass):
    proxy = make_state_proxy(hass, unique_id=UNIQUE_ID)
    proxy._data["C1_T1_bypass_enable"] = "1"

    entity = make_switch(hass, proxy, "C1_T2", C1_THERMOSTATS + C2_THERMOSTATS)

    await entity.async_turn_on()

    proxy._client.send_data.assert_called_once_with({"C1_T2_bypass_enable": "1"})


async def test_limit_is_per_controller_not_global(hass):
    # C1 already has 2 active bypasses; C2 has none. Enabling on C2 must not
    # be blocked by C1's count.
    proxy = make_state_proxy(hass, unique_id=UNIQUE_ID)
    proxy._data["C1_T1_bypass_enable"] = "1"
    proxy._data["C1_T2_bypass_enable"] = "1"

    entity = make_switch(hass, proxy, "C2_T1", C1_THERMOSTATS + C2_THERMOSTATS)

    await entity.async_turn_on()

    proxy._client.send_data.assert_called_once_with({"C2_T1_bypass_enable": "1"})
