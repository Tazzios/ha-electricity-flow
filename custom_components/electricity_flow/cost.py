"""Cost calculation helpers, kept free of Home Assistant imports.

Choices (see README):
- The price of a quarter is the time weighted average of the price entity
  during that quarter. Dynamic price sensors usually change exactly on the
  hour or quarter, sometimes a few seconds late; the average makes it
  irrelevant whether the timer or the price update comes first.
- If the price was unusable during the whole quarter, the last known valid
  price is used. If there never was a valid price, nothing is booked.
- The cost of a quarter is booked when the quarter ends, in the day and month
  that contain the start of the quarter (so 23:45-00:00 counts for that day).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Energy part of the price unit -> factor to get the price per kWh
_ENERGY_FACTOR: dict[str, float] = {"kwh": 1.0, "mwh": 0.001, "wh": 1000.0}
# Currency parts that mean cents
_CENT_UNITS = {"ct", "c", "cent", "cents", "eurocent", "¢"}


def price_factor(unit: str | None) -> float | None:
    """Return the factor to convert a price in unit to currency per kWh.

    None (no unit) is assumed to be currency/kWh. Returns None when the unit is
    not a price per energy unit (for example "EUR" or "W").
    """
    if unit is None:
        return 1.0
    if "/" not in unit:
        return None
    currency, energy = (part.strip().lower() for part in unit.rsplit("/", 1))
    factor = _ENERGY_FACTOR.get(energy)
    if factor is None:
        return None
    if currency in _CENT_UNITS:
        factor *= 0.01
    return factor


@dataclass(slots=True)
class PriceAverager:
    """Time weighted average of a price during one quarter."""

    weighted_sum: float = 0.0
    seconds: float = 0.0
    current: float | None = None
    since: datetime | None = None
    last_valid: float | None = None

    def update(self, price: float | None, now: datetime) -> None:
        """Register a (possibly unusable) new price at moment now."""
        self._close_segment(now)
        self.current = price
        self.since = now
        if price is not None:
            self.last_valid = price

    def _close_segment(self, now: datetime) -> None:
        if self.current is not None and self.since is not None:
            seconds = (now - self.since).total_seconds()
            if seconds > 0:
                self.weighted_sum += self.current * seconds
                self.seconds += seconds
        self.since = now

    def finish(self, now: datetime) -> float | None:
        """Return the price of the quarter that ends now and start a new one."""
        self._close_segment(now)
        if self.seconds > 0:
            price: float | None = self.weighted_sum / self.seconds
        else:
            price = self.current if self.current is not None else self.last_valid
        self.weighted_sum = 0.0
        self.seconds = 0.0
        return price

    def as_dict(self, now: datetime) -> dict[str, Any]:
        """Return the state for storage, including the running segment."""
        weighted_sum, seconds = self.weighted_sum, self.seconds
        if self.current is not None and self.since is not None:
            extra = max((now - self.since).total_seconds(), 0.0)
            weighted_sum += self.current * extra
            seconds += extra
        return {
            "weighted_sum": weighted_sum,
            "seconds": seconds,
            "last_valid": self.last_valid,
        }

    def restore(self, data: dict[str, Any], same_quarter: bool) -> None:
        """Continue from stored data (only the sums when in the same quarter)."""
        last_valid = data.get("last_valid")
        if last_valid is not None and self.last_valid is None:
            self.last_valid = float(last_valid)
        if same_quarter:
            self.weighted_sum = float(data.get("weighted_sum", 0.0))
            self.seconds = float(data.get("seconds", 0.0))


@dataclass(frozen=True, slots=True)
class QuarterCost:
    """Cost of one output flow in one finished quarter."""

    kwh: float
    price: float | None

    @property
    def cost(self) -> float | None:
        """Return kWh x price, or None when there is no price."""
        if self.price is None:
            return None
        return self.kwh * self.price


@dataclass(frozen=True, slots=True)
class QuarterBooking:
    """All costs of one finished quarter."""

    quarter_start: datetime
    costs: dict[str, QuarterCost]


def day_start(moment: datetime) -> datetime:
    """Return local midnight of the day of moment (moment must be local)."""
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(moment: datetime) -> datetime:
    """Return local midnight of the first day of the month of moment."""
    return day_start(moment).replace(day=1)
