import logging
from homeassistant.helpers.entity import Entity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    DOMAIN,
    CONF_UNIQUE_ID,
    SIGNAL_UPONOR_STATE_UPDATE,
    DEVICE_MANUFACTURER,
)

from homeassistant.config_entries import ConfigEntry

from homeassistant.const import (
    CONF_NAME
)

_LOGGER = logging.getLogger(__name__)

def create_unique_id_from_user_input(user_input):
    if CONF_UNIQUE_ID in user_input and user_input[CONF_UNIQUE_ID] != "":
        return user_input[CONF_UNIQUE_ID]
    return None


def generate_unique_id_from_user_input_conf_name(user_input):
    conf_name = user_input[CONF_NAME]
    raw_unique_id = DOMAIN + "_" + conf_name
    return raw_unique_id.replace(" ", "_").lower()


def _async_get_devices_by_connection(dev_reg, connection: tuple[str, str]):
    """Every device carrying a connection, across all config entries.

    `async_get_devices` was added in HA 2026.8, when identifiers and
    connections stopped being unique across config entries and the single-match
    `async_get_device` was deprecated (it raises from 2027.8).

    The list form is what is wanted here regardless of the deprecation: a MAC
    can legitimately be registered by several integrations at once - a router,
    a device tracker - and only the caller can say which of those devices it
    owns. The deprecated single-match lookup would resolve that ambiguity on
    its own and could hand back somebody else's device.

    On cores older than 2026.8 the new method does not exist and the
    deprecated one is the only lookup available; there, connections really
    were unique, so a one-item list is the same answer.

    Drop this shim once the integration requires 2026.8 or newer.
    """
    if hasattr(dev_reg, "async_get_devices"):
        return dev_reg.async_get_devices(connections={connection})
    device = dev_reg.async_get_device(connections={connection})
    return [device] if device is not None else []


def get_unique_id_from_config_entry(config_entry: ConfigEntry):
    return config_entry.unique_id

class UponorThermostatEntity(Entity):
    """Base class for entity connected to termostat."""

    _attr_has_entity_name = True

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        self._unique_instance_id = unique_instance_id
        self._state_proxy = state_proxy
        self._thermostat = thermostat
        self._controller = thermostat.split('_')[0]
        self._controller_name = state_proxy.get_controller_name(self._controller)
        self._room_name = state_proxy.get_room_name(self._thermostat)

    @property
    def device_info(self):
        return {
            "identifiers": {(self._unique_instance_id, self._state_proxy.get_thermostat_id(self._thermostat))},
            "name": self._state_proxy.get_room_name(self._thermostat),
            "manufacturer": DEVICE_MANUFACTURER,
            "model": self._state_proxy.get_thermostat_model(self._thermostat),
            "sw_version": self._state_proxy.get_version(self._thermostat),
            "serial_number": self._state_proxy.get_thermostat_id(self._thermostat),
            "via_device": (self._unique_instance_id, self._state_proxy.get_controller_id(self._controller)),
        }

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self._state_proxy.is_available()

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPONOR_STATE_UPDATE, self._update_callback)
        )

    @callback
    def _update_callback(self):
        """Update sensor state. when data updates"""
        _LOGGER.debug(f"Updating state for {self._attr_unique_id}")
        self.async_schedule_update_ha_state(True)

class UponorControllerEntity(Entity):
    """Diagnostic sensor showing communication status for a controller."""

    _attr_has_entity_name = True

    def __init__(self, unique_instance_id, state_proxy, controller):
        self._unique_instance_id = unique_instance_id
        self._state_proxy = state_proxy
        self._controller = controller
        self._controller_name = state_proxy.get_controller_name(controller)

    @property
    def device_info(self):
        return {
            "identifiers": {(self._unique_instance_id, self._state_proxy.get_controller_id(self._controller))},
            "name": self._controller_name,
            "manufacturer": DEVICE_MANUFACTURER,
            "model": self._state_proxy.get_controller_hardware(self._controller),
            "sw_version": self._state_proxy.get_controller_version(self._controller),
            "serial_number": self._state_proxy.get_controller_id(self._controller),
            "via_device": (self._unique_instance_id, self._state_proxy.get_gateway_id()),
        }

    @property
    def available(self):
        return self._state_proxy.is_available()

    @property
    def should_poll(self):
        return False

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPONOR_STATE_UPDATE, self._update_callback)
        )

    @callback
    def _update_callback(self):
        self.async_schedule_update_ha_state(True)
        
class UponorGatewayEntity(Entity):
    """Base class for entity connected to gatewayen."""

    _attr_has_entity_name = True

    def __init__(self, unique_instance_id, state_proxy):
        self._unique_instance_id = unique_instance_id
        self._state_proxy = state_proxy
        self._gateway_id = self._state_proxy.get_gateway_id()

        
    @property
    def device_info(self):
        return {
            "identifiers": {(self._unique_instance_id, self._gateway_id)},
            "name": self._state_proxy.get_integration_name(),
            "manufacturer": DEVICE_MANUFACTURER,
            "model": self._state_proxy.get_model(),
            "serial_number": self._gateway_id,
        }

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self._state_proxy.is_available()

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPONOR_STATE_UPDATE, self._update_callback)
        )

    @callback
    def _update_callback(self):
        self.async_schedule_update_ha_state(True)