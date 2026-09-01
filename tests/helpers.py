"""Shared test helpers for constructing an UponorStateProxy without real network/storage I/O."""

from unittest.mock import AsyncMock

from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uponorx265 import UponorStateProxy
from custom_components.uponorx265.const import DOMAIN, STORAGE_KEY, STORAGE_VERSION

# A single thermostat's setpoint/limits, matching Uponor's raw encoding
# (raw = degrees_C * 18 + 320). Heating mode, min 15.0 / max 25.0 as in the
# bug report's reproduction environment.
MIN_TEMP_RAW = 590   # 15.0 C
MAX_TEMP_RAW = 770   # 25.0 C


def raw_for(temp_c: float) -> int:
    return round(temp_c * 18 + 320)


def make_state_proxy(hass, data=None, entry_data=None, unique_id="uponorx265_test"):
    """Build an UponorStateProxy with a mocked JNAP client and hass-backed Store.

    Network calls (send_data) are captured on proxy._client.send_data instead
    of hitting a real gateway.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data or {}, unique_id=unique_id)
    entry.add_to_hass(hass)

    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{unique_id}")
    proxy = UponorStateProxy(hass, "10.0.0.1", None, store, unique_id, entry)
    proxy._client = AsyncMock()
    # The gateway core action is optional; default it to "not answered" so a
    # test only sees device info when it opts in.
    proxy._client.get_device_info.return_value = {}

    if data:
        proxy._data.update(data)

    return proxy


def thermostat_data(thermostat: str, setpoint_c: float, min_c: float = 15.0, max_c: float = 25.0) -> dict:
    """Raw _data entries for one thermostat at the given setpoint/limits."""
    return {
        f"{thermostat}_setpoint": raw_for(setpoint_c),
        f"{thermostat}_minimum_setpoint": raw_for(min_c),
        f"{thermostat}_maximum_setpoint": raw_for(max_c),
    }
