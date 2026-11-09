"""Telling "Home Assistant cannot reach the gateway" apart from "the gateway
cannot reach its controller" (issue #40), and saying so once.

The report was a few blips a day of "Communication failure with main
controller" on the gateway and "API error" on the thermostats, each clearing
in 2-5 minutes. Both come out of a *successful* poll: the R-208 answers, sets
`cust_controller_1_lost`, and relays placeholder values (room temperature
32767). Meanwhile a real outage between Home Assistant and the R-208 could not
show at all - the gateway status tested a bool against None, so Offline was
unreachable - and was logged as an ERROR on every poll.
"""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.uponorx265.const import (
    DOMAIN,
    STATUS_ERROR_CONTROLER,
    STATUS_ERROR_MAINCONTROLER_FAIL,
    STATUS_ERROR_TOO_HIGH_TEMP,
    STATUS_OFFLINE,
    STATUS_OK,
    STATUS_ONLINE,
    UNAVAILABLE_THRESHOLD,
)
from tests.helpers import make_state_proxy

LOGGER = "custom_components.uponorx265"

HEALTHY = {
    "sys_controller_1_presence": "1",
    "sys_controller_2_presence": "1",
    "C1_thermostat_1_presence": "1",
    "C2_thermostat_1_presence": "1",
    "controller1_id": "419524869",
    "controller2_id": "419524870",
    "C1_thermostat1_id": "285805788",
    "C2_thermostat1_id": "285805789",
    "cust_controller_1_lost": "0",
    "sys_controller_1_lost": "0",
    "sys_controller_2_lost": "0",
    "C1_T1_room_temperature": "716",  # 22.0 C
    "C2_T1_room_temperature": "716",
}

# What the R-208 relays while it has lost the controller it is attached to.
MAIN_LOST = {
    **HEALTHY,
    "cust_controller_1_lost": "1",
    "C1_T1_room_temperature": "32767",
    "C2_T1_room_temperature": "32767",
}

SUB_LOST = {**HEALTHY, "sys_controller_2_lost": "1", "C2_T1_room_temperature": "32767"}


def _polled(hass, data):
    """A proxy whose last poll succeeded just now with `data`."""
    proxy = make_state_proxy(hass, data=dict(data))
    proxy._last_successful_update = dt_util.now()
    return proxy


def _messages(caplog, level, fragment):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == level and fragment in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# Gateway status
# ---------------------------------------------------------------------------

async def test_gateway_status_is_offline_before_any_successful_poll(hass):
    proxy = make_state_proxy(hass, data=dict(HEALTHY))

    assert proxy.get_gateway_status() == STATUS_OFFLINE, (
        "is_available() returns a bool; testing it against None never matched"
    )


async def test_gateway_status_goes_offline_once_the_poll_is_stale(hass):
    proxy = _polled(hass, MAIN_LOST)
    proxy._last_successful_update = dt_util.now() - UNAVAILABLE_THRESHOLD - timedelta(seconds=1)

    assert proxy.get_gateway_status() == STATUS_OFFLINE, (
        "an unreachable gateway kept showing whatever its last answer said"
    )


async def test_gateway_status_online_and_main_controller_lost(hass):
    assert _polled(hass, HEALTHY).get_gateway_status() == STATUS_ONLINE
    assert _polled(hass, MAIN_LOST).get_gateway_status() == STATUS_ERROR_MAINCONTROLER_FAIL


# ---------------------------------------------------------------------------
# Controller loss: thermostat status and availability
# ---------------------------------------------------------------------------

async def test_main_controller_lost_is_not_reported_as_api_error(hass):
    proxy = _polled(hass, MAIN_LOST)

    assert proxy.is_available(), "the gateway itself is answering"
    for thermostat in ("C1_T1", "C2_T1"):
        status = proxy.get_status(thermostat)
        assert status != STATUS_ERROR_TOO_HIGH_TEMP, "placeholder 32767 reported as API error"
        assert status == STATUS_ERROR_CONTROLER
        assert not proxy.is_thermostat_available(thermostat), (
            "placeholder values must not be reported as readings"
        )
    for controller in ("C1", "C2"):
        assert proxy.get_controller_status(controller) == STATUS_ERROR_CONTROLER
        assert not proxy.is_controller_available(controller)


async def test_lost_sub_controller_only_affects_its_own_thermostats(hass):
    proxy = _polled(hass, SUB_LOST)

    assert proxy.is_thermostat_available("C1_T1")
    assert proxy.get_status("C1_T1") == STATUS_OK
    assert not proxy.is_thermostat_available("C2_T1")
    assert proxy.get_status("C2_T1") == STATUS_ERROR_CONTROLER
    assert proxy.get_gateway_status() == STATUS_ONLINE


async def test_missing_reading_without_controller_loss_is_still_reported(hass):
    """The relabelled state keeps its key, so existing automations still match."""
    proxy = _polled(hass, {**HEALTHY, "C1_T1_room_temperature": "32767"})

    assert proxy.get_status("C1_T1") == STATUS_ERROR_TOO_HIGH_TEMP
    assert proxy.is_thermostat_available("C1_T1")


async def test_healthy_system_is_fully_available(hass):
    proxy = _polled(hass, HEALTHY)

    for thermostat in ("C1_T1", "C2_T1"):
        assert proxy.is_thermostat_available(thermostat)
        assert proxy.get_status(thermostat) == STATUS_OK


# ---------------------------------------------------------------------------
# Polling: log transitions, not every poll
# ---------------------------------------------------------------------------

async def test_outage_logs_once_and_recovery_logs_duration(hass, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = [
        HomeAssistantError("timeout"),
        HomeAssistantError("timeout"),
        HomeAssistantError("timeout"),
        dict(HEALTHY),
    ]

    for _ in range(4):
        await proxy.async_update()

    assert len(_messages(caplog, logging.WARNING, "is not responding")) == 1
    assert len(_messages(caplog, logging.DEBUG, "still not responding")) == 2
    assert not [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.ERROR], (
        "an outage was logged as an error on every poll"
    )
    assert len(_messages(caplog, logging.INFO, "is responding again after")) == 1
    assert proxy._unavailable_since is None


async def test_failed_poll_pushes_a_state_update(hass):
    """Entities are push-updated; without this they never notice an outage."""
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = HomeAssistantError("timeout")

    with patch.object(proxy, "call_state_update", AsyncMock()) as pushed:
        await proxy.async_update()
        await hass.async_block_till_done()

    pushed.assert_awaited()


async def test_processing_error_is_not_counted_as_an_outage(hass, caplog):
    proxy = make_state_proxy(hass)
    proxy._client.get_data.return_value = dict(HEALTHY)

    with patch.object(
        proxy, "_async_persist_discovery_metadata", AsyncMock(side_effect=RuntimeError("bug"))
    ), patch.object(proxy, "call_state_update", AsyncMock()) as pushed:
        await proxy.async_update()
        await hass.async_block_till_done()

    assert proxy._unavailable_since is None, "a local bug was treated as the gateway going away"
    assert proxy.is_available()
    assert proxy._data == HEALTHY
    # Otherwise the fresh data never reaches the entities.
    pushed.assert_awaited()
    assert _messages(caplog, logging.ERROR, "Error processing data")


async def test_controller_link_loss_and_recovery_are_logged_once(hass, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = [dict(MAIN_LOST), dict(MAIN_LOST), dict(HEALTHY)]

    for _ in range(3):
        await proxy.async_update()

    assert len(_messages(caplog, logging.WARNING, "lost contact with the main controller")) == 1
    assert len(_messages(caplog, logging.INFO, "in contact with the main controller again")) == 1
    assert proxy._links_lost_since == {}


async def test_sub_controller_state_is_kept_while_main_is_lost(hass, caplog):
    """Sub-controller flags travel through the main controller: no false recovery."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = [
        dict(SUB_LOST),
        {**MAIN_LOST, "sys_controller_2_lost": "0"},
        dict(SUB_LOST),
    ]

    for _ in range(3):
        await proxy.async_update()

    assert len(_messages(caplog, logging.WARNING, "lost contact with the controller C2")) == 1
    assert not _messages(caplog, logging.INFO, "in contact with the controller C2 again")
    assert "controller C2" in proxy._links_lost_since


# ---------------------------------------------------------------------------
# Config flow (issue #41)
# ---------------------------------------------------------------------------

async def _submit_host(hass, side_effect):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(
        "custom_components.uponorx265.config_flow.UponorJnap.get_data",
        AsyncMock(side_effect=side_effect),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.10", CONF_NAME: "Uponor"}
        )


async def test_unreachable_gateway_is_not_called_an_invalid_host(hass):
    result = await _submit_host(hass, HomeAssistantError("POST failed: timeout"))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unexpected_answer_is_reported_as_such(hass):
    result = await _submit_host(hass, ValueError("missing 'output'"))

    assert result["errors"] == {"base": "invalid_response"}
