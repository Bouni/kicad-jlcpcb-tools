"""Periodic retries must retain the batch that already owns unchanged inputs."""

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .test_impedance_calculation_completion import (
    _result,
    _setup,
    constructor_api,
    draft_api,
    workers,
)

__all__ = ["constructor_api", "draft_api", "workers"]


@pytest.mark.parametrize(
    "state", ["no_stackup", "incomplete", "success", "unsupported"]
)
def test_inactive_noop_refresh_does_not_read_source_or_rebuild_rows(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Only starting or owning actual work needs an async source boundary."""
    api = draft_api
    dialog, _saved = _setup(api, workers, prior_results=True)
    config = dialog.session.config
    if state == "no_stackup":
        config = replace(config, stackup=None)
    elif state == "incomplete":
        config = replace(config, specifications=())
    elif state == "unsupported":
        config = replace(
            config,
            width_results=(
                _result(
                    api,
                    config.stackup,
                    config.specifications[0],
                    "F.Cu",
                    status="unsupported",
                ),
            ),
        )
    dialog.session.config = config
    source_reads = []
    rebuilds = []
    original_source = dialog.refresh_snapshot
    monkeypatch.setattr(
        dialog,
        "refresh_snapshot",
        lambda: source_reads.append(True) or original_source(),
    )
    monkeypatch.setattr(dialog, "_populate_sections", lambda: rebuilds.append(True))
    generation = dialog._calculation_generation
    dialog._width_refresh_alert = "An obsolete slow notice"
    dialog._schedule_width_refresh()
    dialog._schedule_width_refresh()
    assert source_reads == []
    assert rebuilds == []
    assert workers == []
    assert dialog._calculation_generation == generation
    assert dialog.session.config == config
    assert dialog._width_refresh_alert == ""


def test_forced_cached_refresh_still_verifies_source(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit recalculation retains its source check even for cached inputs."""
    dialog, _saved = _setup(draft_api, workers, prior_results=True)
    reads = []
    original = dialog.refresh_snapshot
    monkeypatch.setattr(
        dialog, "refresh_snapshot", lambda: reads.append(True) or original()
    )
    dialog._schedule_width_refresh(force=True)
    assert reads == [True]
    assert len(workers) == 1


def test_periodic_tick_keeps_same_input_worker_and_slow_alert(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """A timer is a retry opportunity, not permission to restart pending work."""
    dialog, _saved = _setup(draft_api, workers, prior_results=False, two_layers=True)
    dialog._schedule_width_refresh()
    token, generation = dialog._calculation_cancel, dialog._calculation_generation
    dialog._width_refresh_alert = "Still waiting for this batch"
    started = dialog._width_refresh_started_at
    for _ in range(3):
        dialog._on_width_refresh_timer(SimpleNamespace())
    assert token is not None and not token.is_set()
    assert dialog._calculation_cancel is token
    assert dialog._calculation_generation == generation
    assert dialog._width_refresh_started_at == started
    assert dialog._width_refresh_alert == "Still waiting for this batch"
    assert len(workers) == 1


def test_timer_during_each_job_allows_batch_completion(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Even multiple periodic events cannot starve a multi-layer calculation."""
    api = draft_api
    dialog, _saved = _setup(api, workers, prior_results=False, two_layers=True)
    layers = []

    def calculate(
        stackup: object, spec: object, layer: str, **_kwargs: object
    ) -> object:
        layers.append(layer)
        dialog._on_width_refresh_timer(SimpleNamespace())
        return _result(api, stackup, spec, layer)

    dialog.calculate_width_callback = calculate
    dialog._schedule_width_refresh()
    workers.pop()()
    api.drain()
    assert layers == ["F.Cu", "B.Cu"]
    assert workers == []
    assert len(dialog.session.config.width_results) == 2
    assert dialog._calculation_cancel is None


@pytest.mark.parametrize("force", [False, True])
def test_changed_or_forced_inputs_cancel_the_old_owner(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]], force: bool
) -> None:
    """Only a changed key or an explicit forced request supersedes valid work."""
    api = draft_api
    dialog, _saved = _setup(api, workers, prior_results=False)
    dialog._schedule_width_refresh()
    token = dialog._calculation_cancel
    if not force:
        spec = dialog.session.config.specifications[0]
        dialog.session.config = replace(
            dialog.session.config, specifications=(replace(spec, target_ohms="60"),)
        )
    dialog._schedule_width_refresh(force=force)
    assert token is not None and token.is_set()
    assert dialog._calculation_cancel is not token
    assert len(workers) == 2


def test_removed_stackup_cancels_work_without_replacement(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """An incomplete new configuration cannot leave obsolete work alive."""
    dialog, _saved = _setup(draft_api, workers, prior_results=False)
    dialog._schedule_width_refresh()
    token = dialog._calculation_cancel
    dialog.session.config = replace(dialog.session.config, stackup=None)
    dialog._on_width_refresh_timer(SimpleNamespace())
    assert token is not None and token.is_set()
    assert dialog._calculation_cancel is None
    assert len(workers) == 1


def test_source_failure_cancels_current_work(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed verification cannot leave a worker owning an unverified revision."""
    dialog, _saved = _setup(draft_api, workers, prior_results=False)
    dialog._schedule_width_refresh()
    token = dialog._calculation_cancel

    def fail() -> None:
        raise ValueError("Current PCB cannot be read")

    monkeypatch.setattr(dialog, "_verify_calculation_snapshot", fail)
    dialog._schedule_width_refresh()
    assert token is not None and token.is_set()
    assert dialog._calculation_cancel is None
