"""Test the price options and the daily/monthly cost sensors."""

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .helpers import make_entry

KWH = {"unit_of_measurement": "kWh"}
PRICE = {"unit_of_measurement": "EUR/kWh"}
CONFIG = {
    "grid_import_sensor": "sensor.p1_import",
    "grid_export_sensor": "sensor.p1_export",
    "solar_sensors": ["sensor.solar"],
}
OPTIONS = {
    **CONFIG,
    "price_grid_to_home": "sensor.price",
    "price_solar_to_grid": "sensor.feed_in",
}


def _local(*args) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


def _sources(hass, imp, exp, solar):
    hass.states.async_set("sensor.p1_import", imp, KWH)
    hass.states.async_set("sensor.p1_export", exp, KWH)
    hass.states.async_set("sensor.solar", solar, KWH)


def _val(hass, entity_id) -> float:
    return float(hass.states.get(entity_id).state)


async def _goto(hass, freezer, moment: datetime) -> None:
    freezer.move_to(dt_util.as_utc(moment))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup(hass, options=OPTIONS) -> MockConfigEntry:
    entry = make_entry(options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _change_device(hass, entry, kind, user_input):
    """Change the (migrated) grid, solar or battery device."""
    subentry = next(s for s in entry.subentries.values() if s.data["kind"] == kind)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == f"reconfigure_{kind}"
    return await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)


async def test_price_forms(hass: HomeAssistant) -> None:
    """Every device has the prices of the flows that come from it."""
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.bat_in", 1, KWH)
    hass.states.async_set("sensor.bat_out", 1, KWH)
    hass.states.async_set("sensor.price", 0.25, PRICE)
    hass.states.async_set("sensor.power", 300, {"unit_of_measurement": "W"})
    entry = await _setup(
        hass,
        options={
            **CONFIG,
            "battery_charge_sensors": ["sensor.bat_in"],
            "battery_discharge_sensors": ["sensor.bat_out"],
        },
    )
    # No prices: no cost sensors at all
    assert not [s for s in hass.states.async_entity_ids("sensor") if "_price_" in s]

    grid = {"grid_import_sensor": "sensor.p1_import", "grid_export_sensor": "sensor.p1_export"}
    # Only prices per energy can be picked
    with pytest.raises(InvalidData):
        await _change_device(hass, entry, "grid", {**grid, "price_grid_to_home": "sensor.power"})
    # Errors keep you on the page
    result = await _change_device(
        hass,
        entry,
        "grid",
        {**grid, "price_grid_to_home": "sensor.price", "price_template_grid_to_home": "{{ 1 }}"},
    )
    assert result["errors"] == {"price_template_grid_to_home": "price_and_template"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**grid, "price_template_grid_to_battery": "{{ 'abc' }}"}
    )
    assert result["errors"] == {"price_template_grid_to_battery": "template_not_number"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            **grid,
            "price_template_grid_to_home": "  ",
            "price_grid_to_home": "sensor.price",
            "price_grid_to_battery": "sensor.price",
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    # Solar prices on the solar set, battery prices on the battery
    result = await _change_device(
        hass,
        entry,
        "solar",
        {
            "name": "Zon",
            "solar_sensors": "sensor.solar",
            "price_template_solar_to_grid": "{{ 0.1 }}",
            "price_solar_to_battery": "sensor.price",
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    battery = {
        "name": "Battery",
        "battery_charge_sensors": "sensor.bat_in",
        "battery_discharge_sensors": "sensor.bat_out",
        "price_battery_to_home": "sensor.price",
    }
    result = await _change_device(hass, entry, "battery", battery)
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    def cost_sensors(prefix: str) -> set[str]:
        return {
            s.removeprefix(prefix).removesuffix("_day")
            for s in hass.states.async_entity_ids("sensor")
            if s.startswith(prefix) and s.endswith("_day")
        }

    # The main device only has the house, every flow is on its source
    assert cost_sensors("sensor.ef_price_") == {"house"}
    assert cost_sensors("sensor.ef_grid_price_") == {"grid_home", "grid_battery", "total"}
    assert cost_sensors("sensor.ef_zon_price_") == {"solar_grid", "solar_battery", "total"}
    # Charging the battery is in its total, not again as its own sensor
    assert cost_sensors("sensor.ef_battery_price_") == {"battery_home", "total"}
    assert (
        hass.states.get("sensor.ef_zon_price_solar_grid_day").attributes["price_template"]
        == "{{ 0.1 }}"
    )
    assert (
        hass.states.get("sensor.ef_grid_price_grid_home_day").attributes["price_entity"]
        == "sensor.price"
    )

    # The form shows the stored values again
    subentry = next(s for s in entry.subentries.values() if s.data["kind"] == "battery")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "price_battery_to_home")
    assert key.description["suggested_value"] == "sensor.price"
    key = next(k for k in schema if k == "battery_charge_sensors")
    assert key.description["suggested_value"] == "sensor.bat_in"
    hass.config_entries.subentries.async_abort(result["flow_id"])

    # Removing the prices removes the cost sensors from the registry
    await _change_device(hass, entry, "grid", grid)
    await hass.async_block_till_done()
    await _change_device(hass, entry, "solar", {"name": "Zon", "solar_sensors": "sensor.solar"})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("sensor.ef_grid_price_grid_home_day") is None
    assert registry.async_get("sensor.ef_zon_price_solar_grid_day") is None
    assert registry.async_get("sensor.ef_price_house_day") is not None
    assert registry.async_get("sensor.ef_battery_price_battery_home_day") is not None


async def test_template_price(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Price = price entity + 0.10 via a template, re-rendered when it changes."""
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 21, 14, 0, 0)))
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.20, PRICE)
    await _setup(
        hass,
        options={
            **CONFIG,
            "price_template_grid_to_home": "{{ states('sensor.price') | float + 0.1 }}",
        },
    )
    _sources(hass, 101, 50, 3)
    await hass.async_block_till_done()
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 21, 14, 5, 0)))
    hass.states.async_set("sensor.price", 0.50, PRICE)
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 11, 21, 14, 15, 0))
    # 5 min at 0.30 and 10 min at 0.60 = 0.50 average, 1 kWh
    state = hass.states.get("sensor.ef_grid_price_grid_home_day")
    assert float(state.state) == 0.5
    assert state.attributes["last_quarter_price"] == 0.5


async def test_costs_per_quarter_day_and_month(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 30, 23, 30, 0)))
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.20, PRICE)
    hass.states.async_set("sensor.feed_in", 0.05, {"unit_of_measurement": "ct/kWh"})
    await _setup(hass)

    day = "sensor.ef_grid_price_grid_home_day"
    month = "sensor.ef_grid_price_grid_home_month"
    state = hass.states.get(day)
    assert state.state == "0.0"
    assert state.attributes["unit_of_measurement"] == "EUR"
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "monetary"
    assert state.attributes["last_reset"].startswith("2026-11-30T00:00:00")
    assert hass.states.get(month).attributes["last_reset"].startswith("2026-11-01T00:00:00")

    day_values: list[float] = []

    def _record(event: Event) -> None:
        new_state = event.data["new_state"]
        if event.data["entity_id"] == day and new_state and new_state.state != "unavailable":
            day_values.append(float(new_state.state))

    hass.bus.async_listen("state_changed", _record)

    # Quarter 23:30: 1 kWh from the grid; 10 min at 0.20 and 5 min at 0.50
    _sources(hass, 101, 50, 3)
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 30, 23, 40, 0)))
    hass.states.async_set("sensor.price", 0.50, PRICE)
    await hass.async_block_till_done()
    # Costs are only booked when the quarter ends
    assert _val(hass, day) == 0

    await _goto(hass, freezer, _local(2026, 11, 30, 23, 45, 0))
    assert _val(hass, day) == 0.3
    assert _val(hass, month) == 0.3
    attrs = hass.states.get(day).attributes
    assert attrs["last_quarter_kwh"] == 1.0
    assert attrs["last_quarter_price"] == 0.3
    assert attrs["price_entity"] == "sensor.price"
    assert _val(hass, "sensor.ef_grid_price_total_day") == 0.3
    assert _val(hass, "sensor.ef_price_house_day") == 0.3
    # Solar to grid has a price but no energy this quarter
    assert _val(hass, "sensor.ef_solar_price_solar_grid_day") == 0

    # Quarter 23:45: 2 kWh at 0.50. It ends at midnight but belongs to the old
    # day and month, after which both start again at 0.
    _sources(hass, 103, 50, 3)
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 12, 1, 0, 0, 0))
    assert 1.3 in day_values
    assert _val(hass, day) == 0
    assert _val(hass, month) == 0
    assert hass.states.get(day).attributes["last_reset"].startswith("2026-12-01T00:00:00")
    assert hass.states.get(month).attributes["last_reset"].startswith("2026-12-01T00:00:00")
    assert hass.states.get(day).attributes["last_quarter_cost"] == 1.0

    # Export of solar with a feed-in price in ct/kWh (0.05 ct = 0.0005 EUR)
    _sources(hass, 103, 52, 5)
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 12, 1, 0, 15, 0))
    assert _val(hass, "sensor.ef_solar_price_solar_grid_day") == 0.001
    assert _val(hass, day) == 0
    # The total of the grid: what it delivered minus what was fed back
    assert _val(hass, "sensor.ef_grid_price_total_day") == -0.001
    assert _val(hass, "sensor.ef_solar_price_total_day") == 0.001


async def test_unavailable_price_uses_last_known(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 21, 14, 0, 0)))
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.40, PRICE)
    await _setup(hass, options={**CONFIG, "price_grid_to_home": "sensor.price"})

    hass.states.async_set("sensor.price", "unavailable", PRICE)
    _sources(hass, 100.5, 50, 3)
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 11, 21, 14, 15, 0))
    # The price was 0.40 for 0 seconds, so the last valid price is used
    assert _val(hass, "sensor.ef_grid_price_grid_home_day") == 0.2


async def test_restore_totals(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 21, 14, 5, 0)))
    today = _local(2026, 11, 21).isoformat()
    last_month = _local(2026, 10, 1).isoformat()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.ef_grid_price_grid_home_day", "1.5", {"last_reset": today}),
                {"native_value": 1.5, "native_unit_of_measurement": "EUR"},
            ),
            (
                State(
                    "sensor.ef_grid_price_grid_home_month",
                    "40.0",
                    {"last_reset": last_month},
                ),
                {"native_value": 40.0, "native_unit_of_measurement": "EUR"},
            ),
        ],
    )
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.20, PRICE)
    await _setup(hass, options={**CONFIG, "price_grid_to_home": "sensor.price"})

    # Same day: continue counting. Month total is from an old month: start at 0
    assert _val(hass, "sensor.ef_grid_price_grid_home_day") == 1.5
    assert _val(hass, "sensor.ef_grid_price_grid_home_month") == 0

    _sources(hass, 101, 50, 3)
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 11, 21, 14, 15, 0))
    assert _val(hass, "sensor.ef_grid_price_grid_home_day") == 1.7
    assert _val(hass, "sensor.ef_grid_price_grid_home_month") == 0.2
