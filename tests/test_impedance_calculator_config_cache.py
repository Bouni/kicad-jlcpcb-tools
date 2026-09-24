"""Calculator metadata cache freshness and persistence helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from impedance.calculator_config_cache import (
    CALCULATOR_CONFIG_CHECK_INTERVAL,
    CalculatorConfigCache,
    calculator_config_check_due,
    calculator_config_checked_at,
    calculator_config_from_payload,
    calculator_config_to_payload,
)


def _sample(**overrides: object) -> CalculatorConfigCache:
    payload = {
        "models": ({"impedanceType": "CoatedMicrostrip1B"},),
        "copper": ({"baseCopperThickness": "1"},),
        "coating": ({"coatingAboveSubstrate": "0.01"},),
        "limits": ({"impedanceName": "W2", "minValue": "2.5", "maxValue": "100"},),
        "checked_at_utc": "2026-09-01T00:00:00.000000Z",
    }
    payload.update(overrides)
    return CalculatorConfigCache(**payload)  # type: ignore[arg-type]


def test_calculator_config_check_due_respects_seven_day_window() -> None:
    """Empty or aged calculator metadata must refetch after seven days."""
    fresh = _sample(checked_at_utc=calculator_config_checked_at())
    assert calculator_config_check_due(fresh) is False
    stale_time = (
        datetime.now(timezone.utc)
        - CALCULATOR_CONFIG_CHECK_INTERVAL
        - timedelta(minutes=1)
    )
    stale = _sample(
        checked_at_utc=stale_time.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    )
    assert calculator_config_check_due(stale) is True
    assert calculator_config_check_due(CalculatorConfigCache()) is True


def test_calculator_config_round_trip_payload() -> None:
    """Successful checks encode and decode without migrating on read."""
    cache = _sample()
    encoded = calculator_config_to_payload(cache)
    assert calculator_config_from_payload(encoded) == cache
    with pytest.raises(ValueError):
        calculator_config_to_payload(CalculatorConfigCache())
