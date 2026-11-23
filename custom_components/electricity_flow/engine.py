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

from .calculation import FlowInput, FlowResult, calculate_flow, split_battery, split_solar
from .const import (
    CONF_BATTERY_CHARGE_SENSORS,
    CONF_BATTERY_DISCHARGE_SENSORS,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_SOLAR_SENSORS,
    DOMAIN,
    OUTPUT_PRECISION,
    PERIOD_MINUTES,
    ROLE_BATTERY_CHARGE,
    ROLE_BATTERY_DISCHARGE,
    ROLE_GRID_EXPORT,
    ROLE_GRID_IMPORT,
    ROLE_SOLAR,
    STORAGE_VERSION,
)
from .cost import (
    PriceAverager,
    QuarterBooking,
    QuarterCost,
    combine_costs,
    price_currency,
    price_factor,
    signed_total,
)
from .issues import TEMPLATE_PREFIX, IssueTracker, price_key
from .units import (
    FLOW_SOURCE,
    KIND_BATTERY,
    KIND_FLOWS,
    KIND_GRID,
    Unit,
    sources_from_entry,
    units_from_entry,
)

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
    """Map every role to the list of configured entity ids (no duplicates).

    config has the meters in the flat format of sources_from_entry.
    """

    def meters(key: str) -> list[str]:
        value = config.get(key) or []
        return list(dict.fromkeys([value] if isinstance(value, str) else value))

    return {
        ROLE_SOLAR: meters(CONF_SOLAR_SENSORS),
        ROLE_BATTERY_CHARGE: meters(CONF_BATTERY_CHARGE_SENSORS),
        ROLE_BATTERY_DISCHARGE: meters(CONF_BATTERY_DISCHARGE_SENSORS),
        ROLE_GRID_IMPORT: meters(CONF_GRID_IMPORT_SENSOR),
        ROLE_GRID_EXPORT: meters(CONF_GRID_EXPORT_SENSOR),
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
        # The grid, solar sets and batteries, each a device (sub-entry) with
        # the prices of the flows it is the source of
        self.units: list[Unit] = units_from_entry(entry)
        self.sources = sources_from_config(sources_from_entry(entry))
        # unit id -> flow key -> energy of that unit in the current period
        self.unit_results: dict[str, dict[str, float]] = {}
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
            source: PriceAverager() for source in {s for u in self.units for s in u.prices.values()}
        }

        self._listeners: list[Callable[[], None]] = []
        self._booking_listeners: list[Callable[[QuarterBooking], None]] = []
        self._store: Store[dict[str, Any]] = create_store(hass, entry.entry_id)
        self.issues = IssueTracker(hass)

    def _units_of(self, kind: str) -> list[Unit]:
        return [unit for unit in self.units if unit.kind == kind]

    @property
    def total_price_keys(self) -> set[str]:
        """Return the flows of the totals that have costs.

        A flow has costs when one of its sources has a price. The house has
        costs when one of the flows to home has.
        """
        keys = {key for unit in self.units for key in unit.prices}
        if keys & {"solar_to_home", "grid_to_home", "battery_to_home"}:
            keys.add("house")
        return keys

    @property
    def main_cost_keys(self) -> set[str]:
        """Return the cost sensors of the main device: only the house.

        The costs of every flow are on the device it comes from.
        """
        return self.total_price_keys & {"house"}

    def unit_cost_keys(self, unit: Unit) -> set[str]:
        """Return the flows of a device with a cost sensor: the ones it has a price for.

        Charging a battery is not a sensor of the battery (that would double
        the sensor of the source), but it is part of the battery total.
        """
        return set(unit.prices)

    def unit_has_total(self, unit: Unit) -> bool:
        """Return whether a device gets a total cost sensor."""
        keys = set(self.unit_cost_keys(unit))
        if unit.kind == KIND_BATTERY:
            keys |= {"solar_to_battery", "grid_to_battery"} & self.total_price_keys
        if unit.kind == KIND_GRID:
            keys |= {"solar_to_grid", "battery_to_grid"} & self.total_price_keys
        return bool(keys)

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
        if not self._price_avg:
            return
        # Close the price averages at the real end of the quarter
        averages = {
            entity_id: averager.finish(end) for entity_id, averager in self._price_avg.items()
        }
        for source, price in averages.items():
            if price is None:
                _LOGGER.warning(
                    "No valid price from %s for the quarter starting %s, cost not booked",
                    source,
                    self.period_start,
                )
        # Every device with the prices of the flows it is the source of
        unit_costs: dict[str, dict[str, QuarterCost]] = {}
        for unit in self.units:
            flows = self.unit_results.get(unit.id, {})
            unit_costs[unit.id] = {
                key: QuarterCost(kwh=flows.get(key, 0.0), price=averages[source])
                for key, source in unit.prices.items()
            }
        # The totals: every flow is the sum of its sources
        costs: dict[str, QuarterCost] = {}
        for key, kind in FLOW_SOURCE.items():
            parts = [unit_costs[u.id][key] for u in self._units_of(kind) if key in unit_costs[u.id]]
            if parts:
                costs[key] = combine_costs(parts)
        to_home = [
            costs[k] for k in ("solar_to_home", "grid_to_home", "battery_to_home") if k in costs
        ]
        if to_home:
            costs["house"] = combine_costs(to_home)
        # A battery pays for its charging with the (average) price of the source
        for battery in self._units_of(KIND_BATTERY):
            flows = self.unit_results.get(battery.id, {})
            for key in ("solar_to_battery", "grid_to_battery"):
                if key in costs:
                    unit_costs[battery.id][key] = QuarterCost(
                        kwh=flows.get(key, 0.0), price=costs[key].price
                    )
        unit_totals = {
            unit.id: signed_total(self._total_parts(unit, unit_costs[unit.id], costs))
            for unit in self.units
        }
        self.last_booking = QuarterBooking(
            quarter_start=self.period_start,
            costs=costs,
            unit_costs=unit_costs,
            unit_totals=unit_totals,
        )
        for booking_callback in list(self._booking_listeners):
            booking_callback(self.last_booking)

    @staticmethod
    def _total_parts(
        unit: Unit, own: dict[str, QuarterCost], totals: dict[str, QuarterCost]
    ) -> list[tuple[int, QuarterCost]]:
        """Return the costs of a device with their sign for its total.

        A solar set: the value of all it delivers. A battery: what it delivers
        minus what charging cost. The grid: what it delivers minus the
        energy fed back (solar and battery to grid, priced at their source).
        """
        if unit.kind == KIND_BATTERY:
            signs = {
                "battery_to_home": 1,
                "battery_to_grid": 1,
                "solar_to_battery": -1,
                "grid_to_battery": -1,
            }
        else:
            signs = dict.fromkeys(KIND_FLOWS[unit.kind], 1)
        parts = [(sign, own[key]) for key, sign in signs.items() if key in own]
        if unit.kind == KIND_GRID:
            parts += [(-1, totals[k]) for k in ("solar_to_grid", "battery_to_grid") if k in totals]
        return parts

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
        return self._energy(self.sources[role])

    def _energy(self, entity_ids: tuple[str, ...] | list[str]) -> float:
        return sum(self._period_energy.get(e, 0.0) for e in entity_ids)

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
        self._split_over_units()
        self._store.async_delay_save(self._data_to_save, 10)

    @callback
    def _split_over_units(self) -> None:
        """Divide the flows over the devices (solar sets and batteries by share)."""
        if not self.units:
            return
        production = {u.id: self._energy(u.solar) for u in self.units if u.solar}
        charge = {u.id: self._energy(u.charge) for u in self.units if u.charge}
        discharge = {u.id: self._energy(u.discharge) for u in self.units if u.discharge}
        self.unit_results = {
            **split_solar(self.period_input, self.result, production, OUTPUT_PRECISION),
            **split_battery(self.period_input, self.result, charge, discharge, OUTPUT_PRECISION),
            # The grid (one at most) has the grid flows completely
            **{
                u.id: {key: getattr(self.result, key) for key in KIND_FLOWS[KIND_GRID]}
                for u in self._units_of(KIND_GRID)
            },
        }
