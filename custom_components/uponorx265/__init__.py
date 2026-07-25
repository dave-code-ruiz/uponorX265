import asyncio
import math
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform

from homeassistant.const import CONF_HOST, CONF_NAME, ATTR_DEVICE_ID
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from getmac import get_mac_address

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
    STATUS_ERROR_COMFAILOUT,
    STATUS_ERROR_CONTROLER,
    STATUS_ONLINE,
    STATUS_OFFLINE,
    STATUS_ERROR_MAINCONTROLER_FAIL,
    TOO_HIGH_TEMP_LIMIT,
    DEFAULT_TEMP,
    DEVICE_MANUFACTURER
)
from .jnap import UponorJnap
from .helper import get_unique_id_from_config_entry, _get_mac_with_arp_refresh 

from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_COMFORT
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR, Platform.BINARY_SENSOR]

SET_VARIABLE_SCHEMA = vol.Schema(
    {
        vol.Required("var_name"): str,
        vol.Required("var_value"): vol.Any(str, int, float),
        vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
    }
)


def _get_all_state_proxies(hass: HomeAssistant) -> dict:
    """Return {unique_id: state_proxy} for every loaded uponorx265 config entry."""
    proxies = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = hass.data.get(entry.unique_id)
        if data:
            proxies[entry.unique_id] = data["state_proxy"]
    return proxies


def _resolve_target_proxies(hass: HomeAssistant, call) -> list:
    """Resolve which gateway(s) a service call targets.

    Multiple gateways (config entries) can be configured, so a call without
    'device_id' is only unambiguous when exactly one gateway is loaded.
    """
    all_proxies = _get_all_state_proxies(hass)
    device_ids = call.data.get(ATTR_DEVICE_ID)

    if not device_ids:
        if len(all_proxies) == 1:
            return list(all_proxies.values())
        _LOGGER.warning(
            "uponorx265.set_variable: %d gateways are configured; specify 'device_id' "
            "to target a specific one",
            len(all_proxies),
        )
        return []

    dev_reg = device_registry.async_get(hass)
    targeted = {}
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == DOMAIN and entry.unique_id in all_proxies:
                targeted[entry.unique_id] = all_proxies[entry.unique_id]
    return list(targeted.values())


def _migrate_entity_unique_ids(hass: HomeAssistant, config_entry: ConfigEntry, unique_instance_id: str) -> None:
    
    ent_reg = entity_registry.async_get(hass)
    entries = entity_registry.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
    prefix = f"{unique_instance_id}_"

    for entry in entries:
        if entry.unique_id.startswith(prefix):
            continue

        new_unique_id = f"{prefix}{entry.unique_id}"

        # Scenario 2: prefixed entity already exists (created by 1.1.2 as '_2').
        # Remove the stale bare-id entry instead of failing.
        existing_entity_id = ent_reg.async_get_entity_id(entry.domain, DOMAIN, new_unique_id)
        if existing_entity_id is not None:
            _LOGGER.info(
                "Removing stale entity %s (unique_id '%s') because '%s' already exists as %s",
                entry.entity_id, entry.unique_id, new_unique_id, existing_entity_id,
            )
            ent_reg.async_remove(entry.entity_id)
            continue

        # Scenario 1: safe to rename in-place.
        try:
            ent_reg.async_update_entity(entry.entity_id, new_unique_id=new_unique_id)
            _LOGGER.info(
                "Migrated entity %s unique_id: '%s' -> '%s'",
                entry.entity_id, entry.unique_id, new_unique_id,
            )
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Failed to migrate entity %s unique_id '%s': %s",
                entry.entity_id, entry.unique_id, exc,
            )

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
    # Storage must be keyed per config entry, otherwise multiple gateways
    # (config entries) overwrite each other's cached thermostat/controller data.
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{unique_id}")
    session = async_get_clientsession(hass)

    state_proxy = UponorStateProxy(hass, host, session, store, unique_id, config_entry)
    _LOGGER.debug(f"host {host} {config_entry} {unique_id}")
    await state_proxy.async_load_storage()

    thermostats = state_proxy.get_cached_thermostats()
    if thermostats:
        if config_entry.entry_id:
            await state_proxy.async_update()
        else:
            hass.async_create_task(state_proxy.async_update())
    else:
        await state_proxy.async_update()
        thermostats = state_proxy.get_active_thermostats()

    hass.data[unique_id] = {
        "state_proxy": state_proxy,
        "thermostats": thermostats,
    }

    if not hass.services.has_service(DOMAIN, "set_variable"):
        hass.services.async_register(
            DOMAIN, "set_variable", _create_set_variable_handler(hass), schema=SET_VARIABLE_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, "dump_hardware_info"):
        hass.services.async_register(
            DOMAIN, "dump_hardware_info", _create_dump_hardware_handler(hass),
            supports_response=SupportsResponse.ONLY,
        )

    # Migrate entity unique_ids from pre-1.1.2 bare format to prefixed format.
    # Must run before platform setup so HA matches existing registry entries
    # to the new unique_ids instead of creating duplicate entities.
    _migrate_entity_unique_ids(hass, config_entry, unique_id)

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
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )
    if unload_ok:
        hass.data.pop(get_unique_id_from_config_entry(config_entry), None)
    return unload_ok


def _create_set_variable_handler(hass: HomeAssistant):
    """Build the uponorx265.set_variable service handler bound to this hass instance.

    Supports multiple gateways: pass 'device_id' (any device belonging to the
    target gateway) to disambiguate when more than one gateway is configured.
    """
    async def handle_set_variable(call) -> None:
        var_name = call.data.get('var_name')
        var_value = call.data.get('var_value')
        if not var_name:
            return

        proxies = _resolve_target_proxies(hass, call)
        if not proxies:
            return

        for proxy in proxies:
            await proxy.async_set_variable(var_name, var_value)

    return handle_set_variable


def _create_dump_hardware_handler(hass: HomeAssistant):
    """Build the uponorx265.dump_hardware_info service handler.

    Returns hardware IDs and capability flags for every thermostat and
    controller as a service response, visible directly in Developer Tools.
    """
    async def handle_dump_hardware_info(call) -> dict:
        all_proxies = _get_all_state_proxies(hass)
        if not all_proxies:
            _LOGGER.warning("dump_hardware_info: no gateways loaded")
            return {}

        result = {"gateways": []}

        for unique_id, proxy in all_proxies.items():
            gateway = {
                "gateway_id": proxy.get_gateway_id(),
                "gateway_model": proxy.get_model(),
                "controllers": [],
                "thermostats": [],
            }

            thermostats = hass.data.get(unique_id, {}).get("thermostats", [])
            seen_controllers = set()
            for thermostat in thermostats:
                controller = thermostat.split('_')[0]
                if controller not in seen_controllers:
                    seen_controllers.add(controller)
                    ctrl_id = proxy.get_controller_id(controller)
                    gateway["controllers"].append({
                        "controller": controller,
                        "sn_start": ctrl_id[:4] if ctrl_id else None,
                        "hardware_type_raw": proxy._data.get(controller + '_hardware_type'),
                        "detected_model": str(proxy.get_controller_hardware(controller)),
                        "sw_version": proxy.get_controller_version(controller),
                    })

                t_id = proxy.get_thermostat_id(thermostat)
                gateway["thermostats"].append({
                    "thermostat": thermostat,
                    "sn_start": t_id[:4] if t_id else None,
                    "hardware_type_raw": proxy._data.get(thermostat + '_thermostat_type'),
                    "detected_model": str(proxy.get_thermostat_model(thermostat)),
                    "has_humidity_control": proxy.has_humidity_control(thermostat),
                    "has_humidity_sensor": proxy.has_humidity_sensor(thermostat),
                    "has_floor_temperature": proxy.has_floor_temperature(thermostat),
                    "is_public_device": proxy.is_public_device(thermostat),
                    "is_sensor_only": proxy.is_sensor_only(thermostat),
                })

            result["gateways"].append(gateway)

        return result

    return handle_dump_hardware_info


class UponorStateProxy:
    def __init__(self, hass, host, session, store, unique_id, config_entry):
        self._hass = hass
        self._client = UponorJnap(host, session)
        self._store = store
        self._host = host
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
        self._gateway_id = None
        _LOGGER.debug(f"Configdata = {self._config_entry}")
    # Controlers config  
    def get_active_controllers(self):
        active = []
        for c in range(1, 5):
            var = 'sys_controller_' + str(c) + '_presence'
            if var in self._data and self._data[var] == "1":
                active.append('C' + str(c))
        return active  
        
    def get_controller_id(self, controller):
        var = controller.replace('C', 'controller') + '_id'
        if var in self._data:
            return self._data[var]
        return self._storage_metadata.get("controller_ids", {}).get(controller)
        
    def get_controller_status(self, controller):
        var = controller.replace('C','sys_controller_') + '_lost'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_CONTROLER
        var = controller + 'stat_out_module_com_lost'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_COMFAILOUT
        var = controller + 'stat_general_system_alarm'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_GENERAL
        return STATUS_OK
        
    def get_controller_hardware(self, controller):
        var = controller + '_hardware_type'
        if var in self._data:
            hwid = int(self._data[var])
            controller_id = self.get_controller_id(controller)
            if controller_id is None:
                return hwid
            sn = controller_id[:4]
            prodk = sn[:3]
            mod = sn[-1:]
            _LOGGER.debug(f"id {hwid} s/n start {sn}")            
            if prodk=="419":
                if mod=="5":
# Smartix Base Pulse
                    return("X-245")
# Smatrix Wave Pulse
#                   return("X-265")
# Smatrix Base PRO
#                   return("X-147")
# Modbus RTU model  return("X-147") 
            return hwid

    def get_controller_name(self, controller):
        configured_name = self._config_entry.data.get(controller.lower())
        if configured_name:
            return configured_name
        var = 'cust_' + controller.replace('C', 'Controller') + '_Name'
        if var in self._data:
            return self._data[var]
        return self._storage_metadata.get("controller_names", {}).get(controller)

    def get_integration_name(self) -> str:
        """Return the user-configured name for this integration instance (gateway)."""
        return self._config_entry.data.get(CONF_NAME, DEVICE_MANUFACTURER)

    def get_gateway_id(self) -> str:
        """Return cached gateway ID (MAC or host fallback)."""
        if self._gateway_id is None:
            # Cache not yet populated; return host fallback until async_resolve_gateway_id runs
            return self._host.replace('.', '')
        return self._gateway_id

    async def async_resolve_gateway_id(self) -> str:
        """Resolve gateway MAC via ARP (with UDP socket to prime ARP cache) and cache it."""
        if self._gateway_id is None:
            mac = get_mac_address(ip=self._host)
            if mac is not None:
                self._gateway_id = mac.replace(':', '')
            else:
                mac = await self._hass.async_add_executor_job(
                    _get_mac_with_arp_refresh, self._host
                )
                if mac is not None:
                    self._gateway_id = mac.replace(':', '')
                else:
                    _LOGGER.warning(
                        "Could not resolve MAC address for %s, using host as fallback",
                        self._host,
                    )
                    self._gateway_id = self._host.replace('.', '')
        return self._gateway_id

    def get_gateway_status(self):
        if self.is_available() is None:
            return STATUS_OFFLINE
        var = 'cust_controller_1_lost'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_MAINCONTROLER_FAIL        
        return STATUS_ONLINE 
        
    def get_controller_version(self, controller):
        var = controller + '_sw_version'
        if var in self._data:
            hexver = hex(int(self._data[var])).replace('0x', '')
            return hexver[:-2] + '.' + hexver[-2:]
        return None

    def get_controller_avgtemp(self, controller):
        var = controller + '_average_room_temperature'
        if var in self._data:
            temp = int(self._data[var])
            if temp != 32767 and temp <= TOO_HIGH_TEMP_LIMIT:
                return round((temp - 320) / 18, 1)
        return None

    def get_inavg(self, thermostat):
        var = thermostat.replace('_T', '_channel_') + '_ave_temp'
        return self._data.get(var) == "1"
        
    async def async_iset_inavg(self, thermostat, override):
        var = thermostat.replace('_T', '_channel_') + '_ave_temp'
        data = "1" if override else "0"
        await self._client.send_data({var: data})
        self._data[var] = data
        self._hass.async_create_task(self.call_state_update())        

    def _get_room_name_from_data(self, thermostat):
        var = 'cust_' + thermostat + '_name'
        return self._data.get(var)

    def _get_thermostat_id_from_data(self, thermostat):
        var = thermostat.replace('T', 'thermostat') + '_id'
        return self._data.get(var)

    def _compose_storage_payload(self):
        payload = dict(self._storage_data)
        if self._storage_metadata:
            payload["_meta"] = self._storage_metadata
        return payload

    # -------------------------------------------------------------------------
    # Storage
    # -------------------------------------------------------------------------

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
        controllers = self.get_active_controllers()
        if not controllers:
            return
            
        thermostats = self.get_active_thermostats()
        if not thermostats:
            return

        # Merge with previously cached thermostats so that a transient JNAP
        # response missing one thermostat does not permanently remove it from
        # cache and cause its entity to be missing after the next HA restart.
        cached_controllers = self._storage_metadata.get("controllers", [])
        merged_controllers = list(dict.fromkeys(
            controllers + [t for t in cached_controllers if t not in controllers]        
        ))
        
        cached_thermostats = self._storage_metadata.get("thermostats", [])
        merged_thermostats = list(dict.fromkeys(
            thermostats + [t for t in cached_thermostats if t not in thermostats]
        ))

        new_metadata = {
            "gateway_id": self._data.get('cust_ip_device'),
            "controllers": merged_controllers,
            "controller_names" : {
                **self._storage_metadata.get("controller_names", {}),
                **{
                    controller: controller_name
                    for controller in self.get_active_controllers()
                    if (controller_name := self.get_controller_name(controller))
                },
            },
            "controller_ids": {
                **self._storage_metadata.get("controller_ids", {}),
                **{
                    controller: controller_id
                    for controller in self.get_active_controllers()
                    if (controller_id := self.get_controller_id(controller))
                },
            },
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

    # -------------------------------------------------------------------------
    # Thermostat config
    # -------------------------------------------------------------------------

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
        configured_name = self._config_entry.data.get(thermostat.lower())
        if configured_name:
            return configured_name        
        room_name = self._get_room_name_from_data(thermostat)
        if room_name is not None:
            return room_name
        cached_rooms = self._storage_metadata.get("rooms", {})
        if thermostat in cached_rooms:
            return cached_rooms[thermostat]
        return thermostat

    def get_thermostat_id(self, thermostat):
        thermostat_id = self._get_thermostat_id_from_data(thermostat)
        if thermostat_id is not None:
            return thermostat_id
        cached_ids = self._storage_metadata.get("ids", {})
        if thermostat in cached_ids:
            return cached_ids[thermostat]
        return thermostat

    def get_thermostat_model(self, thermostat):
        var = thermostat + '_thermostat_type'
        if var in self._data:
            hwid = int(self._data[var])
            sn = self.get_thermostat_id(thermostat)[:4]
            prodk = sn[:3]
            mod = sn[-1:]
      
            if prodk=="269":
                if mod=="1":
                    return ('T-144')
                if mod=="2":
                    return ('T-145')
            _LOGGER.debug(f"id {hwid} s/n start {sn} rh_c {self.has_humidity_control(thermostat)} rh_s {self.has_humidity_sensor(thermostat)} pd {self.is_public_device(thermostat)} hft {self.has_floor_temperature(thermostat)} Sensor only {self.is_sensor_only(thermostat)}")
# Smartix Base Pulse                   
#                   return("T-141") #No temp adjustment/RH
#                   return("T-143") #No temp adjustment/External temp/Tamper Alarm
#                   return("T-144") #Nobb for temp/inwall mount same as T145
#                   return("T-146") #Digital display/External temp
#                   return("T-148") #Digital display/External temp/RH/TimeDate
#                   return("T-149") #Digital display/External temp/RH 
# Smatrix Wave Pulse
#                   return("T-161") #No temp adjustment/RH
#                   return("T-162") #Digital display/External temp                  
#                   return("T-163") #No temp adjustment/External temp/Tamper Alarm
#                   return("T-165") #Nobb for temp
#                   return("T-166") #Digital display/External temp
#                   return("T-168") #Digital display/External temp/RH/TimeDate
#                   return("T-169") #Digital display/External temp/RH
#                    return("T-247")
        return hwid

    def get_model(self):
        return "R-208"

    def get_sw_version(self):
        var = 'cust_SW_version_update'
        if var in self._data:
            return self._data[var].split('_')[0]
        return '-'

    def get_version(self, thermostat):
        var = thermostat + '_sw_version'
        if var in self._data:
            return hex(int(self._data[var])).replace("0x", "")
        return None

    # -------------------------------------------------------------------------
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

    def has_humidity_control(self, thermostat):
        var = thermostat + '_rh_control'
        if var in self._data:
            return int(self._data[var])

    def is_public_device(self, thermostat):
        var = thermostat + '_system_device_public'
        if var in self._data:
            return int(self._data[var])

    def is_sensor_only(self, thermostat):
        var = thermostat + '_sensor_only'
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

    # -------------------------------------------------------------------------
    # Temperature setpoint
    # -------------------------------------------------------------------------

    def get_setpoint(self, thermostat):
        var = thermostat + '_setpoint'
        if var in self._data:
            raw = int(self._data[var])
            temp = math.floor((raw - 320) / 1.8) / 10
            return math.floor((raw - self.get_active_setback(thermostat, temp) - 320) / 1.8) / 10

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

    def get_local_override(self, thermostat):
        var = thermostat + '_pub_setpoint_override'
        return var in self._data and int(self._data[var]) != 0

    async def async_local_override(self, thermostat, override):
        var = thermostat + '_pub_setpoint_override'
        data = "1" if override else "0"
        await self._client.send_data({var: data})
        self._data[var] = data
        if not override:
            # Re-poll immediately so HA displays the setpoint the physical dial has set,
            # rather than the last HA-set value.
            self._hass.async_create_task(self.async_update())
        else:
            self._hass.async_create_task(self.call_state_update())

    # -------------------------------------------------------------------------
    # State
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # HVAC modes
    # -------------------------------------------------------------------------

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
        last_temp = self._storage_data.get(thermostat, DEFAULT_TEMP)
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

    # -------------------------------------------------------------------------
    # Cooling
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Away & Eco
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Polling & reload
    # -------------------------------------------------------------------------

    async def async_update(self, _=None):
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
        _LOGGER.debug("Called set variable: name: %s, value: %s", var_name, var_value)
        await self._client.send_data({var_name: var_value})
        self._data[var_name] = var_value
        self._hass.async_create_task(self.call_state_update())

    async def async_set_setpoint(self, thermostat, temp):
        var = thermostat + '_setpoint'
        setpoint = int(temp * 18 + self.get_active_setback(thermostat, temp) + 320)
        await self._client.send_data({var: setpoint})
        self._data[var] = setpoint
        self._hass.async_create_task(self.call_state_update())
