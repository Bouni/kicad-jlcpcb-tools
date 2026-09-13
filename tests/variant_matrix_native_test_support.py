"""Real grid construction, identity-based input, and native geometry for view tests."""

from collections.abc import Callable, Iterator
import importlib
import os
from types import ModuleType
from typing import Any, Optional

import pytest

from .native_wx_support import pump, run_native
from .variant_model_test_support import Snapshot, State, assignment
from .wx_harness import package_stubs, temporary_modules

VARIANTS = ("", "A", "B", "C", "D")
native_marks = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="Native wx requires an explicitly enabled desktop session",
    ),
]


@pytest.fixture
def modules() -> Iterator[tuple[ModuleType, ModuleType]]:
    """Import the product modules without registering a KiCad plugin."""
    pytest.importorskip("wx.grid")
    package = "matrix_native_tests"
    with temporary_modules(package_stubs(package), namespaces=(package,)):
        yield tuple(
            importlib.import_module(f"{package}.variant.{name}")
            for name in ("matrix_view", "matrix_model")
        )


def make_model(
    module: ModuleType,
    *,
    rows: int = 3,
    variants: tuple[str, ...] = VARIANTS,
    populated: bool = True,
    stale: bool = False,
) -> Any:
    """Build distinct real component states and matching catalog metadata."""
    states = tuple(
        State(
            f"component-{row}",
            f"R{row}",
            name,
            value=f"{rows + 1 - row}{index}k",
            lcsc=f"C{row}{index}01",
            assignment=assignment(inherited=name == "B"),
        )
        for row in range(1, rows + 1)
        if populated
        for index, name in enumerate(variants)
    )
    metadata = {
        (state.component_id, state.variant_name): module.CatalogMetadata(
            lcsc="C999999" if stale else state.lcsc,
            params="10kΩ ±1%",
            type="Extended" if state.variant_name == "A" else "Basic",
            standard=state.variant_name == "A",
            stock=1000,
            price="0.20" if state.variant_name == "A" else "0.10",
            status="complete",
            description=f"Precision resistor for {state.component_id}/{state.variant_name or 'Default'}; full catalog description",
        )
        for state in states
    }
    return module.MatrixModel(
        Snapshot(states, names=variants),
        enrichment=metadata,
        corrections={
            state.component_id: module.CorrectionState(
                final_angle=state.pcb_angle % 360
            )
            for state in states
            if state.variant_name == ""
        },
    )


def cell_point(view: Any, wx: Any, row: int, col: int) -> tuple[Any, Any]:
    """Locate the visible cell in whichever native child currently contains it."""
    view.MakeCellVisible(row, col)
    pump(wx)
    window = (
        view.GetFrozenColGridWindow()
        if col < view.GetNumberFrozenCols()
        else view.GetGridWindow()
    )
    rect = view.CellToRect(row, col)
    x, y = view.CalcGridWindowScrolledPosition(
        rect.x + rect.width // 2, rect.y + rect.height // 2, window
    )
    offset = view.GetGridWindowOffset(window)
    return window, wx.Point(x - offset.x, y - offset.y)


def send_mouse(
    wx: Any,
    window: Any,
    kind: int,
    point: Any,
    *,
    down: bool = False,
    shift: bool = False,
    command: bool = False,
    pump_events: bool = True,
) -> None:
    """Deliver a marked native mouse event through its real window's event handler."""
    event = wx.MouseEvent(kind)
    event._variant_test_event = True
    event.SetEventObject(window)
    event.SetPosition(point)
    event.SetLeftDown(down)
    event.SetShiftDown(shift)
    event.SetControlDown(command)
    event.SetMetaDown(command)
    window.GetEventHandler().ProcessEvent(event)
    if pump_events:
        pump(wx)


def click_native_cell(
    view: Any,
    wx: Any,
    row: int,
    col: int,
    *,
    shift: bool = False,
    command: bool = False,
    right: bool = False,
    double: bool = False,
) -> None:
    """Click through native selection and activation using current frozen geometry."""
    kinds = (
        (wx.wxEVT_RIGHT_DOWN, wx.wxEVT_RIGHT_UP)
        if right
        else (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP)
    )
    if double:
        kinds += (wx.wxEVT_LEFT_DCLICK, wx.wxEVT_LEFT_UP)
    for kind in kinds:
        window, point = cell_point(view, wx, row, col)
        send_mouse(wx, window, kind, point, shift=shift, command=command)


class MatrixHarness:
    """Route input through actual frozen/scrolled children without emulating wx state."""

    def __init__(
        self,
        frame: Any,
        wx: Any,
        view_module: ModuleType,
        model_module: ModuleType,
        model: Any = None,
        *,
        view: Any = None,
    ) -> None:
        self.frame, self.wx = frame, wx
        self.view_module, self.model_module = view_module, model_module
        self.actions: list[tuple[str, Any]] = []
        self.edits: list[tuple[Any, Any]] = []
        self.activations: list[Any] = []
        self.targets: list[Any] = []
        self.physical_notifications: list[tuple[str, ...]] = []
        if view is None:
            view = view_module.VariantMatrixView(
                frame,
                model if model is not None else make_model(model_module),
                on_action=lambda name, target: self.actions.append((name, target)),
                on_edit=lambda target, value: self.edits.append((target, value)),
                on_activate=self.activations.append,
                on_target_changed=self.targets.append,
                on_selection_changed=self.physical_notifications.append,
            )
            self.mount(view)
        else:
            self.view, self.sizer = view, frame.GetSizer()
            view.GetGridColLabelWindow().Bind(
                wx.EVT_MOTION, self._ignore_unpressed_os_motion
            )

    def mount(self, view: Any) -> None:
        """Mount one real grid, retaining any controller-owned callbacks."""
        self.view = view
        self.sizer = self.wx.BoxSizer(self.wx.VERTICAL)
        self.sizer.Add(view, 1, self.wx.EXPAND)
        self.frame.SetSizer(self.sizer)
        self.frame.Layout()
        view.GetGridColLabelWindow().Bind(
            self.wx.EVT_MOTION, self._ignore_unpressed_os_motion
        )
        pump(self.wx)

    def _ignore_unpressed_os_motion(self, event: Any) -> None:
        """Keep unrelated OS motion from contradicting an injected held gesture."""
        if (
            self.view._header_drag is not None
            and not getattr(event, "_variant_test_event", False)
            and not event.LeftIsDown()
        ):
            return
        event.Skip()

    def reopen(
        self, *, model: Any = None, preferences: Optional[dict[str, Any]] = None
    ) -> None:
        """Reconstruct the grid with product-persisted preferences and fresh selection."""
        if preferences is None:
            preferences = self.view.capture_preferences()
        self.sizer.Detach(self.view)
        self.view.Destroy()
        pump(self.wx)
        self.mount(
            self.view_module.VariantMatrixView(
                self.frame, make_model(self.model_module) if model is None else model
            )
        )
        self.view.restore_preferences(preferences)
        pump(self.wx)

    def cell_point(self, row: int, col: int) -> tuple[Any, Any]:
        """Locate a cell relative to its current native child window."""
        return cell_point(self.view, self.wx, row, col)

    def mouse(self, window: Any, kind: int, point: Any, **modifiers: bool) -> None:
        """Send a native event with the requested held-button/modifier state."""
        send_mouse(self.wx, window, kind, point, **modifiers)

    def click(self, row: int, col: int, **modifiers: bool) -> None:
        """Click through the same input path used by standalone grid tests."""
        click_native_cell(self.view, self.wx, row, col, **modifiers)

    def drag_cells(self, col: int) -> None:
        """Drag a column's component cells through native mouse selection handling."""
        for kind, row in (
            (self.wx.wxEVT_LEFT_DOWN, 0),
            (self.wx.wxEVT_MOTION, 0),
            (self.wx.wxEVT_MOTION, 1),
            (self.wx.wxEVT_MOTION, 2),
            (self.wx.wxEVT_LEFT_UP, 2),
        ):
            window, point = self.cell_point(row, col)
            self.mouse(window, kind, point, down=kind != self.wx.wxEVT_LEFT_UP)

    def hover(self, row: int, col: int) -> str:
        """Read the actual child tooltip after a native motion event."""
        window, point = self.cell_point(row, col)
        self.mouse(window, self.wx.wxEVT_MOTION, point)
        tooltip = window.GetToolTip()
        return tooltip.GetTip() if tooltip else ""

    def key(
        self,
        code: int,
        *,
        command: bool = False,
        shift: bool = False,
        native: bool = False,
        pump_events: bool = True,
    ) -> None:
        """Send commands through CHAR_HOOK or navigation through the native grid."""
        kind = self.wx.wxEVT_KEY_DOWN if native else self.wx.wxEVT_CHAR_HOOK
        event = self.wx.KeyEvent(kind)
        event.SetEventObject(self.view)
        event.SetKeyCode(code)
        event.SetControlDown(command)
        event.SetMetaDown(command)
        event.SetShiftDown(shift)
        self.view.GetEventHandler().ProcessEvent(event)
        if pump_events:
            pump(self.wx)

    def selection(self) -> set[tuple[str, Optional[str], str]]:
        """Read component-block membership without mistaking native rows for all variants."""
        return {
            (row.component_id, col.variant, col.key)
            for row_index, row in enumerate(self.view.model.rows)
            for col_index, col in enumerate(self.view.model.columns)
            if self.view.is_variant_selected(row_index, col_index)
        }

    def order(self) -> tuple[str, ...]:
        """Read visible group identities from the production model."""
        return self.view.model.variant_order

    def bounds(self, variant: str) -> tuple[int, int]:
        """Return logical boundaries for one complete variant block."""
        return self.view.GetColLeft(
            self.view.model.column_for(variant, "value")
        ), self.view.GetColRight(self.view.model.column_for(variant, "price"))

    def viewport(self) -> tuple[int, int]:
        """Return the scrolling child's logical horizontal interval."""
        window = self.view.GetGridWindow()
        offset = self.view.GetGridWindowOffset(window)
        left, _ = self.view.CalcGridWindowUnscrolledPosition(*offset, window)
        return left, left + window.GetClientSize().width

    def header_event(
        self, kind: int, x: int, y: Optional[int] = None, *, down: bool = False
    ) -> None:
        """Send a mouse event to the scrolling name/subheader window."""
        self.mouse(
            self.view.GetGridColLabelWindow(),
            kind,
            self.wx.Point(x, self.view._group_header_height // 2 if y is None else y),
            down=down,
        )

    def start_drag(
        self,
        variant: str,
        before: str,
        *,
        after_press: Optional[Callable[[], None]] = None,
    ) -> int:
        """Start a group gesture using current native geometry; leave it captured."""
        view = self.view
        origin = view.GetGridWindowOffset(view.GetGridWindow()).x
        view.Scroll(
            max(0, min(self.bounds(variant)[0], self.bounds(before)[0]) - origin),
            view.GetViewStart()[1],
        )
        pump(self.wx)
        left, right = self.viewport()
        start = (
            max(self.bounds(variant)[0], left) + min(self.bounds(variant)[1], right)
        ) // 2 - left
        destination = max(self.bounds(before)[0] + 3, left + 3) - left
        self.header_event(self.wx.wxEVT_LEFT_DOWN, start, down=True)
        if after_press:
            after_press()
        self.header_event(self.wx.wxEVT_MOTION, destination, down=True)
        return destination

    def drag(self, variant: str, before: str) -> None:
        """Complete a group gesture through native header mouse handlers."""
        destination = self.start_drag(variant, before)
        self.header_event(self.wx.wxEVT_LEFT_UP, destination)

    def refresh_catalog(self, params: str = "fresh supplier parameters") -> Any:
        """Publish supplier-only changes on a later event-loop turn."""
        metadata = {
            (state.component_id, state.variant_name): self.model_module.CatalogMetadata(
                params=params, lcsc=state.lcsc, status="complete"
            )
            for state in self.view.model.snapshot.components
        }
        model = self.model_module.MatrixModel(
            self.view.model.snapshot,
            enrichment=metadata,
            corrections=self.view.model._corrections,
        )
        self.wx.CallAfter(self.view.set_model, model)
        pump(self.wx)
        return model


@pytest.fixture
def matrix(modules: tuple[ModuleType, ModuleType]) -> Callable[..., None]:
    """Run each independent assertion workflow inside a real wx event loop."""
    if os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1":
        pytest.skip("Native matrix controls require KICAD_JLCPCB_NATIVE_TESTS=1")

    def run(
        check: Callable[[MatrixHarness], None],
        model: Any = None,
        *,
        size: tuple[int, int] = (1200, 650),
    ) -> None:
        run_native(
            lambda frame, wx: check(MatrixHarness(frame, wx, *modules, model)),
            size=size,
        )

    return run
