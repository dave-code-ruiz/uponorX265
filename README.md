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

## Setup wizard

The setup wizard has four steps:

1. **Connection** — enter the IP address and a name for this gateway.
2. **Controllers** — optionally rename each detected controller. A checkbox lets you choose whether
   controller devices and sensors should be created in HA.
3. **Sensors** — choose which optional sensors to create per thermostat:
   - **Current temperature sensor** (on by default)
   - **Valve binary sensor** (off by default)
4. **Rooms** — optionally rename each detected thermostat/room.

All settings can be changed later via **Settings → Devices & Services → UponorX265 → Configure**.

## Multiple gateways

Multiple R-208 gateways can be added as separate integration instances. Each instance is
fully independent with its own devices, entities, and cached data.

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

| Entity | Description |
|---|---|
| Away | Activates away/ECO mode for all thermostats |
| Cooling mode | Switches the entire system between heating and cooling mode (only shown if cooling is available) |
| HA controlled (per room) | Per-thermostat toggle for HA temperature control (mirrors the HA controlled preset) |

### Sensors

| Entity | Created when |
|---|---|
| Gateway status | Always — shows Online/Offline for the R-208 module |
| Status (controller) | Controller entities enabled in setup |
| Average room temperature | Controller entities enabled in setup |
| Status (thermostat) | Always — shows alarm/error codes for each thermostat |
| Room temperature | Temperature sensor enabled in setup (default: on) |
| Floor temperature | Thermostat has an external floor probe |
| Humidity | Thermostat has a humidity sensor |

### Binary sensors

| Entity | Created when |
|---|---|
| Valve | Valve sensor enabled in setup (default: off) — shows whether the actuator is open |

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
```json
{
  "gateway_id": "aabbccddeeff",
  "controllers": [
    {
      "controller": "C1",
      "name": "Floor 1",
      "controller_id": "4195...",
      "hardware_type_raw": "11",
      "detected_model": "X-245"
    }
  ],
  "thermostats": [
    {
      "thermostat": "C1_T1",
      "name": "Living room",
      "thermostat_id": "2691...",
      "hardware_type_raw": "7",
      "detected_model": "T-144",
      "has_humidity_control": false,
      "has_humidity_sensor": false,
      "has_floor_temperature": true,
      "is_public_device": false,
      "is_sensor_only": false
    }
  ]
}
```

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
