"""Check real workbook column geometry across current content and empty states."""

from base64 import b64decode
from dataclasses import replace
import importlib
import os
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import Any, Optional
from unittest.mock import patch

import pytest

from scripts.linux_native_impedance_smoke import event, grid_cell_click

from .native_wx_support import isolated_native, pump, run_native, wait_until

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


def _diagnostic(message: str) -> None:
    """Preserve the last native phase even when a child crashes before reporting."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _header_ink(control: Any, column: int) -> frozenset[tuple[int, int]]:
    """Read a complete native header's green text raster, independent of its position."""
    import wx

    width, height = control.GetColSize(column), control.GetColLabelSize()
    bitmap = wx.Bitmap(width + 8, height + 8)
    dc = wx.MemoryDC(bitmap)
    try:
        dc.SetBackground(wx.Brush(wx.WHITE))
        dc.Clear()
        dc.SetDeviceOrigin(4 - control.GetColLeft(column), 4)
        control.DrawColLabel(dc, column)
    finally:
        dc.SelectObject(wx.NullBitmap)
    image = bitmap.ConvertToImage()
    pixels = {
        (x, y)
        for y in range(image.GetHeight())
        for x in range(image.GetWidth())
        if image.GetGreen(x, y) > image.GetRed(x, y) + 20
        and image.GetGreen(x, y) > image.GetBlue(x, y) + 20
    }
    assert pixels, (column, "Native header did not draw its label text")
    left, top = min(x for x, _y in pixels), min(y for _x, y in pixels)
    return frozenset((x - left, y - top) for x, y in pixels)


def assert_rendered_compact_columns(control: Any) -> None:
    """Measure the fonts and drawing actually used by compact workbook columns."""
    import wx
    import wx.grid as gridlib

    assert isinstance(control, gridlib.Grid), (
        "The workbook must use the same native grid sizing as Variants; "
        "DataView adds platform header reservations to compact columns."
    )
    bitmap = wx.Bitmap(max(control.GetColSize(col) for col in (0, 1, 4)) + 12, 120)
    dc = wx.MemoryDC(bitmap)
    label_colour = control.GetLabelTextColour()
    label_background = control.GetLabelBackgroundColour()
    control.SetLabelTextColour(wx.Colour(0, 120, 0))
    control.SetLabelBackgroundColour(wx.WHITE)
    try:
        for col in (0, 1, 4):
            compact_width = control.GetColSize(col)
            compact_ink = _header_ink(control, col)
            try:
                control.SetColSize(col, compact_width + control.FromDIP(120))
                assert _header_ink(control, col) == compact_ink, (
                    col,
                    "Compact native header clips or truncates its rendered text",
                )
            finally:
                control.SetColSize(col, compact_width)
            dc.SetFont(control.GetLabelFont())
            header_width = dc.GetTextExtent(control.GetColLabelValue(col)).width
            painted: list[Any] = []
            for row in range(control.GetNumberRows()):
                rect = wx.Rect(
                    4, 4, control.GetColSize(col) - 1, control.GetRowSize(row) - 1
                )
                attr = gridlib.GridCellAttr()
                attr.SetFont(control.GetCellFont(row, col))
                attr.SetTextColour(control.GetCellTextColour(row, col))
                attr.SetBackgroundColour(control.GetCellBackgroundColour(row, col))
                draw_text = dc.DrawText
                draw_checkbox = wx.RendererNative.Get().DrawCheckBox

                def observe_text(
                    text: str,
                    x: int,
                    y: int,
                    _rect: Any = rect,
                    _col: int = col,
                    _painted: list[Any] = painted,
                    _draw: Any = draw_text,
                    _font: Any = control.GetCellFont(row, col),
                ) -> None:
                    assert dc.GetFont() == _font
                    width, height = dc.GetTextExtent(text)
                    bounds = wx.Rect(x, y, width, height)
                    assert _rect.Contains(bounds), (
                        _col,
                        text,
                        tuple(_rect),
                        tuple(bounds),
                    )
                    _painted.append(width)
                    _draw(text, x, y)

                def observe_checkbox(
                    native: Any,
                    window: Any,
                    context: Any,
                    bounds: Any,
                    flags: int = 0,
                    _rect: Any = rect,
                    _painted: list[Any] = painted,
                    _draw: Any = draw_checkbox,
                ) -> None:
                    assert _rect.Contains(bounds), (tuple(_rect), tuple(bounds))
                    _painted.append(bounds.width)
                    _draw(window, context, bounds, flags)

                with (
                    patch.object(dc, "DrawText", observe_text),
                    patch.object(wx.RendererNative, "DrawCheckBox", observe_checkbox),
                ):
                    control.GetCellRenderer(row, col).Draw(
                        control, attr, dc, rect, row, col, False
                    )
                assert painted, (
                    col,
                    "Renderer must draw the displayed text or checkbox",
                )
            ink = max([header_width] + painted)
            assert ink <= control.GetColSize(col) <= ink + control.FromDIP(6), (
                col,
                control.GetColLabelValue(col),
                ink,
                control.GetColSize(col),
            )
    finally:
        control.SetLabelTextColour(label_colour)
        control.SetLabelBackgroundColour(label_background)
        dc.SelectObject(wx.NullBitmap)


@pytest.mark.parametrize(
    "enlarged", [False, True], ids=["normal-font", "enlarged-font"]
)
def test_native_rendered_compact_headers_cells_and_checkbox(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    enlarged: bool,
) -> None:
    """Actual body/header rendering determines compact Incl., Row and Target widths."""
    if isolated_native(request, "KICAD_IMPEDANCE_RENDERED_COMPACT_CHILD"):
        return
    from .test_impedance_draft_dialog import _board, _intent

    name = "_impedance_rendered_compact_native"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, name, package)
    dialog_api = importlib.import_module(f"{name}.dialog")
    model = importlib.import_module(f"{name}.model")
    service = importlib.import_module(f"{name}.service")
    api = type("Api", (), {"model": model})
    snapshot = _board(api)

    def exercise(frame: Any, wx: Any) -> None:
        dialog = dialog_api.ImpedanceDialog(
            frame,
            _intent(api),
            snapshot,
            preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
            refresh_snapshot=lambda: snapshot,
            fetch_stackup_catalog=lambda *_args, **_kwargs: (),
        )
        dialog.Show()
        pump(wx)
        if enlarged:
            import wx.grid as gridlib

            if isinstance(dialog.sections, gridlib.Grid):
                dialog.sections.SetDefaultCellFont(
                    dialog.sections.GetDefaultCellFont().Scaled(1.5)
                )
                dialog.sections.SetLabelFont(dialog.sections.GetLabelFont().Scaled(1.5))
            else:
                dialog.sections.SetFont(dialog.sections.GetFont().Scaled(1.5))
        with patch.object(
            dialog.sections, "fit_columns", wraps=dialog.sections.fit_columns
        ) as fit_columns:
            changed = wx.SysColourChangedEvent()
            changed.SetEventObject(dialog.sections)
            dialog.sections.GetEventHandler().ProcessEvent(changed)
            wait_until(
                wx,
                lambda: fit_columns.call_count == 1,
                reason="Changed native metrics did not remeasure workbook content",
            )
        try:
            assert_rendered_compact_columns(dialog.sections)
        finally:
            dialog.Destroy()
            pump(wx)

    run_native(exercise)


def assert_columns_fit(
    control: Any, *, phase: str = "workbook", expected_rows: Optional[int] = None
) -> None:
    """Await compact geometry measured with each font actually used for drawing."""
    import wx
    import wx.grid as gridlib

    started = time.monotonic()
    assert isinstance(control, gridlib.Grid)
    rows = control.GetItemCount()
    if expected_rows is not None:
        assert rows == expected_rows, (
            phase,
            "unexpected row count",
            rows,
            expected_rows,
        )
    assert control.GetNumberCols() == 11
    contents: list[tuple[str, ...]] = []
    bounds: list[tuple[int, int]] = []
    dc = wx.ClientDC(control)
    try:
        for index in range(11):
            strings = [control.GetColLabelValue(index)]
            if index:
                strings.extend(control.GetTextValue(row, index) for row in range(rows))
            contents.append(tuple(strings))
            dc.SetFont(control.GetLabelFont())
            widths = [dc.GetTextExtent(strings[0]).width]
            for row in range(rows):
                dc.SetFont(control.GetCellFont(row, index))
                if index:
                    widths.append(
                        dc.GetTextExtent(control.GetTextValue(row, index)).width
                    )
            if index == 8:
                dc.SetFont(control.GetDefaultCellFont())
                widths.append(dc.GetTextExtent("!=").width)
            if index == 0:
                widths.append(
                    wx.RendererNative.Get()
                    .GetCheckBoxSize(control, wx.CONTROL_CELL)
                    .width
                )
            ink = max(widths)
            dc.SetFont(control.GetDefaultCellFont())
            maximum = ink + (
                control.FromDIP(6)
                if index in (0, 1, 4)
                else 4 * dc.GetTextExtent("M").width
            )
            bounds.append((ink, maximum))
    finally:
        del dc

    def allocated() -> bool:
        """Read final native allocation after deferred layout and calculator callbacks."""
        return control.GetItemCount() == rows and all(
            minimum <= control.GetColSize(index) <= maximum
            for index, (minimum, maximum) in enumerate(bounds)
        )

    _diagnostic(f"Workbook sizing: {phase}, rows={rows}, awaiting allocation")
    try:
        wait_until(
            wx, allocated, reason=f"{phase}: native column allocation did not fit"
        )
    except AssertionError as error:
        geometry = [
            (
                index,
                control.GetColLabelValue(index),
                bounds[index],
                control.GetColSize(index),
            )
            for index in range(11)
        ]
        raise AssertionError(
            f"{phase}: after {time.monotonic() - started:.3f}s, "
            f"rows={control.GetItemCount()} expected={rows}; "
            f"columns=(index, title, allowed, allocated): {geometry}"
        ) from error
    assert contents == [
        (control.GetColLabelValue(index),)
        + tuple(control.GetTextValue(row, index) for row in range(rows) if index)
        for index in range(11)
    ], (phase, "Workbook contents changed while awaiting native geometry")
    _diagnostic(
        f"Workbook sizing: {phase}, allocation fitted in {time.monotonic() - started:.3f}s"
    )


@pytest.mark.parametrize(
    "enlarged", [False, True], ids=["normal-font", "enlarged-font"]
)
def test_native_compact_columns_shrink_resize_and_reopen_without_changing_state(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    enlarged: bool,
) -> None:
    """Real constructors retain compact toggles/targets at normal and larger fonts."""
    if isolated_native(request, "KICAD_IMPEDANCE_COMPACT_COLUMNS_CHILD"):
        return
    package_name = "_impedance_compact_columns_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    names = ("CLOCK", "DATA", "RESET")
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=tuple(
            model.Trace(
                f"route-{index}",
                "F.Cu",
                name,
                150_000,
                ((0, index * 2_000_000), (1_000_000, index * 2_000_000)),
            )
            for index, name in enumerate(names)
        ),
        net_classes=("RF",),
        net_class_memberships=tuple((name, ("RF",)) for name in names),
        net_class_context_digest="compact-column-class",
    )
    spec = model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        (model.LayerSettings("F.Cu", ("B.Cu",)),),
    )
    config = model.Config(enabled=True, specifications=(spec,))
    saved: list[Any] = []

    def exercise(frame: Any, wx: Any) -> None:
        import wx.grid as gridlib

        failures: list[BaseException] = []
        font = wx.Font(frame.GetFont())
        if enlarged:
            font.SetPointSize(
                max(font.GetPointSize() + 5, round(font.GetPointSize() * 1.5))
            )

        def open_dialog(value: Any) -> Any:
            """Construct actual product controls, then use an explicit native font."""
            dialog = dialog_api.ImpedanceDialog(
                frame,
                value,
                snapshot,
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=lambda: snapshot,
                fetch_stackup_catalog=lambda _count, **_kwargs: (),
                save_config=saved.append,
            )
            dialog.sections.SetDefaultCellFont(font)
            dialog.sections.SetLabelFont(font)
            assert (
                dialog.sections.GetDefaultCellFont().GetPointSize()
                == font.GetPointSize()
            )
            dialog._fit_workbook_columns()
            return dialog

        def state(dialog: Any) -> tuple[object, ...]:
            """Observe settings, review, identities, selection and inclusion independently."""
            return (
                dialog.session.config,
                tuple(dialog._row_ids),
                dialog.sections.GetSelectedRow(),
                frozenset(dialog.session.included),
                tuple(
                    dialog.sections.GetToggleValue(row, 0)
                    for row in range(dialog.sections.GetItemCount())
                ),
            )

        def settle(dialog: Any, phase: str) -> None:
            """Wait for the first image stamp before comparing subsequent UI state."""
            wait_until(wx, lambda: dialog.preview_pane.ready)
            assert_columns_fit(dialog.sections, phase=phase, expected_rows=3)

        def refit_and_resize(dialog: Any, phase: str) -> None:
            """Sizing alone cannot change data or turn spare width into data columns."""
            settle(dialog, phase)
            before = state(dialog)
            compact = tuple(dialog.sections.GetColSize(index) for index in (0, 4))
            original_size = dialog.GetSize()
            with patch.object(
                dialog.sections, "fit_columns", wraps=dialog.sections.fit_columns
            ) as fit_columns:
                for width in (original_size.width + 400, original_size.width):
                    dialog.SetSize((width, original_size.height))
                    wait_until(
                        wx,
                        lambda width=width: (dialog.GetSize().width == width),
                        reason=f"{phase}: resize did not settle",
                    )
                    dialog.Layout()
                    assert_columns_fit(dialog.sections, phase=f"{phase}: width {width}")
                    assert (
                        tuple(dialog.sections.GetColSize(index) for index in (0, 4))
                        == compact
                    )
                    assert state(dialog) == before
                assert fit_columns.call_count == 0, (
                    "Native viewport resize remeasured unchanged workbook content"
                )

        dialog = open_dialog(config)
        initial_widths: tuple[int, ...] = ()

        def interact() -> None:
            nonlocal initial_widths
            try:
                settle(dialog, "compact initial")
                event(wx, dialog.sections, gridlib.EVT_GRID_SELECT_CELL, 1)
                wait_until(
                    wx,
                    lambda: dialog.preview_pane.ready
                    and dialog.sections.GetSelectedRow() == 1,
                )
                event(wx, dialog.sections, gridlib.EVT_GRID_CELL_LEFT_CLICK, 0)
                wait_until(
                    wx, lambda: dialog._row_ids[0] not in dialog.session.included
                )
                refit_and_resize(dialog, "compact selection and inclusion")
                initial_widths = tuple(
                    dialog.sections.GetColSize(index) for index in (0, 4)
                )

                long_target = replace(spec, target_ohms="12345678901234567890.125")
                dialog.session.replace_specifications((long_target,))
                assert dialog._refresh_rows()
                refit_and_resize(dialog, "long target")
                long_width = dialog.sections.GetColSize(4)
                assert long_width > initial_widths[1]

                dialog.session.replace_specifications((spec,))
                assert dialog._refresh_rows()
                refit_and_resize(dialog, "short target restored")
                assert (
                    tuple(dialog.sections.GetColSize(index) for index in (0, 4))
                    == initial_widths
                )
                assert dialog.session.config.specifications == (spec,)
                assert not dialog.session.approved
                assert dialog.session.config.review_tracking.layers == ()
                event(wx, dialog.FindWindow(wx.ID_OK), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_OK
            assert len(saved) == 1
            assert saved[0].specifications == (spec,)
            assert saved[0].reviewed_digest == ""
            assert saved[0].review_tracking.layers == ()
        finally:
            dialog.Destroy()
            pump(wx)

        reopened = open_dialog(model.Config.from_dict(saved[0].to_dict()))

        def interact_reopened() -> None:
            try:
                refit_and_resize(reopened, "compact reopened")
                assert (
                    tuple(reopened.sections.GetColSize(index) for index in (0, 4))
                    == initial_widths
                )
                assert reopened.session.config.specifications == saved[0].specifications
                assert not reopened.session.approved
                assert reopened.session.config.review_tracking.layers == ()
                event(wx, reopened.FindWindow(wx.ID_CANCEL), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                if reopened and reopened.IsModal():
                    reopened.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact_reopened)
        try:
            result = reopened.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
            assert len(saved) == 1
        finally:
            reopened.Destroy()
            pump(wx)

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]


def test_native_workbook_sizes_offscreen_rows_refresh_retry_and_empty_state(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """An offscreen long net fits, then columns shrink with short or absent rows."""
    if isolated_native(request, "KICAD_IMPEDANCE_WORKBOOK_SIZING_CHILD"):
        return
    package_name = "_impedance_workbook_sizing_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    model = importlib.import_module(f"{package_name}.model")
    dialog_api = importlib.import_module(f"{package_name}.dialog")
    service = importlib.import_module(f"{package_name}.service")
    count = 900
    long_name = "OFFSCREEN_IMPEDANCE_BRANCH_" + "RF_TRACE_" * 10

    def snapshot(
        *, long_net: bool = False, empty: bool = False, row_count: int = count
    ) -> Any:
        """Keep rows physically separate and place one long name deep in the model."""
        names = tuple(
            long_name if long_net and index == 450 else f"SIGNAL_{index:03d}"
            for index in range(row_count)
        )
        return model.BoardSnapshot(
            layers=("F.Cu", "B.Cu"),
            traces=()
            if empty
            else tuple(
                model.Trace(
                    f"route-{index}",
                    "F.Cu",
                    name,
                    150_000,
                    ((0, index * 2_000_000), (100_000, index * 2_000_000)),
                )
                for index, name in enumerate(names)
            ),
            net_classes=("RF",),
            net_class_memberships=tuple((name, ("RF",)) for name in names),
            net_class_context_digest="deterministic-native-sizing-classes",
            copper_features=(
                model.CopperFeature(
                    "covered-pad",
                    names[0],
                    "F.Cu",
                    "pad",
                    "rect",
                    (0, 0),
                    1_000_000,
                    1_000_000,
                ),
            ),
        )

    current = [snapshot()]
    unavailable = [False]
    saved: list[Any] = []
    config = model.Config(
        specifications=(
            model.Specification(
                "rf",
                "RF",
                "50",
                "single_ended",
                "RF",
                (model.LayerSettings("F.Cu", ("B.Cu",)),),
            ),
        )
    )

    def refresh() -> Any:
        """Expose a recoverable real board-read failure at the snapshot seam."""
        if unavailable[0]:
            raise RuntimeError("Native sizing test board is temporarily unavailable")
        return current[0]

    def exercise(frame: Any, wx: Any) -> None:
        import wx.grid as gridlib

        started = time.monotonic()

        def phase(label: str) -> None:
            """Leave the current workflow stage in crash and timeout diagnostics."""
            _diagnostic(
                f"Workbook sizing phase: {label} at {time.monotonic() - started:.3f}s"
            )

        def open_dialog(value: Any) -> Any:
            """Use persisted configuration and the real constructor on reopening."""
            phase("construct dialog")
            return dialog_api.ImpedanceDialog(
                frame,
                value,
                current[0],
                preview=lambda *_args, **_kwargs: service.CapturedImage(_PNG),
                refresh_snapshot=refresh,
                fetch_stackup_catalog=lambda _count, **_kwargs: (),
                save_config=saved.append,
            )

        dialog = open_dialog(config)
        failures: list[BaseException] = []

        def resize_without_stretching(label: str) -> None:
            """Await real resizing while retaining widths, selection and inclusion."""
            original_size = dialog.GetSize()
            intrinsic = tuple(dialog.sections.GetColSize(index) for index in range(11))
            selected = dialog.sections.GetSelectedRow()
            included = set(dialog.session.included)
            with patch.object(
                dialog.sections, "fit_columns", wraps=dialog.sections.fit_columns
            ) as fit_columns:
                for width in (original_size.width + 600, original_size.width):
                    previous_client_width = dialog.sections.GetClientSize().width
                    dialog.SetSize((width, original_size.height))
                    wait_until(
                        wx,
                        lambda width=width,
                        previous_client_width=previous_client_width: (
                            dialog.GetSize().width == width
                            and dialog.sections.GetClientSize().width
                            != previous_client_width
                        ),
                        reason=f"{label}: native dialog did not resize to {width}",
                    )
                    assert_columns_fit(
                        dialog.sections, phase=f"{label}: resize to {width}"
                    )
                    assert (
                        tuple(dialog.sections.GetColSize(index) for index in range(11))
                        == intrinsic
                    )
                    dialog.Layout()
                    assert_columns_fit(
                        dialog.sections, phase=f"{label}: explicit Layout"
                    )
                    assert (
                        tuple(dialog.sections.GetColSize(index) for index in range(11))
                        == intrinsic
                    )
                    assert dialog.sections.GetSelectedRow() == selected
                    assert dialog.session.included == included
                assert fit_columns.call_count == 0, (
                    "Native viewport resize remeasured unchanged workbook content"
                )

        def uncheck_selected_row() -> tuple[str, set[str]]:
            """Exercise actual selection and include events before refitting."""
            selected = 2
            event(wx, dialog.sections, gridlib.EVT_GRID_SELECT_CELL, selected)
            wait_until(wx, lambda: dialog.sections.GetSelectedRow() == selected)
            selected_id = dialog._row_ids[selected]
            event(wx, dialog.sections, gridlib.EVT_GRID_CELL_LEFT_CLICK, selected)
            wait_until(wx, lambda: selected_id not in dialog.session.included)
            pump(wx)
            assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected_id
            return selected_id, set(dialog.session.included)

        def interact() -> None:
            try:
                phase("initial rows")
                assert dialog.sections.GetItemCount() == count
                assert_columns_fit(
                    dialog.sections, phase="initial rows", expected_rows=count
                )
                resize_without_stretching("initial rows")
                initial_net_width = dialog.sections.GetColSize(6)
                stub_id = next(
                    section.section_id
                    for section in dialog.session.analysis.sections
                    if section.exclusion_reason == model.EXCLUSION_COVERED_STUB
                )
                stub = dialog._row_ids.index(stub_id)
                assert stub == count - 1
                assert dialog.sections.GetTextValue(stub, 7) == "Covered stub"
                assert dialog.sections.GetTextValue(stub, 8) == ""
                assert dialog.sections.GetTextValue(stub, 9) == ""
                assert not dialog.sections.GetToggleValue(stub, 0)

                phase("populate offscreen long net")
                current[0] = snapshot(long_net=True)
                assert dialog._refresh_rows()
                long_row = next(
                    row
                    for row in range(dialog.sections.GetItemCount())
                    if dialog.sections.GetTextValue(row, 6) == long_name
                )
                assert long_row > 100
                assert long_row < count - 100
                assert_columns_fit(
                    dialog.sections, phase="offscreen long net", expected_rows=count
                )

                def long_row_is_offscreen() -> bool:
                    """Wait for GTK's row geometry as well as its column allocation."""
                    rectangle = dialog.sections.CellToRect(long_row, 6)
                    _x, y = dialog.sections.CalcScrolledPosition(
                        rectangle.x, rectangle.y
                    )
                    return y >= dialog.sections.GetGridWindow().GetClientSize().height

                wait_until(
                    wx,
                    long_row_is_offscreen,
                    reason=f"Long net row {long_row} did not reach offscreen geometry",
                )
                assert long_row_is_offscreen()
                long_net_width = dialog.sections.GetColSize(6)
                assert long_net_width > initial_net_width
                phase("retain deep vertical and horizontal viewport")
                long_id = dialog._row_ids[long_row]
                long_number = dialog.sections.GetTextValue(long_row, 1)
                grid_cell_click(wx, dialog.sections, long_row, 9)
                wait_until(
                    wx,
                    lambda: dialog.sections.GetSelectedRow() == long_row
                    and dialog.preview_pane.ready
                    and dialog.preview_pane._sections[
                        dialog.preview_pane._index
                    ].section_id
                    == long_id,
                )
                before_scroll = tuple(dialog.sections.GetViewStart())
                assert before_scroll[0] > 0 and before_scroll[1] > 0
                assert dialog.sections.GetGridCursorCol() == 9

                def assert_selected_visible(row: int) -> None:
                    """Inspect the actual selected cell in the scrolled native viewport."""
                    rectangle = dialog.sections.CellToRect(row, 9)
                    x, y = dialog.sections.CalcScrolledPosition(
                        rectangle.x, rectangle.y
                    )
                    visible = wx.Rect(
                        x, y, rectangle.width, rectangle.height
                    ).Intersect(dialog.sections.GetGridWindow().GetClientRect())
                    assert not visible.IsEmpty(), (row, tuple(visible))

                assert_selected_visible(long_row)
                before_included = set(dialog.session.included)
                key = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                key.SetEventObject(dialog.sections)
                key.SetKeyCode(wx.WXK_SPACE)
                assert dialog.sections.GetEventHandler().ProcessEvent(key)
                assert long_id not in dialog.session.included
                pump(wx)
                moved_row = dialog._row_ids.index(long_id)
                assert moved_row == count - 1
                assert dialog.sections.GetSelectedRow() == moved_row
                assert dialog.sections.GetGridCursorRow() == moved_row
                assert dialog.sections.GetGridCursorCol() == 9
                assert dialog.sections.GetTextValue(moved_row, 1) == long_number
                assert dialog.sections.GetViewStart()[0] == before_scroll[0]
                assert dialog.sections.GetViewStart()[1] > before_scroll[1]
                assert_selected_visible(moved_row)
                assert dialog.session.included == before_included - {long_id}
                assert (
                    dialog.preview_pane._sections[dialog.preview_pane._index].section_id
                    == long_id
                )
                moved_scroll = tuple(dialog.sections.GetViewStart())
                assert dialog._refresh_rows()
                pump(wx)
                assert dialog._row_ids[dialog.sections.GetSelectedRow()] == long_id
                assert dialog.sections.GetGridCursorCol() == 9
                assert tuple(dialog.sections.GetViewStart()) == moved_scroll
                assert_selected_visible(moved_row)
                for code, row in (
                    (wx.WXK_UP, moved_row - 1),
                    (wx.WXK_DOWN, moved_row),
                ):
                    key = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                    key.SetEventObject(dialog.sections)
                    key.SetKeyCode(code)
                    assert dialog.sections.GetEventHandler().ProcessEvent(key)
                    wait_until(
                        wx,
                        lambda row=row: dialog.sections.GetSelectedRow() == row
                        and dialog.preview_pane.ready
                        and dialog.preview_pane._sections[
                            dialog.preview_pane._index
                        ].section_id
                        == dialog._row_ids[row],
                    )
                    assert dialog.sections.GetGridCursorCol() == 9
                    assert dialog.sections.GetViewStart()[0] == before_scroll[0]
                    assert_selected_visible(row)

                # The large model is needed to challenge offscreen sampling.
                # Keep subsequent failure/retry cases small and bounded.
                phase("replace long net with short rows")
                current[0] = snapshot(row_count=12)
                assert dialog._refresh_rows()
                assert_columns_fit(
                    dialog.sections, phase="short rows", expected_rows=12
                )
                assert dialog.sections.GetColSize(6) < long_net_width

                phase("read failure clears rows")
                unavailable[0] = True
                assert not dialog._refresh_rows()
                assert dialog.sections.GetItemCount() == 0
                assert "temporarily unavailable" in dialog.status.GetValue()
                assert_columns_fit(
                    dialog.sections, phase="read failure", expected_rows=0
                )
                assert dialog.sections.GetColSize(6) < long_net_width

                phase("retry restores short rows")
                unavailable[0] = False
                current[0] = snapshot(row_count=12)
                event(wx, dialog.retry_button, wx.EVT_BUTTON)
                assert dialog.sections.GetItemCount() == 12
                assert_columns_fit(dialog.sections, phase="retry", expected_rows=12)

                selected_id, included = uncheck_selected_row()
                resize_without_stretching("selected retry rows")
                dialog._populate_sections()
                assert_columns_fit(
                    dialog.sections, phase="repopulate selected rows", expected_rows=12
                )
                assert dialog._row_ids[dialog.sections.GetSelectedRow()] == selected_id
                assert dialog.session.included == included
                phase("save short rows for reopening")
                event(wx, dialog.FindWindow(wx.ID_OK), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
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

        dialog = open_dialog(model.Config.from_dict(saved[0].to_dict()))

        def interact_reopened() -> None:
            try:
                phase("reopened short rows")
                assert_columns_fit(
                    dialog.sections, phase="reopened rows", expected_rows=12
                )
                assert dialog.session.config.specifications == saved[0].specifications
                assert dialog.session.config.stackup == saved[0].stackup
                assert dialog.session.config.width_results == saved[0].width_results
                assert dialog.session.included == {
                    section.section_id
                    for section in dialog.session.analysis.sections
                    if not section.exclusion_reason
                }
                reopened_net_width = dialog.sections.GetColSize(6)
                resize_without_stretching("reopened rows")
                phase("empty board clears rows")
                current[0] = snapshot(empty=True, row_count=12)
                assert dialog._refresh_rows()
                assert_columns_fit(
                    dialog.sections, phase="empty board", expected_rows=0
                )
                assert dialog.sections.GetColSize(6) < reopened_net_width
                resize_without_stretching("empty board")
                phase("close with a queued native metrics update")
                changed = wx.SysColourChangedEvent()
                changed.SetEventObject(dialog.sections)
                dialog.sections.GetEventHandler().ProcessEvent(changed)
                event(wx, dialog.FindWindow(wx.ID_CANCEL), wx.EVT_BUTTON)
            except BaseException as error:
                failures.append(error)
                if dialog and dialog.IsModal():
                    dialog.EndModal(wx.ID_ABORT)

        wx.CallAfter(interact_reopened)
        try:
            result = dialog.ShowModal()
            if failures:
                raise failures[0]
            assert result == wx.ID_CANCEL
        finally:
            dialog.Destroy()
            wait_until(wx, lambda: not dialog, reason="Reopened workbook did not close")

    try:
        run_native(exercise)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(package_name + "."):
                del sys.modules[name]
