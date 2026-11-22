"""Engine that replaces the utility meters and the trigger based template.

For every configured source sensor (a cumulative energy meter) the engine keeps
track of how much energy was added in the current 15 minute period, exactly
like the quarter-hourly utility_meter helpers did in the package. On every
change of a source sensor the energy flow is recalculated and the sensors are
updated. At every quarter (hh:00, hh:15, hh:30, hh:45) the period is reset.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import ATTR_LAST_RESET
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.event import (
    TrackTemplate,
    TrackTemplateResult,
    async_track_state_change_event,
    async_track_template_result,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

from .calculation import FlowInput, FlowResult, calculate_flow
from .const import (
    CONF_BATTERY_CHARGE_SENSORS,
    CONF_BATTERY_DISCHARGE_SENSORS,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_SOLAR_SENSORS,
    DOMAIN,
    OUTPUT_KEYS,
    OUTPUT_PRECISION,
    PERIOD_MINUTES,
    ROLE_BATTERY_CHARGE,
    ROLE_BATTERY_DISCHARGE,
    ROLE_GRID_EXPORT,
    ROLE_GRID_IMPORT,
    ROLE_SOLAR,
    STORAGE_VERSION,
    price_option,
    price_template_option,
)
from .cost import PriceAverager, QuarterBooking, QuarterCost, price_currency, price_factor
from .issues import TEMPLATE_PREFIX, IssueTracker, price_key

_LOGGER = logging.getLogger(__name__)

# Conversion factors to kWh
UNIT_TO_KWH: dict[str, float] = {
    UnitOfEnergy.WATT_HOUR: 0.001,
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.MEGA_WATT_HOUR: 1000.0,
}

# A drop of the meter value below this fraction of the previous value is seen
# as a meter reset (for example a "per day" counter at midnight). Smaller drops
# are treated as measurement noise and ignored.
RESET_THRESHOLD = 0.5

# One update of a meter can not plausibly add more energy than this (kWh).
# Larger jumps come from a meter that briefly reported 0 or a wrong value (or
# was unavailable for a long time) and are ignored instead of being booked in
# one quarter.
MAX_DELTA_KWH = 10.0


def period_start(moment: datetime) -> datetime:
    """Return the start of the 15 minute period that contains moment."""
    return moment.replace(
        minute=(moment.minute // PERIOD_MINUTES) * PERIOD_MINUTES,
        second=0,
        microsecond=0,
    )


def sources_from_config(config: dict[str, Any]) -> dict[str, list[str]]:
    """Map every role to the list of configured entity ids."""
    return {
        ROLE_SOLAR: list(config.get(CONF_SOLAR_SENSORS, [])),
        ROLE_BATTERY_CHARGE: list(config.get(CONF_BATTERY_CHARGE_SENSORS, [])),
        ROLE_BATTERY_DISCHARGE: list(config.get(CONF_BATTERY_DISCHARGE_SENSORS, [])),
        ROLE_GRID_IMPORT: [config[CONF_GRID_IMPORT_SENSOR]],
        ROLE_GRID_EXPORT: [config[CONF_GRID_EXPORT_SENSOR]],
    }


def state_to_kwh(state: State | None) -> float | None:
    """Return the value of a meter state in kWh, or None if not usable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit is None:
        # Assumption: sensors without a unit are in kWh
        return value
    factor = UNIT_TO_KWH.get(unit)
    if factor is None:
        _LOGGER.warning(
            "Sensor %s has unsupported unit %s, expected Wh, kWh or MWh",
            state.entity_id,
            unit,
        )
        return None
    return value * factor


def prices_from_config(config: dict[str, Any]) -> dict[str, str]:
    """Map every output key with a price to its price source.

    The source is the entity id of a price entity, or "template:" + the
    template (a template wins when both are set).
    """
    prices: dict[str, str] = {}
    for key in OUTPUT_KEYS:
        if template := (config.get(price_template_option(key)) or "").strip():
            prices[key] = f"{TEMPLATE_PREFIX}{template}"
        elif entity_id := config.get(price_option(key)):
            prices[key] = entity_id
    return prices


def template_result_to_price(result: Any) -> float | None:
    """Return the rendered template as price per kWh, or None if not a number."""
    if isinstance(result, TemplateError):
        return None
    try:
        return float(result)
    except (TypeError, ValueError):
        return None


def state_to_price(state: State | None) -> float | None:
    """Return the price per kWh of a price state, or None if not usable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    factor = price_factor(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
    if factor is None:
        _LOGGER.warning(
            "Price entity %s has unsupported unit %s, expected a price per kWh",
            state.entity_id,
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT),
        )
        return None
    return value * factor


def create_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Return the store with the period state of a config entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


class ElectricityFlowEngine:
    """Collects the period energy per source and calculates the flows."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the engine."""
        self.hass = hass
        self.entry = entry
        # The options (when saved once) contain the complete configuration
        config = dict(entry.options or entry.data)
        self.sources = sources_from_config(config)
        # output key -> price source (only outputs with a price get costs)
        self.prices = prices_from_config(config)
        self.period_start: datetime = period_start(dt_util.now())
        self.result = FlowResult()
        self.period_input = FlowInput()
        # Costs of the last finished quarter (None until the first one ends)
        self.last_booking: QuarterBooking | None = None

        # last seen meter value (kWh) and energy in the current period per entity
        self._last_value: dict[str, float] = {}
        self._period_energy: dict[str, float] = {}
        # last seen last_reset attribute per entity (meters that report resets)
        self._source_reset: dict[str, str] = {}
        # price source -> average price of the current period
        self._price_avg: dict[str, PriceAverager] = {
            source: PriceAverager() for source in set(self.prices.values())
        }

        self._listeners: list[Callable[[], None]] = []
        self._booking_listeners: list[Callable[[QuarterBooking], None]] = []
        self._store: Store[dict[str, Any]] = create_store(hass, entry.entry_id)
        self.issues = IssueTracker(hass)

    @property
    def entity_ids(self) -> list[str]:
        """Return all source entity ids (without duplicates)."""
        seen: dict[str, None] = {}
        for entity_ids in self.sources.values():
            for entity_id in entity_ids:
                seen[entity_id] = None
        return list(seen)

    # ------------------------------------------------------------------ setup

    async def async_start(self) -> None:
        """Restore state, take the first readings and start listening."""
        await self._async_restore()

        self.issues.check_duplicates(self.sources)
        for entity_id in self.entity_ids:
            self._process_state(entity_id, self.hass.states.get(entity_id))
        self._recalculate()

        now = dt_util.now()
        price_entities = [s for s in self._price_avg if not s.startswith(TEMPLATE_PREFIX)]
        for entity_id in price_entities:
            state = self.hass.states.get(entity_id)
            self._check_price_entity(entity_id, state)
            self._price_avg[entity_id].update(state_to_price(state), now)

        self.entry.async_on_unload(
            async_track_state_change_event(self.hass, self.entity_ids, self._handle_source_change)
        )
        if price_entities:
            self.entry.async_on_unload(
                async_track_state_change_event(self.hass, price_entities, self._handle_price_change)
            )
        for source in self._price_avg:
            if source.startswith(TEMPLATE_PREFIX):
                self._track_price_template(source)
        self.entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._handle_new_period,
                minute=list(range(0, 60, PERIOD_MINUTES)),
                second=0,
            )
        )

    async def _async_restore(self) -> None:
        """Continue the current period after a restart of Home Assistant."""
        data = await self._store.async_load()
        if not data:
            return
        try:
            stored_start = dt_util.parse_datetime(data["period_start"])
        except (KeyError, TypeError):
            return
        same_period = stored_start is not None and stored_start == self.period_start
        prices = data.get("prices", {})
        for entity_id, averager in self._price_avg.items():
            if isinstance(prices.get(entity_id), dict):
                averager.restore(prices[entity_id], same_period)
        if not same_period:
            # A different period, the energy in between can not be assigned
            return
        configured = set(self.entity_ids)
        self._last_value = {
            k: float(v) for k, v in data.get("last_value", {}).items() if k in configured
        }
        self._period_energy = {
            k: float(v) for k, v in data.get("period_energy", {}).items() if k in configured
        }
        self._source_reset = {
            k: str(v) for k, v in data.get("source_reset", {}).items() if k in configured
        }

    async def async_save(self) -> None:
        """Write the period state now (on unload), instead of delayed."""
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        now = dt_util.now()
        return {
            "period_start": self.period_start.isoformat(),
            "last_value": self._last_value,
            "period_energy": self._period_energy,
            "source_reset": self._source_reset,
            "prices": {
                entity_id: averager.as_dict(now) for entity_id, averager in self._price_avg.items()
            },
        }

    # -------------------------------------------------------------- listeners

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback that is called after every recalculation."""
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            self._listeners.remove(update_callback)

        return remove

    @callback
    def async_add_booking_listener(
        self, booking_callback: Callable[[QuarterBooking], None]
    ) -> Callable[[], None]:
        """Register a callback that is called when a quarter has been finished."""
        self._booking_listeners.append(booking_callback)

        @callback
        def remove() -> None:
            self._booking_listeners.remove(booking_callback)

        return remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ----------------------------------------------------------------- events

    @callback
    def _handle_source_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle a new value of a source meter."""
        # Safety net: if the quarter timer has not fired yet (it runs at
        # second 0), make sure energy is not booked in an old period.
        if period_start(dt_util.now()) != self.period_start:
            self._start_new_period(dt_util.now())
        self._process_state(event.data["entity_id"], event.data["new_state"])
        self._recalculate()
        self._notify()

    @callback
    def _handle_new_period(self, now: datetime) -> None:
        """Reset all period counters at hh:00, hh:15, hh:30 and hh:45."""
        self.issues.check_unusable(now)
        if period_start(now) == self.period_start:
            return
        self._start_new_period(now)
        self._recalculate()
        self._notify()

    @callback
    def _track_price_template(self, source: str) -> None:
        """Follow the result of a price template (re-rendered on every change)."""
        template = Template(source.removeprefix(TEMPLATE_PREFIX), self.hass)

        @callback
        def _template_changed(
            event: Event[EventStateChangedData] | None,
            updates: list[TrackTemplateResult],
        ) -> None:
            price = template_result_to_price(updates[-1].result)
            self.issues.usable(price_key(source), price is not None, dt_util.now())
            if price is None:
                _LOGGER.warning(
                    "Price template %s did not give a number: %s",
                    template.template,
                    updates[-1].result,
                )
            self._price_update(source, price)

        info = async_track_template_result(
            self.hass, [TrackTemplate(template, None)], _template_changed
        )
        info.async_refresh()
        self.entry.async_on_unload(info.async_remove)

    @callback
    def _handle_price_change(self, event: Event[EventStateChangedData]) -> None:
        """Track a new value of a price entity for the average of the quarter."""
        entity_id = event.data["entity_id"]
        self._check_price_entity(entity_id, event.data["new_state"])
        self._price_update(entity_id, state_to_price(event.data["new_state"]))

    @callback
    def _check_price_entity(self, entity_id: str, state: State | None) -> None:
        """Update the repair issues of a price entity."""
        unit = None if state is None else state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        bad_unit = state is not None and price_factor(unit) is None
        self.issues.price_unit(entity_id, unit if bad_unit else None)
        self.issues.price_currency(entity_id, None if bad_unit else price_currency(unit))
        # A wrong unit has its own issue, so it does not count as unavailable
        usable = bad_unit or state_to_price(state) is not None
        self.issues.usable(price_key(entity_id), usable, dt_util.now())

    @callback
    def _price_update(self, source: str, price: float | None) -> None:
        now = dt_util.now()
        if period_start(now) != self.period_start:
            # The quarter timer has not run yet: finish the old quarter first
            self._start_new_period(now)
            self._recalculate()
            self._notify()
        averager = self._price_avg.get(source)
        if averager is not None:
            averager.update(price, now)
            self._store.async_delay_save(self._data_to_save, 10)

    @callback
    def _start_new_period(self, now: datetime) -> None:
        self._book_finished_period(period_start(now))
        self.period_start = period_start(now)
        self._period_energy = {}

    @callback
    def _book_finished_period(self, end: datetime) -> None:
        """Calculate kWh x average price per output for the quarter that ended."""
        if not self.prices:
            return
        # Close the price averages at the real end of the quarter
        averages = {
            entity_id: averager.finish(end) for entity_id, averager in self._price_avg.items()
        }
        costs: dict[str, QuarterCost] = {}
        for key, entity_id in self.prices.items():
            price = averages[entity_id]
            if price is None:
                _LOGGER.warning(
                    "No valid price from %s for the quarter starting %s, cost not booked",
                    entity_id,
                    self.period_start,
                )
            costs[key] = QuarterCost(kwh=getattr(self.result, key), price=price)
        self.last_booking = QuarterBooking(quarter_start=self.period_start, costs=costs)
        for booking_callback in list(self._booking_listeners):
            booking_callback(self.last_booking)

    # ------------------------------------------------------------ calculation

    @callback
    def _process_state(self, entity_id: str, state: State | None) -> None:
        """Process a new state of a source meter."""
        self._check_source(entity_id, state)
        value = state_to_kwh(state)
        if state is None or value is None:
            return
        last_reset = state.attributes.get(ATTR_LAST_RESET)
        self._process_value(entity_id, value, None if last_reset is None else str(last_reset))

    @callback
    def _check_source(self, entity_id: str, state: State | None) -> None:
        """Update the repair issues of a source meter."""
        usable = False
        bad_unit = None
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            try:
                float(state.state)
            except ValueError:
                pass
            else:
                usable = True
                unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                if unit is not None and unit not in UNIT_TO_KWH:
                    bad_unit = str(unit)
        self.issues.source_unit(entity_id, bad_unit)
        self.issues.usable(entity_id, usable, dt_util.now())

    @callback
    def _process_value(self, entity_id: str, value: float, last_reset: str | None = None) -> None:
        """Add the increase of a meter to the energy of the current period.

        last_reset is the last_reset attribute of the meter (if it has one). A
        change of it means the meter started again from zero, like a daily
        counter at midnight.
        """
        previous_reset = self._source_reset.get(entity_id)
        if last_reset is not None:
            self._source_reset[entity_id] = last_reset
        previous = self._last_value.get(entity_id)
        if previous is None:
            self._last_value[entity_id] = value
            return
        delta = value - previous
        if last_reset is not None and previous_reset is not None and last_reset != previous_reset:
            # The meter reports a reset: all of its value is new energy
            _LOGGER.debug("Meter %s reports a reset (last_reset %s)", entity_id, last_reset)
            delta = value
        elif delta < 0:
            if value >= previous * RESET_THRESHOLD:
                # Measurement noise: keep the old reading as baseline, so the
                # dip is not counted again when the meter goes back up
                _LOGGER.debug(
                    "Ignoring small decrease of %s (%s -> %s)", entity_id, previous, value
                )
                return
            # Meter was reset (e.g. daily counter), count from zero
            _LOGGER.debug("Meter reset detected for %s", entity_id)
            delta = value
        self._last_value[entity_id] = value
        if delta > MAX_DELTA_KWH:
            _LOGGER.warning(
                "Ignoring implausible increase of %s kWh for %s (%s -> %s)",
                round(delta, OUTPUT_PRECISION),
                entity_id,
                previous,
                value,
            )
            return
        if delta:
            self._period_energy[entity_id] = self._period_energy.get(entity_id, 0.0) + delta

    def _role_total(self, role: str) -> float:
        return sum(self._period_energy.get(e, 0.0) for e in self.sources[role])

    @callback
    def _recalculate(self) -> None:
        self.period_input = FlowInput(
            solar=self._role_total(ROLE_SOLAR),
            battery_charge=self._role_total(ROLE_BATTERY_CHARGE),
            battery_discharge=self._role_total(ROLE_BATTERY_DISCHARGE),
            grid_import=self._role_total(ROLE_GRID_IMPORT),
            grid_export=self._role_total(ROLE_GRID_EXPORT),
        )
        self.result = calculate_flow(self.period_input, OUTPUT_PRECISION)
        self._store.async_delay_save(self._data_to_save, 10)
