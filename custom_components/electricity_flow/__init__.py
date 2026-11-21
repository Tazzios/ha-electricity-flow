"""The Electricity Flow integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .engine import ElectricityFlowEngine, create_store

PLATFORMS: list[Platform] = [Platform.SENSOR]

type ElectricityFlowConfigEntry = ConfigEntry[ElectricityFlowEngine]


async def async_setup_entry(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> bool:
    """Set up Electricity Flow from a config entry."""
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
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> None:
    """Remove the stored period state when the integration is removed."""
    await create_store(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: ElectricityFlowConfigEntry) -> None:
    """Reload when the options (source sensors) are changed."""
    await hass.config_entries.async_reload(entry.entry_id)
