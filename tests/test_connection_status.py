"""Telling "Home Assistant cannot reach the gateway" apart from "the gateway
cannot reach its controller" (issue #40), and saying so once.

The report was a few blips a day of "Communication failure with main
controller" on the gateway and "API error" on the thermostats, each clearing
in 2-5 minutes. Both come out of a *successful* poll: the R-208 answers, sets
`cust_controller_1_lost`, and relays placeholder values (room temperature
32767). Meanwhile a real outage between Home Assistant and the R-208 could not
show at all - the gateway status tested a bool against None, so Offline was
unreachable - and was logged as an ERROR on every poll.

Pulling cables on a live Wave Pulse system then showed the reload that followed
two minutes of silence did nothing useful: the client it rebuilt is the same
address on Home Assistant's shared HTTP session, both recoveries came from the
ordinary poll, and because the reload discarded its own cooldown it repeated
every two minutes for as long as the gateway was gone. What replaces it is a
repair, raised once a fault has lasted REPAIR_DELAY.
"""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import homeassistant.util.dt as dt_util
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import async_setup_entry
from custom_components.uponorx265.const import (
    DOMAIN,
    REPAIR_DELAY,
    STATUS_ERROR_COMFAILOUT,
    STATUS_ERROR_CONTROLER,
    STATUS_ERROR_GENERAL,
    STATUS_ERROR_MAINCONTROLER_FAIL,
    STATUS_ERROR_TOO_HIGH_TEMP,
    STATUS_OFFLINE,
    STATUS_OK,
    STATUS_ONLINE,
    UNAVAILABLE_THRESHOLD,
)
from custom_components.uponorx265.sensor import UponorGatewayStatusSensor
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


def _rewind(proxy, delta):
    """Move every timestamp the proxy keeps back by `delta`, as if that much time had passed."""
    if proxy._last_successful_update is not None:
        proxy._last_successful_update -= delta
    if proxy._unavailable_since is not None:
        proxy._unavailable_since -= delta
    for link in proxy._links_lost_since:
        proxy._links_lost_since[link] -= delta


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


GATEWAY_FAILS = HomeAssistantError("POST http://10.0.0.1/JNAP/ failed: timeout")


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


async def test_gateway_status_sensor_shows_offline_instead_of_unavailable(hass):
    """The proxy saying Offline is not enough: HA only reads native_value while
    the entity is available. `_attr_available = True` on the sensor was
    shadowed by UponorGatewayEntity's `available` property, so during an outage
    the sensor went unavailable and Offline was never displayed."""
    proxy = _polled(hass, HEALTHY)
    proxy._last_successful_update = dt_util.now() - UNAVAILABLE_THRESHOLD - timedelta(seconds=1)
    sensor = UponorGatewayStatusSensor("uponorx265_test", proxy)

    assert not proxy.is_available()
    assert sensor.available
    assert sensor.native_value == STATUS_OFFLINE


# ---------------------------------------------------------------------------
# Controller status
# ---------------------------------------------------------------------------

async def test_controller_out_module_and_system_alarm_are_reported(hass):
    """Both used to be looked up as `C1stat_...`, a name the gateway never sends."""
    proxy = _polled(hass, {
        **HEALTHY,
        "C1_stat_out_module_com_lost": "1",
        "C2_stat_general_system_alarm": "1",
    })

    assert proxy.get_controller_status("C1") == STATUS_ERROR_COMFAILOUT
    assert proxy.get_controller_status("C2") == STATUS_ERROR_GENERAL


async def test_controller_status_ok_when_alarm_flags_are_clear(hass):
    proxy = _polled(hass, {
        **HEALTHY,
        "C1_stat_out_module_com_lost": "0",
        "C1_stat_general_system_alarm": "0",
    })

    assert proxy.get_controller_status("C1") == STATUS_OK


async def test_controller_loss_outranks_its_alarm_flags(hass):
    proxy = _polled(hass, {**MAIN_LOST, "C1_stat_general_system_alarm": "1"})

    assert proxy.get_controller_status("C1") == STATUS_ERROR_CONTROLER


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


async def test_a_single_failed_poll_is_not_an_outage(hass, caplog):
    """One poll in a few hundred fails on a healthy system and the next one
    answers. Nothing goes unavailable, so there is nothing to warn about."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    proxy = _polled(hass, HEALTHY)
    proxy._client.get_data.side_effect = [GATEWAY_FAILS, dict(HEALTHY)]

    await proxy.async_update()
    assert proxy.is_available()
    await proxy.async_update()

    assert not _messages(caplog, logging.WARNING, "not responding")
    assert not _messages(caplog, logging.INFO, "is responding again")
    assert len(_messages(caplog, logging.DEBUG, "did not answer a poll")) == 1
    assert len(_messages(caplog, logging.DEBUG, "is responding again after")) == 1


async def test_outage_is_announced_when_the_entities_go_unavailable(hass, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    proxy = _polled(hass, HEALTHY)
    proxy._client.get_data.side_effect = GATEWAY_FAILS

    await proxy.async_update()
    assert not _messages(caplog, logging.WARNING, "is not responding")

    _rewind(proxy, UNAVAILABLE_THRESHOLD)
    await proxy.async_update()
    assert not proxy.is_available()
    assert len(_messages(caplog, logging.WARNING, "is not responding")) == 1

    _rewind(proxy, timedelta(minutes=1))
    await proxy.async_update()
    assert len(_messages(caplog, logging.WARNING, "is not responding")) == 1, "logged again"

    proxy._client.get_data.side_effect = [dict(HEALTHY)]
    await proxy.async_update()
    assert len(_messages(caplog, logging.INFO, "is responding again after")) == 1


async def test_a_long_outage_never_reloads_the_entry(hass):
    """Measured: three reloads in seven minutes, none of which brought anything back."""
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = GATEWAY_FAILS

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        for _ in range(4):
            await proxy.async_update()
            _rewind(proxy, REPAIR_DELAY)

    reload.assert_not_awaited()


# ---------------------------------------------------------------------------
# Repairs for connection faults that last
# ---------------------------------------------------------------------------

async def test_gateway_repair_waits_for_the_delay_and_clears_on_the_next_answer(hass):
    proxy = make_state_proxy(hass, entry_data={CONF_NAME: "Home"})
    issue_id = proxy._gateway_issue_id()
    proxy._client.get_data.side_effect = GATEWAY_FAILS

    await proxy.async_update()
    _rewind(proxy, REPAIR_DELAY - timedelta(seconds=1))
    await proxy.async_update()
    assert _issue(hass, issue_id) is None, "an R-208 restart answers again well inside this"

    _rewind(proxy, timedelta(seconds=1))
    await proxy.async_update()
    issue = _issue(hass, issue_id)
    assert issue is not None and issue.active
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "gateway_unreachable"
    assert issue.translation_placeholders == {"name": "Home", "host": "10.0.0.1", "minutes": "10"}
    assert not issue.is_persistent, "nothing would clear it after a restart"

    proxy._client.get_data.side_effect = [dict(HEALTHY)]
    await proxy.async_update()
    assert _issue(hass, issue_id) is None


async def test_main_controller_repair_only_for_a_loss_that_lasts(hass):
    """The #40 reports cleared by themselves within a few minutes."""
    proxy = make_state_proxy(hass)
    issue_id = proxy._link_issue_id("main controller")
    proxy._client.get_data.side_effect = [dict(MAIN_LOST), dict(HEALTHY)]

    await proxy.async_update()
    await proxy.async_update()
    assert _issue(hass, issue_id) is None

    proxy._client.get_data.side_effect = [dict(MAIN_LOST), dict(MAIN_LOST), dict(HEALTHY)]
    await proxy.async_update()
    _rewind(proxy, REPAIR_DELAY)
    await proxy.async_update()

    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR, "nothing at all can be read or changed"
    assert issue.translation_key == "main_controller_lost"
    assert issue.translation_placeholders == {"name": "Uponor", "minutes": "10"}

    await proxy.async_update()
    assert _issue(hass, issue_id) is None


async def test_sub_controller_repair_names_the_controller(hass):
    proxy = make_state_proxy(hass)
    issue_id = proxy._link_issue_id("controller C2")
    proxy._client.get_data.side_effect = [dict(SUB_LOST), dict(SUB_LOST), dict(HEALTHY)]

    await proxy.async_update()
    _rewind(proxy, REPAIR_DELAY)
    await proxy.async_update()

    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING, "the other controllers are fine"
    assert issue.translation_key == "controller_lost"
    assert issue.translation_placeholders["controller_name"] == "Uponor Controller 2"
    assert _issue(hass, proxy._link_issue_id("main controller")) is None

    await proxy.async_update()
    assert _issue(hass, issue_id) is None


async def test_controller_loss_that_outlasts_a_gateway_outage_is_raised_on_return(hass):
    """The loss is timed from when it was first seen, not from the last answer."""
    proxy = make_state_proxy(hass)
    issue_id = proxy._link_issue_id("main controller")
    proxy._client.get_data.side_effect = [dict(MAIN_LOST), GATEWAY_FAILS, dict(MAIN_LOST)]

    await proxy.async_update()
    await proxy.async_update()
    _rewind(proxy, REPAIR_DELAY)
    await proxy.async_update()

    assert _issue(hass, issue_id) is not None


async def test_unloading_withdraws_the_connection_repairs(hass):
    proxy = make_state_proxy(hass)
    proxy._client.get_data.side_effect = [dict(SUB_LOST), dict(SUB_LOST), GATEWAY_FAILS, GATEWAY_FAILS]
    await proxy.async_update()
    _rewind(proxy, REPAIR_DELAY)
    await proxy.async_update()
    await proxy.async_update()
    _rewind(proxy, REPAIR_DELAY)
    await proxy.async_update()
    link_issue = proxy._link_issue_id("controller C2")
    assert _issue(hass, link_issue) is not None
    assert _issue(hass, proxy._gateway_issue_id()) is not None

    proxy.clear_issues()

    assert _issue(hass, link_issue) is None
    assert _issue(hass, proxy._gateway_issue_id()) is None


# ---------------------------------------------------------------------------
# Setup with nothing cached
# ---------------------------------------------------------------------------

async def test_first_setup_without_an_answer_is_retried_by_home_assistant(hass, caplog):
    """Setup used to carry on with no thermostats and only came right because
    the integration kept reloading itself while the gateway was down."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "10.0.0.1", CONF_NAME: "Uponor"},
        options={CONF_HOST: "10.0.0.1", CONF_NAME: "Uponor"},
        unique_id="uponorx265_test",
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.get_data.side_effect = GATEWAY_FAILS

    with patch("custom_components.uponorx265.UponorJnap", return_value=client), \
         patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()) as platforms, \
         pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)

    platforms.assert_not_awaited()
    assert "uponorx265_test" not in hass.data
    # Home Assistant logs a setup retry itself.
    assert not _messages(caplog, logging.WARNING, "is not responding")


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
