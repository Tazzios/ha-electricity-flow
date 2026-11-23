"""Sensors of the Electricity Flow integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ElectricityFlowConfigEntry
from .const import CONF_DIAGNOSTIC_SENSORS, DOMAIN, OUTPUT_KEYS, OUTPUT_PRECISION, TOTAL_KEY
from .cost import QuarterBooking, day_start, month_start
from .engine import ElectricityFlowEngine
from .issues import TEMPLATE_PREFIX
from .naming import cost_entity_id, energy_entity_id
from .units import KIND_FLOWS, Unit, house_subentry


@dataclass(frozen=True, kw_only=True)
class ElectricityFlowSensorDescription(SensorEntityDescription):
    """Describes an Electricity Flow sensor."""

    value_fn: Callable[[ElectricityFlowEngine], float]


def _period_sensor(
    key: str, value_fn: Callable[[ElectricityFlowEngine], float]
) -> ElectricityFlowSensorDescription:
    """A flow in kWh in the current quarter: diagnostic, for debugging.

    Without state class, so they do not create statistics.
    """
    return ElectricityFlowSensorDescription(
        key=key,
        translation_key=key,
        value_fn=value_fn,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=OUTPUT_PRECISION,
        entity_category=EntityCategory.DIAGNOSTIC,
    )


def _result_fn(key: str) -> Callable[[ElectricityFlowEngine], float]:
    return lambda engine: float(getattr(engine.result, key))


# Calculated flows of the totals (the "EF Output ..." sensors of the package)
OUTPUT_SENSORS: tuple[ElectricityFlowSensorDescription, ...] = tuple(
    _period_sensor(key, _result_fn(key)) for key in OUTPUT_KEYS
)
_OUTPUTS_BY_KEY = {description.key: description for description in OUTPUT_SENSORS}


def subentry_device(entry: ElectricityFlowConfigEntry, subentry_id: str, name: str) -> DeviceInfo:
    """Return the device of a sub-entry: the house, grid, a solar set or a battery."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
        name=name,
        entry_type=DeviceEntryType.SERVICE,
    )


def unit_device(entry: ElectricityFlowConfigEntry, unit: Unit) -> DeviceInfo:
    """Return the device of the grid, a solar set or a battery (named after the sub-entry)."""
    return subentry_device(entry, unit.id, unit.name)


def _unit_value_fn(unit_id: str, key: str) -> Callable[[ElectricityFlowEngine], float]:
    """Return a value function that reads the flows of a device."""

    def value_fn(engine: ElectricityFlowEngine) -> float:
        return engine.unit_results.get(unit_id, {}).get(key, 0.0)

    return value_fn


def unit_descriptions(unit: Unit) -> list[ElectricityFlowSensorDescription]:
    """Return the flow sensors of a device."""
    return [
        replace(_OUTPUTS_BY_KEY[key], value_fn=_unit_value_fn(unit.id, key))
        for key in KIND_FLOWS[unit.kind]
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricityFlowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of the totals and of every device.

    Sensors of this integration that are no longer made (a removed price, the
    diagnostic sensors switched off, old versions) are removed.
    """
    engine = entry.runtime_data
    diagnostic = bool(entry.options.get(CONF_DIAGNOSTIC_SENSORS, False))
    currency = currency_symbol(hass.config.currency)
    # The house (a sub-entry made at setup) has the totals
    house = house_subentry(entry)
    house_id = house.subentry_id if house is not None else ""
    device = subentry_device(entry, house_id, house.title if house is not None else "House")

    groups: list[tuple[list[SensorEntity], str | None]] = []
    sensors: list[SensorEntity] = []
    if diagnostic:
        sensors += [
            _with_entity_id(
                ElectricityFlowSensor(
                    engine, f"{entry.entry_id}_{house_id}_{description.key}", device, description
                ),
                energy_entity_id(description.key),
            )
            for description in OUTPUT_SENSORS
        ]
    sensors += _cost_sensors(
        engine,
        entry,
        None,
        device,
        currency,
        sorted_keys(engine.main_cost_keys),
        None,
        id_part=house_id,
    )
    groups.append((sensors, house_id or None))

    # The grid, every solar set and every battery is a sub-entry with its own device
    for unit in engine.units:
        unit_dev = unit_device(entry, unit)
        unit_sensors: list[SensorEntity] = []
        if diagnostic:
            unit_sensors += [
                _with_entity_id(
                    ElectricityFlowSensor(
                        engine,
                        f"{entry.entry_id}_{unit.id}_{description.key}",
                        unit_dev,
                        description,
                    ),
                    energy_entity_id(description.key, unit.name),
                )
                for description in unit_descriptions(unit)
            ]
        keys = sorted_keys(engine.unit_cost_keys(unit))
        if engine.unit_has_total(unit):
            keys.append(TOTAL_KEY)
        unit_sensors += _cost_sensors(engine, entry, unit.id, unit_dev, currency, keys, unit.name)
        groups.append((unit_sensors, unit.id))

    # Remove the sensors that are not made any more
    wanted = {sensor.unique_id for group, _ in groups for sensor in group}
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "sensor" and reg_entry.unique_id not in wanted:
            registry.async_remove(reg_entry.entity_id)
    # The main device of older versions is replaced by the house
    devices = dr.async_get(hass)
    if (old := devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})) is not None:
        devices.async_remove_device(old.id)

    for group, subentry_id in groups:
        if subentry_id is None:
            async_add_entities(group)
        else:
            async_add_entities(group, config_subentry_id=subentry_id)


def sorted_keys(keys: Iterable[str]) -> list[str]:
    """Return flow keys in the order of the outputs."""
    return [key for key in OUTPUT_KEYS if key in set(keys)]


def _with_entity_id[T: SensorEntity](sensor: T, entity_id: str) -> T:
    """Ask for an entity id in the style of the package (used when first added)."""
    sensor.entity_id = entity_id
    return sensor


def _cost_sensors(
    engine: ElectricityFlowEngine,
    entry: ElectricityFlowConfigEntry,
    unit_id: str | None,
    device: DeviceInfo,
    currency: str,
    keys: Iterable[str],
    device_name: str | None,
    *,
    id_part: str | None = None,
) -> list[SensorEntity]:
    """Return the day and month cost sensors of the given flows (or total)."""
    return [
        _with_entity_id(
            ElectricityFlowCostSensor(
                engine, entry, key, period, currency, device, unit_id, id_part=id_part
            ),
            cost_entity_id(key, period, device_name),
        )
        for key in keys
        for period in (PERIOD_DAY, PERIOD_MONTH)
    ]


class ElectricityFlowSensor(SensorEntity):
    """Energy in the current 15 minute period."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: ElectricityFlowSensorDescription

    def __init__(
        self,
        engine: ElectricityFlowEngine,
        unique_id: str,
        device: DeviceInfo,
        description: ElectricityFlowSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        self._engine = engine
        self.entity_description = description
        self._attr_unique_id = unique_id
        self._attr_device_info = device

    async def async_added_to_hass(self) -> None:
        """Register for updates of the engine."""
        self.async_on_remove(self._engine.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        """Return the energy of the current period."""
        # Rounded: summed meter deltas carry float noise (0.700000000000728)
        return round(self.entity_description.value_fn(self._engine), OUTPUT_PRECISION) + 0.0


PERIOD_DAY = "day"
PERIOD_MONTH = "month"

_PERIOD_START: dict[str, Callable[[datetime], datetime]] = {
    PERIOD_DAY: day_start,
    PERIOD_MONTH: month_start,
}

# Internal precision of the totals (the frontend shows 2 decimals)
COST_PRECISION = 5


class ElectricityFlowCostSensor(RestoreSensor):
    """Cost (kWh x price per quarter) of one flow, summed per day or month."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    # Change every quarter: keep them out of the recorder database
    _unrecorded_attributes = frozenset(
        {"last_quarter_start", "last_quarter_kwh", "last_quarter_price", "last_quarter_cost"}
    )

    def __init__(
        self,
        engine: ElectricityFlowEngine,
        entry: ElectricityFlowConfigEntry,
        output_key: str,
        period: str,
        currency: str,
        device: DeviceInfo,
        unit_id: str | None = None,
        *,
        id_part: str | None = None,
    ) -> None:
        """Initialize the cost sensor (of a device when unit_id is set).

        output_key is a flow, or TOTAL_KEY for the total of a device.
        """
        self._engine = engine
        self._output_key = output_key
        self._unit_id = unit_id
        self._period = period
        self._period_start_fn = _PERIOD_START[period]
        self._attr_translation_key = f"{output_key}_cost_{period}"
        self._attr_translation_placeholders = {"currency": currency}
        # The sub-entry in the unique id: the device, or the house for the totals
        part = unit_id or id_part
        unit_part = f"{part}_" if part else ""
        self._attr_unique_id = f"{entry.entry_id}_{unit_part}{output_key}_cost_{period}"
        self._attr_device_info = device
        self._total = 0.0
        self._last_reset = self._period_start_fn(dt_util.now())
        self._last_quarter: dict[str, object] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the total and listen for finished quarters."""
        self._attr_native_unit_of_measurement = self.hass.config.currency
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is not None and last_data is not None:
            restored_reset = dt_util.parse_datetime(str(last_state.attributes.get("last_reset")))
            if (
                restored_reset is not None
                and dt_util.as_local(restored_reset) == self._last_reset
                and isinstance(last_data.native_value, (int, float))
            ):
                # Same day/month: continue counting. Otherwise start at 0.
                self._total = float(last_data.native_value)
        self.async_on_remove(self._engine.async_add_booking_listener(self._handle_booking))

    @callback
    def _handle_booking(self, booking: QuarterBooking) -> None:
        """Add the cost of the finished quarter and roll over to a new day/month."""
        quarter = self._quarter(booking)
        if quarter is not None:
            cost, details = quarter
            booking_period = self._period_start_fn(dt_util.as_local(booking.quarter_start))
            if booking_period > self._last_reset:
                self._start_period(booking_period)
            if cost is not None and booking_period == self._last_reset:
                self._total += cost
            self._last_quarter = {
                "last_quarter_start": booking.quarter_start.isoformat(),
                **details,
                "last_quarter_cost": cost,
            }
            self.async_write_ha_state()
        # The quarter that ends at midnight belongs to the old day: its final
        # total was written above, only now start the new day/month.
        current = self._period_start_fn(dt_util.now())
        if current > self._last_reset:
            self._start_period(current)
            self.async_write_ha_state()

    def _quarter(self, booking: QuarterBooking) -> tuple[float | None, dict[str, object]] | None:
        """Return the cost of this sensor in a finished quarter, with details.

        None when the quarter has nothing for this sensor.
        """
        if self._output_key == TOTAL_KEY:
            if self._unit_id is None or self._unit_id not in booking.unit_totals:
                return None
            return booking.unit_totals[self._unit_id], {}
        costs = (
            booking.costs if self._unit_id is None else booking.unit_costs.get(self._unit_id, {})
        )
        if (quarter_cost := costs.get(self._output_key)) is None:
            return None
        return quarter_cost.cost, {
            "last_quarter_kwh": quarter_cost.kwh,
            "last_quarter_price": quarter_cost.price,
        }

    @callback
    def _start_period(self, start: datetime) -> None:
        self._total = 0.0
        self._last_reset = start

    @property
    def native_value(self) -> float:
        """Return the total cost of the current day or month."""
        return round(self._total, COST_PRECISION) + 0.0

    @property
    def last_reset(self) -> datetime:
        """Return the start of the current day or month."""
        return self._last_reset

    def _price_source(self) -> str:
        if self._unit_id is None:
            return ""
        for unit in self._engine.units:
            if unit.id == self._unit_id:
                return unit.prices.get(self._output_key, "")
        return ""

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the price entity and the details of the last quarter."""
        return {
            **_price_source_attributes(self._price_source()),
            **self._last_quarter,
        }


def _price_source_attributes(source: str) -> dict[str, str]:
    """Return the price entity or the price template as attribute."""
    if not source:
        # A total of devices with each their own price, or charging a battery
        return {}
    if source.startswith(TEMPLATE_PREFIX):
        return {"price_template": source.removeprefix(TEMPLATE_PREFIX)}
    return {"price_entity": source}


_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "INR": "₹"}


def currency_symbol(currency: str) -> str:
    """Return the sign of a currency code (or the code itself)."""
    return _CURRENCY_SYMBOLS.get(currency, currency)
