"""Test the flow calculation against the template of the package."""

from custom_components.electricity_flow.calculation import (
    FlowInput,
    calculate_flow,
    split_battery,
    split_solar,
)


def test_example_from_design_document():
    """Solar 0.5, battery discharge 0.2, grid 0 (example in the TDD)."""
    r = calculate_flow(FlowInput(solar=0.5, battery_discharge=0.2))
    assert r.house == 0.7
    assert r.solar_to_home == 0.5
    assert r.battery_to_home == 0.2
    assert r.grid_to_home == 0
    assert r.solar_to_grid == 0


def test_solar_surplus_to_battery_and_grid():
    r = calculate_flow(FlowInput(solar=1.0, battery_charge=0.3, grid_export=0.5))
    assert r.house == 0.2
    assert r.solar_to_home == 0.2
    assert r.solar_to_battery == 0.3
    assert r.solar_to_grid == 0.5
    assert r.grid_to_battery == 0


def test_grid_charges_battery():
    r = calculate_flow(FlowInput(battery_charge=0.5, grid_import=0.7))
    assert r.house == 0.2
    assert r.grid_to_battery == 0.5
    assert r.grid_to_home == 0.2


def test_battery_to_grid():
    r = calculate_flow(FlowInput(battery_discharge=0.6, grid_export=0.4))
    assert r.house == 0.2
    assert r.battery_to_grid == 0.4
    assert r.battery_to_home == 0.2


def test_import_and_export_are_netted():
    r = calculate_flow(FlowInput(grid_import=0.3, grid_export=0.1))
    assert r.grid_net == 0.2
    assert r.house == 0.2
    assert r.grid_to_home == 0.2


def test_split_solar_by_share_of_production() -> None:
    data = FlowInput(solar=2.0, battery_charge=0.5, grid_export=0.5)
    result = calculate_flow(data)
    # East produced 1.5, west 0.5 (no meter without a set)
    split = split_solar(data, result, {"east": 1.5, "west": 0.5})
    assert split["east"] == {
        "solar_to_home": 0.75,
        "solar_to_battery": 0.375,
        "solar_to_grid": 0.375,
    }
    assert split["west"] == {
        "solar_to_home": 0.25,
        "solar_to_battery": 0.125,
        "solar_to_grid": 0.125,
    }
    for key in ("solar_to_home", "solar_to_battery", "solar_to_grid"):
        assert split["east"][key] + split["west"][key] == getattr(result, key)


def test_split_battery_charge_and_discharge_shares() -> None:
    # One battery charges from the grid while the other discharges to the home
    data = FlowInput(battery_charge=1.0, battery_discharge=0.4, grid_import=1.0)
    result = calculate_flow(data)
    split = split_battery(data, result, {"a": 1.0}, {"b": 0.4})
    assert split["a"]["grid_to_battery"] == result.grid_to_battery == 1.0
    assert split["a"]["battery_to_home"] == 0
    assert split["b"]["battery_to_home"] == result.battery_to_home == 0.4
    assert split["b"]["grid_to_battery"] == 0


def test_split_without_energy_is_zero() -> None:
    data = FlowInput()
    result = calculate_flow(data)
    assert split_solar(data, result, {"east": 0.0})["east"]["solar_to_home"] == 0
    assert split_battery(data, result, {"a": 0.0}, {})["a"]["battery_to_grid"] == 0
