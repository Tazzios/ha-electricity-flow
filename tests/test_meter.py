"""Test meter glitches, storage on unload/reload and removal of the entry."""

from datetime import datetime
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.electricity_flow.const import DOMAIN

KWH = {"unit_of_measurement": "kWh"}
CONFIG = {
    "grid_import_sensor": "sensor.p1_import",
    "grid_export_sensor": "sensor.p1_export",
    "solar_sensors": ["sensor.solar"],
}
SOLAR_INPUT = "sensor.electricity_flow_input_quarter_solar"


def _val(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def _setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> MockConfigEntry:
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 11, 21, 14, 1, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    hass.states.async_set("sensor.p1_import", 100, KWH)
    hass.states.async_set("sensor.p1_export", 50, KWH)
    hass.states.async_set("sensor.solar", 12345, KWH)
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _solar(hass: HomeAssistant, value: Any) -> None:
    hass.states.async_set("sensor.solar", value, KWH)
    await hass.async_block_till_done()


async def test_zero_glitch_is_not_a_huge_jump(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, freezer)
    await _solar(hass, 12345.5)
    assert _val(hass, SOLAR_INPUT) == pytest.approx(0.5)

    # The meter briefly reports 0 and then its real value again
    await _solar(hass, 0)
    await _solar(hass, 12345.5)
    assert _val(hass, SOLAR_INPUT) == pytest.approx(0.5)

    # Counting continues normally afterwards
    await _solar(hass, 12345.7)
    assert _val(hass, SOLAR_INPUT) == pytest.approx(0.7)


async def test_small_dip_is_not_counted_twice(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, freezer)
    await _solar(hass, 12345.5)
    await _solar(hass, 12345.499)
    await _solar(hass, 12345.5)
    assert _val(hass, SOLAR_INPUT) == pytest.approx(0.5)
    await _solar(hass, 12345.6)
    assert _val(hass, SOLAR_INPUT) == pytest.approx(0.6)


async def test_reload_continues_the_quarter(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    entry = await _setup(hass, freezer)
    await _solar(hass, 12345.5)

    assert await hass.config_entries.async_unload(entry.entry_id)
    # Written immediately on unload, not only after the save delay
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["period_energy"] == {"sensor.solar": 0.5}

    # Energy produced while the entry was unloaded is counted after the reload
    await _solar(hass, 12345.8)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _val(hass, SOLAR_INPUT) == pytest.approx(0.8)


async def test_remove_entry_removes_storage(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    entry = await _setup(hass, freezer)
    await _solar(hass, 12345.5)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage


async def test_daily_counter_with_last_reset(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    yesterday = {**KWH, "last_reset": "2026-11-20T00:00:00+01:00"}
    today = {**KWH, "last_reset": "2026-11-21T00:00:00+01:00"}
    entry = await _setup(hass, freezer)
    hass.states.async_set("sensor.solar", 1.0, yesterday)
    await hass.async_block_till_done()
    # From 12345 to 1.0 is a normal reset, counted from zero
    assert _val(hass, SOLAR_INPUT) == 1.0

    # The counter restarts and is already at 0.8 at its first new reading.
    # Without last_reset this would look like noise (more than half of 1.0).
    hass.states.async_set("sensor.solar", 0.8, today)
    await hass.async_block_till_done()
    assert _val(hass, SOLAR_INPUT) == 1.8

    # A small dip with the same last_reset is still noise
    hass.states.async_set("sensor.solar", 0.79, today)
    await hass.async_block_till_done()
    hass.states.async_set("sensor.solar", 0.9, today)
    await hass.async_block_till_done()
    assert _val(hass, SOLAR_INPUT) == 1.9

    # The last seen last_reset survives a reload
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["source_reset"]["sensor.solar"] == today["last_reset"]
