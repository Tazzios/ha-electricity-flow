"""Constants for the Electricity Flow integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "electricity_flow"

# Config entry keys (source sensors, all cumulative energy meters)
CONF_SOLAR_SENSORS: Final = "solar_sensors"
CONF_BATTERY_CHARGE_SENSORS: Final = "battery_charge_sensors"
CONF_BATTERY_DISCHARGE_SENSORS: Final = "battery_discharge_sensors"
CONF_GRID_IMPORT_SENSOR: Final = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR: Final = "grid_export_sensor"

# Roles a source sensor can have
ROLE_SOLAR: Final = "solar"
ROLE_BATTERY_CHARGE: Final = "battery_charge"
ROLE_BATTERY_DISCHARGE: Final = "battery_discharge"
ROLE_GRID_IMPORT: Final = "grid_import"
ROLE_GRID_EXPORT: Final = "grid_export"

# Length of one aggregation period in minutes (the old utility_meter "quarter-hourly")
PERIOD_MINUTES: Final = 15

# Number of decimals for the calculated values (output_precision in the package)
OUTPUT_PRECISION: Final = 3

STORAGE_VERSION: Final = 1

# Keys of the calculated flows (fields of FlowResult and sensor keys)
OUTPUT_KEYS: Final = (
    "house",
    "solar_to_home",
    "solar_to_battery",
    "solar_to_grid",
    "grid_to_home",
    "grid_to_battery",
    "battery_to_home",
    "battery_to_grid",
)

# Option key of the price entity of an output: "price_<output key>"
CONF_PRICE_PREFIX: Final = "price_"


def price_option(output_key: str) -> str:
    """Return the option key of the price entity of an output flow."""
    return f"{CONF_PRICE_PREFIX}{output_key}"

# Option key of a price template of an output: "price_template_<output key>"
CONF_PRICE_TEMPLATE_PREFIX: Final = "price_template_"


def price_template_option(output_key: str) -> str:
    """Return the option key of the price template of an output flow."""
    return f"{CONF_PRICE_TEMPLATE_PREFIX}{output_key}"


# Config pages: source sensors and the output flows whose price is set there
PAGE_GRID: Final = "grid"
PAGE_SOLAR: Final = "solar"
PAGE_BATTERY: Final = "battery"
PAGE_HOUSE: Final = "house"
