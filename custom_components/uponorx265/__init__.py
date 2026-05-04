import asyncio
import math
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform

from homeassistant.const import CONF_HOST
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.util.dt as dt_util

from .const import (
    DOMAIN,
    SIGNAL_UPONOR_STATE_UPDATE,
    SCAN_INTERVAL,
    UNAVAILABLE_THRESHOLD,
    RELOAD_COOLDOWN,
    STORAGE_KEY,
    STORAGE_VERSION,
    STATUS_OK,
    STATUS_ERROR_BATTERY,
    STATUS_ERROR_VALVE,
    STATUS_ERROR_GENERAL,
    STATUS_ERROR_AIR_SENSOR,
    STATUS_ERROR_EXT_SENSOR,
    STATUS_ERROR_RH_SENSOR,
    STATUS_ERROR_RF_SENSOR,
    STATUS_ERROR_TAMPER,
    STATUS_ERROR_TOO_HIGH_TEMP,
    TOO_HIGH_TEMP_LIMIT,
    DEFAULT_TEMP
)
from .jnap import UponorJnap
from .helper import get_unique_id_from_config_entry

from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_COMFORT
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR]

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    # Sync options to data if they differ
    if config_entry.options:
        if config_entry.data != config_entry.options:
            dev_reg = device_registry.async_get(hass)
            ent_reg = entity_registry.async_get(hass)
            dev_reg.async_clear_config_entry(config_entry.entry_id)
            ent_reg.async_clear_config_entry(config_entry.entry_id)
            hass.config_entries.async_update_entry(config_entry, data=config_entry.options)

    host = config_entry.data[CONF_HOST]
    unique_id = get_unique_id_from_config_entry(config_entry)
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    session = async_get_clientsession(hass)

    state_proxy = UponorStateProxy(hass, host, session, store, unique_id, config_entry)
    await state_proxy.async_load_storage()

    thermostats = state_proxy.get_cached_thermostats()
    if thermostats:
        hass.async_create_task(state_proxy.async_update())
    else:
        await state_proxy.async_update()
        thermostats = state_proxy.get_active_thermostats()

    hass.data[unique_id] = {
        "state_proxy": state_proxy,
        "thermostats": thermostats
    }

    async def handle_set_variable(call):
        var_name = call.data.get('var_name')
        var_value = call.data.get('var_value')
        if not var_name:
            return
        await hass.data[unique_id]['state_proxy'].async_set_variable(var_name, var_value)

    hass.services.async_register(DOMAIN, "set_variable", handle_set_variable)

    # Forward setup for "climate" and "switch" platforms (done outside of the event loop)
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Track time interval for updates (use async function)
    cancel_interval = async_track_time_interval(hass, state_proxy.async_update, SCAN_INTERVAL)
    config_entry.async_on_unload(cancel_interval)

    config_entry.async_on_unload(config_entry.add_update_listener(async_update_options))

    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.debug("Update setup entry: %s, data: %s, options: %s", entry.entry_id, entry.data, entry.options)
    # Unload first to ensure clean state (if loaded), then reload
    # This handles the case where setup may have failed initially
    if entry.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading setup entry: %s, data: %s, options: %s", config_entry.entry_id, config_entry.data, config_entry.options)
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, [Platform.SWITCH, Platform.CLIMATE, Platform.SENSOR])
    return unload_ok

class UponorStateProxy:
    def __init__(self, hass, host, session, store, unique_id, config_entry):
        self._hass = hass
        self._client = UponorJnap(host, session)
        self._store = store
        self._data = {}
        self._storage_data = {}
        self._storage_metadata = {}
        self.next_sp_from_dt = None
        self._unique_id = unique_id
        self._config_entry = config_entry
        self._last_successful_update = None
        self._unavailable_since = None
        self._update_lock = asyncio.Lock()
        self._reload_in_progress = False
        self._last_reload_attempt = None

    def _get_room_name_from_data(self, thermostat):
        var = 'cust_' + thermostat + '_name'
        if var in self._data:
            return self._data[var]
        return None

    def _get_thermostat_id_from_data(self, thermostat):
        var = thermostat.replace('T', 'thermostat') + '_id'
        if var in self._data:
            return self._data[var]
        return None

    def _compose_storage_payload(self):
        payload = dict(self._storage_data)
        if self._storage_metadata:
            payload["_meta"] = self._storage_metadata
        return payload

    async def async_load_storage(self):
        data = await self._store.async_load()
        if not isinstance(data, dict):
            self._storage_data = {}
            self._storage_metadata = {}
            return

        self._storage_metadata = data.get("_meta", {}) if isinstance(data.get("_meta", {}), dict) else {}
        self._storage_data = {key: value for key, value in data.items() if key != "_meta"}

    def get_cached_thermostats(self):
        thermostats = self._storage_metadata.get("thermostats", [])
        ids = self._storage_metadata.get("ids", {})
        if isinstance(thermostats, list) and thermostats and all(ids.get(thermostat) for thermostat in thermostats):
            return thermostats
        return []

    def is_available(self):
        return self._last_successful_update is not None and dt_util.now() - self._last_successful_update <= UNAVAILABLE_THRESHOLD

    async def _async_persist_discovery_metadata(self):
        thermostats = self.get_active_thermostats()
        if not thermostats:
            return

        # Merge with previously cached thermostats so that a transient JNAP
        # response missing one thermostat does not permanently remove it from
        # cache and cause its entity to be missing after the next HA restart.
        cached_thermostats = self._storage_metadata.get("thermostats", [])
        merged_thermostats = list(dict.fromkeys(
            thermostats + [t for t in cached_thermostats if t not in thermostats]
        ))

        new_metadata = {
            "thermostats": merged_thermostats,
            "ids": {
                **self._storage_metadata.get("ids", {}),
                **{
                    thermostat: thermostat_id
                    for thermostat in thermostats
                    if (thermostat_id := self._get_thermostat_id_from_data(thermostat))
                },
            },
            "rooms": {
                **self._storage_metadata.get("rooms", {}),
                **{
                    thermostat: room_name
                    for thermostat in thermostats
                    if (room_name := self._get_room_name_from_data(thermostat))
                },
            },
            "humidity": list(dict.fromkeys(
                [thermostat for thermostat in thermostats if thermostat + '_rh' in self._data and int(self._data[thermostat + '_rh']) != 0]
                + self._storage_metadata.get("humidity", [])
            )),
            "floor": list(dict.fromkeys(
                [thermostat for thermostat in thermostats if thermostat + '_external_temperature' in self._data and int(self._data[thermostat + '_external_temperature']) != 32767]
                + self._storage_metadata.get("floor", [])
            )),
            "cooling_available": self._data.get('sys_cooling_available') == "1",
        }

        if new_metadata != self._storage_metadata:
            self._storage_metadata = new_metadata
            await self._store.async_save(self._compose_storage_payload())

    # Thermostats config

    def get_active_thermostats(self):
        active = []
        for c in range(1, 5):
            var = 'sys_controller_' + str(c) + '_presence'
            if var in self._data and self._data[var] != "1":
                continue
            for i in range(1, 13):
                var = 'C' + str(c) + '_thermostat_' + str(i) + '_presence'
                if var in self._data and self._data[var] == "1":
                    active.append('C' + str(c) + '_T' + str(i))
        return active

    def get_room_name(self, thermostat):
        room_name = self._get_room_name_from_data(thermostat)
        if room_name is not None:
            return room_name

        cached_rooms = self._storage_metadata.get("rooms", {})
        if thermostat in cached_rooms:
            return cached_rooms[thermostat]

        configured_name = self._config_entry.data.get(thermostat.lower())
        if configured_name:
            return configured_name

        return thermostat

    def get_thermostat_id(self, thermostat):
        thermostat_id = self._get_thermostat_id_from_data(thermostat)
        if thermostat_id is not None:
            return thermostat_id

        cached_ids = self._storage_metadata.get("ids", {})
        if thermostat in cached_ids:
            return cached_ids[thermostat]

        return thermostat

    def get_model(self):
        var = 'cust_SW_version_update'
        if var in self._data:
            return self._data[var].split('_')[0]
        return '-'

    def get_version(self, thermostat):
        var = thermostat[0:3] + 'sw_version'
        if var in self._data:
            return self._data[var].split('_')[0]

    # Temperatures & humidity

    def get_temperature(self, thermostat):
        var = thermostat + '_room_temperature'
        if var in self._data and int(self._data[var]) <= TOO_HIGH_TEMP_LIMIT:
            return round((int(self._data[var]) - 320) / 18, 1)

    def get_min_limit(self, thermostat):
        var = thermostat + '_minimum_setpoint'
        if var in self._data:
            return round((int(self._data[var]) - 320) / 18, 1)

    def get_max_limit(self, thermostat):
        var = thermostat + '_maximum_setpoint'
        if var in self._data:
            return round((int(self._data[var]) - 320) / 18, 1)

    def has_humidity_sensor(self, thermostat):
        var = thermostat + '_rh'
        if var in self._data:
            return int(self._data[var]) != 0
        return thermostat in self._storage_metadata.get("humidity", [])

    def get_humidity(self, thermostat):
        var = thermostat + '_rh'
        if var in self._data:
            return int(self._data[var])
        
    def has_floor_temperature(self, thermostat):
        var = thermostat + '_external_temperature'
        if var in self._data:
            return int(self._data[var]) != 32767
        return thermostat in self._storage_metadata.get("floor", [])

    def get_floor_temperature(self, thermostat):
        var = thermostat + '_external_temperature'
        if var in self._data:
            temp = int(self._data[var])
            if temp != 32767 and temp <= TOO_HIGH_TEMP_LIMIT:
                return round((temp - 320) / 18, 1)
        return None
    
    # Temperature setpoint

    def get_setpoint(self, thermostat):
        var = thermostat + '_setpoint'
        if var in self._data:
            temp = math.floor((int(self._data[var]) - 320) / 1.8) / 10
            return math.floor((int(self._data[var]) - self.get_active_setback(thermostat, temp) - 320) / 1.8) / 10

    def get_setpoint_raw(self, thermostat):
        """Get the raw setpoint value (with offset applied, as stored in the system)"""
        var = thermostat + '_setpoint'
        if var in self._data:
            return math.floor((int(self._data[var]) - 320) / 1.8) / 10
        return None

    def get_active_setback(self, thermostat, temp):
        min_lim = self.get_min_limit(thermostat)
        max_lim = self.get_max_limit(thermostat)
        if (min_lim is not None and abs(temp - min_lim) < 0.05) or \
           (max_lim is not None and abs(temp - max_lim) < 0.05):
            return 0

        cool_setback = 0
        var_cool_setback = 'sys_heat_cool_offset'
        if var_cool_setback in self._data and self.is_cool_enabled():
            cool_setback = int(self._data[var_cool_setback]) * -1

        eco_setback = 0
        var_eco_setback = thermostat + '_eco_offset'
        mode = -1 if self.is_cool_enabled() else 1
        if var_eco_setback in self._data and (self.is_eco(thermostat) or self.is_away()):
            eco_setback = int(self._data[var_eco_setback]) * mode

        return cool_setback + eco_setback

    # State

    def is_active(self, thermostat):
        var = thermostat + '_stat_cb_actuator'
        if var in self._data:
            return self._data[var] == "1"

    def get_pwm(self, thermostat):
        var = thermostat + '_ufh_pwm_output'
        if var in self._data:
            return int(self._data[var])

    def get_status(self, thermostat):
        var = thermostat + '_stat_battery_error'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_BATTERY
        var = thermostat + '_stat_valve_position_err'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_VALVE
        var = thermostat[0:3] + 'stat_general_system_alarm'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_GENERAL
        var = thermostat + '_stat_air_sensor_error'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_AIR_SENSOR
        var = thermostat + '_stat_external_sensor_err'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_EXT_SENSOR
        var = thermostat + '_stat_rh_sensor_error'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_RH_SENSOR
        var = thermostat + '_stat_rf_error'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_RF_SENSOR
        var = thermostat + '_stat_tamper_alarm'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_TAMPER
        var = thermostat + '_room_temperature'
        if var in self._data and int(self._data[var]) > TOO_HIGH_TEMP_LIMIT:
            return STATUS_ERROR_TOO_HIGH_TEMP
        return STATUS_OK

    # HVAC modes

    async def async_switch_to_cooling(self):
        for thermostat in self._hass.data[self._unique_id]['thermostats']:
            if self.get_setpoint(thermostat) == self.get_min_limit(thermostat):
                await self.async_set_setpoint(thermostat, self.get_max_limit(thermostat))

        await self._client.send_data({'sys_heat_cool_mode': '1'})
        self._data['sys_heat_cool_mode'] = '1'
        self._hass.async_create_task(self.call_state_update())

    async def async_switch_to_heating(self):
        for thermostat in self._hass.data[self._unique_id]['thermostats']:
            if self.get_setpoint(thermostat) == self.get_max_limit(thermostat):
                await self.async_set_setpoint(thermostat, self.get_min_limit(thermostat))

        await self._client.send_data({'sys_heat_cool_mode': '0'})
        self._data['sys_heat_cool_mode'] = '0'
        self._hass.async_create_task(self.call_state_update())

    async def async_turn_on(self, thermostat):
        await self.async_load_storage()
        last_temp = self._storage_data[thermostat] if thermostat in self._storage_data else DEFAULT_TEMP
        await self.async_set_setpoint(thermostat, last_temp)

    async def async_turn_off(self, thermostat):
        await self.async_load_storage()
        self._storage_data[thermostat] = self.get_setpoint(thermostat)
        await self._store.async_save(self._compose_storage_payload())
        off_temp = self.get_max_limit(thermostat) if self.is_cool_enabled() else self.get_min_limit(thermostat)
        await self.async_set_setpoint(thermostat, off_temp)

    async def async_set_preset_mode(self, preset_mode):
        if preset_mode == PRESET_AWAY:
            await self.async_set_away(True)
        elif preset_mode == PRESET_COMFORT:
            await self.async_set_away(False)


    # Cooling

    def is_cool_available(self):
        var = 'sys_cooling_available'
        if var in self._data:
            return self._data[var] == "1"
        # Fallback to cached value when _data is not yet populated (startup with cached thermostats)
        return self._storage_metadata.get("cooling_available", False)

    def is_cool_enabled(self):
        var = 'sys_heat_cool_mode'
        if var in self._data:
            return self._data[var] == "1"

    # Away & Eco

    def is_away(self):
        var = 'sys_forced_eco_mode'
        return var in self._data and self._data[var] == "1"

    async def async_set_away(self, is_away):
        var = 'sys_forced_eco_mode'
        data = "1" if is_away else "0"
        await self._client.send_data({var: data})
        self._data[var] = data
        self._hass.async_create_task(self.call_state_update())

    def is_eco(self, thermostat):
        if self.get_eco_setback(thermostat) == 0:
            return False
        var = thermostat + '_stat_cb_comfort_eco_mode'
        var_temp = 'cust_Temporary_ECO_Activation'
        return (var in self._data and self._data[var] == "1") or (
                    var_temp in self._data and self._data[var_temp] == "1")

    def get_eco_setback(self, thermostat):
        var = thermostat + '_eco_offset'
        if var in self._data:
            return round(int(self._data[var]) / 18, 1)
        
    def get_last_update(self):
        return self.next_sp_from_dt
    
    async def call_state_update(self):
        async_dispatcher_send(self._hass, SIGNAL_UPONOR_STATE_UPDATE)

    # Rest
    async def async_update(self,_=None):
        if self._update_lock.locked():
            _LOGGER.debug("Skipping Uponor update because a previous update is still running")
            return

        async with self._update_lock:
            try:
                self.next_sp_from_dt = dt_util.now()
                self._data = await self._client.get_data()
                self._last_successful_update = dt_util.now()
                self._unavailable_since = None
                await self._async_persist_discovery_metadata()
                self._hass.async_create_task(self.call_state_update())
                return
            except Exception as ex:
                _LOGGER.error("Uponor thermostat was unable to update: %s", ex)

            now = dt_util.now()
            if self._unavailable_since is None:
                self._unavailable_since = now
                return

            if now - self._unavailable_since <= UNAVAILABLE_THRESHOLD:
                return

            if self._reload_in_progress:
                return

            if self._last_reload_attempt is not None and now - self._last_reload_attempt <= RELOAD_COOLDOWN:
                return

            self._reload_in_progress = True
            self._last_reload_attempt = now
            _LOGGER.warning("Uponor entities have been unavailable for more than 2 minutes. Triggering reload...")
            try:
                await self._hass.config_entries.async_reload(self._config_entry.entry_id)
            finally:
                self._reload_in_progress = False

    async def async_set_variable(self, var_name, var_value):
        _LOGGER.debug("Called set variable: name: %s, value: %s, data: %s", var_name, var_value, self._data)
        await self._client.send_data({var_name: var_value})
        self._data[var_name] = var_value
        self._hass.async_create_task(self.call_state_update())

    async def async_set_setpoint(self, thermostat, temp):
        var = thermostat + '_setpoint'
        setpoint = int(temp * 18 + self.get_active_setback(thermostat, temp) + 320)
        await self._client.send_data({var: setpoint})
        self._data[var] = setpoint
        self._hass.async_create_task(self.call_state_update())
