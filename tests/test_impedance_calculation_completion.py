"""Keep calculator transport failures out of the saved, approved result batch.

Use the real dialog constructor, Calculate/Save handlers and calculator failure
contract with stateful wx controls. Worker dispatch is queued deterministically;
native event delivery and persistence are covered separately by the smoke test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

import pytest

from .test_impedance_automatic_rows import _show
from .test_impedance_draft_dialog import (
    _board,
    _intent,
    _open,
    constructor_api,  # noqa: F401 -- dependency of draft_api
    draft_api as _draft_api,
)

draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog
    from impedance.model import BoardSnapshot, Config, Specification
    from impedance.stackup_model import Stackup, WidthResult


@pytest.fixture
def workers(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> list[Callable[[], None]]:
    """Run production worker bodies only when the test dispatches their queue."""
    pending: list[Callable[[], None]] = []

    class QueuedThread:
        def __init__(
            self, *, target: Callable[[], None], name: str, daemon: bool
        ) -> None:
            self.target = target

        def start(self) -> None:
            pending.append(self.target)

    monkeypatch.setattr(draft_api.dialog, "Thread", QueuedThread)
    monkeypatch.setattr(
        draft_api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    return pending


def _result(
    api: SimpleNamespace,
    stackup: Stackup,
    spec: Specification,
    layer: str,
    *,
    status: str = "success",
    width_nm: int = 150_000,
) -> WidthResult:
    """Return a validated provider result for the exact synthetic construction."""
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    return model.WidthResult(
        spec_id=spec.spec_id,
        layer=layer,
        input_digest=model.calculation_fingerprint(stackup, spec, layer),
        status=status,
        target_width_nm=width_nm if status == "success" else None,
        calculated_at_utc="2026-09-23T00:00:00.000000Z",
        model="CoatedMicrostrip1B" if status == "success" else "",
        calculation_digest="a" * 64 if status == "success" else "",
        message="" if status == "success" else "Sample provider failure reason.",
    )


def _approved_board(
    api: SimpleNamespace,
    *,
    prior_results: bool,
    two_layers: bool = False,
    width_results: Optional[tuple[WidthResult, ...]] = None,
) -> tuple[Config, BoardSnapshot]:
    """Build approved synthetic intent with optional nominal-width cache rows."""
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = model.Stackup(
        "sample",
        "Sample two-layer construction",
        2,
        "0.324",
        "1",
        "",
        calculator_id="sample-calculator",
        layers=(
            model.StackupLayer("L1", "copper", "0.035"),
            model.StackupLayer("Core", "core", "0.254", "FR4", "4.2"),
            model.StackupLayer("L2", "copper", "0.035"),
        ),
    )
    board = _board(api)
    config = replace(_intent(api), stackup=stackup)
    if two_layers:
        spec = replace(
            config.specifications[0],
            layer_settings=(
                api.model.LayerSettings("F.Cu", ("B.Cu",)),
                api.model.LayerSettings("B.Cu", ("F.Cu",)),
            ),
        )
        config = replace(config, specifications=(spec,))
        board = replace(
            board,
            traces=board.traces
            + (replace(board.traces[0], trace_id="back", layer="B.Cu"),),
        )
    if width_results is not None:
        config = replace(config, width_results=width_results)
    elif prior_results:
        config = replace(
            config,
            width_results=tuple(
                _result(api, stackup, spec, settings.layer)
                for spec in config.specifications
                for settings in spec.layer_settings
            ),
        )
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    session.approve()
    return session.save_result(), board


def _open_cached(
    api: SimpleNamespace,
    workers: list[Callable[[], None]],
    *,
    prior_results: bool,
    two_layers: bool = False,
    width_results: Optional[tuple[WidthResult, ...]] = None,
    load_config: Optional[Config] = None,
    board: Optional[BoardSnapshot] = None,
    calculate_width_callback: Optional[Callable[..., WidthResult]] = None,
) -> tuple[ImpedanceDialog, dict[str, Config]]:
    """Construct and show without cancelling the show-time width schedule.

    Unlike `_setup`, this leaves any CallAfter refresh intact so open-path cache
    assertions can observe first paint and show-time worker decisions. Install any
    calculator callback before show so the captured schedule closure uses it.
    """
    if load_config is None:
        load_config, board = _approved_board(
            api,
            prior_results=prior_results,
            two_layers=two_layers,
            width_results=width_results,
        )
    assert board is not None
    persisted = {"config": load_config}
    dialog = _open(
        api, persisted["config"], board, lambda saved: persisted.update(config=saved)
    )

    def pending(*_args: Any, **_kwargs: Any) -> WidthResult:
        raise AssertionError("Install calculate_width_callback before refreshing.")

    dialog.calculate_width_callback = (
        calculate_width_callback if calculate_width_callback is not None else pending
    )
    _show(api, dialog)
    assert dialog.session.approved
    return dialog, persisted


def _setup(
    api: SimpleNamespace,
    workers: list[Callable[[], None]],
    *,
    prior_results: bool,
    two_layers: bool = False,
) -> tuple[ImpedanceDialog, dict[str, Config]]:
    """Open approved intent, then cancel show-time refresh for mid-session tests."""
    dialog, persisted = _open_cached(
        api, workers, prior_results=prior_results, two_layers=two_layers
    )
    dialog._cancel_calculation()
    workers.clear()
    return dialog, persisted


def _layer_messages(dialog: ImpedanceDialog, layer: str) -> list[str]:
    """Return Width comparisons Status cells for one copper layer."""
    return [
        row[5]
        for row in dialog.width_comparisons.table_rows
        if row[0].endswith(f"/ {layer}") or row[0].endswith(layer)
    ]


def _assert_no_waiting(dialog: ImpedanceDialog, *, layer: Optional[str] = None) -> None:
    """Fail if any (or one layer's) comparison Status still shows JLCPCB waiting."""
    messages = (
        _layer_messages(dialog, layer)
        if layer is not None
        else _comparison_messages(dialog)
    )
    assert messages
    assert all("waiting for JLCPCB" not in message for message in messages)


def _assert_cached_comparison_status(dialog: ImpedanceDialog) -> None:
    """Fully cached successes must show only Differs/matches, never fetch language."""
    messages = _comparison_messages(dialog)
    assert messages
    banned = ("JLCPCB", "waiting", "pending", "refreshing")
    for message in messages:
        lowered = message.lower()
        assert all(token.lower() not in lowered for token in banned), message
        assert (
            "Differs from nominal" in message or "matches the saved nominal" in message
        ), message
    assert dialog._width_refresh_alert == ""


def _calculate(
    api: SimpleNamespace,
    dialog: ImpedanceDialog,
    workers: list[Callable[[], None]],
    *,
    force: bool = True,
) -> None:
    """Run one quiet background refresh through the production worker path."""
    before = dialog.session.config
    dialog._schedule_width_refresh(force=force)
    assert dialog._calculation_cancel is not None
    assert dialog.session.config == before
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert dialog._calculation_cancel is None


@pytest.mark.parametrize("prior_results", [False, True])
def test_real_unavailable_result_retains_config_approval_and_saved_state(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    prior_results: bool,
) -> None:
    """A normal calculator failure return is as atomic as a raised exception."""
    api = draft_api
    dialog, persisted = _setup(api, workers, prior_results=prior_results)
    before = dialog.session.config
    saved = persisted["config"]
    module = importlib.import_module(api.dialog.__package__ + ".jlcpcb_calculator")
    requests: list[str] = []

    def reject(path: str, _payload: dict[str, object], **kwargs: Any) -> dict[str, Any]:
        requests.append(path)
        return {"success": False, "result": "failed"}

    calculator = module.JlcpcbCalculator(post=reject, connector=lambda: None)
    dialog.calculate_width_callback = calculator.calculate_width

    _calculate(api, dialog, workers)

    assert requests
    assert dialog.session.config == before
    assert dialog.session.approved
    assert persisted["config"] == saved
    assert "did not provide its impedance models" in dialog.status.GetValue()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    api.drain()
    # Saving may persist the image view recorded before calculation started.
    assert persisted["config"] == before
    reopened = _open(api, persisted["config"], dialog.session.snapshot)
    assert reopened.session.config == before
    assert reopened.session.approved


@pytest.mark.parametrize("failure_status", ["unavailable", "error", "pending"])
def test_mixed_failure_keeps_successful_layers_and_clears_approval(
    draft_api: SimpleNamespace,
    workers: list[Callable[[], None]],
    failure_status: str,
) -> None:
    """A mixed batch must cache successes so the next open can skip those jobs."""
    api = draft_api
    dialog, persisted = _setup(api, workers, prior_results=True, two_layers=True)
    saved = persisted["config"]
    delivered: list[WidthResult] = []

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        result = _result(
            api,
            stackup,
            spec,
            layer,
            width_nm=250_000,
            status=failure_status if layer == "B.Cu" else "success",
        )
        delivered.append(result)
        return result

    dialog.calculate_width_callback = calculate
    _calculate(api, dialog, workers)

    assert [result.status for result in delivered] == ["success", failure_status]
    assert {
        result.layer: result.status for result in dialog.session.config.width_results
    } == {
        "F.Cu": "success",
        "B.Cu": failure_status,
    }
    assert any(
        result.layer == "F.Cu" and result.target_width_nm == 250_000
        for result in dialog.session.config.width_results
    )
    assert not dialog.session.approved
    assert dialog.session.config.reviewed_digest == ""
    assert persisted["config"] == saved
    assert "Sample provider failure reason" in dialog.status.GetValue()
    _assert_no_waiting(dialog, layer="F.Cu")

    delivered.clear()
    dialog.session.invalidate_review()
    dialog._refresh_rows(dialog.session.snapshot)
    _calculate(api, dialog, workers, force=False)
    assert [result.layer for result in delivered] == ["B.Cu"]


def test_unsupported_layer_is_a_complete_displayable_result(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Known unsupported geometry must remain distinguishable from a service outage."""
    api = draft_api
    dialog, persisted = _setup(api, workers, prior_results=False, two_layers=True)
    before = persisted["config"]
    delivered: list[WidthResult] = []

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        result = _result(
            api,
            stackup,
            spec,
            layer,
            status="unsupported" if layer == "B.Cu" else "success",
        )
        delivered.append(result)
        return result

    dialog.calculate_width_callback = calculate
    _calculate(api, dialog, workers)

    assert set(dialog.session.config.width_results) == set(delivered)
    assert not dialog.session.approved
    assert "Nominal widths updated" in dialog.status.GetValue()
    assert persisted["config"] == before


def _comparison_messages(dialog: ImpedanceDialog) -> list[str]:
    """Read the Width comparisons Status column from the stateful list double."""
    return [row[5] for row in dialog.width_comparisons.table_rows]


def test_open_with_cached_successes_never_waits_or_starts_workers(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Digest-current successes must paint immediately and skip show-time JLCPCB work."""
    api = draft_api
    dialog, _persisted = _open_cached(api, workers, prior_results=True)
    _assert_cached_comparison_status(dialog)
    assert dialog._calculation_cancel is None
    assert workers == []


def test_open_keeps_cached_success_stable_while_other_layer_refreshes(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """F.Cu success must not flash waiting when show-time only refreshes B.Cu."""
    api = draft_api
    base, board = _approved_board(api, prior_results=False, two_layers=True)
    stackup = base.stackup
    assert stackup is not None
    spec = base.specifications[0]
    # Analysis digests include width_results; approve only after the mixed cache exists.
    session = api.dialog.ReviewSession(
        replace(
            base,
            reviewed_digest="",
            included_section_ids=(),
            width_results=(
                _result(api, stackup, spec, "F.Cu"),
                _result(api, stackup, spec, "B.Cu", status="unavailable"),
            ),
        ),
        board,
    )
    session.refresh()
    session.approve()
    config = session.save_result()
    calls: list[str] = []

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        calls.append(layer)
        return _result(api, stackup, spec, layer, width_nm=250_000)

    dialog, _persisted = _open_cached(
        api,
        workers,
        prior_results=False,
        two_layers=True,
        load_config=config,
        board=board,
        calculate_width_callback=calculate,
    )
    _assert_no_waiting(dialog, layer="F.Cu")
    assert dialog._calculation_cancel is not None
    assert len(workers) == 1
    dialog._populate_width_comparisons()
    _assert_no_waiting(dialog, layer="F.Cu")
    workers.pop()()
    api.drain()
    assert calls == ["B.Cu"]
    _assert_no_waiting(dialog, layer="F.Cu")
    _assert_no_waiting(dialog, layer="B.Cu")


def test_save_reopen_cached_successes_keep_digests_and_skip_refetch(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Persistence must not drift fingerprints into a waiting flash on reopen."""
    api = draft_api
    first, persisted = _open_cached(api, workers, prior_results=True)
    board = first.session.snapshot
    saved = api.model.Config.from_dict(persisted["config"].to_dict())
    first.Destroy()
    workers.clear()

    dialog, _reopened = _open_cached(
        api,
        workers,
        prior_results=True,
        load_config=saved,
        board=board,
    )
    _assert_cached_comparison_status(dialog)
    assert dialog._calculation_cancel is None
    assert workers == []
    jobs = dialog._width_refresh_jobs(dialog.session.config)
    assert jobs is not None
    assert dialog._width_results_current(dialog.session.config, jobs)


def test_stackup_change_failure_stops_waiting_and_shows_provider_reason(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """After inputs change, a finished JLCPCB failure must not leave comparisons pending."""
    api = draft_api
    dialog, persisted = _setup(api, workers, prior_results=True)
    new_stackup = replace(
        dialog.session.config.stackup,
        stackup_id="changed-sample",
        name="Changed sample construction",
    )
    dialog.session.replace_calculation_context(
        new_stackup, dialog.session.config.width_results
    )
    assert dialog._refresh_rows(dialog.session.snapshot)
    assert any(
        "waiting for JLCPCB" in message or "inputs changed" in message
        for message in _comparison_messages(dialog)
    )
    reason = "JLCPCB could not calculate a width for these inputs."

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        return _result(api, stackup, spec, layer, status="unavailable", width_nm=0)

    # Override the canned unavailable message from _result.
    original = calculate

    def calculate_with_reason(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        result = original(stackup, spec, layer, **kwargs)
        return replace(result, message=reason)

    dialog.calculate_width_callback = calculate_with_reason
    _calculate(api, dialog, workers)

    messages = _comparison_messages(dialog)
    assert messages
    assert all("waiting for JLCPCB" not in message for message in messages)
    assert all("inputs changed" not in message for message in messages)
    assert any(reason in message for message in messages)
    assert reason in dialog.status.GetValue()
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    current = {
        model.calculation_fingerprint(
            dialog.session.config.stackup, spec, settings.layer
        )
        for spec in dialog.session.config.specifications
        for settings in spec.layer_settings
    }
    assert any(
        result.status == "unavailable" and result.input_digest in current
        for result in dialog.session.config.width_results
    )
    assert not dialog.session.approved
    assert persisted["config"] != dialog.session.config


def test_switching_stackups_reuses_cached_nominal_widths(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """Returning to a prior stackup must not call JLCPCB again for the same geometry."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True)
    first = dialog.session.config.stackup
    assert first is not None
    second = replace(
        first,
        stackup_id="other-sample",
        name="Other sample construction",
        thickness_mm="0.400",
    )
    calls: list[str] = []

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        calls.append(stackup.stackup_id)
        return _result(
            api,
            stackup,
            spec,
            layer,
            width_nm=250_000 if stackup.stackup_id == second.stackup_id else 150_000,
        )

    dialog.calculate_width_callback = calculate

    dialog.session.replace_calculation_context(
        second, dialog.session.config.width_results
    )
    assert dialog._refresh_rows(dialog.session.snapshot)
    _calculate(api, dialog, workers)
    assert calls == [second.stackup_id]
    assert len(dialog.session.config.width_results) == 2

    calls.clear()
    dialog.session.replace_calculation_context(
        first, dialog.session.config.width_results
    )
    assert dialog._refresh_rows(dialog.session.snapshot)
    _assert_cached_comparison_status(dialog)
    before = dialog.session.config.width_results
    dialog._schedule_width_refresh(force=False)
    assert dialog._calculation_cancel is None
    assert workers == []
    assert calls == []
    assert dialog.session.config.width_results == before
    _assert_cached_comparison_status(dialog)
    jobs = dialog._width_refresh_jobs(dialog.session.config)
    assert jobs is not None
    assert dialog._width_results_current(dialog.session.config, jobs)


def test_returning_to_fully_cached_stackup_shows_differs_without_fetch_language(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """A→B→A with all successes on A must repaint Differs before any schedule work."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True, two_layers=True)
    first = dialog.session.config.stackup
    assert first is not None
    second = replace(
        first,
        stackup_id="other-sample",
        name="Other sample construction",
        thickness_mm="0.400",
    )

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        return _result(api, stackup, spec, layer, width_nm=250_000)

    dialog.calculate_width_callback = calculate
    dialog.session.replace_calculation_context(
        second, dialog.session.config.width_results
    )
    assert dialog._refresh_rows(dialog.session.snapshot)
    _calculate(api, dialog, workers, force=False)
    dialog.session.replace_calculation_context(
        first, dialog.session.config.width_results
    )
    assert dialog._refresh_rows(dialog.session.snapshot)
    _assert_cached_comparison_status(dialog)
    dialog._schedule_width_refresh(force=False)
    assert dialog._calculation_cancel is None
    assert workers == []
    _assert_cached_comparison_status(dialog)


def test_cached_success_is_not_refetched_when_other_jobs_need_refresh(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """A current nominal width must stay cached while unresolved jobs refresh."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True, two_layers=True)
    calls: list[str] = []

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        calls.append(layer)
        return _result(
            api,
            stackup,
            spec,
            layer,
            status="unavailable" if layer == "B.Cu" else "success",
            width_nm=250_000,
        )

    dialog.calculate_width_callback = calculate
    dialog.session.invalidate_review()
    _calculate(api, dialog, workers, force=True)
    assert calls == ["F.Cu", "B.Cu"]
    assert any(
        result.layer == "F.Cu" and result.status == "success"
        for result in dialog.session.config.width_results
    )
    dialog._refresh_rows(dialog.session.snapshot)
    f_cu_messages = [
        row[5]
        for row in dialog.width_comparisons.table_rows
        if row[0].endswith("/ F.Cu") or row[0].endswith("F.Cu")
    ]
    b_cu_messages = [
        row[5]
        for row in dialog.width_comparisons.table_rows
        if row[0].endswith("/ B.Cu") or row[0].endswith("B.Cu")
    ]
    assert f_cu_messages
    assert all("waiting for JLCPCB" not in message for message in f_cu_messages)
    assert b_cu_messages
    assert all("Nominal width unavailable" in message for message in b_cu_messages)

    jobs = dialog._width_refresh_jobs(dialog.session.config)
    assert jobs is not None
    needing = dialog._width_jobs_needing_refresh(dialog.session.config, jobs)
    assert [layer for _spec, layer in needing] == ["B.Cu"]

    calls.clear()
    dialog._schedule_width_refresh(force=False)
    assert dialog._calculation_cancel is not None
    assert len(workers) == 1
    # Before the worker finishes, the cached F.Cu comparison must not flip to waiting,
    # and the settled B.Cu failure text must not flash waiting either.
    dialog._populate_width_comparisons()
    f_cu_messages = [
        row[5]
        for row in dialog.width_comparisons.table_rows
        if row[0].endswith("/ F.Cu") or row[0].endswith("F.Cu")
    ]
    assert f_cu_messages
    assert all("waiting for JLCPCB" not in message for message in f_cu_messages)
    b_cu_messages = [
        row[5]
        for row in dialog.width_comparisons.table_rows
        if row[0].endswith("/ B.Cu") or row[0].endswith("B.Cu")
    ]
    assert b_cu_messages
    assert all("waiting for JLCPCB" not in message for message in b_cu_messages)
    assert all("Nominal width unavailable" in message for message in b_cu_messages)
    workers.pop()()
    api.drain()
    assert calls == ["B.Cu"]
    assert any(
        result.layer == "F.Cu"
        and result.status == "success"
        and result.target_width_nm == 250_000
        for result in dialog.session.config.width_results
    )


def test_stackup_change_clears_stale_failure_status_while_refreshing(
    draft_api: SimpleNamespace, workers: list[Callable[[], None]]
) -> None:
    """A prior calculator rejection must not linger after the stackup changes."""
    api = draft_api
    dialog, _persisted = _setup(api, workers, prior_results=True)
    first = dialog.session.config.stackup
    assert first is not None
    second = replace(
        first,
        stackup_id="other-sample",
        name="Other sample construction",
        thickness_mm="0.400",
    )
    failure = "Nominal width unavailable for F.Cu: JLCPCB could not calculate a width"
    dialog._width_refresh_alert = failure
    dialog._populate_sections()
    assert failure in dialog.status.GetValue()

    dialog._clear_width_refresh_alert()
    dialog.session.replace_calculation_context(
        second, dialog.session.config.width_results
    )
    assert dialog._refresh_rows(dialog.session.snapshot)
    assert failure not in dialog.status.GetValue()
    assert all(
        "could not calculate a width" not in message
        for message in _comparison_messages(dialog)
    )

    def calculate(
        stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        return _result(api, stackup, spec, layer, width_nm=250_000)

    dialog.calculate_width_callback = calculate
    dialog._schedule_width_refresh(force=False)
    assert dialog._width_refresh_alert == ""
    assert dialog._calculation_cancel is not None
    dialog._populate_sections()
    dialog._populate_width_comparisons()
    assert "could not calculate a width" not in dialog.status.GetValue()
    assert all(
        "could not calculate a width" not in message
        for message in _comparison_messages(dialog)
    )
    workers.pop()()
    api.drain()
    assert any(
        "Differs from nominal" in message or "matches the saved nominal" in message
        for message in _comparison_messages(dialog)
    )
