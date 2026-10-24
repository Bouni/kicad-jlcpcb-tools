"""Review fixes through real native choices, nested modals, paint and idle work.

Board snapshots and PNGs are deterministic seams. These checks establish native
control/event behavior and do not exercise KiCad plotting or provider transport.
"""

from dataclasses import replace
import importlib
import os
from types import SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_capture_preparation_native import (
    _approved_history,
    _fixture,
    _select_image,
)
from .test_impedance_draft_dialog import _board, _intent
from .test_impedance_settings_native import _api, _click, _modal, _select
from .test_impedance_spec_width_hint import _stackup

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


def _approved_config(
    api: SimpleNamespace, snapshot: Any, spec: Any, history: Any
) -> Any:
    """Approve the workbook from evidence obtained through actual native review."""
    session = api.dialog.ReviewSession(
        api.model.Config(enabled=True, specifications=(spec,), review_tracking=history),
        snapshot,
    )
    session.refresh()
    assert session.approve_if_layer_reviews_complete()
    return session.save_result()


def _wait_for_main(wx: Any, dialog: Any) -> None:
    """Drain real selected painting and every queued current-image check."""
    wx.WakeUpIdle()
    wait_until(
        wx,
        lambda: dialog.preview_pane.displayed
        and not dialog._capture_prepare_sections
        and not dialog._capture_restore_pending,
        reason="The current workbook images did not finish native idle verification",
    )


def test_native_kind_edit_autoapproval_survives_idle_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A fresh coplanar review cannot be revoked by another kind's cached hashes."""
    if isolated_native(request, "KICAD_IMPEDANCE_REVIEW_KIND_IDLE_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        history = _approved_history(api, frame, wx, snapshot, spec, capture)
        config = _approved_config(api, snapshot, spec, history)
        saved: list[Any] = []

        def open_main(settings: Any) -> Any:
            return api.dialog.ImpedanceDialog(
                frame,
                settings,
                snapshot,
                preview=lambda *_args, **_options: capture,
                refresh_snapshot=lambda: snapshot,
                fetch_stackup_catalog=lambda *_args, **_options: (),
                save_config=saved.append,
            )

        dialog = open_main(config)
        child_errors: list[BaseException] = []
        accepted: list[bool] = []

        class EditHook(wx.ModalDialogHook):
            def Enter(self, child: Any) -> int:
                if not isinstance(child, api.dialog.SpecificationDialog):
                    return wx.ID_NONE

                def review_child() -> None:
                    try:
                        _select(wx, child.kind, 2)
                        for layer in ("F.Cu", "B.Cu"):
                            _select(wx, child.layer, child._layer_names.index(layer))
                            child.ground_gap.SetValue("0.1")
                            for index in range(len(child.preview_pane.sections)):
                                if child._active_layer != layer:
                                    _select(
                                        wx, child.layer, child._layer_names.index(layer)
                                    )
                                _select_image(wx, child.preview_pane.checklist, index)
                                wait_until(wx, lambda: child.preview_pane.displayed)
                                _click(wx, child.approve_layer)
                            assert child._layer_drafts[layer].confirmed
                        _click(wx, child.FindWindow(wx.ID_OK))
                        assert not child.IsModal(), child.validation_message.GetLabel()
                        accepted.append(True)
                    except BaseException as error:
                        child_errors.append(error)
                        if child.IsModal():
                            child.EndModal(wx.ID_CANCEL)

                wx.CallAfter(review_child)
                return wx.ID_NONE

        hook = EditHook()
        hook.Register()

        def inspect() -> None:
            _wait_for_main(wx, dialog)
            assert dialog.session.approved
            dialog.specifications.SelectRow(0)
            _click(wx, dialog._selection_buttons[0])
            if child_errors:
                raise child_errors[0]
            assert accepted == [True]
            assert dialog.session.approved, (
                "Completed native layer review must autoapprove"
            )
            _wait_for_main(wx, dialog)
            assert dialog.session.approved, (
                "Idle work revoked the fresh coplanar review"
            )
            assert (
                dialog.session.config.specifications[0].kind == "single_ended_coplanar"
            )
            _click(wx, dialog.FindWindow(wx.ID_OK))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_OK
            assert len(saved) == 1 and saved[0].reviewed_digest
        finally:
            hook.Unregister()
            dialog.Destroy()
            pump(wx)

        reopened = open_main(saved[0])

        def inspect_reopen() -> None:
            _wait_for_main(wx, reopened)
            assert reopened.session.approved
            assert (
                reopened.session.config.review_tracking.layers
                == saved[0].review_tracking.layers
            )
            _click(wx, reopened.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, reopened, inspect_reopen) == wx.ID_CANCEL
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)


def test_native_early_unchanged_apply_retains_verified_workbook_approval(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Applying before child warm-up cannot convert unchanged approval to a draft."""
    if isolated_native(request, "KICAD_IMPEDANCE_REVIEW_EARLY_APPLY_CHILD"):
        return
    api = _api(monkeypatch)

    def exercise(frame: Any, wx: Any) -> None:
        snapshot, spec, capture = _fixture(api)
        history = _approved_history(api, frame, wx, snapshot, spec, capture)
        config = _approved_config(api, snapshot, spec, history)
        dialog = api.dialog.ImpedanceDialog(
            frame,
            config,
            snapshot,
            preview=lambda *_args, **_options: capture,
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_options: (),
        )
        child_errors: list[BaseException] = []
        pending: list[int] = []

        class EarlyApplyHook(wx.ModalDialogHook):
            def Enter(self, child: Any) -> int:
                if not isinstance(child, api.dialog.SpecificationDialog):
                    return wx.ID_NONE

                def apply_child() -> None:
                    try:
                        pending.append(4 - len(child.capture_digests))
                        assert pending[-1] > 0, (
                            "The fixture must Apply before warm-up finishes"
                        )
                        _click(wx, child.FindWindow(wx.ID_OK))
                        assert not child.IsModal(), child.validation_message.GetLabel()
                    except BaseException as error:
                        child_errors.append(error)
                        if child.IsModal():
                            child.EndModal(wx.ID_CANCEL)

                wx.CallAfter(apply_child)
                return wx.ID_NONE

        hook = EarlyApplyHook()
        hook.Register()

        def inspect() -> None:
            _wait_for_main(wx, dialog)
            assert dialog.session.approved
            verified = dict(dialog.session.capture_digests)
            dialog.specifications.SelectRow(0)
            _click(wx, dialog._selection_buttons[0])
            if child_errors:
                raise child_errors[0]
            assert pending and pending[0] > 0
            assert dialog.session.approved
            assert dialog.session.capture_digests == verified
            _wait_for_main(wx, dialog)
            assert dialog.session.approved
            assert dialog.session.config.review_tracking.layers == history.layers
            _click(wx, dialog.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, dialog, inspect) == wx.ID_CANCEL
        finally:
            hook.Unregister()
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


def test_native_linked_kind_roundtrip_saves_equal_reactivated_gaps(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Native synchronous text/choice events cannot save divergent linked gaps."""
    if isolated_native(request, "KICAD_IMPEDANCE_REVIEW_LINKED_KIND_CHILD"):
        return
    api = _api(monkeypatch)
    stackup_model = importlib.import_module(f"{api.dialog.__package__}.stackup_model")
    original = _board(api)
    snapshot = replace(
        original,
        traces=original.traces
        + tuple(
            replace(trace, trace_id=trace.trace_id + "-bottom", layer="B.Cu")
            for trace in original.traces
            if trace.layer == "F.Cu"
        ),
    )
    stackup = _stackup(api)

    def calculate(construction: Any, spec: Any, layer: str, **_options: Any) -> Any:
        return stackup_model.WidthResult(
            spec.spec_id,
            layer,
            stackup_model.calculation_fingerprint(construction, spec, layer),
            "unsupported",
            message="Deterministic settings fixture; no supplier request.",
        )

    def exercise(frame: Any, wx: Any) -> None:
        dialog = api.dialog.SpecificationDialog(
            frame,
            snapshot,
            specification=_intent(api).specifications[0],
            stackup=stackup,
            calculate_width=calculate,
        )

        def set_independent(value: bool) -> None:
            dialog.independent_layer.SetValue(value)
            event = wx.CommandEvent(
                wx.EVT_CHECKBOX.typeId, dialog.independent_layer.GetId()
            )
            event.SetEventObject(dialog.independent_layer)
            assert dialog.independent_layer.GetEventHandler().ProcessEvent(event)

        def edit() -> None:
            _select(wx, dialog.kind, 3)
            dialog.spacing.SetValue("0.200001")
            dialog.ground_gap.SetValue("0.100001")
            _select(wx, dialog.layer, dialog._layer_names.index("B.Cu"))
            set_independent(True)
            dialog.ground_gap.SetValue("0.250003")
            _select(wx, dialog.kind, 0)
            set_independent(False)
            assert dialog._linked_layer_groups == (("F.Cu", "B.Cu"),)
            _select(wx, dialog.kind, 3)
            _click(wx, dialog.FindWindow(wx.ID_OK))

        try:
            assert _modal(wx, dialog, edit) == wx.ID_OK
            accepted = dialog.specification
            assert accepted is not None
            assert accepted.linked_layer_groups == (("F.Cu", "B.Cu"),)
            assert {item.ground_gap_nm for item in accepted.layer_settings} == {250_003}
        finally:
            dialog.Destroy()
            pump(wx)

        reopened = api.dialog.SpecificationDialog(
            frame,
            snapshot,
            specification=accepted,
            stackup=stackup,
            calculate_width=calculate,
        )

        def inspect_reopen() -> None:
            assert reopened._linked_layer_groups == (("F.Cu", "B.Cu"),)
            for layer in ("F.Cu", "B.Cu"):
                _select(wx, reopened.layer, reopened._layer_names.index(layer))
                assert reopened.ground_gap.GetValue() == "0.250003"
            _click(wx, reopened.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, reopened, inspect_reopen) == wx.ID_CANCEL
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)
