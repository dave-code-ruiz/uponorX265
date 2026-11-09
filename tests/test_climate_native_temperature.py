"""The climate entity must not provide the deprecated temperature members.

HA 2026.11 renamed the climate temperature members with a `native_` prefix
(`temperature_unit`, `current_temperature`, `target_temperature` and their
`_attr_` shorthands). A class providing only the old names is still served,
but logs once per class when it is created, and the old names stop working in
HA 2027.11:

    custom_components.uponorx265.climate::UponorClimate provides the
    deprecated _attr_temperature_unit, this will stop working in Home
    Assistant 2027.11, use _attr_native_temperature_unit instead, please
    report it to the custom integration author

(and the same for `current_temperature` and `target_temperature`).

Cores older than 2026.11 only know the old names, so `UponorClimate` gives the
unit and both temperatures under both. Core serves the native ones without a
warning when a class provides both.

The warning is caught from the log, because that is the only thing it does:
the class still works either way. A class providing only the old names is
created alongside as a control, so the check fails rather than passing
silently if core ever words the warning differently.
"""

import importlib
import logging

import pytest
from homeassistant.components.climate import ClimateEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

import custom_components.uponorx265.climate as climate
from tests.helpers import make_state_proxy, raw_for, thermostat_data

THERMOSTAT = "C1_T1"
DEPRECATED = "provides the deprecated"

# 2026.11 added the native_ climate API; before it there is nothing to warn about.
NATIVE_API = hasattr(ClimateEntity, "native_current_temperature")


@pytest.mark.skipif(not NATIVE_API, reason="core predates the native_ climate API (2026.11)")
def test_climate_class_provides_no_deprecated_temperature_member(caplog):
    with caplog.at_level(logging.WARNING):
        # Control: a class with the old name alone is reported.
        class OldNamesOnly(ClimateEntity):
            _attr_temperature_unit = UnitOfTemperature.CELSIUS

        control = [r for r in caplog.records if DEPRECATED in r.getMessage()]
        caplog.clear()

        # Re-run the class body of UponorClimate, which is where core checks it.
        importlib.reload(climate)

    assert control, "the old-names-only control class was not reported"
    reported = [r.getMessage() for r in caplog.records if DEPRECATED in r.getMessage()]
    assert reported == []


@pytest.mark.parametrize(
    ("units", "current", "target", "min_temp", "max_temp"),
    [
        # Shown as reported.
        (METRIC_SYSTEM, 21.5, 19.5, 15.0, 25.0),
        # Converted from Celsius and shown in whole degrees.
        (US_CUSTOMARY_SYSTEM, 71, 67, 59, 77),
    ],
    ids=["metric", "us_customary"],
)
async def test_climate_temperatures_are_read_as_celsius(
    hass, units, current, target, min_temp, max_temp
):
    """Whichever names the core reads, it gets the values and their unit."""
    hass.config.units = units
    data = thermostat_data(THERMOSTAT, setpoint_c=19.5, min_c=15.0, max_c=25.0)
    data[f"{THERMOSTAT}_room_temperature"] = raw_for(21.5)
    proxy = make_state_proxy(hass, data=data)

    entity = climate.UponorClimate("uponorx265_test", proxy, THERMOSTAT)
    entity.hass = hass

    state = entity.state_attributes
    capabilities = entity.capability_attributes
    assert state["current_temperature"] == current
    assert state["temperature"] == target
    assert capabilities["min_temp"] == min_temp
    assert capabilities["max_temp"] == max_temp
