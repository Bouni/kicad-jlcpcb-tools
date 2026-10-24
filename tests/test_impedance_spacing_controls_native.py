"""Real wx controls for offline spacing checks; snapshots use deterministic routes."""

from __future__ import annotations

from base64 import b64decode
from dataclasses import replace
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest

from scripts.linux_native_impedance_smoke import event

from .native_wx_support import isolated_native, pump, run_native
from .test_impedance_workbook_sizing_native import assert_columns_fit

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_native_spacing_events_refresh_repair_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Real constructors/events update independent dimensions and clear old warnings."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_CONTROLS_CHILD"):
        return
    package_name = "_impedance_spacing_controls_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialogs = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    spec = model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (model.LayerSettings("F.Cu", ("B.Cu",), 200_000),),
    )
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=(
            model.Trace("p", "F.Cu", "USB_P", 150_000, ((0, 0), (2_000_000, 0))),
            model.Trace(
                "n",
                "F.Cu",
                "USB_N",
                150_000,
                ((0, 350_000), (2_000_000, 350_000)),
            ),
        ),
        net_classes=("USB",),
        net_class_memberships=(("USB_P", ("USB",)), ("USB_N", ("USB",))),
        differential_pairs=(("USB_P", "USB_N"),),
        net_class_context_digest="native-spacing-controls",
    )
    current = [snapshot]
    saved: list[Any] = []

    def exercise(frame: Any, wx: Any) -> None:
        draft = replace(
            spec,
            target_ohms="",
            kind="differential_coplanar",
            layer_settings=(model.LayerSettings("F.Cu", (), 200_000),),
        )
        editor = dialogs.SpecificationDialog(frame, snapshot, draft)
        try:
            editor.Show()
            pump(wx)
            assert "Match" in editor.spacing_comparison.GetLabel()
            for entered, label in (
                ("0.15", "Deviation"),
                ("", "Needs target"),
                ("0.2", "Match"),
            ):
                editor.spacing.SetValue(entered)
                event(wx, editor.spacing, wx.EVT_TEXT)
                pump(wx)
                assert label in editor.spacing_comparison.GetLabel()
                expected = editor._spacing_text_colour
                actual = editor.spacing_comparison.GetForegroundColour()
                assert (actual == expected) is (label == "Match")
            editor.kind.SetSelection(0)
            event(wx, editor.kind, wx.EVT_CHOICE)
            pump(wx)
            assert "Match" not in editor.spacing_comparison.GetLabel()
            assert (
                editor.spacing_comparison.GetForegroundColour()
                == editor._spacing_text_colour
            )
            assert (
                editor.spacing_comparison.GetRect().GetBottom()
                <= editor.ground_gap.GetRect().GetTop()
            )
        finally:
            editor.Destroy()
            pump(wx)

        def open_dialog(config: Any) -> Any:
            """Construct the actual modal with offline snapshot and capture seams."""
            return dialogs.ImpedanceDialog(
                frame,
                config,
                current[0],
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=lambda: current[0],
                fetch_stackup_catalog=lambda *_args, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(model.Config(specifications=(spec,)))
        failures: list[BaseException] = []

        def interact() -> None:
            """Keep actual Save handling inside its native modal loop."""
            try:
                table = dialog.sections
                assert table.GetColLabelValue(11) == "Pair spacing check"
                assert table.GetTextValue(0, 11) == "Match"
                table.SelectRow(0, column=11)
                included = table.GetToggleValue(0, 0)
                for offset, verdict in ((250_000, "Deviation"), (350_000, "Match")):
                    current[0] = replace(
                        snapshot,
                        traces=(
                            snapshot.traces[0],
                            replace(
                                snapshot.traces[1],
                                points=((0, offset), (2_000_000, offset)),
                            ),
                        ),
                    )
                    assert dialog._refresh_rows(current[0])
                    pump(wx)
                    assert table.GetTextValue(0, 11).startswith(verdict)
                    assert table.GetTextValue(0, 7) == "0.15 mm"
                    colour = table.GetCellTextColour(0, 11)
                    expected = (
                        wx.Colour(180, 140, 0)
                        if verdict == "Deviation"
                        else table.GetDefaultCellTextColour()
                    )
                    assert colour == expected
                    assert table.GetSelectedRow() == 0
                    assert table.GetGridCursorCol() == 11
                    assert table.GetToggleValue(0, 0) == included
                    assert_columns_fit(table, expected_rows=1)
                event(wx, dialog.FindWindow(wx.ID_OK), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_OK
            assert len(saved) == 1
        finally:
            dialog.Destroy()
            pump(wx)
        reopened = open_dialog(model.Config.from_dict(saved[0].to_dict()))
        try:
            reopened.Show()
            pump(wx)
            assert reopened.sections.GetTextValue(0, 11) == "Match"
            assert_columns_fit(reopened.sections, expected_rows=1)
        finally:
            reopened.Destroy()
            pump(wx)

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]


def test_native_workbook_spacing_precision_survives_refresh_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Rounded native cells must retain exact nanometre verdicts and saved targets."""
    if isolated_native(request, "KICAD_IMPEDANCE_SPACING_PRECISION_CHILD"):
        return
    name = "_impedance_spacing_precision_native"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, name, package)
    dialogs = importlib.import_module(f"{name}.dialog")
    model = importlib.import_module(f"{name}.model")
    service = importlib.import_module(f"{name}.service")
    specification = model.Specification(
        "usb",
        "USB",
        "90",
        "differential",
        "USB",
        (model.LayerSettings("F.Cu", ("B.Cu",), 203_205),),
    )

    def board(gap_nm: int) -> Any:
        """Declare exact copper edge distances independently of display rounding."""
        return model.BoardSnapshot(
            layers=("F.Cu", "B.Cu"),
            traces=tuple(
                model.Trace(
                    net,
                    "F.Cu",
                    net,
                    150_000,
                    (
                        (0, side * (150_000 + gap_nm)),
                        (2_000_000, side * (150_000 + gap_nm)),
                    ),
                )
                for side, net in enumerate(("USB_P", "USB_N"))
            ),
            net_classes=("USB",),
            net_class_memberships=(("USB_P", ("USB",)), ("USB_N", ("USB",))),
            net_class_context_digest="spacing-precision-native-membership",
            differential_pairs=(("USB_P", "USB_N"),),
        )

    def exercise(frame: Any, wx: Any) -> None:
        """Inspect real grid text and native colors before and after reconstruction."""
        current = [board(204_205)]

        def open_dialog(config: Any) -> Any:
            return dialogs.ImpedanceDialog(
                frame,
                config,
                current[0],
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=lambda: current[0],
                fetch_stackup_catalog=lambda *_args, **_kwargs: (),
            )

        def assert_cells(dialog: Any, *, mismatch: bool) -> None:
            """Keep the one-nanometre threshold crossing visible in the verdict."""
            table = dialog.sections
            assert table.GetItemCount() == 1
            assert table.GetTextValue(0, 11) == "0.20421 mm"
            assert table.GetTextValue(0, 13) == "0.20321 mm"
            assert table.GetTextValue(0, 12) == ("!=" if mismatch else "")
            assert table.GetTextValue(0, 14) == ("Deviation" if mismatch else "Match")
            for column in (11, 12, 13, 14):
                assert table.GetCellTextColour(0, column) == (
                    wx.Colour(180, 140, 0)
                    if mismatch
                    else table.GetDefaultCellTextColour()
                )

        dialog = open_dialog(model.Config(specifications=(specification,)))
        try:
            dialog.Show()
            pump(wx)
            assert_cells(dialog, mismatch=False)
            current[0] = board(204_206)
            assert dialog._refresh_rows(current[0])
            pump(wx)
            assert_cells(dialog, mismatch=True)
            config = model.Config.from_dict(dialog.session.config.to_dict())
            assert config.specifications[0].layer_settings[0].spacing_nm == 203_205
        finally:
            dialog.Destroy()
            pump(wx)
        reopened = open_dialog(config)
        try:
            reopened.Show()
            pump(wx)
            assert_cells(reopened, mismatch=True)
        finally:
            reopened.Destroy()
            pump(wx)

    run_native(exercise)
