"""The Electricity Flow integration."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_CHARGE_SENSORS,
    CONF_BATTERY_DISCHARGE_SENSORS,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_PRICE_PREFIX,
    CONF_SOLAR_SENSORS,
)
from .engine import ElectricityFlowEngine, create_store
from .issues import async_delete_all_issues
from .units import (
    CONF_KIND,
    HOUSE_TITLES,
    KIND_BATTERY,
    KIND_GRID,
    KIND_HOUSE,
    KIND_SOLAR,
    SUBENTRY_TYPE,
    house_subentry,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type ElectricityFlowConfigEntry = ConfigEntry[ElectricityFlowEngine]


async def async_setup_entry(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> bool:
    """Set up Electricity Flow from a config entry."""
    if house_subentry(entry) is None:
        # The house is an item like the grid and the devices (also when removed)
        title = HOUSE_TITLES.get(hass.config.language, "House")
        hass.config_entries.async_add_subentry(entry, _subentry(title, {CONF_KIND: KIND_HOUSE}))
    engine = ElectricityFlowEngine(hass, entry)
    await engine.async_start()
    entry.runtime_data = engine

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Write the current period now, so a reload continues from it
        await entry.runtime_data.async_save()
        # The issues are checked again when the integration starts
        entry.runtime_data.issues.async_clear_all()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> None:
    """Remove the stored period state when the integration is removed."""
    await create_store(hass, entry.entry_id).async_remove()
    async_delete_all_issues(hass)


async def _async_update_listener(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> None:
    """Reload when the options (source sensors) are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


def _meter_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return a readable name of a meter for a migrated solar set."""
    if (state := hass.states.get(entity_id)) is not None and state.name:
        return str(state.name)
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and (name := entry.name or entry.original_name):
        return name
    return entity_id.split(".", 1)[-1].replace("_", " ").capitalize()


def _subentry(title: str, data: dict[str, Any]) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type=SUBENTRY_TYPE,
        title=title,
        unique_id=None,
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> bool:
    """Bring an entry of an older version to the current one.

    Version 1 had all meters in the entry: they move into devices
    (sub-entries). Version 2 had prices on the House page and the battery:
    now every price is set at the source of the flow, so all old prices are
    removed and are entered again on the devices. Sensors that are no longer
    made are removed when the sensors are set up.
    """
    if entry.version > 3:
        return False
    if entry.version == 1:
        _migrate_to_devices(hass, entry)
    if entry.version == 2:
        for subentry in entry.subentries.values():
            data = {k: v for k, v in subentry.data.items() if not k.startswith(CONF_PRICE_PREFIX)}
            if data != dict(subentry.data):
                hass.config_entries.async_update_subentry(entry, subentry, data=data)
        hass.config_entries.async_update_entry(entry, options={}, version=3, minor_version=1)
        _LOGGER.info("Removed the old prices of Electricity Flow, set them on the devices")
    return True


def _migrate_to_devices(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> None:
    """Move the meters of version 1 into devices (sub-entries), without prices."""
    config: dict[str, Any] = {**entry.data, **entry.options}

    dutch = hass.config.language == "nl"
    if config.get(CONF_GRID_IMPORT_SENSOR) and config.get(CONF_GRID_EXPORT_SENSOR):
        hass.config_entries.async_add_subentry(
            entry,
            _subentry(
                "Net" if dutch else "Grid",
                {
                    CONF_KIND: KIND_GRID,
                    CONF_GRID_IMPORT_SENSOR: config[CONF_GRID_IMPORT_SENSOR],
                    CONF_GRID_EXPORT_SENSOR: config[CONF_GRID_EXPORT_SENSOR],
                },
            ),
        )
    # Meters already in a device (added with a test version of 0.9.0) stay there
    used = {
        meter
        for sub in entry.subentries.values()
        for key in (CONF_SOLAR_SENSORS, CONF_BATTERY_CHARGE_SENSORS, CONF_BATTERY_DISCHARGE_SENSORS)
        for meter in sub.data.get(key) or []
    }
    titles: set[str] = {sub.title.casefold() for sub in entry.subentries.values()}
    for meter in config.get(CONF_SOLAR_SENSORS) or []:
        if meter in used:
            continue
        title = base = _meter_name(hass, meter)
        number = 2
        while title.casefold() in titles:
            title, number = f"{base} {number}", number + 1
        titles.add(title.casefold())
        hass.config_entries.async_add_subentry(
            entry,
            _subentry(
                title,
                {CONF_KIND: KIND_SOLAR, CONF_SOLAR_SENSORS: [meter]},
            ),
        )
    charge = [m for m in config.get(CONF_BATTERY_CHARGE_SENSORS) or [] if m not in used]
    discharge = [m for m in config.get(CONF_BATTERY_DISCHARGE_SENSORS) or [] if m not in used]
    if charge or discharge:
        hass.config_entries.async_add_subentry(
            entry,
            _subentry(
                "Batterij" if dutch else "Battery",
                {
                    CONF_KIND: KIND_BATTERY,
                    CONF_BATTERY_CHARGE_SENSORS: charge,
                    CONF_BATTERY_DISCHARGE_SENSORS: discharge,
                },
            ),
        )
    hass.config_entries.async_update_entry(entry, data={}, options={}, version=2, minor_version=1)
    _LOGGER.info("Moved the meters of Electricity Flow into devices")
