import logging

from homeassistant.components.switch import SwitchEntity

from homeassistant.exceptions import HomeAssistantError

from .const import PRESET_MANUAL, CONF_SWITCH_SENSOR_AVG, CONF_INSTALLER_SETTINGS
from .helper import (
    get_unique_id_from_config_entry,
    UponorGatewayEntity,
    UponorControllerEntity,
    UponorThermostatEntity,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    unique_id = get_unique_id_from_config_entry(entry)
    state_proxy = hass.data[unique_id]["state_proxy"]

    entities = [AwaySwitch(unique_id, state_proxy), AutoUpdateSwitch(unique_id, state_proxy)]

    if state_proxy.is_cool_available():
        entities.append(CoolSwitch(unique_id, state_proxy))

    installer_settings = entry.data.get(CONF_INSTALLER_SETTINGS, False)

    for thermostat in hass.data[unique_id]["thermostats"]:
        entities.append(LocalOverride(unique_id, state_proxy, thermostat))

        if entry.data.get(CONF_SWITCH_SENSOR_AVG, False):
            entities.append(ClimatControlInAvg(unique_id, state_proxy, thermostat))

        if installer_settings:
            entities.append(BypassEnableSwitch(unique_id, state_proxy, thermostat))

    async_add_entities(entities)


class AwaySwitch(UponorGatewayEntity, SwitchEntity):
    _attr_translation_key = "away"
    _attr_icon = "mdi:home-export-outline"

    def __init__(self, unique_instance_id, state_proxy):
        super().__init__(unique_instance_id, state_proxy)
        self._attr_unique_id = f"{unique_instance_id}_away"

    @property
    def is_on(self):
        return self._state_proxy.is_away()

    async def async_turn_on(self, **kwargs):
        await self._state_proxy.async_set_away(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._state_proxy.async_set_away(False)
        self.async_write_ha_state()


class CoolSwitch(UponorGatewayEntity, SwitchEntity):
    _attr_translation_key = "cool_mode"
    _attr_icon = "mdi:snowflake"

    def __init__(self, unique_instance_id, state_proxy):
        super().__init__(unique_instance_id, state_proxy)
        self._attr_unique_id = f"{unique_instance_id}_cool"

    @property
    def is_on(self):
        return self._state_proxy.is_cool_enabled()

    async def async_turn_on(self, **kwargs):
        await self._state_proxy.async_switch_to_cooling()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._state_proxy.async_switch_to_heating()
        self.async_write_ha_state()


class AutoUpdateSwitch(UponorGatewayEntity, SwitchEntity):
    _attr_translation_key = "auto_update"
    _attr_icon = "mdi:update"

    def __init__(self, unique_instance_id, state_proxy):
        super().__init__(unique_instance_id, state_proxy)
        self._attr_unique_id = f"{unique_instance_id}_auto_update"

    @property
    def is_on(self):
        return self._state_proxy.is_autoupdate()

    async def async_turn_on(self, **kwargs):
        await self._state_proxy.async_set_autoupdate(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._state_proxy.async_set_autoupdate(False)
        self.async_write_ha_state()


class LocalOverride(UponorThermostatEntity, SwitchEntity):
    _attr_translation_key = "local_override"

    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_local_override"

    @property
    def is_on(self):
        return self._state_proxy.get_local_override(self._thermostat)

    async def async_turn_on(self, **kwargs):
        await self._state_proxy.async_local_override(self._thermostat, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._state_proxy.async_local_override(self._thermostat, False)
        self.async_write_ha_state()

class BypassEnableSwitch(UponorControllerEntity, SwitchEntity):
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

    async def async_turn_on(self, **kwargs):
        thermostats = self.hass.data[self._unique_instance_id]["thermostats"]
        active = sum(
            1 for t in thermostats
            if t.split('_')[0] == self._controller and self._state_proxy.get_bypass_enable(t)
        )
        if active >= 2:
            raise HomeAssistantError(
                f"Max 2 bypass zones per controller — {self._controller} already has {active} active"
            )
        await self._state_proxy.async_set_bypass_enable(self._thermostat, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._state_proxy.async_set_bypass_enable(self._thermostat, False)
        self.async_write_ha_state()


class ClimatControlInAvg(UponorThermostatEntity, SwitchEntity):
    _attr_translation_key = "avg_included"
    
    def __init__(self, unique_instance_id, state_proxy, thermostat):
        super().__init__(unique_instance_id, state_proxy, thermostat)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_avg_included"
        self._attr_icon = "mdi:thermometer-check"
        
    @property
    def is_on(self):
        return self._state_proxy.get_inavg(self._thermostat)

    async def async_turn_on(self, **kwargs):
        await self._state_proxy.async_iset_inavg(self._thermostat, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._state_proxy.async_iset_inavg(self._thermostat, False)
        self.async_write_ha_state()    