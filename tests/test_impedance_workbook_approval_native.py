"""Approve two real native class rows and retain the result across idle and reopen.

The board snapshot, nominal calculator result and image bytes are deterministic
seams. Native controls, event handlers, paints and persistence remain real.
"""

import importlib
import os
from types import SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_capture_review_restore import _changed_capture
from .test_impedance_settings_native import _PNG, _api, _click, _modal
from .test_impedance_spec_width_hint import _stackup

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]


def _fixture(api: SimpleNamespace) -> tuple[Any, Any, Any]:
    """Supply separate RF sections, a covered stub and a matching USB pair."""
    model = api.model
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=(
            model.Trace("rf-a", "F.Cu", "RF_A", 150_000, ((0, 0), (2_000_000, 0))),
            model.Trace(
                "rf-b",
                "F.Cu",
                "RF_B",
                150_000,
                ((0, 2_000_000), (2_000_000, 2_000_000)),
            ),
            model.Trace(
                "rf-stub",
                "F.Cu",
                "RF_STUB",
                150_000,
                ((0, 4_000_000), (100_000, 4_000_000)),
            ),
            model.Trace(
                "usb-p",
                "F.Cu",
                "USB_P",
                150_000,
                ((0, 10_000_000), (2_000_000, 10_000_000)),
            ),
            model.Trace(
                "usb-n",
                "F.Cu",
                "USB_N",
                150_000,
                ((0, 10_353_200), (2_000_000, 10_353_200)),
            ),
        ),
        net_classes=("RF", "USB"),
        net_class_memberships=tuple(
            (net, ("RF" if net.startswith("RF") else "USB",))
            for net in ("RF_A", "RF_B", "RF_STUB", "USB_P", "USB_N")
        ),
        net_class_context_digest="native-two-class-workbook-approval",
        differential_pairs=(("USB_P", "USB_N"),),
        copper_features=(
            model.CopperFeature(
                "rf-stub-pad",
                "RF_STUB",
                "F.Cu",
                "pad",
                "rect",
                (0, 4_000_000),
                1_000_000,
                1_000_000,
            ),
        ),
    )
    specs = (
        model.Specification(
            "rf",
            "RF",
            "50",
            "single_ended",
            "RF",
            (model.LayerSettings("F.Cu", ("B.Cu",)),),
        ),
        model.Specification(
            "usb",
            "USB",
            "90",
            "differential",
            "USB",
            (model.LayerSettings("F.Cu", ("B.Cu",), 203_200),),
        ),
    )
    stackup = _stackup(api)
    calculations = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    results = tuple(
        calculations.WidthResult(
            spec_id=spec.spec_id,
            layer="F.Cu",
            input_digest=calculations.calculation_fingerprint(stackup, spec, "F.Cu"),
            status="success",
            target_width_nm=150_000,
            calculated_at_utc="2026-10-29T12:00:00.000000Z",
            model="CoatedMicrostrip1B"
            if spec.kind == "single_ended"
            else "DifferentialMicrostrip1B",
            calculation_digest="c" * 64,
        )
        for spec in specs
    )
    config = model.Config(
        enabled=True,
        specifications=specs,
        stackup=stackup,
        width_results=results,
    )
    service = importlib.import_module(api.dialog.__package__ + ".service")
    return snapshot, config, service.CapturedImage.from_bytes(_PNG)


def _assert_class_statuses(wx: Any, dialog: Any, *, approved: bool) -> None:
    """Read both native list values and the actual cell attributes supplied to wx."""
    import wx.dataview as dv

    expected = "Approved" if approved else "Needs approval"
    assert dialog.specifications.GetItemCount() == 2
    assert {dialog.specifications.GetTextValue(row, 1) for row in range(2)} == {
        "RF",
        "USB",
    }
    store = dialog.specifications.GetStore()
    for row in range(2):
        assert dialog.specifications.GetTextValue(row, 0) == expected
        attr = dv.DataViewItemAttr()
        assert store.GetAttrByRow(row, 0, attr)
        assert attr.GetColour() == wx.Colour(255, 255, 255)
        assert attr.GetBackgroundColour() == wx.Colour(
            *(35, 135, 60) if approved else (160, 90, 16)
        )


def _wait_for_images(wx: Any, dialog: Any) -> None:
    """Complete real background capture verification before checking stable state."""
    wx.WakeUpIdle()
    wait_until(
        wx,
        lambda: dialog.preview_pane.displayed
        and not dialog._capture_prepare_sections
        and not dialog._capture_restore_pending,
    )
    pump(wx)


@pytest.mark.parametrize("changed_images", [False, True], ids=["same", "changed"])
def test_native_two_class_workbook_button_updates_status_idle_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    changed_images: bool,
) -> None:
    """Workbook approval synchronizes RF and USB even with saved image history."""
    if isolated_native(request, "KICAD_IMPEDANCE_TWO_CLASS_APPROVAL_CHILD"):
        return
    api = _api(monkeypatch)
    snapshot, config, capture = _fixture(api)
    current_capture = [capture]
    saved: list[Any] = []

    def unexpected_calculation(*_args: Any, **_kwargs: Any) -> Any:
        """Require the persisted nominal results to remain applicable throughout."""
        raise AssertionError("The fixture's complete cached nominals were discarded")

    def exercise(frame: Any, wx: Any) -> None:
        def open_dialog(settings: Any) -> Any:
            """Preserve production construction, timers and snapshot verification."""
            return api.dialog.ImpedanceDialog(
                frame,
                settings,
                snapshot,
                preview=lambda *_args, **_options: current_capture[0],
                refresh_snapshot=lambda: snapshot,
                fetch_stackup_catalog=lambda *_args, **_options: (),
                calculate_width=unexpected_calculation,
                save_config=saved.append,
            )

        draft = open_dialog(config)

        def save_viewed_draft() -> None:
            _wait_for_images(wx, draft)
            assert len(draft.session.analysis.sections) == 4
            assert len(draft.session.included) == 3
            assert not draft.session.pending_spacing_reviews
            _assert_class_statuses(wx, draft, approved=False)
            for section in draft.preview_pane.sections:
                draft.preview_pane.select(draft.preview_pane.entries.index(section))
                wait_until(wx, lambda: draft.preview_pane.displayed)
            _click(wx, draft.FindWindow(wx.ID_OK))

        try:
            assert _modal(wx, draft, save_viewed_draft) == wx.ID_OK
            assert len(saved) == 1
            assert saved[0].review_tracking.images
            assert not saved[0].reviewed_digest
        finally:
            draft.Destroy()
            pump(wx)

        if changed_images:
            current_capture[0] = _changed_capture(SimpleNamespace(capture=capture))
        dialog = open_dialog(api.model.Config.from_dict(saved[0].to_dict()))

        def approve_and_save() -> None:
            _wait_for_images(wx, dialog)
            _assert_class_statuses(wx, dialog, approved=False)
            assert dialog.approve_button.GetLabel() == "Approve 3 workbook rows"
            assert dialog.approve_button.IsEnabled()
            assert dialog.FindWindow(wx.ID_OK).GetLabel() == "Save draft and close"
            _click(wx, dialog.approve_button)
            assert dialog.session.approved, dialog.status.GetValue()
            assert dialog.approve_button.GetLabel() == "Workbook rows approved"
            assert dialog.FindWindow(wx.ID_OK).GetLabel() == "Save and close"
            _assert_class_statuses(wx, dialog, approved=True)
            _wait_for_images(wx, dialog)
            assert dialog.session.approved
            _assert_class_statuses(wx, dialog, approved=True)
            _click(wx, dialog.FindWindow(wx.ID_OK))

        try:
            assert _modal(wx, dialog, approve_and_save) == wx.ID_OK
            assert len(saved) == 2 and saved[-1].reviewed_digest
        finally:
            dialog.Destroy()
            pump(wx)

        reopened = open_dialog(api.model.Config.from_dict(saved[-1].to_dict()))

        def inspect_reopened() -> None:
            _wait_for_images(wx, reopened)
            assert reopened.session.approved
            _assert_class_statuses(wx, reopened, approved=True)
            assert reopened.approve_button.GetLabel() == "Workbook rows approved"
            assert reopened.FindWindow(wx.ID_OK).GetLabel() == "Save and close"
            _click(wx, reopened.FindWindow(wx.ID_CANCEL))

        try:
            assert _modal(wx, reopened, inspect_reopened) == wx.ID_CANCEL
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)
