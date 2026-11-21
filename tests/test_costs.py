"""Test the price options and the daily/monthly cost sensors."""

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from homeassistant.core import Event, HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.electricity_flow.const import DOMAIN

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
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


GRID = {"grid_import_sensor": "sensor.p1_import", "grid_export_sensor": "sensor.p1_export"}
SOLAR = {"solar_sensors": ["sensor.solar"]}
BATTERY = {"charge": {}, "discharge": {}}


async def _options(hass, entry, grid=GRID, solar=SOLAR, battery=BATTERY, house=None):
    """Walk through the Grid, Solar, Battery and House pages of the options."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step, user_input in (
        ("grid", grid),
        ("solar", solar),
        ("battery", battery),
        ("house", house or {}),
    ):
        assert result["step_id"] == step
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input
        )
        if result.get("errors"):
            return result
    return result


async def test_options_flow_prices(hass: HomeAssistant) -> None:
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.25, PRICE)
    hass.states.async_set("sensor.power", 300, {"unit_of_measurement": "W"})
    entry = await _setup(hass, options={})

    # Page errors keep you on the page
    result = await _options(hass, entry, house={"price_house": "sensor.power"})
    assert result["step_id"] == "house"
    assert result["errors"] == {"price_house": "invalid_price_unit"}
    result = await _options(
        hass, entry, house={"price_house": "sensor.price", "price_template_house": "{{ 0.3 }}"}
    )
    assert result["errors"] == {"price_template_house": "price_and_template"}
    result = await _options(hass, entry, house={"price_template_house": "{{ 'abc' }}"})
    assert result["errors"] == {"price_template_house": "template_not_number"}
    # Errors inside a battery group
    result = await _options(
        hass,
        entry,
        battery={"charge": {"price_grid_to_battery": "sensor.power"}, "discharge": {}},
    )
    assert result["step_id"] == "battery"
    assert result["errors"] == {"price_grid_to_battery": "invalid_price_unit"}

    result = await _options(
        hass,
        entry,
        GRID,
        solar={**SOLAR, "price_template_solar_to_grid": "{{ 0.1 }}"},
        battery={
            "charge": {
                "price_grid_to_battery": "sensor.price",
                "price_template_solar_to_battery": "{{ 0.05 }}",
            },
            "discharge": {"price_template_battery_to_grid": "{{ 0.2 }}"},
        },
        house={"price_template_house": "  ", "price_grid_to_home": "sensor.price"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["price_grid_to_home"] == "sensor.price"
    assert entry.options["price_template_solar_to_grid"] == "{{ 0.1 }}"
    assert entry.options["price_grid_to_battery"] == "sensor.price"
    assert entry.options["price_template_battery_to_grid"] == "{{ 0.2 }}"
    assert entry.options["price_template_solar_to_battery"] == "{{ 0.05 }}"
    assert "price_template_house" not in entry.options
    assert "charge" not in entry.options
    await hass.async_block_till_done()

    # Entry is reloaded: only the flows with a price get cost sensors
    assert hass.states.get("sensor.ef_price_grid_home_day") is not None
    assert hass.states.get("sensor.ef_price_grid_home_month") is not None
    assert hass.states.get("sensor.ef_price_grid_battery_day") is not None
    assert (
        hass.states.get("sensor.ef_price_solar_grid_day").attributes["price_template"]
        == "{{ 0.1 }}"
    )
    assert hass.states.get("sensor.ef_price_house_day") is None

    # The form shows the stored values again, also inside the groups
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], GRID)
    result = await hass.config_entries.options.async_configure(result["flow_id"], SOLAR)
    charge_schema = result["data_schema"].schema["charge"].schema.schema
    key = next(k for k in charge_schema if k == "price_grid_to_battery")
    assert key.description["suggested_value"] == "sensor.price"
    hass.config_entries.options.async_abort(result["flow_id"])

    # Removing the prices removes the cost sensors from the registry
    result = await _options(hass, entry)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get("sensor.ef_price_grid_home_day") is None
    assert er.async_get(hass).async_get("sensor.ef_price_solar_grid_day") is None


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
    state = hass.states.get("sensor.ef_price_grid_home_day")
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

    day = "sensor.ef_price_grid_home_day"
    month = "sensor.ef_price_grid_home_month"
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
    # Solar to grid has a price but no energy this quarter
    assert _val(hass, "sensor.ef_price_solar_grid_day") == 0

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
    assert _val(hass, "sensor.ef_price_solar_grid_day") == 0.001
    assert _val(hass, day) == 0


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
    assert _val(hass, "sensor.ef_price_grid_home_day") == 0.2


async def test_restore_totals(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 21, 14, 5, 0)))
    today = _local(2026, 11, 21).isoformat()
    last_month = _local(2026, 10, 1).isoformat()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.ef_price_grid_home_day", "1.5", {"last_reset": today}),
                {"native_value": 1.5, "native_unit_of_measurement": "EUR"},
            ),
            (
                State("sensor.ef_price_grid_home_month", "40.0", {"last_reset": last_month}),
                {"native_value": 40.0, "native_unit_of_measurement": "EUR"},
            ),
        ],
    )
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.20, PRICE)
    await _setup(hass, options={**CONFIG, "price_grid_to_home": "sensor.price"})

    # Same day: continue counting. Month total is from an old month: start at 0
    assert _val(hass, "sensor.ef_price_grid_home_day") == 1.5
    assert _val(hass, "sensor.ef_price_grid_home_month") == 0

    _sources(hass, 101, 50, 3)
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 11, 21, 14, 15, 0))
    assert _val(hass, "sensor.ef_price_grid_home_day") == 1.7
    assert _val(hass, "sensor.ef_price_grid_home_month") == 0.2


async def test_migrate_old_entity_ids(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """sensor.ef_cost_* of older versions becomes sensor.ef_price_*, keeping the total."""
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 21, 14, 5, 0)))
    today = _local(2026, 11, 21).isoformat()
    entry = MockConfigEntry(
        domain=DOMAIN, data=CONFIG, options={**CONFIG, "price_grid_to_home": "sensor.price"}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_grid_to_home_cost_day",
        suggested_object_id="ef_cost_grid_home_day",
        config_entry=entry,
    )
    # A user defined entity id is left alone
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_grid_to_home_cost_month",
        suggested_object_id="my_grid_cost_month",
        config_entry=entry,
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.ef_cost_grid_home_day", "1.5", {"last_reset": today}),
                {"native_value": 1.5, "native_unit_of_measurement": "EUR"},
            ),
        ],
    )
    _sources(hass, 100, 50, 3)
    hass.states.async_set("sensor.price", 0.20, PRICE)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get("sensor.ef_cost_grid_home_day") is None
    state = hass.states.get("sensor.ef_price_grid_home_day")
    assert float(state.state) == 1.5
    assert state.attributes["friendly_name"] == "Electricity Flow Day € – Grid to home"
    assert hass.states.get("sensor.my_grid_cost_month") is not None
