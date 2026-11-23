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
        solar_production + battery_discharge + grid_import - battery_charge - grid_export,
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


# Flows that are divided over the solar sets, by share of the production
SOLAR_UNIT_FLOWS: tuple[str, ...] = ("solar_to_home", "solar_to_battery", "solar_to_grid")
# Flows that are divided over the batteries, by share of the discharge
BATTERY_DISCHARGE_FLOWS: tuple[str, ...] = ("battery_to_home", "battery_to_grid")
# Flows that are divided over the batteries, by share of the charge
BATTERY_CHARGE_FLOWS: tuple[str, ...] = ("solar_to_battery", "grid_to_battery")
BATTERY_UNIT_FLOWS: tuple[str, ...] = BATTERY_DISCHARGE_FLOWS + BATTERY_CHARGE_FLOWS


def _share(part: float, total: float) -> float:
    return part / total if total > 0 else 0.0


def split_solar(
    data: FlowInput,
    result: FlowResult,
    production: dict[str, float],
    precision: int = 3,
) -> dict[str, dict[str, float]]:
    """Divide the solar flows over the solar sets, by their share of the production.

    production: unit id -> energy produced by that set in the period. The
    share is taken of data.solar (all solar meters, also the ones that
    are not part of a set), so the sets together never get more than the total.
    """
    total = data.solar
    return {
        unit: {
            key: round(getattr(result, key) * _share(energy, total), precision) + 0.0
            for key in SOLAR_UNIT_FLOWS
        }
        for unit, energy in production.items()
    }


def split_battery(
    data: FlowInput,
    result: FlowResult,
    charge: dict[str, float],
    discharge: dict[str, float],
    precision: int = 3,
) -> dict[str, dict[str, float]]:
    """Divide the battery flows over the batteries.

    Discharge flows go by share of the discharged energy, charge flows by share
    of the charged energy (unit id -> energy in the period). The shares are
    taken of the totals in data, which include meters without a battery name.
    """
    total_charge = data.battery_charge
    total_discharge = data.battery_discharge
    split: dict[str, dict[str, float]] = {}
    for unit in {**charge, **discharge}:
        discharge_share = _share(discharge.get(unit, 0.0), total_discharge)
        charge_share = _share(charge.get(unit, 0.0), total_charge)
        flows = {key: getattr(result, key) * discharge_share for key in BATTERY_DISCHARGE_FLOWS}
        flows |= {key: getattr(result, key) * charge_share for key in BATTERY_CHARGE_FLOWS}
        split[unit] = {key: round(value, precision) + 0.0 for key, value in flows.items()}
    return split
