"""Sensors of the Electricity Flow integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, restore_state
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ElectricityFlowConfigEntry
from .const import DOMAIN, OUTPUT_PRECISION
from .cost import QuarterBooking, day_start, month_start
from .engine import TEMPLATE_PREFIX, ElectricityFlowEngine


@dataclass(frozen=True, kw_only=True)
class ElectricityFlowSensorDescription(SensorEntityDescription):
    """Describes an Electricity Flow sensor."""

    value_fn: Callable[[ElectricityFlowEngine], float]
    # Entity id used by the old package, so history and other configuration
    # (like dynamic_energy_cost) keep working after the switch.
    object_id: str


def _period_sensor(
    key: str,
    object_id: str,
    value_fn: Callable[[ElectricityFlowEngine], float],
    *,
    diagnostic: bool = False,
) -> ElectricityFlowSensorDescription:
    return ElectricityFlowSensorDescription(
        key=key,
        translation_key=key,
        object_id=object_id,
        value_fn=value_fn,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=None if diagnostic else SensorStateClass.TOTAL,
        suggested_display_precision=OUTPUT_PRECISION,
        entity_category=EntityCategory.DIAGNOSTIC if diagnostic else None,
    )


# Calculated flows (the "EF Output ..." sensors of the package)
OUTPUT_SENSORS: tuple[ElectricityFlowSensorDescription, ...] = (
    _period_sensor("house", "ef_output_house_kwh", lambda e: e.result.house),
    _period_sensor("solar_to_home", "ef_output_solar_home_kwh", lambda e: e.result.solar_to_home),
    _period_sensor(
        "solar_to_battery", "ef_output_solar_battery_kwh", lambda e: e.result.solar_to_battery
    ),
    _period_sensor("solar_to_grid", "ef_output_solar_grid_kwh", lambda e: e.result.solar_to_grid),
    _period_sensor("grid_to_home", "ef_output_grid_home_kwh", lambda e: e.result.grid_to_home),
    _period_sensor(
        "grid_to_battery", "ef_output_grid_battery_kwh", lambda e: e.result.grid_to_battery
    ),
    _period_sensor(
        "battery_to_home", "ef_output_battery_home_kwh", lambda e: e.result.battery_to_home
    ),
    _period_sensor(
        "battery_to_grid", "ef_output_battery_grid_kwh", lambda e: e.result.battery_to_grid
    ),
)

# Measured energy in the current period (replaces the utility meters).
# Diagnostic and without state class, so they do not create extra statistics.
INPUT_SENSORS: tuple[ElectricityFlowSensorDescription, ...] = (
    _period_sensor(
        "input_solar", "ef_input_solar_kwh_quarter", lambda e: e.period_input.solar, diagnostic=True
    ),
    _period_sensor(
        "input_battery_charge",
        "ef_input_battery_charge_kwh_quarter",
        lambda e: e.period_input.battery_charge,
        diagnostic=True,
    ),
    _period_sensor(
        "input_battery_discharge",
        "ef_input_battery_discharge_kwh_quarter",
        lambda e: e.period_input.battery_discharge,
        diagnostic=True,
    ),
    _period_sensor(
        "input_grid_import",
        "ef_input_grid_import_kwh_quarter",
        lambda e: e.period_input.grid_import,
        diagnostic=True,
    ),
    _period_sensor(
        "input_grid_export",
        "ef_input_grid_export_kwh_quarter",
        lambda e: e.period_input.grid_export,
        diagnostic=True,
    ),
    _period_sensor("grid_net", "ef_grid_kwh_quarter", lambda e: e.result.grid_net, diagnostic=True),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricityFlowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    engine = entry.runtime_data
    async_add_entities(
        ElectricityFlowSensor(engine, entry, description)
        for description in (*OUTPUT_SENSORS, *INPUT_SENSORS)
    )

    # Cost sensors only for outputs with a price entity. Cost sensors of
    # outputs whose price was removed in the options are removed completely.
    cost_sensors: list[ElectricityFlowCostSensor] = []
    registry = er.async_get(hass)
    currency = currency_symbol(hass.config.currency)
    for description in OUTPUT_SENSORS:
        for period in (PERIOD_DAY, PERIOD_MONTH):
            sensor = ElectricityFlowCostSensor(engine, entry, description, period, currency)
            _migrate_entity_id(registry, sensor)
            if description.key in engine.prices:
                cost_sensors.append(sensor)
            elif entity_id := registry.async_get_entity_id(
                "sensor", DOMAIN, sensor.unique_id
            ):
                registry.async_remove(entity_id)
    async_add_entities(cost_sensors)


class ElectricityFlowSensor(SensorEntity):
    """Energy in the current 15 minute period."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: ElectricityFlowSensorDescription

    def __init__(
        self,
        engine: ElectricityFlowEngine,
        entry: ElectricityFlowConfigEntry,
        description: ElectricityFlowSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        self._engine = engine
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # Only used the first time the entity is registered
        self.entity_id = f"sensor.{description.object_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Electricity Flow",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Register for updates of the engine."""
        self.async_on_remove(self._engine.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        """Return the energy of the current period."""
        return self.entity_description.value_fn(self._engine)

    @property
    def last_reset(self) -> datetime | None:
        """Return the start of the current period (state class total only)."""
        if self.entity_description.state_class is SensorStateClass.TOTAL:
            return self._engine.period_start
        return None


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

    def __init__(
        self,
        engine: ElectricityFlowEngine,
        entry: ElectricityFlowConfigEntry,
        output: ElectricityFlowSensorDescription,
        period: str,
        currency: str,
    ) -> None:
        """Initialize the cost sensor."""
        self._engine = engine
        self._output_key = output.key
        self._period = period
        self._period_start_fn = _PERIOD_START[period]
        self._attr_translation_key = f"{output.key}_cost_{period}"
        self._attr_translation_placeholders = {"currency": currency}
        self._attr_unique_id = f"{entry.entry_id}_{output.key}_cost_{period}"
        flow = output.object_id.removeprefix("ef_output_").removesuffix("_kwh")
        # Only used the first time the entity is registered
        self.entity_id = f"sensor.ef_price_{flow}_{period}"
        # Entity id of versions before 0.6.0 (see _migrate_entity_id)
        self.legacy_entity_id = f"sensor.ef_cost_{flow}_{period}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Electricity Flow",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._total = 0.0
        self._last_reset = self._period_start_fn(dt_util.now())
        self._last_quarter: dict[str, object] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the total and listen for finished quarters."""
        self._attr_native_unit_of_measurement = self.hass.config.currency
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is None or last_data is None:
            # Renamed from the old entity id: the restore data is stored
            # under the old id until the next shutdown.
            stored = restore_state.async_get(self.hass).last_states.get(self.legacy_entity_id)
            if stored is not None and stored.extra_data is not None:
                last_state = stored.state
                last_data = SensorExtraStoredData.from_dict(stored.extra_data.as_dict())
        if last_state is not None and last_data is not None:
            restored_reset = dt_util.parse_datetime(
                str(last_state.attributes.get("last_reset"))
            )
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
        quarter_cost = booking.costs.get(self._output_key)
        if quarter_cost is not None:
            booking_period = self._period_start_fn(dt_util.as_local(booking.quarter_start))
            if booking_period > self._last_reset:
                self._start_period(booking_period)
            if quarter_cost.cost is not None and booking_period == self._last_reset:
                self._total += quarter_cost.cost
            self._last_quarter = {
                "last_quarter_start": booking.quarter_start.isoformat(),
                "last_quarter_kwh": quarter_cost.kwh,
                "last_quarter_price": quarter_cost.price,
                "last_quarter_cost": quarter_cost.cost,
            }
            self.async_write_ha_state()
        # The quarter that ends at midnight belongs to the old day: its final
        # total was written above, only now start the new day/month.
        current = self._period_start_fn(dt_util.now())
        if current > self._last_reset:
            self._start_period(current)
            self.async_write_ha_state()

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

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the price entity and the details of the last quarter."""
        return {
            **_price_source_attributes(self._engine.prices.get(self._output_key, "")),
            **self._last_quarter,
        }


def _price_source_attributes(source: str) -> dict[str, str]:
    """Return the price entity or the price template as attribute."""
    if source.startswith(TEMPLATE_PREFIX):
        return {"price_template": source.removeprefix(TEMPLATE_PREFIX)}
    return {"price_entity": source}


_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "INR": "₹"}


def currency_symbol(currency: str) -> str:
    """Return the sign of a currency code (or the code itself)."""
    return _CURRENCY_SYMBOLS.get(currency, currency)


@callback
def _migrate_entity_id(registry: er.EntityRegistry, sensor: ElectricityFlowCostSensor) -> None:
    """Rename sensor.ef_cost_* (before 0.6.0) to sensor.ef_price_*.

    Only when the user did not change the entity id. History and long term
    statistics move along with the entity id.
    """
    old = registry.async_get_entity_id("sensor", DOMAIN, sensor.unique_id or "")
    if (
        old == sensor.legacy_entity_id
        and sensor.entity_id
        and registry.async_get(sensor.entity_id) is None
    ):
        registry.async_update_entity(old, new_entity_id=sensor.entity_id)
