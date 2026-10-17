"""Spec-dialog width hint: one pending message and live draft calculation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_dialog import (
    _snapshot,
    constructor_api,  # noqa: F401 -- shared real-constructor fixture
)
from .test_impedance_netclass_dialog import choose_class


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


def _stackup(api: SimpleNamespace):
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

    assert (
        find_width_result(
            dialog.width_results,
            dialog._selection_specification().spec_id,
            dialog._active_layer,
        )
        is None
    )
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
