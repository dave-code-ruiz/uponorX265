"""A low battery on a wireless thermostat, as a battery binary sensor.

Thermostat batteries last years, so a low one is easy to forget until the
thermostat goes quiet and the controller drops the room onto its fallback
cycle. The gateway reports `C?_T?_stat_battery_error`; a binary sensor with
the battery device class is what Home Assistant's own "battery became low"
trigger reads, so a single automation can watch every thermostat.

Only Wave Pulse thermostats have batteries - Base Pulse ones are powered over
their bus cable, though the gateway reports the variable for them too - and
the series is not known on a cached start until the first poll, so which
thermostats have one is cached.

The flag has not yet been seen set on real hardware. Pulling a battery is no
test of it: the power goes at once, and an hour later the controller raises
`_stat_rf_error` instead.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import CONF_HOST, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

import homeassistant.util.dt as dt_util

from custom_components.uponorx265.binary_sensor import UponorBatterySensor
from custom_components.uponorx265.const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from tests.helpers import make_state_proxy

UNIQUE_ID = "uponorx265_test"
HOST = "192.168.1.10"
LOW = "C1_T1"
LOW_ID = "286000001"
FINE = "C1_T2"
FINE_ID = "286000002"

WAVE = {
    "cust_SW_version_update": "X265_121.hex",
    "sys_controller_1_presence": "1",
    "controller1_id": "419000001",
    "C1_thermostat_1_presence": "1",
    "C1_thermostat1_id": LOW_ID,
    "C1_thermostat_2_presence": "1",
    "C1_thermostat2_id": FINE_ID,
    f"{LOW}_stat_battery_error": "1",
    f"{FINE}_stat_battery_error": "0",
    f"{LOW}_stat_rf_error": "0",
    f"{FINE}_stat_rf_error": "0",
}
BASE = {**WAVE, "cust_SW_version_update": "X245_122.hex"}

CACHED_META = {
    "thermostats": [LOW, FINE],
    "ids": {LOW: LOW_ID, FINE: FINE_ID},
    "controllers": ["C1"],
    "controller_ids": {"C1": "419000001"},
}


@pytest.fixture(autouse=True)
def _no_background_noise():
    """Keep the poll timer out of these tests."""
    with patch("custom_components.uponorx265.async_track_time_interval",
               return_value=lambda: None):
        yield


async def _setup(hass, data=None, fails=False):
    """Set an entry up for real against a mocked gateway."""
    entry_data = {CONF_HOST: HOST, "name": "Uponor"}
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, options=entry_data, unique_id=UNIQUE_ID)
    entry.add_to_hass(hass)

    client = AsyncMock()
    if fails:
        client.get_data.side_effect = HomeAssistantError("POST failed: timeout")
    else:
        client.get_data.return_value = dict(data)
    client.get_device_info.return_value = {}
    with patch("custom_components.uponorx265.UponorJnap", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()


def _seed_cache(hass_storage, meta):
    hass_storage[f"{STORAGE_KEY}_{UNIQUE_ID}"] = {
        "version": STORAGE_VERSION,
        "key": f"{STORAGE_KEY}_{UNIQUE_ID}",
        "data": {"_meta": meta},
    }


def _entity_id(hass, thermostat_id):
    return er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{UNIQUE_ID}_{thermostat_id}_battery"
    )


def _sensor(hass, data, thermostat=LOW):
    proxy = make_state_proxy(hass, data=dict(data))
    proxy._last_successful_update = dt_util.now()
    return UponorBatterySensor(UNIQUE_ID, proxy, thermostat)


# ---------------------------------------------------------------------------
# Which thermostats get one
# ---------------------------------------------------------------------------

async def test_every_wave_thermostat_gets_a_battery_sensor(hass):
    await _setup(hass, WAVE)

    low = hass.states.get(_entity_id(hass, LOW_ID))
    assert low.state == STATE_ON
    assert hass.states.get(_entity_id(hass, FINE_ID)).state == STATE_OFF
    assert low.attributes["device_class"] == BinarySensorDeviceClass.BATTERY
    assert low.attributes["friendly_name"].endswith("Battery"), "named by its device class"
    entry = er.async_get(hass).async_get(_entity_id(hass, LOW_ID))
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_wired_base_thermostats_get_none(hass):
    """The gateway reports the variable for them too; they have no battery."""
    await _setup(hass, BASE)

    assert _entity_id(hass, LOW_ID) is None
    assert _entity_id(hass, FINE_ID) is None


async def test_cached_start_builds_them_before_the_gateway_answers(hass, hass_storage):
    _seed_cache(hass_storage, {**CACHED_META, "battery": [LOW, FINE]})

    await _setup(hass, fails=True)

    assert hass.states.get(_entity_id(hass, LOW_ID)).state == STATE_UNAVAILABLE
    assert hass.states.get(_entity_id(hass, FINE_ID)).state == STATE_UNAVAILABLE


async def test_cached_base_system_builds_none(hass, hass_storage):
    _seed_cache(hass_storage, {**CACHED_META, "battery": []})

    await _setup(hass, fails=True)

    assert _entity_id(hass, LOW_ID) is None


async def test_cache_from_before_the_battery_list_falls_back_on_the_model(hass, hass_storage):
    """Otherwise the first restart after updating would build none of them."""
    _seed_cache(hass_storage, {**CACHED_META, "models": {LOW: "T-169", FINE: "T-146"}})

    await _setup(hass, fails=True)

    assert _entity_id(hass, LOW_ID) is not None, "T-169 is a Wave thermostat"
    assert _entity_id(hass, FINE_ID) is None, "T-146 is a wired Base thermostat"


async def test_a_poll_caches_which_thermostats_have_one(hass):
    for data, expected in ((WAVE, [LOW, FINE]), (BASE, [])):
        proxy = make_state_proxy(hass, unique_id=f"uponorx265_{data['cust_SW_version_update']}")
        proxy._client.get_data.return_value = dict(data)

        await proxy.async_update()

        assert proxy._storage_metadata["battery"] == expected


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

async def test_follows_the_flag(hass):
    assert _sensor(hass, WAVE, LOW).is_on is True
    assert _sensor(hass, WAVE, FINE).is_on is False


async def test_unavailable_while_the_controller_has_lost_contact(hass):
    """The flag is as old as the thermostat's last message then."""
    sensor = _sensor(hass, {**WAVE, f"{LOW}_stat_rf_error": "1"})

    assert not sensor.available


async def test_unavailable_while_the_gateway_is_not_answering(hass):
    sensor = _sensor(hass, WAVE)
    sensor._state_proxy._last_successful_update = None

    assert not sensor.available
