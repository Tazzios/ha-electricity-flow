"""Test config flow, setup and the 15 minute logic."""

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.electricity_flow.const import DOMAIN

KWH = {"unit_of_measurement": "kWh"}
CONFIG = {
    "grid_import_sensor": "sensor.p1_import",
    "grid_export_sensor": "sensor.p1_export",
    "solar_sensors": ["sensor.solar"],
    "battery_charge_sensors": ["sensor.bat_charge"],
    "battery_discharge_sensors": ["sensor.bat_discharge_wh"],
}


def _set_sources(hass, imp, exp, solar, charge, discharge_wh):
    hass.states.async_set("sensor.p1_import", imp, KWH)
    hass.states.async_set("sensor.p1_export", exp, KWH)
    hass.states.async_set("sensor.solar", solar, KWH)
    hass.states.async_set("sensor.bat_charge", charge, KWH)
    hass.states.async_set("sensor.bat_discharge_wh", discharge_wh, {"unit_of_measurement": "Wh"})


def _val(hass, entity_id) -> float:
    return float(hass.states.get(entity_id).state)


async def test_config_flow(hass: HomeAssistant) -> None:
    _set_sources(hass, 100, 50, 3, 10, 2000)
    hass.states.async_set("sensor.power", 300, {"unit_of_measurement": "W"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "grid"

    grid = {"grid_import_sensor": "sensor.p1_import", "grid_export_sensor": "sensor.p1_import"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], grid)
    assert result["errors"] == {"grid_export_sensor": "same_grid_sensor"}
    grid["grid_export_sensor"] = "sensor.p1_export"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], grid)
    assert result["step_id"] == "solar"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"solar_sensors": ["sensor.power"]}
    )
    assert result["errors"] == {"solar_sensors": "invalid_unit"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"solar_sensors": CONFIG["solar_sensors"]}
    )
    assert result["step_id"] == "battery"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "charge": {"battery_charge_sensors": CONFIG["battery_charge_sensors"]},
            "discharge": {"battery_discharge_sensors": CONFIG["battery_discharge_sensors"]},
        },
    )
    assert result["step_id"] == "house"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == CONFIG
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ef_output_house_kwh") is not None


async def test_quarter_logic(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(dt_util.as_utc(datetime(2026, 11, 21, 14, 1, 0, tzinfo=dt_util.get_default_time_zone())))
    _set_sources(hass, 100, 50, 3, 10, 2000)
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _val(hass, "sensor.ef_output_house_kwh") == 0
    last_reset = hass.states.get("sensor.ef_output_house_kwh").attributes["last_reset"]
    assert last_reset.startswith("2026-11-21T14:00:00")

    # Solar +0.5, battery discharge +200 Wh, nothing on the grid
    _set_sources(hass, 100, 50, 3.5, 10, 2200)
    await hass.async_block_till_done()
    assert _val(hass, "sensor.ef_input_solar_kwh_quarter") == 0.5
    assert _val(hass, "sensor.ef_input_battery_discharge_kwh_quarter") == 0.2
    assert _val(hass, "sensor.ef_output_house_kwh") == 0.7
    assert _val(hass, "sensor.ef_output_solar_home_kwh") == 0.5
    assert _val(hass, "sensor.ef_output_battery_home_kwh") == 0.2

    # Daily solar counter reset is treated like the utility meter does
    _set_sources(hass, 100, 50, 0.1, 10, 2200)
    await hass.async_block_till_done()
    assert _val(hass, "sensor.ef_input_solar_kwh_quarter") == 0.6

    # New quarter: everything back to zero, last_reset moves
    freezer.move_to(dt_util.as_utc(datetime(2026, 11, 21, 14, 15, 0, tzinfo=dt_util.get_default_time_zone())))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _val(hass, "sensor.ef_output_house_kwh") == 0
    assert hass.states.get("sensor.ef_output_house_kwh").attributes["last_reset"].startswith(
        "2026-11-21T14:15:00"
    )

    # Grid import that charges the battery
    _set_sources(hass, 100.7, 50, 0.1, 10.5, 2200)
    await hass.async_block_till_done()
    assert _val(hass, "sensor.ef_output_grid_battery_kwh") == 0.5
    assert _val(hass, "sensor.ef_output_grid_home_kwh") == 0.2

    # Unavailable source is ignored and does not create a jump afterwards
    hass.states.async_set("sensor.p1_import", "unavailable", KWH)
    await hass.async_block_till_done()
    assert _val(hass, "sensor.ef_output_grid_home_kwh") == 0.2

    assert await hass.config_entries.async_unload(entry.entry_id)
