"""Independently collected native matrix rendering and state-preservation workflows."""

from collections.abc import Callable
from dataclasses import replace
import time
from types import ModuleType
from typing import Any, Optional

import pytest

from . import variant_matrix_native_test_support as support
from .native_wx_support import isolated_native, pump, run_native, wait_until
from .variant_matrix_native_test_support import (
    MatrixHarness,
    click_native_cell,
    make_model,
    native_marks,
)
from .variant_matrix_render_test_support import (
    observe_cell_paints,
    paint_header,
    resize,
)
from .variant_model_test_support import Snapshot, State, assignment

modules = support.modules
matrix = support.matrix

pytestmark = native_marks


def display_model(model_module: ModuleType) -> Any:
    """Use a single component for native geometry and header-state checks."""
    return make_model(model_module, rows=1, variants=("", "A", "B"))


def settle(wx: Any) -> None:
    """Allow native child-window invalidations to reach their paint bindings."""
    for _ in range(3):
        wx.MilliSleep(20)
        pump(wx)


def assert_native_output_header(
    view: Any, wx: Any, expected: Optional[str], *, moving: Optional[str] = None
) -> None:
    """Check native raster fill/underline and the real DC's text palette by identity."""
    rendered = paint_header(view, wx)
    image, labels = rendered.image, rendered.text
    foreground = view.GetLabelTextColour().Get()[:3]
    selected_foreground = wx.SystemSettings.GetColour(
        wx.SYS_COLOUR_HIGHLIGHTTEXT
    ).Get()[:3]
    tier = view._group_header_height
    underline = max(1, view.FromDIP(2))

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        return image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y)

    def raster_color(color: Any) -> tuple[int, int, int]:
        # Cocoa can color-manage bitmap pixels. Compare with the same native
        # palette rendered through a real DC, not unconverted logical RGB.
        swatch = wx.Bitmap(4, 4)
        canvas = wx.MemoryDC(swatch)
        canvas.SetBackground(wx.Brush(color))
        canvas.Clear()
        canvas.SelectObject(wx.NullBitmap)
        rendered = swatch.ConvertToImage()
        return rendered.GetRed(2, 2), rendered.GetGreen(2, 2), rendered.GetBlue(2, 2)

    plain_fill = raster_color(view.GetLabelBackgroundColour())
    active_fill = raster_color(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT))
    active_line = raster_color(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT))
    for variant in view.model.variants:
        col = view.model.column_for(variant, "value")
        left = view.GetColLeft(col)
        active = variant == expected
        emphasized = active or variant == moving
        assert pixel(left + view.FromDIP(6), 3) == (
            active_fill if emphasized else plain_fill
        ), f"Wrong name-tier fill for {variant!r} with Output {expected!r}"
        assert pixel(left + view.FromDIP(6), tier - underline - 1) == (
            active_fill if emphasized else plain_fill
        )
        for y in range(tier - underline, tier - 1):
            assert pixel(left + view.FromDIP(6), y) == (
                active_line if active else active_fill if emphasized else plain_fill
            ), f"Wrong output underline for {variant!r} at {y}"
        assert pixel(left + view.FromDIP(6), tier + 3) == plain_fill
        name = next(
            label.color
            for label in labels
            if label.text == view.model.variant_label(variant)
            and label.rect[1] == 0
            and label.rect[0] == left
        )
        assert name == (selected_foreground if emphasized else foreground)
    for label in labels:
        if label.rect[1] > 0 or label.rect[0] < view.GetColLeft(5):
            assert label.color == foreground, (
                f"Output styling leaked into {label.text!r}"
            )
    assert pixel(3, 3) == plain_fill, "Shared headers retain the normal palette"


def test_native_dragged_name_keeps_output_header_underlined(
    modules: tuple[ModuleType, ModuleType],
) -> None:
    """Native header dragging highlights the source without claiming Output identity."""

    def exercise(frame: Any, wx: Any) -> None:
        harness = MatrixHarness(frame, wx, *modules)
        view = harness.view
        view.set_output_variant("B")
        harness.start_drag("A", "")
        assert view._header_drag.active
        assert view._header_drag.variant == "A"
        assert view.output_variant == "B"
        assert_native_output_header(view, wx, "B", moving="A")
        harness.key(wx.WXK_ESCAPE)
        assert view._header_drag is None
        assert view.output_variant == "B"
        assert_native_output_header(view, wx, "B")

    run_native(exercise)


@pytest.mark.parametrize("dark_labels", [False, True])
def test_native_output_changes_repaint_without_moving_edit_target_and_survive_refresh(
    modules: tuple[ModuleType, ModuleType],
    dark_labels: bool,
) -> None:
    """Output changes immediately repaint, independently of cursor and group order."""
    view_module, model_module = modules

    def exercise(frame: Any, wx: Any) -> None:
        harness = MatrixHarness(frame, wx, *modules, display_model(model_module))
        view = harness.view
        if dark_labels:
            view.SetLabelBackgroundColour(wx.Colour(32, 34, 38))
            view.SetLabelTextColour(wx.Colour(225, 227, 230))
            view.SetLabelFont(view.GetLabelFont().Scaled(1.35))
        view.ForceRefresh()
        settle(wx)
        assert view.output_variant is None
        assert_native_output_header(view, wx, None)
        view.GoToCell(0, view.model.column_for("A", "lcsc"))
        settle(wx)
        selected = view.selected_target
        scroll = tuple(view.GetViewStart())
        widths = tuple(view.GetColSize(col) for col in range(view.GetNumberCols()))
        names = tuple(view.model.variant_label(name) for name in view.model.variants)
        paint_states: list[Optional[str]] = []
        draw_labels = view._draw_header_labels

        def observe_paint(
            dc: Any, cols: list[int], left_origin: int, visible_width: int
        ) -> None:
            paint_states.append(view.output_variant)
            draw_labels(dc, cols, left_origin, visible_width)

        view._draw_header_labels = observe_paint
        for output in ("A", "B", "", None, "missing"):
            paint_states.clear()
            view.set_output_variant(output)
            settle(wx)
            assert view.output_variant == output
            assert paint_states and all(state == output for state in paint_states)
            assert_native_output_header(view, wx, output)
            assert view.selected_target == selected
            assert tuple(view.GetViewStart()) == scroll
            assert (
                tuple(view.GetColSize(col) for col in range(view.GetNumberCols()))
                == widths
            )
            assert (
                tuple(view.model.variant_label(name) for name in view.model.variants)
                == names
            )

        view.set_output_variant("B")
        view.reorder_variant("B", "")
        settle(wx)
        assert view.model.variant_order == ("B", "", "A")
        assert view.selected_target == selected
        assert_native_output_header(view, wx, "B")
        view.set_model(display_model(model_module))
        settle(wx)
        assert view.output_variant == "B"
        assert view.selected_target == selected
        assert_native_output_header(view, wx, "B")

        filtered_snapshot = Snapshot(
            tuple(replace(state, pop=False) for state in view.model.snapshot.components)
        )
        filtered = model_module.MatrixModel(filtered_snapshot)
        filtered.set_filter(require_pop=True)
        view.set_model(filtered)
        settle(wx)
        assert view.GetNumberRows() == 0
        assert view.output_variant == "B"
        assert_native_output_header(view, wx, "B")
        view.set_model(display_model(model_module))
        settle(wx)
        assert view.output_variant == "B"
        assert_native_output_header(view, wx, "B")

        snapshot = view.model.snapshot
        missing = Snapshot(
            tuple(state for state in snapshot.components if state.variant_name != "B")
        )
        missing = Snapshot(missing.components, names=("", "A"))
        view.set_model(model_module.MatrixModel(missing))
        settle(wx)
        assert view.output_variant == "B"
        assert_native_output_header(view, wx, None)

    run_native(exercise)


def test_native_large_matrix_paints_headers_and_preserves_deep_scroll(
    modules: tuple[ModuleType, ModuleType],
) -> None:
    """Frozen labels, distant dividers and targets survive real viewport changes."""
    view_module, model_module = modules
    variants = ("", "A", "B", "C", "D", "E", "F", "G")
    snapshot = Snapshot(
        tuple(
            State(f"component-{row}", f"R{row + 1}", variant, assignment=assignment())
            for row in range(1000)
            for variant in variants
        )
    )
    snapshot = Snapshot(snapshot.components, names=variants)

    def exercise(frame: Any, wx: Any) -> None:
        harness = MatrixHarness(frame, wx, *modules, model_module.MatrixModel(snapshot))
        view = harness.view
        frozen_labels: set[int] = set()
        dividers: set[int] = set()
        draw_labels = view._draw_header_labels
        grid_line_pen = view.GetColGridLinePen

        def observe_labels(
            dc: Any, cols: list[int], left_origin: int, visible_width: int
        ) -> None:
            if left_origin == 0:
                frozen_labels.update(col for col in cols if col < 5)
            draw_labels(dc, cols, left_origin, visible_width)

        def observe_grid_line(col: int) -> Any:
            pen = grid_line_pen(col)
            if pen.GetWidth() == view.FromDIP(3):
                dividers.add(col)
            return pen

        view._draw_header_labels = observe_labels
        view.GetColGridLinePen = observe_grid_line
        view.ForceRefresh()
        settle(wx)
        assert view.GetNumberRows() == 1000
        assert view.GetNumberFrozenCols() == 5
        assert frozen_labels == set(range(5))

        click_native_cell(view, wx, 0, 0)
        assert view.selected_target.component_id == "component-0"
        assert view.selected_target.variant is None
        assert view.selected_component_ids() == ()
        with observe_cell_paints(view, view_module.MatrixCellRenderer) as draws:
            view.ForceRefresh()
            settle(wx)
            painted = {col for row, col, _clip in draws if row == 0}
            assert set(range(5)) <= painted, (
                "Ref selection must repaint the native frozen physical cells"
            )
            assert any(
                view.model.columns[col].variant is not None for col in painted
            ), "Ref selection must also repaint the scrolling variant cells"

        # Derive expected separators from public variant identity, rather than
        # the implementation's boundary predicate or obsolete column indices.
        expected_dividers = {
            view.model.column_for(variant, "price") for variant in variants[:-1]
        }
        for variant in ("A", "G"):
            view.GoToCell(999, view.model.column_for(variant, "value"))
            view.ForceRefresh()
            settle(wx)
        assert {min(expected_dividers), max(expected_dividers)} <= dividers
        assert dividers <= expected_dividers

        col = view.model.column_for("G", "lcsc")
        view.GoToCell(700, col)
        pump(wx)
        assert (
            view.selected_target.component_id,
            view.selected_target.variant,
            view.selected_target.field,
        ) == ("component-700", "G", "lcsc")
        assert view.MoveCursorRight(False)
        pump(wx)
        assert view.selected_target.field == "bom"
        assert view.GetGridCursorRow() == 700
        assert view.GetGridCursorCol() == col + 1
        window = view.GetGridWindow()
        rect = view.CellToRect(700, col + 1)
        point = view.CalcGridWindowScrolledPosition(rect.x + 3, rect.y + 3, window)
        logical = view.CalcGridWindowUnscrolledPosition(*point, window)
        cell = view.XYToCell(*logical, window)
        assert (cell.GetRow(), cell.GetCol()) == (700, col + 1)
        retained = view.selected_target
        scroll = tuple(view.GetViewStart())
        assert all(value > 0 for value in scroll)
        view.set_model(model_module.MatrixModel(snapshot))
        settle(wx)
        assert view.selected_target == retained
        assert tuple(view.GetViewStart()) == scroll

        footprint = view.model.column_for(None, "footprint")
        original_width = view.GetColSize(footprint)
        original_size = frame.GetSize()
        frame.SetSize((650, 420))
        settle(wx)
        assert view.GetNumberFrozenCols() == 5
        assert view.GetFrozenColGridWindow().GetClientSize().width == sum(
            view.GetColSize(index) for index in range(5)
        )
        assert (
            view.GetGridWindow().GetClientSize().width
            >= view.GetTextExtent("MMMMMMMM")[0]
        )
        assert view.selected_target == retained
        frame.SetSize(original_size)
        settle(wx)
        assert view.GetColSize(footprint) == original_width
        assert view.selected_target == retained

    run_native(exercise)


@pytest.mark.parametrize(
    "ending", ["drop", "escape", "native-change", "read-failure", "close", "superseded"]
)
def test_native_supplier_resort_does_not_interrupt_header_drag(
    modules: tuple[ModuleType, ModuleType], ending: str
) -> None:
    """Price-driven row changes wait until a held column gesture has finished."""

    def exercise(frame: Any, wx: Any) -> None:
        harness = MatrixHarness(frame, wx, *modules)
        view = harness.view
        original = view.model
        view._sort_state = ("", "price", False)
        view.set_model(original)
        col = view.model.column_for("A", "lcsc")
        harness.click(0, col)
        selected = harness.selection()
        rows = tuple(row.component_id for row in view.model.rows)
        table = view.GetTable()
        end_x = harness.start_drag("B", "A")
        header = view.GetGridColLabelWindow()
        updated = modules[1].MatrixModel(
            original.snapshot,
            enrichment={
                key: replace(metadata, price=str(4 - int(key[0][-1])))
                for key, metadata in original._metadata.items()
            },
        )
        view.set_model(updated)
        assert view._header_drag is not None and header.HasCapture(), (
            "Supplier results must not cancel a header drag when the active sort changes rows"
        )
        assert view.GetTable() is table
        assert tuple(row.component_id for row in view.model.rows) == rows
        if ending == "drop":
            harness.header_event(wx.wxEVT_LEFT_UP, end_x)
            assert view.model.variant_order == ("", "B", "A", "C", "D")
        elif ending == "escape":
            harness.key(wx.WXK_ESCAPE)
        elif ending == "native-change":
            snapshot = replace(original.snapshot, board_token="new-board-revision")
            view.set_model(modules[1].MatrixModel(snapshot))
            assert view.model.snapshot is snapshot
            assert tuple(row.component_id for row in view.model.rows) == rows
        elif ending == "read-failure":
            view.set_mutations_enabled(False)
            assert view.model is original
        elif ending == "close":
            harness.sizer.Detach(view)
            view.Destroy()
            pump(wx)
        else:
            latest = modules[1].MatrixModel(original.snapshot)
            view.set_model(latest)
            assert view.model is latest and header.HasCapture()
            harness.key(wx.WXK_ESCAPE)
            assert view.model is latest
        assert view._header_drag is None and wx.Window.GetCapture() is None
        assert view._deferred_projection is None
        if ending in ("drop", "escape"):
            assert view.model is updated
            assert tuple(row.component_id for row in view.model.rows) == rows[::-1]
            assert harness.selection() == selected

    run_native(exercise)


def _physical_matrix_harness(
    frame: Any, wx: Any, modules: tuple[ModuleType, ModuleType]
) -> MatrixHarness:
    """Mount the physical-input fixture using the product's pre-show lifecycle."""
    if wx.Platform != "__WXGTK__":
        return MatrixHarness(frame, wx, *modules)
    # Adding/refreezing the initial grid in an already mapped GTK frame can
    # leave its new native child at 1x1 while wx reports its requested full size.
    frame.Hide()
    try:
        return MatrixHarness(frame, wx, *modules)
    finally:
        frame.Show()


def _wait_physical_header_allocation(wx: Any, view: Any) -> None:
    """Wait for the newly frozen GTK pane to receive its native allocation."""
    if wx.Platform != "__WXGTK__":
        return
    frozen = view.GetFrozenColGridWindow()
    painted: Optional[tuple[int, int]] = None

    def observe(event: Any) -> None:
        nonlocal painted
        size = frozen.GetClientSize()
        # GTK can still own a 1x1 child after wx reports the requested pane
        # geometry. Its real paint region, unlike cached sizes, proves allocation.
        complete = (
            size.width
            == sum(view.GetColSize(col) for col in range(view.GetNumberFrozenCols()))
            and size.height > 1
            and frozen.GetUpdateRegion().Contains(wx.Rect((0, 0), size)) == wx.InRegion
        )
        if complete:
            painted = (size.width, size.height)
        event.Skip()

    frozen.Bind(wx.EVT_PAINT, observe)
    try:
        frozen.Refresh()
        wait_until(
            wx,
            lambda: painted is not None
            and painted[0]
            == frozen.GetClientSize().width
            == sum(view.GetColSize(col) for col in range(view.GetNumberFrozenCols()))
            and painted[1] >= frozen.GetClientSize().height,
            reason="The native frozen pane must be allocated before physical header input",
        )
    finally:
        frozen.Unbind(wx.EVT_PAINT, handler=observe)


def _physical_header_drag(
    wx: Any,
    view: Any,
    header: Any,
    delivered: list[bool],
    resolve_points: Callable[[bool, int], tuple[Any, Any]],
) -> None:
    """Deliver real held input before release, preserving Cocoa's native drag API."""
    simulator = wx.UIActionSimulator()
    # Simulator construction itself dispatches native events on X11. Resolve
    # the source afterwards, rather than retaining pre-layout screen positions.
    first, last = resolve_points(False, 3000)
    if wx.Platform != "__WXGTK__":
        # Cocoa MouseMove emits plain motion even while a button is held.
        assert simulator.MouseDragDrop(first.x, first.y, last.x, last.y)
        return
    assert simulator.MouseMove(first.x, first.y)

    def remaining() -> int:
        return max(0, int((deadline - time.monotonic()) * 1000))

    deadline = time.monotonic() + 3
    try:
        assert simulator.MouseDown()
        wait_until(
            wx,
            lambda: view._header_drag is not None
            and view._header_drag.variant == "B"
            and header.HasCapture(),
            timeout_ms=remaining(),
            reason="The OS must deliver the B header press before held motion",
        )
        # Press/focus delivery can finish native frozen-pane layout and move
        # the header. Locate A in that captured geometry before held movement.
        _first, last = resolve_points(False, remaining())
        assert simulator.MouseMove(last.x, last.y)
        wait_until(
            wx,
            lambda: bool(delivered)
            and view._header_drag is not None
            and view._header_drag.active
            and header.HasCapture(),
            timeout_ms=remaining(),
            reason="The OS must deliver supplier data during the captured held motion",
        )
        # Keep the same press and capture while aiming within A again after
        # the actual supplier handler and pending native layout have run.
        _first, last = resolve_points(True, remaining())
        assert time.monotonic() < deadline, (
            "Held input target resolution exceeded its deadline"
        )
        assert simulator.MouseMove(last.x, last.y)
        wait_until(
            wx,
            lambda: bool(delivered)
            and view._header_drag is not None
            and view._header_drag.active
            and view._header_drag.valid
            and view._header_drag.before == "A"
            and header.HasCapture(),
            timeout_ms=remaining(),
            reason="The OS must deliver a valid B-before-A drag and supplier update before release",
        )
    finally:
        assert simulator.MouseUp()


@pytest.mark.os_input
def test_native_physical_header_drag_survives_supplier_update(
    modules: tuple[ModuleType, ModuleType], request: pytest.FixtureRequest
) -> None:
    """Exercise OS-routed held mouse input without the injected-event motion filter."""
    if isolated_native(request, "KICAD_MATRIX_PHYSICAL_CHILD"):
        return

    result: dict[str, Any] = {}

    def exercise(frame: Any, wx: Any) -> None:
        harness = _physical_matrix_harness(frame, wx, modules)
        view = harness.view
        header = view.GetGridColLabelWindow()
        header.Unbind(wx.EVT_MOTION, handler=harness._ignore_unpressed_os_motion)
        frame.Raise()
        is_gtk = wx.Platform == "__WXGTK__"
        frame.SetFocus()
        wait_until(
            wx,
            lambda: frame.IsActive() and wx.Window.FindFocus() is not None,
            reason="The desktop must activate the native fixture before OS drag input",
        )
        origin = view.GetGridWindowOffset(view.GetGridWindow()).x
        view.Scroll(max(0, harness.bounds("A")[0] - origin), 0)
        settle(wx)
        _wait_physical_header_allocation(wx, view)
        points: list[Any] = []

        def header_ready(published: bool) -> bool:
            """Locate actual B/A hit targets after native scroll/layout delivery."""
            left, right = harness.viewport()
            source_left, source_right = harness.bounds("B")
            visible_left, visible_right = (
                max(source_left, left),
                min(source_right, right),
            )
            if not header.IsShownOnScreen() or visible_left >= visible_right:
                return False
            start = (visible_left + visible_right) // 2 - left
            if is_gtk:
                target_left, target_right = harness.bounds("A")
                edge = view._header_scroll_edge()
                # Aim inside A's insertion half, clear of edge-scroll strips.
                drop_left = max(target_left - left, edge + 5)
                drop_right = min(
                    (target_left + target_right) // 2 - left, right - left - edge - 5
                )
                if drop_left >= drop_right:
                    return False
                # Use distinct interior points for the two held motions, with
                # both remaining in A's insertion half and clear of edge scroll.
                fraction = 2 if published else 1
                destination = drop_left + (drop_right - drop_left) * fraction // 3
            else:
                destination = (
                    max(harness.bounds("A")[0] - left, view._header_scroll_edge()) + 5
                )
            first = wx.Point(start, view._group_header_height // 2)
            last = wx.Point(destination, view._group_header_height // 2)
            if (
                view._header_variant_at(first) != "B"
                or view._header_variant_at(last) != "A"
            ):
                return False
            points[:] = [header.ClientToScreen(first), header.ClientToScreen(last)]
            return True

        def resolve_points(published: bool, timeout_ms: int) -> tuple[Any, Any]:
            """Resolve screen positions at the native boundary that will use them."""
            wait_until(
                wx,
                lambda: header_ready(published),
                timeout_ms=timeout_ms,
                reason="Native scrolling must expose the B source and A drop target before physical input",
            )
            return points[0], points[1]

        initial = view.model
        updated = modules[1].MatrixModel(
            initial.snapshot,
            enrichment={
                key: replace(metadata, params="Updated during physical drag")
                for key, metadata in initial._metadata.items()
            },
        )
        delivered = []
        update_drag = view._update_header_drag

        def on_held_motion(position: Any) -> None:
            update_drag(position)
            if (
                view._header_drag is not None
                and view._header_drag.active
                and not delivered
            ):
                assert header.HasCapture()
                table = view.GetTable()
                drag = view._header_drag
                widths = tuple(
                    view.GetColSize(col) for col in range(view.GetNumberCols())
                )
                columns = view.model.columns
                order = view.model.variant_order
                view.set_model(updated)
                assert view.GetTable() is table
                assert view.GetGridColLabelWindow() is header and header.HasCapture()
                assert view._header_drag is drag
                assert (
                    tuple(view.GetColSize(col) for col in range(view.GetNumberCols()))
                    == widths
                )
                assert view.model.columns == columns
                assert view.model.variant_order == order
                delivered.append(True)

        view._update_header_drag = on_held_motion
        result.update(view=view, header=header, delivered=delivered)
        _physical_header_drag(wx, view, header, delivered, resolve_points)

    def verify(_frame: Any, _wx: Any) -> None:
        view, header = result["view"], result["header"]
        wait_until(
            _wx,
            lambda: bool(result["delivered"])
            and view._header_drag is None
            and not header.HasCapture(),
        )
        assert result["delivered"], (
            "The OS must deliver held motion while the header owns capture"
        )
        assert view.model.variant_order == ("", "B", "A", "C", "D")
        assert view._header_drag is None and not header.HasCapture()

    run_native(exercise, after_events=verify)


def test_native_sparse_selection_survives_reorder_sort_and_catalog_refresh(
    matrix: Any,
) -> None:
    """Nonadjacent component blocks retain one variant and their UUIDs through remapping."""

    def exercise(harness: MatrixHarness) -> None:
        view, wx = harness.view, harness.wx
        harness.click(0, view.model.column_for("A", "value"))
        components = ("component-1", "component-3")
        harness.click(2, view.model.column_for("A", "params"), command=True)
        pump(wx)
        expected = {
            (component, "A", column.key)
            for component in components
            for column in view.model.columns
            if column.variant == "A"
        }
        for phase in ("capture", "reorder", "sort"):
            if phase == "reorder":
                view.reorder_variant("A", "D")
            elif phase == "sort":
                col = view.model.column_for("A", "value")
                view.MakeCellVisible(0, col)
                pump(wx)
                left, _ = harness.viewport()
                x = (view.GetColLeft(col) + view.GetColRight(col)) // 2 - left
                y = (view._group_header_height + view.GetColLabelSize()) // 2
                harness.header_event(wx.wxEVT_LEFT_DOWN, x, y, down=True)
                harness.header_event(wx.wxEVT_LEFT_UP, x, y)
                assert view._sort_state == ("A", "value", False)
                assert view.model.rows[0].component_id == "component-3"
            for iteration in range(2):
                harness.refresh_catalog(f"Catalog update {iteration}")
                assert set(view.selected_component_ids()) == set(components)
                assert view.selected_target.variant == "A"
                assert set(view.selected_physical_component_ids()) == set(components)
                assert harness.selection() == expected
                assert len(view.GetSelectionBlockTopLeft()) <= len(components)
                assert set(view.capture_state().components) == set(components)

    matrix(exercise)


@pytest.mark.parametrize("legacy_resize", [False, True])
@pytest.mark.parametrize("frozen", [1, 5])
def test_native_freeze_permissions_follow_empty_repopulate_geometry(
    modules: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    frozen: int,
    legacy_resize: bool,
) -> None:
    """Filtering to empty cannot strand the previous fixed seam's resize lock."""
    view_module, model_module = modules
    if legacy_resize:
        monkeypatch.delattr(view_module.gridlib.Grid, "EnableColResize", raising=False)
        monkeypatch.delattr(
            view_module.gridlib.Grid, "GetFrozenColLabelWindow", raising=False
        )

    def exercise(frame: Any, wx: Any) -> None:
        model = display_model(model_module)
        harness = MatrixHarness(frame, wx, *modules, model)
        view = harness.view
        normal = wx.Font(view.GetDefaultCellFont())
        view.SetMinSize((1, 1))

        def geometry(count: int) -> None:
            view.SetDefaultCellFont(normal.Scaled(3) if count == 1 else normal)
            frame.SetSize((420, 450) if count == 1 else (1200, 650))
            settle(wx)

        previous = 5 if frozen == 1 else 1
        geometry(previous)
        assert view.GetNumberFrozenCols() == previous
        view.set_model(model_module.MatrixModel(Snapshot(())))
        geometry(frozen)
        view.set_model(display_model(model_module))
        settle(wx)
        assert view.GetNumberFrozenCols() == frozen
        assert not view.CanDragGridSize()
        if callable(getattr(view, "EnableColResize", None)):
            assert not view.CanDragColSize(frozen - 1)
        assert view.CanDragColSize(previous - 1), "The old seam is now a normal column"

        for side in ("frozen", "scrolling"):
            for double in (False, True):
                window = (
                    view._frozen_col_label_window()
                    if side == "frozen"
                    else view.GetGridColLabelWindow()
                )
                x = window.GetClientSize().width - 1 if side == "frozen" else 1
                y = view.GetColLabelSize() - 5
                widths = tuple(
                    view.GetColSize(col) for col in range(view.GetNumberCols())
                )
                events = (
                    ((wx.wxEVT_LEFT_DCLICK, x, True), (wx.wxEVT_LEFT_UP, x, False))
                    if double
                    else (
                        (wx.wxEVT_LEFT_DOWN, x, True),
                        (wx.wxEVT_MOTION, x + 30, True),
                        (wx.wxEVT_LEFT_UP, x + 30, False),
                    )
                )
                for kind, event_x, held in events:
                    support.send_mouse(
                        wx, window, kind, wx.Point(event_x, y), down=held
                    )
                assert (
                    tuple(view.GetColSize(col) for col in range(view.GetNumberCols()))
                    == widths
                )
                assert view.GetNumberFrozenCols() == frozen

    run_native(exercise)


@pytest.mark.parametrize("legacy_resize", [False, True])
def test_native_ref_only_seam_allows_scrolling_correction_auto_size(
    modules: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    legacy_resize: bool,
) -> None:
    """Correction can reset its manual width once it joins the scrolling columns."""
    view_module, model_module = modules
    if legacy_resize:
        monkeypatch.delattr(view_module.gridlib.Grid, "EnableColResize", raising=False)
        monkeypatch.delattr(
            view_module.gridlib.Grid, "GetFrozenColLabelWindow", raising=False
        )

    def exercise(frame: Any, wx: Any) -> None:
        harness = MatrixHarness(frame, wx, *modules, display_model(model_module))
        view = harness.view
        view.SetMinSize((1, 1))
        view.SetDefaultCellFont(view.GetDefaultCellFont().Scaled(3))
        frame.SetSize((420, 450))
        settle(wx)
        assert view.GetNumberFrozenCols() == 1
        window, point = support.cell_point(view, wx, 0, 0)
        support.send_mouse(wx, window, wx.wxEVT_MOTION, point)
        assert "R1" in window.GetToolTip().GetTip()
        for shared_col in range(5):
            click_native_cell(view, wx, 0, shared_col)
            assert view.selected_target.field == view.model.columns[shared_col].key
        col = view.model.column_for(None, "correction")
        resize(harness, col, view.GetColSize(col) + 50)
        assert "correction" in view.capture_preferences()["widths"]
        resize(harness, col)
        assert "correction" not in view.capture_preferences()["widths"]

    run_native(exercise)
