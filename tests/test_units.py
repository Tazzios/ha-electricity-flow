"""Test the solar sets and batteries with their own device."""

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.electricity_flow.const import DOMAIN

KWH = {"unit_of_measurement": "kWh"}
PRICE = {"unit_of_measurement": "EUR/kWh"}
GRID = {
    "kind": "grid",
    "grid_import_sensor": "sensor.p1_import",
    "grid_export_sensor": "sensor.p1_export",
}
SOLAR_PRICE = {"price_solar_to_grid": "sensor.feed_in"}
BATTERY_PRICE = {"price_battery_to_home": "sensor.price"}
SUBENTRIES = [
    ConfigSubentryData(
        subentry_type="device",
        title=title,
        data=data,
        subentry_id=subentry_id,
        unique_id=None,
    )
    for subentry_id, title, data in (
        ("grid", "Net", GRID),
        (
            "east",
            "Zon Oost",
            {"kind": "solar", "solar_sensors": ["sensor.pv_east"], **SOLAR_PRICE},
        ),
        (
            "west",
            "Zon West",
            {"kind": "solar", "solar_sensors": ["sensor.pv_west"], **SOLAR_PRICE},
        ),
        (
            "a",
            "Accu A",
            {
                "kind": "battery",
                "battery_charge_sensors": ["sensor.a_charge"],
                "battery_discharge_sensors": ["sensor.a_discharge"],
                **BATTERY_PRICE,
            },
        ),
        (
            "b",
            "Accu B",
            {
                "kind": "battery",
                "battery_charge_sensors": ["sensor.b_charge"],
                "battery_discharge_sensors": ["sensor.b_discharge"],
                **BATTERY_PRICE,
            },
        ),
    )
]
METERS = (
    "sensor.p1_import",
    "sensor.p1_export",
    "sensor.pv_east",
    "sensor.pv_west",
    "sensor.a_charge",
    "sensor.a_discharge",
    "sensor.b_charge",
    "sensor.b_discharge",
)


def _local(*args) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


def _meters(hass, *values) -> None:
    for entity_id, value in zip(METERS, values, strict=True):
        hass.states.async_set(entity_id, value, KWH)


def _val(hass, entity_id) -> float:
    return float(hass.states.get(entity_id).state)


async def _setup(hass, subentries=SUBENTRIES, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={},
        options={"diagnostic_sensors": True} if options is None else options,
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_flows_and_costs_per_unit(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 12, 1)))
    hass.states.async_set("sensor.feed_in", 0.1, PRICE)
    hass.states.async_set("sensor.price", 0.3, PRICE)
    _meters(hass, 100, 50, 10, 20, 5, 5, 7, 7)
    entry = await _setup(hass)

    # East 1.5 and west 0.5 solar, battery A charges 0.5, 0.5 export.
    # House = 2.0 - 0.5 - 0.5 = 1.0
    _meters(hass, 100, 50.5, 11.5, 20.5, 5.5, 5, 7, 7)
    await hass.async_block_till_done()

    # The totals stay exactly the same as without sets
    assert _val(hass, "sensor.ef_output_house_kwh") == 1.0
    assert _val(hass, "sensor.ef_output_solar_home_kwh") == 1.0
    assert _val(hass, "sensor.ef_output_solar_grid_kwh") == 0.5

    assert _val(hass, "sensor.ef_zon_oost_output_solar_home_kwh") == 0.75
    assert _val(hass, "sensor.ef_zon_west_output_solar_home_kwh") == 0.25
    assert _val(hass, "sensor.ef_zon_oost_output_solar_grid_kwh") == 0.375
    assert _val(hass, "sensor.ef_zon_west_output_solar_battery_kwh") == 0.125
    assert _val(hass, "sensor.ef_accu_a_output_solar_battery_kwh") == 0.5
    assert _val(hass, "sensor.ef_accu_b_output_solar_battery_kwh") == 0

    # One device per set and battery, next to the main device
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == [
        "Accu A",
        "Accu B",
        "House",
        "Net",
        "Zon Oost",
        "Zon West",
    ]

    # Costs per set use the same price as the total
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 12, 15)))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert round(_val(hass, "sensor.ef_zon_oost_price_solar_grid_day"), 5) == 0.0375
    assert round(_val(hass, "sensor.ef_zon_west_price_solar_grid_day"), 5) == 0.0125
    # No total per flow on the main device: it is on the sets
    assert hass.states.get("sensor.ef_price_solar_grid_day") is None

    # Battery B discharges 0.4 to the home, 0.3 comes from the grid
    _meters(hass, 100.3, 50.5, 11.5, 20.5, 5.5, 5, 7, 7.4)
    await hass.async_block_till_done()
    assert _val(hass, "sensor.ef_accu_b_output_battery_home_kwh") == 0.4
    assert _val(hass, "sensor.ef_accu_a_output_battery_home_kwh") == 0

    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 12, 30)))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert round(_val(hass, "sensor.ef_accu_b_price_battery_home_month"), 5) == 0.12
    assert _val(hass, "sensor.ef_accu_a_price_battery_home_month") == 0
    # No price for grid to battery: no cost sensor
    assert hass.states.get("sensor.ef_accu_a_price_grid_battery_day") is None
    # The grid has no prices, but solar to grid is subtracted from its total
    assert hass.states.get("sensor.ef_net_price_grid_home_day") is None
    assert round(_val(hass, "sensor.ef_net_price_total_day"), 5) == -0.05

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_from_version_1(hass: HomeAssistant) -> None:
    """The meters of 0.8.0 become devices, the old prices are removed."""
    _meters(hass, 100, 50, 10, 20, 5, 5, 7, 7)
    old = {
        "grid_import_sensor": "sensor.p1_import",
        "grid_export_sensor": "sensor.p1_export",
        "solar_sensors": ["sensor.pv_east", "sensor.pv_west"],
        "battery_charge_sensors": ["sensor.a_charge"],
        "battery_discharge_sensors": ["sensor.a_discharge"],
        "price_solar_to_grid": "sensor.feed_in",
        "price_house": "sensor.price",
    }
    hass.states.async_set("sensor.feed_in", 0.1, PRICE)
    hass.states.async_set("sensor.price", 0.3, PRICE)
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=old)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (entry.version, entry.minor_version) == (3, 1)
    assert dict(entry.data) == {}
    assert dict(entry.options) == {}
    devices = {s.title: dict(s.data) for s in entry.subentries.values()}
    assert devices["Grid"] == GRID
    assert devices["pv east"] == {"kind": "solar", "solar_sensors": ["sensor.pv_east"]}
    assert devices["Battery"]["battery_charge_sensors"] == ["sensor.a_charge"]
    # The totals count all meters
    _meters(hass, 100, 50, 11, 20.5, 5, 5, 7, 7)
    await hass.async_block_till_done()
    assert entry.runtime_data.period_input.solar == 1.5
    assert not [s for s in hass.states.async_entity_ids("sensor") if s.startswith("sensor.ef_")]


async def test_migration_from_version_2(hass: HomeAssistant) -> None:
    """Prices of 0.9 are removed, as are the sensors that are not made any more."""
    _meters(hass, 100, 50, 10, 20, 5, 5, 7, 7)
    hass.states.async_set("sensor.price", 0.3, PRICE)
    subentries = [
        ConfigSubentryData(
            subentry_type="device",
            title="Accu A",
            data={
                "kind": "battery",
                "battery_charge_sensors": ["sensor.a_charge"],
                "battery_discharge_sensors": ["sensor.a_discharge"],
                "price_grid_to_battery": "sensor.price",
                "price_template_battery_to_home": "{{ 0.2 }}",
            },
            subentry_id="a",
            unique_id=None,
        )
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        data={},
        options={"price_house": "sensor.price"},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique in ("input_solar", "house", "house_cost_day", "a_input_battery_charge"):
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{entry.entry_id}_{unique}", config_entry=entry
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (entry.version, entry.minor_version) == (3, 1)
    assert dict(entry.options) == {}
    assert dict(entry.subentries["a"].data) == {
        "kind": "battery",
        "battery_charge_sensors": ["sensor.a_charge"],
        "battery_discharge_sensors": ["sensor.a_discharge"],
    }
    assert er.async_entries_for_config_entry(registry, entry.entry_id) == []


async def test_device_totals(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Totals per device; a battery pays for charging at the price of the source."""
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 12, 1)))
    hass.states.async_set("sensor.price", 0.3, PRICE)
    hass.states.async_set("sensor.feed_in", 0.1, PRICE)
    hass.states.async_set("sensor.solar_value", 0.2, PRICE)
    hass.states.async_set("sensor.battery_value", 0.4, PRICE)
    data = {s["subentry_id"]: dict(s["data"]) for s in SUBENTRIES}
    data["grid"] |= {"price_grid_to_home": "sensor.price", "price_grid_to_battery": "sensor.price"}
    for set_id in ("east", "west"):
        data[set_id] = {
            "kind": "solar",
            "solar_sensors": data[set_id]["solar_sensors"],
            "price_solar_to_home": "sensor.solar_value",
            "price_solar_to_battery": "sensor.solar_value",
            "price_solar_to_grid": "sensor.feed_in",
        }
    for battery_id in ("a", "b"):
        data[battery_id] |= {
            "price_battery_to_home": "sensor.battery_value",
            "price_battery_to_grid": "sensor.feed_in",
        }
    subentries = [
        ConfigSubentryData(
            subentry_type="device",
            title=s["title"],
            data=data[s["subentry_id"]],
            subentry_id=s["subentry_id"],
            unique_id=None,
        )
        for s in SUBENTRIES
    ]
    _meters(hass, 100, 50, 10, 20, 5, 5, 7, 7)
    await _setup(hass, subentries=subentries, options={})
    # Only the cost sensors, no kWh sensors without the diagnostic option
    assert hass.states.get("sensor.ef_output_house_kwh") is None

    # Quarter 1: east 1.0 solar, A charges 0.5 from solar and 1.0 from the grid,
    # house uses 0.5 solar. Grid import 1.0.
    _meters(hass, 101, 50, 11, 20, 6.5, 5, 7, 7)
    await hass.async_block_till_done()
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 12, 15)))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Charging A: 0.5 x 0.2 (solar) + 1.0 x 0.3 (grid) = 0.4, only in its total
    assert hass.states.get("sensor.ef_accu_a_price_grid_battery_day") is None
    assert round(_val(hass, "sensor.ef_accu_a_price_total_day"), 5) == -0.4
    assert _val(hass, "sensor.ef_accu_b_price_total_day") == 0
    # East delivered 0.5 home and 0.5 to the battery at 0.2
    assert round(_val(hass, "sensor.ef_zon_oost_price_total_day"), 5) == 0.2
    # The grid delivered 1.0 to the battery at 0.3
    assert round(_val(hass, "sensor.ef_net_price_total_day"), 5) == 0.3
    # The house: 0.5 solar at 0.2
    assert round(_val(hass, "sensor.ef_price_house_day"), 5) == 0.1

    # Quarter 2: A discharges 1.0: 0.6 to the house and 0.4 exported
    _meters(hass, 101, 50.4, 11, 20, 6.5, 6, 7, 7)
    await hass.async_block_till_done()
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 12, 30)))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # 0.6 x 0.4 + 0.4 x 0.1 = 0.28 back: the total goes up again
    assert round(_val(hass, "sensor.ef_accu_a_price_total_day"), 5) == -0.12
    # The grid total goes down by the export of the battery (0.4 x 0.1)
    assert round(_val(hass, "sensor.ef_net_price_total_day"), 5) == 0.26
    assert round(_val(hass, "sensor.ef_price_house_day"), 5) == 0.34


async def test_add_change_and_remove_device(hass: HomeAssistant) -> None:
    _meters(hass, 100, 50, 10, 20, 5, 5, 7, 7)
    hass.states.async_set("sensor.feed_in", 0.1, PRICE)
    hass.states.async_set("sensor.price", 0.3, PRICE)
    entry = await _setup(hass, subentries=SUBENTRIES[:2])

    # Add device -> menu -> battery
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    # The grid is already there, so it is not offered again
    assert result["menu_options"] == ["solar", "battery"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    assert result["step_id"] == "battery"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "zon oost"}
    )
    assert result["errors"] == {"name": "name_not_unique", "base": "no_meters"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Thuisaccu", "battery_charge_sensors": "sensor.pv_east"},
    )
    assert result["errors"] == {"battery_charge_sensors": "meter_in_use"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Thuisaccu",
            "battery_charge_sensors": "sensor.a_charge",
            "battery_discharge_sensors": "sensor.a_discharge",
            "price_battery_to_home": "sensor.price",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ef_thuisaccu_output_battery_home_kwh") is not None
    assert hass.states.get("sensor.ef_thuisaccu_price_battery_home_day") is not None
    battery_id = next(
        sub.subentry_id for sub in entry.subentries.values() if sub.title == "Thuisaccu"
    )

    # Rename: same sensors (unique ids follow the device, not the name)
    registry = er.async_get(hass)
    entity_id = "sensor.ef_thuisaccu_output_battery_home_kwh"
    unique_id = registry.async_get(entity_id).unique_id
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": battery_id},
    )
    assert result["step_id"] == "reconfigure_battery"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Accu garage",
            "battery_charge_sensors": "sensor.a_charge",
            "battery_discharge_sensors": "sensor.a_discharge",
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert registry.async_get(entity_id).unique_id == unique_id
    device = dr.async_get(hass).async_get_device({(DOMAIN, f"{entry.entry_id}_{battery_id}")})
    assert device.name == "Accu garage"

    # Remove the device: its device and sensors go, the totals stay
    hass.config_entries.async_remove_subentry(entry, battery_id)
    await hass.async_block_till_done()
    assert registry.async_get(entity_id) is None
    assert dr.async_get(hass).async_get(device.id) is None
    assert registry.async_get("sensor.ef_output_house_kwh") is not None
    assert registry.async_get("sensor.ef_zon_oost_output_solar_home_kwh") is not None


async def test_house_is_an_item(hass: HomeAssistant) -> None:
    """The house is made automatically, has the house costs and comes back when removed."""
    _meters(hass, 100, 50, 10, 20, 5, 5, 7, 7)
    hass.states.async_set("sensor.feed_in", 0.1, PRICE)
    hass.states.async_set("sensor.price", 0.3, PRICE)
    entry = await _setup(hass)
    house = next(s for s in entry.subentries.values() if s.data["kind"] == "house")
    assert house.title == "House"

    registry = er.async_get(hass)
    house_cost = registry.async_get("sensor.ef_price_house_day")
    assert house_cost.config_subentry_id == house.subentry_id
    assert registry.async_get("sensor.ef_output_house_kwh").config_subentry_id == (
        house.subentry_id
    )
    devices = dr.async_get(hass)
    assert not [
        d
        for d in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (DOMAIN, entry.entry_id) in d.identifiers
    ]
    assert devices.async_get(house_cost.device_id).name == "House"

    # Nothing to change
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": house.subentry_id},
    )
    assert result["reason"] == "house_nothing_to_change"

    # Removed: made again
    hass.config_entries.async_remove_subentry(entry, house.subentry_id)
    await hass.async_block_till_done()
    houses = [s for s in entry.subentries.values() if s.data["kind"] == "house"]
    assert len(houses) == 1
    assert hass.states.get("sensor.ef_price_house_day") is not None
