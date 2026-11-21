"""Test the flow calculation against the template of the package."""

from custom_components.electricity_flow.calculation import FlowInput, calculate_flow


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
