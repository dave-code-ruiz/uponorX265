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
   the controller *Status* and *Average room temperature* sensors are created. The controller
   devices themselves are always created.
3. **Sensors** — choose which optional sensors and features to create:
   - **Current temperature sensor** (on by default)
   - **Valve binary sensor** (off by default)
   - **Average inclusion switch** (off by default) — per-thermostat toggle for controller average temperature
   - **Controller relay/IO sensors** (off by default) — pump relay and boiler request per controller
   - **Installer mode** (off by default) — see below
4. **Rooms** — optionally rename each detected thermostat/room.

The IP address and the step 3 options can be changed later via
**Settings → Devices & Services → UponorX265 → Configure**. To rename a controller or room
afterwards, rename its device in Home Assistant.

## If the gateway changes IP address

The gateway is configured by IP address, so a DHCP lease change would normally leave the
integration talking to nothing. It recovers on its own: the gateway reports its own MAC
address, which is recorded on the gateway device, and Home Assistant's DHCP discovery
matches a new lease against it and updates the stored address.

Recovery happens when the gateway next renews its lease, so it is not instant, and it
requires Home Assistant to be able to observe DHCP traffic (reliable on HAOS and
Supervised installs; container installs on a bridge network may not see it). Assigning the
gateway a static DHCP reservation on your router avoids the situation entirely and is
still worth doing.

## Multiple gateways

Multiple R-208 gateways can be added as separate integration instances. Each instance is
fully independent with its own devices, entities, and cached data.

## Installer mode

Enabling **Installer mode** in setup unlocks writable entities for advanced system configuration.
When disabled, these settings are visible as read-only sensors instead.

| Setting | Installer mode off | Installer mode on |
|---|---|---|
| Controller relays | Read-only sensor | Writable select |
| Pump control | Read-only sensor (C1 only) | Writable select (C1 only) |
| Bypass (per room) | Read-only binary sensor | Writable switch (max 2 active per controller) |
| Floor temperature limits (per room) | Read-only sensors | Writable numbers |

## Entities

### Climate (`climate.ROOM_NAME`)

One climate entity per thermostat.

| Feature | Description |
|---|---|
| Current temperature | Room temperature from the thermostat sensor |
| Target temperature | Settable from HA — on T-144/T-145 dial thermostats only in the **HA controlled** preset |
| HVAC mode | Heat or Cool (following the system-wide mode), or Off |
| Presets | See below |

**Presets:**

| Preset | Description |
|---|---|
| Comfort | Normal operation |
| ECO | The room is in ECO — from its scheduled ECO profile, or Temporary ECO in the mobile app |
| Away | System-wide forced ECO is on (the Away switch) |
| HA controlled | T-144/T-145 dial thermostats only — HA sets the target temperature instead of the dial |

Choosing a preset from Home Assistant acts on the whole system, not just that room: **ECO**
and **Away** both turn on forced ECO for every thermostat (the same as the Away switch), and
**Comfort** turns it off for every thermostat.

**Dial thermostats (T-144/T-145):** the physical dial is in control unless the preset is
**HA controlled**. Setting a target temperature from HA in any other preset is rejected with an
error explaining why, and the displayed temperature is refreshed from the controller.
Switching away from **HA controlled** immediately re-polls the controller so HA shows the
temperature the dial is set to. Other thermostat models accept a target temperature from HA
in every preset.

**Turn off:** since the Uponor API has no true off command, turning off a climate entity sets
the setpoint to the minimum (heating mode) or maximum (cooling mode) configured limit.
Turning it back on restores the previous setpoint (20 °C if none is known). A target
temperature set while the room is off is remembered for when it is turned back on, without
turning it on.

### Switches

| Entity | Device | Description |
|---|---|---|
| Away | Gateway | Activates away/ECO mode for all thermostats |
| Automatic firmware update | Gateway | Enables automatic firmware updates on the R-208 module |
| Cooling mode | Gateway | Switches the entire system between heating and cooling mode (only shown if cooling is available) |
| HA controlled (per room) | Thermostat | T-144/T-145 dial thermostats only — toggles HA temperature control (mirrors the HA controlled preset) |
| Included in average (per room) | Thermostat | Toggles whether the thermostat contributes to the controller's average room temperature (enabled in setup, default: off) |
| Bypass (per room) | Controller | Enables bypass for a room — max 2 active per controller (installer mode only) |

### Selects

| Entity | Device | Description |
|---|---|---|
| Controller relays | Controller | Relay configuration — Not in use / Pump+Heater / Pump+Eco+Comfort / Not configured (installer mode only) |
| Pump control | Controller (C1) | Individual or common pump control (installer mode only) |

### Numbers

| Entity | Device | Description |
|---|---|---|
| Floor temperature min limit | Thermostat | Lowest floor temperature the room is held at — 10–30 °C, never above the max limit (installer mode only, thermostat has an external floor probe) |
| Floor temperature max limit | Thermostat | Highest floor temperature the room may reach — 20–35 °C, never below the min limit (installer mode only, thermostat has an external floor probe) |

### Sensors

| Entity | Device | Created when |
|---|---|---|
| Gateway status | Gateway | Always — Online, Offline (Home Assistant cannot reach the R-208), or Communication failure with main controller (the R-208 answers but has lost the controller). Either one that lasts 10 minutes also raises a repair under Settings → Repairs |
| Status (controller) | Controller | Controller entities enabled in setup. Whether or not it is, a controller the R-208 has had no contact with for 10 minutes raises a repair |
| Average room temperature | Controller | Controller entities enabled in setup |
| Controller relays | Controller | Installer mode off |
| Pump control | Controller (C1) | Installer mode off |
| Status (thermostat) | Thermostat | Always — shows alarm/error codes for each thermostat. *No contact with thermostat* means the controller has stopped hearing from it (it reports this after about an hour on Wave systems); the room's readings are then unavailable and a repair appears under Settings → Repairs |
| Room temperature | Thermostat | Temperature sensor enabled in setup (default: on) |
| Floor temperature | Thermostat | Thermostat has an external floor probe |
| Floor temperature min limit / max limit | Thermostat | Installer mode off, and thermostat has an external floor probe |
| Humidity | Thermostat | Thermostat has a humidity sensor |

### Binary sensors

| Entity | Device | Created when |
|---|---|---|
| Valve | Thermostat | Valve sensor enabled in setup (default: off) — shows whether the actuator is open |
| Battery | Thermostat | Always, on Wave Pulse (wireless) thermostats — *Low* when the thermostat reports a low battery. Works with Home Assistant's *Battery became low* trigger, so one automation can watch every room |
| Pump relay | Controller | Controller relay/IO sensors enabled in setup (default: off). Hidden for C2+ when pump control is set to Common |
| Boiler request | Controller | Controller relay/IO sensors enabled in setup (default: off) — shows Heat / Normal |
| Bypass (per room) | Controller | Installer mode off — read-only view of bypass state per room |

### Translations

Entity names and sensor states are translated using Home Assistant's translation system.
The language used is the **HA system language** (Settings → System → General → Language),
not the individual user's profile language. Swedish (`sv`) and English (`en`) are supported;
English is the fallback for all other languages. A partial Spanish (`es`) translation covers
only a few setup and configuration screens; everything else appears in English.

## Services

### `uponorx265.set_variable`

Sends a raw variable update to the Uponor API. Use with caution.

| Field | Required | Description |
|---|---|---|
| `var_name` | Yes | Variable name, e.g. `sys_heat_cool_mode` |
| `var_value` | Yes | Value to set |
| `device_id` | No | Any device of the target gateway (the gateway, a controller or a thermostat). Required if more than one gateway is configured. |

### `uponorx265.dump_hardware_info`

Returns raw hardware IDs and capability flags for every thermostat and controller
as a service response, shown directly in **Developer Tools → Actions**.
Useful for identifying unrecognised device models and reporting them as issues.

Example output (a Wave Pulse system):
```yaml
gateways:
  - gateway_id: AABBCCDDEEFF
    gateway_model: R-208
    gateway_serial: 000000XX000000
    gateway_hw_version: "1"
    gateway_sw_version: "20006006"
    gateway_firmware_version: Sky_smatrixrelease_2_0_6_6_locked
    controllers:
      - controller: C1
        sn_start: "4195"
        hardware_type_raw: "1"
        detected_model: X-265
        sw_version: "1.21"
        relays_config: "3"
    thermostats:
      - thermostat: C1_T1
        sn_start: "2858"
        hardware_type_raw: "2"
        hw_type_raw: "7"
        sw_version_raw: "12"
        sw_version: C
        detected_model: T-169
        has_humidity_control: 0
        has_humidity_sensor: true
        has_floor_temperature: false
        is_public_device: 0
        is_sensor_only: 0
```

### `uponorx265.dump_raw_data`

Returns the complete raw data dictionary received from the gateway as a service response,
shown directly in **Developer Tools → Actions**.
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
