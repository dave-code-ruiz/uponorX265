import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import CONF_BINARY_SENSOR_VALVE, CONF_CONTROLLER_IO, CONF_INSTALLER_SETTINGS
from .helper import get_unique_id_from_config_entry, UponorThermostatEntity, UponorControllerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    unique_id = get_unique_id_from_config_entry(entry)
    state_proxy = hass.data[unique_id]["state_proxy"]
    entities = []

    if entry.data.get(CONF_BINARY_SENSOR_VALVE, False):
        entities += [
            UponorValveSensor(unique_id, state_proxy, thermostat)
            for thermostat in hass.data[unique_id]["thermostats"]
        ]

    if entry.data.get(CONF_CONTROLLER_IO, False):
        common_pump = state_proxy.get_pump_management() == "1"
        seen_controllers = set()
        for thermostat in hass.data[unique_id]["thermostats"]:
            controller = thermostat.split('_')[0]
            if controller not in seen_controllers:
                seen_controllers.add(controller)
                if not common_pump or controller == "C1":
                    entities.append(ControllerPumpRelaySensor(unique_id, state_proxy, controller))
                entities.append(ControllerBoilerDemandSensor(unique_id, state_proxy, controller))

    if not entry.data.get(CONF_INSTALLER_SETTINGS, False):
        for thermostat in hass.data[unique_id]["thermostats"]:
            entities.append(BypassReadOnlySensor(unique_id, state_proxy, thermostat))

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


class ControllerPumpRelaySensor(UponorControllerEntity, BinarySensorEntity):
    _attr_translation_key = "pump_relay"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, unique_instance_id, state_proxy, controller):
        super().__init__(unique_instance_id, state_proxy, controller)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_controller_id(controller)}_pump_relay"

    @property
    def is_on(self):
        return self._state_proxy.get_pump_relay(self._controller)


class ControllerBoilerDemandSensor(UponorControllerEntity, BinarySensorEntity):
    _attr_translation_key = "boiler_demand"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, unique_instance_id, state_proxy, controller):
        super().__init__(unique_instance_id, state_proxy, controller)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_controller_id(controller)}_boiler_demand"

    @property
    def is_on(self):
        return self._state_proxy.get_boiler_demand(self._controller)


class BypassReadOnlySensor(UponorControllerEntity, BinarySensorEntity):
    _attr_icon = "mdi:valve"
    _attr_is_on = False

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        controller = thermostat.split('_')[0]
        super().__init__(unique_instance_id, state_proxy, controller)
        self._thermostat = thermostat
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_bypass_enable"
        self._attr_name = f"Bypass {state_proxy.get_room_name(thermostat)}"

    @property
    def is_on(self):
        return self._state_proxy.get_bypass_enable(self._thermostat)
