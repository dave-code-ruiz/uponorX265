import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .const import (
    CONF_INSTALLER_SETTINGS,
    FLOOR_LIMIT_STEP,
    FLOOR_MAX_LIMIT_RANGE,
    FLOOR_MIN_LIMIT_RANGE,
)
from .helper import get_unique_id_from_config_entry, UponorThermostatEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    unique_id = get_unique_id_from_config_entry(entry)
    state_proxy = hass.data[unique_id]["state_proxy"]

    entities = []

    if entry.data.get(CONF_INSTALLER_SETTINGS, False):
        for thermostat in hass.data[unique_id]["thermostats"]:
            if state_proxy.has_floor_temperature(thermostat):
                entities.append(FloorMinLimitNumber(unique_id, state_proxy, thermostat))
                entities.append(FloorMaxLimitNumber(unique_id, state_proxy, thermostat))

    async_add_entities(entities)


class UponorFloorLimitNumber(UponorThermostatEntity, NumberEntity):
    """Base for the two writable floor temperature limits of a thermostat."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = FLOOR_LIMIT_STEP
    # A box rather than a slider: this is a floor protection limit, not
    # something to nudge by accident.
    _attr_mode = NumberMode.BOX

    @property
    def available(self):
        return self._state_proxy.is_available() and self._state_proxy.has_floor_temperature(self._thermostat)


class FloorMinLimitNumber(UponorFloorLimitNumber):
    _attr_translation_key = "floor_min_limit"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_floor_min_limit"

    @property
    def native_value(self):
        return self._state_proxy.get_floor_min_limit(self._thermostat)

    @property
    def native_min_value(self):
        return FLOOR_MIN_LIMIT_RANGE[0]

    @property
    def native_max_value(self):
        # Uponor does not let the minimum pass the maximum.
        max_limit = self._state_proxy.get_floor_max_limit(self._thermostat)
        if max_limit is None:
            return FLOOR_MIN_LIMIT_RANGE[1]
        return min(FLOOR_MIN_LIMIT_RANGE[1], max_limit)

    async def async_set_native_value(self, value: float) -> None:
        await self._state_proxy.async_set_floor_min_limit(self._thermostat, value)
        self.async_write_ha_state()


class FloorMaxLimitNumber(UponorFloorLimitNumber):
    _attr_translation_key = "floor_max_limit"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_floor_max_limit"

    @property
    def native_value(self):
        return self._state_proxy.get_floor_max_limit(self._thermostat)

    @property
    def native_min_value(self):
        # Uponor does not let the maximum drop below the minimum.
        min_limit = self._state_proxy.get_floor_min_limit(self._thermostat)
        if min_limit is None:
            return FLOOR_MAX_LIMIT_RANGE[0]
        return max(FLOOR_MAX_LIMIT_RANGE[0], min_limit)

    @property
    def native_max_value(self):
        return FLOOR_MAX_LIMIT_RANGE[1]

    async def async_set_native_value(self, value: float) -> None:
        await self._state_proxy.async_set_floor_max_limit(self._thermostat, value)
        self.async_write_ha_state()
