from datetime import timedelta

CONF_UNIQUE_ID = "unique_id"

DOMAIN = "uponorx265"

SIGNAL_UPONOR_STATE_UPDATE = "uponor_state_update"
SCAN_INTERVAL = timedelta(seconds=30)
UNAVAILABLE_THRESHOLD = timedelta(minutes=2)
RELOAD_COOLDOWN = timedelta(minutes=10)

STORAGE_KEY = "uponorx265_data"
STORAGE_VERSION = 1

DEVICE_MANUFACTURER = "Uponor"

STATUS_OK                       = 'ok'
STATUS_ERROR_BATTERY            = 'battery_error'
STATUS_ERROR_VALVE              = 'valve_error'
STATUS_ERROR_GENERAL            = 'general_error'
STATUS_ERROR_AIR_SENSOR         = 'air_sensor_error'
STATUS_ERROR_EXT_SENSOR         = 'ext_sensor_error'
STATUS_ERROR_RH_SENSOR          = 'rh_sensor_error'
STATUS_ERROR_RF_SENSOR          = 'rf_sensor_error'
STATUS_ERROR_TAMPER             = 'tamper_error'
STATUS_ERROR_TOO_HIGH_TEMP      = 'api_error'
STATUS_ERROR_COMFAILOUT         = 'comfail_out'
STATUS_ERROR_CONTROLER          = 'comfail_controller'
STATUS_ONLINE                   = 'online'
STATUS_OFFLINE                  = 'offline'
STATUS_ERROR_MAINCONTROLER_FAIL = 'comfail_main_controller'
PRESET_MANUAL = 'ha_controlled'

# Dial thermostats accept remote setpoint changes only while local override
# is enabled; all other models accept them directly.
DIAL_THERMOSTAT_MODELS = ("T-144", "T-145")

# Product series. Smatrix Wave Pulse is wireless and built on the X-265
# controller; Smatrix Base Pulse is a wired bus on the X-245. The two run
# parallel thermostat ranges (T-146<->T-166, T-148<->T-168, T-149<->T-169)
# that report identical values for everything except the hardware type, so
# the series has to be resolved before a thermostat can be identified.
SERIES_WAVE = "wave"
SERIES_BASE = "base"

# The controller firmware image name (cust_SW_version_update, e.g.
# "X265_121.hex") states the controller model outright. This is a read, not
# an inference, so it is tried first.
CONTROLLER_FIRMWARE_SERIES = {
    "X265": SERIES_WAVE,
    "X245": SERIES_BASE,
}

# Fallback: the controller's own hardware type register. Agrees with the
# firmware image name on every system observed.
CONTROLLER_HARDWARE_SERIES = {
    "1": SERIES_WAVE,
    "0": SERIES_BASE,
}

SERIES_CONTROLLER_MODELS = {
    SERIES_WAVE: "X-265",
    SERIES_BASE: "X-245",
}

# (series, C?_T?_hw_type) -> thermostat model.
#
# Derived from field data, not from Uponor documentation - no public source
# maps these codes. Every entry below is backed by a system whose models were
# confirmed by its owner. Codes that are absent resolve to None rather than to
# a guess: an unknown model is better than a wrong one (issue #36).
#
# Deliberately NOT keyed on humidity or floor-temperature readings. Those
# report what a unit is currently sensing, not what it is capable of - a
# T-146 with no RH sensor and a T-169 sitting at 0% RH are indistinguishable
# that way.
THERMOSTAT_MODELS = {
    (SERIES_WAVE, "7"): "T-169",
    (SERIES_BASE, "3"): "T-146",
}

CONF_CREATE_CONTROLLERS = "create_controllers"
CONF_SENSOR_TEMP = "sensor_temperature"
CONF_BINARY_SENSOR_VALVE = "binary_sensor_valve"
CONF_SWITCH_SENSOR_AVG = "switch_sensor_avg"
CONF_CONTROLLER_IO = "controller_io"
CONF_INSTALLER_SETTINGS = "installer_settings"

# The documented default for every optional feature, matching the defaults the
# config flow offers. Config entries created before the 1.1.5 refactor carry
# none of these keys, so setup fills them in - see async_setup_entry.
FLAG_DEFAULTS = {
    CONF_SENSOR_TEMP: True,
    CONF_CREATE_CONTROLLERS: True,
    CONF_BINARY_SENSOR_VALVE: False,
    CONF_SWITCH_SENSOR_AVG: False,
    CONF_CONTROLLER_IO: False,
    CONF_INSTALLER_SETTINGS: False,
}

TOO_HIGH_TEMP_LIMIT = 4508
DEFAULT_TEMP = 20
