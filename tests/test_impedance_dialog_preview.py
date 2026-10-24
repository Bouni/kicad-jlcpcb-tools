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
