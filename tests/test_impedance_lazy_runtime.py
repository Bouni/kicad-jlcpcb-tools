"""Cached opening avoids provider runtime; needed jobs retain their metadata handoff."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import replace
import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tests.impedance_performance_fixtures import routed_draft
from tests.test_impedance_dialog import constructor_api  # noqa: F401 -- shared fixture


def _missing_provider(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    module: str,
    error_type: type[Exception] = ImportError,
) -> None:
    """Fail the real relative import without supplying a replacement provider."""
    original_import = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == module and args and args[0].get("__package__") == package:
            raise error_type("Provider dependency unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)


@pytest.fixture
def lazy_api(
    constructor_api: SimpleNamespace,  # noqa: F811 -- shared fixture
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Add wx's silent text update while retaining the shared control's read state."""

    def change_value(control: Any, value: str) -> None:
        control.value = value

    monkeypatch.setattr(
        constructor_api.wx.TextCtrl, "ChangeValue", change_value, raising=False
    )
    return constructor_api


def _open_cached(api: SimpleNamespace, **kwargs: Any) -> tuple[Any, Any]:
    """Use real constructors, first-show, first-paint, and catalog cache callbacks."""
    snapshot, config = routed_draft(SimpleNamespace(model=api.model))
    stackups = importlib.import_module(f"{api.dialog.__package__}.stackup_model")
    construction = replace(config.stackup, calculator_id="cached-calculator")
    specs = {spec.spec_id: spec for spec in config.specifications}
    config = replace(
        config,
        stackup=construction,
        width_results=tuple(
            replace(
                result,
                input_digest=stackups.calculation_fingerprint(
                    construction, specs[result.spec_id], result.layer
                ),
            )
            for result in config.width_results
        ),
    )
    cache = importlib.import_module(f"{api.dialog.__package__}.catalog_cache")
    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        snapshot,
        api.preview,
        lambda: snapshot,
        load_stackup_catalog=lambda _count: cache.CatalogCache(
            (config.stackup,), cache.catalog_checked_at()
        ),
        **kwargs,
    )
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
        )
    )
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    assert dialog.session.analysis is not None
    assert dialog._row_ids and dialog.preview_pane.displayed
    assert dialog._catalog_controller._completed
    assert not dialog._catalog_controller.state.error
    return dialog, config


@pytest.mark.parametrize("runtime", ["jlcpcb_calculator", "jlcpcb_stackups"])
def test_cached_opening_does_not_import_unused_provider_runtime(
    lazy_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """Settled widths and a current catalog require neither provider implementation."""
    api = lazy_api
    catalog = importlib.import_module(f"{api.dialog.__package__}.catalog_controller")

    def unexpected_worker(**_kwargs: Any) -> None:
        pytest.fail("A cached opening started an unnecessary network worker")

    monkeypatch.setattr(api.dialog, "Thread", unexpected_worker)
    monkeypatch.setattr(catalog, "Thread", unexpected_worker)
    dialog, _config = _open_cached(api)
    try:
        assert f"{api.dialog.__package__}.{runtime}" not in sys.modules
        assert dialog._calculation_cancel is None
    finally:
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)


def test_needed_width_job_imports_provider_and_copies_cached_metadata(
    lazy_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferring provider imports must preserve the real batch's copied handoff."""
    api = lazy_api
    cache_api = importlib.import_module(
        f"{api.dialog.__package__}.calculator_config_cache"
    )
    cached = cache_api.CalculatorConfigCache(
        models=({"value": "cached"},),
        copper=({"value": "copper"},),
        coating=({"value": "coating"},),
        limits=({"value": "limits"},),
        checked_at_utc=cache_api.calculator_config_checked_at(),
    )
    workers: list[Callable[[], None]] = []
    handoffs = []
    solves = []
    original_handoff = api.dialog._calculator_configuration_handoff

    class Worker:
        def __init__(self, *, target: Callable[[], None], **_kwargs: Any) -> None:
            self.target = target

        def start(self) -> None:
            workers.append(self.target)

    def handoff(*args: Any, **kwargs: Any) -> Any:
        result = original_handoff(*args, **kwargs)
        handoffs.append(result)
        return result

    def calculate(_stackup: Any, spec: Any, layer: str, **_kwargs: Any) -> Any:
        solves.append((spec.spec_id, layer))
        return next(
            result
            for result in original.width_results
            if (result.spec_id, result.layer) == (spec.spec_id, layer)
        )

    monkeypatch.setattr(api.dialog, "Thread", Worker)
    monkeypatch.setattr(api.dialog, "_calculator_configuration_handoff", handoff)
    dialog, original = _open_cached(
        api, load_calculator_config=lambda: cached, calculate_width=calculate
    )
    try:
        assert f"{api.dialog.__package__}.jlcpcb_calculator" not in sys.modules
        assert not workers and not handoffs
        dialog.session.replace_calculation_context(original.stackup, ())
        dialog._schedule_width_refresh()
        assert len(workers) == len(handoffs) == 1
        provider = sys.modules[f"{api.dialog.__package__}.jlcpcb_calculator"]
        configuration, _publish = handoffs[0]
        assert isinstance(configuration, provider.ProviderConfiguration)
        cached.models[0]["value"] = "changed after scheduling"
        assert configuration.models[0]["value"] == "cached"
        workers[0]()
        api.drain()
        assert len(solves) == len(original.width_results)
        assert dialog.session.config.width_results == original.width_results
        assert dialog._calculation_cancel is None
    finally:
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)


@pytest.mark.parametrize("failure", ["worker_import", "worker_start"])
@pytest.mark.parametrize(
    "error_type", [ImportError, RuntimeError, OSError, SyntaxError]
)
def test_catalog_dependency_failure_releases_reservation_and_recovers_on_reopen(
    lazy_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    error_type: type[Exception],
) -> None:
    """A missing transport must report failure without stranding another dialog."""
    api = lazy_api
    package = api.dialog.__package__
    catalog = importlib.import_module(f"{package}.catalog_controller")
    cache_api = importlib.import_module(f"{package}.catalog_cache")
    refresh = importlib.import_module(f"{package}.catalog_refresh")
    _snapshot, config = routed_draft(SimpleNamespace(model=api.model))
    cache = cache_api.CatalogCache((config.stackup,))
    gate = refresh.CatalogRefreshGate()
    workers = []
    saves = []

    class Worker:
        def __init__(
            self, *, target: Callable[..., None], args: tuple[Any, ...], **_kwargs: Any
        ) -> None:
            self.run = lambda: target(*args)

        def start(self) -> None:
            if failure == "worker_start":
                raise RuntimeError("Worker could not start")
            workers.append(self.run)

    monkeypatch.setattr(catalog, "Thread", Worker)
    controller = catalog.CatalogController(4, cache, refresh_gate=gate)
    try:
        with monkeypatch.context() as patch:
            _missing_provider(patch, package, "jlcpcb_stackups", error_type)
            controller.ensure_started()
            if workers:
                workers.pop()()
            api.drain()
        assert not controller.state.loading
        assert controller.state.error
        assert controller._cancel is None
        assert controller.state.cache == cache
        expected = (
            "Worker could not start"
            if failure == "worker_start"
            else "Provider dependency unavailable"
        )
        assert expected in controller.state.message
        controller.close()
        failure = "recovered"
        reopened = catalog.CatalogController(
            4,
            cache,
            refresh_gate=gate,
            fetch_catalog=lambda *_args, **_kwargs: (config.stackup,),
            save_cache=lambda _count, updated: saves.append(updated),
        )
        try:
            reopened.ensure_started()
            assert len(workers) == 1
            workers.pop()()
            api.drain()
            assert not reopened.state.loading and not reopened.state.error
            assert len(saves) == 1
        finally:
            reopened.close()
    finally:
        controller.close()


def test_draft_provider_import_failure_clears_inflight_and_schedules_retry(
    lazy_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed cached-metadata import must complete through the normal worker path."""
    from tests.test_impedance_draft_dialog import _board, _intent
    from tests.test_impedance_spec_width_hint import _stackup

    api = lazy_api
    package = api.dialog.__package__
    cache_api = importlib.import_module(f"{package}.calculator_config_cache")
    cache = cache_api.CalculatorConfigCache(
        models=({"value": "model"},),
        copper=({"value": "copper"},),
        coating=({"value": "coating"},),
        limits=({"value": "limits"},),
        checked_at_utc=cache_api.calculator_config_checked_at(),
    )
    workers = []

    class Worker:
        def __init__(self, *, target: Callable[[], None], **_kwargs: Any) -> None:
            self.run = target

        def start(self) -> None:
            workers.append(self.run)

    monkeypatch.setattr(api.dialog, "Thread", Worker)
    dialog = api.dialog.SpecificationDialog(
        None,
        _board(api),
        _intent(api).specifications[0],
        stackup=_stackup(api),
        load_calculator_config=lambda: cache,
    )
    try:
        dialog._draft_calc_timer.Stop()
        with monkeypatch.context() as patch:
            _missing_provider(patch, package, "jlcpcb_calculator")
            dialog._on_draft_calc_timer(SimpleNamespace())
            assert len(workers) == 1
            workers.pop()()
            api.drain()
        assert not dialog._draft_calc_inflight
        assert dialog._draft_calc_cancel is None
        assert not dialog._draft_slow_timer.IsRunning()
        assert dialog._draft_retry_timer.IsRunning()
        assert dialog.width_results[0].status == "unavailable"
        assert "dependency unavailable" in dialog.width_results[0].message
        dialog._on_draft_retry_timer(SimpleNamespace())
        assert dialog._draft_calc_timer.IsRunning()
        dialog._on_draft_calc_timer(SimpleNamespace())
        assert len(workers) == 1
    finally:
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)
