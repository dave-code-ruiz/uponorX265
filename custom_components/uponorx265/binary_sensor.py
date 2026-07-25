import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import CONF_BINARY_SENSOR_VALVE
from .helper import get_unique_id_from_config_entry, UponorThermostatEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    if not entry.data.get(CONF_BINARY_SENSOR_VALVE, False):
        return

    unique_id = get_unique_id_from_config_entry(entry)
    state_proxy = hass.data[unique_id]["state_proxy"]

    entities = [
        UponorValveSensor(unique_id, state_proxy, thermostat)
        for thermostat in hass.data[unique_id]["thermostats"]
    ]
    async_add_entities(entities)


class UponorValveSensor(UponorThermostatEntity, BinarySensorEntity):
    """Binary sensor showing whether the valve (actuator) is open for a thermostat."""

    _attr_translation_key = "valve"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_cb_actuator"
        self._attr_device_class = BinarySensorDeviceClass.OPENING
        self._attr_icon = "mdi:radiator"

    @property
    def is_on(self):
        return self._state_proxy.is_active(self._thermostat)
