import logging

from homeassistant.components.select import SelectEntity

from .const import CONF_INSTALLER_SETTINGS
from .helper import get_unique_id_from_config_entry, UponorControllerEntity

_LOGGER = logging.getLogger(__name__)

PUMP_MANAGEMENT_OPTIONS = {"0": "individual", "1": "common"}

RELAY_CONFIG_OPTIONS = {
    "1": "not_in_use",
    "3": "pump_heater",
    "4": "pump_eco_comfort",
    "7": "not_configured",
}


async def async_setup_entry(hass, entry, async_add_entities):
    unique_id = get_unique_id_from_config_entry(entry)
    state_proxy = hass.data[unique_id]["state_proxy"]

    controllers = list(dict.fromkeys(
        t.split('_')[0] for t in hass.data[unique_id]["thermostats"]
    ))

    entities = []
    installer_settings = entry.data.get(CONF_INSTALLER_SETTINGS, False)

    if "C1" in controllers and installer_settings:
        entities.append(PumpManagementSelect(unique_id, state_proxy))

    if entry.data.get(CONF_INSTALLER_SETTINGS, False):
        for controller in controllers:
            entities.append(ControllerRelayConfigSelect(unique_id, state_proxy, controller))

    async_add_entities(entities)


class PumpManagementSelect(UponorControllerEntity, SelectEntity):
    _attr_translation_key = "pump_management"
    _attr_options = list(PUMP_MANAGEMENT_OPTIONS.values())

    def __init__(self, unique_instance_id, state_proxy):
        super().__init__(unique_instance_id, state_proxy, "C1")
        self._attr_unique_id = f"{unique_instance_id}_pump_management"

    @property
    def current_option(self):
        raw = self._state_proxy.get_pump_management()
        return PUMP_MANAGEMENT_OPTIONS.get(raw)

    async def async_select_option(self, option: str) -> None:
        raw = next(k for k, v in PUMP_MANAGEMENT_OPTIONS.items() if v == option)
        await self._state_proxy.async_set_pump_management(raw)
        self.async_write_ha_state()


class ControllerRelayConfigSelect(UponorControllerEntity, SelectEntity):
    _attr_translation_key = "relay_config"
    _attr_options = list(RELAY_CONFIG_OPTIONS.values())

    def __init__(self, unique_instance_id, state_proxy, controller):
        super().__init__(unique_instance_id, state_proxy, controller)
        self._attr_unique_id = f"{unique_instance_id}_{state_proxy.get_controller_id(controller)}_relay_config"

    @property
    def current_option(self):
        return self._state_proxy.get_controller_relayconfig(self._controller)

    async def async_select_option(self, option: str) -> None:
        raw = next(k for k, v in RELAY_CONFIG_OPTIONS.items() if v == option)
        await self._state_proxy.async_set_controller_relayconfig(self._controller, raw)
        self.async_write_ha_state()
