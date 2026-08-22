"""_detect_thermostat_model's hwid/serial-prefix heuristic, and its fallback
to cached data when live detection isn't possible. See ARCHITECTURE.md
"Thermostat model identification" for the (reverse-engineered, unofficial)
rules this encodes.
"""

from tests.helpers import make_state_proxy

THERMOSTAT = "C1_T1"
ID_VAR = "C1_thermostat1_id"  # thermostat.replace('T', 'thermostat') + '_id'


def proxy_with(hass, hwid, serial_prefix):
    return make_state_proxy(hass, data={
        f"{THERMOSTAT}_thermostat_type": str(hwid),
        ID_VAR: f"{serial_prefix}00000",
    })


async def test_hwid_0_prefix_269_mod_1_is_t144(hass):
    proxy = proxy_with(hass, hwid=0, serial_prefix="2691")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-144"


async def test_hwid_0_prefix_269_mod_2_is_t145(hass):
    proxy = proxy_with(hass, hwid=0, serial_prefix="2692")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-145"


async def test_hwid_0_unknown_prefix_defaults_to_t145(hass):
    # Field-confirmed prefix 268, not covered by the explicit 269 rule.
    proxy = proxy_with(hass, hwid=0, serial_prefix="2688")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-145"


async def test_hwid_0_prefix_269_unknown_mod_defaults_to_t145(hass):
    # 269-prefixed but neither mod "1" nor "2" — must not fall through to
    # None, it should hit the same catch-all as any other unknown unit.
    proxy = proxy_with(hass, hwid=0, serial_prefix="2699")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-145"


async def test_hwid_2_is_t146(hass):
    proxy = proxy_with(hass, hwid=2, serial_prefix="2856")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-146"


async def test_missing_thermostat_type_falls_back_to_cached_model(hass):
    proxy = make_state_proxy(hass, data={})  # no *_thermostat_type at all
    proxy._storage_metadata = {"models": {THERMOSTAT: "T-165"}}
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-165"


async def test_missing_thermostat_type_and_no_cache_is_none(hass):
    proxy = make_state_proxy(hass, data={})
    assert proxy.get_thermostat_model(THERMOSTAT) is None
