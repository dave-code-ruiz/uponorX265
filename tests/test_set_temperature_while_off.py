"""Bug C: setting a temperature on an off thermostat must not be silently discarded.

The old code guarded async_set_target_temperature behind `self._is_on` with
no else branch: calling climate.set_temperature on an off room returned
success and changed nothing, with no error and no way for the caller to
detect it. Combined with Bug B, this is what stranded rooms off permanently
in the field report — the follow-up set_temperature that would have rescued
a poisoned room was silently dropped.
"""

from custom_components.uponorx265.climate import UponorClimate
from homeassistant.const import ATTR_TEMPERATURE

from tests.helpers import make_state_proxy, thermostat_data

THERMOSTAT = "C1_T1"


async def test_set_temperature_while_off_is_remembered_not_discarded(hass):
    data = thermostat_data(THERMOSTAT, setpoint_c=19.5)
    proxy = make_state_proxy(hass, data=data)

    entity = UponorClimate("uponorx265_test", proxy, THERMOSTAT)
    entity.hass = hass
    entity._is_on = False  # room is currently off

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 21.0})

    # Must not have written the live setpoint (that would silently turn the
    # room back on, violating hvac_mode: off).
    proxy._client.send_data.assert_not_called()

    # The requested target must be recorded so turn_on restores it.
    await proxy.async_load_storage()
    assert proxy._storage_data.get(THERMOSTAT) == 21.0


async def test_set_temperature_while_on_still_writes_the_live_setpoint(hass):
    data = thermostat_data(THERMOSTAT, setpoint_c=19.5)
    proxy = make_state_proxy(hass, data=data)

    entity = UponorClimate("uponorx265_test", proxy, THERMOSTAT)
    entity.hass = hass
    entity._is_on = True

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 21.0})

    proxy._client.send_data.assert_called_once()
