import logging

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTemperature, PERCENTAGE
from homeassistant.helpers.entity import EntityCategory

from .const import STATUS_OK, CONF_CREATE_CONTROLLERS, CONF_SENSOR_TEMP, CONF_INSTALLER_SETTINGS
from .helper import get_unique_id_from_config_entry, UponorGatewayEntity, UponorThermostatEntity, UponorControllerEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    unique_id = get_unique_id_from_config_entry(entry)
    _LOGGER.debug(f"unique id {unique_id} entety {entry} data = {entry.data}")
    state_proxy = hass.data[unique_id]["state_proxy"]

    entities = []

    # Gateway diagnostic sensor (one per integration)
    entities.append(UponorGatewayStatusSensor(unique_id, state_proxy))

    create_controllers = entry.data.get(CONF_CREATE_CONTROLLERS, True)
    create_temp_sensor = entry.data.get(CONF_SENSOR_TEMP, True)
    installer_settings = entry.data.get(CONF_INSTALLER_SETTINGS, False)

    seen_controllers = set()
    for thermostat in hass.data[unique_id]["thermostats"]:
        controller = thermostat.split('_')[0]
        if controller not in seen_controllers:
            seen_controllers.add(controller)
            if create_controllers:
                entities.append(UponorRoomAvg(unique_id, state_proxy, controller))
                entities.append(UponorControllerStatusSensor(unique_id, state_proxy, controller))
            if not installer_settings:
                entities.append(UponorControllerRelayConfigSensor(unique_id, state_proxy, controller))

    if "C1" in seen_controllers and not installer_settings:
        entities.append(UponorPumpManagementSensor(unique_id, state_proxy))

    for thermostat in hass.data[unique_id]["thermostats"]:
        room_name = state_proxy.get_room_name(thermostat)
        _LOGGER.debug(f"Adding sensors for {room_name} (thermostat ID: {thermostat})")
        if create_temp_sensor:
            entities.append(UponorRoomCurrentTemperatureSensor(unique_id, state_proxy, thermostat))
        entities.append(UponorThermostatStatusSensor(unique_id, state_proxy, thermostat))

        if state_proxy.has_floor_temperature(thermostat):
            entities.append(UponorFloorTemperatureSensor(unique_id, state_proxy, thermostat))
            _LOGGER.debug(f"Added floor sensor for: {room_name}")

        if state_proxy.has_humidity_sensor(thermostat):
            entities.append(UponorHumiditySensor(unique_id, state_proxy, thermostat))
            _LOGGER.debug(f"Added humidity sensor for: {room_name}")

    _LOGGER.debug(f"Total number of sensors added: {len(entities)}")
    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Diagnostic sensors
# ---------------------------------------------------------------------------

class UponorThermostatStatusSensor(UponorThermostatEntity, SensorEntity):
    """Diagnostic sensor showing alarm/error status for a single thermostat."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "thermostat_status"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_status"

    @property
    def native_value(self):
        return self._state_proxy.get_status(self._thermostat)

    @property
    def icon(self):
        status = self._state_proxy.get_status(self._thermostat)
        return "mdi:check-circle-outline" if status == STATUS_OK else "mdi:alert-circle"

class UponorControllerStatusSensor(UponorControllerEntity,SensorEntity):
    """Diagnostic sensor showing communication status for a controller."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "controller_status"

    def __init__(self, unique_instance_id, state_proxy, controller):
        super().__init__(unique_instance_id, state_proxy, controller)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_controller_id(controller)}_status"

    @property
    def native_value(self):
        return self._state_proxy.get_controller_status(self._controller)

    @property
    def icon(self):
        return "mdi:check-circle-outline" if self._state_proxy.is_available() else "mdi:alert-circle"


class UponorGatewayStatusSensor(UponorGatewayEntity, SensorEntity):
    """Diagnostic sensor showing online/offline status for the Uponor gateway."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_available = True  # Always available so the sensor can show "Offline"
    _attr_translation_key = "gateway_status"

    def __init__(self, unique_instance_id, state_proxy):
        super().__init__(unique_instance_id, state_proxy)
        # Deliberately free of the gateway id. There is one gateway per config
        # entry, so the instance id alone is unique - and the resolved gateway
        # id is volatile (MAC when it resolves, host-based when it does not),
        # which used to change this unique_id underneath the registry and
        # strand the old entity as sensor.uponor_gateway_status_2.
        self._attr_unique_id = f"{self._unique_instance_id}_gateway_status"

    @property
    def native_value(self):
        return self._state_proxy.get_gateway_status()

    @property
    def icon(self):
        return "mdi:lan-connect" if self._state_proxy.is_available() else "mdi:lan-disconnect"

# ---------------------------------------------------------------------------
# Regular measurement sensors
# ---------------------------------------------------------------------------

class UponorFloorTemperatureSensor(UponorThermostatEntity, SensorEntity):
    """Sensor showing floor temperature for a single thermostat."""

    _attr_translation_key = "floor_temp"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_floor_temp"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self):
        return self._state_proxy.is_available() and self._state_proxy.has_floor_temperature(self._thermostat)

    @property
    def native_value(self):
        return self._state_proxy.get_floor_temperature(self._thermostat)

class UponorRoomCurrentTemperatureSensor(UponorThermostatEntity, SensorEntity):
    """Sensor showing current room temperature for a single thermostat."""

    _attr_translation_key = "room_temp"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_current_temp"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return self._state_proxy.get_temperature(self._thermostat)

class UponorHumiditySensor(UponorThermostatEntity, SensorEntity):
    _attr_translation_key = "humidity"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_rh"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self):
        """Return True if the sensor is available."""
        return self._state_proxy.is_available() and self._state_proxy.has_humidity_sensor(self._thermostat)

    @property
    def native_value(self):
        return self._state_proxy.get_humidity(self._thermostat)

class UponorRoomAvg(UponorControllerEntity, SensorEntity):
    _attr_translation_key = "room_avg_temp"

    def __init__(self, unique_instance_id, state_proxy, controller):
        super().__init__(unique_instance_id, state_proxy, controller)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_controller_id(self._controller)}_average_room_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return self._state_proxy.get_controller_avgtemp(self._controller)


class UponorControllerRelayConfigSensor(UponorControllerEntity, SensorEntity):
    _attr_translation_key = "relay_config"

    def __init__(self, unique_instance_id, state_proxy, controller):
        super().__init__(unique_instance_id, state_proxy, controller)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_controller_id(controller)}_relay_config"

    @property
    def native_value(self):
        return self._state_proxy.get_controller_relayconfig(self._controller)


class UponorPumpManagementSensor(UponorControllerEntity, SensorEntity):
    _attr_translation_key = "pump_management"

    def __init__(self, unique_instance_id, state_proxy):
        super().__init__(unique_instance_id, state_proxy, "C1")
        self._attr_unique_id = f"{unique_instance_id}_pump_management"

    @property
    def native_value(self):
        raw = self._state_proxy.get_pump_management()
        return {"0": "individual", "1": "common"}.get(raw)
