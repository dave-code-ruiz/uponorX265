from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
import voluptuous as vol
import logging

from homeassistant.const import (
    CONF_HOST,
    CONF_NAME
)

from .jnap import UponorJnap

from .const import (
    DOMAIN,
    CONF_UNIQUE_ID,
    DEVICE_MANUFACTURER,
    CONF_CREATE_CONTROLLERS,
    CONF_SENSOR_TEMP,
    CONF_BINARY_SENSOR_VALVE,
    CONF_SWITCH_SENSOR_AVG,
    CONF_CONTROLLER_IO,
    CONF_INSTALLER_SETTINGS,
)

from .helper import (
    create_unique_id_from_user_input,
    generate_unique_id_from_user_input_conf_name,
    _async_get_devices_by_connection,
)

_LOGGER = logging.getLogger(__name__)


class DomainConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    def __init__(self):
        self._api_response = {}
        self._entry_data = {}

    @property
    def schema(self):
        return vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_NAME, default=DEVICE_MANUFACTURER): str,
                vol.Optional(CONF_UNIQUE_ID): str,
            }
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo):
        """Follow a known gateway to its new IP address.

        Declared in the manifest as `registered_devices`, so Home Assistant
        only calls this for a MAC already recorded as a connection on a
        gateway device this integration created - a recovery path for an
        existing entry, never a way to adopt a new gateway.

        The entry's unique_id is derived from the user's chosen name rather
        than from the MAC, so the usual
        `_abort_if_unique_id_configured(updates=...)` shortcut cannot find it.
        The device registry is what links the MAC back to the entry.
        """
        mac = dr.format_mac(discovery_info.macaddress)
        devices = _async_get_devices_by_connection(
            dr.async_get(self.hass), (dr.CONNECTION_NETWORK_MAC, mac)
        )

        # A MAC can be registered by more than one integration (a router, a
        # device tracker), so every match is checked and only ours is acted on.
        for device in devices:
            for entry_id in device.config_entries:
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is None or entry.domain != DOMAIN:
                    continue
                if entry.data.get(CONF_HOST) == discovery_info.ip:
                    return self.async_abort(reason="already_configured")

                _LOGGER.info(
                    "Uponor gateway %s is now at %s (was %s); updating the config entry",
                    mac, discovery_info.ip, entry.data.get(CONF_HOST),
                )
                # Both data and options, deliberately. async_setup_entry merges
                # the two with options taking precedence, so writing the new
                # host to data alone would be silently reverted to the stale
                # address on the next setup.
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_HOST: discovery_info.ip},
                    options={**entry.options, CONF_HOST: discovery_info.ip},
                )
                # Updating the entry fires its update listener, which reloads.
                # If the entry is in a failed/retrying state that listener is
                # not registered, and HA's own setup retry picks up the new
                # host instead.
                return self.async_abort(reason="already_configured")

        return self.async_abort(reason="not_uponor_device")

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            unique_id = create_unique_id_from_user_input(user_input)
            if unique_id is None:
                unique_id = generate_unique_id_from_user_input_conf_name(user_input)

            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            try:
                session = async_get_clientsession(self.hass)
                client = UponorJnap(user_input[CONF_HOST], session)
                self._api_response = await client.get_data()
            except Exception as e:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self.schema,
                    errors={"base": "invalid_host", "debug": repr(e)},
                )
            self._entry_data = user_input
            return self.async_show_form(
                step_id="controllers",
                data_schema=self.get_controllers_schema(),
            )

        return self.async_show_form(step_id="user", data_schema=self.schema)

    async def async_step_controllers(self, user_input=None):
        """Handle controller naming step."""
        if user_input is None:
            return self.async_show_form(
                step_id="controllers",
                data_schema=self.get_controllers_schema(),
            )
        self._entry_data = {**self._entry_data, **user_input}
        return self.async_show_form(
            step_id="features",
            data_schema=self.get_features_schema(),
        )

    async def async_step_features(self, user_input=None):
        """Handle entity feature selection step."""
        if user_input is None:
            return self.async_show_form(
                step_id="features",
                data_schema=self.get_features_schema(),
            )
        self._entry_data = {**self._entry_data, **user_input}
        return self.async_show_form(
            step_id="rooms",
            data_schema=self.get_rooms_schema(),
        )

    async def async_step_rooms(self, user_input=None):
        """Handle 3rd step."""
        if user_input is None:
            return self.async_show_form(
                step_id="rooms",
                data_schema=self.get_rooms_schema(),
            )
        data = {**self._entry_data, **user_input}
        _LOGGER.debug(f"in {user_input} {data}")
        return self.async_create_entry(
            #title="Uponorx265",
            title=data['name'],
            data=data
        )

    def get_features_schema(self, current_data=None):
        current_data = current_data or {}
        return vol.Schema({
            vol.Required(
                CONF_SENSOR_TEMP,
                default=current_data.get(CONF_SENSOR_TEMP, True),
            ): bool,
            vol.Required(
                CONF_BINARY_SENSOR_VALVE,
                default=current_data.get(CONF_BINARY_SENSOR_VALVE, False),
            ): bool,
            vol.Required(
                CONF_SWITCH_SENSOR_AVG,
                default=current_data.get(CONF_SWITCH_SENSOR_AVG, False),
            ): bool,
            vol.Required(
                CONF_CONTROLLER_IO,
                default=current_data.get(CONF_CONTROLLER_IO, False),
            ): bool,
            vol.Required(
                CONF_INSTALLER_SETTINGS,
                default=current_data.get(CONF_INSTALLER_SETTINGS, False),
            ): bool,
        })

    def get_controllers_schema(self, current_data=None):
        current_data = current_data or {}
        controllers_schema = {
            vol.Required(
                CONF_CREATE_CONTROLLERS,
                default=current_data.get(CONF_CREATE_CONTROLLERS, True),
            ): bool,
        }
        for c in self.get_active_controllers():
            controllers_schema[vol.Optional(c.lower(), default=self.get_controller_name(c))] = str
        return vol.Schema(controllers_schema)

    def get_rooms_schema(self):
        rooms_schema = {}
        for t in self.get_active_thermostats():
            rooms_schema[vol.Optional(t.lower(), default=self.get_room_name(t))] = str
        return vol.Schema(rooms_schema)

    def get_active_controllers(self):
        active = []
        for c in range(1, 5):
            var = 'sys_controller_' + str(c) + '_presence'
            if var in self._api_response and self._api_response[var] == "1":
                active.append('C' + str(c))
        return active

    def get_active_thermostats(self):
        active = []
        for c in range(1, 5):
            var = 'sys_controller_' + str(c) + '_presence'
            if var in self._api_response and self._api_response[var] == "1":
                for i in range(1, 13):
                    var = 'C' + str(c) + '_thermostat_' + str(i) + '_presence'
                    if var in self._api_response and self._api_response[var] == "1":
                        active.append('C' + str(c) + '_T' + str(i))
        return active

    def get_controller_name(self, controller):
        var = 'cust_' + controller.replace('C', 'Controller') + '_Name'
        if var in self._api_response:
            return self._api_response[var]
        return controller

    def get_room_name(self, thermostat):
        var = 'cust_' + thermostat + '_name'
        if var in self._api_response:
            return self._api_response[var]
        return thermostat

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry):
        return OptionsFlowHandler(entry)


class OptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry):
        """Initialize options flow."""
        super().__init__()

    async def async_step_init(self, user_input=None):
        _LOGGER.debug(f"in {user_input} ")
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        current_data = self.config_entry.data
        _LOGGER.debug(f"in {user_input} {self.config_entry.data}")
        if user_input is not None:
            self._pending_data = {**current_data, CONF_HOST: user_input[CONF_HOST]}
            return self.async_show_form(
                step_id="features",
                data_schema=self._features_schema(current_data),
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_HOST,
                    default=current_data.get(CONF_HOST)
                ): str,
            }),
        )

    async def async_step_features(self, user_input=None):
        current_data = self.config_entry.data
        if user_input is not None:
            data = {**self._pending_data, **user_input}
            return self.async_create_entry(title=current_data['name'], data=data)
        return self.async_show_form(
            step_id="features",
            data_schema=self._features_schema(current_data),
        )

    def _features_schema(self, current_data):
        return vol.Schema({
            vol.Required(
                CONF_SENSOR_TEMP,
                default=current_data.get(CONF_SENSOR_TEMP, True),
            ): bool,
            vol.Required(
                CONF_BINARY_SENSOR_VALVE,
                default=current_data.get(CONF_BINARY_SENSOR_VALVE, False),
            ): bool,
            vol.Required(
                CONF_SWITCH_SENSOR_AVG,
                default=current_data.get(CONF_SWITCH_SENSOR_AVG, False),
            ): bool,
            vol.Required(
                CONF_CONTROLLER_IO,
                default=current_data.get(CONF_CONTROLLER_IO, False),
            ): bool,
            vol.Required(
                CONF_INSTALLER_SETTINGS,
                default=current_data.get(CONF_INSTALLER_SETTINGS, False),
            ): bool,
        })
