"""Refresh ownership/cooldown requirements independent of GUI and HTTP timing."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from impedance.catalog_refresh import CatalogRefreshGate
from impedance.stackup_model import Stackup


def test_one_worker_per_layer_count_until_actual_completion() -> None:
    """Concurrent/reopened dialogs cannot overlap requests for the same catalog."""
    gate = CatalogRefreshGate(clock=lambda: 10.0)
    with ThreadPoolExecutor(max_workers=8) as executor:
        attempts = tuple(executor.map(gate.reserve, [6] * 8))
    owners = [attempt for attempt in attempts if attempt.token is not None]
    assert len(owners) == 1
    assert all(attempt.retry_in > 0 for attempt in attempts if attempt.token is None)
    assert gate.reserve(4).token is not None
    token = owners[0].token
    assert token is not None
    gate.finish(6, token)
    assert gate.reserve(6).token is not None


@pytest.mark.parametrize("provider_delay", [0.0, 20.0, 900.0])
def test_transient_cooldown_survives_new_observers_and_honors_provider(
    provider_delay: float,
) -> None:
    """Cooldown blocks traffic for five minutes or the longer server delay."""
    now = [100.0]
    gate = CatalogRefreshGate(clock=lambda: now[0])
    token = gate.reserve(6).token
    assert token is not None
    gate.finish(6, token, retry_after=provider_delay)
    delay = max(300.0, provider_delay)
    for elapsed in (0.0, 30.0, delay - 0.001):
        now[0] = 100.0 + elapsed
        reservation = gate.reserve(6)
        assert reservation.token is None
        assert reservation.cooling_down
        assert reservation.retry_in == pytest.approx(delay - elapsed)
    now[0] = 100.0 + delay
    assert gate.reserve(6).token is not None


def test_stale_completion_cannot_release_replacement_or_overwrite_cooldown() -> None:
    """A late callback from a destroyed owner cannot alter a newer request."""
    now = [10.0]
    gate = CatalogRefreshGate(clock=lambda: now[0])
    old = gate.reserve(6).token
    assert old is not None
    gate.finish(6, old)
    current = gate.reserve(6).token
    assert current is not None and current != old
    gate.finish(6, old, retry_after=10000.0)
    assert gate.reserve(6).token is None
    gate.finish(6, current)
    assert gate.reserve(6).token is not None


def test_success_and_cancellation_release_without_failure_cooldown() -> None:
    """Completing without a transient failure permits the next due check."""
    gate = CatalogRefreshGate(clock=lambda: 10.0)
    for _ in range(2):
        token = gate.reserve(6).token
        assert token is not None
        gate.finish(6, token)


@pytest.mark.parametrize("empty", [False, True])
def test_joined_observer_consumes_exact_completed_request(empty: bool) -> None:
    """Already-waiting owners reuse success, including a validated empty result."""
    gate = CatalogRefreshGate(clock=lambda: 10.0)
    owner = gate.reserve(6).token
    assert owner is not None
    waiting = gate.reserve(6)
    assert waiting.token is None and waiting.waiting_for == owner
    assert gate.completed(6, owner) is None
    catalog = () if empty else (Stackup("six", "Six layer", 6, "1.6", "1", "0.5"),)
    gate.finish(6, owner, catalog=catalog)
    assert gate.completed(6, owner) is catalog
    replacement = gate.reserve(6).token
    assert replacement is not None and replacement != owner
    assert gate.completed(6, replacement) is None
    gate.finish(6, owner, catalog=())
    assert gate.reserve(6).token is None


def test_failed_request_does_not_publish_a_completed_catalog() -> None:
    """A temporary outage cannot look like a successful empty catalog."""
    gate = CatalogRefreshGate(clock=lambda: 10.0)
    owner = gate.reserve(6).token
    assert owner is not None
    gate.finish(6, owner, retry_after=0.0)
    assert gate.completed(6, owner) is None
    assert gate.reserve(6).cooling_down


def test_joined_observer_can_use_newer_completed_catalog_before_it_wakes() -> None:
    """An intervening successful refresh cannot make a waiting owner refetch."""
    gate = CatalogRefreshGate(clock=lambda: 10.0)
    first = gate.reserve(6).token
    assert first is not None
    waiting = gate.reserve(6).waiting_for
    assert waiting == first
    original = (Stackup("old", "Original construction", 6, "1.6", "1", "0.5"),)
    gate.finish(6, first, catalog=original)
    later = gate.reserve(6).token
    assert later is not None
    newest = (Stackup("new", "New construction", 6, "1.6", "1", "0.5"),)
    gate.finish(6, later, catalog=newest)
    assert gate.completed(6, waiting) is newest


@pytest.mark.parametrize("layer_count", [True, 0, 1, 65, "6"])
def test_invalid_count_does_not_allocate_process_state(layer_count: object) -> None:
    """Unknown catalog keys cannot grow the lifetime traffic map."""
    gate = CatalogRefreshGate()
    with pytest.raises(ValueError):
        gate.reserve(layer_count)  # type: ignore[arg-type]
