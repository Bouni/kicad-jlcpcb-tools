"""Exercise the production-capture pane with stateful native-control doubles."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
import importlib
from io import BytesIO
from pathlib import Path
import struct
import textwrap
from types import ModuleType, SimpleNamespace
from typing import Any
import zlib

import pytest

from tests.wx_harness import FakeWxModule, temporary_modules


class Control:
    """Retain choice, bitmap, layout and event state through real constructors."""

    standard_button_ids: tuple[int, ...] = ()

    def __init__(self, parent: object = None, **kwargs: Any) -> None:
        self.parent = parent
        self.children: list[Control] = []
        if isinstance(parent, Control):
            parent.children.append(self)
        self.position = (0, 0)
        self.label = kwargs.get("label", "")
        self.title = kwargs.get("title", self.label)
        self.bitmap = kwargs.get("bitmap")
        self.items: list[str] = []
        self.selection = -1
        self.enabled = True
        self.bindings: dict[object, Callable[..., None]] = {}
        self.client_size = (420, 240)
        self.history: list[object] = []
        self.line_height = 18
        self.character_width = 8
        self.font_name = "native-default-18"
        self.value = kwargs.get("value", "")
        self.style = kwargs.get("style", 0)
        self.shown = self.alive = self.on_screen = True
        self.refreshes = 0
        self.drawn: list[tuple[Image, int, int]] = []
        self.annotations: list[tuple[str, object]] = []
        self.columns: list[tuple[str, int]] = []
        self.cells: dict[tuple[int, int], str] = {}
        self.colours: dict[int, object] = {}
        self.modal_shown = False
        self.cursor = None

    def __bool__(self) -> bool:
        """Match wx window lifetime checks."""
        return self.alive

    def Show(self, shown: bool = True) -> None:
        """Retain the visibility changed by the owner."""
        self.shown = shown

    def Hide(self) -> None:
        """Retain native hidden state when no issue navigation is needed."""
        self.Show(False)

    def IsShownOnScreen(self) -> bool:
        """Require the canvas and all owning panels to be visible."""
        if not self.alive or not self.shown or not self.on_screen:
            return False
        return not isinstance(self.parent, Control) or self.parent.IsShownOnScreen()

    def SetName(self, name: str) -> None:
        """Retain an accessible control name."""
        self.name = name

    def SetBackgroundStyle(self, style: object) -> None:
        """Retain the buffered-paint background mode."""
        self.background_style = style

    def GetBackgroundColour(self) -> tuple[int, int, int]:
        """Supply the panel background without emulating rendering."""
        return (20, 20, 20)

    def Refresh(self) -> None:
        """Request painting without delivering a synthetic paint event."""
        self.refreshes += 1

    def SetValue(self, value: str) -> None:
        """Retain text without synthesizing an input event."""
        self.value = value

    def GetValue(self) -> str:
        """Return the value actually assigned to the control."""
        return self.value

    def SetString(self, index: int, value: str) -> None:
        """Update one choice without changing the current selection."""
        self.items[index] = value

    def InsertColumn(self, index: int, label: str, width: int) -> None:
        """Retain native report-column definitions."""
        self.columns.insert(index, (label, width))

    def DeleteAllItems(self) -> None:
        """Clear report rows, colours, and native selection together."""
        self.items.clear()
        self.cells.clear()
        self.colours.clear()
        self.selection = -1

    def InsertItem(self, index: int, text: str) -> None:
        """Insert a report row at its displayed index."""
        self.items.insert(index, text)
        self.cells[index, 0] = text

    def SetItem(self, index: int, column: int, text: str) -> None:
        """Retain text for the requested report cell."""
        self.cells[index, column] = text

    def GetItemText(self, index: int, column: int = 0) -> str:
        """Expose text written into the native report-list cell."""
        return self.cells[index, column]

    def GetItemCount(self) -> int:
        """Report the number of real native list rows."""
        return len(self.items)

    def GetFirstSelected(self) -> int:
        """Return native list selection independently of row text."""
        return self.selection

    def SetItemTextColour(self, index: int, colour: object) -> None:
        """Make completed versus pending row colours observable."""
        self.colours[index] = colour

    def Select(self, index: int, on: bool = True) -> None:
        """Retain ListCtrl selection for the displayed workbook image."""
        self.selection = index if on else -1

    def Focus(self, index: int) -> None:
        """Retain the focused checklist row without claiming native focus."""
        self.focused_index = index

    def EnsureVisible(self, index: int) -> None:
        """Retain the row requested for native scrolling."""
        self.visible_index = index

    def SetMinSize(self, size: tuple[int, int]) -> None:
        """Retain the requested native minimum independently of client size."""
        self.minimum_size = size

    def SetCursor(self, cursor: object) -> None:
        """Retain the cursor assigned while a capture can be enlarged."""
        self.cursor = cursor

    def SetItems(self, items: list[str]) -> None:
        """Replace choices and clear native selection like wx.ItemContainer."""
        self.items = list(items)
        self.selection = -1

    def SetSelection(self, index: int) -> None:
        """Set selection without inventing a wx user event."""
        self.selection = index

    def GetSelection(self) -> int:
        """Return state established by the constructor or a user event."""
        return self.selection

    def Enable(self, enabled: bool) -> None:
        """Record whether a candidate can be selected."""
        self.enabled = enabled

    def Bind(
        self, event: object, handler: Callable[..., None], source: object = None
    ) -> None:
        """Retain the real event callback for later invocation."""
        self.bindings[event if source is None else (event, source)] = handler

    def SetLabel(self, label: str) -> None:
        """Replace the inline status text."""
        self.label = label

    def Wrap(self, width: int) -> None:
        """Retain wrapping for subsequent native-style best-size measurement."""
        self.wrap_width = width

    def GetBestSize(self) -> SimpleNamespace:
        """Measure wrapped content using explicit font metrics rather than min size."""
        columns = max(1, getattr(self, "wrap_width", 420) // self.character_width)
        lines = textwrap.wrap(self.label, columns, break_long_words=False) or [""]
        return SimpleNamespace(
            GetWidth=lambda: max(map(len, lines)) * self.character_width,
            GetHeight=lambda: len(lines) * self.line_height,
        )

    def GetFont(self) -> SimpleNamespace:
        """Expose a stable description that changes with simulated native metrics."""
        return SimpleNamespace(GetNativeFontInfoDesc=lambda: self.font_name)

    def InvalidateBestSize(self) -> None:
        """Record native metric invalidation after label and wrapping changes."""
        self.best_size_invalidated = True

    def SetSize(self, size: tuple[int, int]) -> None:
        """Retain the actual child bounds distinct from its viewport minimum."""
        self.size = size

    def SetVirtualSize(self, size: tuple[int, int]) -> None:
        """Retain full scrollable content even when the viewport is bounded."""
        self.virtual_size = size

    def SetScrollRate(self, horizontal: int, vertical: int) -> None:
        """Keep available scroll directions observable."""
        self.scroll_rate = (horizontal, vertical)

    def Scroll(self, horizontal: int, vertical: int) -> None:
        """Retain the current viewport origin for fresh versus resized errors."""
        self.scroll_position = (horizontal, vertical)

    def SetBitmap(self, bitmap: object) -> None:
        """Change the actual displayed bitmap and retain operation order."""
        self.bitmap = bitmap
        self.history.append(bitmap)

    def SetSizer(self, sizer: object) -> None:
        """Retain the native layout object."""
        self.sizer = sizer

    def GetClientSize(self) -> SimpleNamespace:
        """Read the current viewport, including temporarily hidden panels."""
        width, height = self.client_size
        return SimpleNamespace(GetWidth=lambda: width, GetHeight=lambda: height)

    def Layout(self) -> None:
        """Keep layout observable without generating synthetic resize events."""
        self.laid_out = True

    def GetSize(self) -> SimpleNamespace:
        """Measure the fixture's explicitly modeled image and native chrome."""
        width, height = (
            self.canvas.minimum_size if hasattr(self, "canvas") else self.client_size
        )
        return SimpleNamespace(
            GetWidth=lambda: width + 20, GetHeight=lambda: height + 180
        )

    def SetPosition(self, position: tuple[int, int]) -> None:
        """Retain the fitted top-level position within the modeled display."""
        self.position = position

    def GetParent(self) -> object:
        """Expose actual ownership for native-style clipping checks."""
        return self.parent

    def FindWindow(self, control_id: int) -> object:
        """Return a real standard-button descendant rather than manufacturing one."""
        return next(
            (
                child
                for child in self.children
                if getattr(child, "window_id", None) == control_id
            ),
            None,
        )

    def CreateButtonSizer(self, _flags: int) -> Sizer:
        """Create stateful standard buttons for the actual modal control checks."""
        layout = Sizer(0)
        for control_id in self.standard_button_ids:
            child = Control(self)
            child.window_id = control_id
            layout.Add(child)
        return layout

    def SetSizerAndFit(self, sizer: object) -> None:
        """Retain the enlarge-dialog layout tree."""
        self.sizer = sizer

    def ShowModal(self) -> int:
        """Record that the enlarge dialog became modal."""
        self.modal_shown = True
        return getattr(self, "modal_result", 5100)

    def EndModal(self, result: int) -> None:
        """Retain actual modal acceptance and remove its visible screen state."""
        self.modal_result = result
        self.on_screen = False

    def Destroy(self) -> None:
        """Mark the enlarge dialog destroyed after ShowModal returns."""
        self.alive = False


class Sizer:
    """Retain child layout without approximating native size calculations."""

    def __init__(self, orientation: int) -> None:
        self.orientation = orientation
        self.items: list[object] = []

    def Add(self, child: object, *args: object) -> None:
        """Record child insertion and its layout constraints."""
        self.items.append((child, args))

    def AddStretchSpacer(self) -> None:
        """Record an expanding spacer."""
        self.items.append("stretch")


class Image:
    """Keep source image size stable when a scaled copy is requested."""

    def __init__(self, size: tuple[int, int], valid: bool = True) -> None:
        self.size = size
        self.valid = valid

    def IsOk(self) -> bool:
        """Report the PNG decoder's success state."""
        return self.valid

    def GetWidth(self) -> int:
        """Return source width in pixels."""
        return self.size[0]

    def GetHeight(self) -> int:
        """Return source height in pixels."""
        return self.size[1]

    def Scale(self, width: int, height: int, quality: object) -> Image:
        """Return a distinct resized image as wx.Image.Scale does."""
        return Image((width, height), self.valid)

    def GetSubImage(self, rectangle: tuple[int, int, int, int]) -> Image:
        """Return a cropped copy while retaining the source pixels' dimensions."""
        return Image(rectangle[2:], self.valid)


class PaintDC:
    """Retain actual draw calls; a Refresh request alone is not a display."""

    def __init__(self, canvas: Control) -> None:
        self.canvas = canvas

    def SetBackground(self, brush: object) -> None:
        """Retain the brush used for this paint."""
        self.brush = brush

    def Clear(self) -> None:
        """Remove draws from the previous paint."""
        self.canvas.drawn.clear()
        self.canvas.annotations.clear()

    def DrawBitmap(self, bitmap: Image, x: int, y: int, transparent: bool) -> None:
        """Record a valid bitmap drawn by the real paint handler."""
        assert bitmap.IsOk()
        self.canvas.drawn.append((bitmap, x, y))

    def SetBrush(self, brush: object) -> None:
        """Retain the actual brush used to fill label and outline shapes."""
        self.brush = brush

    def SetPen(self, pen: object) -> None:
        """Retain widths and colours to check selected issue emphasis."""
        self.pen = pen

    def DrawRectangle(self, *rectangle: int) -> None:
        """Retain rectangles positioned by the real bitmap transform."""
        self.canvas.annotations.append(("rectangle", (rectangle, self.pen)))

    def GetTextExtent(self, text: str) -> SimpleNamespace:
        """Expose deterministic text metrics separately from overlay geometry."""
        return SimpleNamespace(GetWidth=lambda: 8 * len(text), GetHeight=lambda: 16)

    def SetTextForeground(self, colour: object) -> None:
        """Retain label foreground for high contrast drawing."""
        self.foreground = colour

    def DrawText(self, text: str, x: int, y: int) -> None:
        """Retain the numbered marker drawn at its issue rectangle."""
        self.canvas.annotations.append(("text", (text, x, y)))


def _png(
    colour: tuple[int, int, int] = (0, 0, 0),
    size: tuple[int, int] = (800, 420),
) -> bytes:
    """Provide real PNG bytes while the native decoder is explicitly doubled."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", *size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + bytes(colour) * size[0]) * size[1]))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def pane_api() -> Iterator[SimpleNamespace]:
    """Import real constructors with queued callbacks and explicitly painted wx."""
    from tests.test_impedance_dialog import _HarnessTimer

    package_name = "_impedance_workbook_preview_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    state = SimpleNamespace(
        loaded=[], logging_suspended=False, log_scopes=[], image=Image((800, 420))
    )
    queued: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    @contextmanager
    def suspend_logging() -> Iterator[None]:
        previous = state.logging_suspended
        state.log_scopes.append("enter")
        state.logging_suspended = True
        try:
            yield
        finally:
            state.logging_suspended = previous
            state.log_scopes.append("exit")

    def load_image(source: BytesIO, kind: object) -> Image:
        state.loaded.append((source.getvalue(), kind))
        return state.image

    def call_after(callback: Callable[..., object], *args: object) -> None:
        queued.append((callback, args))

    def drain() -> None:
        while queued:
            callback, args = queued.pop(0)
            callback(*args)

    wx = FakeWxModule(
        "wx",
        Panel=Control,
        Dialog=Control,
        Choice=Control,
        Button=Control,
        StaticText=Control,
        TextCtrl=Control,
        ListCtrl=Control,
        BoxSizer=Sizer,
        Timer=_HarnessTimer,
        CallAfter=call_after,
        AutoBufferedPaintDC=PaintDC,
        Brush=lambda colour: colour,
        Pen=lambda colour, width: (colour, width),
        Rect=lambda *rectangle: rectangle,
        Colour=lambda *channels: tuple(channels),
        SystemSettings=SimpleNamespace(GetColour=lambda _kind: (120, 120, 120)),
        Image=load_image,
        LogNull=suspend_logging,
        Bitmap=lambda image: image,
        NullCursor=object(),
        Display=type(
            "Display",
            (),
            {
                "GetFromWindow": staticmethod(lambda _window: 0),
                "__init__": lambda self, _index: None,
                "GetClientArea": lambda self: SimpleNamespace(
                    x=0,
                    y=0,
                    width=1600,
                    height=1000,
                    GetWidth=lambda: 1600,
                    GetHeight=lambda: 1000,
                ),
            },
        ),
    )
    Control.standard_button_ids = (wx.ID_OK, wx.ID_CANCEL)
    with temporary_modules(
        {package_name: package, "wx": wx}, namespaces=(package_name,)
    ):
        module = importlib.import_module(f"{package_name}.dialog_preview")
        model = importlib.import_module(f"{package_name}.model")
        service = importlib.import_module(f"{package_name}.service")
        section = model.Section(
            "row-a", "spec", "In2.Cu", 175_000, (), (0, 0, 2_000_000, 0), ("USB_P",)
        )
        yield SimpleNamespace(
            module=module,
            wx=wx,
            state=state,
            section=section,
            capture=service.CapturedImage.from_bytes(_png()),
            drain=drain,
            queued=queued,
        )


def choose(api: SimpleNamespace, pane: Any, index: int) -> None:
    """Deliver a native choice event; capture and paint remain separate."""
    pane.checklist.Select(index)
    pane.checklist.bindings[api.wx.EVT_LIST_ITEM_SELECTED](
        SimpleNamespace(GetIndex=lambda: index, Skip=lambda: None)
    )


def paint(api: SimpleNamespace, pane: Any, *, drain: bool = True) -> None:
    """Only an explicit visible paint can produce image-view evidence."""
    pane.canvas.bindings[api.wx.EVT_PAINT](SimpleNamespace())
    if drain:
        api.drain()


def show(api: SimpleNamespace, pane: Any) -> None:
    """Run the queued render, then dispatch the actual display callback."""
    api.drain()
    paint(api, pane)


def resize(api: SimpleNamespace, pane: Any, size: tuple[int, int]) -> list[bool]:
    """Let native resize request a later paint, not fabricate one."""
    pane.canvas.client_size = size
    skipped: list[bool] = []
    pane.canvas.bindings[api.wx.EVT_SIZE](
        SimpleNamespace(Skip=lambda: skipped.append(True))
    )
    return skipped


def open_pane(api: SimpleNamespace, **kwargs: Any) -> Any:
    """Construct a pane whose production provider returns immutable bytes."""
    return api.module.WorkbookPreview(
        Control(),
        lambda section, **options: api.capture,
        image_list=Control(),
        **kwargs,
    )


def test_constructor_queues_exact_capture_but_requires_visible_paint(
    pane_api: SimpleNamespace,
) -> None:
    """Loading or queuing a PNG does not establish that the user saw it."""
    api = pane_api
    calls: list[object] = []
    views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda section, **options: calls.append(section) or api.capture,
        image_viewed=lambda *args: views.append(args),
        image_list=Control(),
    )
    assert pane.canvas.minimum_size == (420, 180)
    pane.set_sections((api.section,), ("board-a", "scan-1"))
    assert calls == [] and not pane.ready
    api.drain()
    assert calls == [api.section]
    assert api.state.loaded == [(api.capture.data, api.wx.BITMAP_TYPE_PNG)]
    assert pane.checklist.GetFirstSelected() == 0 and pane.checklist.enabled
    assert all(
        value in pane.checklist.GetItemText(0, 1)
        for value in ("1", "In2.Cu", "0.175", "USB_P")
    )
    assert "needs review" in pane.checklist.GetItemText(0, 1)
    assert views == [] and not pane.ready
    with pytest.raises(ValueError, match="Images still needing review: Image 1"):
        pane.ensure_reviewed()
    paint(api, pane, drain=False)
    assert views == [] and not pane.ready
    api.drain()
    assert pane.ready and len(views) == 1
    assert views[0][0:2] == (api.section, api.capture.sha256)
    assert pane.canvas.drawn[0][0].size == (420, 220)
    assert api.state.image.size == (800, 420)
    assert pane.checklist.cells[0, 0] == "☑"
    assert pane.checklist.colours[0] == (70, 210, 110)
    assert "last viewed" not in pane.checklist.cells[0, 1].lower()
    assert pane.reviewed_capture_hashes() == ((api.section, api.capture.sha256),)


def test_covered_stub_preview_entries_are_labeled_and_stay_muted(
    pane_api: SimpleNamespace,
) -> None:
    """Covered stubs read as Covered stub and stay muted after viewing."""
    api = pane_api
    model = importlib.import_module(api.module.__package__ + ".model")
    checks = importlib.import_module(api.module.__package__ + ".width_checks")
    stub = replace(
        api.section,
        section_id="stub-a",
        exclusion_reason=model.EXCLUSION_COVERED_STUB,
        net_names=("USB_STUB",),
        width_nm=100_000,
    )
    pane = open_pane(api)
    pane.set_sections((api.section, stub), "board-a")
    assert "0.175 mm" in pane.checklist.GetItemText(0, 1)
    assert "Covered stub" not in pane.checklist.GetItemText(0, 1)
    assert "Covered stub" in pane.checklist.GetItemText(1, 1)
    assert "0.1 mm" not in pane.checklist.GetItemText(1, 1)
    assert "Covered stub" in pane.checklist.cells[1, 1]
    assert pane.checklist.colours[1] == checks.COVERED_STUB_COLOUR
    choose(api, pane, 1)
    show(api, pane)
    assert "viewed" in pane.checklist.GetItemText(1, 1)
    assert "Covered stub" in pane.checklist.GetItemText(1, 1)
    assert pane.checklist.colours[1] == checks.COVERED_STUB_COLOUR
    assert pane.checklist.colours[0] == api.wx.SystemSettings.GetColour(
        api.wx.SYS_COLOUR_GRAYTEXT
    )


def test_skip_stub_review_ignores_unviewed_stubs_by_default(
    pane_api: SimpleNamespace,
) -> None:
    """Default skip leaves stubs listed but not required for layer approval."""
    api = pane_api
    model = importlib.import_module(api.module.__package__ + ".model")
    stub = replace(
        api.section,
        section_id="stub-a",
        exclusion_reason=model.EXCLUSION_COVERED_STUB,
        net_names=("USB_STUB",),
    )
    exportable = replace(api.section, section_id="row-b", layer="B.Cu")
    pane = api.module.WorkbookPreview(
        Control(),
        lambda section, **options: api.capture,
        skip_stub_review=lambda: True,
        image_list=Control(),
    )
    pane.set_sections((stub, exportable), "board-a")
    assert pane.checklist.GetFirstSelected() == 1
    show(api, pane)
    assert pane.advance_to_unreviewed() is False
    pane.ensure_reviewed()
    assert pane.reviewed_capture_hashes() == ((exportable, api.capture.sha256),)


def test_skip_stub_review_off_requires_viewing_stubs(
    pane_api: SimpleNamespace,
) -> None:
    """Disabling skip stops on covered stubs until they are viewed."""
    api = pane_api
    model = importlib.import_module(api.module.__package__ + ".model")
    stub = replace(
        api.section,
        section_id="stub-a",
        exclusion_reason=model.EXCLUSION_COVERED_STUB,
        net_names=("USB_STUB",),
    )
    exportable = replace(api.section, section_id="row-b", layer="B.Cu")
    pane = api.module.WorkbookPreview(
        Control(),
        lambda section, **options: api.capture,
        skip_stub_review=lambda: False,
        image_list=Control(),
    )
    pane.set_sections((exportable, stub), "board-a")
    show(api, pane)
    assert pane.advance_to_unreviewed() is True
    assert pane.checklist.GetFirstSelected() == 1
    with pytest.raises(ValueError, match="Images still needing review: Image 2"):
        pane.ensure_reviewed()
    show(api, pane)
    assert pane.advance_to_unreviewed() is False
    pane.ensure_reviewed()


def test_every_row_requires_display_and_approve_advances_to_next(
    pane_api: SimpleNamespace,
) -> None:
    """Approval advances through pending captures without silently approving them."""
    api = pane_api
    pane = open_pane(api)
    second = replace(api.section, section_id="row-b", layer="B.Cu")
    pane.set_sections((api.section, second), "board-a")
    show(api, pane)
    assert pane.checklist.cells[1, 0] == "—"
    assert pane.checklist.colours[1] == (120, 120, 120)
    with pytest.raises(ValueError, match="Images still needing review: Image 2"):
        pane.ensure_reviewed()
    assert pane.advance_to_unreviewed() is True
    assert pane.checklist.GetFirstSelected() == 1 and not pane.ready
    with pytest.raises(ValueError):
        pane.ensure_reviewed()
    show(api, pane)
    assert pane.advance_to_unreviewed() is False
    pane.ensure_reviewed()
    assert all(
        "— viewed" in label
        for label in [
            pane.checklist.GetItemText(index, 1)
            for index in range(pane.checklist.GetItemCount())
        ]
    )


@pytest.mark.parametrize("change", ["context", "section"])
def test_changed_inputs_clear_old_pixels_and_require_new_display(
    pane_api: SimpleNamespace, change: str
) -> None:
    """Settings and board revisions cannot inherit an old capture's approval."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    pane.ensure_reviewed()
    section = (
        replace(api.section, width_nm=200_000) if change == "section" else api.section
    )
    pane.set_sections((section,), "board-b" if change == "context" else "board-a")
    assert pane.canvas._image is None and not pane.ready
    with pytest.raises(ValueError):
        pane.ensure_reviewed()
    api.drain()
    assert not pane.ready
    paint(api, pane)
    pane.ensure_reviewed()


@pytest.mark.parametrize(
    "failure", ["callback", "invalid", "zero-width", "zero-height", "decode"]
)
def test_failed_repreview_clears_stale_pixels_and_explicit_retry_recaptures(
    pane_api: SimpleNamespace, failure: str
) -> None:
    """Every capture or decode failure stays inline and supports a forced retry."""
    api = pane_api
    failed = False
    refreshes: list[bool] = []
    decoder = api.wx.Image

    def preview(section: object, *, refresh: bool = False) -> Any:
        refreshes.append(refresh)
        if failed and failure == "callback":
            raise RuntimeError("Native rendering failed")
        return api.capture

    pane = api.module.WorkbookPreview(Control(), preview, image_list=Control())
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    pane.ensure_reviewed()
    api.state.image = Image(
        (0 if failure == "zero-width" else 800, 0 if failure == "zero-height" else 420),
        valid=failure != "invalid",
    )
    if failure == "decode":

        def decode(source: BytesIO, kind: object) -> Image:
            raise RuntimeError("PNG decoder failure")

        api.wx.Image = decode
    failed = True
    pane.select(pane.checklist.GetFirstSelected(), refresh=True)
    assert not pane.ready
    show(api, pane)
    assert pane.canvas._image is None
    assert "unavailable" in pane.status.GetValue().lower()
    with pytest.raises(ValueError, match="(?i)preview"):
        pane.ensure_reviewed()
    failed = False
    api.wx.Image = decoder
    api.state.image = Image((800, 420))
    pane.select(pane.checklist.GetFirstSelected(), refresh=True)
    show(api, pane)
    pane.ensure_reviewed()
    assert refreshes == [False, True, True]


@pytest.mark.parametrize("empty", [False, True])
def test_clear_and_empty_results_remove_rows_pixels_and_approval(
    pane_api: SimpleNamespace, empty: bool
) -> None:
    """Missing matches or invalid inputs cannot leave the old row on screen."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    if empty:
        pane.set_sections((), "board-b")
    else:
        pane.clear("Choose a net class.")
        assert pane.status.GetValue() == "Choose a net class."
    assert pane.canvas._image is None and not pane.ready
    assert [
        pane.checklist.GetItemText(index, 1)
        for index in range(pane.checklist.GetItemCount())
    ] == [] and not pane.checklist.enabled
    assert not pane._retry_timer.IsRunning()
    with pytest.raises(ValueError):
        pane.ensure_reviewed()


def test_offline_constructor_cannot_approve_without_production_renderer(
    pane_api: SimpleNamespace,
) -> None:
    """A useful offline dialog must still fail closed at the approval boundary."""
    api = pane_api
    pane = api.module.WorkbookPreview(Control(), None)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    assert pane.canvas._image is None and not pane._retry_timer.IsRunning()
    assert "unavailable" in pane.status.GetValue().lower()
    with pytest.raises(ValueError, match="(?i)preview"):
        pane.ensure_reviewed()


@pytest.mark.parametrize(
    "size,expected",
    [((200, 100), (190, 100)), ((1600, 1000), (800, 420)), ((100, 800), (100, 52))],
)
def test_resize_fits_whole_capture_without_upscaling_or_changing_layout(
    pane_api: SimpleNamespace, size: tuple[int, int], expected: tuple[int, int]
) -> None:
    """Narrow and wide panes preserve the whole image within fixed layout bounds."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    assert resize(api, pane, size) == [True]
    paint(api, pane)
    bitmap, x, y = pane.canvas.drawn[0]
    assert bitmap.size == expected
    assert (x, y) == ((size[0] - expected[0]) // 2, (size[1] - expected[1]) // 2)
    assert api.state.image.size == (800, 420)
    assert not getattr(pane.canvas, "laid_out", False)
    pane.ensure_reviewed()


@pytest.mark.parametrize("unavailable", ["zero", "hidden"])
def test_nonvisible_image_cannot_be_approved_before_later_visible_paint(
    pane_api: SimpleNamespace, unavailable: str
) -> None:
    """Hidden or zero-sized panels do not count as viewing an image."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    api.drain()
    if unavailable == "zero":
        pane.canvas.client_size = (0, 0)
    else:
        pane.canvas.Show(False)
    paint(api, pane)
    assert not pane.ready
    with pytest.raises(ValueError):
        pane.ensure_reviewed()
    pane.canvas.Show()
    resize(api, pane, (420, 240))
    paint(api, pane)
    pane.ensure_reviewed()


def test_callback_reentrancy_cannot_resurrect_invalidated_capture(
    pane_api: SimpleNamespace,
) -> None:
    """A callback yielding to changed settings cannot restore obsolete pixels."""
    api = pane_api

    def preview(section: object, **options: object) -> Any:
        pane.clear("Settings changed; refresh the preview.")
        return api.capture

    pane = api.module.WorkbookPreview(Control(), preview, image_list=Control())
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    assert pane.canvas._image is None
    assert pane.status.GetValue() == "Settings changed; refresh the preview."
    with pytest.raises(ValueError):
        pane.ensure_reviewed()


def test_same_inputs_preserve_selected_row_without_recapture_or_restamping(
    pane_api: SimpleNamespace,
) -> None:
    """Unrelated UI updates preserve the current image and its review timestamp."""
    api = pane_api
    captures: list[object] = []
    views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda section, **options: captures.append(section) or api.capture,
        image_viewed=lambda *args: views.append(args),
        image_list=Control(),
    )
    second = replace(api.section, section_id="row-b")
    sections = (api.section, second)
    pane.set_sections(sections, "board-a")
    show(api, pane)
    choose(api, pane, 1)
    show(api, pane)
    bitmap = pane.canvas._bitmap
    pane.set_sections(sections, "board-a")
    show(api, pane)
    assert captures == list(sections) and len(views) == 2
    assert pane.checklist.GetFirstSelected() == 1 and pane.canvas._bitmap is bitmap
    pane.ensure_reviewed()


def test_reordering_rows_does_not_transfer_review_to_unseen_section(
    pane_api: SimpleNamespace,
) -> None:
    """Review identity follows the section, not the displayed row number."""
    api = pane_api
    pane = open_pane(api)
    second = replace(api.section, section_id="row-b")
    unseen = replace(api.section, section_id="unseen", net_names=("OTHER_P", "OTHER_N"))
    pane.set_sections((api.section, second), "board-a")
    show(api, pane)
    choose(api, pane, 1)
    show(api, pane)
    pane.ensure_reviewed()
    pane.set_sections((second, unseen, api.section), "board-a")
    show(api, pane)
    assert "needs review" in pane.checklist.GetItemText(1, 1)
    with pytest.raises(ValueError, match="Images still needing review: Image 2"):
        pane.ensure_reviewed()


@pytest.mark.parametrize(
    "failure", ["scale", "invalid-scale", "bitmap", "invalid-bitmap", "draw"]
)
def test_native_display_failure_does_not_mark_loaded_capture_viewed(
    pane_api: SimpleNamespace, failure: str
) -> None:
    """Successful decoding alone is not proof that native drawing succeeded."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    api.drain()

    def fail(*args: object) -> Any:
        raise RuntimeError("Native display failure")

    if failure == "scale":
        api.state.image.Scale = fail
    elif failure == "invalid-scale":
        api.state.image.Scale = lambda *args: Image((0, 0), valid=False)
    elif failure == "bitmap":
        api.wx.Bitmap = fail
    elif failure == "invalid-bitmap":
        api.wx.Bitmap = lambda image: Image((0, 0), valid=False)
    else:

        class FailedDC(PaintDC):
            def DrawBitmap(self, *args: object) -> None:
                """Record a valid bitmap drawn by the real paint handler."""
                fail()

        api.wx.AutoBufferedPaintDC = FailedDC
    paint(api, pane)
    assert pane.canvas._image is None and not pane.ready
    with pytest.raises(ValueError, match="(?i)preview unavailable"):
        pane.ensure_reviewed()


def test_failed_resize_revokes_review_but_allows_explicit_retry(
    pane_api: SimpleNamespace,
) -> None:
    """A failed native redraw cannot retain approval or poison a later retry."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    pane.ensure_reviewed()
    original = api.wx.Bitmap
    api.wx.Bitmap = lambda image: Image((0, 0), valid=False)
    resize(api, pane, (210, 120))
    paint(api, pane)
    assert pane.canvas._image is None and not pane.ready
    with pytest.raises(ValueError):
        pane.ensure_reviewed()
    api.wx.Bitmap = original
    pane.select(pane.checklist.GetFirstSelected(), refresh=True)
    show(api, pane)
    assert pane.canvas.drawn[0][0].size == (210, 110)
    pane.ensure_reviewed()


def test_failed_later_row_remains_unreviewed_after_returning_to_good_row(
    pane_api: SimpleNamespace,
) -> None:
    """A good sibling image cannot hide an independently failed workbook row."""
    api = pane_api
    failed = False

    def preview(section: Any, **options: object) -> Any:
        if failed and section.section_id == "row-b":
            raise RuntimeError("Second row capture failed")
        return api.capture

    pane = api.module.WorkbookPreview(Control(), preview, image_list=Control())
    second = replace(api.section, section_id="row-b")
    pane.set_sections((api.section, second), "board-a")
    show(api, pane)
    choose(api, pane, 1)
    show(api, pane)
    pane.ensure_reviewed()
    failed = True
    pane.select(pane.checklist.GetFirstSelected(), refresh=True)
    show(api, pane)
    choose(api, pane, 0)
    show(api, pane)
    with pytest.raises(ValueError, match="Images still needing review: Image 2"):
        pane.ensure_reviewed()


def test_layer_navigation_uses_capture_provider_and_review_keys_not_row_indices(
    pane_api: SimpleNamespace,
) -> None:
    """Layer changes preserve only evidence for the exact section and context."""
    api = pane_api
    captures: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda section, **options: captures.append(section) or api.capture,
        image_list=Control(),
    )
    second = replace(api.section, section_id="row-b")
    pane.set_sections((api.section, second), ("board-a", "In2.Cu"))
    show(api, pane)
    choose(api, pane, 1)
    show(api, pane)
    pane.set_sections((second,), ("board-a", "B.Cu"))
    show(api, pane)
    pane.set_sections((api.section, second), ("board-a", "In2.Cu"))
    api.drain()
    # Returning uses the provider (whose revision cache belongs outside this pane).
    assert captures == [api.section, second, second, api.section]
    with pytest.raises(ValueError, match="Images still needing review: Image 1"):
        pane.ensure_reviewed()
    paint(api, pane)
    pane.ensure_reviewed()


@pytest.mark.parametrize("result", ["success", "invalid", "exception"])
def test_decoder_logs_are_suppressed_without_suppressing_renderer_logs(
    pane_api: SimpleNamespace, result: str
) -> None:
    """PNG errors stay inline without silencing unrelated native logging."""
    api = pane_api
    decoder_states: list[bool] = []

    def preview(section: object, **options: object) -> Any:
        assert not api.state.logging_suspended
        return api.capture

    def decode(source: BytesIO, kind: object) -> Image:
        decoder_states.append(api.state.logging_suspended)
        if result == "exception":
            raise RuntimeError("PNG decode exception")
        return Image((800, 420), valid=result == "success")

    api.wx.Image = decode
    pane = api.module.WorkbookPreview(Control(), preview)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    assert decoder_states == [True]
    assert api.state.log_scopes == ["enter", "exit"]
    assert not api.state.logging_suspended
    if result == "success":
        pane.ensure_reviewed()
    else:
        assert pane.canvas._image is None
        with pytest.raises(ValueError, match="(?i)preview unavailable"):
            pane.ensure_reviewed()


@pytest.mark.parametrize(
    "changed_during", ["before-render", "render", "decode", "paint"]
)
def test_source_mutation_before_actual_display_never_earns_view_evidence(
    pane_api: SimpleNamespace, changed_during: str
) -> None:
    """Changed source data cannot reach approval through any capture boundary."""
    api = pane_api
    current = changed_during != "before-render"
    views: list[object] = []
    captures: list[object] = []

    def verify() -> None:
        if not current:
            raise ValueError("Board contents changed.")

    def preview(section: object, **options: object) -> Any:
        nonlocal current
        captures.append(section)
        if changed_during == "render":
            current = False
        return api.capture

    def decode(source: BytesIO, kind: object) -> Image:
        nonlocal current
        if changed_during == "decode":
            current = False
        return Image((800, 420))

    api.wx.Image = decode
    pane = api.module.WorkbookPreview(
        Control(),
        preview,
        verify_current=verify,
        image_viewed=lambda *args: views.append(args),
    )
    pane.set_sections((api.section,), "board-a")
    api.drain()
    if changed_during == "paint":
        current = False
    paint(api, pane)
    # The revision-owned provider guards capture; the pane must reject stale
    # source state before assigning any review evidence, not duplicate its scan.
    assert captures == [api.section]
    assert views == [] and not pane.ready and pane.canvas._image is None
    with pytest.raises(ValueError, match="changed"):
        pane.ensure_reviewed()


def test_approval_rechecks_source_even_after_successful_display(
    pane_api: SimpleNamespace,
) -> None:
    """A previously viewed image does not excuse a later board change."""
    api = pane_api
    current = True

    def verify() -> None:
        if not current:
            raise ValueError("Board contents changed.")

    pane = open_pane(api, verify_current=verify)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    pane.ensure_reviewed()
    current = False
    with pytest.raises(ValueError, match="changed"):
        pane.ensure_reviewed()


def test_repaints_and_repeated_resize_do_not_recapture_rescan_or_restamp(
    pane_api: SimpleNamespace,
) -> None:
    """Paint and size events operate only on already captured pixels."""
    api = pane_api
    checks: list[bool] = []
    captures: list[object] = []
    views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda section, **options: captures.append(section) or api.capture,
        verify_current=lambda: checks.append(True),
        image_viewed=lambda *args: views.append(args),
    )
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    checks.clear()
    bitmap = pane.canvas._bitmap
    for _ in range(5):
        resize(api, pane, (420, 240))
        paint(api, pane)
    assert pane.canvas._bitmap is bitmap
    assert checks == [] and captures == [api.section] and len(views) == 1
    resize(api, pane, (210, 120))
    paint(api, pane)
    assert pane.canvas.drawn[0][0].size == (210, 110)
    assert checks == [] and len(views) == 1


def _spacing_entry(api: SimpleNamespace, view_id: str = "spacing-a") -> Any:
    """Build a real diagnostic record without manufacturing section coercion."""
    spacing = importlib.import_module(api.module.__package__ + ".spacing_review")
    composition = importlib.import_module(
        api.module.__package__ + ".spacing_composition"
    )
    detail_width, detail_height = composition.DETAIL_RECT[2:]
    return spacing.SpacingReviewEntry(
        api.section.spec_id,
        api.section.layer,
        "issue-a",
        view_id,
        (api.section.section_id,),
        ("USB_P", "USB_N"),
        "too_wide",
        "Spacing concern 1: routed gap 0.25 mm; declared gap 0.15 mm.",
        (100, 80, 200, 120),
        (0.0, 0.0, 800.0, 800.0 * detail_height / detail_width),
        "a" * 64,
        (),
        (),
    )


def test_spacing_images_are_separate_entries_with_external_captions(
    pane_api: SimpleNamespace,
) -> None:
    """A diagnostic is reviewed independently and never obscures workbook copper."""
    api = pane_api
    captures: list[object] = []
    workbook_views: list[object] = []
    spacing_views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda entry, **_options: captures.append(entry) or api.capture,
        image_viewed=lambda *args: workbook_views.append(args),
        spacing_image_viewed=lambda *args: spacing_views.append(args),
        image_list=Control(),
        approved_images=lambda: set(),
    )
    entry = _spacing_entry(api)
    pane.set_sections((api.section,), "board-a")
    pane.set_spacing_entries((entry,))
    spacing_index = pane.entries.index(entry)
    assert pane.sections == (api.section,)
    assert pane.entries == (entry, api.section)
    assert pane.spacing_entries == (entry,)
    assert "Spacing image 1" in pane.checklist.GetItemText(spacing_index, 1)
    show(api, pane)
    assert len(workbook_views) == 1 and spacing_views == []
    assert pane.canvas.annotations == []
    choose(api, pane, spacing_index)
    assert pane.selected_entry is entry
    assert pane.selected_section is api.section
    assert pane.caption.label == entry.caption and pane.caption.shown
    assert pane.title.label == "Spacing review image"
    assert not pane.ready and not pane.displayed
    show(api, pane)
    assert spacing_views == []
    assert len(workbook_views) == 1
    assert pane.canvas.annotations == []
    assert "needs review" in pane.checklist.GetItemText(spacing_index, 1)
    assert "approved" not in pane.checklist.GetItemText(spacing_index, 1)
    assert captures == [api.section, entry]
    evidence = dict(pane._viewed)
    resize(api, pane, (800, 600))
    paint(api, pane)
    assert pane._viewed == evidence and spacing_views == []
    with pytest.raises(ValueError, match="Images still needing review"):
        pane.reviewed_capture_hashes()


def test_enlarged_capture_refits_complete_png_without_modifying_pixels(
    pane_api: SimpleNamespace,
) -> None:
    """The actual inspection window refits the complete immutable capture."""
    api = pane_api
    capture = replace(api.capture)
    dialog = api.module.EnlargedCaptureDialog(Control(), capture, "Workbook image")
    assert {control.label for control in dialog.children}.isdisjoint(
        {"Fit image", "Original image", "Fit issue", "Fit route"}
    )
    for size, dimensions in (((1200, 700), (1200, 630)), ((400, 300), (400, 210))):
        resize(api, dialog, size)
        paint(api, dialog)
        assert dialog.canvas._image.size == (capture.width, capture.height)
        assert dialog.canvas.drawn[0][0].size == dimensions
        assert dialog.canvas._visible_source_rect == (
            0,
            0,
            capture.width,
            capture.height,
        )
        assert dialog.canvas._painted_size == size
        assert dialog.canvas.annotations == []
        assert capture.data == api.capture.data and capture.sha256 == api.capture.sha256


def test_replaced_spacing_entry_rejects_queued_old_image_display(
    pane_api: SimpleNamespace,
) -> None:
    """A deferred paint cannot stamp a concern replaced by a changed target."""
    api = pane_api
    views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda *_args, **_kwargs: api.capture,
        spacing_image_viewed=lambda *args: views.append(args),
    )
    first = _spacing_entry(api)
    second = replace(first, view_id="spacing-b", input_digest="b" * 64)
    pane.set_sections((api.section,), "board-a")
    pane.set_spacing_entries((first,))
    pane.select(pane.entries.index(first))
    api.drain()
    paint(api, pane, drain=False)
    pane.set_spacing_entries((second,))
    assert pane._capture is None and not pane.ready
    api.drain()
    assert views == []
    pane.select(pane.entries.index(second))
    show(api, pane)
    assert views == []
    assert pane.selected_entry == second and pane.ready
    assert pane.canvas.annotations == []


def test_selection_error_is_literal_complete_readonly_multiline_status(
    pane_api: SimpleNamespace,
) -> None:
    """Long recovery text is retained in a native scrolling text control."""
    api = pane_api
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    message = "Default contains an unpaired CONTEXT_1 net. " + "LONG_NAME" * 100
    pane.clear(message, error=True)
    assert pane.status.GetValue() == message
    assert pane.status.style & api.wx.TE_MULTILINE
    assert pane.status.style & api.wx.TE_READONLY
    assert not pane._retry_timer.IsRunning() and pane.canvas._image is None
    with pytest.raises(ValueError, match="unpaired CONTEXT_1"):
        pane.ensure_reviewed()
    pane.set_sections((api.section,), "board-b")
    show(api, pane)
    pane.ensure_reviewed()


def test_click_with_loaded_capture_opens_enlarge_dialog(
    pane_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thumbnail click opens a large modal of the same capture for inspection."""
    api = pane_api
    shown: list[Control] = []

    def track_modal(self: Control) -> int:
        shown.append(self)
        self.modal_shown = True
        return api.wx.ID_OK

    monkeypatch.setattr(Control, "ShowModal", track_modal)
    pane = open_pane(api)
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    assert pane.canvas._image is not None
    skipped: list[bool] = []
    pane.canvas.bindings[api.wx.EVT_LEFT_UP](
        SimpleNamespace(Skip=lambda: skipped.append(True))
    )
    assert skipped == [True]
    assert len(shown) == 1
    assert "In2.Cu" in shown[0].title
    assert "USB_P" in shown[0].title
    assert shown[0].alive is False


def test_click_without_image_does_not_open_enlarge_dialog(
    pane_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicks on an empty preview are ignored."""
    api = pane_api
    shown: list[Control] = []
    monkeypatch.setattr(
        Control,
        "ShowModal",
        lambda self: shown.append(self) or api.wx.ID_OK,
    )
    pane = open_pane(api)
    assert pane.canvas._image is None
    pane.canvas.bindings[api.wx.EVT_LEFT_UP](SimpleNamespace(Skip=lambda: None))
    assert shown == []


def test_workbook_capture_never_receives_spacing_annotations(
    pane_api: SimpleNamespace,
) -> None:
    """Manufacturing captures retain an unobscured, diagnostic-free presentation."""
    api = pane_api
    pane = api.module.WorkbookPreview(
        Control(),
        lambda *_args, **_kwargs: replace(api.capture, viewport_nm=(0, 0, 800, 420)),
    )
    pane.set_sections((api.section,), "board-a")
    show(api, pane)
    assert pane.canvas._visible_source_rect == (
        0,
        0,
        api.capture.width,
        api.capture.height,
    )
    assert pane.canvas.annotations == []


class _CountedContext:
    """Expose structural-context hash work independently of wall-clock timings."""

    def __init__(self, value: str, hashes: list[str]) -> None:
        self.value, self.hashes = value, hashes

    def __hash__(self) -> int:
        """Count full-context hashing, like hashing a large immutable board."""
        self.hashes.append(self.value)
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        """Retain semantic equality across independently reconstructed contexts."""
        return isinstance(other, _CountedContext) and self.value == other.value


def test_large_review_context_hashing_is_bounded_per_changed_binding(
    pane_api: SimpleNamespace,
) -> None:
    """A checklist repaint must not rehash the complete board once per row."""
    api = pane_api
    pane = open_pane(api)
    sections = tuple(
        replace(api.section, section_id=f"row-{index}") for index in range(900)
    )
    hashes: list[str] = []
    context = _CountedContext("board-a", hashes)
    pane.set_sections(sections, context)
    show(api, pane)
    initial = len(hashes)
    assert initial <= 1
    for _ in range(4):
        pane.refresh_review_labels()
        assert pane.ready
        assert pane.image_digest(sections[0]) == api.capture.sha256
    assert len(hashes) == initial
    pane.set_sections(sections, _CountedContext("board-a", hashes))
    assert len(hashes) == initial
    pane.set_sections(sections, _CountedContext("board-b", hashes))
    assert len(hashes) <= initial + 1
    assert not pane.ready
    assert pane.image_digest(sections[0]) is None


def test_equal_preview_context_adopts_latest_reference_without_resetting_view(
    pane_api: SimpleNamespace,
) -> None:
    """A no-op context refresh compares once while retaining painted evidence."""
    api = pane_api
    compared: list[object] = []

    class CountedEquality(_CountedContext):
        """Count semantic comparison independently of context interning hashes."""

        __hash__ = _CountedContext.__hash__

        def __eq__(self, other: object) -> bool:
            """Preserve the existing context's equality contract."""
            compared.append(other)
            return super().__eq__(other)

    hashes: list[str] = []
    original = CountedEquality("board-a", hashes)
    latest = CountedEquality("board-a", hashes)
    pane = open_pane(api)
    sections = (api.section,)
    pane.set_sections(sections, original)
    show(api, pane)
    token, context = pane._context_token, pane._bound_context
    viewed = dict(pane._viewed)
    request = pane._request
    compared.clear()
    pane.set_sections(sections, latest)
    assert len(compared) == 1
    compared.clear()
    for _attempt in range(5):
        pane.set_sections(sections, latest)
    assert compared == []
    assert pane._context_key is latest
    assert pane._context_token is token
    assert pane._bound_context is context and context.value is original
    assert pane._request == request
    assert pane._viewed == viewed
    assert pane.ready and pane.image_digest(api.section) == api.capture.sha256


def test_review_list_only_rewrites_changed_row_state(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native row updates stay local when one displayed image gains approval."""
    api = pane_api
    approved: set[str] = set()
    pane = open_pane(api, approved_images=lambda: approved)
    sections = tuple(
        replace(api.section, section_id=f"row-{index}") for index in range(900)
    )
    pane.set_sections(sections, "board-a")
    show(api, pane)
    writes: list[tuple[int, str]] = []
    set_item, set_colour = pane.checklist.SetItem, pane.checklist.SetItemTextColour

    def write_item(index: int, column: int, value: str) -> None:
        """Observe real cell writes without weakening the stateful native double."""
        writes.append((index, "text"))
        set_item(index, column, value)

    def write_colour(index: int, colour: object) -> None:
        """Observe native row recoloring while retaining actual control state."""
        writes.append((index, "colour"))
        set_colour(index, colour)

    monkeypatch.setattr(pane.checklist, "SetItem", write_item)
    monkeypatch.setattr(pane.checklist, "SetItemTextColour", write_colour)
    pane.refresh_review_labels()
    assert writes == []
    approved.add(sections[0].section_id)
    pane.refresh_review_labels()
    assert writes and {index for index, _kind in writes} == {0}
    assert "approved" in pane.checklist.GetItemText(0, 1)
    assert "needs review" in pane.checklist.GetItemText(1, 1)
    writes.clear()
    pane.refresh_review_labels()
    assert writes == []


def test_compact_review_keys_preserve_context_restore_and_appearance_invalidation(
    pane_api: SimpleNamespace,
) -> None:
    """Equal reconstructed contexts restore proof, while a different context cannot."""
    api = pane_api
    hashes: list[str] = []
    palette = ["before"]
    views: list[object] = []
    pane = open_pane(
        api,
        appearance_context=lambda: palette[0],
        image_viewed=lambda *args: views.append(args),
    )
    first = _CountedContext("board-a", hashes)
    pane.set_sections((api.section,), first)
    show(api, pane)
    assert len(views) == 1
    original_evidence = dict(pane._viewed)
    pane.set_sections((api.section,), _CountedContext("board-b", hashes))
    assert pane.image_digest(api.section) is None
    assert not pane.ready and len(views) == 1
    pane.restore_viewed(
        lambda _section, _digest: False, {api.section: api.capture.sha256}
    )
    assert pane._viewed == original_evidence
    pane.set_sections((api.section,), _CountedContext("board-a", hashes))
    assert pane.image_digest(api.section) == api.capture.sha256
    assert not pane.ready  # Historical proof has not supplied a current visible paint.
    assert pane._viewed == original_evidence and len(views) == 1
    before = len(hashes)
    changed = replace(api.section, width_nm=180_000)
    pane.set_sections((changed,), _CountedContext("board-a", hashes))
    assert len(hashes) == before
    assert pane.image_digest(changed) is None
    pane.set_sections((api.section,), first)
    assert pane.image_digest(api.section) == api.capture.sha256
    palette[0] = "after"
    with pytest.raises(ValueError, match="appearance changed"):
        pane.ensure_appearance_current()
    assert pane.image_digest(api.section) is None
    assert pane._viewed == {} and len(views) == 1
    assert len(hashes) == before


def test_compact_review_keys_do_not_retain_unseen_contexts_or_invent_reopened_views(
    pane_api: SimpleNamespace,
) -> None:
    """Intermediate revisions stay bounded and reopening needs verified saved hashes."""
    api = pane_api
    hashes: list[str] = []
    pane = open_pane(api)
    for index in range(40):
        pane.set_sections((api.section,), _CountedContext(str(index), hashes))
    assert len(pane._context_tokens) == 1
    assert not pane._viewed
    reopened = open_pane(api)
    reopened.set_sections((api.section,), _CountedContext("39", hashes))
    assert reopened.image_digest(api.section) is None
    reopened.restore_viewed(
        lambda _section, _digest: True, {api.section: api.capture.sha256}
    )
    assert reopened.image_digest(api.section) == api.capture.sha256
    assert not reopened.ready and not reopened.displayed
    assert reopened.reviewed_capture_hashes() == ((api.section, api.capture.sha256),)
    reopened.clear("Reset review")
    assert not reopened._viewed and not reopened._context_tokens


def test_compact_context_hash_collision_cannot_reuse_another_board_review(
    pane_api: SimpleNamespace,
) -> None:
    """A compact token still requires full semantic equality at context binding."""
    api = pane_api

    class CollidingContext(_CountedContext):
        """Model distinct structural boards with an identical ordinary hash."""

        def __hash__(self) -> int:
            """Force a collision without changing semantic equality."""
            self.hashes.append(self.value)
            return 7

    hashes: list[str] = []
    pane = open_pane(api)
    pane.set_sections((api.section,), CollidingContext("board-a", hashes))
    show(api, pane)
    pane.set_sections((api.section,), CollidingContext("board-b", hashes))
    assert pane.image_digest(api.section) is None
    assert not pane.ready
    pane.set_sections((api.section,), CollidingContext("board-a", hashes))
    assert pane.image_digest(api.section) == api.capture.sha256
    assert not pane.ready and not pane.displayed


def test_spacing_thumbnail_paint_is_navigation_without_diagnostic_view_evidence(
    pane_api: SimpleNamespace,
) -> None:
    """A small full-image thumbnail cannot satisfy the readable review requirement."""
    api = pane_api
    views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda *_args, **_kwargs: api.capture,
        spacing_image_viewed=lambda *args: views.append(args),
    )
    entry = _spacing_entry(api)
    pane.set_sections((api.section,), "board-a")
    pane.set_spacing_entries((entry,))
    spacing_index = pane.entries.index(entry)
    pane.select(spacing_index)
    show(api, pane)
    assert pane.ready and pane.displayed
    assert (
        min(
            pane.canvas._dimensions[0] / api.capture.width,
            pane.canvas._dimensions[1] / api.capture.height,
        )
        < 2 / 3
    )
    assert views == []
    assert pane._key(spacing_index) not in pane._viewed


@pytest.mark.parametrize("owner_state", ["closed", "inactive", "hidden"])
def test_spacing_open_rejects_inactive_owner_before_constructing_modal(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    owner_state: str,
) -> None:
    """Fresh source checking cannot make a previously painted dead owner reviewable."""
    api = pane_api
    current = {"active": True}
    pane = open_pane(api, active=lambda: current["active"])
    pane.set_sections((api.section,), "board-a", spacing_entries=(_spacing_entry(api),))
    show(api, pane)
    assert pane.displayed
    if owner_state == "closed":
        pane.close()
    elif owner_state == "inactive":
        current["active"] = False
    else:
        pane.window.on_screen = False
    monkeypatch.setattr(
        api.module,
        "EnlargedCaptureDialog",
        lambda *_args, **_kwargs: pytest.fail("An obsolete owner opened a modal"),
    )
    with pytest.raises(ValueError, match="spacing image"):
        pane.approve_spacing_image(lambda *_args: pytest.fail("Unexpected consent"))
    assert not pane.review_window_open


def _readable_large_viewer(
    api: SimpleNamespace,
    *,
    composition: object = None,
) -> tuple[Any, list[str], dict[str, Any]]:
    """Construct a real 1200×800 viewer with native-like visible screen bounds."""
    api.state.image = Image((1200, 800))
    service = importlib.import_module(api.module.__package__ + ".service")
    capture = service.CapturedImage.from_bytes(_png(size=(1200, 800)))
    events: list[str] = []
    state = {"active": True, "source": True, "frame_width": 1020}

    def verify() -> None:
        """Fail when the live source changes after the actual large image paint."""
        events.append("verify")
        if not state["source"]:
            raise ValueError("The board changed during spacing review")

    viewer = api.module.EnlargedCaptureDialog(
        Control(),
        capture,
        "Spacing review — F.Cu — USB_P, USB_N",
        caption="Too wide · declared gap 0.2 mm",
        verify_current=verify,
        displayed=lambda: events.append("viewed"),
        approve=lambda: events.append("approved"),
        active=lambda: state["active"],
        **({"composition": composition} if composition is not None else {}),
    )
    viewer.client_size = (1020, 780)
    viewer.canvas.client_size = (1000, 667)
    viewer.GetScreenRect = lambda: SimpleNamespace(
        x=0, y=0, width=state["frame_width"], height=800
    )
    viewer.ClientToScreen = lambda _x, _y: (0, 20)
    viewer.canvas.GetParent = lambda: viewer
    viewer.canvas.GetScreenRect = lambda: SimpleNamespace(
        x=10,
        y=40,
        width=viewer.canvas.client_size[0],
        height=viewer.canvas.client_size[1],
    )
    for control, top, width in (
        (viewer.caption, 20, 600),
        (viewer.status, 715, 700),
        (viewer.approve_button, 740, 220),
        (viewer.FindWindow(api.wx.ID_CANCEL), 770, 100),
    ):
        control.GetScreenRect = lambda top=top, width=width: SimpleNamespace(
            x=10, y=top, width=width, height=20
        )
    return viewer, events, state


def test_combined_spacing_requires_full_source_fit_at_readable_scale(
    pane_api: SimpleNamespace,
) -> None:
    """Automatic resizing requires a fresh, readable full-image paint before consent."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    resize(api, viewer, (300, 200))
    paint(api, viewer)
    assert not viewer.readable and "viewed" not in events
    resize(api, viewer, (1000, 667))
    assert not viewer.readable and not viewer.approve_button.enabled
    paint(api, viewer)
    assert viewer.readable and viewer.approve_button.enabled
    assert events.count("viewed") == 1 and "approved" not in events
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert viewer.approved and events[-1] == "approved"


def test_enlarged_spacing_action_names_exception_without_granting_consent(
    pane_api: SimpleNamespace,
) -> None:
    """The modal offers the same explicit exception approval only after current paint."""
    viewer, events, _state = _readable_large_viewer(pane_api)
    assert viewer.approve_button.label == "Approve spacing exception"
    assert not viewer.approve_button.enabled
    assert not viewer.approved and "approved" not in events
    paint(pane_api, viewer)
    assert viewer.readable and viewer.approve_button.enabled
    assert "viewed" in events and "approved" not in events
    viewer.EndModal(pane_api.wx.ID_CANCEL)
    assert not viewer.approved and "approved" not in events


def test_combined_spacing_automatic_fit_cannot_authorize_undersized_pixels(
    pane_api: SimpleNamespace,
) -> None:
    """Automatically fitting a small canvas cannot grant evidence or consent."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    resize(api, viewer, (300, 200))
    paint(api, viewer)
    assert viewer.canvas._painted_scale < 0.35
    assert not viewer.readable and "viewed" not in events
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert not viewer.approved and "approved" not in events


def test_combined_spacing_thumbnail_crops_detail_and_opens_explicit_review(
    pane_api: SimpleNamespace,
) -> None:
    """A compact detail remains navigation and never reviews composite panels."""
    api = pane_api
    entry = _spacing_entry(api)
    image_size = entry.width_px, entry.height_px
    api.state.image = Image(image_size)
    service = importlib.import_module(api.module.__package__ + ".service")
    capture = service.CapturedImage.from_bytes(_png(size=image_size))
    opened: list[bool] = []
    viewed: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda *_args, **_kwargs: capture,
        open_spacing_review=lambda: opened.append(True),
        spacing_image_viewed=lambda *args: viewed.append(args),
    )
    pane.set_sections((api.section,), "board-a")
    pane.set_spacing_entries((entry,))
    spacing_index = pane.entries.index(entry)
    pane.select(spacing_index)
    show(api, pane)
    assert pane.displayed
    assert pane.canvas._scaled_view == entry.composition.detail_rect
    assert pane.open_spacing_button.shown and pane.open_spacing_button.enabled
    pane.open_spacing_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    pane.canvas.bindings[api.wx.EVT_LEFT_UP](SimpleNamespace(Skip=lambda: None))
    assert opened == [True, True]
    assert viewed == [] and pane._key(spacing_index) not in pane._viewed


def test_combined_compact_preview_keeps_range_summary_when_no_visible_witness(
    pane_api: SimpleNamespace,
) -> None:
    """A range-only tile retains short mm context and no duplicate main action."""
    api = pane_api
    checks = importlib.import_module(api.module.__package__ + ".spacing_checks")
    entry = replace(
        _spacing_entry(api),
        target_spacing_nm=200_000,
        region_gap_nm=checks.NmBounds(100_000, 100_000),
    )
    image_size = entry.width_px, entry.height_px
    api.state.image = Image(image_size)
    service = importlib.import_module(api.module.__package__ + ".service")
    capture = service.CapturedImage.from_bytes(_png(size=image_size))
    pane = api.module.WorkbookPreview(
        Control(),
        lambda *_args, **_kwargs: capture,
        open_spacing_review=lambda: None,
        show_spacing_review_action=False,
    )
    pane.set_sections((api.section,), "board-a")
    pane.set_spacing_entries((entry,))
    spacing_index = pane.entries.index(entry)
    pane.select(spacing_index)
    show(api, pane)
    assert pane.caption.label == "Region gap 0.1 mm · target 0.2 mm"
    assert not pane.open_spacing_button.shown
    assert pane.title.shown and pane.displayed


def test_combined_fit_view_has_no_scroll_or_wheel_handlers(
    pane_api: SimpleNamespace,
) -> None:
    """Native gestures cannot move a full-image spacing review into a local viewport."""
    api = pane_api
    viewer, _events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    assert not hasattr(viewer.canvas, "_on_scroll")
    assert not hasattr(viewer.canvas, "_on_mousewheel")
    assert not viewer.canvas.style & (api.wx.HSCROLL | api.wx.VSCROLL)


def test_combined_fractional_fit_preserves_all_source_pixels(
    pane_api: SimpleNamespace,
) -> None:
    """Fractional full-image scaling reaches each source edge without native scrolling."""
    api = pane_api
    viewer, _events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    viewer.canvas.client_size = (503, 303)
    paint(api, viewer)
    assert viewer.canvas._visible_source_rect == (0, 0, 1200, 800)
    assert viewer.canvas._dimensions == (454, 303)
    assert viewer.canvas._painted_scale >= 0.35


def test_combined_queued_old_fit_cannot_record_resized_display(
    pane_api: SimpleNamespace,
) -> None:
    """Resize invalidates queued display work before the old Fit paint can be credited."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    paint(api, viewer, drain=False)
    resize(api, viewer, (500, 300))
    api.drain()
    assert not viewer.readable and not viewer.canvas._recorded
    assert "viewed" not in events
    paint(api, viewer)
    assert viewer.readable and events.count("viewed") == 1


def test_combined_fit_letterboxes_without_clipping_source_pixels(
    pane_api: SimpleNamespace,
) -> None:
    """A wide canvas centers the whole image and cannot claim an off-canvas strip."""
    api = pane_api
    viewer, _events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    viewer.canvas.client_size = (922, 461)
    paint(api, viewer)
    assert viewer.canvas._dimensions == (692, 461)
    assert viewer.canvas.drawn[-1][1:] == (115, 0)
    assert viewer.canvas._visible_source_rect == (0, 0, 1200, 800)


def test_combined_native_same_size_layout_settles_after_fresh_paint(
    pane_api: SimpleNamespace,
) -> None:
    """Wrapping unchanged status cannot invalidate every subsequent native paint."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api,
        composition=SimpleNamespace(
            detail_rect=(0, 0, 500, 300),
            required_regions=((0, 0, 500, 300),),
            minimum_scale=0.35,
        ),
    )
    layouts: list[bool] = []

    def native_layout() -> None:
        """Model the same-size event that macOS wx emits during native Layout."""
        layouts.append(True)
        viewer.canvas._resize(SimpleNamespace(Skip=lambda: None))

    viewer.Layout = native_layout
    viewer._review_wrap_key = None
    paint(api, viewer)
    assert viewer.readable and viewer.approve_button.enabled
    assert len(layouts) == 1
    assert events.count("viewed") == 1


def test_combined_fit_never_installs_native_scrollbars(
    pane_api: SimpleNamespace,
) -> None:
    """The full-source paint path does not mutate its client bounds with scrollbars."""
    api = pane_api
    viewer, _events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )

    def no_scrollbar(*_args: object) -> None:
        raise AssertionError("A Fit-only review must not install scrollbars")

    viewer.canvas.SetScrollbar = no_scrollbar
    paint(api, viewer)
    assert viewer.readable and viewer.canvas._recorded


def test_readable_large_spacing_view_requires_fresh_full_paint_and_explicit_button(
    pane_api: SimpleNamespace,
) -> None:
    """The 24-pixel native floor reaches eight visible pixels before consent."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(api)
    assert not viewer.readable and not viewer.approve_button.enabled
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert not viewer.approved and "approved" not in events
    # A failed early click cannot grant consent; a fresh real paint recovers.
    paint(api, viewer)
    assert viewer.readable and viewer.approve_button.enabled
    assert (
        24
        * min(viewer.canvas._dimensions[0] / 1200, viewer.canvas._dimensions[1] / 800)
        >= 8
    )
    assert events.count("viewed") == 1 and "approved" not in events
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert viewer.approved and events[-1] == "approved"
    assert viewer.modal_result == api.wx.ID_OK
    assert not viewer.readable  # A closed viewer cannot authorize another image.
    assert viewer.caption.label == "Too wide · declared gap 0.2 mm"
    assert viewer.canvas.annotations == []


@pytest.mark.parametrize(
    "state_name", ["small", "hidden", "stale", "failed", "clipped"]
)
def test_spacing_large_view_rejects_unreadable_or_invalid_paints(
    pane_api: SimpleNamespace,
    state_name: str,
) -> None:
    """Scale, source, visibility and real ancestor clipping all precede viewing."""
    api = pane_api
    viewer, events, state = _readable_large_viewer(api)
    if state_name == "small":
        viewer.canvas.client_size = (300, 200)
    elif state_name == "hidden":
        viewer.on_screen = False
    elif state_name == "stale":
        state["active"] = False
    elif state_name == "failed":
        api.state.image.valid = False
    else:
        state["frame_width"] = 900  # A logical 1000px canvas extends outside its frame.
    paint(api, viewer)
    assert not viewer.readable and not viewer.approve_button.enabled
    assert "viewed" not in events
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert not viewer.approved and "approved" not in events


@pytest.mark.parametrize("change", ["resize", "source", "selection", "clip"])
def test_readable_spacing_view_revalidates_at_explicit_approval(
    pane_api: SimpleNamespace,
    change: str,
) -> None:
    """An earlier readable paint cannot approve newly stale or clipped pixels."""
    api = pane_api
    viewer, events, state = _readable_large_viewer(api)
    paint(api, viewer)
    assert viewer.readable and events.count("viewed") == 1
    if change == "resize":
        resize(api, viewer, (300, 200))
        assert not viewer.approve_button.enabled
    elif change == "source":
        state["source"] = False
    elif change == "selection":
        state["active"] = False
    else:
        state["frame_width"] = 900
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert not viewer.approved and "approved" not in events
    assert not viewer.readable


def test_spacing_large_view_cancel_does_not_approve_a_viewed_image(
    pane_api: SimpleNamespace,
) -> None:
    """Readable viewing remains a separate event when the user cancels."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(api)
    paint(api, viewer)
    assert viewer.readable and "viewed" in events
    viewer.EndModal(api.wx.ID_CANCEL)
    assert not viewer.approved and not viewer.readable and "approved" not in events


def test_spacing_large_view_resize_requires_another_readable_native_paint(
    pane_api: SimpleNamespace,
) -> None:
    """A too-small initial display cannot become approved from resize alone."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(api)
    viewer.canvas.client_size = (300, 200)
    paint(api, viewer)
    assert not viewer.readable and "viewed" not in events
    resize(api, viewer, (1000, 667))
    assert not viewer.readable and not viewer.approve_button.enabled
    paint(api, viewer)
    assert viewer.readable and events.count("viewed") == 1


def test_moving_spacing_review_back_on_screen_requires_another_full_paint(
    pane_api: SimpleNamespace,
) -> None:
    """A clipped window recovers through a real visible paint after being moved."""
    api = pane_api
    viewer, events, state = _readable_large_viewer(api)
    state["frame_width"] = 900
    paint(api, viewer)
    assert not viewer.readable and "viewed" not in events
    state["frame_width"] = 1020
    viewer.bindings[api.wx.EVT_MOVE](SimpleNamespace(Skip=lambda: None))
    assert not viewer.readable and not viewer.approve_button.enabled
    paint(api, viewer)
    assert viewer.readable and events.count("viewed") == 1
    resize(api, viewer, (950, 634))
    paint(api, viewer)
    assert viewer.readable and events.count("viewed") == 1


def test_spacing_viewer_reserves_measured_chrome_within_small_work_area(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All controls fit an 800px-high work area while the full image stays readable."""
    api = pane_api
    area = SimpleNamespace(
        x=100,
        y=50,
        width=1280,
        height=800,
        GetWidth=lambda: 1280,
        GetHeight=lambda: 800,
    )
    monkeypatch.setattr(api.wx.Display, "GetClientArea", lambda _self: area)
    viewer, _events, _state = _readable_large_viewer(api)
    width, height = viewer.canvas.minimum_size
    fitted = viewer.GetSize()
    assert fitted.GetWidth() <= area.width and fitted.GetHeight() <= area.height
    assert viewer.position[0] >= area.x and viewer.position[1] >= area.y
    assert viewer.position[0] + fitted.GetWidth() <= area.x + area.width
    assert viewer.position[1] + fitted.GetHeight() <= area.y + area.height
    assert 24 * min(width / 1200, height / 800) >= 8


def test_atomic_binding_starts_with_spacing_without_incidental_trace_capture(
    pane_api: SimpleNamespace,
) -> None:
    """A fresh context queues its first spacing entry before any trace provider runs."""
    api = pane_api
    captured: list[object] = []
    entry = _spacing_entry(api)
    pane = api.module.WorkbookPreview(
        Control(),
        lambda selected, **_options: captured.append(selected) or api.capture,
        image_list=Control(),
    )
    pane.set_sections((api.section,), "board-a", spacing_entries=(entry,))
    assert pane.entries == (entry, api.section)
    assert pane.selected_entry is entry
    assert "Spacing image 1" in pane.checklist.GetItemText(0, 1)
    assert "Image 1" in pane.checklist.GetItemText(1, 1)
    with pytest.raises(ValueError, match="Spacing image 1, Image 1"):
        pane.ensure_reviewed()
    show(api, pane)
    assert captured == [entry]
    pane.select(pane.entries.index(api.section))
    show(api, pane)
    replacement = replace(entry, caption="Updated diagnostic caption")
    pane.set_sections((api.section,), "board-a", spacing_entries=(replacement,))
    assert pane.selected_entry is api.section
    pane.set_sections((api.section,), "board-b", spacing_entries=(replacement,))
    assert pane.selected_entry is replacement


def test_atomic_binding_detects_spacing_only_changes_and_explicit_selection(
    pane_api: SimpleNamespace,
) -> None:
    """Unchanged trace/context inputs cannot hide changed diagnostic entries."""
    api = pane_api
    pane = open_pane(api)
    first = _spacing_entry(api)
    second = replace(first, view_id="spacing-b", input_digest="b" * 64)
    pane.set_sections((api.section,), "board-a", spacing_entries=(first,))
    show(api, pane)
    pane.set_sections(
        (api.section,), "board-a", api.section.section_id, spacing_entries=(second,)
    )
    assert pane.entries == (second, api.section)
    assert pane.selected_entry is api.section
    pane.set_spacing_entries((first, second))
    assert pane.entries == (first, second, api.section)
    assert pane.selected_entry is api.section
    assert "Spacing image 2" in pane.checklist.GetItemText(1, 1)
    assert "Image 1" in pane.checklist.GetItemText(2, 1)


def test_combined_spacing_automatically_fits_full_image_without_navigation(
    pane_api: SimpleNamespace,
) -> None:
    """One legible full composition paint grants viewing, requiring later consent."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35, required_regions=())
    )
    assert {control.label for control in viewer.children}.isdisjoint(
        {"Fit image", "Original image"}
    )
    viewer.canvas.client_size = (500, 300)
    paint(api, viewer)
    assert viewer.canvas._scaled_view == (0, 0, 1200, 800)
    assert viewer.canvas._visible_source_rect == (0.0, 0.0, 1200.0, 800.0)
    assert viewer.canvas._dimensions == (450, 300)
    assert viewer.readable and events.count("viewed") == 1
    assert not viewer.approved and "approved" not in events
    assert not hasattr(viewer, "fit_image_button")
    assert not hasattr(viewer, "original_button")
    assert not hasattr(viewer, "_navigation_buttons")
    assert not hasattr(viewer, "_on_original")
    assert not hasattr(viewer, "_comparing_original")
    assert not hasattr(viewer.canvas, "fit_image")
    assert {control.label for control in viewer.children if control.shown} == {
        "",
        viewer.caption.label,
        viewer.status.label,
        "Approve spacing exception",
    }
    assert not hasattr(viewer, "readable_button")
    assert not hasattr(viewer, "zoom_in_button")
    assert not hasattr(viewer.canvas, "scroll_to")


def test_combined_spacing_cropped_or_unreadable_paint_cannot_grant_consent(
    pane_api: SimpleNamespace,
) -> None:
    """No local crop or undersized composition may substitute for current Fit proof."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35, required_regions=())
    )
    viewer.canvas.client_size = (300, 200)
    paint(api, viewer)
    assert not viewer.readable and "viewed" not in events
    assert not any(word in viewer.status.label.lower() for word in ("zoom", "scroll"))
    resize(api, viewer, (500, 300))
    viewer.canvas._presentation_crop = (0, 0, 500, 300)
    paint(api, viewer)
    assert not viewer.readable and "viewed" not in events
    viewer.approve_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert not viewer.approved and "approved" not in events


def test_spacing_capture_errors_name_the_diagnostic_group(
    pane_api: SimpleNamespace,
) -> None:
    """A failed initial spacing capture never reports that a workbook PNG failed."""
    api = pane_api
    pane = open_pane(api)
    entry = _spacing_entry(api)

    def unavailable(_entry: Any, *, refresh: bool = False) -> Any:
        raise ValueError("Native spacing pixels unavailable")

    pane._preview = unavailable
    pane.set_sections((api.section,), "board-a", spacing_entries=(entry,))
    api.drain()
    assert pane.selected_entry is entry
    assert pane.failure.startswith("Spacing preview unavailable:")
    assert "Native spacing pixels unavailable" in pane.failure
    assert not pane.displayed


def test_automatic_review_returns_from_manual_trace_to_pending_spacing_first(
    pane_api: SimpleNamespace,
) -> None:
    """Approving an explicitly chosen trace cannot auto-select another trace first."""
    api = pane_api
    pane = open_pane(api)
    spacing = _spacing_entry(api)
    second = replace(api.section, section_id="row-b")
    pane.set_sections((api.section, second), "board-a", spacing_entries=(spacing,))
    pane.select(pane.entries.index(api.section))
    show(api, pane)
    assert pane.advance_to_unreviewed()
    assert pane.selected_entry is spacing
    # The currently pending spacing entry remains selected until its own consent.
    assert pane.advance_to_unreviewed()
    assert pane.selected_entry is spacing
    approved = {spacing.view_id, api.section.section_id}
    assert pane.advance_to_unreviewed(approved)
    assert pane.selected_entry is second


def test_same_size_native_layout_keeps_current_full_fit_paint(
    pane_api: SimpleNamespace,
) -> None:
    """Native Layout may emit size events without changing any displayed geometry."""
    api = pane_api
    viewer, events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    paint(api, viewer)
    assert viewer.readable and viewer.approve_button.enabled
    viewer.canvas.bindings[api.wx.EVT_SIZE](SimpleNamespace(Skip=lambda: None))
    assert viewer.readable and viewer.canvas._recorded
    assert viewer.approve_button.enabled and events.count("viewed") == 1


@pytest.mark.parametrize("iconized", [True, False])
def test_minimize_or_restore_requires_fresh_full_fit_paint(
    pane_api: SimpleNamespace,
    iconized: bool,
) -> None:
    """Window-state transitions remain fresh even when their client bounds are equal."""
    api = pane_api
    viewer, _events, _state = _readable_large_viewer(
        api, composition=SimpleNamespace(minimum_scale=0.35)
    )
    paint(api, viewer)
    assert viewer.readable
    viewer.bindings[api.wx.EVT_ICONIZE](
        SimpleNamespace(IsIconized=lambda: iconized, Skip=lambda: None)
    )
    assert not viewer.readable and not viewer.canvas._recorded
    assert not viewer.approve_button.enabled
    paint(api, viewer)
    assert viewer.readable


@pytest.mark.parametrize("kind", ["trace", "spacing"])
def test_enlarged_window_lifecycle_reports_readonly_pause_without_review_events(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Pause cold work before construction until teardown without changing review guards."""
    api = pane_api
    transitions: list[bool] = []
    phases: list[str] = []
    reviews: list[object] = []
    pane = open_pane(
        api,
        state_changed=lambda: transitions.append(
            getattr(pane, "review_window_open", False)
        ),
        spacing_image_viewed=lambda *args: reviews.append(args),
    )
    entry = _spacing_entry(api)
    pane.set_sections((api.section,), "board-a", spacing_entries=(entry,))
    if kind == "trace":
        pane.select(pane.entries.index(api.section))
    show(api, pane)
    assert getattr(pane, "review_window_open", False) is False
    transitions.clear()
    viewed = dict(pane._viewed)
    real_constructor = api.module.EnlargedCaptureDialog
    real_destroy = Control.Destroy

    def construct(*args: Any, **kwargs: Any) -> Any:
        assert getattr(pane, "review_window_open", False) and pane._active()
        phases.append("construct")
        return real_constructor(*args, **kwargs)

    def show_modal(viewer: Any) -> int:
        assert getattr(pane, "review_window_open", False) and pane._active()
        phases.append("show")
        if kind == "spacing":
            assert viewer._active()
            viewer._verify_current()
            assert not viewer.approved
        return api.wx.ID_CANCEL

    def destroy(viewer: Any) -> None:
        assert getattr(pane, "review_window_open", False)
        phases.append("destroy")
        real_destroy(viewer)

    monkeypatch.setattr(api.module, "EnlargedCaptureDialog", construct)
    monkeypatch.setattr(Control, "ShowModal", show_modal)
    monkeypatch.setattr(Control, "Destroy", destroy)
    if kind == "spacing":
        assert not pane.approve_spacing_image(lambda *args: reviews.append(args))
    else:
        pane._open_enlarged_capture()
    assert phases == ["construct", "show", "destroy"]
    assert transitions == [True, False]
    assert getattr(pane, "review_window_open", False) is False
    assert pane._viewed == viewed and reviews == []
    with pytest.raises(AttributeError):
        pane.review_window_open = True


@pytest.mark.parametrize("kind", ["trace", "spacing"])
@pytest.mark.parametrize("failure", ["construct", "show", "destroy"])
def test_enlarged_window_failure_resumes_background_work_in_finally(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure: str,
) -> None:
    """Every failure phase releases the pause without approving or stamping an image."""
    api = pane_api
    transitions: list[bool] = []
    pane = open_pane(
        api,
        state_changed=lambda: transitions.append(
            getattr(pane, "review_window_open", False)
        ),
    )
    pane.set_sections((api.section,), "board-a", spacing_entries=(_spacing_entry(api),))
    if kind == "trace":
        pane.select(pane.entries.index(api.section))
    show(api, pane)
    transitions.clear()
    viewed = dict(pane._viewed)
    real_constructor = api.module.EnlargedCaptureDialog
    real_destroy = Control.Destroy

    def construct(*args: Any, **kwargs: Any) -> Any:
        assert getattr(pane, "review_window_open", False)
        if failure == "construct":
            raise ValueError("Enlarged constructor failed")
        return real_constructor(*args, **kwargs)

    def show_modal(_viewer: Any) -> int:
        assert getattr(pane, "review_window_open", False)
        if failure == "show":
            raise ValueError("Enlarged ShowModal failed")
        return api.wx.ID_CANCEL

    def destroy(viewer: Any) -> None:
        assert getattr(pane, "review_window_open", False)
        real_destroy(viewer)
        if failure == "destroy":
            raise ValueError("Enlarged Destroy failed")

    monkeypatch.setattr(api.module, "EnlargedCaptureDialog", construct)
    monkeypatch.setattr(Control, "ShowModal", show_modal)
    monkeypatch.setattr(Control, "Destroy", destroy)
    with pytest.raises(ValueError, match="Enlarged"):
        if kind == "spacing":
            pane.approve_spacing_image(lambda *_args: pytest.fail("Unexpected consent"))
        else:
            pane._open_enlarged_capture()
    assert transitions == [True, False]
    assert getattr(pane, "review_window_open", False) is False
    assert pane._viewed == viewed


def test_nested_enlarged_windows_keep_outer_pause_until_return(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested modal teardown cannot resume cold work while its outer viewer stays open."""
    api = pane_api
    transitions: list[bool] = []
    phases: list[str] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda *_args, **_kwargs: api.capture,
        state_changed=lambda: transitions.append(
            getattr(pane, "review_window_open", False)
        ),
    )
    pane.set_sections((api.section,), "board-a", spacing_entries=(_spacing_entry(api),))
    show(api, pane)
    transitions.clear()

    def show_modal(viewer: Any) -> int:
        assert getattr(pane, "review_window_open", False) and pane._active()
        if phases:
            phases.append("nested")
            assert viewer._approve is None
        else:
            phases.append("outer")
            pane._open_enlarged_capture()
            assert getattr(pane, "review_window_open", False) and transitions == [True]
        return api.wx.ID_CANCEL

    monkeypatch.setattr(Control, "ShowModal", show_modal)
    assert not pane.approve_spacing_image(
        lambda *_args: pytest.fail("Unexpected consent")
    )
    assert phases == ["outer", "nested"]
    assert transitions == [True, False]
    assert getattr(pane, "review_window_open", False) is False


def test_failed_enlarged_image_defers_bound_selected_retry_until_close(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed modal stays responsive while one selected retry waits for its return."""
    api = pane_api
    captures: list[tuple[object, bool]] = []
    approvals: list[object] = []
    views: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda entry, *, refresh=False: captures.append((entry, refresh))
        or api.capture,
        spacing_image_viewed=lambda *args: views.append(args),
    )
    entry = _spacing_entry(api)
    pane.set_sections((api.section,), "board-a", spacing_entries=(entry,))
    show(api, pane)

    def show_modal(viewer: Any) -> int:
        viewer._on_canvas_failed("The enlarged native image could not paint")
        retry = pane.window.bindings[api.wx.EVT_TIMER, pane._retry_timer]
        for _tick in range(3):
            pane._retry_timer.Stop()  # Native one-shot timers stop before delivering EVT_TIMER.
            retry(SimpleNamespace())
            api.drain()
        assert captures == [(entry, False)]
        assert not pane.capture_loaded and not viewer.approved
        viewer._on_approve(SimpleNamespace())
        assert not viewer.approved and approvals == views == []
        return api.wx.ID_CANCEL

    monkeypatch.setattr(Control, "ShowModal", show_modal)
    assert not pane.approve_spacing_image(lambda *args: approvals.append(args))
    assert captures == [(entry, False)]
    assert len(api.queued) == 1
    api.drain()
    assert captures == [(entry, False), (entry, True)]
    assert not pane.displayed and approvals == views == []


def test_nested_modal_coalesces_latest_context_and_selected_image_until_outer_close(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated current-source replacements retain one latest render behind both modals."""
    api = pane_api
    captures: list[tuple[object, object, bool]] = []
    approvals: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(),
        lambda entry, *, refresh=False: captures.append(
            (pane._context_key, entry, refresh)
        )
        or api.capture,
    )
    entry = _spacing_entry(api)
    pane.set_sections((api.section,), "board-a", spacing_entries=(entry,))
    show(api, pane)
    shown: list[Any] = []
    latest = replace(api.section, section_id="latest-trace")

    def show_modal(viewer: Any) -> int:
        shown.append(viewer)
        if len(shown) == 1:
            pane._open_enlarged_capture()
            assert pane.review_window_open and len(captures) == 1
            assert api.queued == []
            assert not viewer._active() and not viewer.approved
        else:
            for revision in range(20):
                pane.set_sections(
                    (latest,), f"board-{revision}", spacing_entries=(entry,)
                )
                api.drain()
            pane.select(pane.entries.index(latest), refresh=True)
            api.drain()
            assert len(captures) == 1 and pane.review_window_open
        return api.wx.ID_CANCEL

    monkeypatch.setattr(Control, "ShowModal", show_modal)
    assert not pane.approve_spacing_image(lambda *args: approvals.append(args))
    assert len(shown) == 2 and len(api.queued) == 1
    api.drain()
    assert captures == [("board-a", entry, False), ("board-19", latest, True)]
    assert approvals == [] and not pane.displayed


@pytest.mark.parametrize("finish", ["clear", "close"])
def test_modal_deferred_capture_is_discarded_after_context_clear_or_owner_close(
    pane_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
) -> None:
    """Returning from an obsolete modal cannot resume cancelled native capture work."""
    api = pane_api
    captures: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(), lambda entry, **_kwargs: captures.append(entry) or api.capture
    )
    entry = _spacing_entry(api)
    pane.set_sections((api.section,), "board-a", spacing_entries=(entry,))
    show(api, pane)

    def show_modal(_viewer: Any) -> int:
        pane.set_sections((api.section,), "board-b", spacing_entries=(entry,))
        api.drain()
        assert captures == [entry]
        if finish == "clear":
            pane.clear("The selected source no longer exists.")
        else:
            pane.close()
        return api.wx.ID_CANCEL

    monkeypatch.setattr(Control, "ShowModal", show_modal)
    assert not pane.approve_spacing_image(
        lambda *_args: pytest.fail("Unexpected consent")
    )
    api.drain()
    assert captures == [entry]
    assert not pane.review_window_open


def test_maximized_review_geometry_accepts_measured_hidden_frame_border_only() -> None:
    """The Openbox frame offset is valid only for an actually maximized native window."""
    from .test_impedance_spacing_controls_native import _assert_review_geometry

    area = (0, 0, 1920, 1080)
    frame = (-1, -1, 1920, 1080)
    client = (0, 19, 1920, 1061)
    controls = {"canvas": (8, 60, 1904, 945), "approve": (249, 1038, 187, 34)}
    _assert_review_geometry(area, frame, client, controls, maximized=True)
    _assert_review_geometry(area, area, client, controls, maximized=False)
    with pytest.raises(AssertionError):
        _assert_review_geometry(area, frame, client, controls, maximized=False)


@pytest.mark.parametrize("maximized", [False, True])
@pytest.mark.parametrize(
    "client",
    [
        (-1, 19, 1920, 1061),
        (0, -1, 1920, 1080),
        (0, 19, 1921, 1061),
        (0, 19, 1920, 1062),
    ],
)
def test_review_geometry_rejects_every_client_overflow_edge_in_both_window_states(
    client: tuple[int, int, int, int],
    maximized: bool,
) -> None:
    """Neither window state can authorize a client edge outside the work area."""
    from .test_impedance_spacing_controls_native import _assert_review_geometry

    with pytest.raises(AssertionError):
        _assert_review_geometry(
            (0, 0, 1920, 1080),
            (-1, -1, 1920, 1080) if maximized else (0, 0, 1920, 1080),
            client,
            {},
            maximized=maximized,
        )


@pytest.mark.parametrize(
    "control",
    [
        (-1, 60, 100, 100),
        (8, -1, 100, 100),
        (1821, 60, 100, 100),
        (8, 981, 100, 100),
        (8, 18, 100, 17),
    ],
)
def test_maximized_review_geometry_rejects_controls_outside_display_or_client(
    control: tuple[int, int, int, int],
) -> None:
    """Every complete control must fit the display and client, including client-only clips."""
    from .test_impedance_spacing_controls_native import _assert_review_geometry

    with pytest.raises(AssertionError):
        _assert_review_geometry(
            (0, 0, 1920, 1080),
            (-1, -1, 1920, 1080),
            (0, 19, 1920, 1061),
            {"canvas": control},
            maximized=True,
        )
