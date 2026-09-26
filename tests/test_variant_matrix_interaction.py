"""Selection, actions, navigation, and dragging through real wx grid controls."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock

import pytest

from . import native_window_support, variant_matrix_native_test_support as support
from .native_wx_support import pump, wait_until
from .variant_matrix_native_test_support import MatrixHarness, make_model, native_marks
from .variant_matrix_render_test_support import pixel
from .variant_model_test_support import Snapshot, State, changed

modules = support.modules
matrix = support.matrix
window_ui = native_window_support.window_ui

pytestmark = native_marks


@pytest.mark.parametrize("cleared", [False, True])
def test_refresh_resolves_selected_reference_without_reselecting_pcb(
    matrix: Any, cleared: bool
) -> None:
    """A renamed selected UUID updates labels, not the physical PCB selection."""

    def check(h: MatrixHarness) -> None:
        view = h.view
        h.click(0, view.model.column_for("A", "lcsc"))
        if cleared:
            view.ClearSelection()
            pump(h.wx)
        h.targets.clear()
        h.physical_notifications.clear()
        states = tuple(
            replace(state, reference="R9")
            if state.component_id == "component-1"
            else state
            for state in view.model.snapshot.components
        )
        latest = h.model_module.MatrixModel(Snapshot(states))
        view.set_model(latest)
        pump(h.wx)
        assert view.selected_target.reference == "R9"
        assert view.selected_target.label.startswith("R9")
        assert view.selected_target.component_id == "component-1"
        assert view.selected_component_ids() == (() if cleared else ("component-1",))
        assert h.targets[-1] == view.selected_target
        assert h.physical_notifications == []
        assert view.selected_physical_component_ids() == (
            () if cleared else ("component-1",)
        )

    matrix(check)


@pytest.mark.parametrize(
    "field,enabled",
    [
        ("ref", True),
        ("stock", True),
        ("lcsc", True),
        ("correction", True),
        ("lcsc", False),
        ("correction", False),
        ("bom", False),
        ("pop", True),
    ],
)
def test_native_cell_activation_uses_current_field_and_write_availability(
    matrix: Any, field: str, enabled: bool
) -> None:
    """Shared corrections, readonly inspection, assignment and disabled edits stay distinct."""

    def check(h: MatrixHarness) -> None:
        view = h.view
        variant = None if field in ("ref", "correction") else "B"
        col = view.model.column_for(variant, field)
        view.set_mutations_enabled(enabled)

        def edit(target: Any, value: bool) -> None:
            h.edits.append((target, value))
            view.set_model(
                h.model_module.MatrixModel(
                    changed(
                        view.model.snapshot,
                        component=target.component_id,
                        variants=(target.variant,),
                        **{target.field: value},
                    )
                )
            )

        view._on_edit = edit
        h.click(1, col, double=True)
        assert view.selected_target.component_id == "component-2"
        assert view.selected_target.variant == variant
        assert len(h.edits) == int(field == "pop" and enabled)
        if h.edits:
            target, value = h.edits[0]
            assert (target.component_id, target.variant, target.field, value) == (
                "component-2",
                "B",
                "pop",
                False,
            )
            assert view.model.get_value(1, col) is False
            assert all(
                view.model.get_value(1, view.model.column_for(name, "pop"))
                for name in ("", "A", "C", "D")
            )
        assert len(h.activations) == int(
            enabled and variant is not None and field not in {"bom", "pop"}
        )
        assert [name for name, _ in h.actions] == (
            ["correction"] if enabled and field == "correction" else []
        )
        assert view.selected_physical_component_ids() == ("component-2",)

    matrix(check)


@pytest.mark.parametrize(
    "command", ["copy", "paste", "details", "correction", "activate", "toggle"]
)
@pytest.mark.parametrize("enabled", [False, True])
def test_keyboard_actions_follow_reordered_semantic_targets(
    matrix: Any, command: str, enabled: bool
) -> None:
    """Keyboard actions use the new column owner and never mutate informational cells."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        view.reorder_variant("C", "A")
        view.set_mutations_enabled(enabled)
        field = (
            "correction"
            if command == "correction"
            else "bom"
            if command == "toggle"
            else "lcsc"
        )
        col = view.model.column_for(None if field == "correction" else "C", field)
        view._focus_cell(1, col)
        key = {
            "copy": ord("C"),
            "paste": ord("V"),
            "details": wx.WXK_F2,
            "toggle": wx.WXK_SPACE,
        }.get(command, wx.WXK_RETURN)
        h.key(key, command=command in ("copy", "paste"))
        targets = (
            [target for _name, target in h.actions]
            + h.activations
            + [target for target, _value in h.edits]
        )
        assert len(targets) == int(enabled or command in ("copy", "details"))
        if not targets:
            return
        view.reorder_variant("C", None)
        assert (targets[0].component_id, targets[0].variant, targets[0].field) == (
            "component-2",
            None if field == "correction" else "C",
            field,
        )
        if command == "toggle":
            assert h.edits[0][1] is False
        elif command != "activate":
            assert h.actions[0][0] == command

    matrix(check)


@pytest.mark.parametrize(
    "gesture,rows,variant",
    [
        ("click", (1,), "A"),
        ("component_drag", (1, 2, 3), "A"),
        ("first_ref", (1,), None),
        ("same_ref", (1,), None),
        ("initial_shift", (2,), "A"),
        ("shift", (1, 2, 3), "A"),
        ("ctrl", (1, 3), "A"),
        ("ctrl_clear", (), "A"),
        ("keyboard", (2,), "A"),
        ("shift_keyboard", (1, 2), "A"),
        ("enter_flag", (2,), "A"),
        ("enter_ref", (2,), None),
        ("up_boundary", (1,), "A"),
        ("end_boundary", (3,), "A"),
        ("cross_plain", (2,), "B"),
        ("cross_shift", (2,), "B"),
        ("cross_command", (2,), "B"),
        ("cross_keyboard", (3,), "B"),
        ("modified_flag", (1, 2, 3), "A"),
        ("clear", (), "A"),
        ("programmatic", (1, 3), "A"),
    ],
)
def test_selection_notifications_read_completed_native_selection(
    matrix: Any, gesture: str, rows: tuple[int, ...], variant: Optional[str]
) -> None:
    """Native row gestures publish one variant's complete blocks and matching PCB UUIDs."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        expected = tuple(f"component-{row}" for row in rows)
        assert h.physical_notifications == []
        col = (
            0
            if gesture in ("first_ref", "same_ref", "enter_ref")
            else view.model.column_for(
                "A",
                {"cross_keyboard": "price", "enter_flag": "bom"}.get(gesture, "value"),
            )
        )
        if gesture != "initial_shift":
            h.click(0, col)
        h.physical_notifications.clear()
        if gesture == "component_drag":
            h.drag_cells(col)
        elif gesture == "initial_shift":
            h.click(1, col, shift=True)
        elif gesture in ("shift", "ctrl"):
            h.click(2, col, shift=True)
            if gesture == "ctrl":
                h.click(1, col, command=True)
        elif gesture == "ctrl_clear":
            h.click(0, col, command=True)
        elif gesture in (
            "keyboard",
            "shift_keyboard",
            "up_boundary",
            "end_boundary",
            "cross_keyboard",
            "enter_flag",
            "enter_ref",
        ):
            if gesture in ("end_boundary", "cross_keyboard"):
                h.click(2, col)
                h.click(0, col, shift=True)
            key = {
                "up_boundary": wx.WXK_UP,
                "cross_keyboard": wx.WXK_RIGHT,
                "enter_flag": wx.WXK_RETURN,
                "enter_ref": wx.WXK_RETURN,
            }.get(gesture, wx.WXK_DOWN)
            h.key(key, native=True, shift=gesture == "shift_keyboard")
        elif gesture.startswith("cross_"):
            h.click(2, col, shift=True)
            h.click(
                1,
                view.model.column_for("B", "params"),
                shift=gesture == "cross_shift",
                command=gesture == "cross_command",
            )
        elif gesture == "modified_flag":
            h.click(2, view.model.column_for("A", "bom"), shift=True)
            assert h.edits == []
        elif gesture == "clear":
            view.ClearSelection()
            view.SetGridCursor(1, col)
            target = view.selected_target
            assert target.component_id == "component-2"
            view.reorder_variant("B", "A")
            assert view.selected_target == target
        elif gesture == "programmatic":
            view.select_components(("component-1", "component-3"), "A")
        else:
            h.click(0, col)
        pump(wx)
        assert view.selected_target.variant == variant
        assert view.selected_component_ids() == (() if variant is None else expected)
        assert h.selection() == {
            (component, variant, column.key)
            for component in expected
            for column in view.model.columns
            if variant is not None and column.variant == variant
        }
        assert view.selected_physical_component_ids() == expected
        assert h.physical_notifications[-1] == expected
        assert all(len(ids) == len(set(ids)) for ids in h.physical_notifications)
        previous = list(h.physical_notifications)
        target, selected = view.selected_target, h.selection()
        windows = (
            view.GetTable(),
            view.GetGridWindow(),
            view.GetFrozenColGridWindow(),
            view.GetGridColLabelWindow(),
        )
        for iteration in range(8 if gesture in {"click", "component_drag"} else 1):
            h.refresh_catalog(f"Catalog update {iteration}")
            assert h.physical_notifications == previous
            assert view.selected_target == target and h.selection() == selected
            assert view.selected_physical_component_ids() == expected
            assert (
                view.GetTable(),
                view.GetGridWindow(),
                view.GetFrozenColGridWindow(),
                view.GetGridColLabelWindow(),
            ) == windows
            assert len(view.GetSelectionBlockTopLeft()) <= len(expected)
            captured = view.capture_state().components
            assert len(captured) == len(set(captured)) == len(expected)
            assert set(captured) == set(expected)

    matrix(check)


@pytest.mark.parametrize("after_key", ["mouse", "refresh", "sort", "reorder"])
def test_pending_navigation_respects_newer_selection_and_refresh(
    matrix: Any, after_key: str
) -> None:
    """Semantic changes retain arrow intent; a newer mouse batch supersedes it."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        h.click(0, view.model.column_for("A", "value"))
        col = view.model.column_for("B" if after_key == "mouse" else "A", "value")
        first, last = h.cell_point(0, col), h.cell_point(2, col)
        left, _ = h.viewport()
        header_point = wx.Point(
            (view.GetColLeft(col) + view.GetColRight(col)) // 2 - left,
            (view._group_header_height + view.GetColLabelSize()) // 2,
        )
        h.key(wx.WXK_DOWN, native=True, pump_events=False)
        if after_key == "mouse":
            for (window, point), shift in ((first, False), (last, True)):
                for kind in (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP):
                    h.mouse(window, kind, point, shift=shift, pump_events=False)
        elif after_key == "refresh":
            view.set_model(make_model(h.model_module))
        elif after_key == "reorder":
            view.reorder_variant("A", "D")
        else:
            for kind in (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP):
                h.mouse(
                    view.GetGridColLabelWindow(),
                    kind,
                    header_point,
                    down=kind == wx.wxEVT_LEFT_DOWN,
                    pump_events=False,
                )
            assert view._sort_state == ("A", "value", False)
        pump(wx)
        assert view.selected_target.variant == ("B" if after_key == "mouse" else "A")
        assert view.selected_component_ids() == (
            ("component-1", "component-2", "component-3")
            if after_key == "mouse"
            else ("component-2",)
        )
        assert {variant for _, variant, _ in h.selection()} == {
            view.selected_target.variant
        }

    matrix(check)


@pytest.mark.parametrize(
    "case",
    [
        "partial_filter",
        "empty_filter",
        "cursor_survives",
        "deleted_variant",
    ],
)
def test_structural_refresh_fails_closed_for_removed_selection_members(
    matrix: Any, case: str
) -> None:
    """Removed components or the active variant cannot redirect a saved block selection."""

    def check(h: MatrixHarness) -> None:
        view = h.view
        col = view.model.column_for("A", "lcsc")
        h.click(0, col)
        h.click(1, col, shift=case != "cursor_survives")
        if case == "cursor_survives":
            view.SetGridCursor(0, col)
        pump(h.wx)
        states = view.model.snapshot.components
        if case in ("partial_filter", "cursor_survives"):
            states = tuple(
                state for state in states if state.component_id == "component-1"
            )
        elif case == "empty_filter":
            states = ()
        else:
            states = tuple(state for state in states if state.variant_name != "A")
        view.set_model(h.model_module.MatrixModel(Snapshot(states)))
        pump(h.wx)
        expected = ("component-1",) if case == "partial_filter" else ()
        assert view.selected_physical_component_ids() == expected
        assert view.selected_component_ids() == expected
        if expected:
            assert view.selected_target.component_id == "component-1"
        else:
            assert view.selected_target is None
            assert h.selection() == set()

    matrix(check)


def test_saved_order_normalizes_without_losing_board_variants(matrix: Any) -> None:
    """One real grid applies validated model order and discards invalid width overrides."""

    def check(h: MatrixHarness) -> None:
        h.view.restore_preferences(
            {
                "variant_order": ["B", "B", None, "missing", 2, ""],
                "widths": {"ref": True, "value": -3, "lcsc": 10**50},
            }
        )
        assert h.order() == ("B", "", "A", "C", "D")
        assert h.view.capture_preferences()["widths"] == {}

    matrix(check)


@pytest.mark.parametrize(
    "variant,before", [("missing", "A"), ("B", "missing"), ("A", "A")]
)
def test_invalid_reorder_preserves_complete_state(
    matrix: Any, variant: str, before: str
) -> None:
    """Invalid destinations do not clear selection or move the viewport."""

    def check(h: MatrixHarness) -> None:
        h.click(1, h.view.model.column_for("A", "value"))
        saved = h.view.capture_state()
        h.view.reorder_variant(variant, before)
        assert h.view.capture_state() == saved

    matrix(check)


def test_group_order_survives_empty_filter_and_native_variant_inventory_changes(
    matrix: Any,
) -> None:
    """Presentation order remains independent of filtering and appends new native variants."""

    def check(h: MatrixHarness) -> None:
        view = h.view
        view.reorder_variant("B", "")
        view.reorder_variant("A", "")
        view.set_model(
            h.model_module.MatrixModel(Snapshot((), names=("", "A", "B", "C", "D")))
        )
        view.reorder_variant("B", "A")
        assert view.GetNumberRows() == 0
        view.set_model(make_model(h.model_module, variants=("", "A", "C", "E")))
        assert h.order() == ("A", "", "C", "E")
        assert view.selected_target is None
        assert view.GetNumberRows() == 3

    matrix(check)


@pytest.mark.parametrize(
    "mode",
    [
        "partial",
        "visible",
        "exact",
        "oversized",
        "scaled",
        "empty",
        "last",
        "divider",
        "double",
    ],
)
def test_name_click_reveals_variant_without_changing_component_selection(
    matrix: Any, mode: str
) -> None:
    """Click-to-reveal uses live widths, clipping and native scroll limits."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        if mode == "empty":
            view.set_model(make_model(h.model_module, populated=False))
        else:
            view.select_components(("component-1", "component-2"), "A")
        if mode == "scaled":
            view.SetDefaultCellFont(view.GetDefaultCellFont().Scaled(1.5))
            pump(wx)
        variant = "D" if mode == "last" else "A"
        left, right = h.bounds(variant)
        budget = (
            right
            - left
            + (0 if mode == "exact" else -80 if mode == "oversized" else 60)
        )
        for _ in range(3):
            size = h.frame.GetClientSize()
            h.frame.SetClientSize(
                (
                    size.width + budget - view.GetGridWindow().GetClientSize().width,
                    size.height,
                )
            )
            pump(wx)
        origin = view.GetGridWindowOffset(view.GetGridWindow()).x
        view.Scroll(left - origin + (0 if mode == "visible" else 12), 0)
        pump(wx)
        retained = (
            view.selected_target,
            h.selection(),
            view.GetViewStart()[1],
            view._sort_state,
            tuple(view.GetColSize(col) for col in range(5)),
        )
        visible_left, visible_right = h.viewport()
        x = (max(left, visible_left) + min(right, visible_right)) // 2 - visible_left
        if mode == "divider":
            x = next(
                view.GetColRight(col) - visible_left
                for col in range(view.GetNumberCols())
                if visible_left + 5
                < view.GetColRight(col)
                < min(right, visible_right) - 5
            )
        h.header_event(
            wx.wxEVT_LEFT_DCLICK if mode == "double" else wx.wxEVT_LEFT_DOWN,
            x,
            down=True,
        )
        assert not view._header_drag.active
        assert getattr(view._header_drag, "preview", None) is None
        before_release = tuple(view.GetViewStart())
        h.header_event(wx.wxEVT_MOTION, x + 1, down=True)
        assert not view._header_drag.active
        assert getattr(view._header_drag, "preview", None) is None
        assert tuple(view.GetViewStart()) == before_release
        h.header_event(wx.wxEVT_LEFT_UP, x)
        assert retained == (
            view.selected_target,
            h.selection(),
            view.GetViewStart()[1],
            view._sort_state,
            tuple(view.GetColSize(col) for col in range(5)),
        )
        if right - left <= view.GetGridWindow().GetClientSize().width:
            assert h.viewport()[0] <= left and right <= h.viewport()[1]
        else:
            assert h.viewport()[0] == left
        assert not view.GetGridColLabelWindow().HasCapture()

    matrix(check)


def test_drag_preview_keeps_original_grab_point_and_snapshot(matrix: Any) -> None:
    """A translucent block follows the captured pointer without a fresh snapshot."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        header, body = view.GetGridColLabelWindow(), view.GetGridWindow()
        left, right = h.bounds("A")
        origin = view.GetGridWindowOffset(body).x
        view.Scroll(max(0, left - origin + (right - left) // 3), 0)
        pump(wx)
        visible_left, visible_right = h.viewport()
        crop_left, crop_right = max(left, visible_left), min(right, visible_right)
        start_x = (crop_left + crop_right) // 2 - visible_left
        start_y = view._group_header_height // 2
        h.header_event(wx.wxEVT_LEFT_DOWN, start_x, start_y, down=True)
        h.header_event(wx.wxEVT_MOTION, start_x + 1, start_y, down=True)
        assert not view._header_drag.active
        assert getattr(view._header_drag, "preview", None) is None
        h.header_event(wx.wxEVT_MOTION, start_x + 40, start_y, down=True)
        drag = view._header_drag
        preview = getattr(drag, "preview", None)
        assert preview is not None, "Dragging must show the variant's actual columns"
        assert preview.hotspot == wx.Point(start_x - crop_left + visible_left, start_y)
        assert not preview.closed and preview.IsShown()
        assert body.GetScreenRect().Contains(preview.GetScreenRect())
        assert wx.Window.GetCapture() is header
        size = preview.bitmap.GetLogicalSize()
        assert 0 < size.width <= body.GetClientSize().width
        assert (
            header.GetClientSize().height
            < size.height
            <= (header.GetClientSize().height + body.GetClientSize().height)
        )
        source_bitmap = preview.bitmap
        image = source_bitmap.ConvertToImage()
        assert not image.HasAlpha() or set(image.GetAlpha()) == {255}
        snapshot = bytes(image.GetData())

        def painted_bounds() -> tuple[int, int]:
            bitmap = wx.Bitmap(body.GetClientSize())
            dc = wx.MemoryDC(bitmap)
            dc.SetBackground(wx.Brush(wx.Colour(255, 0, 255)))
            dc.Clear()
            try:
                preview._draw(dc, body)
            finally:
                dc.SelectObject(wx.NullBitmap)
            image = bitmap.ConvertToImage()
            ink = [
                x
                for x in range(image.GetWidth())
                if pixel(image, x, view.GetRowSize(0) // 2) != (255, 0, 255)
            ]
            assert ink, "The preview must draw into the scrolling body"
            x, y = ink[len(ink) // 2], view.GetRowSize(0) // 2
            position = body.ScreenToClient(
                header.ClientToScreen((drag.x - preview.hotspot.x, 0))
            )
            scale = source_bitmap.GetScaleFactor()
            original = pixel(
                source_bitmap.ConvertToImage(),
                int((x - position.x) * scale),
                int((y - position.y) * scale),
            )
            expected = tuple(
                round(source * 0.6 + background * 0.4)
                for source, background in zip(original, (255, 0, 255))
            )
            assert all(abs(a - b) <= 3 for a, b in zip(pixel(image, x, y), expected))
            return min(ink), max(ink)

        first = painted_bounds()
        assert first[0] == 40 + crop_left - visible_left
        h.header_event(wx.wxEVT_MOTION, start_x + 60, start_y, down=True)
        assert view._header_drag.preview is preview
        assert painted_bounds() == (first[0] + 20, first[1] + 20)
        h.header_event(
            wx.wxEVT_MOTION, start_x + 60, view.GetColLabelSize() + 5, down=True
        )
        assert not drag.valid and drag.preview is preview
        assert wx.Window.GetCapture() is header
        h.header_event(wx.wxEVT_MOTION, start_x + 40, start_y, down=True)
        assert drag.valid and painted_bounds() == first
        assert preview.bitmap is source_bitmap
        assert bytes(preview.bitmap.ConvertToImage().GetData()) == snapshot
        h.key(wx.WXK_ESCAPE)
        assert preview.closed and wx.Window.GetCapture() is None

    matrix(check)


@pytest.mark.parametrize(
    "error",
    [MemoryError, RuntimeError],
    ids=["memory", "allocation"],
)
def test_preview_failure_leaves_drag_and_drop_usable(
    matrix: Any, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Failed optional artwork must not strand capture or prevent a group move."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        attempt = Mock(side_effect=error("preview allocation failed"))
        monkeypatch.setattr(view, "_drag_preview_bitmap", attempt)
        end = h.start_drag("B", "A")
        drag = view._header_drag
        assert drag.active and drag.valid and drag.preview is None
        assert wx.Window.GetCapture() is view.GetGridColLabelWindow()
        h.header_event(wx.wxEVT_MOTION, end + 1, down=True)
        attempt.assert_called_once()
        h.header_event(wx.wxEVT_LEFT_UP, end)
        assert h.order() == ("", "B", "A", "C", "D")
        assert view._header_drag is None and wx.Window.GetCapture() is None
        assert not view._header_drag_timer.IsRunning()

    matrix(check)


@pytest.mark.parametrize("ending", ["drop", "escape"])
def test_preview_remains_visible_without_native_transparency(
    matrix: Any, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """Unsupported popup opacity must retain the moving columns and mouse capture."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        attempt = Mock(return_value=False)
        monkeypatch.setattr(
            h.view_module._VariantDragPreview, "SetTransparent", attempt
        )
        end = h.start_drag("B", "A")
        drag = view._header_drag
        preview = drag.preview
        header, body = view.GetGridColLabelWindow(), view.GetGridWindow()
        assert preview is not None and preview.IsShown() and not preview.closed
        assert wx.Window.GetCapture() is header
        assert body.GetScreenRect().Contains(preview.GetScreenRect())
        initial_rect, bitmap = preview.GetScreenRect(), preview.bitmap

        h.header_event(wx.wxEVT_MOTION, end + 20, down=True)
        assert drag.preview is preview and preview.bitmap is bitmap
        assert preview.IsShown() and preview.GetScreenRect() != initial_rect
        assert body.GetScreenRect().Contains(preview.GetScreenRect())
        attempt.assert_called_once_with(153)

        if ending == "drop":
            h.header_event(wx.wxEVT_LEFT_UP, end)
            assert h.order() == ("", "B", "A", "C", "D")
        else:
            h.key(wx.WXK_ESCAPE)
            assert h.order() == ("", "A", "B", "C", "D")
        assert preview.closed and view._header_drag is None
        assert wx.Window.GetCapture() is None
        assert not view._header_drag_timer.IsRunning()

    matrix(check)


def test_source_scrolled_out_before_drag_activation_keeps_capture_usable(
    matrix: Any,
) -> None:
    """A source leaving the viewport must not strand a pending header gesture."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        header, body = view.GetGridColLabelWindow(), view.GetGridWindow()
        order = h.order()
        left, right = h.bounds("A")
        origin = view.GetGridWindowOffset(body).x
        view.Scroll(max(0, left - origin), 0)
        pump(wx)
        start = (left + right) // 2 - h.viewport()[0]
        h.header_event(wx.wxEVT_LEFT_DOWN, start, down=True)
        assert not view._header_drag.active
        view.Scroll(right - origin, 0)
        pump(wx)
        assert h.viewport()[0] >= right
        h.header_event(wx.wxEVT_MOTION, start + 40, down=True)
        drag = view._header_drag
        assert drag.active and drag.valid and drag.marker_x is not None
        assert drag.preview is None
        assert wx.Window.GetCapture() is header
        h.key(wx.WXK_ESCAPE)
        assert h.order() == order
        assert view._header_drag is None and wx.Window.GetCapture() is None
        assert not view._header_drag_timer.IsRunning()

    matrix(check)


@pytest.mark.parametrize(
    "reason",
    [
        "escape",
        "capture",
        "outside",
        "frozen",
        "released",
        "disabled",
        "destroyed",
        "board_id",
        "board_token",
        "variants",
        "inventory",
        "components",
        "timestamp",
    ],
)
def test_header_drag_cancellation_and_source_invalidation(
    matrix: Any, reason: str
) -> None:
    """Only a changed native source cancels a drag; every exit releases capture/timers."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        order = h.order()
        h.start_drag("B", "A")
        drag = view._header_drag
        assert drag and drag.active and view.GetGridColLabelWindow().HasCapture()
        preview = getattr(drag, "preview", None)
        assert preview is not None and not preview.closed and preview.IsShown()
        if reason == "escape":
            h.key(wx.WXK_ESCAPE)
        elif reason == "capture":
            header = view.GetGridColLabelWindow()
            header.ReleaseMouse()
            event = wx.MouseCaptureLostEvent(header.GetId())
            event.SetEventObject(header)
            header.GetEventHandler().ProcessEvent(event)
        elif reason in ("outside", "frozen", "released"):
            h.header_event(
                wx.wxEVT_LEFT_UP if reason != "released" else wx.wxEVT_MOTION,
                -10 if reason == "frozen" else 20,
                view.GetColLabelSize() + 5 if reason == "outside" else None,
            )
        elif reason == "disabled":
            view.set_mutations_enabled(False)
        elif reason == "destroyed":
            h.sizer.Detach(view)
            view.Destroy()
            pump(wx)
            assert wx.Window.GetCapture() is None
            assert preview.closed
            return
        else:
            snapshot = view.model.snapshot
            key = "source_token" if reason == "timestamp" else reason
            value = "changed"
            if reason == "variants":
                value = snapshot.variants[:-1]
                snapshot = Snapshot(
                    tuple(
                        state
                        for state in snapshot.components
                        if state.variant_name != snapshot.variants[-1].name
                    )
                )
            elif reason == "inventory":
                value = snapshot.inventory[:-1]
                snapshot = Snapshot(
                    tuple(
                        state
                        for state in snapshot.components
                        if state.component_id in value
                    )
                )
            elif reason == "components":
                value = tuple(
                    replace(state, value="changed") for state in snapshot.components
                )
            view.set_model(
                h.model_module.MatrixModel(replace(snapshot, **{key: value}))
            )
            if reason == "timestamp":
                assert view._header_drag is drag
                assert view.GetGridColLabelWindow().HasCapture()
                h.key(wx.WXK_ESCAPE)
        pump(wx)
        assert h.order() == (order[:-1] if reason == "variants" else order)
        assert view._header_drag is None
        assert not view.GetGridColLabelWindow().HasCapture()
        assert not view._header_drag_timer.IsRunning()
        assert preview.closed

    matrix(check)


@pytest.mark.parametrize("phase", ["pending", "active", "resized", "edge"])
def test_supplier_publication_preserves_captured_drag_and_defers_layout(
    matrix: Any, phase: str
) -> None:
    """Queued supplier facts preserve native capture and become visible after the gesture."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        h.click(1, view.model.column_for("A", "lcsc"))
        selected, target = h.selection(), view.selected_target
        table, header = view.GetTable(), view.GetGridColLabelWindow()
        paints = 0
        draw_drag = view._draw_header_drag

        def observe_paint(dc: Any, origin: int, width: int) -> None:
            # PaintDC belongs to this event; retaining callback arguments is unsafe.
            nonlocal paints
            paints += 1
            draw_drag(dc, origin, width)

        view._draw_header_drag = observe_paint
        initial_widths = tuple(
            view.GetColSize(col) for col in range(view.GetNumberCols())
        )
        if phase == "pending":
            end = h.start_drag("B", "A", after_press=lambda: h.refresh_catalog())
        else:
            end = h.start_drag("B", "A")
        preview = getattr(view._header_drag, "preview", None)
        assert preview is not None
        source_bitmap = preview.bitmap
        snapshot = bytes(source_bitmap.ConvertToImage().GetData())
        if phase == "resized":
            end = header.GetClientSize().width - view.FromDIP(40)
            h.header_event(wx.wxEVT_MOTION, end, down=True)
            pointer = view._header_drag.x, view._header_drag.y
            for shrink in (80, 40):
                old_right = preview.GetScreenRect().GetRight()
                size = h.frame.GetClientSize()
                h.frame.SetClientSize((size.width - view.FromDIP(shrink), size.height))
                pump(wx)
                body_rect = view.GetGridWindow().GetScreenRect()
                assert body_rect.GetRight() < old_right
                assert (view._header_drag.x, view._header_drag.y) == pointer
                assert body_rect.Contains(preview.GetScreenRect()), (
                    "A stationary preview must be clipped to the resized viewport"
                )
                h.refresh_catalog()
        if phase == "edge":
            end = header.GetClientSize().width - 2
            h.header_event(wx.wxEVT_MOTION, end, down=True)
        updated = h.refresh_catalog()
        wait_until(wx, lambda: paints > 0)
        assert view.model is updated
        assert view.GetTable() is table and view.GetGridColLabelWindow() is header
        assert header.HasCapture()
        assert view._header_drag.preview is preview
        assert preview.bitmap is source_bitmap
        assert bytes(preview.bitmap.ConvertToImage().GetData()) == snapshot
        assert (
            tuple(view.GetColSize(col) for col in range(view.GetNumberCols()))
            == initial_widths
        )
        if phase == "edge":
            for _ in range(2):
                before = view.GetViewStart()[0]
                wait_until(wx, lambda before=before: view.GetViewStart()[0] > before)
            assert view._header_drag_timer.IsRunning()
        h.header_event(wx.wxEVT_LEFT_UP, end)
        assert view._header_drag is None and not header.HasCapture()
        assert preview.closed
        assert not view._resize_pending
        assert view.selected_target == target and h.selection() == selected
        assert all(
            view.model.get_display(row, view.model.column_for(name, "params"))
            == "fresh supplier parameters"
            for row in range(view.GetNumberRows())
            for name in view.model.variants
        )

    matrix(check)


@pytest.mark.parametrize(
    "mode",
    ["tooltip", "sort_resize", "preferences_reopen", "header_reset", "header_cancel"],
)
def test_reordered_groups_keep_native_field_workflows(
    matrix: Any, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moved variants retain native field behavior; reopening restores only preferences."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        h.click(0, view.model.column_for("A", "params"))
        h.click(1, view.model.column_for("B", "lcsc"), shift=True)
        selected, target = h.selection(), view.selected_target
        h.drag("C", "B")
        assert h.order() == ("", "A", "C", "B", "D")
        assert h.selection() == selected and view.selected_target == target
        assert view.selected_component_ids() == ("component-2",)
        assert not any(name == "C" for _, name, _ in h.selection())
        if mode == "tooltip":
            row = view.model.row_for_component("component-2")
            col = view.model.column_for("C", "lcsc")
            description = (
                "Precision resistor for component-2/C; full catalog description"
            )
            assert description in h.hover(row, col)
            view.set_model(make_model(h.model_module, stale=True))
            assert "Description unavailable" in h.hover(
                row, view.model.column_for("C", "lcsc")
            )
        elif mode == "sort_resize":
            col = view.model.column_for("C", "lcsc")
            view.MakeCellVisible(0, col)
            pump(wx)
            left, _ = h.viewport()
            y = view._group_header_height + 5
            x = (view.GetColLeft(col) + view.GetColRight(col)) // 2 - left
            for _ in range(2):
                h.header_event(wx.wxEVT_LEFT_DOWN, x, y, down=True)
                h.header_event(wx.wxEVT_LEFT_UP, x, y)
            assert view._sort_state == ("C", "lcsc", True)
            old_width = view.GetColSize(col)
            edge = view.GetColRight(col) - h.viewport()[0] - 1
            for _ in range(2):
                h.header_event(wx.wxEVT_MOTION, edge, view._group_header_height // 2)
                assert view._header_name_hover
                h.header_event(wx.wxEVT_MOTION, edge, y)
                assert not view._header_name_hover
            h.header_event(wx.wxEVT_LEFT_DOWN, edge, y, down=True)
            h.header_event(wx.wxEVT_MOTION, edge + 35, down=True)
            h.header_event(wx.wxEVT_LEFT_UP, edge + 35)
            assert view.GetColSize(col) > old_width + 10
            assert view._header_drag is None
        elif mode in ("header_reset", "header_cancel"):
            header = view.GetGridColLabelWindow()
            monkeypatch.setattr(
                header,
                "GetPopupMenuSelectionFromUser",
                lambda menu: menu.GetMenuItems()[0].GetId()
                if mode == "header_reset"
                else wx.ID_NONE,
            )
            view._on_header_context_menu(SimpleNamespace())
            assert h.order() == (
                ("", "A", "B", "C", "D")
                if mode == "header_reset"
                else ("", "A", "C", "B", "D")
            )
        else:
            preferences = view.capture_preferences()
            h.reopen()
            assert not h.selection() and h.view.selected_target is None
            assert h.view.capture_preferences() == preferences

    matrix(check)


def test_readonly_table_and_native_named_match_details(matrix: Any) -> None:
    """The lazy table rejects uncontrolled writes and tooltips use actual variant names."""

    def check(h: MatrixHarness) -> None:
        view = h.view
        snapshot = Snapshot(
            tuple(State("one", "R1", name) for name in ("", "Default", "A"))
        )
        snapshot = replace(
            snapshot,
            variants=tuple(
                replace(item, label="< Default >") if item.name == "" else item
                for item in snapshot.variants
            ),
        )
        model = h.model_module.MatrixModel(snapshot)
        view.set_model(model)
        col = model.column_for("A", "lcsc")
        assert "Default" in view.GetTable().GetColLabelValue(
            model.column_for("Default", "value")
        )
        with pytest.raises(RuntimeError, match="captured variant target"):
            view.GetTable().SetValue(0, col, "C999")
        details = h.hover(0, model.column_for("A", "value"))
        assert "Matches:" in details and "Default" in details and "A" in details
        assert "[a]" not in model.get_display(0, model.column_for("A", "value"))
        assert (
            h.view_module.target_at(model, 0, model.column_for("", "lcsc")).label
            != h.view_module.target_at(
                model, 0, model.column_for("Default", "lcsc")
            ).label
        )
        view.ClearSelection()
        view._queue_selection_changed()
        h.sizer.Detach(view)
        view.Destroy()
        pump(h.wx)

    matrix(check)


@pytest.mark.parametrize(
    "scope,action",
    [
        (scope, action)
        for scope in ("inside",)
        for action in ("copy", "copy_cell", "remove", "use_base", "paste", "cancel")
    ]
    + [(scope, "dispatch") for scope in ("inside", "outside", "other_variant")],
)
def test_context_menu_selection_controls_real_native_edit_scope(
    window_ui: Any, scope: str, action: str
) -> None:
    """The actual main window preserves a right-click's scope through menu refresh."""
    from .native_window_support import _SelectableFootprint

    ui = window_ui
    ui.board.parts.append(_SelectableFootprint(ui.board, "component-2", "R2"))
    for number, part in enumerate(ui.board.parts, start=1):
        part.fields["LCSC"] = f"C{number}"
        for variant, prefix in (("A", 10), ("B", 20)):
            override = part.AddVariant(variant)
            override.SetFieldValue("LCSC", f"C{prefix + number}")
            override.SetFieldValue("Value", f"{variant}-{number}")

    def board_state() -> Any:
        return deepcopy(
            [
                (
                    part.fields,
                    {
                        name: (item.fields, item.dnp, item.bom, item.pos)
                        for name, item in part.variants.items()
                    },
                )
                for part in ui.board.parts
            ]
        )

    def check(ui: Any) -> None:
        view, controller, wx = ui.controller.view, ui.controller, ui.wx
        expected = board_state()
        variant = "B" if scope == "other_variant" else "A"
        destinations = (
            ("component-1", "component-2") if scope == "inside" else ("component-2",)
        )
        calls: list[Any] = []
        if action == "dispatch":
            view._on_action = lambda name, target: calls.append(
                (name, target, view.selected_component_ids())
            )
        if action == "paste":
            controller._write_clipboard(
                view.model.copy_block(destinations, "", fields=("lcsc",))
            )
        view.select_components(
            ("component-1",) if scope == "outside" else ("component-1", "component-2"),
            "A",
        )

        def popup(menu: Any) -> None:
            assert set(view.selected_component_ids()) == set(destinations)
            assert (
                view.selected_target.component_id,
                view.selected_target.variant,
            ) == ("component-2", variant)
            # A real PopupMenu can publish metadata through its nested loop.
            if action == "dispatch":
                view._sort_state = (None, "ref", True)
            controller.refresh()
            assert set(view.selected_component_ids()) == set(destinations)
            if action != "cancel":
                label = {
                    "copy": "Copy",
                    "copy_cell": "Copy cell value",
                    "remove": "Clear assignment",
                    "use_base": "Use base assignment",
                    "paste": "Paste",
                }.get(action)
                for item in menu.GetMenuItems():
                    if label is None or item.GetItemLabelText() == label:
                        assert item.IsEnabled()
                        menu.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, item.GetId()))

        view.PopupMenu = popup
        support.click_native_cell(
            view, wx, 1, view.model.column_for(variant, "lcsc"), right=True
        )
        if action in ("remove", "use_base", "paste"):
            for number, (fields, variants) in enumerate(expected, start=1):
                if f"component-{number}" in destinations:
                    overrides = variants[variant][0]
                    if action == "use_base":
                        del overrides["LCSC"]
                    else:
                        overrides["LCSC"] = "" if action == "remove" else fields["LCSC"]
        if action == "dispatch":
            assert [name for name, _target, _components in calls] == [
                "copy",
                "copy_cell",
                "details",
                "paste",
                "enter_lcsc",
                "copy_to",
                "remove",
                "use_base",
                "save_preferences",
                "apply_preferences",
            ]
            assert view.model.rows[0].component_id == "component-2"
            for _name, target, components in calls:
                assert (target.component_id, target.variant) == ("component-2", variant)
                assert set(components) == set(destinations)
        assert board_state() == expected
        assert ui.messages == []
        if action in ("copy", "copy_cell"):
            copied = controller._clipboard
            assert tuple(row.component_id for row in copied.rows) == (
                ("component-2",) if action == "copy_cell" else destinations
            )
            assert dict(copied.rows[-1].values)["lcsc"] == (
                "C22" if variant == "B" else "C12"
            )

    ui.run(check)


@pytest.mark.parametrize("tier", ["name", "field"])
@pytest.mark.parametrize("populated", [False, True])
def test_informational_header_hover_clears_stale_cell_details(
    matrix: Any, tier: str, populated: bool
) -> None:
    """Compact status headers cannot retain a preceding cell's misleading tooltip."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        view.set_model(make_model(h.model_module, populated=populated))
        header = view.GetGridColLabelWindow()
        for name in ("", "D"):
            col = view.model.column_for(name, "stock")
            if populated:
                assert h.hover(0, view.model.column_for(name, "lcsc"))
            view._show_variant(name)
            pump(wx)
            x = (view.GetColLeft(col) + view.GetColRight(col)) // 2 - h.viewport()[0]
            h.header_event(
                wx.wxEVT_MOTION,
                x,
                None if tier == "name" else view._group_header_height + 5,
            )
            assert not header.GetToolTip() or not header.GetToolTip().GetTip()
            assert view._header_name_hover is (tier == "name")
            cursor = header.GetCursor()
            # GTK may leave the native field header at NullCursor, which means
            # use the default pointer. The draggable name tier owns a hand.
            assert cursor.IsOk() or (tier == "field" and cursor.IsSameAs(wx.NullCursor))

    matrix(check)


@pytest.mark.parametrize("legacy", [False, True])
def test_frozen_header_lookup_prefers_public_api_and_rejects_legacy_ambiguity(
    matrix: Any, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    """Older bindings discover one current header and never choose an unrelated child."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        if legacy:
            monkeypatch.delattr(
                h.view_module.gridlib.Grid, "GetFrozenColLabelWindow", raising=False
            )
        public = callable(getattr(view, "GetFrozenColLabelWindow", None))
        header = view._frozen_col_label_window()
        assert header is not None
        duplicate = wx.Window(view, pos=header.GetPosition(), size=header.GetSize())
        duplicate.Show()
        assert view._frozen_col_label_window() is (header if public else None)
        duplicate.Destroy()
        pump(wx)
        assert view._frozen_col_label_window() is header
        view.set_model(make_model(h.model_module, rows=4))
        current = view._frozen_col_label_window()
        assert current in view.GetChildren()
        assert current.GetSize().width == view.GetFrozenColGridWindow().GetSize().width
        resized = h.view_module.gridlib.GridSizeEvent(
            view.GetId(), h.view_module.gridlib.wxEVT_GRID_COL_SIZE, view, 1
        )
        view.GetEventHandler().ProcessEvent(resized)
        assert view._frozen_col_label_window() is not None

    matrix(check)


@pytest.mark.parametrize(
    "destination,dark,scale,edge",
    [
        ("", False, 1, "middle"),
        ("B", True, 2, "left"),
        (None, True, 1, "right"),
        ("B", False, 2, "narrow"),
    ],
)
def test_drag_feedback_native_drawing_keeps_markers_and_labels_visible(
    matrix: Any, destination: Optional[str], dark: bool, scale: int, edge: str
) -> None:
    """Real DC output keeps bold outlined arrows and a contrasting destination."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        view.SetLabelBackgroundColour(
            wx.Colour(38, 38, 38) if dark else wx.Colour(245, 245, 245)
        )
        view.SetLabelFont(view.GetLabelFont().Scaled(scale))
        pump(wx)
        h.start_drag("A", "")
        drag = view._header_drag
        assert drag and drag.active
        width = 80 if edge == "narrow" else 1000
        drag.before = destination
        drag.marker_x = (
            -100 if edge in ("left", "narrow") else 1100 if edge == "right" else 500
        )
        bitmap = wx.Bitmap(width, view.GetColLabelSize())
        dc = wx.MemoryDC(bitmap)
        polygons = Mock(wraps=dc.DrawPolygon)
        lines = Mock(wraps=dc.DrawLine)
        pens = Mock(wraps=dc.SetPen)
        dc.DrawPolygon, dc.DrawLine, dc.SetPen = polygons, lines, pens
        draw_text = view.DrawTextRectangle
        labels: list[Any] = []

        def observe_text(
            canvas: Any, text: str, rect: Any, horizontal: int, vertical: int
        ) -> None:
            labels.append((text, wx.Rect(rect)))
            draw_text(canvas, text, rect, horizontal, vertical)

        view.DrawTextRectangle = observe_text
        destinations = []
        try:
            for pointer in (100, 150):
                drag.x = pointer
                labels.clear()
                polygons.reset_mock()
                view._draw_header_drag(dc, 0, width)
                assert polygons.call_count == 2
                for call in polygons.call_args_list:
                    assert all(
                        0 <= x < width and 0 <= y < view.GetColLabelSize()
                        for x, y in call.args[0]
                    )
                    assert (
                        max(x for x, _ in call.args[0])
                        - min(x for x, _ in call.args[0])
                        >= 9
                    )
                assert not any(label.startswith("Move") for label, _rect in labels)
                destinations.extend(
                    rect for label, rect in labels if label.startswith("Drop")
                )
                assert all(0 <= rect.x <= rect.GetRight() < width for _, rect in labels)
            assert len({call.args[0] for call in lines.call_args_list}) == 1
            assert max(
                call.args[0].GetWidth() for call in pens.call_args_list
            ) >= view.FromDIP(10)
            if edge != "narrow":
                assert destinations[0] == destinations[1]
                expected = (
                    "Drop at end"
                    if destination is None
                    else f"Drop before {view.model.variant_label(destination)}"
                )
                assert any(label == expected for label, _rect in labels)
            low, high = sorted(
                (
                    h.view_module._luminance(view.GetLabelBackgroundColour()),
                    h.view_module._luminance(dc.GetBrush().GetColour()),
                )
            )
            assert (high + 0.05) / (low + 0.05) >= 4.5
        finally:
            view.DrawTextRectangle = draw_text
            dc.SelectObject(wx.NullBitmap)
        h.key(wx.WXK_ESCAPE)

    matrix(check)


@pytest.mark.parametrize(
    "state", ["none", "pending", "invalid", "missing_marker", "empty"]
)
def test_inactive_drag_has_no_native_feedback(matrix: Any, state: str) -> None:
    """No valid drop indication is painted before threshold, after cancellation or outside headers."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        h.start_drag("A", "")
        if state == "none":
            h.key(wx.WXK_ESCAPE)
        elif state == "pending":
            view._header_drag.active = False
        elif state == "invalid":
            view._header_drag.valid = False
        elif state == "missing_marker":
            view._header_drag.marker_x = None
        bitmap = wx.Bitmap(300, view.GetColLabelSize())
        dc = wx.MemoryDC(bitmap)
        polygon = Mock(wraps=dc.DrawPolygon)
        rectangle = Mock(wraps=dc.DrawRectangle)
        dc.DrawPolygon, dc.DrawRectangle = polygon, rectangle
        view._draw_header_drag(dc, 0, 0 if state == "empty" else 300)
        dc.SelectObject(wx.NullBitmap)
        polygon.assert_not_called()
        rectangle.assert_not_called()
        h.key(wx.WXK_ESCAPE)

    matrix(check)


@pytest.mark.parametrize(
    "moved,target,expected",
    [
        ("B", "A", ("", "B", "A", "C", "D")),
        ("A", "B", ("", "B", "A", "C", "D")),
        ("", "B", ("A", "B", "", "C", "D")),
    ],
)
def test_center_drop_resolves_forward_and_backward_group_insertions(
    matrix: Any, moved: str, target: str, expected: tuple[str, ...]
) -> None:
    """Dropping at a group center inserts after it moving forward and before it backward."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        left, right = h.bounds(target)
        if (right - left) % 2:
            col = view.model.column_for(target, "value")
            view.SetColSize(col, view.GetColSize(col) + 1)
        h.start_drag(moved, target)
        left, right = h.bounds(target)
        center = (left + right) // 2 - h.viewport()[0]
        h.header_event(wx.wxEVT_MOTION, center, down=True)
        h.header_event(wx.wxEVT_LEFT_UP, center)
        assert h.order() == expected
        assert view.selected_target is None
        assert not any(h.physical_notifications)
        assert not view.GetGridColLabelWindow().HasCapture()

    matrix(check)


@pytest.mark.parametrize("case", ["ref_sort", "stale_rows"])
def test_physical_selection_tracks_ref_sort_and_rejects_stale_native_rows(
    matrix: Any, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Physical UUIDs survive shared-header sorting and cannot come from invalid native indexes."""

    def check(h: MatrixHarness) -> None:
        view, wx = h.view, h.wx
        h.click(0, 0)
        if case == "stale_rows":
            monkeypatch.setattr(
                view,
                "GetSelectedRowBlocks",
                lambda: [
                    h.view_module.gridlib.GridBlockCoords(row, 0, row, 0)
                    for row in (-1, 50)
                ],
            )
            view._queue_selection_changed()
            pump(wx)
            assert view.selected_physical_component_ids() == ()
            assert h.physical_notifications[-1] == ()
        else:
            point = wx.Point(view.GetColSize(0) // 2, view._group_header_height // 2)
            for _ in range(2):
                for event in (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP):
                    h.mouse(view._frozen_col_label_window(), event, point)
            assert view._sort_state == (None, "ref", True)
            assert view.model.rows[-1].component_id == "component-1"
            assert h.view.selected_physical_component_ids() == ("component-1",)
            assert h.view.selected_component_ids() == ()

    matrix(check)
