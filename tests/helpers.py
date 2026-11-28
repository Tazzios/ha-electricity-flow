"""Helpers for the tests: config entries in the current format (devices)."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentryData

from custom_components.electricity_flow.const import DOMAIN
from custom_components.electricity_flow.units import KIND_PRICED_FLOWS


def _prices(config: dict[str, Any], kind: str) -> dict[str, Any]:
    """Return the price options of config that belong on a device of kind."""
    return {
        key: value
        for key, value in config.items()
        if key.startswith("price_")
        and key.removeprefix("price_template_").removeprefix("price_") in KIND_PRICED_FLOWS[kind]
    }


def device(kind: str, title: str, data: dict[str, Any]) -> ConfigSubentryData:
    """Return the data of a device (sub-entry)."""
    return ConfigSubentryData(
        data={"kind": kind, **data}, subentry_type="device", title=title, unique_id=None
    )


def make_entry(config: dict[str, Any], *, diagnostic: bool = True) -> MockConfigEntry:
    """Return an entry with the meters and prices of config (in the flat format).

    The grid meters become the device "Grid", all solar meters the solar set
    "Solar" and all battery meters the battery "Battery", each with the prices
    of the flows that come from it.
    """
    devices: list[ConfigSubentryData] = []
    if config.get("grid_import_sensor"):
        devices.append(
            device(
                "grid",
                "Grid",
                {
                    "grid_import_sensor": config["grid_import_sensor"],
                    "grid_export_sensor": config["grid_export_sensor"],
                    **_prices(config, "grid"),
                },
            )
        )
    if config.get("solar_sensors"):
        devices.append(
            device(
                "solar",
                "Solar",
                {"solar_sensors": config["solar_sensors"], **_prices(config, "solar")},
            )
        )
    if config.get("battery_charge_sensors") or config.get("battery_discharge_sensors"):
        devices.append(
            device(
                "battery",
                "Battery",
                {
                    "battery_charge_sensors": config.get("battery_charge_sensors", []),
                    "battery_discharge_sensors": config.get("battery_discharge_sensors", []),
                    **_prices(config, "battery"),
                },
            )
        )
    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={},
        options={"diagnostic_sensors": diagnostic},
        subentries_data=devices,
    )
