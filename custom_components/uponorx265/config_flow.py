from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
)

from .helper import (
    create_unique_id_from_user_input,
    generate_unique_id_from_user_input_conf_name,
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
        })
