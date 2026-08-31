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


async def test_thermostat_type_2_without_a_series_is_unknown(hass):
    """This used to return T-146 unconditionally - the bug behind issue #36.

    thermostat_type 2 covers T-146 on Base and T-169 on Wave. With nothing to
    say which system this is, the honest answer is no model at all.
    """
    proxy = proxy_with(hass, hwid=2, serial_prefix="2856")
    assert proxy.get_thermostat_model(THERMOSTAT) is None


async def test_missing_thermostat_type_falls_back_to_cached_model(hass):
    proxy = make_state_proxy(hass, data={})  # no *_thermostat_type at all
    proxy._storage_metadata = {"models": {THERMOSTAT: "T-165"}}
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-165"


async def test_missing_thermostat_type_and_no_cache_is_none(hass):
    proxy = make_state_proxy(hass, data={})
    assert proxy.get_thermostat_model(THERMOSTAT) is None


# ---------------------------------------------------------------------------
# Series-aware detection (issue #36)
#
# Every case below is taken from a real system. Two were confirmed by their
# owners (the 16xT-169 Wave install in issue #36, and gjo55's mixed Base
# install in issue #29); the other two are raw JNAP dumps published in
# ChrisTerBeke/homey-uponor's examples/ directory.
# ---------------------------------------------------------------------------

WAVE_FIRMWARE = "X265_121.hex"
BASE_FIRMWARE = "X245_122.hex"


def system(hass, *, firmware=None, controller_hw=None, thermostat_type, hw_type=None,
           serial_prefix="2855"):
    data = {
        "sys_controller_1_presence": "1",
        f"{THERMOSTAT}_thermostat_type": str(thermostat_type),
        ID_VAR: f"{serial_prefix}00000",
    }
    if firmware is not None:
        data["cust_SW_version_update"] = firmware
    if controller_hw is not None:
        data["C1_hardware_type"] = str(controller_hw)
    if hw_type is not None:
        data[f"{THERMOSTAT}_hw_type"] = str(hw_type)
    return make_state_proxy(hass, data=data)


async def test_wave_hw_type_7_is_t169(hass):
    """The install in issue #36: 16 units reported as T-146, actually T-169."""
    proxy = system(hass, firmware=WAVE_FIRMWARE, controller_hw=1,
                   thermostat_type=2, hw_type=7, serial_prefix="2858")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-169"


async def test_base_hw_type_3_is_t146(hass):
    """homey-uponor output_1: X245, hw_type 3, RH flat 0 - gjo55's T-146 profile."""
    proxy = system(hass, firmware=BASE_FIRMWARE, controller_hw=0,
                   thermostat_type=2, hw_type=3, serial_prefix="2856")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-146"


async def test_series_falls_back_to_controller_hardware_type(hass):
    """No firmware string: the controller's own hardware type still resolves it."""
    proxy = system(hass, controller_hw=1, thermostat_type=2, hw_type=7)
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-169"


async def test_firmware_wins_over_controller_hardware_type(hass):
    proxy = system(hass, firmware=BASE_FIRMWARE, controller_hw=1,
                   thermostat_type=2, hw_type=3)
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-146"


async def test_unobserved_hw_type_is_unknown(hass):
    """T-148/T-149/T-166/T-168 codes have never been seen - do not invent one."""
    proxy = system(hass, firmware=WAVE_FIRMWARE, controller_hw=1,
                   thermostat_type=2, hw_type=5)
    assert proxy.get_thermostat_model(THERMOSTAT) is None


async def test_missing_hw_type_is_unknown(hass):
    proxy = system(hass, firmware=WAVE_FIRMWARE, controller_hw=1, thermostat_type=2)
    assert proxy.get_thermostat_model(THERMOSTAT) is None


async def test_base_dial_rule_survives(hass):
    """Issue #29 must not regress: Base dials still resolve, and still get the
    local-override switch that depends on the model."""
    proxy = system(hass, firmware=BASE_FIRMWARE, controller_hw=0,
                   thermostat_type=0, hw_type=0, serial_prefix="2691")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-144"

    proxy = system(hass, firmware=BASE_FIRMWARE, controller_hw=0,
                   thermostat_type=0, hw_type=0, serial_prefix="2688")
    assert proxy.get_thermostat_model(THERMOSTAT) == "T-145"


async def test_wave_dial_is_not_labelled_from_base_data(hass):
    """A Wave dial would be a T-165. None has ever been observed, so the Base
    serial-prefix rule must not be applied to it."""
    proxy = system(hass, firmware=WAVE_FIRMWARE, controller_hw=1,
                   thermostat_type=0, hw_type=0, serial_prefix="2691")
    assert proxy.get_thermostat_model(THERMOSTAT) is None


# ---------------------------------------------------------------------------
# Controller model
# ---------------------------------------------------------------------------

async def test_controller_model_from_firmware(hass):
    proxy = system(hass, firmware=WAVE_FIRMWARE, thermostat_type=2)
    assert proxy.get_controller_hardware("C1") == "X-265"

    proxy = system(hass, firmware=BASE_FIRMWARE, thermostat_type=2)
    assert proxy.get_controller_hardware("C1") == "X-245"


async def test_controller_model_from_hardware_type(hass):
    proxy = system(hass, controller_hw=1, thermostat_type=2)
    assert proxy.get_controller_hardware("C1") == "X-265"

    proxy = system(hass, controller_hw=0, thermostat_type=2)
    assert proxy.get_controller_hardware("C1") == "X-245"


async def test_controller_model_unknown_without_evidence(hass):
    """Serial prefix 4194 appears on both Wave and Base - it proves nothing."""
    proxy = make_state_proxy(hass, data={
        "sys_controller_1_presence": "1",
        "controller1_id": "419469805",
    })
    assert proxy.get_controller_hardware("C1") is None
