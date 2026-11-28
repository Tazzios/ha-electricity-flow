"""Config flow for the Electricity Flow integration.

Like ha-power-insight: the grid, every solar set and every battery are added
one at a time with "Add device" on the integration page (config sub-entries)
and changed or removed with their own menu there. The integration itself only
has the choice for the diagnostic sensors (also with Configure).

Every flow has an optional price at its source (the device it comes from): a
price entity or a template (currency per kWh).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.template import Template

from .const import (
    CONF_BATTERY_CHARGE_SENSORS,
    CONF_BATTERY_DISCHARGE_SENSORS,
    CONF_DIAGNOSTIC_SENSORS,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    DOMAIN,
    price_option,
    price_template_option,
)
from .cost import price_factor
from .engine import UNIT_TO_KWH
from .units import (
    CONF_KIND,
    KIND_BATTERY,
    KIND_GRID,
    KIND_HOUSE,
    KIND_METERS,
    KIND_PRICED_FLOWS,
    KIND_SOLAR,
    SUBENTRY_TYPE,
    grid_subentry,
)

PRICE_DOMAINS = ("sensor", "input_number", "number")


def _matching_entities(
    hass: HomeAssistant, domains: tuple[str, ...], unit_ok: Any, current: Any
) -> list[str]:
    """Return the entities whose unit passes unit_ok, plus the current choice."""
    registry = er.async_get(hass)
    found = [
        state.entity_id
        for domain in domains
        for state in hass.states.async_all(domain)
        if unit_ok(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        # Not our own sensors: they are results, not meters or prices
        and not (
            (reg := registry.async_get(state.entity_id)) is not None and reg.platform == DOMAIN
        )
    ]
    for value in [current] if isinstance(current, str) else current or []:
        if value and value not in found:
            found.append(value)
    return sorted(found)


def _is_energy_unit(unit: Any) -> bool:
    return unit in UNIT_TO_KWH


def _is_price_unit(unit: Any) -> bool:
    return isinstance(unit, str) and price_factor(unit) is not None


def meter_selector(hass: HomeAssistant, current: Any, multiple: bool) -> selector.EntitySelector:
    """Only energy meters (Wh, kWh or MWh) can be picked."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            include_entities=_matching_entities(hass, ("sensor",), _is_energy_unit, current),
            multiple=multiple,
        )
    )


def price_selector(hass: HomeAssistant, current: Any) -> selector.EntitySelector:
    """Only prices per energy (EUR/kWh, ct/kWh, EUR/MWh, ...) can be picked."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            include_entities=_matching_entities(hass, PRICE_DOMAINS, _is_price_unit, current)
        )
    )


PRICE_TEMPLATE = selector.TemplateSelector()


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
    if CONF_GRID_IMPORT_SENSOR in sources and sources.get(CONF_GRID_IMPORT_SENSOR) == sources.get(
        CONF_GRID_EXPORT_SENSOR
    ):
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
        if (
            entity_id
            and (state := hass.states.get(entity_id)) is not None
            and price_factor(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)) is None
        ):
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


def _price_fields(
    hass: HomeAssistant, outputs: tuple[str, ...], defaults: dict[str, Any]
) -> dict[Any, Any]:
    """A price entity and a price template for every flow (all optional)."""
    fields: dict[Any, Any] = {}
    for output in outputs:
        for option, field_selector in (
            (price_option(output), price_selector(hass, defaults.get(price_option(output)))),
            (price_template_option(output), PRICE_TEMPLATE),
        ):
            fields[vol.Optional(option, description={"suggested_value": defaults.get(option)})] = (
                field_selector
            )
    return fields


def _clean_prices(outputs: tuple[str, ...], values: dict[str, Any]) -> dict[str, Any]:
    """Return only the filled in prices (an empty field means: no costs)."""
    prices: dict[str, Any] = {}
    for output in outputs:
        for option in (price_option(output), price_template_option(output)):
            value = values.get(option)
            if isinstance(value, str):
                value = value.strip()
            if value:
                prices[option] = value
    return prices


# Schemas are typed Any: newer HA expects probatio schemas, older HA voluptuous.
def _settings_schema(defaults: dict[str, Any]) -> Any:
    """The settings of the integration: only the diagnostic sensors."""
    return vol.Schema(
        {
            vol.Required(
                CONF_DIAGNOSTIC_SENSORS,
                default=bool(defaults.get(CONF_DIAGNOSTIC_SENSORS, False)),
            ): selector.BooleanSelector()
        }
    )


class ElectricityFlowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Electricity Flow, the devices are added afterwards."""

    VERSION = 3
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Only the diagnostic sensors, the rest goes per device."""
        if user_input is not None:
            return self.async_create_entry(
                title="Electricity Flow",
                data={},
                options={CONF_DIAGNOSTIC_SENSORS: bool(user_input[CONF_DIAGNOSTIC_SENSORS])},
            )
        return self.async_show_form(step_id="user", data_schema=_settings_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ElectricityFlowOptionsFlow:
        """Return the options flow."""
        return ElectricityFlowOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """The grid, solar sets and batteries are added as devices (sub-entries)."""
        return {SUBENTRY_TYPE: DeviceSubentryFlow}


class ElectricityFlowOptionsFlow(OptionsFlow):
    """Switch the diagnostic sensors on or off."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The kWh per quarter sensors, for debugging."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_DIAGNOSTIC_SENSORS: bool(user_input[CONF_DIAGNOSTIC_SENSORS])}
            )
        return self.async_show_form(
            step_id="init", data_schema=_settings_schema(dict(self.config_entry.options))
        )


CONF_NAME = "name"

GRID_TITLES = {"nl": "Net", "de": "Netz"}


def _device_schema(hass: HomeAssistant, kind: str, defaults: dict[str, Any]) -> Any:
    """Name (not for the grid), meters and the prices of the flows from this device."""
    fields: dict[Any, Any] = {}
    if kind == KIND_GRID:
        for key in KIND_METERS[kind]:
            fields[vol.Required(key, description={"suggested_value": defaults.get(key)})] = (
                meter_selector(hass, defaults.get(key), multiple=False)
            )
    else:
        fields[
            vol.Required(CONF_NAME, description={"suggested_value": defaults.get(CONF_NAME)})
        ] = selector.TextSelector()
    if kind in (KIND_SOLAR, KIND_BATTERY):
        # One meter per field (stored as a list of one)
        for key in KIND_METERS[kind]:
            current = _single(defaults.get(key))
            fields[vol.Optional(key, description={"suggested_value": current})] = meter_selector(
                hass, current, multiple=False
            )
    fields.update(_price_fields(hass, KIND_PRICED_FLOWS[kind], defaults))
    return vol.Schema(fields)


def _single(value: Any) -> str | None:
    """Return the one meter of a solar or battery field (stored as a list)."""
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add or change the grid, a solar set or a battery."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose what to add (the grid only once)."""
        options = [KIND_SOLAR, KIND_BATTERY]
        if grid_subentry(self._get_entry()) is None:
            options.insert(0, KIND_GRID)
        return self.async_show_menu(step_id="user", menu_options=options)

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add the grid connection."""
        if grid_subentry(self._get_entry()) is not None:
            return self.async_abort(reason="grid_already_added")
        return await self._async_device_form(KIND_GRID, user_input)

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a solar set."""
        return await self._async_device_form(KIND_SOLAR, user_input)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a battery."""
        return await self._async_device_form(KIND_BATTERY, user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the meters, prices (and name) of the grid, a solar set or a battery."""
        subentry = self._get_reconfigure_subentry()
        kind = subentry.data.get(CONF_KIND, KIND_SOLAR)
        if kind == KIND_HOUSE:
            return self.async_abort(reason="house_nothing_to_change")
        return await self._async_device_form(
            kind,
            user_input,
            step_id=f"reconfigure_{kind}",
            defaults={**subentry.data, CONF_NAME: subentry.title},
        )

    async def async_step_reconfigure_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Form of the grid when it is changed."""
        return await self.async_step_reconfigure(user_input)

    async def async_step_reconfigure_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Form of a solar set when it is changed."""
        return await self.async_step_reconfigure(user_input)

    async def async_step_reconfigure_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Form of a battery when it is changed."""
        return await self.async_step_reconfigure(user_input)

    @property
    def _reconfiguring(self) -> bool:
        return self.source == "reconfigure"

    def _validate(self, kind: str, values: dict[str, Any]) -> dict[str, str]:
        entry = self._get_entry()
        own_id = self._get_reconfigure_subentry().subentry_id if self._reconfiguring else None
        others = [sub for sub in entry.subentries.values() if sub.subentry_id != own_id]
        errors: dict[str, str] = {}
        meter_keys = KIND_METERS[kind]

        if kind == KIND_GRID:
            if values[CONF_GRID_IMPORT_SENSOR] == values[CONF_GRID_EXPORT_SENSOR]:
                errors[CONF_GRID_EXPORT_SENSOR] = "same_grid_sensor"
        else:
            name = values[CONF_NAME]
            if not name:
                errors[CONF_NAME] = "name_required"
            elif any(sub.title.casefold() == name.casefold() for sub in others):
                errors[CONF_NAME] = "name_not_unique"
            if not any(values[key] for key in meter_keys):
                errors["base"] = "no_meters"
            if kind == KIND_BATTERY and set(values[CONF_BATTERY_CHARGE_SENSORS]) & set(
                values[CONF_BATTERY_DISCHARGE_SENSORS]
            ):
                errors[CONF_BATTERY_DISCHARGE_SENSORS] = "same_battery_meter"

        # A meter belongs to one device only, otherwise it is counted twice
        in_use: set[str] = set()
        for sub in others:
            for key in KIND_METERS.get(sub.data.get(CONF_KIND, ""), ()):
                value = sub.data.get(key) or []
                in_use.update([value] if isinstance(value, str) else value)
        for key in meter_keys:
            value = values[key]
            if any(meter in in_use for meter in ([value] if isinstance(value, str) else value)):
                errors[key] = "meter_in_use"

        sources = {key: values[key] for key in meter_keys}
        return {**_validate_sources(self.hass, sources), **errors}

    async def _async_device_form(
        self,
        kind: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = user_input
            values: dict[str, Any]
            prices = _clean_prices(KIND_PRICED_FLOWS[kind], flat)
            if kind == KIND_GRID:
                values = {key: flat.get(key) or "" for key in KIND_METERS[kind]}
            else:
                values = {
                    CONF_NAME: str(flat.get(CONF_NAME) or "").strip(),
                    **{
                        key: [m] if (m := _single(flat.get(key))) else []
                        for key in KIND_METERS[kind]
                    },
                }
            errors = {
                **self._validate(kind, values),
                **_validate_prices(self.hass, KIND_PRICED_FLOWS[kind], prices),
            }
            values.update(prices)
            if not errors:
                if kind == KIND_GRID:
                    title = GRID_TITLES.get(self.hass.config.language, "Grid")
                else:
                    title = str(values.pop(CONF_NAME))
                data = {CONF_KIND: kind, **values}
                if self._reconfiguring:
                    subentry = self._get_reconfigure_subentry()
                    return self.async_update_and_abort(
                        self._get_entry(),
                        subentry,
                        title=subentry.title if kind == KIND_GRID else title,
                        data=data,
                    )
                return self.async_create_entry(title=title, data=data)
            defaults = values
        return self.async_show_form(
            step_id=step_id or kind,
            data_schema=_device_schema(self.hass, kind, defaults or {}),
            errors=errors,
        )
