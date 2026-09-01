import asyncio
import math
import logging
from functools import partial

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
    DEVICE_MANUFACTURER,
    DIAL_THERMOSTAT_MODELS,
    FLAG_DEFAULTS,
    CONTROLLER_FIRMWARE_SERIES,
    CONTROLLER_HARDWARE_SERIES,
    SERIES_CONTROLLER_MODELS,
    SERIES_WAVE,
    THERMOSTAT_MODELS
)
from .jnap import UponorJnap
from .helper import get_unique_id_from_config_entry, _get_mac_with_arp_refresh 

from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]

# Registered once for the domain rather than per config entry, so they have to
# be removed once the last entry goes - see async_unload_entry.
DOMAIN_SERVICES = ("set_variable", "dump_hardware_info", "dump_raw_data")

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
    """Migrate entity registry entries to the current unique_id formats.

    Three historical format changes are handled, composing so that an upgrade
    from any older version lands on the current format in one pass:
    - pre-1.1.2: bare ids (no config-entry prefix) gain the prefix
    - pre-1.1.5: climate ids (no '_climate' suffix) gain the suffix
    - the gateway status sensor drops the embedded gateway id
    """
    ent_reg = entity_registry.async_get(hass)
    entries = entity_registry.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
    prefix = f"{unique_instance_id}_"

    def _entity_preference_key(entry):
        """Prefer the oldest row, then an entity id without a numeric suffix."""
        _, separator, suffix = entry.entity_id.rpartition("_")
        return (
            entry.created_at,
            bool(separator and suffix.isdigit()),
            entry.entity_id,
        )

    for entry in entries:
        new_unique_id = entry.unique_id
        if not new_unique_id.startswith(prefix):
            new_unique_id = f"{prefix}{new_unique_id}"
        if entry.domain == "climate" and not new_unique_id.endswith("_climate"):
            new_unique_id = f"{new_unique_id}_climate"
        # The gateway status sensor used to embed the resolved gateway id
        # (MAC when resolvable, the host with dots stripped when not). That id
        # changes the first time MAC resolution starts working, which silently
        # changed this unique_id and left the previous entity behind holding
        # the good entity_id - so the new one came back as
        # sensor.uponor_gateway_status_2. Collapse every historical form to
        # the id-free one, which cannot drift.
        if entry.domain == "sensor" and new_unique_id.endswith("_gateway_status"):
            new_unique_id = f"{prefix}gateway_status"

        if new_unique_id == entry.unique_id:
            continue

        # Scenario 2: an entity with the new unique_id already exists (created
        # as a duplicate by a version that lacked this migration).
        existing_entity_id = ent_reg.async_get_entity_id(entry.domain, DOMAIN, new_unique_id)
        if existing_entity_id is not None:
            existing_entry = ent_reg.async_get(existing_entity_id)
            is_gateway_status = (
                entry.domain == "sensor"
                and new_unique_id == f"{prefix}gateway_status"
                and existing_entry is not None
                and existing_entry.config_entry_id == config_entry.entry_id
            )
            if is_gateway_status:
                # Keep the original registry row, which normally owns the
                # unsuffixed entity_id and all of the user's customizations.
                # created_at also makes this deterministic for less common
                # combinations of several historical gateway ids.
                if _entity_preference_key(entry) < _entity_preference_key(existing_entry):
                    ent_reg.async_remove(existing_entry.entity_id)
                    ent_reg.async_update_entity(entry.entity_id, new_unique_id=new_unique_id)
                    kept_entity_id = entry.entity_id
                    removed_entity_id = existing_entry.entity_id
                else:
                    ent_reg.async_remove(entry.entity_id)
                    kept_entity_id = existing_entry.entity_id
                    removed_entity_id = entry.entity_id
                _LOGGER.info(
                    "Merged duplicate gateway status entities; kept %s and removed %s",
                    kept_entity_id,
                    removed_entity_id,
                )
                continue

            # For all other migrations, retain the already-canonical row.
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

def _get_registered_gateway_id(
    hass: HomeAssistant, config_entry: ConfigEntry, unique_instance_id: str
) -> str | None:
    """Return the best existing gateway identifier for an unconfirmed fallback."""
    dev_reg = device_registry.async_get(hass)
    candidates = []
    for device in device_registry.async_entries_for_config_entry(
        dev_reg, config_entry.entry_id
    ):
        if device.via_device_id is not None:
            continue
        for identifier_domain, identifier in device.identifiers:
            if identifier_domain == unique_instance_id:
                candidates.append(
                    (
                        device.model != "R-208",
                        device.created_at,
                        device.id,
                        identifier,
                    )
                )

    if not candidates:
        return None
    return min(candidates)[-1]


def _migrate_gateway_device_id(hass: HomeAssistant, config_entry: ConfigEntry, unique_instance_id: str, new_gateway_id: str) -> None:
    """Reconcile the gateway device's identifier with a newly-resolved MAC.

    The gateway device's identifier has changed format over time (host-based
    with dots stripped -> lowercase MAC -> uppercase MAC), and the host-based
    fallback itself can drift across restarts (DHCP reassigning the IP while
    MAC resolution keeps failing, e.g. gateway and HA host on different
    subnets — see the "Gateway ID" section of ARCHITECTURE.md). Guessing at
    specific old id strings would miss that drift and leave a fresh orphaned
    device behind on every IP change. Instead, this identifies the gateway
    device structurally: for a given config entry there is exactly one
    device with no via_device (the root of the gateway/controller/thermostat
    hierarchy) — whatever identifier it currently holds, that's the old
    gateway device to reconcile. Renames it in place if no device already
    exists under the new identifier, or merges it (including attached
    entities, child devices, and user customization) if one does.
    """
    if new_gateway_id is None:
        return

    dev_reg = device_registry.async_get(hass)
    new_identifier = (unique_instance_id, new_gateway_id)
    new_device = dev_reg.async_get_device(identifiers={new_identifier})

    old_device = next(
        (
            device
            for device in device_registry.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
            if device.via_device_id is None and new_identifier not in device.identifiers
        ),
        None,
    )
    if old_device is None:
        return

    if new_device is None:
        # First time resolving to this identifier: rename the existing device in place.
        dev_reg.async_update_device(old_device.id, new_identifiers={new_identifier})
        _LOGGER.info(
            "Migrated gateway device identifier for %s to '%s'",
            unique_instance_id, new_gateway_id,
        )
        return

    # A new device already exists (created by a prior restart). Move every
    # reference away from the old device before removing it: Home Assistant
    # otherwise deletes attached entity rows and clears child parent links.
    if old_device.area_id and not new_device.area_id:
        dev_reg.async_update_device(new_device.id, area_id=old_device.area_id)
    if old_device.name_by_user and not new_device.name_by_user:
        dev_reg.async_update_device(new_device.id, name_by_user=old_device.name_by_user)

    ent_reg = entity_registry.async_get(hass)
    for entity_entry in entity_registry.async_entries_for_device(
        ent_reg, old_device.id, include_disabled_entities=True
    ):
        ent_reg.async_update_entity(entity_entry.entity_id, device_id=new_device.id)

    for child_device in device_registry.async_entries_for_config_entry(
        dev_reg, config_entry.entry_id
    ):
        if child_device.via_device_id == old_device.id:
            dev_reg.async_update_device(
                child_device.id,
                via_device_id=new_device.id,
            )

    dev_reg.async_remove_device(old_device.id)
    _LOGGER.info(
        "Removed orphaned gateway device (%s) for %s, superseded by '%s'",
        old_device.id, unique_instance_id, new_gateway_id,
    )


def _register_gateway_devices(hass: HomeAssistant, config_entry: ConfigEntry, unique_instance_id: str, state_proxy) -> None:
    """Explicitly register the gateway and controller devices before platform setup.

    Thermostat/controller entities declare a `via_device` pointing at their
    parent, but the parent device is otherwise only created as a side effect
    of a specific entity (a controller sensor, gated behind
    CONF_CREATE_CONTROLLERS) which may load after — or never, if that entity
    is disabled. Registering the devices up front guarantees the parent
    always exists regardless of platform order or which optional entities
    are enabled.
    """
    dev_reg = device_registry.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(unique_instance_id, state_proxy.get_gateway_id())},
        manufacturer=DEVICE_MANUFACTURER,
        name=state_proxy.get_integration_name(),
        model=state_proxy.get_model(),
        serial_number=state_proxy.get_gateway_id(),
    )
    controllers = state_proxy.get_active_controllers() or state_proxy.get_cached_controllers()
    for controller in controllers:
        dev_reg.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(unique_instance_id, state_proxy.get_controller_id(controller))},
            manufacturer=DEVICE_MANUFACTURER,
            name=state_proxy.get_controller_name(controller),
            model=state_proxy.get_controller_hardware(controller),
            sw_version=state_proxy.get_controller_version(controller),
            serial_number=state_proxy.get_controller_id(controller),
            via_device=(unique_instance_id, state_proxy.get_gateway_id()),
        )


def _remove_unsupported_local_override_entities(hass: HomeAssistant, config_entry: ConfigEntry, unique_instance_id: str, state_proxy, thermostats) -> None:
    """Remove local-override switches created for thermostats that do not support the feature."""
    ent_reg = entity_registry.async_get(hass)
    stale_unique_ids = {
        f"{unique_instance_id}_{state_proxy.get_thermostat_id(thermostat)}_local_override"
        for thermostat in thermostats
        if not state_proxy.requires_local_override(thermostat)
    }
    for entry in entity_registry.async_entries_for_config_entry(ent_reg, config_entry.entry_id):
        if entry.domain == "switch" and entry.unique_id in stale_unique_ids:
            _LOGGER.info(
                "Removing switch %s: thermostat does not support local override",
                entry.entity_id,
            )
            ent_reg.async_remove(entry.entity_id)


def _sync_entry_config(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reconcile entry.data with entry.options, filling in feature defaults."""
    # The options flow reads entry.data and writes entry.options, while every
    # platform reads entry.data - so the two have to be kept in step or an
    # options change never reaches the entities.
    #
    # Merge rather than replace: options win over data, a key only data holds
    # survives (a wholesale replace would drop it), and any feature flag
    # neither carries falls back to its documented default. Entries created
    # before the 1.1.5 refactor have none of those keys, so without the
    # defaults data and options can never compare equal and this ran on every
    # single setup.
    #
    # Deliberately does NOT touch the device or entity registries. This block
    # used to call async_clear_config_entry() on both first, which deletes
    # every entity row belonging to the entry - taking custom names,
    # entity_ids, areas and icons with it, and re-registering under fresh
    # entity_ids - and it ran before both migrations, leaving them an empty
    # device list and nothing to migrate.
    merged = {**FLAG_DEFAULTS, **config_entry.data, **(config_entry.options or {})}
    if merged != dict(config_entry.data) or merged != dict(config_entry.options):
        hass.config_entries.async_update_entry(config_entry, data=merged, options=merged)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    _sync_entry_config(hass, config_entry)

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
        hass.async_create_task(state_proxy.async_update())
    else:
        await state_proxy.async_update()
        thermostats = state_proxy.get_active_thermostats()

    # Must run before platform setup: device_info reads get_gateway_id(). Only
    # an authoritative MAC lookup may migrate the registry; a failed lookup
    # retains the existing gateway identifier as an unconfirmed fallback.
    resolved_gateway_id = await state_proxy.async_resolve_gateway_id()
    if state_proxy._gateway_id is not None:
        _migrate_gateway_device_id(hass, config_entry, unique_id, resolved_gateway_id)

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

    if not hass.services.has_service(DOMAIN, "dump_raw_data"):
        hass.services.async_register(
            DOMAIN, "dump_raw_data", _create_dump_raw_data_handler(hass),
            supports_response=SupportsResponse.ONLY,
        )

    # Migrate entity unique_ids from older formats (pre-1.1.2 bare ids,
    # pre-1.1.5 climate ids without '_climate' suffix).
    # Must run before platform setup so HA matches existing registry entries
    # to the new unique_ids instead of creating duplicate entities.
    _migrate_entity_unique_ids(hass, config_entry, unique_id)

    # Register gateway/controller devices before platform setup: CLIMATE and
    # SWITCH load before SENSOR, and their entities' via_device would
    # otherwise reference a controller device that doesn't exist yet.
    _register_gateway_devices(hass, config_entry, unique_id, state_proxy)

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
    # async_reload() unloads first when the entry is loaded, and sets up
    # cleanly when it is not (including after a failed initial setup), so an
    # explicit unload here only tears the entry down twice.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading setup entry: %s, data: %s, options: %s", config_entry.entry_id, config_entry.data, config_entry.options)
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )
    if unload_ok:
        hass.data.pop(get_unique_id_from_config_entry(config_entry), None)

        # The services are registered on the domain, not on the entry, so
        # unloading the last entry has to take them with it. Left behind, they
        # stay callable with handlers that resolve to no state proxies and
        # return silently. This entry may still report LOADED while its own
        # unload is in flight, hence <= 1 rather than == 0.
        loaded_entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if len(loaded_entries) <= 1:
            for service in DOMAIN_SERVICES:
                hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: device_registry.DeviceEntry,
) -> bool:
    """Allow deleting a device the gateway no longer reports.

    Home Assistant only renders the Delete action on a device page when the
    integration implements this, and the same gate makes programmatic removal
    refuse. Without it, anything the integration orphans - a gateway device
    left under a stale identifier, a thermostat that has been unpaired - can
    only be cleared by editing the registry by hand.

    A device counts as still present if any of its identifiers matches the
    gateway, a controller or a thermostat this entry currently knows about.
    Cached controllers and thermostats count too, so a unit missing from one
    transient JNAP response is not offered up for deletion.
    """
    unique_instance_id = get_unique_id_from_config_entry(config_entry)
    entry_data = hass.data.get(unique_instance_id)
    if not entry_data:
        # The entry is not loaded, so the live set is unknown. Refuse rather
        # than risk removing a device that is in fact still present.
        _LOGGER.debug(
            "Refusing removal of device %s: config entry %s is not loaded",
            device_entry.id, config_entry.entry_id,
        )
        return False

    state_proxy = entry_data["state_proxy"]

    known_ids = {state_proxy.get_gateway_id()}
    controllers = set(state_proxy.get_active_controllers()) | set(state_proxy.get_cached_controllers())
    for controller in controllers:
        known_ids.add(state_proxy.get_controller_id(controller))
    thermostats = set(state_proxy.get_active_thermostats()) | set(state_proxy.get_cached_thermostats())
    for thermostat in thermostats:
        known_ids.add(state_proxy.get_thermostat_id(thermostat))
    known_ids.discard(None)

    still_present = any(
        domain == unique_instance_id and identifier in known_ids
        for domain, identifier in device_entry.identifiers
    )
    if still_present:
        _LOGGER.debug(
            "Refusing removal of device %s: still reported by the gateway",
            device_entry.id,
        )
    return not still_present


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
                        "relays_config": proxy._data.get(controller + '_controller_relays_config'),
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


def _create_dump_raw_data_handler(hass: HomeAssistant):
    """Build the uponorx265.dump_raw_data service handler.

    Returns the complete raw data dict received from the gateway,
    visible directly in Developer Tools → Services.
    """
    async def handle_dump_raw_data(call) -> dict:
        all_proxies = _get_all_state_proxies(hass)
        if not all_proxies:
            _LOGGER.warning("dump_raw_data: no gateways loaded")
            return {}

        if len(all_proxies) == 1:
            proxy = next(iter(all_proxies.values()))
            return dict(proxy._data)

        return {uid: dict(proxy._data) for uid, proxy in all_proxies.items()}

    return handle_dump_raw_data


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
        self._storage_lock = asyncio.Lock()
        self._reload_in_progress = False
        self._last_reload_attempt = None
        self._gateway_id = None
        self._registered_gateway_id = _get_registered_gateway_id(
            hass, config_entry, unique_id
        )
        self._stale_override_switches_cleaned = False
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
        
    def get_product_series(self, controller=None):
        """Resolve Smatrix Wave Pulse vs Base Pulse for this system.

        The controller firmware image name states the model outright
        ("X265_121.hex" / "X245_122.hex"), so that is read first. Failing
        that, the controller's own hardware type register carries the same
        distinction. The serial number does NOT: prefix 4194 has been seen on
        both Wave and Base controllers.
        """
        firmware = self._data.get('cust_SW_version_update')
        if firmware:
            series = CONTROLLER_FIRMWARE_SERIES.get(firmware.split('_')[0].upper())
            if series is not None:
                return series

        candidates = [controller] if controller is not None else self.get_active_controllers()
        for candidate in candidates:
            raw_type = self._data.get(str(candidate) + '_hardware_type')
            if raw_type is not None:
                series = CONTROLLER_HARDWARE_SERIES.get(str(raw_type))
                if series is not None:
                    return series
        return None

    def get_controller_hardware(self, controller):
        """The controller's model: X-265 (Wave Pulse) or X-245 (Base Pulse)."""
        series = self.get_product_series(controller)
        if series is None:
            return None
        return SERIES_CONTROLLER_MODELS.get(series)

    def get_controller_name(self, controller):
        configured_name = self._config_entry.data.get(controller.lower())
        if configured_name:
            return configured_name
        var = 'cust_' + controller.replace('C', 'Controller') + '_Name'
        if var in self._data:
            return self._data[var]
        cached_name = self._storage_metadata.get("controller_names", {}).get(controller)
        if cached_name:
            return cached_name
        # Fall back to a generated name ("<gateway name> Controller 1") so
        # controller devices are never registered unnamed.
        return f"{self.get_integration_name()} {controller.replace('C', 'Controller ')}"

    def get_integration_name(self) -> str:
        """Return the user-configured name for this integration instance (gateway)."""
        return self._config_entry.data.get(CONF_NAME, DEVICE_MANUFACTURER)

    def get_gateway_id(self) -> str:
        """Return the confirmed MAC, retained registry ID, or host fallback."""
        return (
            self._gateway_id
            or self._registered_gateway_id
            or self._host.replace('.', '')
        )

    async def async_resolve_gateway_id(self) -> str:
        """Resolve and cache the gateway MAC, retaining a stable fallback.

        Resolution can fail briefly at boot because the ARP cache is cold. A
        previously registered gateway ID is retained in that case; a new
        install uses the host form. Neither fallback is stored in _gateway_id,
        so later polls keep trying until a MAC is authoritatively resolved.

        Both lookups run in the executor: getmac reads /proc/net/arp and on
        several platforms shells out to `arp` or `ip neighbor`, which is
        exactly what Home Assistant's blocking-call detector flags.
        """
        if self._gateway_id is not None:
            return self._gateway_id

        mac = await self._hass.async_add_executor_job(
            partial(get_mac_address, ip=self._host)
        )
        _LOGGER.debug("Direct get_mac_address(%s) (no ARP priming) returned: %s", self._host, mac)
        if mac is None:
            mac = await self._hass.async_add_executor_job(
                _get_mac_with_arp_refresh, self._host
            )

        if mac is None:
            # A previous successful run may already have registered a stable
            # MAC identifier. Retain that exact identity instead of migrating
            # it backwards to the IP-derived fallback after one failed lookup.
            if self._registered_gateway_id is None:
                self._registered_gateway_id = _get_registered_gateway_id(
                    self._hass, self._config_entry, self._unique_id
                )
            fallback_id = self.get_gateway_id()
            _LOGGER.warning(
                "Could not resolve MAC address for %s; retaining gateway id %s "
                "until it resolves on a later poll",
                self._host,
                fallback_id,
            )
            return fallback_id

        self._gateway_id = mac.replace(':', '').upper()
        return self._gateway_id

    async def _async_retry_gateway_id(self) -> None:
        """Second chance at the MAC after a startup fallback, then repair the registry."""
        resolved = await self.async_resolve_gateway_id()
        if self._gateway_id is None:
            return

        _LOGGER.info(
            "Resolved gateway MAC for %s after starting on fallback id %s: %s",
            self._host,
            self._registered_gateway_id or self._host.replace('.', ''),
            resolved,
        )
        _migrate_gateway_device_id(
            self._hass, self._config_entry, self._unique_id, resolved
        )

    def get_pump_management(self):
        var = 'sys_pump_management'
        return self._data.get(var)

    async def async_set_pump_management(self, value):
        var = 'sys_pump_management'
        await self._client.send_data({var: value})
        self._data[var] = value
        self._hass.async_create_task(self.call_state_update())

    def is_autoupdate(self):
        var = 'cust_Enable_SW_Update'
        return var in self._data and self._data[var] == "1"

    async def async_set_autoupdate(self, set_on):
        var = 'cust_Enable_SW_Update'
        data = "1" if set_on else "0"
        await self._client.send_data({var: data})
        self._data[var] = data
        self._hass.async_create_task(self.call_state_update())

    def get_gateway_status(self):
        if self.is_available() is None:
            return STATUS_OFFLINE
        var = 'cust_controller_1_lost'
        if var in self._data and self._data[var] == "1":
            return STATUS_ERROR_MAINCONTROLER_FAIL        
        return STATUS_ONLINE 
        
    def get_controller_relayconfig(self, controller):
        var = controller + '_controller_relays_config'
        if var in self._data:
            match self._data[var]:
                case "1":
                    return "not_in_use"
                case "3":
                    return "pump_heater"
                case "4":
                    return "pump_eco_comfort"
                case "7":
                    return "not_configured"
        return None

    async def async_set_controller_relayconfig(self, controller, value):
        var = controller + '_controller_relays_config'
        await self._client.send_data({var: value})
        self._data[var] = value
        self._hass.async_create_task(self.call_state_update())

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

    def get_bypass_enable(self, thermostat):
        return self._data.get(thermostat + '_bypass_enable') == "1"

    async def async_set_bypass_enable(self, thermostat, value):
        var = thermostat + '_bypass_enable'
        data = "1" if value else "0"
        await self._client.send_data({var: data})
        self._data[var] = data
        self._hass.async_create_task(self.call_state_update())

    def get_pump_relay(self, controller):
        var = controller + '_stat_pump_relay'
        return self._data.get(var) == "1"

    def get_boiler_demand(self, controller):
        var = controller + '_stat_demand'
        return self._data.get(var) == "1"

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
        async with self._storage_lock:
            await self._async_load_storage_unlocked()

    async def _async_load_storage_unlocked(self):
        """Load storage while the caller holds _storage_lock."""
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

    def get_cached_controllers(self):
        controllers = self._storage_metadata.get("controllers", [])
        ids = self._storage_metadata.get("controller_ids", {})
        if isinstance(controllers, list) and controllers and all(ids.get(controller) for controller in controllers):
            return controllers
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

        async with self._storage_lock:
            # Merge with previously cached thermostats so that a transient
            # JNAP response missing one thermostat does not permanently remove
            # it from cache and hide its entity after the next HA restart.
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
                "models": {
                    **self._storage_metadata.get("models", {}),
                    **{
                        thermostat: model
                        for thermostat in thermostats
                        if (model := self._detect_thermostat_model(thermostat))
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
        model = self._detect_thermostat_model(thermostat)
        if model is not None:
            return model
        # Fall back to the cached detection so the model (and the gating that
        # depends on it) is available before the first live update.
        return self._storage_metadata.get("models", {}).get(thermostat)

    def _detect_thermostat_model(self, thermostat):
        """Identify a thermostat from its hardware type within the product series.

        The two Smatrix ranges run in parallel (T-146<->T-166, T-148<->T-168,
        T-149<->T-169) and report the same `_thermostat_type`, so that value
        alone cannot name a model - every `_thermostat_type == 2` unit used to
        be reported as T-146, including the sixteen T-169s that prompted
        issue #36. `_hw_type` does separate them once the series is known.

        Reference ranges, from the product documentation:
          Base Pulse (X-245): T-141 T-143 T-144 T-145 T-146 T-148 T-149
          Wave Pulse (X-265): T-161 T-162 T-163 T-165 T-166 T-168 T-169

        Only combinations actually observed on a system whose models were
        confirmed by its owner appear in THERMOSTAT_MODELS. Anything else
        returns None - the raw hardware type is a device class, not a model
        id, and reporting no model beats reporting a wrong one.
        """
        var = thermostat + '_thermostat_type'
        if var not in self._data:
            return None
        hwid = int(self._data[var])

        if hwid == 0:
            # The dial family. T-144 and T-145 report the same hardware id and
            # the same _hw_type, so the serial prefix is still the only thing
            # separating them. This rule is field-confirmed on Base units
            # only; a Wave T-165 has never been observed, so it is left to
            # fall through to the series/hw_type lookup below rather than
            # being labelled from Base data.
            sn = self.get_thermostat_id(thermostat)[:4]
            prodk = sn[:3]
            mod = sn[-1:]
            if self.get_product_series(thermostat.split('_')[0]) != SERIES_WAVE:
                if prodk == "269" and mod == "1":
                    return 'T-144'
                # Every other Base dial defaults to T-145 - Uponor's own app
                # appears to do the same when it cannot tell either.
                return 'T-145'

        series = self.get_product_series(thermostat.split('_')[0])
        hw_type = self._data.get(thermostat + '_hw_type')
        if series is not None and hw_type is not None:
            model = THERMOSTAT_MODELS.get((series, str(hw_type)))
            if model is not None:
                return model

        _LOGGER.debug(
            "Unrecognised thermostat %s: series=%s thermostat_type=%s hw_type=%s "
            "sn_start=%s rh=%s floor=%s public=%s sensor_only=%s - reporting no "
            "model. Please attach this line to a report so it can be mapped.",
            thermostat, series, hwid, hw_type, self.get_thermostat_id(thermostat)[:4],
            self.has_humidity_sensor(thermostat), self.has_floor_temperature(thermostat),
            self.is_public_device(thermostat), self.is_sensor_only(thermostat),
        )
        return None

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

        return cool_setback + self._get_active_eco_setback(thermostat)

    def _get_active_eco_setback(self, thermostat):
        var = thermostat + '_eco_offset'
        if var not in self._data or not self.is_setback_active(thermostat):
            return 0

        mode = -1 if self.is_cool_enabled() else 1
        return int(self._data[var]) * mode

    def requires_local_override(self, thermostat):
        return self.get_thermostat_model(thermostat) in DIAL_THERMOSTAT_MODELS

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
        off_temp = self.get_max_limit(thermostat) if self.is_cool_enabled() else self.get_min_limit(thermostat)
        last_temp = self._storage_data.get(thermostat, DEFAULT_TEMP)
        if last_temp == off_temp:
            # A poisoned memo (recorded while already at the off value) must
            # not strand the room off forever.
            last_temp = DEFAULT_TEMP
        await self.async_set_setpoint(thermostat, last_temp)

    async def async_turn_off(self, thermostat):
        off_temp = self.get_max_limit(thermostat) if self.is_cool_enabled() else self.get_min_limit(thermostat)
        current = self.get_setpoint(thermostat)
        async with self._storage_lock:
            await self._async_load_storage_unlocked()
            if current != off_temp:
                # Don't record the off value itself as the restore target.
                self._storage_data[thermostat] = current
                await self._store.async_save(self._compose_storage_payload())
        await self.async_set_setpoint(thermostat, off_temp)

    async def async_remember_setpoint(self, thermostat, temp):
        """Record a target temperature requested while the room is off, so turn_on restores it."""
        async with self._storage_lock:
            await self._async_load_storage_unlocked()
            self._storage_data[thermostat] = temp
            await self._store.async_save(self._compose_storage_payload())

    async def async_set_preset_mode(self, preset_mode):
        if preset_mode in (PRESET_AWAY, PRESET_ECO):
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

    def is_setback_active(self, thermostat):
        return self.is_away() or self.is_eco(thermostat)

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

                # Runs here rather than at setup because model detection is
                # only authoritative with live data; on a cache-based startup
                # it would treat dial thermostats as unsupported.
                if not self._stale_override_switches_cleaned:
                    self._stale_override_switches_cleaned = True
                    _remove_unsupported_local_override_entities(
                        self._hass, self._config_entry, self._unique_id,
                        self, self.get_active_thermostats(),
                    )

                # The MAC may not have been resolvable at setup. Retry here so
                # the session converges on the real identifier rather than
                # running on the host fallback until the next restart.
                if self._gateway_id is None:
                    await self._async_retry_gateway_id()

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

    async def async_set_target_temperature(self, thermostat, temp):
        if self.is_setback_active(thermostat):
            await self.async_set_setback_target(thermostat, temp)
            return

        await self.async_set_setpoint(thermostat, temp)

    async def async_set_setback_target(self, thermostat, temp):
        current_target = self.get_setpoint(thermostat)
        if current_target is None:
            await self.async_set_setpoint(thermostat, temp)
            return

        active_eco_setback = self._get_active_eco_setback(thermostat)
        comfort_target = current_target + active_eco_setback / 18
        mode = -1 if self.is_cool_enabled() else 1
        offset = round((comfort_target - temp) * 18 / mode)

        if offset < 0:
            _LOGGER.warning(
                "Requested setback target %.1f for %s would require a negative eco offset; using 0 instead",
                temp,
                thermostat,
            )
            offset = 0

        var = thermostat + '_eco_offset'
        await self._client.send_data({var: offset})
        self._data[var] = offset
        self._hass.async_create_task(self.call_state_update())

    async def async_set_setpoint(self, thermostat, temp):
        var = thermostat + '_setpoint'
        setpoint = int(temp * 18 + self.get_active_setback(thermostat, temp) + 320)
        await self._client.send_data({var: setpoint})
        self._data[var] = setpoint
        self._hass.async_create_task(self.call_state_update())
