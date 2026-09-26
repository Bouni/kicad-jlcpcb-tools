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
    patch = pytest.MonkeyPatch()
    patch.setitem(sys.modules, package_name, package)
    patch.setitem(sys.modules, "wx", wx)
    patch.setitem(sys.modules, "wx.adv", adv)
    wx.adv = adv
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

    def __init__(self, *_arguments: object, **_options: object) -> None:
        self.items: list[object] = []

    def Add(self, item: object, *_arguments: object) -> None:
        """Retain controls inserted into the layout."""
        self.items.append(item)

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


class _ConstructedControl(_DimensionControl):
    """Model choice/checklist state and explicit user events for real constructors."""

    standard_button_ids: tuple[int, ...] = ()

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
        self.parent = _parent
        self.children: list[object] = []
        if hasattr(_parent, "children"):
            _parent.children.append(self)
        self.columns: list[tuple[str, int]] = []
        self.table_rows: list[list[str]] = []
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

    def __bool__(self) -> bool:
        return self.alive

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
        self.value = self.items[index] if index >= 0 else ""

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

    def emit(self, event: int, control_id: int = -1) -> None:
        """Dispatch a user event after the test has updated the actual control state."""
        self.bindings[event, control_id](
            SimpleNamespace(GetSelection=self.GetSelection)
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

    def GetFont(self) -> SimpleNamespace:
        """Supply stable native-like font identity for metric caching."""
        return SimpleNamespace(
            GetNativeFontInfoDesc=lambda: "native-default",
            GetPointSize=lambda: 11,
        )

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
        self.selection = -1
        self.row_colours.clear()

    def InsertItem(self, index: int, value: str) -> int:
        """Insert and retain a native report row."""
        self.table_rows.insert(index, [value])
        return index

    def GetItemCount(self) -> int:
        """Read the current report row count."""
        return len(self.table_rows)

    def SetItem(self, row: int, column: int, value: str) -> None:
        """Retain the displayed specification summary cell."""
        while len(self.table_rows[row]) <= column:
            self.table_rows[row].append("")
        self.table_rows[row][column] = value

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


@pytest.fixture
def constructor_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Load an isolated dialog using stateful controls and the real constructor."""
    package_name = "_impedance_dialog_constructor_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    errors: list[str] = []
    preview_calls: list[object] = []
    queued: list[tuple[Callable[..., object], tuple[object, ...]]] = []

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

    def show_error(message: str, *_arguments: object) -> None:
        """Capture validation failures without opening a native message box."""
        errors.append(message)

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
        Bitmap=lambda image: image,
        NullBitmap=object(),
        LogNull=nullcontext,
        Button=_ConstructedControl,
        Timer=_HarnessTimer,
        BoxSizer=_LayoutDouble,
        FlexGridSizer=_LayoutDouble,
        MessageBox=show_error,
        Colour=lambda *rgb: tuple(rgb),
        SystemSettings=SimpleNamespace(GetColour=lambda _kind: (120, 120, 120)),
        AutoBufferedPaintDC=PaintDC,
        Brush=lambda colour: colour,
        CallAfter=queue,
    )
    adv = FakeWxModule("wx.adv", HyperlinkCtrl=_HyperlinkControl)
    wx.adv = adv
    wx.__path__ = []
    _ConstructedTextControl.text_event = wx.EVT_TEXT
    monkeypatch.setattr(
        _ConstructedControl, "standard_button_ids", (wx.ID_OK, wx.ID_CANCEL)
    )
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, "wx", wx)
    monkeypatch.setitem(sys.modules, "wx.adv", adv)
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
        ) -> SpecificationDialog:
            """Construct the actual dialog with a stateful native-image decoder double."""
            result = dialog.SpecificationDialog(
                None, snapshot, specification, preview=render
            )
            result.on_screen = True
            drain()
            paint(result.preview_pane)
            return result

        yield SimpleNamespace(
            model=model,
            dialog=dialog,
            wx=wx,
            errors=errors,
            preview=preview,
            preview_calls=preview_calls,
            create_dialog=create_dialog,
            queued=queued,
            drain=drain,
            paint=paint,
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
def test_complete_200mm_routes_are_atomic_with_differential_pairs_together(
    review_api: SimpleNamespace, kind: str
) -> None:
    """Preserve entire long routes and one highlight for each native differential pair."""
    session = _long_route_session(review_api, kind=kind)
    rows = session.refresh().sections
    assert len(rows) == (1 if kind == "differential" else 2)
    assert {trace for row in rows for trace in row.traces} == set(
        session.snapshot.traces
    )
    assert all(row.bounds[2] - row.bounds[0] >= 200_000_000 for row in rows)
    if kind == "differential":
        assert set(rows[0].net_names) == {"CLK_P", "CLK_N"}
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
