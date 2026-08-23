# System Description – uponorX265

Home Assistant custom integration that connects to an **Uponor Smatrix Pulse** gateway over the local network and exposes the heating/cooling system's thermostats, controllers, and gateway as HA entities. All communication happens locally (`iot_class: local_polling`) — no cloud involved.

## Contents

1. [Architecture](#architecture)
2. [Testing](#testing)
3. [Functionality](#functionality)
4. [Official Pulse app (reference)](#official-pulse-app-reference)
5. [Raw data (JNAP variables)](#raw-data-jnap-variables)
6. [Supported hardware](#supported-hardware)

---

## Architecture

### Communication layer — [jnap.py](custom_components/uponorx265/jnap.py)
`UponorJnap` speaks JNAP (JSON Network API) to the gateway's `/JNAP/` endpoint via `aiohttp`. Two operations: `get_data()` (fetches all variables as a flat dict `waspVarName → waspVarValue`) and `send_data()` (writes variables). Built-in retry logic (2 attempts, 1s delay) and a shared timeout; network errors are converted to `HomeAssistantError`.

### State layer — [__init__.py](custom_components/uponorx265/__init__.py)
`UponorStateProxy` is the central class: keeps the raw data (`_data`) in memory, polls the gateway on `SCAN_INTERVAL` (30s), and exposes typed getters/setters (`get_setpoint`, `async_set_target_temperature`, `get_bypass_enable`, etc.) that the platform files build entities from. On every update, `SIGNAL_UPONOR_STATE_UPDATE` is sent via HA's dispatcher so all entities update at once. Data is also cached in `Store` (per config entry) for fast restarts. The module also registers the integration's services (`set_variable`, `dump_hardware_info`, `dump_raw_data`).

### Gateway ID (MAC resolution) — [__init__.py](custom_components/uponorx265/__init__.py) / [helper.py](custom_components/uponorx265/helper.py)
The gateway's `device_info` identifier and serial number are based on its MAC address when it can be resolved, otherwise it falls back to a host-based ID (the IP address with dots stripped). `UponorStateProxy.async_resolve_gateway_id()` runs in `async_setup_entry` before the platforms are set up (since `device_info` reads `get_gateway_id()`), and tries, in order:

1. `get_mac_address(ip=host)` directly — works if the OS's ARP cache already has an entry.
2. `_get_mac_with_arp_refresh()` in [helper.py](custom_components/uponorx265/helper.py) — primes the ARP cache by actually sending data over a UDP socket (not just `connect()`, which doesn't guarantee anything is sent), then tries `getmac` again, and as a last resort reads `/proc/net/arp` directly (for HA installs in Docker where the `arp`/`ip neighbor` binaries may be missing from the container).
3. The MAC address is uppercased (`.upper()`) before being used as the ID.

**Important limitation:** ARP only works within the same broadcast domain/subnet. If the HA host and the gateway are on different subnets/VLANs, the MAC address can never be resolved (the kernel never gets an ARP entry for it), and it permanently falls back to the host-based ID — this is not a code bug, it's a network topology limitation.

Since the gateway ID's format can change over time (host-based → lowercase MAC → uppercase MAC, in the order the integration has evolved) — and the host-based fallback itself can drift across restarts if MAC resolution keeps failing while DHCP reassigns the IP — `_migrate_gateway_device_id()` handles the transition. Rather than guessing at specific old id strings (which would miss that drift), it identifies the old device *structurally*: for a given config entry there is exactly one device with no `via_device` (the root of the gateway/controller/thermostat hierarchy), whatever identifier it currently happens to hold. It renames that device's identifier in place if no device already exists under the new identifier, or carries over the area/custom name and removes the old device if one does (created by a prior restart before this reconciliation ran). This runs on every startup and is idempotent.

### Device registration ordering — [__init__.py](custom_components/uponorx265/__init__.py)
Thermostat and controller entities declare a `via_device` pointing at their parent (controller, then gateway) in their `device_info`. Historically the parent device only ever got created as a side effect of a specific entity — a controller status sensor, gated behind the optional `CONF_CREATE_CONTROLLERS` — which lives in the `SENSOR` platform, loaded *after* `CLIMATE` and `SWITCH` in `PLATFORMS`. HA would log a `via_device` referencing a non-existing device warning and eventually stop honoring it, and if the controller sensor was disabled the parent device was never created at all.

`_register_gateway_devices()` fixes this by registering the gateway and controller devices explicitly in `async_setup_entry`, before `async_forward_entry_setups()` is called — so the parent always exists regardless of platform order or which optional entities are enabled. It falls back to `get_cached_controllers()` (mirroring `get_cached_thermostats()`) when live data isn't loaded yet, e.g. on a warm restart where `async_update()` runs as a background task instead of being awaited.

### Setpoint storage & restore-on-off — [__init__.py](custom_components/uponorx265/__init__.py) / [climate.py](custom_components/uponorx265/climate.py)
The integration has no real on/off register — "off" is encoded as `setpoint == min_temp` (or `max_temp` in cool mode). The `.storage` file's per-thermostat setpoint memo is therefore the only thing that can restore a room to its pre-off temperature, which makes its read-modify-write path load-bearing:

- **`self._storage_lock`** (`asyncio.Lock`) serialises every read-modify-write against `_storage_data`: `async_turn_off()`, `async_remember_setpoint()`, and the metadata-refresh save in `_async_persist_discovery_metadata()` all take it. Without this, HA turning off several thermostats in one service call runs those coroutines concurrently; each would load its own fresh copy of the storage dict, mutate only its own key, and save — last writer wins, silently discarding every other room's memo.
- **`async_turn_off()`** never memorises the off value (`min_temp`/`max_temp`) itself as the restore target — doing so would permanently strand the room off, since `async_turn_on()` would just write the off value straight back. **`async_turn_on()`** also treats a previously-poisoned memo (one that does equal the off value, e.g. left over from before this fix) as "no memo" and falls back to `DEFAULT_TEMP` instead of restoring it.
- **`async_remember_setpoint()`** backs `UponorClimate.async_set_temperature()` when the room is off: rather than silently discarding the request (the old behavior) or writing the live setpoint through (which would silently turn the room back on, violating `hvac_mode: off`), it records the requested temperature in storage so the next `turn_on` restores exactly what was asked for.

### Entity base — [helper.py](custom_components/uponorx265/helper.py)
Three base classes build a device hierarchy in HA:

| Base class | Device | `via_device` |
|---|---|---|
| `UponorGatewayEntity` | Gateway (root) | — |
| `UponorControllerEntity` | Controller | Gateway |
| `UponorThermostatEntity` | Thermostat | Controller |

All inherit poll-free behavior (`should_poll = False`) and subscribe to the dispatcher signal for push updates.

### Platform files
One file per HA domain; each reads `hass.data[unique_id]` for the state_proxy + lists of controllers/thermostats and builds entities:

| File | Contents |
|---|---|
| [climate.py](custom_components/uponorx265/climate.py) | The main entity per thermostat (temperature, HVAC mode, presets: Comfort/Eco/Away/HA controlled) |
| [sensor.py](custom_components/uponorx265/sensor.py) | Temperature, humidity, status, relay configuration, pump management, etc. |
| [binary_sensor.py](custom_components/uponorx265/binary_sensor.py) | Valve, pump relay, boiler demand, bypass (read-only) |
| [switch.py](custom_components/uponorx265/switch.py) | Away, cool mode, HA override (dial thermostats), auto-update, bypass (installer mode) |
| [select.py](custom_components/uponorx265/select.py) | Relay configuration and pump management (writable, installer mode) |
| [config_flow.py](custom_components/uponorx265/config_flow.py) | Setup wizard + options flow (host, controller names, room names, feature toggles) |

---

## Testing

A `pytest` suite lives under [tests/](tests/), using `pytest-homeassistant-custom-component` for a real (in-memory) `hass` instance — the JNAP client is always mocked, no real network calls are made. Run with:

```
pip install -r requirements_test.txt
pytest tests/ -v
```

`tests/helpers.py` provides `make_state_proxy()`, which builds a `UponorStateProxy` backed by a mocked `UponorJnap` client and a `hass`-backed `Store`, and `thermostat_data()` for constructing raw `_data` entries at a given setpoint/limits.

The suite is weighted towards regression coverage for bugs found during review rather than exhaustive coverage of the whole integration:

| File | Covers |
|---|---|
| [test_turn_off_storage_race.py](tests/test_turn_off_storage_race.py) | The storage lock — concurrent `async_turn_off()` calls must not lose each other's memo |
| [test_poisoned_memo.py](tests/test_poisoned_memo.py) | The off value is never memorised as a restore target, and a poisoned memo self-heals on `turn_on` |
| [test_set_temperature_while_off.py](tests/test_set_temperature_while_off.py) | `set_temperature` on an off room is remembered, not silently discarded |
| [test_device_registration.py](tests/test_device_registration.py) | Gateway/controller devices exist before platform setup, regardless of `CONF_CREATE_CONTROLLERS` |
| [test_gateway_device_migration.py](tests/test_gateway_device_migration.py) | The gateway device migration finds the old device structurally, including after host-id drift |
| [test_bypass_max_two.py](tests/test_bypass_max_two.py) | The max-2-bypass-zones-per-controller business rule, and that it's per-controller not global |
| [test_thermostat_model_detection.py](tests/test_thermostat_model_detection.py) | The `hwid`/serial-prefix model detection heuristic and its cache fallback |

Note on the storage-race test specifically: `pytest-homeassistant-custom-component`'s mocked `Store.async_save` never actually suspends (no executor read, no disk write), so without an explicit forced yield point (`asyncio.sleep(0)` injected into the mock) the test can pass "by accident" on platforms/schedulers where the mocked coroutines happen to run to completion sequentially anyway — masking a regression instead of catching it. The injected yield point makes the test deterministic regardless of platform.

Windows-specific: `tests/conftest.py` neutralises `pytest_socket.disable_socket()`, which `pytest-homeassistant-custom-component` calls unconditionally before every test. On Windows, asyncio's event loop needs a real socketpair for its internal self-pipe, so blocking all sockets breaks fixture setup before any test code runs; since no test here makes real network calls anyway, this is safe to disable rather than fight.

## Functionality

**Core function:** every physical thermostat becomes a `climate` entity with a target temperature, current temperature/humidity, HVAC mode (Heat/Cool + Off), and presets. Dial thermostats (T-144/T-145) require "HA controlled" mode (local override) before HA can control the setpoint.

**Two-tier feature model**, driven by flags in the config entry:
- `controller_io` → creates relay/IO sensors per controller (pump relay, boiler demand)
- `installer_settings` ("Installer mode") → makes relay configuration, bypass, and pump management **writable** (select/switch); otherwise the same data is shown as **read-only sensors**. The same `unique_id` format is shared between the writable/read-only version so history is preserved when toggling.

**Business rules built into the entities:**
- Max 2 active bypass zones per controller (enforced in `BypassEnableSwitch.async_turn_on`, raises `HomeAssistantError` otherwise)
- Pump relay is hidden for C2–C4 when pump management is set to "common"
- Bypass defaults to off

**Multi-gateway support:** multiple config entries can run in parallel; services like `set_variable` and `dump_raw_data` match against the right gateway via `device_id`, or the single configured one if only one exists.

**Migration:** `_migrate_entity_unique_ids` handles historical unique_id format changes (prefix addition, climate suffix) automatically on startup so upgrades don't create duplicates.

---

## Official Pulse app (reference)

The Uponor Smatrix Pulse app (requires a communication module, see [R-208](#uponor-smatrix-pulse-com-r-208-communication-module)) has the following menu options per thermostat:

- **My ECO profiles** — Comfort/ECO schedule per room
- **Show trends** — history of temperature/humidity over time
- **Room settings** — configuration for the individual room's thermostat

These features live in Uponor's app/cloud and have no equivalent in the integration today — the JNAP gateway doesn't expose ECO profile schedules or historical trend data, only current variable values (see [dump_raw_data](custom_components/uponorx265/__init__.py)).

### Structure under "My ECO profiles"

**System ECO adjustment**
- Global temperature offset for ECO mode, −4 °C to +4 °C.

**Preset profiles**
- 6 of them (ECO profile 1–6), each with:
  - Day selection (which weekdays the profile applies to)
  - 3 time intervals per day, each with an ECO on/off time

**My ECO profiles** (user-defined, per room)
- Each profile has: name (editable), assigned room, Mon–Sun schedule
- "Add ECO" creates a new custom profile

None of these schedules/profiles are represented as HA variables in the data the integration reads via JNAP — they're handled entirely by the app/controller's internal logic.

### Structure under "Room settings"

- **Room name** → change the room's name
- **ECO profile** → assign one of "My ECO profiles" (e.g. "My ECO profile 1") to the room
- **ECO temperature setback** → set the temperature setback for the room, 0.5–10 °C
- **Override thermostat value** → on/off toggle
- **Advanced room settings** →
  - **Max setpoint** → 5–35 °C
  - **Min setpoint** → 5–35 °C
  - **Include in average temperature** → on/off; display value only, doesn't affect operation (ON by default)
  - **Comfort setting** → 0–12%, baseline comfort level when there's no heating demand (shortens heat-up time, e.g. with another heat source like a wood stove — the value is the fraction of time the actuators are held open)
  - **Floor temperature** (display) plus **Maximum/Minimum floor temperature** (limits, only in RFT control mode)

Comparison against the integration:
- **Max/Min setpoint** already corresponds to `get_max_limit()` / `get_min_limit()` in [climate.py](custom_components/uponorx265/climate.py) (`min_temp`/`max_temp` properties).
- **ECO temperature setback** corresponds to `get_eco_setback()`, exposed via `UponorClimate.extra_state_attributes`.
- **Include in average temperature** corresponds to the `ClimatControlInAvg` switch (`avg_included`, controlled by `get_inavg()`/`async_iset_inavg()`) in [switch.py](custom_components/uponorx265/switch.py), gated behind `CONF_SWITCH_SENSOR_AVG`.
- **ECO profile, Room name, Override thermostat value, Comfort setting, Floor temperature limits** have no equivalent in the integration today — not available via the JNAP variables it reads.

### System settings / installer settings in the app

Accessed via the app's side menu → "System settings", or specifically "Installer settings" (warning text: *"Changing these settings may cause your system to stop working correctly"*).

- **Cooling** → enable cooling mode in the system (disabled at delivery); then gives access to cooling settings
- **GPI configuration** → sets which signal type the controller's GPI (general purpose input) accepts: **Comfort/ECO switching** or **General system alarm** (heat/cool switching requires the system to have heating/cooling; automatically disabled if an external Comfort/ECO switch, e.g. a T-143 registered as a system device, is already connected)
- **Pump management** → **Individual** (one circulation pump per controller, connected to relay 1) or **Common** (one pump for the whole system, connected to the master controller's relay 1 — the relays on slave controllers then become available for other functions)
- **Controller relays** → two independent relays (Relay 1 / Relay 2) per controller, with predefined combinations:
  - **Master controller:** Circulation pump+Boiler (default) · Circulation pump+Heat/cool switching · Circulation pump+Dehumidifier · Cooling unit+Boiler · Circulation pump+Comfort/ECO · Not configured+Not configured
  - **Slave controller** (requires communication module): Circulation pump+Heat/cool switching · Circulation pump+Dehumidifier · Not configured+Not configured
- **Bypass room** → the system handles bypass for **up to two rooms per controller** (to maintain minimum flow); rooms are chosen manually per controller tab, or with a time limit for the bypass function
- **Valve/pump exercise** → prevents circulation pumps/actuators from seizing up during extended inactivity. By default: every 6th day ±24 h, the pump runs for 3 minutes, the actuators are fully opened/closed. Runs independently per component, only if the component hasn't been used since the last exercise
- **Autobalancing** → **Enabled** (default) or **Disabled**; controls the actuator outputs via pulse-width modulation (PWM) instead of simple on/off signals, giving more even floor temperatures, faster response time, and lower energy consumption. Can be combined with pre-balancing
- **Installation name** → free text field
- **Low average temperature limit** → triggers an alarm if the system's average temperature (calculated from rooms flagged "Include in average temperature") falls below the threshold. Default 10 °C (5–30 °C), plus hysteresis, default 5 °C (1–10 °C); the alarm clears when the average temperature rises above threshold + hysteresis
- **System information** → list of all connected devices (controller, communication module, thermostats) with model, software version, and ID, plus the ability to trigger an update
- **Supply temp. control** → on/off toggle (supply water temperature monitoring)

Comparison against the integration:
- **Pump management** (Individual/Common) corresponds exactly to `PumpManagementSelect`/`get_pump_management()`/`sys_pump_management` in [select.py](custom_components/uponorx265/select.py) and [__init__.py](custom_components/uponorx265/__init__.py) — the same `"0"`/`"1"` values.
- **Controller relays** corresponds to `ControllerRelayConfigSelect`/`get_controller_relayconfig()` (`C?_controller_relays_config`) in the same files; the integration's `RELAY_CONFIG_OPTIONS` (`not_in_use`/`pump_heater`/`pump_eco_comfort`/`not_configured`) is a simplified subset of the app's combination table.
- **Bypass room, max 2 per controller** confirms exactly the business rule already hardcoded in `BypassEnableSwitch.async_turn_on` ([switch.py](custom_components/uponorx265/switch.py)) — the app's limit and the integration's limit are therefore identical.
- **Cooling, GPI configuration, Valve/pump exercise, Autobalancing, Low average temperature limit, Installation name, System information, Supply temp. control** have no equivalent in the integration today.

---

## Raw data (JNAP variables)

The `dump_raw_data` service returns the entire `_data` dictionary as-is — every `waspVarName`/`waspVarValue` pair the gateway exposes. The variable names follow a few clear prefix patterns:

| Prefix | Level | Example | Contents |
|---|---|---|---|
| `cust_*` | Gateway/customer | `cust_Controller1_Name`, `cust_C1_T1_name`, `cust_wifi_device`, `cust_ip_device`, `cust_Enable_SW_Update`, `cust_General_RH_Setpoint`, `cust_Low_temperature_Limit` | Names (controllers, rooms), network, firmware update, alarm limits |
| `sys_*` | System | `sys_pump_management`, `sys_autobalance`, `sys_heat_cool_mode`, `sys_time_limit_bypass`, `sys_day`/`sys_Month`/`sys_year`/…, `sys_controller_?_presence` | Global operating settings, system clock, which controllers are connected |
| `C?_*` (no `T?`) | Controller | `C1_controller_relays_config`, `C1_stat_pump_relay`, `C1_stat_demand`, `C1_output_module_configuration`, `C1_general_purpose_input`, `C1_average_room_temperature`, `C1_sw_version` | Relay configuration, pump/boiler status, GPI, average temperature, software version, alarms |
| `C?_T?_*` | Thermostat | `C1_T1_setpoint`, `C1_T1_room_temperature`, `C1_T1_eco_setting`, `C1_T1_bypass_enable`, `C1_T1_eco_profile_number`, `C1_T1_stat_*_error` | Setpoint/room temperature, ECO settings, bypass, error statuses per thermostat |
| `C?_T?_<Weekday>` | Thermostat, schedule | `C1_T1_Monday` … `C1_T1_Sunday` | 12-character hex bitmask per weekday — the schedule for the ECO profile assigned to the room |
| `controller?_id`, `C?_thermostat?_id`, `C?_TTH_?_id` | Identities | `controller1_id`, `C1_thermostat1_id` | Hardware IDs for controllers, thermostats, and external TTH sensors |

**Correction to an earlier note:** the ECO profiles' weekly schedule is actually available in the raw data via the `C?_T?_<Weekday>` bitmasks (e.g. `C1_T1_Monday: c0ffffffffff`) together with `C?_T?_eco_profile_number` and `cust_C?_T?_Custom_Eco_Profile`. What's missing isn't the data itself, but an interpretation/exposure of it in the integration — the bitmask format isn't decoded anywhere in the code today.

Other observations from the dump:
- `sys_pump_management: '1'` (common) and `C1_controller_relays_config: '3'` / `C2_controller_relays_config: '1'` show `pump_heater` on C1 and `not_in_use` on C2 — matches the rule that C2's pump relay is hidden when pump management is common.
- `C1_stat_pump_relay` and `C1_stat_demand` are boolean strings (`'0'`/`'1'`), as expected by `get_pump_relay()`/`get_boiler_demand()`.
- Temperature values (`setpoint`, `room_temperature`, etc.) are stored as integers in tenths of a degree (e.g. `692` = 20.5 °C), `32767` means "not connected/no value".
- `C1_general_purpose_input: '3'` and `C1_output_module_configuration: '7'` are separate bitfields from `controller_relays_config` — not the same thing as the relay configuration choice in the app.

<details>
<summary>Example of full <code>dump_raw_data</code> output (anonymized)</summary>

```yaml
cust_New_ControllerSW: '0'
cust_CX_SW_Distributed: '0'
cust_Start_SW_Update: '0'
cust_Update_Counter_TimeOut: '0'
cust_Update_SW_Retries: '0'
cust_SW_Update_Fail: '0'
cust_Mini_FW_Updated: '0'
cust_General_RH_Setpoint: '75'
cust_controller_1_lost: '0'
cust_Controller1_Name: nere
cust_wifi_device: ethernet
cust_ip_device: 10.x.x.x
cust_Enable_SW_Update: '1'
cust_C1_T1_name: Renee lekrum
cust_C1_T2_name: Hallen
cust_C1_T3_name: Tv rum
cust_C1_T4_name: Gammla Kontor
cust_C1_T5_name: Badrum
cust_C1_T6_name: Vardagsrum
cust_C1_T7_name: Köket
cust_Low_temperature_Limit: '500'
cust_Enable_Low_Temp_Alarm: '0'
cust_Low_temperature_Hyst: '90'
cust_SW_version_update: X245_122.hex
cust_Succesfull_SW_Instal: '1'
cust_C2_T1_name: Emmas gammla
cust_C2_T2_name: Sovrum R&F
cust_Controller2_Name: uppe
cust_C2_T3_name: Sovrum olivia
cust_C2_T4_name: Allrum
cust_C2_T5_name: Kontor
cust_C2_T6_name: Badrum uppe
sys_valve_exercise: '0'
sys_pump_exercise: '0'
sys_supply_diagnostic: '0'
sys_autobalance: '1'
sys_pump_management: '1'
sys_rh_control_activation: '0'
sys_supply_water_activation: '0'
sys_cooling_available: '0'
sys_forced_eco_mode: '0'
sys_heat_cool_mode: '0'
sys_comm_module_exist: '1'
sys_time_limit_bypass: '0'
sys_heat_pump_dynamic_heatcurve: '0'
sys_heat_pump_response: '0'
sys_heat_pump_defrost: '0'
sys_heat_cool_master_switch: '0'
Sys_CeilingCooling_Type: '0'
sys_HC_supply_limit: '644'
sys_HC_supply_hyst: '72'
sys_first_stage_offset: '36'
sys_day: '1'
sys_Month: '8'
sys_year: '26'
sys_minutes: '29'
sys_hour: '20'
sys_days: '18'
sys_seconds: '5'
Sys_ext_outdoor_temp: '32767'
sys_heat_cool_offset: '36'
sys_eco_mode_offset: '72'
sys_indoor_temp_switch: '788'
sys_outdoor_temp_hyst: '36'
sys_outdoor_temp_switch: '824'
sys_indoor_temp_hyst: '72'
sys_indoor_temp_delay: '24'
sys_pun_protocol_version: '0'
sys_OTA_status: '0'
sys_controller_1_presence: '1'
sys_controller_2_presence: '1'
sys_controller_3_presence: '0'
sys_controller_4_presence: '0'
sys_controller_1_lost: '0'
sys_controller_2_lost: '0'
sys_controller_3_lost: '0'
sys_controller_4_lost: '0'
sys_average_relative_humidity: '0'
C1_channel_1_fancoil: '0'
C2_channel_1_fancoil: '0'
C1_channel_2_fancoil: '0'
C2_channel_2_fancoil: '0'
C1_channel_3_fancoil: '0'
C2_channel_3_fancoil: '0'
C1_channel_4_fancoil: '0'
C2_channel_4_fancoil: '0'
C1_channel_5_fancoil: '0'
C2_channel_5_fancoil: '0'
C1_channel_6_fancoil: '0'
C2_channel_6_fancoil: '0'
C1_channel_7_fancoil: '0'
C2_channel_7_fancoil: '0'
C1_channel_8_fancoil: '0'
C2_channel_8_fancoil: '0'
C1_channel_9_fancoil: '0'
C2_channel_9_fancoil: '0'
C1_channel_10_fancoil: '0'
C2_channel_10_fancoil: '0'
C1_channel_11_fancoil: '0'
C2_channel_11_fancoil: '0'
C1_channel_12_fancoil: '0'
C2_channel_12_fancoil: '0'
C1_out_relay_heat_cool_SwFunct: '0'
C2_out_relay_heat_cool_SwFunct: '0'
C1_general_purpose_input: '3'
C2_general_purpose_input: '3'
C1_output_module_configuration: '7'
C2_output_module_configuration: '7'
C1_controller_relays_config: '3'
C2_controller_relays_config: '1'
C1_channel_1_ceiling_cooling: '0'
C2_channel_1_ceiling_cooling: '0'
C1_channel_2_ceiling_cooling: '0'
C2_channel_2_ceiling_cooling: '0'
C1_channel_3_ceiling_cooling: '0'
C2_channel_3_ceiling_cooling: '0'
C1_channel_4_ceiling_cooling: '0'
C2_channel_4_ceiling_cooling: '0'
C1_channel_5_ceiling_cooling: '0'
C2_channel_5_ceiling_cooling: '0'
C1_channel_6_ceiling_cooling: '0'
C2_channel_6_ceiling_cooling: '0'
C1_channel_7_ceiling_cooling: '0'
C2_channel_7_ceiling_cooling: '0'
C1_channel_8_ceiling_cooling: '0'
C2_channel_8_ceiling_cooling: '0'
C1_channel_9_ceiling_cooling: '0'
C2_channel_9_ceiling_cooling: '0'
C1_channel_10_ceiling_cooling: '0'
C2_channel_10_ceiling_cooling: '0'
C1_channel_11_ceiling_cooling: '0'
C2_channel_11_ceiling_cooling: '0'
C1_channel_12_ceiling_cooling: '0'
C2_channel_12_ceiling_cooling: '0'
C1_channel_1_ave_temp: '0'
C2_channel_1_ave_temp: '1'
C1_channel_2_ave_temp: '0'
C2_channel_2_ave_temp: '1'
C1_channel_3_ave_temp: '1'
C2_channel_3_ave_temp: '1'
C1_channel_4_ave_temp: '1'
C2_channel_4_ave_temp: '1'
C1_channel_5_ave_temp: '0'
C2_channel_5_ave_temp: '1'
C1_channel_6_ave_temp: '0'
C2_channel_6_ave_temp: '0'
C1_channel_7_ave_temp: '0'
C2_channel_7_ave_temp: '1'
C1_channel_8_ave_temp: '1'
C2_channel_8_ave_temp: '1'
C1_channel_9_ave_temp: '1'
C2_channel_9_ave_temp: '1'
C1_channel_10_ave_temp: '1'
C2_channel_10_ave_temp: '1'
C1_channel_11_ave_temp: '1'
C2_channel_11_ave_temp: '1'
C1_channel_12_ave_temp: '1'
C2_channel_12_ave_temp: '1'
C1_rh_dead_zone: '5'
C2_rh_dead_zone: '5'
C1_rh_worst: '0'
C2_rh_worst: '0'
C1_sw_version: '290'
C2_sw_version: '290'
C1_thermostat_1_presence: '1'
C2_thermostat_1_presence: '1'
C1_thermostat_2_presence: '1'
C2_thermostat_2_presence: '1'
C1_thermostat_3_presence: '1'
C2_thermostat_3_presence: '1'
C1_thermostat_4_presence: '1'
C2_thermostat_4_presence: '1'
C1_thermostat_5_presence: '1'
C2_thermostat_5_presence: '1'
C1_thermostat_6_presence: '1'
C2_thermostat_6_presence: '1'
C1_thermostat_7_presence: '1'
C2_thermostat_7_presence: '0'
C1_thermostat_8_presence: '0'
C2_thermostat_8_presence: '0'
C1_thermostat_9_presence: '0'
C2_thermostat_9_presence: '0'
C1_thermostat_10_presence: '0'
C2_thermostat_10_presence: '0'
C1_thermostat_11_presence: '0'
C2_thermostat_11_presence: '0'
C1_thermostat_12_presence: '0'
C2_thermostat_12_presence: '0'
C1_output_module_presence: '0'
C2_output_module_presence: '0'
C1_outdoor_temp_sensor_presence: '0'
C2_outdoor_temp_sensor_presence: '0'
C1_heat_cool_presence: '0'
C2_heat_cool_presence: '0'
C1_eco_mode_presence: '0'
C2_eco_mode_presence: '0'
C1_stat_pump_relay: '0'
C2_stat_pump_relay: '0'
C1_stat_supply_temp_hi_alarm: '0'
C2_stat_supply_temp_hi_alarm: '0'
C1_stat_supply_temp_low_alarm: '0'
C2_stat_supply_temp_low_alarm: '0'
C1_eco_mode_forced_pub_thermo: '0'
C2_eco_mode_forced_pub_thermo: '0'
C1_stat_demand: '0'
C2_stat_demand: '0'
C1_stat_general_system_alarm: '0'
C2_stat_general_system_alarm: '0'
C1_device_system_alarm_eco_loss: '0'
C2_device_system_alarm_eco_loss: '0'
C1_stat_heat_cool_mode: '0'
C2_stat_heat_cool_mode: '0'
C1_stat_heat_cool_slave_input: '0'
C2_stat_heat_cool_slave_input: '0'
C1_thermostat_change_1: '0'
C2_thermostat_change_1: '0'
C1_thermostat_change_2: '0'
C2_thermostat_change_2: '0'
C1_thermostat_change_3: '0'
C2_thermostat_change_3: '0'
C1_thermostat_change_4: '0'
C2_thermostat_change_4: '0'
C1_thermostat_change_5: '0'
C2_thermostat_change_5: '0'
C1_thermostat_change_6: '0'
C2_thermostat_change_6: '0'
C1_thermostat_change_7: '0'
C2_thermostat_change_7: '0'
C1_thermostat_change_8: '0'
C2_thermostat_change_8: '0'
C1_thermostat_change_9: '0'
C2_thermostat_change_9: '0'
C1_thermostat_change_10: '0'
C2_thermostat_change_10: '0'
C1_thermostat_change_11: '0'
C2_thermostat_change_11: '0'
C1_thermostat_change_12: '0'
C2_thermostat_change_12: '0'
C1_average_room_temperature: '740'
C2_average_room_temperature: '762'
C1_average_setpoint: '32767'
C2_average_setpoint: '32767'
C1_outdoor_temperature: '32767'
C2_outdoor_temperature: '32767'
C1_alarm_type: '0'
C2_alarm_type: '0'
C1_supply_temperature: '32767'
C2_supply_temperature: '32767'
C1_worst_room_temperature: '32767'
C2_worst_room_temperature: '32767'
C1_worst_setpoint: '32767'
C2_worst_setpoint: '32767'
C1_stat_heat_pump_dyn_heat: '0'
C2_stat_heat_pump_dyn_heat: '0'
C1_hardware_type: '0'
C2_hardware_type: '0'
C1_memory_map: '1'
C2_memory_map: '1'
C1_out_module_relay1_cmd: '0'
C2_out_module_relay1_cmd: '0'
C1_out_module_relay2_cmd: '0'
C2_out_module_relay2_cmd: '0'
C1_stat_out_module_relay1: '0'
C2_stat_out_module_relay1: '0'
C1_stat_out_module_relay2: '0'
C2_stat_out_module_relay2: '0'
C1_stat_out_module_com_lost: '0'
C2_stat_out_module_com_lost: '0'
C1_pending_sw_version: '65535'
C2_pending_sw_version: '65535'
C1_bootloader_sw_version: '1044'
C2_bootloader_sw_version: '1044'
C1_T1_bypass_enable: '0'
C1_T2_bypass_enable: '0'
C1_T3_bypass_enable: '0'
C1_T4_bypass_enable: '0'
C1_T5_bypass_enable: '0'
C1_T6_bypass_enable: '0'
C1_T7_bypass_enable: '0'
C2_T1_bypass_enable: '0'
C2_T2_bypass_enable: '0'
C2_T3_bypass_enable: '0'
C2_T4_bypass_enable: '0'
C2_T5_bypass_enable: '0'
C2_T6_bypass_enable: '0'
C1_T1_manual_fan_on: '0'
C1_T2_manual_fan_on: '0'
C1_T3_manual_fan_on: '0'
C1_T4_manual_fan_on: '0'
C1_T5_manual_fan_on: '0'
C1_T6_manual_fan_on: '0'
C1_T7_manual_fan_on: '0'
C2_T1_manual_fan_on: '0'
C2_T2_manual_fan_on: '0'
C2_T3_manual_fan_on: '0'
C2_T4_manual_fan_on: '0'
C2_T5_manual_fan_on: '0'
C2_T6_manual_fan_on: '0'
C1_T1_mode_comfort_eco: '0'
C1_T2_mode_comfort_eco: '0'
C1_T3_mode_comfort_eco: '0'
C1_T4_mode_comfort_eco: '0'
C1_T5_mode_comfort_eco: '0'
C1_T6_mode_comfort_eco: '0'
C1_T7_mode_comfort_eco: '0'
C2_T1_mode_comfort_eco: '0'
C2_T2_mode_comfort_eco: '0'
C2_T3_mode_comfort_eco: '0'
C2_T4_mode_comfort_eco: '0'
C2_T5_mode_comfort_eco: '0'
C2_T6_mode_comfort_eco: '0'
C1_T1_dehumidifier_activation: '0'
C1_T2_dehumidifier_activation: '0'
C1_T3_dehumidifier_activation: '0'
C1_T4_dehumidifier_activation: '0'
C1_T5_dehumidifier_activation: '0'
C1_T6_dehumidifier_activation: '0'
C1_T7_dehumidifier_activation: '0'
C2_T1_dehumidifier_activation: '0'
C2_T2_dehumidifier_activation: '0'
C2_T3_dehumidifier_activation: '0'
C2_T4_dehumidifier_activation: '0'
C2_T5_dehumidifier_activation: '0'
C2_T6_dehumidifier_activation: '0'
C1_T1_rh_control: '0'
C1_T2_rh_control: '0'
C1_T3_rh_control: '0'
C1_T4_rh_control: '0'
C1_T5_rh_control: '0'
C1_T6_rh_control: '0'
C1_T7_rh_control: '0'
C2_T1_rh_control: '0'
C2_T2_rh_control: '0'
C2_T3_rh_control: '0'
C2_T4_rh_control: '0'
C2_T5_rh_control: '0'
C2_T6_rh_control: '0'
C1_T1_eco_profile_number: '7'
C1_T2_eco_profile_number: '0'
C1_T3_eco_profile_number: '0'
C1_T4_eco_profile_number: '0'
C1_T5_eco_profile_number: '0'
C1_T6_eco_profile_number: '0'
C1_T7_eco_profile_number: '0'
C2_T1_eco_profile_number: '0'
C2_T2_eco_profile_number: '0'
C2_T3_eco_profile_number: '0'
C2_T4_eco_profile_number: '0'
C2_T5_eco_profile_number: '0'
C2_T6_eco_profile_number: '0'
C1_T1_pub_setpoint_override: '1'
C1_T2_pub_setpoint_override: '1'
C1_T3_pub_setpoint_override: '1'
C1_T4_pub_setpoint_override: '1'
C1_T5_pub_setpoint_override: '1'
C1_T6_pub_setpoint_override: '1'
C1_T7_pub_setpoint_override: '1'
C2_T1_pub_setpoint_override: '1'
C2_T2_pub_setpoint_override: '1'
C2_T3_pub_setpoint_override: '1'
C2_T4_pub_setpoint_override: '1'
C2_T5_pub_setpoint_override: '1'
C2_T6_pub_setpoint_override: '1'
C1_T1_cooling_allowed: '1'
C1_T2_cooling_allowed: '1'
C1_T3_cooling_allowed: '1'
C1_T4_cooling_allowed: '1'
C1_T5_cooling_allowed: '1'
C1_T6_cooling_allowed: '1'
C1_T7_cooling_allowed: '1'
C2_T1_cooling_allowed: '1'
C2_T2_cooling_allowed: '1'
C2_T3_cooling_allowed: '1'
C2_T4_cooling_allowed: '1'
C2_T5_cooling_allowed: '1'
C2_T6_cooling_allowed: '1'
C1_T1_rh_setpoint: '75'
C1_T2_rh_setpoint: '75'
C1_T3_rh_setpoint: '75'
C1_T4_rh_setpoint: '75'
C1_T5_rh_setpoint: '75'
C1_T6_rh_setpoint: '75'
C1_T7_rh_setpoint: '75'
C2_T1_rh_setpoint: '75'
C2_T2_rh_setpoint: '75'
C2_T3_rh_setpoint: '75'
C2_T4_rh_setpoint: '75'
C2_T5_rh_setpoint: '75'
C2_T6_rh_setpoint: '75'
C1_T1_comfort_heating_setpoint: '0'
C1_T2_comfort_heating_setpoint: '0'
C1_T3_comfort_heating_setpoint: '0'
C1_T4_comfort_heating_setpoint: '0'
C1_T5_comfort_heating_setpoint: '0'
C1_T6_comfort_heating_setpoint: '0'
C1_T7_comfort_heating_setpoint: '8'
C2_T1_comfort_heating_setpoint: '0'
C2_T2_comfort_heating_setpoint: '0'
C2_T3_comfort_heating_setpoint: '0'
C2_T4_comfort_heating_setpoint: '0'
C2_T5_comfort_heating_setpoint: '0'
C2_T6_comfort_heating_setpoint: '0'
C1_T1_minimum_setpoint: '410'
C1_T2_minimum_setpoint: '410'
C1_T3_minimum_setpoint: '410'
C1_T4_minimum_setpoint: '410'
C1_T5_minimum_setpoint: '410'
C1_T6_minimum_setpoint: '410'
C1_T7_minimum_setpoint: '410'
C2_T1_minimum_setpoint: '410'
C2_T2_minimum_setpoint: '410'
C2_T3_minimum_setpoint: '410'
C2_T4_minimum_setpoint: '410'
C2_T5_minimum_setpoint: '410'
C2_T6_minimum_setpoint: '410'
C1_T1_maximum_setpoint: '950'
C1_T2_maximum_setpoint: '950'
C1_T3_maximum_setpoint: '950'
C1_T4_maximum_setpoint: '950'
C1_T5_maximum_setpoint: '950'
C1_T6_maximum_setpoint: '950'
C1_T7_maximum_setpoint: '950'
C2_T1_maximum_setpoint: '950'
C2_T2_maximum_setpoint: '950'
C2_T3_maximum_setpoint: '950'
C2_T4_maximum_setpoint: '950'
C2_T5_maximum_setpoint: '950'
C2_T6_maximum_setpoint: '950'
C1_T1_minimum_floor_setpoint: '680'
C1_T2_minimum_floor_setpoint: '680'
C1_T3_minimum_floor_setpoint: '680'
C1_T4_minimum_floor_setpoint: '680'
C1_T5_minimum_floor_setpoint: '680'
C1_T6_minimum_floor_setpoint: '680'
C1_T7_minimum_floor_setpoint: '680'
C2_T1_minimum_floor_setpoint: '680'
C2_T2_minimum_floor_setpoint: '680'
C2_T3_minimum_floor_setpoint: '680'
C2_T4_minimum_floor_setpoint: '680'
C2_T5_minimum_floor_setpoint: '680'
C2_T6_minimum_floor_setpoint: '680'
C1_T1_maximum_floor_setpoint: '788'
C1_T2_maximum_floor_setpoint: '788'
C1_T3_maximum_floor_setpoint: '788'
C1_T4_maximum_floor_setpoint: '788'
C1_T5_maximum_floor_setpoint: '788'
C1_T6_maximum_floor_setpoint: '788'
C1_T7_maximum_floor_setpoint: '788'
C2_T1_maximum_floor_setpoint: '788'
C2_T2_maximum_floor_setpoint: '788'
C2_T3_maximum_floor_setpoint: '788'
C2_T4_maximum_floor_setpoint: '788'
C2_T5_maximum_floor_setpoint: '788'
C2_T6_maximum_floor_setpoint: '788'
C1_T1_setpoint: '692'
C1_T2_setpoint: '696'
C1_T3_setpoint: '683'
C1_T4_setpoint: '687'
C1_T5_setpoint: '698'
C1_T6_setpoint: '687'
C1_T7_setpoint: '644'
C2_T1_setpoint: '667'
C2_T2_setpoint: '638'
C2_T3_setpoint: '644'
C2_T4_setpoint: '719'
C2_T5_setpoint: '698'
C2_T6_setpoint: '698'
C1_T1_eco_offset: '72'
C1_T2_eco_offset: '72'
C1_T3_eco_offset: '72'
C1_T4_eco_offset: '72'
C1_T5_eco_offset: '72'
C1_T6_eco_offset: '72'
C1_T7_eco_offset: '72'
C2_T1_eco_offset: '72'
C2_T2_eco_offset: '72'
C2_T3_eco_offset: '72'
C2_T4_eco_offset: '72'
C2_T5_eco_offset: '72'
C2_T6_eco_offset: '72'
C1_T1_stat_cb_wifi_installed: '1'
C1_T2_stat_cb_wifi_installed: '1'
C1_T3_stat_cb_wifi_installed: '1'
C1_T4_stat_cb_wifi_installed: '1'
C1_T5_stat_cb_wifi_installed: '1'
C1_T6_stat_cb_wifi_installed: '1'
C1_T7_stat_cb_wifi_installed: '1'
C2_T1_stat_cb_wifi_installed: '1'
C2_T2_stat_cb_wifi_installed: '1'
C2_T3_stat_cb_wifi_installed: '1'
C2_T4_stat_cb_wifi_installed: '1'
C2_T5_stat_cb_wifi_installed: '1'
C2_T6_stat_cb_wifi_installed: '1'
C1_T1_stat_cb_need_date_info: '0'
C1_T2_stat_cb_need_date_info: '0'
C1_T3_stat_cb_need_date_info: '0'
C1_T4_stat_cb_need_date_info: '0'
C1_T5_stat_cb_need_date_info: '0'
C1_T6_stat_cb_need_date_info: '0'
C1_T7_stat_cb_need_date_info: '0'
C2_T1_stat_cb_need_date_info: '0'
C2_T2_stat_cb_need_date_info: '0'
C2_T3_stat_cb_need_date_info: '0'
C2_T4_stat_cb_need_date_info: '0'
C2_T5_stat_cb_need_date_info: '0'
C2_T6_stat_cb_need_date_info: '0'
C1_T1_stat_cb_comfort_eco_mode: '0'
C1_T2_stat_cb_comfort_eco_mode: '0'
C1_T3_stat_cb_comfort_eco_mode: '0'
C1_T4_stat_cb_comfort_eco_mode: '0'
C1_T5_stat_cb_comfort_eco_mode: '0'
C1_T6_stat_cb_comfort_eco_mode: '0'
C1_T7_stat_cb_comfort_eco_mode: '0'
C2_T1_stat_cb_comfort_eco_mode: '0'
C2_T2_stat_cb_comfort_eco_mode: '0'
C2_T3_stat_cb_comfort_eco_mode: '0'
C2_T4_stat_cb_comfort_eco_mode: '0'
C2_T5_stat_cb_comfort_eco_mode: '0'
C2_T6_stat_cb_comfort_eco_mode: '0'
C1_T1_stat_cb_eco_forced: '0'
C1_T2_stat_cb_eco_forced: '0'
C1_T3_stat_cb_eco_forced: '0'
C1_T4_stat_cb_eco_forced: '0'
C1_T5_stat_cb_eco_forced: '0'
C1_T6_stat_cb_eco_forced: '0'
C1_T7_stat_cb_eco_forced: '0'
C2_T1_stat_cb_eco_forced: '0'
C2_T2_stat_cb_eco_forced: '0'
C2_T3_stat_cb_eco_forced: '0'
C2_T4_stat_cb_eco_forced: '0'
C2_T5_stat_cb_eco_forced: '0'
C2_T6_stat_cb_eco_forced: '0'
C1_T1_stat_cb_sub_actuator: '0'
C1_T2_stat_cb_sub_actuator: '0'
C1_T3_stat_cb_sub_actuator: '0'
C1_T4_stat_cb_sub_actuator: '0'
C1_T5_stat_cb_sub_actuator: '0'
C1_T6_stat_cb_sub_actuator: '0'
C1_T7_stat_cb_sub_actuator: '0'
C2_T1_stat_cb_sub_actuator: '0'
C2_T2_stat_cb_sub_actuator: '0'
C2_T3_stat_cb_sub_actuator: '0'
C2_T4_stat_cb_sub_actuator: '0'
C2_T5_stat_cb_sub_actuator: '0'
C2_T6_stat_cb_sub_actuator: '0'
C1_T1_stat_cb_actuator: '0'
C1_T2_stat_cb_actuator: '0'
C1_T3_stat_cb_actuator: '0'
C1_T4_stat_cb_actuator: '0'
C1_T5_stat_cb_actuator: '0'
C1_T6_stat_cb_actuator: '0'
C1_T7_stat_cb_actuator: '0'
C2_T1_stat_cb_actuator: '0'
C2_T2_stat_cb_actuator: '0'
C2_T3_stat_cb_actuator: '0'
C2_T4_stat_cb_actuator: '0'
C2_T5_stat_cb_actuator: '0'
C2_T6_stat_cb_actuator: '0'
C1_T1_stat_cb_rh_cool_shutdown: '0'
C1_T2_stat_cb_rh_cool_shutdown: '0'
C1_T3_stat_cb_rh_cool_shutdown: '0'
C1_T4_stat_cb_rh_cool_shutdown: '0'
C1_T5_stat_cb_rh_cool_shutdown: '0'
C1_T6_stat_cb_rh_cool_shutdown: '0'
C1_T7_stat_cb_rh_cool_shutdown: '0'
C2_T1_stat_cb_rh_cool_shutdown: '0'
C2_T2_stat_cb_rh_cool_shutdown: '0'
C2_T3_stat_cb_rh_cool_shutdown: '0'
C2_T4_stat_cb_rh_cool_shutdown: '0'
C2_T5_stat_cb_rh_cool_shutdown: '0'
C2_T6_stat_cb_rh_cool_shutdown: '0'
C1_T1_stat_cb_floor_limit_reach: '0'
C1_T2_stat_cb_floor_limit_reach: '0'
C1_T3_stat_cb_floor_limit_reach: '0'
C1_T4_stat_cb_floor_limit_reach: '0'
C1_T5_stat_cb_floor_limit_reach: '0'
C1_T6_stat_cb_floor_limit_reach: '0'
C1_T7_stat_cb_floor_limit_reach: '0'
C2_T1_stat_cb_floor_limit_reach: '0'
C2_T2_stat_cb_floor_limit_reach: '0'
C2_T3_stat_cb_floor_limit_reach: '0'
C2_T4_stat_cb_floor_limit_reach: '0'
C2_T5_stat_cb_floor_limit_reach: '0'
C2_T6_stat_cb_floor_limit_reach: '0'
C1_T1_stat_cb_fallbk_heatalarm: '0'
C1_T2_stat_cb_fallbk_heatalarm: '0'
C1_T3_stat_cb_fallbk_heatalarm: '0'
C1_T4_stat_cb_fallbk_heatalarm: '0'
C1_T5_stat_cb_fallbk_heatalarm: '0'
C1_T6_stat_cb_fallbk_heatalarm: '0'
C1_T7_stat_cb_fallbk_heatalarm: '0'
C2_T1_stat_cb_fallbk_heatalarm: '0'
C2_T2_stat_cb_fallbk_heatalarm: '0'
C2_T3_stat_cb_fallbk_heatalarm: '0'
C2_T4_stat_cb_fallbk_heatalarm: '0'
C2_T5_stat_cb_fallbk_heatalarm: '0'
C2_T6_stat_cb_fallbk_heatalarm: '0'
C1_T1_stat_cb_holiday_mode: '0'
C1_T2_stat_cb_holiday_mode: '0'
C1_T3_stat_cb_holiday_mode: '0'
C1_T4_stat_cb_holiday_mode: '0'
C1_T5_stat_cb_holiday_mode: '0'
C1_T6_stat_cb_holiday_mode: '0'
C1_T7_stat_cb_holiday_mode: '0'
C2_T1_stat_cb_holiday_mode: '0'
C2_T2_stat_cb_holiday_mode: '0'
C2_T3_stat_cb_holiday_mode: '0'
C2_T4_stat_cb_holiday_mode: '0'
C2_T5_stat_cb_holiday_mode: '0'
C2_T6_stat_cb_holiday_mode: '0'
C1_T1_stat_cb_heat_cool_mode: '0'
C1_T2_stat_cb_heat_cool_mode: '0'
C1_T3_stat_cb_heat_cool_mode: '0'
C1_T4_stat_cb_heat_cool_mode: '0'
C1_T5_stat_cb_heat_cool_mode: '0'
C1_T6_stat_cb_heat_cool_mode: '0'
C1_T7_stat_cb_heat_cool_mode: '0'
C2_T1_stat_cb_heat_cool_mode: '0'
C2_T2_stat_cb_heat_cool_mode: '0'
C2_T3_stat_cb_heat_cool_mode: '0'
C2_T4_stat_cb_heat_cool_mode: '0'
C2_T5_stat_cb_heat_cool_mode: '0'
C2_T6_stat_cb_heat_cool_mode: '0'
C1_T1_stat_air_sensor_error: '0'
C1_T2_stat_air_sensor_error: '0'
C1_T3_stat_air_sensor_error: '0'
C1_T4_stat_air_sensor_error: '0'
C1_T5_stat_air_sensor_error: '0'
C1_T6_stat_air_sensor_error: '0'
C1_T7_stat_air_sensor_error: '0'
C2_T1_stat_air_sensor_error: '0'
C2_T2_stat_air_sensor_error: '0'
C2_T3_stat_air_sensor_error: '0'
C2_T4_stat_air_sensor_error: '0'
C2_T5_stat_air_sensor_error: '0'
C2_T6_stat_air_sensor_error: '0'
C1_T1_stat_external_sensor_err: '0'
C1_T2_stat_external_sensor_err: '0'
C1_T3_stat_external_sensor_err: '0'
C1_T4_stat_external_sensor_err: '0'
C1_T5_stat_external_sensor_err: '0'
C1_T6_stat_external_sensor_err: '0'
C1_T7_stat_external_sensor_err: '0'
C2_T1_stat_external_sensor_err: '0'
C2_T2_stat_external_sensor_err: '0'
C2_T3_stat_external_sensor_err: '0'
C2_T4_stat_external_sensor_err: '0'
C2_T5_stat_external_sensor_err: '0'
C2_T6_stat_external_sensor_err: '0'
C1_T1_stat_rh_sensor_error: '0'
C1_T2_stat_rh_sensor_error: '0'
C1_T3_stat_rh_sensor_error: '0'
C1_T4_stat_rh_sensor_error: '0'
C1_T5_stat_rh_sensor_error: '0'
C1_T6_stat_rh_sensor_error: '0'
C2_T1_stat_rh_sensor_error: '0'
C2_T2_stat_rh_sensor_error: '0'
C2_T3_stat_rh_sensor_error: '0'
C2_T4_stat_rh_sensor_error: '0'
C2_T5_stat_rh_sensor_error: '0'
C2_T6_stat_rh_sensor_error: '0'
C1_T1_stat_comfort_eco_mode: '0'
C1_T2_stat_comfort_eco_mode: '0'
C1_T3_stat_comfort_eco_mode: '0'
C1_T4_stat_comfort_eco_mode: '0'
C1_T5_stat_comfort_eco_mode: '0'
C1_T6_stat_comfort_eco_mode: '0'
C1_T7_stat_comfort_eco_mode: '0'
C2_T1_stat_comfort_eco_mode: '0'
C2_T2_stat_comfort_eco_mode: '0'
C2_T3_stat_comfort_eco_mode: '0'
C2_T4_stat_comfort_eco_mode: '0'
C2_T5_stat_comfort_eco_mode: '0'
C2_T6_stat_comfort_eco_mode: '0'
C1_T1_stat_tamper_alarm: '0'
C1_T2_stat_tamper_alarm: '0'
C1_T3_stat_tamper_alarm: '0'
C1_T4_stat_tamper_alarm: '0'
C1_T5_stat_tamper_alarm: '0'
C1_T6_stat_tamper_alarm: '0'
C1_T7_stat_tamper_alarm: '0'
C2_T1_stat_tamper_alarm: '0'
C2_T2_stat_tamper_alarm: '0'
C2_T3_stat_tamper_alarm: '0'
C2_T4_stat_tamper_alarm: '0'
C2_T5_stat_tamper_alarm: '0'
C2_T6_stat_tamper_alarm: '0'
C1_T1_stat_rf_error: '0'
C1_T2_stat_rf_error: '0'
C1_T3_stat_rf_error: '0'
C1_T4_stat_rf_error: '0'
C1_T5_stat_rf_error: '0'
C1_T6_stat_rf_error: '0'
C1_T7_stat_rf_error: '0'
C2_T1_stat_rf_error: '0'
C2_T2_stat_rf_error: '0'
C2_T3_stat_rf_error: '0'
C2_T4_stat_rf_error: '0'
C2_T5_stat_rf_error: '0'
C2_T6_stat_rf_error: '0'
C1_T1_stat_battery_error: '0'
C1_T2_stat_battery_error: '0'
C1_T3_stat_battery_error: '0'
C1_T4_stat_battery_error: '0'
C1_T5_stat_battery_error: '0'
C1_T6_stat_battery_error: '0'
C1_T7_stat_battery_error: '0'
C2_T1_stat_battery_error: '0'
C2_T2_stat_battery_error: '0'
C2_T3_stat_battery_error: '0'
C2_T4_stat_battery_error: '0'
C2_T5_stat_battery_error: '0'
C2_T6_stat_battery_error: '0'
C1_T1_stat_rf_low_sig_warning: '0'
C1_T2_stat_rf_low_sig_warning: '0'
C1_T3_stat_rf_low_sig_warning: '0'
C1_T4_stat_rf_low_sig_warning: '0'
C1_T5_stat_rf_low_sig_warning: '0'
C1_T6_stat_rf_low_sig_warning: '0'
C1_T7_stat_rf_low_sig_warning: '0'
C2_T1_stat_rf_low_sig_warning: '0'
C2_T2_stat_rf_low_sig_warning: '0'
C2_T3_stat_rf_low_sig_warning: '0'
C2_T4_stat_rf_low_sig_warning: '0'
C2_T5_stat_rf_low_sig_warning: '0'
C2_T6_stat_rf_low_sig_warning: '0'
C1_T1_stat_valve_position_err: '0'
C1_T2_stat_valve_position_err: '0'
C1_T3_stat_valve_position_err: '0'
C1_T4_stat_valve_position_err: '0'
C1_T5_stat_valve_position_err: '0'
C1_T6_stat_valve_position_err: '0'
C1_T7_stat_valve_position_err: '0'
C2_T1_stat_valve_position_err: '0'
C2_T2_stat_valve_position_err: '0'
C2_T3_stat_valve_position_err: '0'
C2_T4_stat_valve_position_err: '0'
C2_T5_stat_valve_position_err: '0'
C2_T6_stat_valve_position_err: '0'
C1_T1_stat_eco_program: '0'
C1_T2_stat_eco_program: '0'
C1_T3_stat_eco_program: '0'
C1_T4_stat_eco_program: '0'
C1_T5_stat_eco_program: '0'
C1_T6_stat_eco_program: '0'
C1_T7_stat_eco_program: '0'
C2_T1_stat_eco_program: '0'
C2_T2_stat_eco_program: '0'
C2_T3_stat_eco_program: '0'
C2_T4_stat_eco_program: '0'
C2_T5_stat_eco_program: '0'
C2_T6_stat_eco_program: '0'
C1_T1_stat_demand_led: '0'
C1_T2_stat_demand_led: '0'
C1_T3_stat_demand_led: '0'
C1_T4_stat_demand_led: '0'
C1_T5_stat_demand_led: '0'
C1_T6_stat_demand_led: '0'
C1_T7_stat_demand_led: '1'
C2_T1_stat_demand_led: '0'
C2_T2_stat_demand_led: '0'
C2_T3_stat_demand_led: '0'
C2_T4_stat_demand_led: '0'
C2_T5_stat_demand_led: '0'
C2_T6_stat_demand_led: '0'
C1_T1_thermostat_type: '0'
C1_T2_thermostat_type: '0'
C1_T3_thermostat_type: '0'
C1_T4_thermostat_type: '0'
C1_T5_thermostat_type: '0'
C1_T6_thermostat_type: '0'
C1_T7_thermostat_type: '0'
C2_T1_thermostat_type: '0'
C2_T2_thermostat_type: '0'
C2_T3_thermostat_type: '0'
C2_T4_thermostat_type: '0'
C2_T5_thermostat_type: '0'
C2_T6_thermostat_type: '0'
C1_T1_eco_setting: '1'
C1_T2_eco_setting: '1'
C1_T3_eco_setting: '1'
C1_T4_eco_setting: '1'
C1_T5_eco_setting: '1'
C1_T6_eco_setting: '1'
C1_T7_eco_setting: '1'
C2_T1_eco_setting: '1'
C2_T2_eco_setting: '1'
C2_T3_eco_setting: '1'
C2_T4_eco_setting: '1'
C2_T5_eco_setting: '1'
C2_T6_eco_setting: '0'
C1_T1_system_device_public: '0'
C1_T2_system_device_public: '0'
C1_T3_system_device_public: '0'
C1_T4_system_device_public: '0'
C1_T5_system_device_public: '0'
C1_T6_system_device_public: '0'
C1_T7_system_device_public: '0'
C2_T1_system_device_public: '0'
C2_T2_system_device_public: '0'
C2_T3_system_device_public: '0'
C2_T4_system_device_public: '0'
C2_T5_system_device_public: '0'
C2_T6_system_device_public: '0'
C1_T1_input_state: '0'
C1_T2_input_state: '0'
C1_T3_input_state: '0'
C1_T4_input_state: '0'
C1_T5_input_state: '0'
C1_T6_input_state: '0'
C1_T7_input_state: '0'
C2_T1_input_state: '0'
C2_T2_input_state: '0'
C2_T3_input_state: '0'
C2_T4_input_state: '0'
C2_T5_input_state: '0'
C2_T6_input_state: '0'
C1_T1_sensor_only: '0'
C1_T2_sensor_only: '0'
C1_T3_sensor_only: '0'
C1_T4_sensor_only: '0'
C1_T5_sensor_only: '0'
C1_T6_sensor_only: '0'
C1_T7_sensor_only: '0'
C2_T1_sensor_only: '0'
C2_T2_sensor_only: '0'
C2_T3_sensor_only: '0'
C2_T4_sensor_only: '0'
C2_T5_sensor_only: '0'
C2_T6_sensor_only: '0'
C1_T1_regulation_mode: '0'
C1_T2_regulation_mode: '0'
C1_T3_regulation_mode: '0'
C1_T4_regulation_mode: '0'
C1_T5_regulation_mode: '0'
C1_T6_regulation_mode: '0'
C1_T7_regulation_mode: '0'
C2_T1_regulation_mode: '0'
C2_T2_regulation_mode: '0'
C2_T3_regulation_mode: '0'
C2_T4_regulation_mode: '0'
C2_T5_regulation_mode: '0'
C2_T6_regulation_mode: '0'
C1_T1_cool_allowed: '1'
C1_T2_cool_allowed: '1'
C1_T3_cool_allowed: '1'
C1_T4_cool_allowed: '1'
C1_T5_cool_allowed: '1'
C1_T6_cool_allowed: '1'
C1_T7_cool_allowed: '1'
C2_T1_cool_allowed: '1'
C2_T2_cool_allowed: '1'
C2_T3_cool_allowed: '1'
C2_T4_cool_allowed: '1'
C2_T5_cool_allowed: '1'
C2_T6_cool_allowed: '1'
C1_T1_manual_cool_allowed: '0'
C1_T2_manual_cool_allowed: '1'
C1_T3_manual_cool_allowed: '0'
C1_T4_manual_cool_allowed: '1'
C1_T5_manual_cool_allowed: '0'
C1_T6_manual_cool_allowed: '0'
C1_T7_manual_cool_allowed: '0'
C2_T1_manual_cool_allowed: '0'
C2_T2_manual_cool_allowed: '0'
C2_T3_manual_cool_allowed: '0'
C2_T4_manual_cool_allowed: '1'
C2_T5_manual_cool_allowed: '0'
C2_T6_manual_cool_allowed: '0'
C1_T1_heat_cool_mode: '0'
C1_T2_heat_cool_mode: '0'
C1_T3_heat_cool_mode: '0'
C1_T4_heat_cool_mode: '0'
C1_T5_heat_cool_mode: '0'
C1_T6_heat_cool_mode: '0'
C1_T7_heat_cool_mode: '0'
C2_T1_heat_cool_mode: '0'
C2_T2_heat_cool_mode: '0'
C2_T3_heat_cool_mode: '0'
C2_T4_heat_cool_mode: '0'
C2_T5_heat_cool_mode: '0'
C2_T6_heat_cool_mode: '0'
C1_T1_heat_cool_slave: '0'
C1_T2_heat_cool_slave: '0'
C1_T3_heat_cool_slave: '0'
C1_T4_heat_cool_slave: '0'
C1_T5_heat_cool_slave: '0'
C1_T6_heat_cool_slave: '0'
C1_T7_heat_cool_slave: '0'
C2_T1_heat_cool_slave: '0'
C2_T2_heat_cool_slave: '0'
C2_T3_heat_cool_slave: '0'
C2_T4_heat_cool_slave: '0'
C2_T5_heat_cool_slave: '0'
C2_T6_heat_cool_slave: '0'
C1_T1_room_temperature: '746'
C1_T2_room_temperature: '756'
C1_T3_room_temperature: '741'
C1_T4_room_temperature: '739'
C1_T5_room_temperature: '742'
C1_T6_room_temperature: '761'
C1_T7_room_temperature: '771'
C2_T1_room_temperature: '750'
C2_T2_room_temperature: '752'
C2_T3_room_temperature: '763'
C2_T4_room_temperature: '773'
C2_T5_room_temperature: '775'
C2_T6_room_temperature: '744'
C1_T1_external_temperature: '32767'
C1_T2_external_temperature: '32767'
C1_T3_external_temperature: '32767'
C1_T4_external_temperature: '32767'
C1_T5_external_temperature: '32767'
C1_T6_external_temperature: '32767'
C1_T7_external_temperature: '32767'
C2_T1_external_temperature: '32767'
C2_T2_external_temperature: '32767'
C2_T3_external_temperature: '32767'
C2_T4_external_temperature: '32767'
C2_T5_external_temperature: '32767'
C2_T6_external_temperature: '32767'
C1_T1_rh: '0'
C1_T2_rh: '0'
C1_T3_rh: '0'
C1_T4_rh: '0'
C1_T5_rh: '0'
C1_T6_rh: '0'
C1_T7_rh: '0'
C2_T1_rh: '0'
C2_T2_rh: '0'
C2_T3_rh: '0'
C2_T4_rh: '0'
C2_T5_rh: '0'
C2_T6_rh: '0'
C1_T1_hw_type: '0'
C1_T2_hw_type: '0'
C1_T3_hw_type: '0'
C1_T4_hw_type: '0'
C1_T5_hw_type: '0'
C1_T6_hw_type: '0'
C1_T7_hw_type: '0'
C2_T1_hw_type: '0'
C2_T2_hw_type: '0'
C2_T3_hw_type: '0'
C2_T4_hw_type: '0'
C2_T5_hw_type: '0'
C2_T6_hw_type: '0'
C1_T1_sw_version: '11'
C1_T2_sw_version: '11'
C1_T3_sw_version: '11'
C1_T4_sw_version: '11'
C1_T5_sw_version: '11'
C1_T6_sw_version: '11'
C1_T7_sw_version: '11'
C2_T1_sw_version: '11'
C2_T2_sw_version: '11'
C2_T3_sw_version: '11'
C2_T4_sw_version: '11'
C2_T5_sw_version: '11'
C2_T6_sw_version: '11'
C1_T1_ufh_pwm_output: '50'
C1_T2_ufh_pwm_output: '50'
C1_T3_ufh_pwm_output: '50'
C1_T4_ufh_pwm_output: '50'
C1_T5_ufh_pwm_output: '50'
C1_T6_ufh_pwm_output: '50'
C1_T7_ufh_pwm_output: '50'
C2_T1_ufh_pwm_output: '50'
C2_T2_ufh_pwm_output: '50'
C2_T3_ufh_pwm_output: '50'
C2_T4_ufh_pwm_output: '50'
C2_T5_ufh_pwm_output: '50'
C2_T6_ufh_pwm_output: '50'
C1_T1_head1_supply_temp: '50'
C1_T2_head1_supply_temp: '50'
C1_T3_head1_supply_temp: '50'
C1_T4_head1_supply_temp: '50'
C1_T5_head1_supply_temp: '50'
C1_T6_head1_supply_temp: '50'
C1_T7_head1_supply_temp: '50'
C2_T1_head1_supply_temp: '50'
C2_T2_head1_supply_temp: '50'
C2_T3_head1_supply_temp: '50'
C2_T4_head1_supply_temp: '50'
C2_T5_head1_supply_temp: '50'
C2_T6_head1_supply_temp: '50'
C1_T1_head1_valve_pos_percent: '0'
C1_T2_head1_valve_pos_percent: '0'
C1_T3_head1_valve_pos_percent: '0'
C1_T4_head1_valve_pos_percent: '0'
C1_T5_head1_valve_pos_percent: '0'
C1_T6_head1_valve_pos_percent: '0'
C1_T7_head1_valve_pos_percent: '0'
C2_T1_head1_valve_pos_percent: '0'
C2_T2_head1_valve_pos_percent: '0'
C2_T3_head1_valve_pos_percent: '0'
C2_T4_head1_valve_pos_percent: '0'
C2_T5_head1_valve_pos_percent: '0'
C2_T6_head1_valve_pos_percent: '0'
C1_T1_head1_valve_pos: '0'
C1_T2_head1_valve_pos: '0'
C1_T3_head1_valve_pos: '0'
C1_T4_head1_valve_pos: '0'
C1_T5_head1_valve_pos: '0'
C1_T6_head1_valve_pos: '0'
C1_T7_head1_valve_pos: '0'
C2_T1_head1_valve_pos: '0'
C2_T2_head1_valve_pos: '0'
C2_T3_head1_valve_pos: '0'
C2_T4_head1_valve_pos: '0'
C2_T5_head1_valve_pos: '0'
C2_T6_head1_valve_pos: '0'
C1_T1_head1_sw_version: '0'
C1_T2_head1_sw_version: '0'
C1_T3_head1_sw_version: '0'
C1_T4_head1_sw_version: '0'
C1_T5_head1_sw_version: '0'
C1_T6_head1_sw_version: '0'
C1_T7_head1_sw_version: '0'
C2_T1_head1_sw_version: '0'
C2_T2_head1_sw_version: '0'
C2_T3_head1_sw_version: '0'
C2_T4_head1_sw_version: '0'
C2_T5_head1_sw_version: '0'
C2_T6_head1_sw_version: '0'
C1_T1_ufh1_actuator_cycle: '0'
C1_T2_ufh1_actuator_cycle: '0'
C1_T3_ufh1_actuator_cycle: '0'
C1_T4_ufh1_actuator_cycle: '0'
C1_T5_ufh1_actuator_cycle: '0'
C1_T6_ufh1_actuator_cycle: '0'
C1_T7_ufh1_actuator_cycle: '0'
C2_T1_ufh1_actuator_cycle: '0'
C2_T2_ufh1_actuator_cycle: '0'
C2_T3_ufh1_actuator_cycle: '0'
C2_T4_ufh1_actuator_cycle: '0'
C2_T5_ufh1_actuator_cycle: '0'
C2_T6_ufh1_actuator_cycle: '0'
C1_T1_head2_valve_pos_percent: '0'
C1_T2_head2_valve_pos_percent: '0'
C1_T3_head2_valve_pos_percent: '0'
C1_T4_head2_valve_pos_percent: '0'
C1_T5_head2_valve_pos_percent: '0'
C1_T6_head2_valve_pos_percent: '0'
C1_T7_head2_valve_pos_percent: '0'
C2_T1_head2_valve_pos_percent: '0'
C2_T2_head2_valve_pos_percent: '0'
C2_T3_head2_valve_pos_percent: '0'
C2_T4_head2_valve_pos_percent: '0'
C2_T5_head2_valve_pos_percent: '0'
C2_T6_head2_valve_pos_percent: '0'
C1_T1_head2_valve_pos: '0'
C1_T2_head2_valve_pos: '0'
C1_T3_head2_valve_pos: '0'
C1_T4_head2_valve_pos: '0'
C1_T5_head2_valve_pos: '0'
C1_T6_head2_valve_pos: '0'
C1_T7_head2_valve_pos: '0'
C2_T1_head2_valve_pos: '0'
C2_T2_head2_valve_pos: '0'
C2_T3_head2_valve_pos: '0'
C2_T4_head2_valve_pos: '0'
C2_T5_head2_valve_pos: '0'
C2_T6_head2_valve_pos: '0'
C1_T1_head2_sw_version: '0'
C1_T2_head2_sw_version: '0'
C1_T3_head2_sw_version: '0'
C1_T4_head2_sw_version: '0'
C1_T5_head2_sw_version: '0'
C1_T6_head2_sw_version: '0'
C1_T7_head2_sw_version: '0'
C2_T1_head2_sw_version: '0'
C2_T2_head2_sw_version: '0'
C2_T3_head2_sw_version: '0'
C2_T4_head2_sw_version: '0'
C2_T5_head2_sw_version: '0'
C2_T6_head2_sw_version: '0'
C1_T1_ufh2_actuator_cycle: '0'
C1_T2_ufh2_actuator_cycle: '0'
C1_T3_ufh2_actuator_cycle: '0'
C1_T4_ufh2_actuator_cycle: '0'
C1_T5_ufh2_actuator_cycle: '0'
C1_T6_ufh2_actuator_cycle: '0'
C1_T7_ufh2_actuator_cycle: '0'
C2_T1_ufh2_actuator_cycle: '0'
C2_T2_ufh2_actuator_cycle: '0'
C2_T3_ufh2_actuator_cycle: '0'
C2_T4_ufh2_actuator_cycle: '0'
C2_T5_ufh2_actuator_cycle: '0'
C2_T6_ufh2_actuator_cycle: '0'
C1_T1_head2_supply_temp: '0'
C1_T2_head2_supply_temp: '0'
C1_T3_head2_supply_temp: '0'
C1_T4_head2_supply_temp: '0'
C1_T5_head2_supply_temp: '0'
C1_T6_head2_supply_temp: '0'
C1_T7_head2_supply_temp: '0'
C2_T1_head2_supply_temp: '0'
C2_T2_head2_supply_temp: '0'
C2_T3_head2_supply_temp: '0'
C2_T4_head2_supply_temp: '0'
C2_T5_head2_supply_temp: '0'
C2_T6_head2_supply_temp: '0'
C1_T1_channel_position: '1'
C1_T2_channel_position: '2'
C1_T3_channel_position: '4'
C1_T4_channel_position: '8'
C1_T5_channel_position: '16'
C1_T6_channel_position: '32'
C1_T7_channel_position: '64'
C2_T1_channel_position: '1'
C2_T2_channel_position: '2'
C2_T3_channel_position: '4'
C2_T4_channel_position: '8'
C2_T5_channel_position: '16'
C2_T6_channel_position: '32'
C1_T1_head_number: '0'
C1_T2_head_number: '0'
C1_T3_head_number: '0'
C1_T4_head_number: '0'
C1_T5_head_number: '0'
C1_T6_head_number: '0'
C1_T7_head_number: '0'
C2_T1_head_number: '0'
C2_T2_head_number: '0'
C2_T3_head_number: '0'
C2_T4_head_number: '0'
C2_T5_head_number: '0'
C2_T6_head_number: '0'
C1_id_output_module: '0'
C2_id_output_module: '0'
C1_id_sys_dev_outdoor: '0'
C2_id_sys_dev_outdoor: '0'
C1_id_sys_dev_hc: '0'
C2_id_sys_dev_hc: '0'
C1_id_sys_dev_eco: '0'
C2_id_sys_dev_eco: '0'
C1_thermostat1_id: '<redigerat>'
C2_thermostat1_id: '<redigerat>'
C1_thermostat2_id: '<redigerat>'
C2_thermostat2_id: '<redigerat>'
C1_thermostat3_id: '<redigerat>'
C2_thermostat3_id: '<redigerat>'
C1_thermostat4_id: '<redigerat>'
C2_thermostat4_id: '<redigerat>'
C1_thermostat5_id: '<redigerat>'
C2_thermostat5_id: '<redigerat>'
C1_thermostat6_id: '<redigerat>'
C2_thermostat6_id: '<redigerat>'
C1_thermostat7_id: '<redigerat>'
C2_thermostat7_id: '0'
C1_thermostat8_id: '0'
C2_thermostat8_id: '0'
C1_thermostat9_id: '0'
C2_thermostat9_id: '0'
C1_thermostat10_id: '0'
C2_thermostat10_id: '0'
C1_thermostat11_id: '0'
C2_thermostat11_id: '0'
C1_thermostat12_id: '0'
C2_thermostat12_id: '0'
C1_TTH_1_id: '0'
C2_TTH_1_id: '0'
C1_TTH_2_id: '0'
C2_TTH_2_id: '0'
C1_TTH_3_id: '0'
C2_TTH_3_id: '0'
C1_TTH_4_id: '0'
C2_TTH_4_id: '0'
C1_TTH_5_id: '0'
C2_TTH_5_id: '0'
C1_TTH_6_id: '0'
C2_TTH_6_id: '0'
C1_TTH_7_id: '0'
C2_TTH_7_id: '0'
C1_TTH_8_id: '0'
C2_TTH_8_id: '0'
C1_TTH_9_id: '0'
C2_TTH_9_id: '0'
C1_TTH_10_id: '0'
C2_TTH_10_id: '0'
C1_TTH_11_id: '0'
C2_TTH_11_id: '0'
C1_TTH_12_id: '0'
C2_TTH_12_id: '0'
C1_TTH_13_id: '0'
C2_TTH_13_id: '0'
C1_TTH_14_id: '0'
C2_TTH_14_id: '0'
C1_TTH_15_id: '0'
C2_TTH_15_id: '0'
C1_TTH_16_id: '0'
C2_TTH_16_id: '0'
C1_TTH_17_id: '0'
C2_TTH_17_id: '0'
C1_TTH_18_id: '0'
C2_TTH_18_id: '0'
C1_TTH_19_id: '0'
C2_TTH_19_id: '0'
C1_TTH_20_id: '0'
C2_TTH_20_id: '0'
C1_TTH_21_id: '0'
C2_TTH_21_id: '0'
C1_TTH_22_id: '0'
C2_TTH_22_id: '0'
C1_TTH_23_id: '0'
C2_TTH_23_id: '0'
C1_TTH_24_id: '0'
C2_TTH_24_id: '0'
controller1_id: '<redigerat>'
controller2_id: '<redigerat>'
controller3_id: '0'
controller4_id: '0'
C1_T1_Monday: c0ffffffffff
C1_T2_Monday: '000000000000'
C1_T3_Monday: '000000000000'
C1_T4_Monday: '000000000000'
C1_T5_Monday: '000000000000'
C1_T6_Monday: '000000000000'
C1_T7_Monday: '000000000000'
C2_T1_Monday: '000000000000'
C2_T2_Monday: '000000000000'
C2_T3_Monday: '000000000000'
C2_T4_Monday: '000000000000'
C2_T5_Monday: '000000000000'
C2_T6_Monday: '000000000000'
C1_T1_Tuesday: ffffffffffff
C1_T2_Tuesday: '000000000000'
C1_T3_Tuesday: '000000000000'
C1_T4_Tuesday: '000000000000'
C1_T5_Tuesday: '000000000000'
C1_T6_Tuesday: '000000000000'
C1_T7_Tuesday: '000000000000'
C2_T1_Tuesday: '000000000000'
C2_T2_Tuesday: '000000000000'
C2_T3_Tuesday: '000000000000'
C2_T4_Tuesday: '000000000000'
C2_T5_Tuesday: '000000000000'
C2_T6_Tuesday: '000000000000'
C1_T1_Wednesday: ffffffffffff
C1_T2_Wednesday: '000000000000'
C1_T3_Wednesday: '000000000000'
C1_T4_Wednesday: '000000000000'
C1_T5_Wednesday: '000000000000'
C1_T6_Wednesday: '000000000000'
C1_T7_Wednesday: '000000000000'
C2_T1_Wednesday: '000000000000'
C2_T2_Wednesday: '000000000000'
C2_T3_Wednesday: '000000000000'
C2_T4_Wednesday: '000000000000'
C2_T5_Wednesday: '000000000000'
C2_T6_Wednesday: '000000000000'
C1_T1_Thursday: ffffffffffff
C1_T2_Thursday: '000000000000'
C1_T3_Thursday: '000000000000'
C1_T4_Thursday: '000000000000'
C1_T5_Thursday: '000000000000'
C1_T6_Thursday: '000000000000'
C1_T7_Thursday: '000000000000'
C2_T1_Thursday: '000000000000'
C2_T2_Thursday: '000000000000'
C2_T3_Thursday: '000000000000'
C2_T4_Thursday: '000000000000'
C2_T5_Thursday: '000000000000'
C2_T6_Thursday: '000000000000'
C1_T1_Friday: ffffffffffff
C1_T2_Friday: '000000000000'
C1_T3_Friday: '000000000000'
C1_T4_Friday: '000000000000'
C1_T5_Friday: '000000000000'
C1_T6_Friday: '000000000000'
C1_T7_Friday: '000000000000'
C2_T1_Friday: '000000000000'
C2_T2_Friday: '000000000000'
C2_T3_Friday: '000000000000'
C2_T4_Friday: '000000000000'
C2_T5_Friday: '000000000000'
C2_T6_Friday: '000000000000'
C1_T1_Saturday: ffffffffffff
C1_T2_Saturday: '000000000000'
C1_T3_Saturday: '000000000000'
C1_T4_Saturday: '000000000000'
C1_T5_Saturday: '000000000000'
C1_T6_Saturday: '000000000000'
C1_T7_Saturday: '000000000000'
C2_T1_Saturday: '000000000000'
C2_T2_Saturday: '000000000000'
C2_T3_Saturday: '000000000000'
C2_T4_Saturday: '000000000000'
C2_T5_Saturday: '000000000000'
C2_T6_Saturday: '000000000000'
C1_T1_Sunday: ffffffffffff
C1_T2_Sunday: '000000000000'
C1_T3_Sunday: '000000000000'
C1_T4_Sunday: '000000000000'
C1_T5_Sunday: '000000000000'
C1_T6_Sunday: '000000000000'
C1_T7_Sunday: '000000000000'
C2_T1_Sunday: '000000000000'
C2_T2_Sunday: '000000000000'
C2_T3_Sunday: '000000000000'
C2_T4_Sunday: '000000000000'
C2_T5_Sunday: '000000000000'
C2_T6_Sunday: '000000000000'
cust_C1_T1_Custom_Eco_Profile: '1'
```

</details>

---

## Supported hardware

The integration supports the **Uponor Smatrix Wave Pulse (X-265)** and **Uponor Smatrix Base Pulse (X-245)** systems. The controller (X-265/X-245) is the gateway the integration speaks JNAP with. The thermostats become `climate` entities in HA; the dial models T-144/T-145 (see [const.py](custom_components/uponorx265/const.py) `DIAL_THERMOSTAT_MODELS`) require "HA controlled" mode for remote setpoint control.

### Uponor Smatrix Wave Pulse (X-265)

| Product | Type |
|---|---|
| Uponor Smatrix A-1XX | Transformer module |
| Uponor Smatrix Wave Pulse X-265 | Controller (gateway) |
| Uponor Smatrix Wave Pulse M-262 | Extension module |
| Uponor Smatrix Wave Pulse A-265 | Antenna |
| Uponor Smatrix Pulse Com R-208 | Communication module |
| Uponor Smatrix Wave T-169 | Digital thermostat, with relative humidity and occupancy sensor |
| Uponor Smatrix Wave T-168 | Programmable digital thermostat, with relative humidity sensor |
| Uponor Smatrix Wave T-166 | Digital thermostat |
| Uponor Smatrix Wave T-165 | Standard thermostat with printed dial scale |
| Uponor Smatrix Wave T-163 | Thermostat for public environments |
| Uponor Smatrix Wave T-162 | Thermostat head |
| Uponor Smatrix Wave T-161 | Room sensor thermostat, with relative humidity and occupancy sensor |
| Uponor Smatrix Wave M-161 | Relay module |

### Uponor Smatrix Base Pulse (X-245)

| Product | Type |
|---|---|
| Uponor Smatrix A-1XX | Transformer module |
| Uponor Smatrix Base Pulse X-245 | Controller (gateway) |
| Uponor Smatrix Base Pulse M-242 | Extension module |
| Uponor Smatrix Base Pulse M-243 | Star module |
| Uponor Smatrix Pulse Com R-208 | Communication module |
| Uponor Smatrix Base T-149 | Digital thermostat, with relative humidity and occupancy sensor |
| Uponor Smatrix Base T-148 | Programmable digital thermostat, with relative humidity sensor |
| Uponor Smatrix Base T-146 | Digital thermostat |
| Uponor Smatrix Base T-145 | Standard thermostat with printed dial scale |
| Uponor Smatrix Base T-144 | Recessed thermostat |
| Uponor Smatrix Base T-143 | Thermostat for public environments |
| Uponor Smatrix Base T-141 | Room sensor thermostat, with relative humidity and occupancy sensor |

### Thermostat model identification

The JNAP gateway doesn't expose the thermostat's model name directly — only a serial number (`C?_thermostatN_id`) and a raw hardware type code (`C?_T?_thermostat_type`). The integration guesses the model from these in [`_detect_thermostat_model()`](custom_components/uponorx265/__init__.py) (`__init__.py`):

- The hardware type code (`hwid`) is the first selection criterion:
  - `hwid == 2` → **T-146** (field-confirmed on `sn` prefix `285`).
  - `hwid == 0` → the T-144/T-145 family, which shares the same `hwid` and needs to be told apart via the serial number.
- For `hwid == 0`, the serial number's first 4 digits (`sn`) are split into a prefix (`prodk`, first 3 digits) and a last digit (`mod`):
  - **Known rule:** prefix `269` → last digit `1` = T-144, last digit `2` = T-145.
  - **Catch-all:** every other `hwid == 0` unit (unknown prefix, or `269` with a different last digit) defaults to **T-145** — the same behavior Uponor's own app seems to have when it can't tell them apart either. Prefix `268` (field-confirmed, `sn "2688"`) is already covered by the catch-all but keeps its own branch as a marker, in case a pattern emerges once more thermostats report in.
- If identification isn't possible at all (e.g. a missing `thermostat_type` variable), it falls back to the last cached model (`get_thermostat_model()`), and ultimately `None` — HA then shows no model for the device, but functionality is unaffected (only the `DIAL_THERMOSTAT_MODELS` gating, see `requires_local_override()`).

This is reverse-engineering without access to Uponor's official serial number scheme — so there's no guarantee the `hwid`/prefix pattern holds for hardware we haven't seen yet. New hardware is logged via the `dump_hardware_info` service (`sn_start`, `hardware_type_raw`, `detected_model`) and can be submitted to refine the rules above.

### Component descriptions (from Uponor's installation manual)

<details>
<summary><strong>Uponor Smatrix Base Pulse X-245</strong> (controller)</summary>

- Integrated Dynamic Energy Management (DEM) features, e.g. autobalancing (enabled by default). Other DEM features (comfort setting, room bypass, supply temperature monitoring) require the Pulse app (communication module) and in some cases Uponor's cloud services.
- Electronic actuator control, up to eight actuators (24 V AC).
- Two-way communication with up to six room thermostats.
- Heat/cool switching (advanced) and/or Comfort/ECO via closing contact, public-environment thermostat, or the Pulse app.
- Separate relays for pump and boiler control; other control functions require a communication module + app.
- Valve and pump exercise. Relative humidity control (requires the Pulse app).
- Control of combined floor heating/cooling and ceiling cooling (requires a communication module + app).
- ECO mode lowers indoor temp (heating) / raises it (cooling); activated globally via closing contact, public-environment thermostat, or the app, or per room via a programmable thermostat/ECO profiles.
- Options: communication module for app connectivity (remote access requires Uponor's cloud services); extension module (+6 thermostat channels, +6 actuator outputs); star module (+8 bus connections); up to four controllers in one system (requires a communication module + app); modular placement with a detachable transformer; cabinet/wall mounting (DIN rail or screws); free placement/orientation (the communication module must, however, be mounted vertically).

</details>

<details>
<summary><strong>Uponor Smatrix Pulse Com R-208</strong> (communication module)</summary>

- Provides Uponor Smatrix Pulse app connectivity via Wi-Fi or Ethernet — this is the module that exposes the JNAP gateway the integration talks to.
- Extra features via the app: heat/cool settings, additional relay functions (cooling unit, dehumidifier, etc.).
- Can integrate up to four controllers in one system.
- Cabinet or wall mounting (DIN rail or included screws).

</details>

<details>
<summary><strong>Uponor Smatrix Base M-242</strong> (extension module)</summary>

- Only one extension module per controller.
- Plug-in installation into an existing controller, no extra wiring needed.
- Registers up to six extra thermostats and connects up to six extra actuators (24 V).
- Electronic control, valve exercise.

</details>

<details>
<summary><strong>Uponor Smatrix Base M-243</strong> (star module)</summary>

- Only one star module per bus type (thermostat and/or system bus) per controller; a star module only handles one bus type at a time.
- Enables star-topology wiring instead of a bus network — more flexible cable routing.
- Requires a Base Pulse controller. Adds 8 extra bus connections. Only inputs from thermostats are allowed.
- Connects directly to the controller or extension module with a communication cable.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-141</strong> (room sensor thermostat)</summary>

- As small as possible while still controlling room temperature.
- Occupancy temperature sensor for improved comfort.
- Setpoint adjustable via the app (requires a communication module), 5–35 °C.
- Relative humidity threshold shown in the app (requires a communication module).

</details>

<details>
<summary><strong>Uponor Smatrix Base T-143</strong> (thermostat for public environments)</summary>

- Dial hidden — must be removed from the wall to set the temperature; triggers a tamper alarm when removed (if enabled, also shown in the app with a communication module).
- Can be registered as a system device — this disables the internal room sensor and unlocks extra functions.
- Setpoint 5–35 °C via a potentiometer on the back.
- Closing contact input for forced ECO mode (as a system device).
- Optional extra outdoor temperature sensor; floor temperature limits only configurable via the app.
- DIP switch for function/sensor mode and for enabling the Comfort/ECO schedule.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-144</strong> (recessed thermostat)</summary>

- Specifically designed for wall mounting (recessed installation), large dial with printed scale, 21 °C marked.
- Max/min temperature only settable via the app. Setpoint 5–35 °C.
- LED indication (~60 s) on heating/cooling demand.
- DIP switch under the dial for Comfort/ECO scheduling.
- Different mounting frames available for switch-plate frames.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-145</strong> (standard thermostat)</summary>

- Large dial with printed scale, 21 °C marked, LED ring indicates setpoint change while turning.
- Max/min temperature only settable via the app. Setpoint 5–35 °C.
- LED in the lower right corner indicates (~60 s) heating/cooling demand.
- DIP switch on the back for Comfort/ECO scheduling.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-146</strong> (digital thermostat with display)</summary>

- Backlit display (turns off after 10 s of inactivity), shows °C/°F, calibratable room temperature, shows heating/cooling demand and software version on startup.
- Setpoint 5–35 °C. Support for external temperature sensors (optional).
- Comfort/ECO scheduling requires the Pulse app. Adjustable ECO temperature setback.

</details>

<details>
<summary><strong>Uponor Smatrix Base T-148</strong> (programmable digital thermostat)</summary>

- Display shows room temperature, setpoint, or relative humidity, plus the current time.
- Recommended only in systems **without** a communication module — its own scheduling function is disabled if a communication module is present in the system.
- Installation wizard for time/date, 12/24h clock, internal memory to survive power outages.
- Setpoint 5–35 °C, support for external temperature sensors.
- Programmable Comfort/ECO switching with its own ECO value; T-148 cannot be overridden by other system settings once programmed.
- Humidity threshold alarm on the display (requires a communication module).

</details>

<details>
<summary><strong>Uponor Smatrix Base T-149</strong> (e-paper thermostat)</summary>

- Low-power e-paper display, updates every 10 minutes. Shows °C/°F, room temperature, setpoint, or relative humidity.
- Adjustment via +/- buttons on the side. Occupancy temperature sensor, calibratable room temperature.
- Shows the Uponor logo and software version on startup. Setpoint 5–35 °C, support for external temperature sensors.
- Comfort/ECO scheduling requires the Pulse app. Adjustable ECO temperature setback.
- Humidity threshold alarm on the display (requires a communication module). Can invert display colors.

</details>
