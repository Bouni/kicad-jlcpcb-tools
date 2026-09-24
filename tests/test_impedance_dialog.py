"""Exercise transactional impedance review without importing a real wx runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import replace
import importlib
import json
from pathlib import Path
import struct
import sys
import textwrap
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Optional

import pytest

from tests.test_impedance_dialog_preview import Image as PreviewImage
from tests.wx_harness import FakeWxModule

if TYPE_CHECKING:
    from impedance.dialog import ReviewSession, SpecificationDialog
    from impedance.dialog_preview import WorkbookPreview
    from impedance.model import BoardSnapshot, Config, Specification
    from impedance.service import CapturedImage


@pytest.fixture(scope="module")
def review_api() -> Iterator[SimpleNamespace]:
    """Load a private feature package with only the wx base class replaced."""
    package_name = "_impedance_dialog_review_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    wx = ModuleType("wx")
    wx.Dialog = object
    adv = ModuleType("wx.adv")
    dataview = ModuleType("wx.dataview")

    class _DataViewItemAttr:
        def SetBackgroundColour(self, _colour: object) -> None:
            return None

        def SetColour(self, _colour: object) -> None:
            return None

    class _DataViewListStore:
        pass

    dataview.DataViewItemAttr = _DataViewItemAttr
    dataview.DataViewListStore = _DataViewListStore
    patch = pytest.MonkeyPatch()
    patch.setitem(sys.modules, package_name, package)
    patch.setitem(sys.modules, "wx", wx)
    patch.setitem(sys.modules, "wx.adv", adv)
    patch.setitem(sys.modules, "wx.dataview", dataview)
    wx.adv = adv
    wx.dataview = dataview
    try:
        model = importlib.import_module(f"{package_name}.model")
        matching = importlib.import_module(f"{package_name}.matching")
        dialog = importlib.import_module(f"{package_name}.dialog")
        yield SimpleNamespace(model=model, matching=matching, dialog=dialog)
    finally:
        for name in tuple(sys.modules):
            if name.startswith(f"{package_name}."):
                del sys.modules[name]
        patch.undo()


def _specification(
    api: SimpleNamespace,
    *,
    spec_id: str = "clock",
    kind: str = "single_ended",
) -> Specification:
    """Describe front-layer routing with a physically distinct back reference."""
    return api.model.Specification(
        spec_id=spec_id,
        label=spec_id,
        target_ohms="90" if kind == "differential" else "50",
        kind=kind,
        net_class="RF",
        layer_settings=(
            api.model.LayerSettings(
                "F.Cu",
                ("B.Cu",),
                spacing_nm=200_000 if kind.startswith("differential") else None,
            ),
        ),
    )


def _snapshot(api: SimpleNamespace) -> BoardSnapshot:
    """Supply two matching disconnected routes and unrelated routing context."""
    trace = api.model.Trace
    return api.model.BoardSnapshot(
        layers=("F.Cu", "B.Cu"),
        traces=(
            trace("track-a", "F.Cu", "CLK_P", 150_000, ((0, 0), (2_000_000, 0))),
            trace(
                "track-b",
                "F.Cu",
                "CLK_N",
                150_000,
                ((0, 350_000), (2_000_000, 350_000)),
            ),
            trace("other", "B.Cu", "GND", 400_000, ((0, 0), (5_000_000, 0))),
        ),
        context_digest="original-board-context",
        differential_pairs=(("CLK_P", "CLK_N"),),
        net_classes=("Default", "RF"),
        net_class_memberships=(
            ("CLK_P", ("RF",)),
            ("CLK_N", ("RF",)),
            ("GND", ("Default",)),
        ),
        net_class_context_digest="classes-v1",
    )


def _config(api: SimpleNamespace) -> Config:
    """Build a disabled, unreviewed configuration ready for a first scan."""
    return api.model.Config(specifications=(_specification(api),))


def _session(api: SimpleNamespace) -> ReviewSession:
    """Create an independent dialog editing session."""
    return api.dialog.ReviewSession(_config(api), _snapshot(api))


def _long_route_session(
    api: SimpleNamespace, route_count: int = 2, kind: str = "differential"
) -> ReviewSession:
    """Provide complete 200 mm routes, each made from two connected track segments."""
    snapshot = _snapshot(api)
    net_names = ("CLK_P", "CLK_N", "CLK_C", "OTHER_P", "OTHER_N", "EXCLUDED")
    y_positions = (0, 350_000, 700_000, 10_000_000, 10_350_000, 20_000_000)
    traces = tuple(
        api.model.Trace(
            f"route-{route_index}-segment-{segment_index}",
            "F.Cu",
            net_names[route_index],
            150_000,
            (
                (segment_index * 100_000_000, y_positions[route_index]),
                ((segment_index + 1) * 100_000_000, y_positions[route_index]),
            ),
        )
        for route_index in range(route_count)
        for segment_index in range(2)
    )
    config = api.model.Config(specifications=(_specification(api, kind=kind),))
    return api.dialog.ReviewSession(config, replace(snapshot, traces=traces))


class _DimensionControl:
    """Retain the small editable-control surface used during unit conversion."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.items: list[str] = []
        self.enabled = True

    def GetValue(self) -> str:
        """Return text exactly as the user entered it."""
        return self.value

    def SetValue(self, value: str) -> None:
        """Update the displayed text."""
        self.value = value

    def GetStringSelection(self) -> str:
        """Return the selected unit or copper layer."""
        return self.value

    def SetStringSelection(self, value: str) -> None:
        """Restore or change the selected unit."""
        self.value = value

    def SetItems(self, items: list[str]) -> None:
        """Replace dropdown suggestions while simulating wx text clearing."""
        self.items = items
        self.value = ""

    def Enable(self, enabled: bool) -> None:
        """Retain whether the field or its label is enabled."""
        self.enabled = enabled

    def SetFocus(self) -> None:
        """Record focus requested by inline validation."""
        self.focused = True


class _LayoutDouble:
    """Accept constructor layout calls without claiming native sizing behavior."""

    instances: list[_LayoutDouble] = []

    def __init__(self, *_arguments: object, **_options: object) -> None:
        self.items: list[object] = []
        self.proportions: list[int] = []
        _LayoutDouble.instances.append(self)

    def Add(self, item: object, *_arguments: object) -> None:
        """Retain controls inserted into the layout."""
        self.items.append(item)
        if _arguments and isinstance(_arguments[0], int):
            self.proportions.append(_arguments[0])

    def Clear(self, delete_windows: bool = False) -> None:
        """Drop retained children, matching wx.Sizer.Clear(delete_windows)."""
        if delete_windows:
            for item in self.items:
                destroy = getattr(item, "Destroy", None)
                if callable(destroy):
                    destroy()
        self.items.clear()

    def AddGrowableCol(self, _index: int, _proportion: int) -> None:
        """Accept a sizing instruction outside the behavior tested here."""

    def AddStretchSpacer(self) -> None:
        """Retain stretch spacers without emulating native size calculation."""
        self.items.append("stretch")


class _HarnessFont:
    """Keep font changes observable without pretending to match native glyphs."""

    def __init__(
        self,
        source: object = 11,
        *_arguments: object,
        name: str = "native-default",
        **_options: object,
    ) -> None:
        self.name = getattr(source, "name", name)
        self.point_size = (
            float(source.GetPointSize())
            if hasattr(source, "GetPointSize")
            else float(source)
        )
        self.weight = getattr(source, "weight", 90)

    def GetPointSize(self) -> int:
        """Read the size used by the deterministic drawing context."""
        return round(self.point_size)

    def SetPointSize(self, size: int) -> None:
        """Update the font copied into later header or body measurements."""
        self.point_size = float(size)

    def GetNativeFontInfoDesc(self) -> str:
        """Distinguish role, size and weight when preview metrics are cached."""
        return f"{self.name}-{self.point_size:g}-{self.weight}"

    def Scaled(self, factor: float) -> _HarnessFont:
        """Return a distinct font whose measured text grows with its size."""
        result = _HarnessFont(self)
        result.point_size = self.point_size * factor
        return result

    def SetWeight(self, weight: int) -> None:
        """Retain emphasis used by native-like grid attributes."""
        self.weight = weight

    def IsOk(self) -> bool:
        """Match a successfully constructed wx.Font."""
        return self.point_size > 0


class _HarnessSize:
    """Expose both wx.Size properties and tuple unpacking to renderer code."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    def GetWidth(self) -> int:
        """Return the retained logical width."""
        return self.width

    def GetHeight(self) -> int:
        """Return the retained logical height."""
        return self.height

    def __iter__(self) -> Iterator[int]:
        yield self.width
        yield self.height

    def __getitem__(self, index: int) -> int:
        return (self.width, self.height)[index]


class _HarnessRect:
    """Retain native drawing bounds used by the production grid renderer."""

    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self.x, self.y, self.width, self.height = x, y, width, height

    def GetWidth(self) -> int:
        return self.width

    def GetHeight(self) -> int:
        return self.height


class _HarnessClientDC:
    """Measure using the font selected into the drawing context, as wx does."""

    def __init__(self, window: _ConstructedControl) -> None:
        self.window = window
        self.font = window.GetFont()
        self.drawn: list[object] = []

    def SetFont(self, font: _HarnessFont) -> None:
        self.font = font

    def GetTextExtent(self, text: str) -> _HarnessSize:
        assert isinstance(text, str)
        self.window.measured_text.append(text)
        factor = self.font.point_size / 11
        return _HarnessSize(round(len(text) * 7 * factor), round(14 * factor))

    def SetTextForeground(self, colour: object) -> None:
        self.foreground = colour

    def SetBackground(self, brush: object) -> None:
        self.background = brush

    def SetBrush(self, brush: object) -> None:
        self.brush = brush

    def SetPen(self, pen: object) -> None:
        self.pen = pen

    def DrawRectangle(self, *rectangle: object) -> None:
        self.drawn.append(("rectangle", rectangle))

    def DrawText(self, text: str, x: int, y: int) -> None:
        self.drawn.append(("text", text, x, y))


class _DataViewColumn:
    """Model requested and resolved column widths using deterministic metrics."""

    def __init__(
        self, owner: _ConstructedControl, index: int, width: int, flags: int = 1
    ) -> None:
        self.owner = owner
        self.index = index
        self.flags = flags
        self.minimum_width = 0
        self.width = width
        self.width_requests: list[int] = []
        self.SetWidth(width)

    def GetTitle(self) -> str:
        """Return the same title retained by the legacy tuple interface."""
        return self.owner.columns[self.index][0]

    def SetMinWidth(self, width: int) -> None:
        """Replace the minimum so later short content can shrink again."""
        self.minimum_width = width

    def GetMinWidth(self) -> int:
        """Read the current floor independently of the resolved width."""
        return self.minimum_width

    def SetWidth(self, width: int) -> None:
        """Resolve autosize against a limited sample, as native views may do."""
        self.width_requests.append(width)
        if width == -2:
            values = [self.GetTitle()]
            # Deliberately sample only initial rows: product code must ensure
            # later offscreen content also fits, without native wx assumptions.
            for row in self.owner.table_rows[:16]:
                if self.index not in self.owner.toggle_columns:
                    values.append(str(row[self.index]))
            width = max(self.owner.GetTextExtent(value)[0] for value in values) + 12
            if self.index in self.owner.toggle_columns:
                width = max(width, 18 + 12)
        self.width = max(self.minimum_width, width)
        self.owner.columns[self.index] = (self.GetTitle(), self.width)

    def GetWidth(self) -> int:
        """Read the resolved width rather than the autosize sentinel."""
        return self.width


class _ConstructedControl(_DimensionControl):
    """Model choice/checklist state and explicit user events for real constructors."""

    standard_button_ids: tuple[int, ...] = ()
    list_checked_event: Optional[int] = None
    list_unchecked_event: Optional[int] = None

    def __init__(
        self,
        _parent: object = None,
        *,
        label: str = "",
        choices: Optional[list[str]] = None,
        **_options: object,
    ) -> None:
        super().__init__(str(_options.get("value", label)))
        self.label = label
        self.window_id = _options.get("id")
        self.size = _options.get("size")
        self.parent = _parent
        self.children: list[object] = []
        if hasattr(_parent, "children"):
            _parent.children.append(self)
        self.columns: list[tuple[str, int]] = []
        self.dataview_columns: list[_DataViewColumn] = []
        self.measured_text: list[str] = []
        self.table_rows: list[list[object]] = []
        self.toggle_columns: list[int] = []
        self.id_controls: dict[int, _ConstructedControl] = {}
        self.items = list(choices or ())
        self.selection = -1
        self.checked: set[int] = set()
        self.bindings: dict[tuple[int, int], Callable[[object], None]] = {}
        self.layout_count = 0
        self.modal_result: Optional[int] = None
        self.foreground = "native-label-colour"
        self.shown = self.alive = True
        self.on_screen = False
        self.drawn: list[object] = []
        self.row_colours: dict[int, object] = {}
        self.cell_colours: dict[tuple[int, int], tuple[object, object]] = {}
        self.item_images: dict[tuple[int, int], int] = {}
        self.image_list: Optional[object] = None

    def __bool__(self) -> bool:
        return self.alive

    def GetId(self) -> int:
        """Retain explicit IDs and give anonymous windows a stable identity."""
        return int(self.window_id) if self.window_id is not None else id(self)

    def Destroy(self) -> None:
        """Mark the control dead when a sizer clears delete_windows=True."""
        self.alive = False
        if (
            isinstance(self.parent, _ConstructedControl)
            and self in self.parent.children
        ):
            self.parent.children.remove(self)

    def Show(self, shown: bool = True) -> None:
        self.shown = shown

    def Hide(self) -> None:
        self.Show(False)

    def IsShown(self) -> bool:
        return self.shown

    def IsShownOnScreen(self) -> bool:
        if not self.alive or not self.shown:
            return False
        if isinstance(self.parent, _ConstructedControl):
            return self.parent.IsShownOnScreen()
        return self.on_screen

    def SetBackgroundStyle(self, style: object) -> None:
        self.background_style = style

    def SetBackgroundColour(self, colour: object) -> None:
        self.background = colour

    def GetBackgroundColour(self) -> object:
        return getattr(self, "background", (20, 20, 20))

    def SetName(self, name: str) -> None:
        self.name = name

    def SetString(self, index: int, value: str) -> None:
        self.items[index] = value

    def SetItemTextColour(self, index: int, colour: object) -> None:
        self.row_colours[index] = colour

    def EnsureVisible(self, index: int) -> None:
        self.visible_index = index

    def GetCount(self) -> int:
        return len(self.items)

    def GetFirstSelected(self) -> int:
        return self.selection

    def GetSelectedRow(self) -> int:
        """DataViewListCtrl selection; wx.NOT_FOUND when nothing is selected."""
        return self.selection if self.selection >= 0 else -1

    def SelectRow(self, index: int) -> None:
        """Select one DataViewListCtrl row without synthesizing an event."""
        self.selection = index

    def RowToItem(self, row: int) -> SimpleNamespace:
        """Represent valid and invalid DataView row identities explicitly."""
        return SimpleNamespace(row=row, IsOk=lambda: 0 <= row < len(self.table_rows))

    def ItemToRow(self, item: SimpleNamespace) -> int:
        """Resolve a DataView identity without conflating it with selection."""
        return item.row if item.IsOk() else -1

    def Select(self, index: int, on: bool = True) -> None:
        """Model native programmatic selection's synchronous notification."""
        previous = self.selection
        self.selection = index if on else -1
        selected = getattr(type(self), "list_selected_event", None)
        if on and index != previous and (selected, -1) in self.bindings:
            self.emit(selected)

    def Focus(self, index: int) -> None:
        """Retain the focused ListCtrl row."""
        self.focused_index = index

    def AssociateModel(self, model: object) -> None:
        """Retain the DataView model used for attribute and value lookups."""
        self.model = model

    def GetModel(self) -> object:
        """Return the associated DataView model."""
        return getattr(self, "model", None)

    def GetColumn(self, index: int) -> _DataViewColumn:
        """Return a stateful DataView column with native-style sizing methods."""
        return self.dataview_columns[index]

    def GetColumnCount(self) -> int:
        """Return the number of columns without depending on model row count."""
        return len(self.dataview_columns)

    def GetTextExtent(self, text: str) -> tuple[int, int]:
        """Measure the retained font using deterministic, scalable test glyphs."""
        size = _HarnessClientDC(self).GetTextExtent(text)
        return size.width, size.height

    def FromDIP(self, value: int) -> int:
        """Use one test pixel per device-independent unit for predictable floors."""
        return value

    def AppendTextColumn(
        self,
        title: str,
        width: int = -1,
        mode: int = 0,
        flags: int = 1,
        **_options: object,
    ) -> _DataViewColumn:
        """Retain DataViewListCtrl column titles and widths."""
        self.columns.append((title, width))
        column = _DataViewColumn(self, len(self.columns) - 1, width, flags)
        self.dataview_columns.append(column)
        return column

    def AppendToggleColumn(
        self, title: str, width: int = -1, **_options: object
    ) -> _DataViewColumn:
        """Retain the boolean column used by native inclusion checkboxes."""
        self.toggle_columns.append(len(self.columns))
        self.columns.append((title, width))
        column = _DataViewColumn(self, len(self.columns) - 1, width)
        self.dataview_columns.append(column)
        return column

    def GetToggleValue(self, row: int, col: int) -> bool:
        """Read checkbox state from the same model updated by its setter."""
        return bool(self.table_rows[row][col])

    def SetToggleValue(self, value: bool, row: int, col: int) -> None:
        """Update a DataView toggle without synthesizing a user event."""
        self.table_rows[row][col] = value

    def GetTextValue(self, row: int, col: int) -> str:
        """Read a native text cell by row and column."""
        return str(self.table_rows[row][col])

    def AppendItem(self, values: list[object]) -> None:
        """Append one DataViewListCtrl row into the control and associated store."""
        row = list(values)
        self.table_rows.append(row)
        self.items.append(row[0])
        model = getattr(self, "model", None)
        if model is not None and hasattr(model, "rows"):
            model.rows.append(row)

    def EnableCheckBoxes(self, enable: bool = True) -> None:
        """Record that report-style checkboxes were requested."""
        self.checkboxes_enabled = enable

    def CheckItem(self, index: int, checked: bool = True) -> None:
        """Change a ListCtrl checkbox and fire CHECKED/UNCHECKED like native wx."""
        previous = self.IsChecked(index)
        self.Check(index, checked)
        if previous == checked:
            return
        code = self.list_checked_event if checked else self.list_unchecked_event
        if code is None:
            return
        handler = self.bindings.get((code, -1))
        if handler is not None:
            handler(SimpleNamespace(GetIndex=lambda: index, GetSelection=lambda: index))

    def IsItemChecked(self, index: int) -> bool:
        """Read ListCtrl checkbox state retained by CheckItem or Check."""
        return self.IsChecked(index)

    def Freeze(self) -> None:
        """Accept Freeze during programmatic list rebuilds."""

    def Thaw(self) -> None:
        """Accept Thaw after programmatic list rebuilds."""

    def GetForegroundColour(self) -> object:
        """Read the colour retained by the actual native setter contract."""
        return self.foreground

    def SetForegroundColour(self, colour: object) -> None:
        """Retain colour changes so subsequent reads verify restoration."""
        self.foreground = colour

    def Refresh(self) -> None:
        """Record invalidation without simulating native painting."""
        self.refreshed = True

    def SetSelection(self, index: int) -> None:
        """Update selected choice and readable text without synthesizing an event."""
        self.selection = index
        self.value = self.items[index] if 0 <= index < len(self.items) else ""

    def GetSelection(self) -> int:
        """Return the selected choice index."""
        return self.selection

    def SetStringSelection(self, value: str) -> bool:
        """Match wx's case-insensitive first match without emitting an event."""
        for index, item in enumerate(self.items):
            if item.casefold() == value.casefold():
                self.SetSelection(index)
                return True
        return False

    def SetItems(self, items: list[str]) -> None:
        """Replace items and clear the selection as a wx dropdown does."""
        super().SetItems(items)
        self.selection = -1
        self.checked.clear()

    def Check(self, index: int, checked: bool = True) -> None:
        """Change a checkbox without creating a command event."""
        if checked:
            self.checked.add(index)
        else:
            self.checked.discard(index)

    def IsChecked(self, index: int) -> bool:
        """Read the checkbox state saved by earlier user or programmatic changes."""
        return index in self.checked

    def Bind(self, event: int, handler: Callable[[object], None], id: int = -1) -> None:
        """Capture the handler that the real constructor binds to an event."""
        self.bindings[event, id] = handler

    def emit(
        self,
        event: int,
        control_id: int = -1,
        *,
        row: Optional[int] = None,
        column: int = 0,
    ) -> None:
        """Dispatch a user event after the test has updated the actual control state."""
        self.bindings[event, control_id](
            SimpleNamespace(
                GetSelection=self.GetSelection,
                GetIndex=lambda: self.selection,
                GetItem=lambda: self.RowToItem(self.selection if row is None else row),
                GetColumn=lambda: column,
                GetEventObject=lambda: self,
                GetId=self.GetId,
                Skip=lambda: None,
            )
        )

    def SetLabel(self, label: str) -> None:
        """Retain the currently visible status text."""
        self.label = label
        self.value = label

    def GetLabel(self) -> str:
        """Read the native label independently from a checkbox's checked value."""
        return self.label

    def GetURL(self) -> str:
        """Read the destination retained by a hyperlink control."""
        return getattr(self, "url", "")

    def SetURL(self, url: str) -> None:
        """Retain a hyperlink destination without opening a browser."""
        self.url = url

    def SetToolTip(self, tooltip: str) -> None:
        """Retain control guidance supplied by the constructor."""
        self.tooltip = tooltip

    def SetMinSize(self, size: tuple[int, int]) -> None:
        """Accept minimum sizing without emulating platform layout."""
        self.minimum_size = size

    def SetMaxSize(self, size: tuple[int, int]) -> None:
        """Accept maximum sizing without emulating platform layout."""
        self.maximum_size = size

    def SetCursor(self, cursor: object) -> None:
        """Retain the cursor assigned for clickable captures."""
        self.cursor = cursor

    def Wrap(self, width: int) -> None:
        """Accept text wrapping without claiming native measurement."""
        self.wrap_width = width

    def GetBestSize(self) -> SimpleNamespace:
        """Measure wrapped text independently of the requested viewport minimum."""
        width = getattr(self, "wrap_width", 440)
        lines = textwrap.wrap(
            self.label, max(1, width // 8), break_long_words=False
        ) or [""]
        return SimpleNamespace(
            GetWidth=lambda: max(map(len, lines)) * 8,
            GetHeight=lambda: len(lines) * 18,
        )

    def GetFont(self) -> _HarnessFont:
        """Read the font retained by its setter, creating the default once."""
        if not hasattr(self, "font"):
            self.font = (
                _HarnessFont(self.parent.GetFont())
                if isinstance(self.parent, _ConstructedControl)
                else _HarnessFont()
            )
        return self.font

    def SetFont(self, font: object) -> None:
        """Retain a font change without claiming native typeface metrics."""
        self.font = font

    def InvalidateBestSize(self) -> None:
        """Keep native metric invalidation observable."""
        self.best_size_invalidated = True

    def SetSize(self, size: tuple[int, int]) -> None:
        """Retain actual child size separately from its minimum size."""
        self.size = size

    def SetVirtualSize(self, size: tuple[int, int]) -> None:
        """Retain complete scrollable content bounds."""
        self.virtual_size = size

    def Scroll(self, horizontal: int, vertical: int) -> None:
        """Retain the viewport position set for a new diagnostic."""
        self.scroll_position = (horizontal, vertical)

    def Layout(self) -> None:
        """Record requested layout updates without computing native geometry."""
        self.layout_count += 1

    def SetSizer(self, sizer: _LayoutDouble) -> None:
        """Retain the dialog's layout tree."""
        self.sizer = sizer

    def GetClientSize(self) -> SimpleNamespace:
        """Supply a usable viewport for actual preview fitting methods."""
        return SimpleNamespace(GetWidth=lambda: 440, GetHeight=lambda: 260)

    def SetBitmap(self, bitmap: object) -> None:
        """Keep the bitmap later inspected for stale-image protection."""
        self.bitmap = bitmap

    def Clear(self) -> None:
        """Clear listbox contents and check state together."""
        self.SetItems([])

    def Append(self, value: str) -> None:
        """Append a visible checklist row."""
        self.items.append(value)

    def InsertColumn(self, index: int, title: str, width: int) -> None:
        """Retain column labels and native width requests."""
        self.columns.insert(index, (title, width))

    def DeleteAllItems(self) -> None:
        """Clear report rows before rebuilding a specification summary."""
        self.table_rows.clear()
        self.items.clear()
        self.checked.clear()
        self.selection = -1
        self.row_colours.clear()
        self.cell_colours.clear()
        self.item_images.clear()
        model = getattr(self, "model", None)
        if model is not None and hasattr(model, "rows"):
            model.rows.clear()

    def SetTextValue(self, value: str, row: int, column: int) -> None:
        """Update native-style DataView text and its model in place."""
        self.table_rows[row][column] = value
        model = getattr(self, "model", None)
        if model is not None and hasattr(model, "rows"):
            model.rows[row][column] = value

    def AssignImageList(self, images: object, _which: int) -> None:
        """Retain the Review status chip list."""
        self.image_list = images

    def SetItemColumnImage(self, row: int, column: int, image: int) -> None:
        """Record a per-column ListCtrl image index."""
        self.item_images[(row, column)] = image

    def InsertItem(self, index: int, value: str, image_index: int = -1) -> int:
        """Insert and retain a native report row, optionally with a chip image."""
        self.table_rows.insert(index, [value])
        self.items.insert(index, value)
        if image_index >= 0:
            self.item_images[(index, 0)] = image_index
        return index

    def GetItemCount(self) -> int:
        """Read the current report row count."""
        return len(self.table_rows)

    def GetItemText(self, row: int, column: int = 0) -> str:
        """Read actual ListCtrl cell content independently of its selection."""
        return str(self.table_rows[row][column])

    def GetItem(self, row: int, column: int = 0) -> SimpleNamespace:
        """Return a mutable ListItem stand-in for one report cell."""
        while len(self.table_rows[row]) <= column:
            self.table_rows[row].append("")
        text = self.table_rows[row][column]
        background, foreground = self.cell_colours.get((row, column), (None, None))
        item = SimpleNamespace(
            row=row,
            column=column,
            text=text,
            background=background,
            foreground=foreground,
        )
        item.GetId = lambda: item.row
        item.GetColumn = lambda: item.column
        item.GetText = lambda: item.text
        item.SetText = lambda value: setattr(item, "text", value)
        item.SetTextColour = lambda colour: setattr(item, "foreground", colour)
        item.SetBackgroundColour = lambda colour: setattr(item, "background", colour)
        item.GetTextColour = lambda: item.foreground
        item.GetBackgroundColour = lambda: item.background
        return item

    def SetItem(self, row: object, column: object = None, value: str = "") -> None:
        """Retain cell text, or apply ListItem colours for one column."""
        if column is None:
            item = row
            row_index = int(item.GetId())
            column_index = int(item.GetColumn())
            while len(self.table_rows[row_index]) <= column_index:
                self.table_rows[row_index].append("")
            self.table_rows[row_index][column_index] = str(item.GetText())
            self.cell_colours[(row_index, column_index)] = (
                item.GetBackgroundColour(),
                item.GetTextColour(),
            )
            return
        row_index = int(row)
        column_index = int(column)
        while len(self.table_rows[row_index]) <= column_index:
            self.table_rows[row_index].append("")
        self.table_rows[row_index][column_index] = value

    def FindWindow(self, control_id: int) -> Optional[_ConstructedControl]:
        """Search existing descendants, without inventing a missing standard button."""
        for child in self.children:
            if child.window_id == control_id:
                return child
            match = child.FindWindow(control_id)
            if match is not None:
                return match
        return None

    def SetScrollRate(self, horizontal: int, vertical: int) -> None:
        """Retain scroll units without claiming actual Mac layout verification."""
        self.scroll_rate = (horizontal, vertical)

    def FitInside(self) -> None:
        """Record virtual-area updates without emulating native measurement."""
        self.fit_inside_count = getattr(self, "fit_inside_count", 0) + 1

    def CreateButtonSizer(self, _flags: int) -> _LayoutDouble:
        """Create real stateful standard-button descendants before initial lookup."""
        layout = _LayoutDouble()
        for control_id in self.standard_button_ids:
            button = _ConstructedControl(self, id=control_id)
            self.id_controls[control_id] = button
            layout.Add(button)
        return layout

    def CentreOnParent(self) -> None:
        """Accept centering without invoking native window APIs."""

    def EndModal(self, result: int) -> None:
        """Retain the accepted modal result for the workflow assertion."""
        self.modal_result = result
        self.on_screen = False


class _ConstructedTextControl(_ConstructedControl):
    """Model native TextCtrl.SetValue's synchronous text notification."""

    text_event = -1

    def SetValue(self, value: str) -> None:
        """Publish the new value before invoking a bound EVT_TEXT handler."""
        self.value = value
        if (self.text_event, -1) in self.bindings:
            self.emit(self.text_event)


class _HyperlinkControl(_ConstructedControl):
    """Capture HyperlinkCtrl(parent, id, label, url) without opening a browser."""

    def __init__(
        self,
        parent: object = None,
        window_id: int = -1,
        label: str = "",
        url: str = "",
        **options: object,
    ) -> None:
        super().__init__(parent, label=label, id=window_id, **options)
        self.label = label
        self.url = url
        self.shown = False


class _HarnessTimer:
    """Record Start/Stop without delivering ticks unless a test asks."""

    def __init__(self, owner: object) -> None:
        self.owner = owner
        self.running = False
        self.interval_ms = 0
        self.one_shot = False

    def Start(self, milliseconds: int, oneShot: bool = False) -> bool:
        self.interval_ms = milliseconds
        self.one_shot = oneShot
        self.running = True
        return True

    def StartOnce(self, milliseconds: int) -> bool:
        return self.Start(milliseconds, oneShot=True)

    def Stop(self) -> None:
        self.running = False

    def IsRunning(self) -> bool:
        return self.running


class _DataViewItemAttr:
    """Record DataView cell colours without native Cocoa rendering."""

    def __init__(self) -> None:
        self.background: Optional[object] = None
        self.colour: Optional[object] = None

    def SetBackgroundColour(self, colour: object) -> None:
        self.background = colour

    def SetColour(self, colour: object) -> None:
        self.colour = colour


class _DataViewListStore:
    """Hold DataViewListCtrl rows for GetValueByRow / GetAttrByRow."""

    def __init__(self) -> None:
        self.rows: list[list[object]] = []

    def GetValueByRow(self, row: int, col: int) -> object:
        return self.rows[row][col]

    def GetAttrByRow(self, row: int, col: int, attr: _DataViewItemAttr) -> bool:
        return False


class _HarnessGridTableBase:
    """Retain the view attached by wx.Grid.SetTable without owning its data."""

    def __init__(self) -> None:
        self.view: Optional[_HarnessGrid] = None

    def SetView(self, view: _HarnessGrid) -> None:
        self.view = view

    def GetView(self) -> Optional[_HarnessGrid]:
        return self.view


class _HarnessGridCellAttr:
    """Keep renderer attributes readable after native-style setters."""

    def __init__(self) -> None:
        self.font = _HarnessFont()
        self.background: object = (255, 255, 255)
        self.foreground: object = (0, 0, 0)
        self.alignment = (0, 0)
        self.read_only = False

    def SetFont(self, font: _HarnessFont) -> None:
        self.font = font

    def GetFont(self) -> _HarnessFont:
        return self.font

    def SetBackgroundColour(self, colour: object) -> None:
        self.background = colour

    def GetBackgroundColour(self) -> object:
        return self.background

    def SetTextColour(self, colour: object) -> None:
        self.foreground = colour

    def GetTextColour(self) -> object:
        return self.foreground

    def SetAlignment(self, horizontal: int, vertical: int) -> None:
        self.alignment = horizontal, vertical

    def GetAlignment(self) -> tuple[int, int]:
        return self.alignment

    def SetReadOnly(self, read_only: bool = True) -> None:
        self.read_only = read_only

    def Clone(self) -> _HarnessGridCellAttr:
        result = _HarnessGridCellAttr()
        result.__dict__.update(self.__dict__)
        return result


class _HarnessGridCellRenderer:
    """Base class for the production renderer, rather than a renderer substitute."""

    def Clone(self) -> _HarnessGridCellRenderer:
        return type(self)()


class _HarnessGridTableMessage:
    """Retain row notifications without mutating the authoritative table twice."""

    def __init__(self, table: object, message: int, *arguments: int) -> None:
        self.table, self.message, self.arguments = table, message, arguments


class _HarnessGridEvent:
    """Use grid row/column events independently of DataView item identities."""

    def __init__(
        self,
        event_type: int = 0,
        window_id: int = -1,
        obj: object = None,
        row: int = -1,
        col: int = -1,
        **_options: object,
    ) -> None:
        self.event_type, self.window_id = event_type, window_id
        self.obj, self.row, self.col = obj, row, col
        self.allowed, self.skipped = True, False

    def GetEventType(self) -> int:
        return self.event_type

    def GetEventObject(self) -> object:
        return self.obj

    def SetEventObject(self, obj: object) -> None:
        self.obj = obj

    def GetRow(self) -> int:
        return self.row

    def SetRow(self, row: int) -> None:
        self.row = row

    def GetCol(self) -> int:
        return self.col

    def SetCol(self, col: int) -> None:
        self.col = col

    def Skip(self, skipped: bool = True) -> None:
        self.skipped = skipped

    def Veto(self) -> None:
        self.allowed = False

    def IsAllowed(self) -> bool:
        return self.allowed

    def ControlDown(self) -> bool:
        return False

    def ShiftDown(self) -> bool:
        return False


class _HarnessGrid(_ConstructedControl):
    """Keep real table, cursor, attributes and sizing behavior for WorkbookGrid."""

    SelectRows = 1
    Tab_Stop = 0
    Tab_Leave = 2

    def SetCanFocus(self, can_focus: bool) -> None:
        """Retain whether the outer composite control owns native keyboard focus."""
        self.can_focus = can_focus

    def __init__(self, parent: object = None, **options: object) -> None:
        super().__init__(parent, **options)
        self.table: Optional[object] = None
        self.default_cell_font = _HarnessFont(self.GetFont())
        self.default_cell_font.name = "native-body"
        self.label_font = _HarnessFont(name="native-label")
        self.default_renderer: Optional[object] = None
        self.default_cell_text_colour: object = (0, 0, 0)
        self.default_cell_background_colour: object = (255, 255, 255)
        self.cell_fonts: dict[tuple[int, int], _HarnessFont] = {}
        self.cell_text_colours: dict[tuple[int, int], object] = {}
        self.cell_background_colours: dict[tuple[int, int], object] = {}
        self.col_minima: dict[int, int] = {}
        self.col_sizes: dict[int, int] = {}
        self.cursor_row, self.cursor_col = -1, -1
        self.selected_rows: set[int] = set()
        self.batch_depth = 0
        self.table_messages: list[_HarnessGridTableMessage] = []
        self.auto_size_requests: list[bool] = []
        self.scroll_rate = (15, 15)
        self.scroll_position = (0, 0)

    def _synchronize_table_diagnostics(self) -> None:
        """Expose the same live data to existing test diagnostics and the grid."""
        if self.table is None:
            return
        self.table_rows = self.table.rows
        self.items = [row[0] for row in self.table_rows]
        count = self.table.GetNumberCols()
        self.columns = [
            (self.table.GetColLabelValue(col), self.col_sizes.get(col, 80))
            for col in range(count)
        ]
        self.dataview_columns = []

    def SetTable(
        self, table: object, takeOwnership: bool = False, selmode: int = 0
    ) -> bool:
        self.table = table
        self.take_table_ownership, self.selection_mode = takeOwnership, selmode
        table.SetView(self)
        self._synchronize_table_diagnostics()
        return True

    def GetTable(self) -> object:
        return self.table

    def ProcessTableMessage(self, message: _HarnessGridTableMessage) -> bool:
        """Apply native row notifications after the backing table has changed."""
        assert message.table is self.table
        self.table_messages.append(message)
        self._synchronize_table_diagnostics()
        grid = sys.modules["wx.grid"]
        count = self.GetNumberRows()
        if message.message == grid.GRIDTABLE_NOTIFY_ROWS_DELETED:
            position, deleted = message.arguments
            for attributes in (
                self.cell_fonts,
                self.cell_text_colours,
                self.cell_background_colours,
                self.cell_colours,
            ):
                retained = {
                    ((row - deleted if row >= position + deleted else row), col): value
                    for (row, col), value in attributes.items()
                    if not position <= row < position + deleted
                }
                attributes.clear()
                attributes.update(retained)
            self.selected_rows = {
                row - deleted if row >= position + deleted else row
                for row in self.selected_rows
                if not position <= row < position + deleted
            }
            if not count:
                self.cursor_row = self.cursor_col = -1
                self.selected_rows.clear()
            elif self.cursor_row >= position:
                self.cursor_row = (
                    self.cursor_row - deleted
                    if self.cursor_row >= position + deleted
                    else min(position, count - 1)
                )
        elif (
            message.message == grid.GRIDTABLE_NOTIFY_ROWS_APPENDED
            and count
            and self.GetNumberCols()
            and (self.cursor_row < 0 or self.cursor_col < 0)
        ):
            # wxGrid creates a current cell when an empty table gains rows.
            self.cursor_row = self.cursor_col = 0
        self.selection = self.cursor_row
        self._clamp_scroll_position()
        return True

    def GetNumberRows(self) -> int:
        return self.table.GetNumberRows() if self.table is not None else 0

    def GetNumberCols(self) -> int:
        return self.table.GetNumberCols() if self.table is not None else 0

    def GetCellValue(self, row: int, col: int) -> str:
        return self.table.GetValue(row, col)

    def SetCellValue(self, row: int, col: int, value: str) -> None:
        """Update the authoritative table without generating a user edit event."""
        self.table.SetValue(row, col, value)
        self._synchronize_table_diagnostics()

    def GetColLabelValue(self, col: int) -> str:
        return self.table.GetColLabelValue(col)

    def SetRowLabelSize(self, width: int) -> None:
        self.row_label_size = width

    def GetRowLabelSize(self) -> int:
        return getattr(self, "row_label_size", 0)

    def SetColMinimalAcceptableWidth(self, width: int) -> None:
        self.col_minimal_acceptable_width = width

    def EnableEditing(self, enabled: bool) -> None:
        self.editing_enabled = enabled

    def SetTabBehaviour(self, behaviour: int) -> None:
        """Retain native navigation policy independently of cell selection."""
        self.tab_behaviour = behaviour

    def GetTabBehaviour(self) -> int:
        """Read the native default until the product chooses a traversal policy."""
        return getattr(self, "tab_behaviour", self.Tab_Stop)

    def DisableDragRowSize(self) -> None:
        self.drag_row_size_enabled = False

    def DisableDragColMove(self) -> None:
        self.drag_col_move_enabled = False

    def DisableDragGridSize(self) -> None:
        self.drag_grid_size_enabled = False

    def SetDefaultRenderer(self, renderer: object) -> None:
        self.default_renderer = renderer

    def GetDefaultRenderer(self) -> object:
        return self.default_renderer

    def GetCellRenderer(self, row: int, col: int) -> object:
        return self.default_renderer

    def SetDefaultCellAlignment(self, horizontal: int, vertical: int) -> None:
        self.default_cell_alignment = horizontal, vertical

    def SetColLabelAlignment(self, horizontal: int, vertical: int) -> None:
        self.col_label_alignment = horizontal, vertical

    def GetColLabelAlignment(self) -> tuple[int, int]:
        return getattr(self, "col_label_alignment", (0, 0))

    def SetDefaultCellFont(self, font: _HarnessFont) -> None:
        self.default_cell_font = font

    def GetDefaultCellFont(self) -> _HarnessFont:
        return self.default_cell_font

    def SetLabelFont(self, font: _HarnessFont) -> None:
        self.label_font = font

    def GetLabelFont(self) -> _HarnessFont:
        return self.label_font

    def SetCellFont(self, row: int, col: int, font: _HarnessFont) -> None:
        self.cell_fonts[row, col] = font

    def GetCellFont(self, row: int, col: int) -> _HarnessFont:
        return self.cell_fonts.get((row, col), self.default_cell_font)

    def GetDefaultCellTextColour(self) -> object:
        return self.default_cell_text_colour

    def SetDefaultCellTextColour(self, colour: object) -> None:
        """Retain the default text palette used by subsequent native-style reads."""
        self.default_cell_text_colour = colour

    def GetDefaultCellBackgroundColour(self) -> object:
        return self.default_cell_background_colour

    def SetDefaultCellBackgroundColour(self, colour: object) -> None:
        """Retain the default fill independently of per-cell overrides."""
        self.default_cell_background_colour = colour

    def GetSelectionBackground(self) -> object:
        return (30, 80, 160)

    def GetSelectionForeground(self) -> object:
        return (255, 255, 255)

    def SetCellTextColour(self, row: int, col: int, colour: object) -> None:
        self.cell_text_colours[row, col] = colour

    def GetCellTextColour(self, row: int, col: int) -> object:
        return self.cell_text_colours.get((row, col), self.default_cell_text_colour)

    def SetCellBackgroundColour(self, row: int, col: int, colour: object) -> None:
        self.cell_background_colours[row, col] = colour

    def GetCellBackgroundColour(self, row: int, col: int) -> object:
        return self.cell_background_colours.get(
            (row, col), self.default_cell_background_colour
        )

    def GetCellAttr(self, row: int, col: int) -> _HarnessGridCellAttr:
        attr = _HarnessGridCellAttr()
        attr.SetFont(self.GetCellFont(row, col))
        attr.SetTextColour(self.GetCellTextColour(row, col))
        attr.SetBackgroundColour(self.GetCellBackgroundColour(row, col))
        return attr

    def SetDefaultRowSize(self, height: int, resizeExistingRows: bool = False) -> None:
        self.default_row_size = height

    def GetDefaultRowSize(self) -> int:
        return getattr(self, "default_row_size", 20)

    def GetRowSize(self, row: int) -> int:
        return self.GetDefaultRowSize()

    def SetColLabelSize(self, height: int) -> None:
        self.col_label_size = height

    def GetColLabelSize(self) -> int:
        return getattr(self, "col_label_size", 20)

    def SetColMinimalWidth(self, col: int, width: int) -> None:
        self.col_minima[col] = max(
            getattr(self, "col_minimal_acceptable_width", 0), width
        )

    def GetColMinimalWidth(self, col: int) -> int:
        return self.col_minima.get(col, 0)

    def SetColSize(self, col: int, width: int) -> None:
        self.col_sizes[col] = max(self.GetColMinimalWidth(col), width)
        if col < len(self.columns):
            self.columns[col] = self.columns[col][0], self.col_sizes[col]
        self._clamp_scroll_position()

    def GetColSize(self, col: int) -> int:
        return self.col_sizes.get(col, 80)

    def BeginBatch(self) -> None:
        self.batch_depth += 1

    def EndBatch(self) -> None:
        assert self.batch_depth > 0
        self.batch_depth -= 1

    def GetBatchCount(self) -> int:
        """Expose nested batch ownership so failure cleanup can be verified."""
        return self.batch_depth

    def AutoSizeColumns(self, setAsMin: bool = True) -> None:
        """Consult every real renderer best size and keep grid's own 10-DIP inset."""
        self.auto_size_requests.append(setAsMin)
        dc = _HarnessClientDC(self)
        for col in range(self.GetNumberCols()):
            dc.SetFont(self.GetLabelFont())
            width = dc.GetTextExtent(self.GetColLabelValue(col)).GetWidth()
            for row in range(self.GetNumberRows()):
                attr = self.GetCellAttr(row, col)
                if self.default_renderer is not None:
                    size = self.default_renderer.GetBestSize(self, attr, dc, row, col)
                else:
                    dc.SetFont(attr.GetFont())
                    size = dc.GetTextExtent(self.GetCellValue(row, col))
                width = max(width, size.GetWidth())
            width += self.FromDIP(10)
            self.SetColSize(col, width)
            if setAsMin:
                self.SetColMinimalWidth(col, self.GetColSize(col))

    def SetGridCursor(self, row: int, col: int) -> None:
        self.cursor_row, self.cursor_col = row, col
        self.selection = row

    def GetGridCursorRow(self) -> int:
        return self.cursor_row

    def GetGridCursorCol(self) -> int:
        return self.cursor_col

    def SelectRow(self, row: int, addToSelected: bool = False) -> None:
        """Select rows without moving the cursor or synthesizing select events."""
        if not addToSelected:
            self.selected_rows.clear()
        if 0 <= row < self.GetNumberRows():
            self.selected_rows.add(row)

    def GetSelectedRows(self) -> list[int]:
        return sorted(self.selected_rows)

    def ClearSelection(self) -> None:
        self.selected_rows.clear()

    def IsInSelection(self, row: int, col: int) -> bool:
        return row in self.selected_rows

    def MakeCellVisible(self, row: int, col: int) -> None:
        """Move only enough to bring the requested cell into the scroll viewport."""
        self.visible_cell = row, col
        if not (0 <= row < self.GetNumberRows() and 0 <= col < self.GetNumberCols()):
            return
        horizontal, vertical = self.scroll_position
        unit_x, unit_y = self.scroll_rate
        visible_width, visible_height = self._grid_viewport_size()
        left = sum(self.GetColSize(index) for index in range(col))
        right = left + self.GetColSize(col)
        top = row * self.GetDefaultRowSize()
        bottom = top + self.GetDefaultRowSize()
        if unit_x:
            if left < horizontal * unit_x or right - left > visible_width:
                horizontal = left // unit_x
            elif right > horizontal * unit_x + visible_width:
                horizontal = (right - visible_width + unit_x - 1) // unit_x
        if unit_y:
            if top < vertical * unit_y or bottom - top > visible_height:
                vertical = top // unit_y
            elif bottom > vertical * unit_y + visible_height:
                vertical = (bottom - visible_height + unit_y - 1) // unit_y
        self.Scroll(horizontal, vertical)

    def _grid_viewport_size(self) -> tuple[int, int]:
        """Exclude labels from the deterministic test window's scrollable area."""
        size = self.GetClientSize()
        return (
            max(1, size.GetWidth() - self.GetRowLabelSize()),
            max(1, size.GetHeight() - self.GetColLabelSize()),
        )

    def _clamp_scroll_position(self) -> None:
        """Clamp retained scroll units when row or column extents shrink."""
        visible_width, visible_height = self._grid_viewport_size()
        content_width = sum(self.GetColSize(col) for col in range(self.GetNumberCols()))
        content_height = self.GetNumberRows() * self.GetDefaultRowSize()
        coordinates = []
        for origin, content, visible, unit in zip(
            self.scroll_position,
            (content_width, content_height),
            (visible_width, visible_height),
            self.scroll_rate,
        ):
            maximum = (max(0, content - visible) + unit - 1) // unit if unit else 0
            coordinates.append(max(0, min(origin, maximum)))
        self.scroll_position = tuple(coordinates)

    def Scroll(self, horizontal: int, vertical: int) -> None:
        """Retain each axis independently; -1 leaves that axis unchanged."""
        previous_x, previous_y = self.scroll_position
        self.scroll_position = (
            previous_x if horizontal == -1 else horizontal,
            previous_y if vertical == -1 else vertical,
        )
        self._clamp_scroll_position()

    def GetViewStart(self) -> tuple[int, int]:
        """Read the same scroll-unit state that Scroll and visibility update."""
        return self.scroll_position

    def SetScrollRate(self, horizontal: int, vertical: int) -> None:
        self.scroll_rate = horizontal, vertical
        self._clamp_scroll_position()

    def GetScrollPixelsPerUnit(self) -> tuple[int, int]:
        return self.scroll_rate

    def GetGridWindow(self) -> _HarnessGrid:
        return self

    def ForceRefresh(self) -> None:
        self.Refresh()

    def GetEventHandler(self) -> _HarnessGrid:
        return self

    def ProcessEvent(self, event: _HarnessGridEvent) -> bool:
        handler = self.bindings.get((event.GetEventType(), -1))
        if handler is None:
            return False
        handler(event)
        return True

    def emit(
        self,
        event: int,
        control_id: int = -1,
        *,
        row: Optional[int] = None,
        column: int = 0,
    ) -> None:
        """Dispatch actual grid row/column coordinates, never DataView item IDs."""
        notification = _HarnessGridEvent(
            event,
            control_id,
            self,
            self.cursor_row if row is None else row,
            column,
        )
        self.bindings[event, control_id](notification)


@pytest.fixture
def constructor_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Load an isolated dialog using stateful controls and the real constructor."""
    package_name = "_impedance_dialog_constructor_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    errors: list[str] = []
    preview_calls: list[object] = []
    queued: list[tuple[Callable[..., object], tuple[object, ...]]] = []
    _LayoutDouble.instances = []

    def queue(callback: Callable[..., object], *arguments: object) -> None:
        queued.append((callback, arguments))

    def drain() -> None:
        while queued:
            callback, arguments = queued.pop(0)
            callback(*arguments)

    class PaintDC:
        def __init__(self, canvas: _ConstructedControl) -> None:
            self.canvas = canvas

        def SetBackground(self, colour: object) -> None:
            self.colour = colour

        def Clear(self) -> None:
            self.canvas.drawn.clear()

        def DrawBitmap(self, bitmap: object, x: int, y: int, transparent: bool) -> None:
            self.canvas.drawn.append((bitmap, x, y))

    def paint(pane: WorkbookPreview) -> None:
        pane.canvas.bindings[wx.EVT_PAINT, -1](SimpleNamespace())
        drain()

    def finish_preparation(dialog: SpecificationDialog) -> None:
        """Deliver idle warming separately from timers and visible paint events."""
        timer = dialog._capture_prepare_timer
        for _attempt in range(100):
            if hasattr(dialog, "_capture_prepare_sections"):
                if timer.IsRunning() or not dialog._capture_prepare_sections:
                    return
                requested = []
                event = SimpleNamespace(
                    Skip=lambda: None,
                    RequestMore=lambda requested=requested: requested.append(True),
                )
                dialog.bindings[wx.EVT_IDLE, -1](event)
                drain()
                if not requested:
                    return
                continue
            if not timer.IsRunning():
                return
            if timer.interval_ms > 25:
                return  # A future retry is deliberately not due in this event turn.
            timer.running = False
            dialog.bindings[wx.EVT_TIMER, timer](SimpleNamespace())
            drain()
        pytest.fail("Capture preparation did not finish")

    def show_error(message: str, *_arguments: object) -> None:
        """Capture validation failures without opening a native message box."""
        errors.append(message)

    class _Bitmap:
        """Accept size-based chip construction and image-based preview bitmaps."""

        def __init__(
            self, width_or_image: object, height: Optional[int] = None
        ) -> None:
            if height is None:
                self.source = width_or_image
                self.width = getattr(width_or_image, "width", None)
                self.height = getattr(width_or_image, "height", None)
            else:
                self.source = None
                self.width = int(width_or_image)  # type: ignore[arg-type]
                self.height = height
            self.drawn_label: Optional[str] = None
            self.chip_bg: Optional[object] = None
            self.text_colour: Optional[object] = None

        def IsOk(self) -> bool:
            return True

    class _MemoryDC:
        """Record chip drawing without requiring a native graphics context."""

        def __init__(self, bitmap: Optional[_Bitmap] = None) -> None:
            self.bitmap = bitmap
            self._bg: Optional[object] = None
            self._fg: Optional[object] = None
            self.font = _HarnessFont()

        def SetBackground(self, brush: object) -> None:
            self._bg = brush
            if self.bitmap is not None:
                self.bitmap.chip_bg = brush

        def Clear(self) -> None:
            return None

        def SetTextForeground(self, colour: object) -> None:
            self._fg = colour

        def SetFont(self, font: object) -> None:
            self.font = font

        def GetTextExtent(self, text: str) -> _HarnessSize:
            """Use the selected font rather than silently measuring a default."""
            factor = self.font.point_size / 11
            return _HarnessSize(round(len(text) * 7 * factor), round(14 * factor))

        def DrawText(self, text: str, _x: int, _y: int) -> None:
            if self.bitmap is not None:
                self.bitmap.drawn_label = text
                self.bitmap.text_colour = self._fg

        def SelectObject(self, bitmap: object) -> None:
            self.bitmap = None if bitmap is null_bitmap else bitmap

    class _ImageList:
        """Retain Review chip bitmaps in insertion order."""

        def __init__(self, width: int, height: int) -> None:
            self.width = width
            self.height = height
            self.bitmaps: list[_Bitmap] = []

        def Add(self, bitmap: _Bitmap) -> int:
            self.bitmaps.append(bitmap)
            return len(self.bitmaps) - 1

    null_bitmap = object()

    class _RendererNative:
        """Provide checkbox glyph metrics independently of text and DIP margins."""

        @staticmethod
        def Get() -> _RendererNative:
            return _RendererNative()

        def GetCheckBoxSize(
            self, _window: _ConstructedControl, _flags: int = 0
        ) -> _HarnessSize:
            return _HarnessSize(18, 18)

        def DrawCheckBox(
            self,
            _window: _ConstructedControl,
            dc: _HarnessClientDC,
            bounds: _HarnessRect,
            flags: int = 0,
        ) -> None:
            """Retain exact glyph bounds and checked state for renderer assertions."""
            dc.drawn.append(("checkbox", bounds, flags))

    wx = FakeWxModule(
        "wx",
        Dialog=_ConstructedControl,
        ScrolledWindow=_ConstructedControl,
        Panel=_ConstructedControl,
        TextCtrl=_ConstructedTextControl,
        Choice=_ConstructedControl,
        ComboBox=_ConstructedTextControl,
        CheckListBox=_ConstructedControl,
        ListCtrl=_ConstructedControl,
        CheckBox=_ConstructedControl,
        StaticText=_ConstructedControl,
        StaticLine=_ConstructedControl,
        StaticBitmap=_ConstructedControl,
        Image=lambda source, _kind: PreviewImage(
            struct.unpack_from(">II", source.getvalue(), 16)
        ),
        Bitmap=_Bitmap,
        NullBitmap=null_bitmap,
        MemoryDC=_MemoryDC,
        ClientDC=_HarnessClientDC,
        Size=_HarnessSize,
        Rect=_HarnessRect,
        DCClipper=lambda *_arguments: nullcontext(),
        ImageList=_ImageList,
        IMAGE_LIST_SMALL=1,
        Font=_HarnessFont,
        FONTFAMILY_DEFAULT=70,
        FONTSTYLE_NORMAL=90,
        FONTWEIGHT_NORMAL=90,
        LogNull=nullcontext,
        Button=_ConstructedControl,
        Timer=_HarnessTimer,
        BoxSizer=_LayoutDouble,
        FlexGridSizer=_LayoutDouble,
        MessageBox=show_error,
        Colour=lambda *rgb: tuple(rgb),
        SystemSettings=SimpleNamespace(GetColour=lambda _kind: (120, 120, 120)),
        RendererNative=_RendererNative,
        AutoBufferedPaintDC=PaintDC,
        Brush=lambda colour: colour,
        CallAfter=queue,
        WakeUpIdle=lambda: None,
        NullCursor=object(),
        NOT_FOUND=-1,
        COL_WIDTH_AUTOSIZE=-2,
        Platform="__WXMAC__",
    )
    adv = FakeWxModule("wx.adv", HyperlinkCtrl=_HyperlinkControl)
    dataview = FakeWxModule(
        "wx.dataview",
        DataViewListCtrl=_ConstructedControl,
        DataViewListStore=_DataViewListStore,
        DataViewItemAttr=_DataViewItemAttr,
        DATAVIEW_CELL_INERT=1,
        DATAVIEW_COL_RESIZABLE=1,
    )
    grid = FakeWxModule(
        "wx.grid",
        Grid=_HarnessGrid,
        GridTableBase=_HarnessGridTableBase,
        GridCellRenderer=_HarnessGridCellRenderer,
        GridCellAttr=_HarnessGridCellAttr,
        GridTableMessage=_HarnessGridTableMessage,
        GridEvent=_HarnessGridEvent,
        EVT_GRID_SELECT_CELL=1_000_001,
        EVT_GRID_CELL_LEFT_CLICK=1_000_002,
        EVT_GRID_CELL_LEFT_DCLICK=1_000_003,
        EVT_GRID_CELL_CHANGED=1_000_004,
        GRIDTABLE_NOTIFY_ROWS_APPENDED=1_001_001,
        GRIDTABLE_NOTIFY_ROWS_DELETED=1_001_002,
    )
    wx.adv = adv
    wx.dataview = dataview
    wx.grid = grid
    wx.__path__ = []
    _ConstructedTextControl.text_event = wx.EVT_TEXT
    _ConstructedControl.list_checked_event = wx.EVT_LIST_ITEM_CHECKED
    _ConstructedControl.list_unchecked_event = wx.EVT_LIST_ITEM_UNCHECKED
    monkeypatch.setattr(
        _ConstructedControl,
        "list_selected_event",
        wx.EVT_LIST_ITEM_SELECTED,
        raising=False,
    )
    monkeypatch.setattr(
        _ConstructedControl, "standard_button_ids", (wx.ID_OK, wx.ID_CANCEL)
    )
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "wx.adv", adv)
    monkeypatch.setitem(sys.modules, "wx.dataview", dataview)
    monkeypatch.setitem(sys.modules, "wx.grid", grid)
    try:
        model = importlib.import_module(f"{package_name}.model")
        dialog = importlib.import_module(f"{package_name}.dialog")
        from tests.test_impedance_shared_preview import _png

        service = importlib.import_module(f"{package_name}.service")
        capture = service.CapturedImage.from_bytes(_png())

        def preview(section: object, *, refresh: bool = False) -> CapturedImage:
            """Return immutable production-contract pixels for the exact selected row."""
            preview_calls.append(section)
            return capture

        def create_dialog(
            snapshot: BoardSnapshot,
            specification: Optional[Specification] = None,
            *,
            render: Optional[Callable[..., CapturedImage]] = preview,
            review_tracking: object = None,
        ) -> SpecificationDialog:
            """Construct the actual dialog with a stateful native-image decoder double."""
            kwargs = {}
            if review_tracking is not None:
                kwargs["review_tracking"] = review_tracking
            result = dialog.SpecificationDialog(
                None, snapshot, specification, preview=render, **kwargs
            )
            result.on_screen = True
            drain()
            paint(result.preview_pane)
            return result

        yield SimpleNamespace(
            model=model,
            dialog=dialog,
            wx=wx,
            dv=dataview,
            grid=grid,
            errors=errors,
            preview=preview,
            preview_calls=preview_calls,
            create_dialog=create_dialog,
            queued=queued,
            drain=drain,
            paint=paint,
            finish_preparation=finish_preparation,
            capture=capture,
        )
    finally:
        for name in tuple(sys.modules):
            if name.startswith(f"{package_name}."):
                del sys.modules[name]


def _checked_reference_layers(dialog: SpecificationDialog) -> tuple[str, ...]:
    """Read reference planes from actual checklist state used by Save."""
    return tuple(
        layer
        for index, layer in enumerate(dialog.layers)
        if dialog.references.IsChecked(index)
    )


def test_new_specification_requires_an_explicit_net_class(
    constructor_api: SimpleNamespace,
) -> None:
    """Require explicit net-class intent with no retired width or name-filter controls."""
    api = constructor_api
    dialog = api.create_dialog(_snapshot(api))
    assert not hasattr(dialog, "selection_mode")
    assert not hasattr(dialog, "nets")
    assert not hasattr(dialog, "width")
    assert dialog.net_class.GetStringSelection() == "Choose a net class…"
    assert dialog.target.GetValue() == "50"
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result is None
    assert api.errors == []
    assert dialog.validation_message.GetValue()


def test_kind_default_changes_to_usb_impedance_but_preserves_custom_target(
    constructor_api: SimpleNamespace,
) -> None:
    """Offer 50/90 ohm defaults without overwriting a user's custom impedance."""
    api = constructor_api
    dialog = api.create_dialog(_snapshot(api))
    dialog.kind.SetSelection(1)
    dialog.kind.emit(api.wx.EVT_CHOICE)
    assert dialog.target.GetValue() == "90"
    dialog.target.SetValue("85")
    for kind in (0, 3, 2):
        dialog.kind.SetSelection(kind)
        dialog.kind.emit(api.wx.EVT_CHOICE)
        assert dialog.target.GetValue() == "85"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("single_ended", (False, False)),
        ("differential", (True, False)),
        ("single_ended_coplanar", (False, True)),
        ("differential_coplanar", (True, True)),
    ],
)
def test_dimension_fields_match_selected_impedance_kind(
    review_api: SimpleNamespace, kind: str, expected: tuple[bool, bool]
) -> None:
    """Only electrically relevant spacing and coplanar-gap controls are required."""
    assert review_api.dialog.dimension_fields_for_kind(kind) == expected


def test_dimension_field_relevance_rejects_unknown_kind(
    review_api: SimpleNamespace,
) -> None:
    """Reject unknown construction codes instead of exposing arbitrary controls."""
    with pytest.raises(ValueError):
        review_api.dialog.dimension_fields_for_kind("coplanar_single_ended")


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("F.Cu", ("In1.Cu",)),
        ("In1.Cu", ("F.Cu", "In2.Cu")),
        ("In2.Cu", ("In1.Cu", "B.Cu")),
        ("B.Cu", ("In2.Cu",)),
    ],
)
def test_reference_defaults_follow_physical_stack_order(
    review_api: SimpleNamespace, signal: str, expected: tuple[str, ...]
) -> None:
    """Default outer and inner references to their adjacent physical copper planes."""
    layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert review_api.dialog.adjacent_reference_layers(layers, signal) == expected
    selection = review_api.dialog.ReferenceLayerSelection(layers, signal)
    assert selection.selected == expected
    assert selection.automatic


def test_reference_override_is_explicit_and_self_reference_is_removed(
    review_api: SimpleNamespace,
) -> None:
    """Retain manual references until explicitly restoring adjacent-layer defaults."""
    selection = review_api.dialog.ReferenceLayerSelection(
        ("F.Cu", "In1.Cu", "B.Cu"), "F.Cu"
    )
    assert selection.set_manual(("F.Cu", "B.Cu")) == ("F.Cu",)
    assert selection.selected == ("B.Cu",)
    assert not selection.automatic
    selection.set_manual(())
    assert selection.selected == ()
    selection.use_adjacent()
    assert selection.selected == ("In1.Cu",)
    assert selection.automatic


@pytest.mark.parametrize("invalid", ["signal", "reference", "self"])
def test_invalid_reference_selection_is_rejected(
    review_api: SimpleNamespace, invalid: str
) -> None:
    """Reject unavailable or self-referencing saved copper declarations."""
    signal = "absent" if invalid == "signal" else "F.Cu"
    selected = (
        ("F.Cu",)
        if invalid == "self"
        else ("absent",)
        if invalid == "reference"
        else None
    )
    with pytest.raises(ValueError):
        review_api.dialog.ReferenceLayerSelection(("F.Cu", "B.Cu"), signal, selected)


def test_edits_do_not_mutate_original_configuration(
    review_api: SimpleNamespace,
) -> None:
    """Cancel remains safe after editing intent and populating new candidates."""
    original = _config(review_api)
    session = review_api.dialog.ReviewSession(original, _snapshot(review_api))
    changed = replace(original.specifications[0], target_ohms="55")
    session.replace_specifications((changed,))
    session.refresh()
    assert original == _config(review_api)
    assert session.config.specifications == (changed,)
    assert not original.reviewed_digest


def test_first_refresh_selects_rows_without_approving_and_allows_draft_save(
    review_api: SimpleNamespace,
) -> None:
    """Discovery creates selectable rows, not export authority, while draft save works."""
    session = _session(review_api)
    result = session.refresh()
    assert len(result.sections) == 2
    assert session.included == {row.section_id for row in result.sections}
    assert not session.approved
    saved = session.save_result()
    assert saved.specifications == session.config.specifications
    assert not saved.reviewed_digest
    assert saved.included_section_ids == ()


@pytest.mark.parametrize(
    "state", ["before_refresh", "empty_selection", "unknown_section"]
)
def test_invalid_selection_and_approval_are_rejected(
    review_api: SimpleNamespace, state: str
) -> None:
    """Do not approve missing rows or accept IDs outside the current board analysis."""
    session = _session(review_api)
    if state != "before_refresh":
        session.refresh()
    before = set(session.included)
    if state == "unknown_section":
        with pytest.raises(ValueError):
            session.set_included(("absent",))
        assert session.included == before
    else:
        if state == "empty_selection":
            session.set_included(())
        with pytest.raises(ValueError):
            session.approve()
    assert not session.approved


def test_approved_selection_round_trips_and_unchanged_refresh_preserves_it(
    review_api: SimpleNamespace,
) -> None:
    """Reopening preserves current approval and exclusions without a new human action."""
    session = _session(review_api)
    result = session.refresh()
    selected = result.sections[-1].section_id
    session.set_included((selected,))
    session.approve()
    saved = session.save_result()
    review_api.matching.validate_review(saved, result)
    restored = review_api.model.Config.from_dict(
        json.loads(json.dumps(saved.to_dict()))
    )
    reopened = review_api.dialog.ReviewSession(restored, _snapshot(review_api))
    reopened.refresh()
    assert reopened.approved
    assert reopened.included == {selected}
    reopened.refresh()
    assert reopened.save_result() == saved


@pytest.mark.parametrize(
    "change", ["selection", "specification", "geometry", "context", "new_candidate"]
)
def test_changed_review_inputs_preserve_intent_but_revoke_approval(
    review_api: SimpleNamespace, change: str
) -> None:
    """Changed settings, selection or board content cannot reuse old export authority."""
    session = _session(review_api)
    analysis = session.refresh()
    session.approve()
    snapshot = session.snapshot
    if change == "selection":
        session.set_included((analysis.sections[0].section_id,))
    elif change == "specification":
        session.replace_specifications(
            (replace(session.config.specifications[0], target_ohms="55"),)
        )
        session.refresh()
    else:
        changed = replace(snapshot, context_digest="different pads")
        if change == "geometry":
            changed = replace(
                snapshot,
                traces=(replace(snapshot.traces[0], points=((0, 0), (3_000_000, 0))),)
                + snapshot.traces[1:],
            )
        elif change == "new_candidate":
            changed = replace(
                snapshot,
                traces=snapshot.traces
                + (
                    replace(
                        snapshot.traces[0],
                        trace_id="new",
                        points=((4_000_000, 0), (5_000_000, 0)),
                    ),
                ),
            )
        session.refresh(changed)
    assert not session.approved
    candidate = session.save_result()
    assert candidate.specifications
    assert candidate.reviewed_digest == ""
    assert candidate.included_section_ids == ()


def test_save_against_changed_snapshot_returns_unapproved_draft(
    review_api: SimpleNamespace,
) -> None:
    """An explicit save preserves intent but cannot authorize stale captures."""
    session = _session(review_api)
    session.refresh()
    session.approve()
    candidate = session.save_result(replace(session.snapshot, context_digest="changed"))
    assert candidate.enabled
    assert candidate.specifications == session.config.specifications
    assert not candidate.reviewed_digest


def test_save_accepts_reordered_identical_snapshot(review_api: SimpleNamespace) -> None:
    """Native enumeration order alone does not invalidate an approved report."""
    session = _session(review_api)
    session.refresh()
    session.approve()
    result = session.save_result(
        replace(session.snapshot, traces=tuple(reversed(session.snapshot.traces)))
    )
    assert result.reviewed_digest == session.config.reviewed_digest


def test_each_specification_requires_an_included_section(
    review_api: SimpleNamespace,
) -> None:
    """Excluding a complete requirement must not silently omit it from the report."""
    api = review_api
    specs = tuple(
        replace(_specification(api, spec_id=name), net_class=name)
        for name in ("Positive", "Negative")
    )
    snapshot = replace(
        _snapshot(api),
        net_classes=("Default", "Positive", "Negative"),
        net_class_memberships=(
            ("CLK_P", ("Positive",)),
            ("CLK_N", ("Negative",)),
            ("GND", ("Default",)),
        ),
    )
    session = api.dialog.ReviewSession(api.model.Config(specifications=specs), snapshot)
    rows = session.refresh().sections
    session.set_included((rows[0].section_id,))
    with pytest.raises(ValueError):
        session.approve()


@pytest.mark.parametrize("kind", ["single_ended", "differential"])
def test_long_route_review_keeps_track_coverage_and_native_pair_identity(
    review_api: SimpleNamespace, kind: str
) -> None:
    """Review closer single-ended views while preserving complete native pair rows."""
    session = _long_route_session(review_api, kind=kind)
    rows = session.refresh().sections
    assert sorted(trace.trace_id for row in rows for trace in row.traces) == sorted(
        trace.trace_id for trace in session.snapshot.traces
    )
    if kind == "differential":
        assert len(rows) == 1
        assert set(rows[0].net_names) == {"CLK_P", "CLK_N"}
        assert rows[0].bounds[2] - rows[0].bounds[0] >= 200_000_000
    else:
        # Joining either pair of 100 mm / 0.15 mm tracks into a 200 mm view
        # would make the selected copper less than one pixel wide at export.
        # Individual source track IDs stay whole, so each needs its own view.
        assert all(len(row.traces) == 1 for row in rows)
        assert {row.net_names for row in rows} == {("CLK_P",), ("CLK_N",)}
        for net in ("CLK_P", "CLK_N"):
            signal_rows = [row for row in rows if row.net_names == (net,)]
            assert len(signal_rows) == 2
            assert min(row.bounds[0] for row in signal_rows) == -75_000
            assert max(row.bounds[2] for row in signal_rows) == 200_075_000
    session.approve()
    assert session.save_result().included_section_ids == tuple(
        row.section_id for row in rows
    )


def test_ui_impedance_kind_codes_match_domain_contract(
    review_api: SimpleNamespace,
) -> None:
    """Every UI construction choice corresponds to a supported persisted domain kind."""
    assert {code for code, _label in review_api.dialog.KIND_CHOICES} == set(
        review_api.model.KINDS
    )
