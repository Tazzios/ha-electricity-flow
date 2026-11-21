"""Pure energy flow calculation, a 1:1 port of the template in the package.

Kept free of Home Assistant imports so it is easy to test.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class FlowInput:
    """Energy (kWh) measured in one period."""

    solar: float = 0.0
    battery_charge: float = 0.0
    battery_discharge: float = 0.0
    grid_import: float = 0.0
    grid_export: float = 0.0


@dataclass(frozen=True, slots=True)
class FlowResult:
    """Calculated energy flows (kWh) for one period."""

    grid_net: float = 0.0
    house: float = 0.0
    solar_to_home: float = 0.0
    solar_to_battery: float = 0.0
    solar_to_grid: float = 0.0
    battery_to_home: float = 0.0
    battery_to_grid: float = 0.0
    grid_to_home: float = 0.0
    grid_to_battery: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Return the result as a dict."""
        return asdict(self)


def calculate_flow(data: FlowInput, precision: int = 3) -> FlowResult:
    """Distribute the measured energy over the possible flows.

    Priority (see the Technical Design Document):
    solar -> home -> battery -> grid, battery -> grid -> home,
    grid -> battery -> home.
    """
    solar_production = data.solar
    battery_charge = data.battery_charge
    battery_discharge = data.battery_discharge

    # The package nets import and export per period ("EF Grid kWh Quarter")
    grid_power = round(data.grid_import - data.grid_export, precision)
    grid_import = max(grid_power, 0.0)
    grid_export = max(-grid_power, 0.0)

    house_consumption = max(
        solar_production
        + battery_discharge
        + grid_import
        - battery_charge
        - grid_export,
        0.0,
    )

    solar_to_home = min(solar_production, house_consumption)
    solar_surplus = max(solar_production - solar_to_home, 0.0)
    solar_to_battery = min(battery_charge, solar_surplus)
    solar_to_grid = max(solar_surplus - solar_to_battery, 0.0)

    battery_to_grid = min(battery_discharge, grid_export)
    battery_to_home = min(
        max(battery_discharge - battery_to_grid, 0.0),
        max(house_consumption - solar_to_home, 0.0),
    )

    grid_to_battery = max(battery_charge - solar_to_battery, 0.0)
    grid_to_home = max(house_consumption - solar_to_home - battery_to_home, 0.0)

    def r(value: float) -> float:
        # "+ 0.0" turns a possible -0.0 into 0.0
        return round(value, precision) + 0.0

    return FlowResult(
        grid_net=r(grid_power),
        house=r(house_consumption),
        solar_to_home=r(solar_to_home),
        solar_to_battery=r(solar_to_battery),
        solar_to_grid=r(solar_to_grid),
        battery_to_home=r(battery_to_home),
        battery_to_grid=r(battery_to_grid),
        grid_to_home=r(grid_to_home),
        grid_to_battery=r(grid_to_battery),
    )
