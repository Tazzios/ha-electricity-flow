"""Config flow for the Electricity Flow integration.

Setup and options use the same four pages: Grid, Solar, Battery (with the
groups Charge and Discharge) and House. Every page has the energy meters of
that part and, per output flow, an optional price: a price entity or a
template (currency per kWh).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import selector
from homeassistant.helpers.template import Template

from .const import (
    CONF_BATTERY_CHARGE_SENSORS,
    CONF_BATTERY_DISCHARGE_SENSORS,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_SOLAR_SENSORS,
    DOMAIN,
    PAGE_BATTERY,
    PAGE_GRID,
    PAGE_HOUSE,
    PAGE_SOLAR,
    price_option,
    price_template_option,
)
from .cost import price_factor
from .engine import UNIT_TO_KWH

MULTI_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", multiple=True)
)
SINGLE_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
PRICE_ENTITY = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"])
)
PRICE_TEMPLATE = selector.TemplateSelector()


@dataclass(frozen=True)
class Group:
    """Part of a page: energy meters and the output flows priced there.

    A group with a name is shown as a section (a collapsible block).
    sources: (key, required); required meters are single, others multiple.
    """

    name: str | None
    sources: tuple[tuple[str, bool], ...]
    outputs: tuple[str, ...]


PAGES: dict[str, tuple[Group, ...]] = {
    PAGE_GRID: (
        Group(
            None,
            ((CONF_GRID_IMPORT_SENSOR, True), (CONF_GRID_EXPORT_SENSOR, True)),
            (),
        ),
    ),
    PAGE_SOLAR: (
        Group(
            None,
            ((CONF_SOLAR_SENSORS, False),),
            ("solar_to_home", "solar_to_grid"),
        ),
    ),
    PAGE_BATTERY: (
        Group(
            "charge",
            ((CONF_BATTERY_CHARGE_SENSORS, False),),
            ("solar_to_battery", "grid_to_battery"),
        ),
        Group(
            "discharge",
            ((CONF_BATTERY_DISCHARGE_SENSORS, False),),
            ("battery_to_home", "battery_to_grid"),
        ),
    ),
    PAGE_HOUSE: (Group(None, (), ("house", "grid_to_home")),),
}
PAGE_ORDER = (PAGE_GRID, PAGE_SOLAR, PAGE_BATTERY, PAGE_HOUSE)


def _group_fields(group: Group, defaults: dict[str, Any]) -> dict[Any, Any]:
    fields: dict[Any, Any] = {}
    for key, required in group.sources:
        if required:
            fields[vol.Required(key, description={"suggested_value": defaults.get(key)})] = (
                SINGLE_SENSOR
            )
        else:
            fields[vol.Optional(key, default=defaults.get(key, []))] = MULTI_SENSOR
    for output in group.outputs:
        for option, field_selector in (
            (price_option(output), PRICE_ENTITY),
            (price_template_option(output), PRICE_TEMPLATE),
        ):
            fields[
                vol.Optional(option, description={"suggested_value": defaults.get(option)})
            ] = field_selector
    return fields


def _page_schema(page: str, defaults: dict[str, Any]) -> vol.Schema:
    """Energy meters, then entity + template per output flow, per group."""
    fields: dict[Any, Any] = {}
    for group in PAGES[page]:
        group_fields = _group_fields(group, defaults)
        if group.name is None:
            fields.update(group_fields)
        else:
            fields[vol.Required(group.name)] = section(
                vol.Schema(group_fields), {"collapsed": False}
            )
    return vol.Schema(fields)


def _flatten(page: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Merge the values of the sections into one dict."""
    flat: dict[str, Any] = {}
    for group in PAGES[page]:
        if group.name is None:
            keys = {k for k, _ in group.sources} | {
                option
                for output in group.outputs
                for option in (price_option(output), price_template_option(output))
            }
            flat.update({k: v for k, v in user_input.items() if k in keys})
        else:
            flat.update(user_input.get(group.name) or {})
    return flat


def _validate_sources(hass: HomeAssistant, sources: dict[str, Any]) -> dict[str, str]:
    """Check that all selected sensors are energy meters (Wh, kWh or MWh)."""
    errors: dict[str, str] = {}
    for key, value in sources.items():
        entity_ids = value if isinstance(value, list) else [value]
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state is None:
                # Sensor may not be loaded yet; accept it
                continue
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            if unit is not None and unit not in UNIT_TO_KWH:
                errors[key] = "invalid_unit"
    if CONF_GRID_IMPORT_SENSOR in sources and sources.get(
        CONF_GRID_IMPORT_SENSOR
    ) == sources.get(CONF_GRID_EXPORT_SENSOR):
        errors[CONF_GRID_EXPORT_SENSOR] = "same_grid_sensor"
    return errors


def _validate_prices(
    hass: HomeAssistant, outputs: tuple[str, ...], prices: dict[str, str]
) -> dict[str, str]:
    """Check the price entities (price per energy) and templates (a number)."""
    errors: dict[str, str] = {}
    for output in outputs:
        entity_id = prices.get(price_option(output))
        template = prices.get(price_template_option(output))
        if entity_id and template:
            errors[price_template_option(output)] = "price_and_template"
            continue
        if entity_id and (state := hass.states.get(entity_id)) is not None:
            if price_factor(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)) is None:
                errors[price_option(output)] = "invalid_price_unit"
        if template:
            try:
                result = Template(template, hass).async_render(parse_result=False)
            except TemplateError:
                errors[price_template_option(output)] = "invalid_template"
                continue
            try:
                float(result)
            except ValueError:
                errors[price_template_option(output)] = "template_not_number"
    return errors


def _defaults_for_form(page: str, values: dict[str, Any]) -> dict[str, Any]:
    """Accept both flat (stored) values and section (submitted) values."""
    flat = dict(values)
    for group in PAGES[page]:
        if group.name is not None and isinstance(values.get(group.name), dict):
            flat.update(values[group.name])
    return flat


class _PagesMixin:
    """The pages shared by setup and options."""

    hass: HomeAssistant
    _data: dict[str, Any]
    _defaults: dict[str, Any]

    async def _async_page(
        self, page: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            values = _flatten(page, user_input)
            source_keys = {k for group in PAGES[page] for k, _ in group.sources}
            outputs = tuple(o for group in PAGES[page] for o in group.outputs)
            sources = {k: v for k, v in values.items() if k in source_keys}
            # Keep only filled in prices (an empty field means: no costs)
            prices = {
                k: v.strip() if isinstance(v, str) else v
                for k, v in values.items()
                if k not in source_keys and v and (not isinstance(v, str) or v.strip())
            }
            errors = {
                **_validate_sources(self.hass, sources),
                **_validate_prices(self.hass, outputs, prices),
            }
            if not errors:
                self._data.update(sources)
                self._data.update(prices)
                next_index = PAGE_ORDER.index(page) + 1
                if next_index < len(PAGE_ORDER):
                    return await getattr(self, f"async_step_{PAGE_ORDER[next_index]}")()
                return self._async_finish()
        defaults = _defaults_for_form(
            page, user_input if user_input is not None else self._defaults
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=page,
            data_schema=_page_schema(page, defaults),
            errors=errors,
            last_step=page == PAGE_ORDER[-1],
        )

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Grid meters."""
        return await self._async_page(PAGE_GRID, user_input)

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Solar meters and prices of the solar flows."""
        return await self._async_page(PAGE_SOLAR, user_input)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Battery meters and prices, grouped in charge and discharge."""
        return await self._async_page(PAGE_BATTERY, user_input)

    async def async_step_house(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prices of the house consumption and of grid to home."""
        return await self._async_page(PAGE_HOUSE, user_input)

    def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError


class ElectricityFlowConfigFlow(_PagesMixin, ConfigFlow, domain=DOMAIN):
    """Handle the setup of Electricity Flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data = {}
        self._defaults = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start with the Grid page."""
        return await self.async_step_grid()

    def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="Electricity Flow", data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> ElectricityFlowOptionsFlow:
        """Return the options flow."""
        return ElectricityFlowOptionsFlow()


class ElectricityFlowOptionsFlow(_PagesMixin, OptionsFlow):
    """Change meters and prices afterwards, on the same pages."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._data = {}

    @property
    def _defaults(self) -> dict[str, Any]:  # type: ignore[override]
        return dict(self.config_entry.options or self.config_entry.data)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start with the Grid page."""
        return await self.async_step_grid()

    def _async_finish(self) -> ConfigFlowResult:
        # The options are replaced completely, so cleared prices disappear
        return self.async_create_entry(data=self._data)
