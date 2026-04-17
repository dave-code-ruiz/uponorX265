# Fix branch summary (`fix/ha-retry-reload-storm`)

This branch contains stability-focused changes compared to `main`.

## Changes vs main

- Migrated Uponor HTTP calls to async `aiohttp`
- Added explicit timeouts and bounded retries
- Prevented overlapping update runs with an async lock
- Added reload cooldown to avoid reload storms during outages
- Added cached thermostat discovery metadata for startup resilience
- Added entity availability handling for cleaner outage/recovery behavior
- Updated config flow to use async client path

## Why

To reduce Home Assistant resource spikes when the controller is unavailable,
improve startup behavior with many thermostats, and make entities recover more reliably after temporary network/controller failures.

## Additional fixes vs `main`

### Bugs fixed
- **`async_set_preset_mode` inverted logic** (`__init__.py`): AWAY/COMFORT calls to `async_set_away` were swapped — selecting Comfort enabled away mode and vice versa.
- **ECO preset branch was dead code** (`climate.py`): The `async_set_preset_mode` ECO branch reassigned a local variable but never acted on it. Now correctly calls `async_set_preset_mode` with the computed target preset.
- **`preset_mode` could return `PRESET_AWAY` not in `preset_modes`** (`climate.py`): Added `PRESET_AWAY` to the `preset_modes` list to match what `preset_mode` can return.

### Unused / dead code removed
- Removed unused `from homeassistant.components import climate` import and its comment (`__init__.py`).
- Removed unused `TEMP_CELSIUS = UnitOfTemperature.CELSIUS` module-level alias (`__init__.py`).
- Removed unused `from homeassistant.util.unit_system import UnitOfTemperature` import (`__init__.py`).
- Removed unused `TEMP_CELSIUS = '°C'` constant (`const.py`).
- Removed unused `TOO_LOW_HUMIDITY_LIMIT = 0` constant (`const.py`) and its always-true `>= 0` check in `get_humidity` (`__init__.py`).
- Removed deprecated `device_state_attributes` property from `UponorClimate` (`climate.py`) — identical duplicate of `extra_state_attributes`.
- Removed redundant `_config_entry` attribute and `config_entry` property override from `OptionsFlowHandler` (`config_flow.py`) — base class provides this automatically.

### Robustness fixes
- **`entry.state.name` string comparison** (`__init__.py`): Replaced fragile string comparison `entry.state.name in ("LOADED", "SETUP_RETRY")` with proper enum check using `ConfigEntryState`.
- **`set_variable` service input validation** (`__init__.py`): Added guard against `None` `var_name` to prevent malformed JNAP requests.
- **`async_step_rooms` None-guard** (`config_flow.py`): Added `if user_input is None` guard to prevent `TypeError` on unexpected calls.
- **Float equality in `get_active_setback`** (`__init__.py`): Replaced direct `==` comparison of floating-point temperatures with an epsilon (`< 0.05`) comparison to prevent precision mismatches at min/max limits.

### Consistency fixes
- **`sensor.py` hardcoded `"Uponor"` strings**: Replaced with `DEVICE_MANUFACTURER` constant in all three sensor classes' `device_info`.
- **`_enable_turn_on_off_backwards_compatibility` on `UponorFloorTemperatureSensor`**: Removed irrelevant climate flag from a sensor entity class.
