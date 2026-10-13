import inspect
import logging
from homeassistant.helpers.entity import Entity
from homeassistant.core import callback
from homeassistant.helpers import device_registry
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


def _async_get_device_by_identifier(
    dev_reg: device_registry.DeviceRegistry,
    identifier: tuple[str, str],
    config_entry_id: str,
) -> device_registry.DeviceEntry | None:
    """Look up a device by a single identifier, scoped to one config entry.

    `async_get_device_by_identifier` was added in HA 2026.8, replacing
    `async_get_device` — identifiers are no longer unique across config
    entries, so the unscoped lookup is deprecated and breaks in HA 2027.8.

    On cores older than 2026.8 the new method doesn't exist and the
    deprecated one is the only lookup available. Falling back to it is safe
    there: identifiers *were* still unique across entries on those versions,
    and this integration's identifiers are namespaced by the per-entry
    unique_instance_id anyway, so both calls resolve the same device.

    Drop this shim (and call the registry directly) once the integration
    requires 2026.8 or newer.
    """
    if hasattr(dev_reg, "async_get_device_by_identifier"):
        return dev_reg.async_get_device_by_identifier(identifier, config_entry_id)
    return dev_reg.async_get_device(identifiers={identifier})


def _device_config_entry_ids(device: device_registry.DeviceEntry) -> set[str]:
    """Every config entry a device belongs to.

    HA 2026.8 restricted a device to a single config entry and gave it
    `config_entry_id`; HA 2026.10 deprecated the set-valued `config_entries`
    that field replaces, and it breaks in HA 2027.10.

    A set is returned regardless, because one case still has several. A device
    id recorded before 2026.8 for a device that several integrations shared -
    an automation may well still hold one - resolves through `async_get` to a
    read-only composite of the devices it was split into. `config_entries` is
    how core exposes all of a composite's entries, and reading it there is not
    reported as deprecated. Core can only be asked whether a device is a
    composite from 2026.10 on; on 2026.8 and 2026.9 one resolves to its former
    primary entry alone.

    On cores older than 2026.8 `config_entry_id` does not exist and
    `config_entries` is the only field there is.

    Drop this shim (and read `config_entry_id` directly) once the integration
    requires 2026.8 or newer and core no longer has composite devices.
    """
    if hasattr(device, "config_entry_id") and not getattr(
        device, "is_composite_device", False
    ):
        return {device.config_entry_id}
    return device.config_entries


# `via_device` (the parent's identifier tuple) was deprecated in HA 2026.9 in
# favour of `via_device_id` (the parent's registry id), which landed in 2026.8.
# Checking the signature keeps this tied to the parameter actually being asked
# about, rather than to a version number. Drop the fallback — and pass
# via_device_id directly — once the integration requires 2026.8 or newer.
_SUPPORTS_VIA_DEVICE_ID = "via_device_id" in inspect.signature(
    device_registry.DeviceRegistry.async_get_or_create
).parameters


def _via_device_kwargs(
    parent: device_registry.DeviceEntry, parent_identifier: tuple[str, str]
) -> dict:
    """Return the async_get_or_create kwarg that links a child to its parent."""
    if _SUPPORTS_VIA_DEVICE_ID:
        return {"via_device_id": parent.id}
    return {"via_device": parent_identifier}


def _entity_config_entry_id(entity: Entity) -> str | None:
    """The config entry id an entity is being added under, if known.

    `device_info` is read by the entity platform after `add_to_platform_start`
    has attached both hass and the platform, so this resolves for every real
    add; it returns None when a property is read off a bare, unattached
    entity.
    """
    config_entry = getattr(entity.platform, "config_entry", None)
    return config_entry.entry_id if config_entry is not None else None


def _via_device_info(entity: Entity, parent_identifier: tuple[str, str]) -> dict:
    """Return the `device_info` key linking an entity's device to its parent.

    The entity platform forwards `device_info` key-for-key to
    `async_get_or_create`, so a `via_device` here earns the same deprecation
    warning as a direct call would — reported against the platform's
    `async_add_entities` line rather than this module. The replacement key
    wants the parent's registry id, so the parent has to be looked up;
    `_register_gateway_devices` puts the gateway and controller devices in the
    registry before platform setup precisely so that lookup resolves.

    An unresolvable parent yields no key at all rather than a fallback to the
    deprecated one. `via_device_id` naming no device raises `DeviceInfoError`,
    which makes the entity platform drop the entity outright, while the old
    `via_device` only logged and left the link unset — so an unlinked device
    is the closer match to the previous behaviour, and much the lesser
    failure.
    """
    if not _SUPPORTS_VIA_DEVICE_ID:
        return {"via_device": parent_identifier}
    hass = entity.hass
    config_entry_id = _entity_config_entry_id(entity)
    if hass is None or config_entry_id is None:
        return {}
    parent = _async_get_device_by_identifier(
        device_registry.async_get(hass), parent_identifier, config_entry_id
    )
    if parent is None:
        return {}
    return {"via_device_id": parent.id}


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
            **_via_device_info(
                self,
                (self._unique_instance_id, self._state_proxy.get_controller_id(self._controller)),
            ),
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
            **_via_device_info(
                self,
                (self._unique_instance_id, self._state_proxy.get_gateway_id()),
            ),
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