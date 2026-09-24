"""Exercise workbook width comparisons through native controls and completions."""

from base64 import b64decode
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from scripts.linux_native_impedance_smoke import event

from .native_wx_support import isolated_native, pump, run_native, wait_until
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


def _assert_comparison(wx: Any, dialog: Any, row: int, *, mismatch: bool) -> None:
    """Read native attributes and actual width paints in selected and ordinary rows."""
    import wx.grid as gridlib

    assert dialog.sections.GetTextValue(row, 8) == ("!=" if mismatch else "")
    for column in range(dialog.sections.GetNumberCols()):
        expected = (
            wx.Colour(180, 140, 0)
            if mismatch and column in (7, 8, 9)
            else dialog.sections.GetDefaultCellTextColour()
        )
        assert dialog.sections.GetCellTextColour(row, column) == expected
        assert (
            dialog.sections.GetCellBackgroundColour(row, column)
            == dialog.sections.GetDefaultCellBackgroundColour()
        )
    for selected in (False, True):
        for column in (7, 8, 9):
            rect = wx.Rect(
                4,
                4,
                dialog.sections.GetColSize(column) - 1,
                dialog.sections.GetRowSize(row) - 1,
            )
            bitmap = wx.Bitmap(rect.width + 8, rect.height + 8)
            dc = wx.MemoryDC(bitmap)
            attr = gridlib.GridCellAttr()
            attr.SetFont(dialog.sections.GetCellFont(row, column))
            attr.SetTextColour(dialog.sections.GetCellTextColour(row, column))
            attr.SetBackgroundColour(
                dialog.sections.GetCellBackgroundColour(row, column)
            )
            painted: list[tuple[Any, ...]] = []
            draw = dc.DrawText

            def observe(
                text: str,
                x: int,
                y: int,
                _painted: list[tuple[Any, ...]] = painted,
                _draw: Any = draw,
                _dc: Any = dc,
            ) -> None:
                _painted.append(
                    (
                        text,
                        x,
                        y,
                        tuple(_dc.GetTextExtent(text)),
                        _dc.GetTextForeground(),
                    )
                )
                _draw(text, x, y)

            try:
                with patch.object(dc, "DrawText", observe):
                    dialog.sections.GetCellRenderer(row, column).Draw(
                        dialog.sections, attr, dc, rect, row, column, selected
                    )
            finally:
                dc.SelectObject(wx.NullBitmap)
            expected = (
                dialog.sections.GetSelectionForeground()
                if selected
                else wx.Colour(180, 140, 0)
                if mismatch
                else dialog.sections.GetDefaultCellTextColour()
            )
            assert painted
            for text, x, y, (width, height), colour in painted:
                assert colour == expected, (row, column, selected, text)
                assert rect.Contains(wx.Rect(x, y, width, height))
                if column == 8 and text:
                    assert abs(x + width / 2 - (rect.x + rect.width / 2)) <= 1


def test_native_width_cells_refresh_selection_save_and_reopen(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Mismatch paint stays amber when unselected and readable when selected."""
    if isolated_native(request, "KICAD_IMPEDANCE_WIDTH_MISMATCH_CHILD"):
        return
    import wx.grid as gridlib

    package_name = "_impedance_width_mismatch_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    stackup_model = importlib.import_module(f"{package_name}.stackup_model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    saved: list[Any] = []
    desired_width = [181_400]
    stackup = stackup_model.Stackup(
        "sample",
        "Sample two-layer construction",
        2,
        "0.324",
        "1",
        "",
        calculator_id="sample-calculator",
        layers=(
            stackup_model.StackupLayer("L1", "copper", "0.035"),
            stackup_model.StackupLayer("Core", "core", "0.254", "FR4", "4.2"),
            stackup_model.StackupLayer("L2", "copper", "0.035"),
        ),
    )
    spec = model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        (model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=(
            model.Trace("narrow", "F.Cu", "NARROW", 150_000, ((0, 0), (1_000_000, 0))),
            model.Trace(
                "nominal",
                "F.Cu",
                "NOMINAL",
                181_400,
                ((0, 2_000_000), (1_000_000, 2_000_000)),
            ),
        ),
        net_classes=("RF",),
        net_class_memberships=(("NARROW", ("RF",)), ("NOMINAL", ("RF",))),
        net_class_context_digest="deterministic-native-width-classes",
    )

    def calculate(
        construction: Any, specification: Any, layer: str, **_options: Any
    ) -> Any:
        """Replace the network seam while retaining real worker completion events."""
        return stackup_model.WidthResult(
            spec_id=specification.spec_id,
            layer=layer,
            input_digest=stackup_model.calculation_fingerprint(
                construction, specification, layer
            ),
            status="success",
            target_width_nm=desired_width[0],
            calculated_at_utc="2026-10-16T12:00:00.000000Z",
            model="CoatedMicrostrip1B",
            calculation_digest="c" * 64,
        )

    config = model.Config(
        specifications=(spec,),
        stackup=stackup,
        width_results=(calculate(stackup, spec, "F.Cu"),),
    )

    def exercise(frame: Any, wx: Any) -> None:
        def open_dialog(value: Any) -> Any:
            """Keep all controls, handlers, timers and saved configuration real."""
            return dialog_api.ImpedanceDialog(
                frame,
                value,
                snapshot,
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=lambda: snapshot,
                calculate_width=calculate,
                fetch_stackup_catalog=lambda _count, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(config)
        failures: list[BaseException] = []

        def interact() -> None:
            try:
                assert isinstance(dialog.sections, gridlib.Grid)
                assert dialog.sections.GetItemCount() == 2
                widths = {
                    section.width_nm: index
                    for index, section in enumerate(dialog.session.analysis.sections)
                }
                narrow, nominal = widths[150_000], widths[181_400]
                assert dialog.sections.GetTextValue(narrow, 7) == "0.15 mm"
                assert dialog.sections.GetTextValue(narrow, 9) == "0.1814 mm"
                assert_columns_fit(dialog.sections)
                short_target_width = dialog.sections.GetColSize(9)

                for selected in (narrow, nominal):
                    event(
                        wx,
                        dialog.sections,
                        gridlib.EVT_GRID_SELECT_CELL,
                        selected,
                    )
                    wait_until(
                        wx,
                        lambda selected=selected: dialog.preview_pane._index
                        == selected,
                    )
                    assert dialog.sections.GetSelectedRow() == selected
                    _assert_comparison(wx, dialog, narrow, mismatch=True)
                    _assert_comparison(wx, dialog, nominal, mismatch=False)

                # A real worker marshals each delivered nominal through the
                # production completion handler and refreshes the native table.
                for target, narrow_mismatch in (
                    (123_456_789_012, True),
                    (150_000, False),
                    (160_000, False),
                    (160_001, True),
                    (139_999, True),
                    (181_400, True),
                ):
                    desired_width[0] = target
                    dialog._schedule_width_refresh(force=True)
                    wait_until(
                        wx,
                        lambda target=target: dialog._calculation_cancel is None
                        and dialog.session.config.width_results[0].target_width_nm
                        == target,
                    )
                    assert dialog.sections.GetSelectedRow() == nominal
                    _assert_comparison(wx, dialog, narrow, mismatch=narrow_mismatch)
                    _assert_comparison(
                        wx, dialog, nominal, mismatch=abs(181_400 - target) > 10_000
                    )
                    assert_columns_fit(dialog.sections)
                    if target == 123_456_789_012:
                        assert dialog.sections.GetColSize(9) > short_target_width
                    elif target == 150_000:
                        assert dialog.sections.GetColSize(9) <= short_target_width
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
            assert reopened.sections.GetItemCount() == 2
            widths = {
                section.width_nm: index
                for index, section in enumerate(reopened.session.analysis.sections)
            }
            _assert_comparison(wx, reopened, widths[150_000], mismatch=True)
            _assert_comparison(wx, reopened, widths[181_400], mismatch=False)
            assert reopened.sections.GetTextValue(widths[150_000], 9) == "0.1814 mm"
            reopened.Show()
            pump(wx)
            assert_columns_fit(reopened.sections)
        finally:
            reopened.Destroy()
            pump(wx)

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]
