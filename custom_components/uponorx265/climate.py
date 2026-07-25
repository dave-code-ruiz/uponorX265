import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature
)

from homeassistant.components.climate.const import (
    HVACMode,
    HVACAction,
    PRESET_ECO,
    PRESET_AWAY,
    PRESET_COMFORT,
    ClimateEntityFeature
)

from .const import (
    SIGNAL_UPONOR_STATE_UPDATE,
    DEVICE_MANUFACTURER,
    PRESET_MANUAL,
)
from .helper import get_unique_id_from_config_entry, UponorThermostatEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    unique_id = get_unique_id_from_config_entry(entry)
    state_proxy = hass.data[unique_id]["state_proxy"]

    entities = []
    for thermostat in hass.data[unique_id]["thermostats"]:
        entities.append(UponorClimate(unique_id, state_proxy, thermostat))
    
    if entities:
        async_add_entities(entities, update_before_add=False)


class UponorClimate(UponorThermostatEntity, ClimateEntity):
    _enable_turn_on_off_backwards_compatibility = False
    _attr_name = None  # Main entity — uses device name directly
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_preset_modes = [PRESET_COMFORT, PRESET_ECO, PRESET_AWAY, PRESET_MANUAL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._is_on = True
        self._update_power_state()
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_climate"

    def _update_power_state(self):
        temp_raw = self._state_proxy.get_setpoint_raw(self._thermostat)
        is_cool = self._state_proxy.is_cool_enabled()
        min_temp = self.min_temp
        max_temp = self.max_temp

        if temp_raw is None or is_cool is None or min_temp is None or max_temp is None:
            self._is_on = True
            return

        self._is_on = not ((is_cool and temp_raw >= max_temp) or (not is_cool and temp_raw <= min_temp))


    @callback
    def _update_callback(self):
        self._update_power_state()
        self.async_schedule_update_ha_state(True)

    @property
    def hvac_modes(self):
        return [HVACMode.COOL, HVACMode.OFF] if self._state_proxy.is_cool_enabled() else [HVACMode.HEAT, HVACMode.OFF]

    @property
    def current_humidity(self):
        humidity = self._state_proxy.get_humidity(self._thermostat)
        return humidity if humidity not in (None, 0) else None

    @property
    def current_temperature(self):
        return self._state_proxy.get_temperature(self._thermostat)

    @property
    def target_temperature(self):
        return self._state_proxy.get_setpoint(self._thermostat)

    @property
    def min_temp(self):
        return self._state_proxy.get_min_limit(self._thermostat)

    @property
    def max_temp(self):
        return self._state_proxy.get_max_limit(self._thermostat)

    @property
    def extra_state_attributes(self):
        return {
            'id': self._thermostat,
            'status': self._state_proxy.get_status(self._thermostat),
            'pulse_width_modulation': self._state_proxy.get_pwm(self._thermostat),
            'eco_setback': self._state_proxy.get_eco_setback(self._thermostat),
        }
    
    @property
    def preset_mode(self):
        if self._state_proxy.get_local_override(self._thermostat):
            return PRESET_MANUAL
        if self._state_proxy.is_eco(self._thermostat):
            return PRESET_ECO
        if self._state_proxy.is_away():
            return PRESET_AWAY
        return PRESET_COMFORT

    @property
    def hvac_mode(self):
        if not self._is_on:
            return HVACMode.OFF
        return HVACMode.COOL if self._state_proxy.is_cool_enabled() else HVACMode.HEAT

    @property
    def hvac_action(self):
        if not self._is_on:
            return HVACAction.OFF
        if self._state_proxy.is_active(self._thermostat):
            return HVACAction.COOLING if self._state_proxy.is_cool_enabled() else HVACAction.HEATING
        return HVACAction.IDLE

    async def async_turn_off(self):
        if self._is_on:
            await self._state_proxy.async_turn_off(self._thermostat)
            self._is_on = False

    async def async_turn_on(self):
        if not self._is_on:
            await self._state_proxy.async_turn_on(self._thermostat)
            self._is_on = True

    async def async_set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.OFF and self._is_on:
            await self._state_proxy.async_turn_off(self._thermostat)
            self._is_on = False
        elif hvac_mode in [HVACMode.HEAT, HVACMode.COOL] and not self._is_on:
            await self._state_proxy.async_turn_on(self._thermostat)
            self._is_on = True

    # Support setting preset_mode
    async def async_set_preset_mode(self, preset_mode):
        if preset_mode == PRESET_MANUAL:
            await self._state_proxy.async_local_override(self._thermostat, True)
        else:
            # Turn off manual override if we switch to another preset
            if self._state_proxy.get_local_override(self._thermostat):
                await self._state_proxy.async_local_override(self._thermostat, False)
            if preset_mode == PRESET_ECO:
                if self._state_proxy.is_away():
                    await self._state_proxy.async_set_preset_mode(PRESET_COMFORT)
                else:
                    await self._state_proxy.async_set_preset_mode(PRESET_AWAY)
            else:
                await self._state_proxy.async_set_preset_mode(preset_mode)

    async def async_set_temperature(self, **kwargs):
        if not self._state_proxy.get_local_override(self._thermostat):
            self.hass.async_create_task(self._state_proxy.async_update())
            raise ServiceValidationError(
                translation_domain="uponorx265",
                translation_key="temperature_not_controllable",
                translation_placeholders={"room_name": self._room_name},
            )
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None and self._is_on:
            await self._state_proxy.async_set_setpoint(self._thermostat, temp)
