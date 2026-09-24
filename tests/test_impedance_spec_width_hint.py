"""Spec-dialog width hint: one pending message and live draft calculation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from .test_impedance_dialog import (
    _snapshot,
    constructor_api,  # noqa: F401 -- shared real-constructor fixture
)
from .test_impedance_netclass_dialog import apply_layer, choose_class, view_all_rows

if TYPE_CHECKING:
    from impedance.dialog import SpecificationDialog
    from impedance.model import BoardSnapshot, Specification
    from impedance.stackup_model import Stackup, WidthResult


@pytest.fixture
def workers(
    constructor_api: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> list[Callable[[], None]]:
    """Queue production Thread bodies for deterministic dispatch."""
    pending: list[Callable[[], None]] = []

    class QueuedThread:
        def __init__(
            self, *, target: Callable[[], None], name: str, daemon: bool
        ) -> None:
            self.target = target

        def start(self) -> None:
            pending.append(self.target)

    monkeypatch.setattr(constructor_api.dialog, "Thread", QueuedThread)
    return pending


def _stackup(api: SimpleNamespace) -> Stackup:
    """Build a calculator-supported two-layer construction for draft solves."""
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    return model.Stackup(
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


def _ninety_ohm_inputs(
    api: SimpleNamespace,
) -> tuple[Stackup, BoardSnapshot, Specification, Specification]:
    """Keep the target unchanged while correcting single-ended to differential."""
    snapshot = replace(
        _snapshot(api),
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )
    single = api.model.Specification(
        spec_id="usb-90",
        label="USB",
        target_ohms="90",
        kind="single_ended",
        net_class="USB",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    differential = replace(
        single,
        kind="differential",
        layer_settings=(replace(single.layer_settings[0], spacing_nm=200_000),),
    )
    return _stackup(api), snapshot, single, differential


def _successful_width(
    api: SimpleNamespace, stackup: Stackup, spec: Specification
) -> WidthResult:
    """Represent a saved solve for the exact corrected fabrication inputs."""
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    return model.WidthResult(
        spec_id=spec.spec_id,
        layer="F.Cu",
        input_digest=model.calculation_fingerprint(stackup, spec, "F.Cu"),
        status="success",
        target_width_nm=150_000,
        calculated_at_utc="2026-09-23T00:00:00.000000Z",
        model="DifferentialMicrostrip1B",
        calculation_digest="b" * 64,
    )


def _correct_to_differential(api: SimpleNamespace, dialog: SpecificationDialog) -> None:
    """Dispatch the same type and spacing events as correcting the form by hand."""
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    dialog.spacing.SetValue("0.2")
    dialog.spacing.emit(api.wx.EVT_TEXT)
    assert dialog.target.GetValue() == "90"


@pytest.mark.parametrize("status", ["success", "unsupported", "unavailable"])
def test_late_width_result_updates_current_layer_approval_truthfully(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
    status: str,
) -> None:
    """A late provider result cannot leave Approved beside a stale fingerprint."""
    api = constructor_api
    stackup, board, single, _differential = _ninety_ohm_inputs(api)
    spec = replace(single, target_ohms="50")
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")

    def calculate(
        calc_stackup: Stackup, current: Specification, layer: str, **_kwargs: Any
    ) -> WidthResult:
        digest = model.calculation_fingerprint(calc_stackup, current, layer)
        if status == "success":
            return _successful_width(api, calc_stackup, current)
        return model.WidthResult(
            current.spec_id, layer, digest, status, message="Provider reason"
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        board,
        spec,
        preview=api.preview,
        stackup=stackup,
        calculate_width=calculate,
    )
    dialog.on_screen = True
    api.drain()
    view_all_rows(api, dialog)
    apply_layer(api, dialog)
    assert dialog._layer_drafts["F.Cu"].confirmed
    history = dialog.review_tracking
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert dialog._layer_drafts["F.Cu"].confirmed is (status == "unavailable")
    assert dialog.review_tracking.layers == history.layers
    if status == "unavailable":
        assert dialog.width_results == ()
        assert dialog._draft_retry_timer.IsRunning()
        assert "Provider reason" in dialog.width_hint.GetLabel()
    else:
        assert "needs review" in dialog._layer_status("F.Cu").lower()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK


def test_pending_width_message_appears_once_for_multiple_actuals(
    constructor_api: SimpleNamespace,  # noqa: F811
) -> None:
    """Do not repeat the same calculator-pending sentence for every neckdown width."""
    api = constructor_api
    snapshot = _snapshot(api)
    first = snapshot.traces[0]
    snapshot = replace(
        snapshot,
        traces=snapshot.traces + (replace(first, trace_id="neck", width_nm=100_000),),
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )
    dialog = api.dialog.SpecificationDialog(
        None, snapshot, stackup=_stackup(api), width_results=()
    )
    choose_class(api, dialog, "USB")
    label = dialog.width_hint.GetLabel()
    assert label.count("waiting for JLCPCB's calculator") == 1
    assert "Actual:" in label


def test_open_with_cached_success_skips_waiting_and_fetch(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """Digest-current saved widths must paint immediately without a draft JLCPCB worker."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    snapshot = replace(
        _snapshot(api),
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )
    spec = api.model.Specification(
        spec_id="usb-cached",
        label="USB",
        target_ohms="50",
        kind="single_ended",
        net_class="USB",
        layer_settings=(api.model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    result = model.WidthResult(
        spec_id=spec.spec_id,
        layer="F.Cu",
        input_digest=model.calculation_fingerprint(stackup, spec, "F.Cu"),
        status="success",
        target_width_nm=150_000,
        calculated_at_utc="2026-09-23T00:00:00.000000Z",
        model="CoatedMicrostrip1B",
        calculation_digest="a" * 64,
    )

    def calculate(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("Cached draft widths must not call JLCPCB.")

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        specification=spec,
        stackup=stackup,
        width_results=(result,),
        calculate_width=calculate,
    )
    assert "waiting for JLCPCB" not in dialog.width_hint.GetLabel()
    labels = [
        item.GetLabel() for item in dialog.width_rows.items if hasattr(item, "GetLabel")
    ]
    assert labels[:6] == [
        "Actual:",
        "0.15 mm",
        "(5.91 mil)",
        "Nominal:",
        "0.15 mm",
        "(5.91 mil)",
    ]
    dialog._draft_calc_timer.Stop()
    dialog._schedule_draft_width_calculation()
    assert workers == []
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert workers == []
    assert dialog._draft_calc_cancel is None


def test_draft_calculation_updates_width_hint(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """A ready draft layer must receive a nominal width without waiting for OK."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    snapshot = _snapshot(api)
    snapshot = replace(
        snapshot,
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )

    def calculate(
        calc_stackup: object, spec: object, layer: str, **kwargs: Any
    ) -> object:
        digest = model.calculation_fingerprint(calc_stackup, spec, layer)
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=digest,
            status="success",
            target_width_nm=150_000,
            calculated_at_utc="2026-09-23T00:00:00.000000Z",
            model="CoatedMicrostrip1B",
            calculation_digest="a" * 64,
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        stackup=stackup,
        width_results=(),
        calculate_width=calculate,
    )
    choose_class(api, dialog, "USB")
    assert "waiting for JLCPCB's calculator" in dialog.width_hint.GetLabel()
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert "waiting for JLCPCB's calculator" not in dialog.width_hint.GetLabel()
    labels = [
        item.GetLabel() for item in dialog.width_rows.items if hasattr(item, "GetLabel")
    ]
    assert labels[:9] == [
        "Actual:",
        "0.15 mm",
        "(5.91 mil)",
        "Nominal:",
        "0.15 mm",
        "(5.91 mil)",
        "Difference:",
        "0 mm",
        "(0.00 mil)",
    ]
    difference_mm = dialog.width_rows.items[7]
    difference_mil = dialog.width_rows.items[8]
    assert difference_mm.GetForegroundColour()[:3] == (35, 135, 60)
    assert difference_mil.GetForegroundColour()[:3] == (35, 135, 60)
    assert "Calculation model:" in dialog.width_hint.GetLabel()
    assert dialog.width_hint.wrap_width == dialog._form_value_wrap_width()
    assert dialog.width_hint.wrap_width >= 200
    assert dialog._draft_calc_cancel is None
    assert not dialog.calculator_link.IsShown()
    assert dialog._draft_calc_alert == ""
    assert getattr(dialog.form, "fit_inside_count", 0) >= 1


def test_draft_calculation_persists_settled_result_immediately(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """A draft-layer solve reaches the cache before OK, so Cancel cannot lose it."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    snapshot = replace(
        _snapshot(api),
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )
    calls = {"count": 0}

    def calculate(
        calc_stackup: object, spec: object, layer: str, **kwargs: Any
    ) -> object:
        calls["count"] += 1
        digest = model.calculation_fingerprint(calc_stackup, spec, layer)
        if calls["count"] == 1:
            return model.WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=digest,
                status="unavailable",
                message="Could not receive JLCPCB's calculator result.",
            )
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=digest,
            status="success",
            target_width_nm=150_000,
            calculated_at_utc="2026-09-23T00:00:00.000000Z",
            model="CoatedMicrostrip1B",
            calculation_digest="a" * 64,
        )

    saved: list[object] = []
    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        stackup=stackup,
        width_results=(),
        calculate_width=calculate,
        save_width_result=saved.append,
    )
    choose_class(api, dialog, "USB")
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()
    api.drain()
    assert saved == []
    dialog._draft_retry_timer.Stop()
    dialog._on_draft_retry_timer(SimpleNamespace())
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()
    api.drain()

    assert len(saved) == 1
    assert saved[0].status == "success"
    assert saved[0] in dialog.width_results


def test_draft_unsupported_result_shows_reason_without_retrying(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """A deterministic rejection is shown once; the draft does not arm a retry."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    snapshot = replace(
        _snapshot(api),
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )
    calls = {"count": 0}

    def calculate(
        calc_stackup: object, spec: object, layer: str, **kwargs: Any
    ) -> object:
        calls["count"] += 1
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=model.calculation_fingerprint(calc_stackup, spec, layer),
            status="unsupported",
            message="JLCPCB's calculator rejected these inputs: the target "
            "impedance is not achievable within its trace-width range.",
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        stackup=stackup,
        width_results=(),
        calculate_width=calculate,
    )
    choose_class(api, dialog, "USB")
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()
    api.drain()

    assert calls["count"] == 1
    assert "not achievable" in dialog.width_hint.GetLabel()
    assert "waiting for JLCPCB" not in dialog.width_hint.GetLabel()
    assert not dialog._draft_retry_timer.IsRunning()
    # A later edit-driven reschedule must reuse the settled result, not re-solve.
    dialog._schedule_draft_width_calculation()
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert workers == []
    assert calls["count"] == 1


def test_draft_comparison_shows_one_set_for_multiple_actual_widths(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """Neckdowns must not repeat Actual/Nominal/Difference for the same layer."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    snapshot = _snapshot(api)
    first = snapshot.traces[0]
    snapshot = replace(
        snapshot,
        traces=snapshot.traces
        + (
            replace(first, trace_id="neck-a", width_nm=100_000),
            replace(first, trace_id="neck-b", width_nm=100_000),
        ),
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )

    def calculate(
        calc_stackup: object, spec: object, layer: str, **kwargs: Any
    ) -> object:
        digest = model.calculation_fingerprint(calc_stackup, spec, layer)
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=digest,
            status="success",
            target_width_nm=150_000,
            calculated_at_utc="2026-09-23T00:00:00.000000Z",
            model="CoatedMicrostrip1B",
            calculation_digest="a" * 64,
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        stackup=stackup,
        width_results=(),
        calculate_width=calculate,
    )
    choose_class(api, dialog, "USB")
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()
    api.drain()
    labels = [
        item.GetLabel() for item in dialog.width_rows.items if hasattr(item, "GetLabel")
    ]
    assert labels.count("Actual:") == 1
    assert labels.count("Nominal:") == 1
    assert labels.count("Difference:") == 1
    assert labels[:3] == ["Actual:", "0.15 mm", "(5.91 mil)"]
    assert "Other actual widths on this layer:" in dialog.width_hint.GetLabel()
    assert "0.1 mm" in dialog.width_hint.GetLabel()


def test_draft_unavailable_result_shows_link_and_keeps_retrying(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """A calculator failure must not freeze the draft on a dead result."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    snapshot = _snapshot(api)
    snapshot = replace(
        snapshot,
        net_classes=("Default", "USB"),
        net_class_memberships=(
            ("CLK_P", ("USB",)),
            ("CLK_N", ("USB",)),
            ("GND", ("Default",)),
        ),
    )
    calls = {"count": 0}

    def calculate(
        calc_stackup: object, spec: object, layer: str, **kwargs: Any
    ) -> object:
        calls["count"] += 1
        digest = model.calculation_fingerprint(calc_stackup, spec, layer)
        if calls["count"] == 1:
            return model.WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=digest,
                status="unavailable",
                message="Could not receive JLCPCB's calculator result.",
            )
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=digest,
            status="success",
            target_width_nm=150_000,
            calculated_at_utc="2026-09-23T00:00:00.000000Z",
            model="CoatedMicrostrip1B",
            calculation_digest="a" * 64,
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        stackup=stackup,
        width_results=(),
        calculate_width=calculate,
    )
    choose_class(api, dialog, "USB")
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()
    api.drain()
    assert (
        "Could not receive JLCPCB's calculator result" in dialog.width_hint.GetLabel()
    )
    assert "retry" not in dialog.width_hint.GetLabel().casefold()
    assert dialog.calculator_link.IsShown()
    assert dialog.calculator_link.GetURL() == api.dialog.CALCULATOR_URL
    assert dialog.calculator_link.GetLabel() == api.dialog.CALCULATOR_LINK_LABEL
    from impedance.stackup_model import find_width_result

    unavailable = find_width_result(
        dialog.width_results,
        dialog._selection_specification().spec_id,
        dialog._active_layer,
    )
    assert unavailable is not None and unavailable.status == "unavailable"
    assert dialog._draft_retry_timer.IsRunning()
    dialog._draft_retry_timer.Stop()
    dialog._on_draft_retry_timer(SimpleNamespace())
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert "Nominal:" in [
        item.GetLabel() for item in dialog.width_rows.items if hasattr(item, "GetLabel")
    ]
    assert "Actual:" in [
        item.GetLabel() for item in dialog.width_rows.items if hasattr(item, "GetLabel")
    ]
    assert not dialog.calculator_link.IsShown()
    assert dialog._draft_calc_alert == ""
    assert calls["count"] == 2


@pytest.mark.parametrize("status", ["unsupported", "unavailable"])
def test_corrected_ninety_ohm_type_clears_failure_and_recalculates(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
    status: str,
) -> None:
    """Changing to a viable construction replaces the old rejection immediately."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup, snapshot, single, differential = _ninety_ohm_inputs(api)
    calls: list[tuple[str, str]] = []

    def calculate(
        calc_stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        calls.append((spec.kind, spec.target_ohms))
        if spec.kind == "differential":
            assert spec.layer_settings[0].spacing_nm == 200_000
            return _successful_width(api, calc_stackup, spec)
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=model.calculation_fingerprint(calc_stackup, spec, layer),
            status=status,
            message="The single-ended 90 ohm target is unavailable.",
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        specification=single,
        stackup=stackup,
        calculate_width=calculate,
    )
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()
    api.drain()
    assert "single-ended 90 ohm target is unavailable" in dialog.width_hint.GetLabel()

    _correct_to_differential(api, dialog)
    assert "unavailable" not in dialog.width_hint.GetLabel().casefold()
    assert not dialog.calculator_link.IsShown()
    assert not dialog._draft_retry_timer.IsRunning()
    assert dialog._draft_calc_timer.IsRunning()
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert calls == [("single_ended", "90"), ("differential", "90")]
    assert "Nominal:" in [item.GetLabel() for item in dialog.width_rows.items]
    assert "unavailable" not in dialog.width_hint.GetLabel().casefold()

    reopened = api.dialog.SpecificationDialog(
        None,
        snapshot,
        specification=differential,
        stackup=stackup,
        width_results=dialog.width_results,
        calculate_width=calculate,
    )
    assert "Nominal:" in [item.GetLabel() for item in reopened.width_rows.items]
    assert "unavailable" not in reopened.width_hint.GetLabel().casefold()
    assert workers == []


@pytest.mark.parametrize("completion", ["unavailable", "unsupported", "exception"])
def test_old_completion_cannot_obscure_cached_corrected_width(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
    completion: str,
) -> None:
    """A queued old callback cannot attach an error or retry to a newer cache hit."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup, snapshot, single, differential = _ninety_ohm_inputs(api)
    cached = _successful_width(api, stackup, differential)

    def calculate(
        calc_stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        if completion == "exception":
            raise RuntimeError("The obsolete single-ended request failed.")
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=model.calculation_fingerprint(calc_stackup, spec, layer),
            status=completion,
            message="The obsolete single-ended request failed.",
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        specification=single,
        stackup=stackup,
        width_results=(cached,),
        calculate_width=calculate,
    )
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    workers.pop()()  # The completed old request is queued for the UI, not delivered.
    _correct_to_differential(api, dialog)
    assert "Nominal:" in [item.GetLabel() for item in dialog.width_rows.items]
    api.drain()

    assert "Nominal:" in [item.GetLabel() for item in dialog.width_rows.items]
    assert "unavailable" not in dialog.width_hint.GetLabel().casefold()
    assert "obsolete" not in dialog.width_hint.GetLabel().casefold()
    assert not dialog.calculator_link.IsShown()
    assert not dialog._draft_retry_timer.IsRunning()
    assert workers == []


@pytest.mark.parametrize("edit", ["blank", "invalid", "excluded", "no_class"])
def test_incomplete_inputs_invalidate_pending_width_failure(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
    edit: str,
) -> None:
    """Uncalculable input must cancel old work and suppress its queued retry."""
    api = constructor_api
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup, snapshot, _single, differential = _ninety_ohm_inputs(api)

    def calculate(
        calc_stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        return model.WidthResult(
            spec_id=spec.spec_id,
            layer=layer,
            input_digest=model.calculation_fingerprint(calc_stackup, spec, layer),
            status="unavailable",
            message="The obsolete spacing request failed.",
        )

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        specification=differential,
        stackup=stackup,
        calculate_width=calculate,
    )
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    pending_cancel = dialog._draft_calc_cancel
    workers.pop()()
    if edit == "excluded":
        dialog.include_layer.SetValue(False)
        dialog.include_layer.emit(api.wx.EVT_CHECKBOX)
    elif edit == "no_class":
        dialog.net_class.SetSelection(0)
        dialog.net_class.emit(api.wx.EVT_CHOICE)
    else:
        dialog.spacing.SetValue("" if edit == "blank" else "invalid")
        dialog.spacing.emit(api.wx.EVT_TEXT)
    assert pending_cancel is not None and pending_cancel.is_set()
    api.drain()
    if edit in ("blank", "invalid"):
        assert "Complete this layer's settings" in dialog.width_hint.GetLabel()
    assert not dialog.calculator_link.IsShown()
    assert not dialog._draft_retry_timer.IsRunning()
    assert not dialog._draft_calc_timer.IsRunning()
    assert dialog._draft_calc_alert == ""
    assert workers == []


def test_changing_display_units_preserves_pending_calculation(
    constructor_api: SimpleNamespace,  # noqa: F811
    workers: list[Callable[[], None]],
) -> None:
    """An exact mm-to-mil conversion must not discard or duplicate a current solve."""
    api = constructor_api
    stackup, snapshot, _single, differential = _ninety_ohm_inputs(api)

    def calculate(
        calc_stackup: Stackup, spec: Specification, layer: str, **kwargs: Any
    ) -> WidthResult:
        return _successful_width(api, calc_stackup, spec)

    dialog = api.dialog.SpecificationDialog(
        None,
        snapshot,
        specification=differential,
        stackup=stackup,
        calculate_width=calculate,
    )
    dialog._draft_calc_timer.Stop()
    dialog._on_draft_calc_timer(SimpleNamespace())
    pending_cancel = dialog._draft_calc_cancel
    assert dialog.units.SetStringSelection("mil")
    dialog.units.emit(api.wx.EVT_CHOICE)
    if dialog._draft_calc_timer.IsRunning():
        dialog._draft_calc_timer.Stop()
        dialog._on_draft_calc_timer(SimpleNamespace())
    assert pending_cancel is not None and not pending_cancel.is_set()
    assert len(workers) == 1
    workers.pop()()
    api.drain()
    assert "Nominal:" in [item.GetLabel() for item in dialog.width_rows.items]
