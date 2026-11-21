"""Tests of the pure cost helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.electricity_flow.cost import (
    PriceAverager,
    QuarterCost,
    day_start,
    month_start,
    price_factor,
)

T0 = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("unit", "factor"),
    [
        (None, 1.0),
        ("EUR/kWh", 1.0),
        ("€/kWh", 1.0),
        ("EUR/MWh", 0.001),
        ("ct/kWh", 0.01),
        ("EUR/Wh", 1000.0),
        ("EUR", None),
        ("W", None),
        ("EUR/m³", None),
    ],
)
def test_price_factor(unit, factor):
    if factor is None:
        assert price_factor(unit) is None
    else:
        assert price_factor(unit) == pytest.approx(factor)


def test_average_is_time_weighted():
    avg = PriceAverager()
    avg.update(0.20, T0)
    avg.update(0.40, T0 + timedelta(minutes=5))
    # 5 min at 0.20 and 10 min at 0.40
    assert avg.finish(T0 + timedelta(minutes=15)) == pytest.approx((5 * 0.20 + 10 * 0.40) / 15)
    # Next quarter continues with the last price
    assert avg.finish(T0 + timedelta(minutes=30)) == pytest.approx(0.40)


def test_unavailable_time_is_skipped():
    avg = PriceAverager()
    avg.update(0.30, T0)
    avg.update(None, T0 + timedelta(minutes=5))
    avg.update(0.60, T0 + timedelta(minutes=10))
    # 5 min 0.30, 5 min unknown (ignored), 5 min 0.60
    assert avg.finish(T0 + timedelta(minutes=15)) == pytest.approx(0.45)


def test_whole_quarter_unavailable_uses_last_valid_price():
    avg = PriceAverager()
    avg.update(0.25, T0)
    avg.update(None, T0)
    assert avg.finish(T0 + timedelta(minutes=15)) == pytest.approx(0.25)


def test_never_a_price():
    avg = PriceAverager()
    avg.update(None, T0)
    assert avg.finish(T0 + timedelta(minutes=15)) is None
    assert QuarterCost(kwh=1.0, price=None).cost is None


def test_price_update_exactly_at_quarter_start():
    """A new price that arrives at the same moment the quarter ends is not counted."""
    avg = PriceAverager()
    avg.update(0.10, T0)
    end = T0 + timedelta(minutes=15)
    assert avg.finish(end) == pytest.approx(0.10)
    avg.update(0.90, end)
    assert avg.finish(end + timedelta(minutes=15)) == pytest.approx(0.90)


def test_storage_roundtrip_same_quarter():
    avg = PriceAverager()
    avg.update(0.20, T0)
    data = avg.as_dict(T0 + timedelta(minutes=5))

    restored = PriceAverager()
    restored.restore(data, same_quarter=True)
    # Home Assistant was down for 2 minutes, then the price is 0.40
    restored.update(0.40, T0 + timedelta(minutes=7))
    assert restored.finish(T0 + timedelta(minutes=15)) == pytest.approx(
        (5 * 0.20 + 8 * 0.40) / 13
    )


def test_storage_other_quarter_keeps_only_last_valid():
    avg = PriceAverager()
    avg.update(0.20, T0)
    restored = PriceAverager()
    restored.restore(avg.as_dict(T0 + timedelta(minutes=5)), same_quarter=False)
    restored.update(None, T0 + timedelta(minutes=20))
    assert restored.finish(T0 + timedelta(minutes=30)) == pytest.approx(0.20)


def test_cost_and_periods():
    assert QuarterCost(kwh=0.5, price=0.3).cost == pytest.approx(0.15)
    # Negative prices give a negative cost
    assert QuarterCost(kwh=0.5, price=-0.1).cost == pytest.approx(-0.05)
    moment = datetime(2026, 11, 21, 23, 45, tzinfo=timezone.utc)
    assert day_start(moment) == datetime(2026, 11, 21, tzinfo=timezone.utc)
    assert month_start(moment) == datetime(2026, 11, 1, tzinfo=timezone.utc)
