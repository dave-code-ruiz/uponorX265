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

CONF_CREATE_CONTROLLERS = "create_controllers"
CONF_SENSOR_TEMP = "sensor_temperature"
CONF_BINARY_SENSOR_VALVE = "binary_sensor_valve"
CONF_SWITCH_SENSOR_AVG = "switch_sensor_avg"
CONF_CONTROLLER_IO = "controller_io"
CONF_INSTALLER_SETTINGS = "installer_settings"
TOO_HIGH_TEMP_LIMIT = 4508
DEFAULT_TEMP = 20
