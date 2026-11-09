"""A controller losing contact with one of its thermostats, and saying so.

The third link, after Home Assistant -> R-208 and R-208 -> controller. A T-169
knocked off the wall hung with its e-ink display still showing the last
reading. The controller kept relaying that reading - 23.2 C and 48 % for four
days - and heated the room on its fallback cycle. The status sensor did say
`rf_sensor_error`, but nothing else did: the room temperature graph stayed a
believable flat line, nothing was logged and nothing notified.

Reproduced by pulling the battery while polling the gateway: after about an
hour `_stat_rf_error`, `_stat_cb_fallbk_heatalarm` and `_stat_cb_actuator` all
went to 1 in the same poll, with room temperature, humidity and PWM frozen.
The data below is that poll.
"""

import logging

from homeassistant.helpers import issue_registry as ir

from custom_components.uponorx265.climate import UponorClimate
from custom_components.uponorx265.const import (
    DOMAIN,
    STATUS_ERROR_BATTERY,
    STATUS_ERROR_CONTROLER,
    STATUS_ERROR_RF_SENSOR,
    STATUS_OK,
    STATUS_WARNING_RF_LOW_SIGNAL,
)
from custom_components.uponorx265.sensor import (
    UponorFloorTemperatureSensor,
    UponorHumiditySensor,
    UponorRoomCurrentTemperatureSensor,
    UponorThermostatStatusSensor,
)
from tests.helpers import make_state_proxy, thermostat_data

import homeassistant.util.dt as dt_util

LOGGER = "custom_components.uponorx265"
ROOM = "C2_T2"
OTHER = "C2_T1"

HEALTHY = {
    "cust_SW_version_update": "X265_121.hex",  # Wave Pulse: wireless thermostats
    "sys_controller_1_presence": "1",
    "sys_controller_2_presence": "1",
    "C2_thermostat_1_presence": "1",
    "C2_thermostat_2_presence": "1",
    "controller1_id": "419469805",
    "controller2_id": "419469819",
    "C2_thermostat1_id": "285840928",
    "C2_thermostat2_id": "285840900",
    "cust_C2_T2_name": "Hall",
    "cust_controller_1_lost": "0",
    "sys_controller_1_lost": "0",
    "sys_controller_2_lost": "0",
    **thermostat_data(ROOM, setpoint_c=22.0),
    f"{ROOM}_room_temperature": "738",  # 23.2 C
    f"{ROOM}_rh": "48",
    f"{ROOM}_external_temperature": "700",  # floor probe fitted
    f"{ROOM}_ufh_pwm_output": "15",
    f"{ROOM}_stat_rf_error": "0",
    f"{ROOM}_stat_cb_fallbk_heatalarm": "0",
    f"{ROOM}_stat_cb_actuator": "0",
    f"{OTHER}_room_temperature": "716",
    f"{OTHER}_rh": "40",
}

# The poll in which the controller gave up on the thermostat.
CONTACT_LOST = {
    **HEALTHY,
    f"{ROOM}_stat_rf_error": "1",
    f"{ROOM}_stat_cb_fallbk_heatalarm": "1",
    f"{ROOM}_stat_cb_actuator": "1",
}


def _polled(hass, data):
    proxy = make_state_proxy(hass, data=dict(data))
    proxy._last_successful_update = dt_util.now()
    return proxy


def _messages(caplog, level, fragment):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == level and fragment in record.getMessage()
    ]


def _issue(hass, proxy, thermostat=ROOM):
    return ir.async_get(hass).async_get_issue(DOMAIN, proxy._contact_issue_id(thermostat))


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

async def test_repeated_readings_are_not_reported_as_current(hass):
    proxy = _polled(hass, CONTACT_LOST)

    assert not proxy.has_live_readings(ROOM)
    for sensor_class in (
        UponorRoomCurrentTemperatureSensor,
        UponorHumiditySensor,
        UponorFloorTemperatureSensor,
    ):
        sensor = sensor_class("uponorx265_test", proxy, ROOM)
        assert not sensor.available, (
            f"{sensor_class.__name__} kept showing the last value it received as a reading"
        )


async def test_status_sensor_stays_up_to_say_why(hass):
    proxy = _polled(hass, CONTACT_LOST)
    status = UponorThermostatStatusSensor("uponorx265_test", proxy, ROOM)

    assert status.available
    assert status.native_value == STATUS_ERROR_RF_SENSOR


async def test_climate_stays_available_without_a_current_reading(hass):
    """The controller still holds the setpoint and still drives the room."""
    proxy = _polled(hass, CONTACT_LOST)
    climate = UponorClimate("uponorx265_test", proxy, ROOM)
    climate.hass = hass

    assert climate.available
    assert climate.native_current_temperature is None, "23.2 C repeated for four days"
    assert climate.current_humidity is None
    assert climate.native_target_temperature == 22.0
    attributes = climate.extra_state_attributes
    assert attributes["fallback_mode"] is True
    assert attributes["pulse_width_modulation"] is None, "frozen at 15 with the actuator on"


async def test_healthy_thermostat_reports_normally(hass):
    proxy = _polled(hass, HEALTHY)
    climate = UponorClimate("uponorx265_test", proxy, ROOM)
    climate.hass = hass

    assert proxy.has_live_readings(ROOM)
    assert UponorRoomCurrentTemperatureSensor("uponorx265_test", proxy, ROOM).available
    assert climate.native_current_temperature == 23.2
    assert climate.current_humidity == 48
    assert climate.extra_state_attributes["fallback_mode"] is False
    assert climate.extra_state_attributes["pulse_width_modulation"] == 15
    assert proxy.get_status(ROOM) == STATUS_OK


async def test_other_thermostats_on_the_controller_are_unaffected(hass):
    proxy = _polled(hass, CONTACT_LOST)

    assert proxy.has_live_readings(OTHER)
    assert proxy.get_status(OTHER) == STATUS_OK


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

async def test_lost_contact_outranks_the_thermostats_stale_flags(hass):
    """Its other flags are as old as its readings; this is what explains them."""
    proxy = _polled(hass, {**CONTACT_LOST, f"{ROOM}_stat_battery_error": "1"})

    assert proxy.get_status(ROOM) == STATUS_ERROR_RF_SENSOR


async def test_controller_loss_still_comes_first(hass):
    proxy = _polled(hass, {**CONTACT_LOST, "sys_controller_2_lost": "1"})

    assert proxy.get_status(ROOM) == STATUS_ERROR_CONTROLER


async def test_weak_signal_is_a_warning_below_real_errors(hass):
    weak = {**HEALTHY, f"{ROOM}_stat_rf_low_sig_warning": "1"}

    assert _polled(hass, weak).get_status(ROOM) == STATUS_WARNING_RF_LOW_SIGNAL
    assert _polled(hass, weak).has_live_readings(ROOM), "still being heard"
    assert _polled(
        hass, {**weak, f"{ROOM}_stat_battery_error": "1"}
    ).get_status(ROOM) == STATUS_ERROR_BATTERY


# ---------------------------------------------------------------------------
# Log and repair: once each way
# ---------------------------------------------------------------------------

async def test_loss_and_recovery_are_logged_once_and_raise_a_repair(hass, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = [
        dict(HEALTHY), dict(CONTACT_LOST), dict(CONTACT_LOST), dict(CONTACT_LOST),
    ]

    await proxy.async_update()
    assert _issue(hass, proxy) is None

    for _ in range(3):
        await proxy.async_update()

    lost = _messages(caplog, logging.WARNING, "has lost contact with the thermostat in Hall")
    assert len(lost) == 1, "logged on every poll, or not at all"
    assert "C2_T2" in lost[0]
    issue = _issue(hass, proxy)
    assert issue is not None and issue.active
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"room_name": "Hall"}
    assert not issue.is_persistent, "nothing would clear it after a restart"

    proxy._client.get_data.side_effect = [dict(HEALTHY)]
    await proxy.async_update()

    assert len(_messages(caplog, logging.INFO, "thermostat in Hall (C2_T2) is reporting again after")) == 1
    assert _issue(hass, proxy) is None
    assert proxy._thermostats_lost_since == {}


async def test_repair_wording_follows_how_the_thermostat_is_connected(hass):
    """Battery advice is nonsense for a wired thermostat."""
    expected = {
        "X265_121.hex": "thermostat_contact_lost_wireless",
        "X245_122.hex": "thermostat_contact_lost_wired",
        None: "thermostat_contact_lost",
    }
    for firmware, translation_key in expected.items():
        data = {k: v for k, v in CONTACT_LOST.items() if k != "cust_SW_version_update"}
        if firmware is not None:
            data["cust_SW_version_update"] = firmware
        proxy = make_state_proxy(hass, unique_id=f"uponorx265_{firmware}")
        proxy._client.get_data.return_value = data

        await proxy.async_update()

        assert _issue(hass, proxy).translation_key == translation_key, firmware


async def test_contact_flags_are_ignored_while_the_controller_is_lost(hass, caplog):
    """They travel through the controller: a lost controller is no recovery."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = [
        dict(CONTACT_LOST),
        {**HEALTHY, "sys_controller_2_lost": "1"},
        dict(CONTACT_LOST),
    ]

    for _ in range(3):
        await proxy.async_update()

    assert len(_messages(caplog, logging.WARNING, "lost contact with the thermostat")) == 1
    assert not _messages(caplog, logging.INFO, "is reporting again")
    assert ROOM in proxy._thermostats_lost_since
    assert _issue(hass, proxy) is not None


async def test_thermostat_already_lost_at_startup_is_reported(hass, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    proxy = make_state_proxy(hass)
    proxy._client.get_data.return_value = dict(CONTACT_LOST)

    await proxy.async_update()

    assert len(_messages(caplog, logging.WARNING, "lost contact with the thermostat")) == 1
    assert _issue(hass, proxy) is not None


async def test_unloading_withdraws_the_repair(hass):
    proxy = make_state_proxy(hass)
    proxy._client.get_data.return_value = dict(CONTACT_LOST)
    await proxy.async_update()
    assert _issue(hass, proxy) is not None

    proxy.clear_contact_issues()

    assert _issue(hass, proxy) is None
