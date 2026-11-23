"""Entity ids of the sensors, in the style of the original package (sensor.ef_...).

The totals keep the ids of the package and of the first versions, so their
history continues. The sensors of a solar set or battery get the same ids with
the name of the device after "ef_", for example sensor.ef_dak_oost_output_solar_home_kwh.
"""

from __future__ import annotations

from homeassistant.util import slugify

# Output key -> short name in the entity id
_SHORT = {
    "house": "house",
    "solar_to_home": "solar_home",
    "solar_to_battery": "solar_battery",
    "solar_to_grid": "solar_grid",
    "grid_to_home": "grid_home",
    "grid_to_battery": "grid_battery",
    "battery_to_home": "battery_home",
    "battery_to_grid": "battery_grid",
}


def _prefix(device_name: str | None) -> str:
    if device_name and (slug := slugify(device_name)):
        return f"ef_{slug}_"
    return "ef_"


def energy_entity_id(key: str, device_name: str | None = None) -> str:
    """Return the entity id of a flow (kWh per quarter) sensor."""
    return f"sensor.{_prefix(device_name)}output_{_SHORT.get(key, key)}_kwh"


def cost_entity_id(key: str, period: str, device_name: str | None = None) -> str:
    """Return the entity id of a day or month cost sensor (key "total": of a device)."""
    return f"sensor.{_prefix(device_name)}price_{_SHORT.get(key, key)}_{period}"
