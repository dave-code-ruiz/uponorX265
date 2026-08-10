# homeassistant-uponor

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Uponor Smatrix Pulse X-265 or X-245 with R-208 heating/cooling integration for Home Assistant.

Forked and extended from [dave-code-ruiz/uponorX265](https://github.com/dave-code-ruiz/uponorX265), which was forked from the original (now unmaintained) [asev/homeassistant-uponor](https://github.com/asev/homeassistant-uponor).

## Supported devices

This integration communicates with the **Uponor Smatrix Pulse R-208** communication module.
It has been tested with the X-265 and X-245 controllers. Up to 4 controllers with 12 thermostats each are supported.

## Installation

1. Configure your system in the Uponor Smatrix mobile app and verify that temperature control works.
   Make sure the R-208 module is connected to your local network and note its IP address.

2. Install via HACS as a custom repository, or copy the `custom_components/uponorx265` folder
   to your Home Assistant `/config/custom_components/` folder.

3. Restart Home Assistant.

4. Go to **Settings → Devices & Services → Add Integration → UponorX265** and complete the setup wizard.

## Model data

If the model of your thermostat/controller does not match or is missing, please run `uponorx265.dump_hardware_info` and upload the output in a support ticket.
Don't forget to include information about which thermostat/controller model you have.

## Setup wizard

The setup wizard has four steps:

1. **Connection** — enter the IP address and a name for this gateway.
2. **Controllers** — optionally rename each detected controller. A checkbox lets you choose whether
   controller devices and sensors should be created in HA.
3. **Sensors** — choose which optional sensors and features to create:
   - **Current temperature sensor** (on by default)
   - **Valve binary sensor** (off by default)
   - **Average inclusion switch** (off by default) — per-thermostat toggle for controller average temperature
   - **Controller relay/IO sensors** (off by default) — pump relay status per controller
   - **Installer mode** (off by default) — see below
4. **Rooms** — optionally rename each detected thermostat/room.

All settings can be changed later via **Settings → Devices & Services → UponorX265 → Configure**.

## Multiple gateways

Multiple R-208 gateways can be added as separate integration instances. Each instance is
fully independent with its own devices, entities, and cached data.

## Installer mode

Enabling **Installer mode** in setup unlocks writable entities for advanced system configuration.
When disabled, these settings are visible as read-only sensors instead.

| Setting | Installer mode off | Installer mode on |
|---|---|---|
| Controller relays | Read-only sensor | Writable select |
| Pump control | Read-only sensor | Writable select |
| Bypass (per room) | Read-only binary sensor | Writable switch (max 2 active per controller) |

## Entities

### Climate (`climate.ROOM_NAME`)

One climate entity per thermostat.

| Feature | Description |
|---|---|
| Current temperature | Room temperature from the thermostat sensor |
| Target temperature | Read-only when preset is not **HA controlled** (set by the physical dial) |
| HVAC mode | Heat / Cool / Off |
| Presets | See below |

**Presets:**

| Preset | Description |
|---|---|
| Comfort | Normal operation — temperature controlled by the physical thermostat dial |
| ECO | Activated by the thermostat's scheduled ECO profile or Temporary ECO in the mobile app |
| Away | Activated by the Away switch (forces ECO mode on all thermostats) |
| HA controlled | Unlocks target temperature control from Home Assistant |

When the preset is anything other than **HA controlled**, the target temperature is read-only.
Attempting to change it shows a notification explaining that the dial is in control and
the displayed temperature is immediately refreshed from the controller.

Switching away from **HA controlled** immediately re-polls the controller so HA shows the
temperature the physical dial is set to.

**Turn off:** since the Uponor API has no true off command, turning off a climate entity sets
the setpoint to the minimum (heating mode) or maximum (cooling mode) configured limit.

### Switches

| Entity | Device | Description |
|---|---|---|
| Away | Gateway | Activates away/ECO mode for all thermostats |
| Automatic firmware update | Gateway | Enables automatic firmware updates on the R-208 module |
| Cooling mode | Gateway | Switches the entire system between heating and cooling mode (only shown if cooling is available) |
| HA controlled (per room) | Thermostat | Per-thermostat toggle for HA temperature control (mirrors the HA controlled preset) |
| Included in average (per room) | Thermostat | Toggles whether the thermostat contributes to the controller's average room temperature (enabled in setup, default: off) |
| Bypass (per room) | Controller | Enables bypass for a room — max 2 active per controller (installer mode only) |

### Selects

| Entity | Device | Description |
|---|---|---|
| Controller relays | Controller | Relay configuration — Not in use / Pump+Heater / Pump+Eco+Comfort / Not configured (installer mode only) |
| Pump control | Controller (C1) | Individual or common pump control (installer mode only) |

### Sensors

| Entity | Device | Created when |
|---|---|---|
| Gateway status | Gateway | Always — shows Online/Offline for the R-208 module |
| Status (controller) | Controller | Controller entities enabled in setup |
| Average room temperature | Controller | Controller entities enabled in setup |
| Controller relays | Controller | Installer mode off |
| Pump control | Controller (C1) | Installer mode off |
| Status (thermostat) | Thermostat | Always — shows alarm/error codes for each thermostat |
| Room temperature | Thermostat | Temperature sensor enabled in setup (default: on) |
| Floor temperature | Thermostat | Thermostat has an external floor probe |
| Humidity | Thermostat | Thermostat has a humidity sensor |

### Binary sensors

| Entity | Device | Created when |
|---|---|---|
| Valve | Thermostat | Valve sensor enabled in setup (default: off) — shows whether the actuator is open |
| Pump relay | Controller | Controller relay/IO sensors enabled in setup (default: off) |
| Bypass (per room) | Controller | Installer mode off |

### Translations

Entity names and sensor states are translated using Home Assistant's translation system.
The language used is the **HA system language** (Settings → System → General → Language),
not the individual user's profile language. Swedish (`sv`) and English (`en`) are supported;
English is the fallback for all other languages.

## Services

### `uponorx265.set_variable`

Sends a raw variable update to the Uponor API. Use with caution.

| Field | Required | Description |
|---|---|---|
| `var_name` | Yes | Variable name, e.g. `sys_heat_cool_mode` |
| `var_value` | Yes | Value to set |
| `device_id` | No | Target gateway device. Required if more than one gateway is configured. |

### `uponorx265.dump_hardware_info`

Returns raw hardware IDs and capability flags for every thermostat and controller
as a service response, shown directly in **Developer Tools → Services**.
Useful for identifying unrecognised device models and reporting them as issues.

Example output:
```yaml
gateways:
  - gateway_id: "101683"
    gateway_model: R-208
    controllers:
      - controller: C1
        sn_start: "4195"
        hardware_type_raw: "0"
        detected_model: X-245
        sw_version: "1.22"
        relays_config: "3"
    thermostats:
      - thermostat: C1_T1
        sn_start: "2692"
        hardware_type_raw: "0"
        detected_model: T-145
        has_humidity_control: 0
        has_humidity_sensor: false
        has_floor_temperature: false
        is_public_device: 0
        is_sensor_only: 0
```

### `uponorx265.dump_raw_data`

Returns the complete raw data dictionary received from the gateway as a service response,
shown directly in **Developer Tools → Services**.
Useful for inspecting all available variables and their current values — for example when
trying to map an unknown variable or verify that a write has taken effect.

If more than one gateway is configured the response is grouped by gateway unique ID.

## Limitations

- Heat/cool mode switching applies to the entire system, not individual thermostats.
- The Uponor API does not expose an off command — see climate entity turn off behaviour above.

## Enable debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.uponorx265: debug
```

## Older module

For the older Uponor X-165 module, see: https://github.com/dave-code-ruiz/uhomeuponor

## Feedback

Your feedback, pull requests or any other contribution are welcome.
