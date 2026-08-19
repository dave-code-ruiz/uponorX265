import logging
from homeassistant.helpers.entity import Entity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

import socket
import time
from getmac import get_mac_address

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


def get_unique_id_from_config_entry(config_entry: ConfigEntry):
    return config_entry.unique_id

def _get_mac_from_proc_arp(host: str):
    """Parse /proc/net/arp directly.

    Fallback for containerized HA installs (HA OS/Supervised) where the
    'arp'/'ip neighbor' binaries getmac relies on aren't present in the
    container image, even though the kernel ARP table itself is reachable.
    """
    try:
        with open('/proc/net/arp') as f:
            lines = f.readlines()
    except Exception as exc:
        _LOGGER.debug("Could not read /proc/net/arp: %s", exc)
        return None

    _LOGGER.debug("/proc/net/arp contents while looking for %s: %s", host, lines)
    for line in lines[1:]:  # skip header
        fields = line.split()
        if len(fields) >= 4 and fields[0] == host and fields[3] != '00:00:00:00:00:00':
            return fields[3]
    return None


def _get_mac_with_arp_refresh(host: str):
    """Prime the ARP cache by actually sending a packet, then read the MAC address.

    connect() alone on a UDP socket does not guarantee a packet leaves the
    interface, so the ARP cache may never get populated; send() forces it.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        sock.connect((host, 80))
        sock.send(b'\x00')
        sock.close()
        time.sleep(0.2)
        _LOGGER.debug("ARP-priming socket to %s sent successfully", host)
    except Exception as exc:
        _LOGGER.debug("ARP-priming socket to %s failed: %s", host, exc)

    mac = get_mac_address(ip=host, network_request=True)
    _LOGGER.debug("get_mac_address(%s, network_request=True) returned: %s", host, mac)
    if mac is not None:
        return mac

    mac = _get_mac_from_proc_arp(host)
    _LOGGER.debug("_get_mac_from_proc_arp(%s) returned: %s", host, mac)
    return mac
    
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