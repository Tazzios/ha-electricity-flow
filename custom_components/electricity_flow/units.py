"""The grid, solar sets and batteries, added as devices.

They are sub-entries of the integration ("Add device" on the integration
page), like ha-power-insight does. The grid (one at most) has the import and
export meter, a solar set one or more solar meters, a battery one charge and
one discharge meter. Every one gets its own device and sensors. All meters
together make the totals.

Prices are set at the source of a flow: the solar set for solar to
home/grid/battery, the grid for grid to home/battery and the battery for
battery to home/grid.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry

from .const import (
    CONF_BATTERY_CHARGE_SENSORS,
    CONF_BATTERY_DISCHARGE_SENSORS,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_SOLAR_SENSORS,
    OUTPUT_KEYS,
    price_option,
    price_template_option,
)
from .issues import TEMPLATE_PREFIX

SUBENTRY_TYPE = "device"
CONF_KIND = "kind"
KIND_GRID = "grid"
KIND_SOLAR = "solar"
KIND_BATTERY = "battery"
# The house: made automatically, has the house costs and the totals
KIND_HOUSE = "house"
HOUSE_TITLES = {"nl": "Huis", "de": "Haus"}

# Flows whose price is set on a device of that kind (the source of the flow)
KIND_PRICED_FLOWS: dict[str, tuple[str, ...]] = {
    KIND_GRID: ("grid_to_home", "grid_to_battery"),
    KIND_SOLAR: ("solar_to_home", "solar_to_grid", "solar_to_battery"),
    KIND_BATTERY: ("battery_to_home", "battery_to_grid"),
}
# Flows of a device (its kWh and cost sensors)
KIND_FLOWS: dict[str, tuple[str, ...]] = {
    KIND_GRID: ("grid_to_home", "grid_to_battery"),
    KIND_SOLAR: ("solar_to_home", "solar_to_grid", "solar_to_battery"),
    KIND_BATTERY: ("battery_to_home", "battery_to_grid", "solar_to_battery", "grid_to_battery"),
}
# The device that sets the price of a flow
FLOW_SOURCE: dict[str, str] = {
    flow: kind for kind, flows in KIND_PRICED_FLOWS.items() for flow in flows
}

# Meter keys of every kind of device (the grid meters are single, the others lists)
KIND_METERS: dict[str, tuple[str, ...]] = {
    KIND_GRID: (CONF_GRID_IMPORT_SENSOR, CONF_GRID_EXPORT_SENSOR),
    KIND_SOLAR: (CONF_SOLAR_SENSORS,),
    KIND_BATTERY: (CONF_BATTERY_CHARGE_SENSORS, CONF_BATTERY_DISCHARGE_SENSORS),
}


@dataclass(frozen=True, slots=True)
class Unit:
    """The grid, a solar set or a battery with its meters."""

    id: str  # the sub-entry id, stays the same when the name changes
    kind: str
    name: str
    solar: tuple[str, ...] = ()
    charge: tuple[str, ...] = ()
    discharge: tuple[str, ...] = ()
    # flow key -> price source (entity id or "template:" + template)
    prices: dict[str, str] = field(default_factory=dict)


def prices_from_config(
    config: dict[str, Any], keys: tuple[str, ...] = OUTPUT_KEYS
) -> dict[str, str]:
    """Map every output key with a price to its price source.

    The source is the entity id of a price entity, or "template:" + the
    template (a template wins when both are set).
    """
    prices: dict[str, str] = {}
    for key in keys:
        if template := (config.get(price_template_option(key)) or "").strip():
            prices[key] = f"{TEMPLATE_PREFIX}{template}"
        elif entity_id := config.get(price_option(key)):
            prices[key] = entity_id
    return prices


def unit_from_subentry(subentry: ConfigSubentry) -> Unit | None:
    """Return the unit of a sub-entry (None for an unknown kind)."""
    data = subentry.data
    kind = data.get(CONF_KIND)
    if kind not in KIND_FLOWS:
        return None
    return Unit(
        id=subentry.subentry_id,
        kind=kind,
        name=subentry.title,
        solar=tuple(data.get(CONF_SOLAR_SENSORS, [])),
        charge=tuple(data.get(CONF_BATTERY_CHARGE_SENSORS, [])),
        discharge=tuple(data.get(CONF_BATTERY_DISCHARGE_SENSORS, [])),
        prices=prices_from_config(dict(data), KIND_PRICED_FLOWS[kind]),
    )


def units_from_entry(entry: ConfigEntry) -> list[Unit]:
    """Return the grid, solar sets and batteries of a config entry."""
    units = (
        unit_from_subentry(subentry)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE
    )
    return [unit for unit in units if unit is not None]


def unit_meters(units: Iterable[Unit]) -> dict[str, list[str]]:
    """Return the meters of the units per config key."""
    return {
        CONF_SOLAR_SENSORS: [e for u in units for e in u.solar],
        CONF_BATTERY_CHARGE_SENSORS: [e for u in units for e in u.charge],
        CONF_BATTERY_DISCHARGE_SENSORS: [e for u in units for e in u.discharge],
    }


def grid_subentry(entry: ConfigEntry) -> ConfigSubentry | None:
    """Return the grid sub-entry, if the grid has been added."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE and subentry.data.get(CONF_KIND) == KIND_GRID:
            return subentry
    return None


def house_subentry(entry: ConfigEntry) -> ConfigSubentry | None:
    """Return the house sub-entry (made when the integration is set up)."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE and subentry.data.get(CONF_KIND) == KIND_HOUSE:
            return subentry
    return None


def sources_from_entry(entry: ConfigEntry) -> dict[str, Any]:
    """Return all meters in the old flat format: {config key: meters}.

    The grid meters are single entity ids (absent when there is no grid yet),
    the others lists over all solar sets and batteries.
    """
    sources: dict[str, Any] = unit_meters(units_from_entry(entry))
    if (grid := grid_subentry(entry)) is not None:
        for key in (CONF_GRID_IMPORT_SENSOR, CONF_GRID_EXPORT_SENSOR):
            if grid.data.get(key):
                sources[key] = grid.data[key]
    return sources
