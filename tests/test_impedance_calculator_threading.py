"""Keep calculator metadata and native window access on the owning GUI thread."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
import logging
from threading import Thread, get_ident
import time
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_calculator_metadata_validation import _configuration
from .test_impedance_dialog import _ConstructedControl
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)
from .test_impedance_spec_width_hint import _stackup

draft_api = _draft_api


@pytest.fixture
def configuration_workflow(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Use real constructors and client metadata fetches with deterministic transport."""
    api = draft_api
    calculator = importlib.import_module(api.dialog.__package__ + ".jlcpcb_calculator")
    cache_module = importlib.import_module(
        api.dialog.__package__ + ".calculator_config_cache"
    )
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    state = SimpleNamespace(
        api=api,
        gui=get_ident(),
        workers=[],
        violations=[],
        reads=[],
        writes=[],
        requests=[],
        configurations=[],
        save_error=None,
        read_error=None,
        request_error=None,
        cache_module=cache_module,
    )
    metadata = _configuration()
    metadata.models[0]["value"] = {"source": "original"}
    state.cache = cache_module.CalculatorConfigCache(
        models=metadata.models,
        copper=metadata.copper,
        coating=metadata.coating,
        limits=metadata.limits,
        checked_at_utc=cache_module.calculator_config_checked_at(),
    )

    def record_owner(operation: str) -> None:
        if get_ident() != state.gui:
            state.violations.append(operation)

    def load() -> Any:
        record_owner("load")
        state.reads.append(get_ident())
        if state.read_error is not None:
            raise state.read_error
        return state.cache

    def save(cache: Any) -> None:
        record_owner("save")
        if state.save_error is not None:
            raise state.save_error
        state.writes.append(cache)

    def post(path: str, _payload: dict[str, object], **_kwargs: Any) -> dict[str, Any]:
        state.requests.append(get_ident())
        if state.request_error is not None:
            raise state.request_error
        records = {
            "selectPageImpedancePicture": ("body", metadata.models),
            "impedance-config/copper-trace-width/list": ("data", metadata.copper),
            "impedance-config/coverlay/list": ("data", metadata.coating),
            "selectImpedanceDefaultValue": ("data", metadata.limits),
        }
        key, values = records[path.removeprefix(calculator.API_PREFIX)]
        return {"code": 200, key: list(values)}

    class MetadataCalculator(calculator.JlcpcbCalculator):
        """Retain real metadata parsing/publication and substitute the remote solve."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(post=post, **kwargs)

        def calculate_width(
            self, stackup: Any, spec: Any, layer: str, *, cancel: Any
        ) -> Any:
            configuration = self._load_configuration(time.monotonic() + 5, cancel)
            state.configurations.append(configuration)
            return model.WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=model.calculation_fingerprint(stackup, spec, layer),
                status="success",
                target_width_nm=150_000,
                calculated_at_utc="2026-09-23T00:00:00.000000Z",
                model="CoatedMicrostrip1B",
                calculation_digest="a" * 64,
            )

    class QueuedThread:
        """Let each production worker run on an actual secondary thread on demand."""

        def __init__(
            self, *, target: Callable[[], None], name: str, daemon: bool
        ) -> None:
            self.target = target

        def start(self) -> None:
            state.workers.append(self.target)

    def window_alive(window: Any) -> bool:
        record_owner("window truthiness")
        return _ConstructedControl.__bool__(window)

    monkeypatch.setattr(api.dialog, "Thread", QueuedThread)
    monkeypatch.setattr(calculator, "JlcpcbCalculator", MetadataCalculator)
    for owner in (api.dialog.SpecificationDialog, api.dialog.ImpedanceDialog):
        monkeypatch.setattr(owner, "__bool__", window_alive)
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog, "_ensure_catalog_started", lambda *_args: None
    )
    state.load, state.save = load, save
    return state


def _open(state: SimpleNamespace, kind: str) -> Any:
    """Start one production draft or automatic width worker from its GUI handler."""
    api = state.api
    board, stackup = _board(api), _stackup(api)
    config = replace(_intent(api), stackup=stackup)
    kwargs = {
        "load_calculator_config": state.load,
        "save_calculator_config": state.save,
    }
    if kind == "draft":
        owner = api.dialog.SpecificationDialog(
            None,
            board,
            specification=config.specifications[0],
            stackup=stackup,
            **kwargs,
        )
        owner._draft_calc_timer.Stop()
        owner._on_draft_calc_timer(SimpleNamespace())
    else:
        owner = api.dialog.ImpedanceDialog(
            None, config, board, api.preview, lambda: board, **kwargs
        )
        owner._schedule_width_refresh()
    assert len(state.workers) == 1
    return owner


def _run_worker(state: SimpleNamespace) -> None:
    """Run the real worker off the GUI thread without hiding unexpected exceptions."""
    failures: list[BaseException] = []
    work = state.workers.pop()

    def run() -> None:
        try:
            work()
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []


def _cancel(owner: Any, kind: str) -> None:
    """Invalidate a worker through the production cancellation handler."""
    if kind == "draft":
        owner._cancel_draft_calculation()
    else:
        owner._cancel_calculation()


def _results(owner: Any, kind: str) -> tuple[Any, ...]:
    """Read current model results without depending on table presentation."""
    return (
        owner.width_results if kind == "draft" else owner.session.config.width_results
    )


@pytest.mark.parametrize("kind", ["draft", "automatic"])
@pytest.mark.parametrize("cache_state", ["fresh", "stale", "empty", "read_error"])
def test_configuration_handoff_owns_callbacks_and_copies_records(
    configuration_workflow: SimpleNamespace, kind: str, cache_state: str
) -> None:
    """Read on GUI, use detached data on worker, and publish only after GUI dispatch."""
    state = configuration_workflow
    if cache_state == "stale":
        state.cache = replace(state.cache, checked_at_utc="2020-01-01T00:00:00Z")
    elif cache_state == "empty":
        state.cache = state.cache_module.CalculatorConfigCache()
    elif cache_state == "read_error":
        state.read_error = RuntimeError("Cache cannot be read")
    owner = _open(state, kind)
    assert state.reads == [state.gui]
    if cache_state == "fresh":
        state.cache.models[0]["value"]["source"] = "changed after scheduling"
    _run_worker(state)
    assert state.writes == []
    assert state.violations == []
    if cache_state == "fresh":
        assert state.requests == []
        assert state.configurations[0].models[0]["value"]["source"] == "original"
    else:
        assert len(state.requests) == 4
        assert all(thread_id != state.gui for thread_id in state.requests)
        state.configurations[0].models[0]["value"]["source"] = (
            "changed after publication"
        )
    state.api.drain()
    assert len(state.writes) == (0 if cache_state == "fresh" else 1)
    if state.writes:
        assert state.writes[0].models[0]["value"]["source"] == "original"
    assert len(_results(owner, kind)) == 1
    assert state.violations == []


@pytest.mark.parametrize("kind", ["draft", "automatic"])
def test_reopening_reuses_the_completed_metadata_cache(
    configuration_workflow: SimpleNamespace, kind: str
) -> None:
    """Publishing a complete fetch lets a fresh dialog skip all four metadata requests."""
    state = configuration_workflow
    state.cache = state.cache_module.CalculatorConfigCache()
    first = _open(state, kind)
    _run_worker(state)
    state.api.drain()
    assert len(state.writes) == 1
    assert len(state.requests) == 4
    _cancel(first, kind)
    first.Destroy()
    state.cache = state.writes[0]
    second = _open(state, kind)
    _run_worker(state)
    state.api.drain()
    assert len(state.requests) == 4
    assert len(state.writes) == 1
    assert len(_results(second, kind)) == 1
    assert state.reads == [state.gui, state.gui]
    assert state.violations == []


@pytest.mark.parametrize("kind", ["draft", "automatic"])
def test_cancelled_worker_does_not_start_a_metadata_fetch(
    configuration_workflow: SimpleNamespace, kind: str
) -> None:
    """Cancellation before worker dispatch cannot contact the provider or publish metadata."""
    state = configuration_workflow
    state.cache = state.cache_module.CalculatorConfigCache()
    owner = _open(state, kind)
    _cancel(owner, kind)
    _run_worker(state)
    state.api.drain()
    assert state.requests == []
    assert state.writes == []
    assert _results(owner, kind) == ()
    assert state.violations == []


@pytest.mark.parametrize("kind", ["draft", "automatic"])
@pytest.mark.parametrize("close", [False, True])
def test_completed_metadata_survives_cancel_without_applying_stale_ui(
    configuration_workflow: SimpleNamespace, kind: str, close: bool
) -> None:
    """A queued cache write survives Cancel/destroy, while obsolete UI results do not."""
    state = configuration_workflow
    state.cache = state.cache_module.CalculatorConfigCache()
    owner = _open(state, kind)
    _run_worker(state)
    _cancel(owner, kind)
    if close:
        owner.Destroy()
    state.api.drain()
    assert len(state.writes) == 1
    assert _results(owner, kind) == ()
    assert state.violations == []


@pytest.mark.parametrize("kind", ["draft", "automatic"])
def test_configuration_save_failure_does_not_discard_a_usable_solve(
    configuration_workflow: SimpleNamespace,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception from the deferred cache saver is logged on GUI and stays optional."""
    state = configuration_workflow
    state.cache = state.cache_module.CalculatorConfigCache()
    state.save_error = RuntimeError("Database is read-only")
    owner = _open(state, kind)
    with caplog.at_level(logging.INFO):
        _run_worker(state)
        state.api.drain()
    assert len(_results(owner, kind)) == 1
    assert "Database is read-only" in caplog.text
    assert state.violations == []


@pytest.mark.parametrize("kind", ["draft", "automatic"])
def test_incomplete_provider_metadata_is_never_persisted(
    configuration_workflow: SimpleNamespace, kind: str
) -> None:
    """A failed metadata fetch leaves the last cache untouched and reports solve failure."""
    state = configuration_workflow
    state.cache = state.cache_module.CalculatorConfigCache()
    state.request_error = RuntimeError("Provider metadata unavailable")
    owner = _open(state, kind)
    _run_worker(state)
    state.api.drain()
    assert state.writes == []
    assert state.configurations == []
    results = _results(owner, kind)
    if kind == "draft":
        assert len(results) == 1
        assert results[0].status == "unavailable"
        assert results[0].target_width_nm is None
        assert results[0].calculation_digest == ""
        assert "Provider metadata unavailable" in results[0].message
    else:
        assert results == ()
    assert state.violations == []
