"""Test the repair issues."""

from datetime import datetime

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.electricity_flow.const import DOMAIN
from custom_components.electricity_flow.cost import price_currency
from custom_components.electricity_flow.issues import describe_source

KWH = {"unit_of_measurement": "kWh"}
PRICE = {"unit_of_measurement": "EUR/kWh"}
CONFIG = {
    "grid_import_sensor": "sensor.p1_import",
    "grid_export_sensor": "sensor.p1_export",
    "solar_sensors": ["sensor.solar"],
    "battery_charge_sensors": [],
    "battery_discharge_sensors": [],
}


def _local(*args) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


def _sources(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.p1_import", 100, KWH)
    hass.states.async_set("sensor.p1_export", 50, KWH)
    hass.states.async_set("sensor.solar", 3, KWH)


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _goto(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: datetime) -> None:
    freezer.move_to(dt_util.as_utc(moment))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


def test_price_currency() -> None:
    assert price_currency("EUR/kWh") == "EUR"
    assert price_currency("€/kWh") == "EUR"
    assert price_currency("sek/MWh") == "SEK"
    assert price_currency("ct/kWh") is None
    assert price_currency("cents/kWh") is None
    assert price_currency(None) is None
    assert price_currency("EUR") is None


def test_describe_source() -> None:
    assert describe_source("sensor.price") == "sensor.price"
    assert describe_source("template:{{ 1 +\n 2 }}") == "template {{ 1 + 2 }}"
    assert len(describe_source("template:" + "x" * 200)) == len("template ") + 60


async def test_no_issues_when_all_is_fine(hass: HomeAssistant) -> None:
    _sources(hass)
    hass.states.async_set("sensor.price", 0.25, PRICE)
    await _setup(hass, {**CONFIG, "price_grid_to_home": "sensor.price"})
    assert _issues(hass) == {}


async def test_duplicate_source(hass: HomeAssistant) -> None:
    _sources(hass)
    entry = await _setup(hass, {**CONFIG, "battery_discharge_sensors": ["sensor.solar"]})
    issue = _issues(hass)["duplicate_source_sensor.solar"]
    assert issue.translation_key == "duplicate_source"
    assert issue.translation_placeholders == {
        "entity": "sensor.solar",
        "roles": "solar, battery_discharge",
    }

    # Fixed in the options: the issue is gone after the reload
    hass.config_entries.async_update_entry(entry, options=CONFIG)
    await hass.async_block_till_done()
    assert _issues(hass) == {}


async def test_source_invalid_unit(hass: HomeAssistant) -> None:
    _sources(hass)
    hass.states.async_set("sensor.solar", 300, {"unit_of_measurement": "W"})
    await _setup(hass, CONFIG)
    issue = _issues(hass)["source_invalid_unit_sensor.solar"]
    assert issue.translation_placeholders == {"entity": "sensor.solar", "unit": "W"}

    hass.states.async_set("sensor.solar", 3, KWH)
    await hass.async_block_till_done()
    assert _issues(hass) == {}


async def test_source_unavailable_after_an_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 10, 1, 0)))
    _sources(hass)
    entry = await _setup(hass, CONFIG)
    hass.states.async_set("sensor.solar", "unavailable", KWH)
    await hass.async_block_till_done()

    # Short outages are normal
    await _goto(hass, freezer, _local(2026, 11, 22, 10, 45, 0))
    assert _issues(hass) == {}

    await _goto(hass, freezer, _local(2026, 11, 22, 11, 15, 0))
    issue = _issues(hass)["source_unavailable_sensor.solar"]
    assert issue.translation_placeholders == {"entity": "sensor.solar"}

    # Back: the issue clears at once
    hass.states.async_set("sensor.solar", 3.2, KWH)
    await hass.async_block_till_done()
    assert _issues(hass) == {}

    # A removed sensor counts too, and unloading clears the issues
    hass.states.async_remove("sensor.solar")
    await hass.async_block_till_done()
    await _goto(hass, freezer, _local(2026, 11, 22, 12, 30, 0))
    assert "source_unavailable_sensor.solar" in _issues(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issues(hass) == {}


async def test_price_issues(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(dt_util.as_utc(_local(2026, 11, 22, 10, 1, 0)))
    _sources(hass)
    hass.states.async_set("sensor.price", 0.25, {"unit_of_measurement": "EUR"})
    hass.states.async_set("sensor.feed_in", 0.08, {"unit_of_measurement": "SEK/kWh"})
    hass.states.async_set("sensor.cents", 25, {"unit_of_measurement": "ct/kWh"})
    await _setup(
        hass,
        {
            **CONFIG,
            "price_grid_to_home": "sensor.price",
            "price_solar_to_grid": "sensor.feed_in",
            "price_solar_to_home": "sensor.cents",
            "price_template_house": "{{ states('sensor.missing') | float(none) }}",
        },
    )
    issues = _issues(hass)
    assert issues["price_invalid_unit_sensor.price"].translation_placeholders == {
        "entity": "sensor.price",
        "unit": "EUR",
    }
    assert issues["price_currency_sensor.feed_in"].translation_placeholders == {
        "entity": "sensor.feed_in",
        "currency": "SEK",
        "expected": "EUR",
    }
    # Cents can be any currency, a wrong unit is not also "unavailable"
    assert set(issues) == {"price_invalid_unit_sensor.price", "price_currency_sensor.feed_in"}

    # After an hour the template without a number is reported
    await _goto(hass, freezer, _local(2026, 11, 22, 11, 15, 0))
    unavailable = [i for i in _issues(hass).values() if i.translation_key == "price_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].translation_placeholders == {
        "entity": "template {{ states('sensor.missing') | float(none) }}"
    }

    # Fixing the units clears those issues
    hass.states.async_set("sensor.price", 0.25, PRICE)
    hass.states.async_set("sensor.feed_in", 0.08, PRICE)
    hass.states.async_set("sensor.missing", 0.3)
    await hass.async_block_till_done()
    assert _issues(hass) == {}
