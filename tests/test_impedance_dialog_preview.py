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

    def __init__(self, parent: object = None, **kwargs: Any) -> None:
        self.parent = parent
        self.label = kwargs.get("label", "")
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
        self.columns: list[tuple[str, int]] = []
        self.cells: dict[tuple[int, int], str] = {}
        self.colours: dict[int, object] = {}

    def __bool__(self) -> bool:
        """Match wx window lifetime checks."""
        return self.alive

    def Show(self, shown: bool = True) -> None:
        """Retain the visibility changed by the owner."""
        self.shown = shown

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

    def SetItemTextColour(self, index: int, colour: object) -> None:
        """Make completed versus pending row colours observable."""
        self.colours[index] = colour

    def EnsureVisible(self, index: int) -> None:
        """Retain the row requested for native scrolling."""
        self.visible_index = index

    def SetMinSize(self, size: tuple[int, int]) -> None:
        """Retain the requested native minimum independently of client size."""
        self.minimum_size = size

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

    def Bind(self, event: object, handler: Callable[..., None]) -> None:
        """Retain the real event callback for later invocation."""
        self.bindings[event] = handler

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

    def DrawBitmap(self, bitmap: Image, x: int, y: int, transparent: bool) -> None:
        """Record a valid bitmap drawn by the real paint handler."""
        assert bitmap.IsOk()
        self.canvas.drawn.append((bitmap, x, y))


def _png() -> bytes:
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
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 800, 420, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * 2400) * 420))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def pane_api() -> Iterator[SimpleNamespace]:
    """Import real constructors with queued callbacks and explicitly painted wx."""
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
        Choice=Control,
        Button=Control,
        StaticText=Control,
        TextCtrl=Control,
        ListCtrl=Control,
        BoxSizer=Sizer,
        CallAfter=call_after,
        AutoBufferedPaintDC=PaintDC,
        Brush=lambda colour: colour,
        Colour=lambda *channels: tuple(channels),
        SystemSettings=SimpleNamespace(GetColour=lambda _kind: (120, 120, 120)),
        Image=load_image,
        LogNull=suspend_logging,
        Bitmap=lambda image: image,
    )
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
    pane.rows.SetSelection(index)
    pane.rows.bindings[api.wx.EVT_CHOICE](SimpleNamespace(GetSelection=lambda: index))


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
        Control(), lambda section, **options: api.capture, **kwargs
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
    )
    assert pane.canvas.minimum_size == (420, 180)
    pane.set_sections((api.section,), ("board-a", "scan-1"))
    assert calls == [] and not pane.ready
    api.drain()
    assert calls == [api.section]
    assert api.state.loaded == [(api.capture.data, api.wx.BITMAP_TYPE_PNG)]
    assert pane.rows.GetSelection() == 0 and pane.rows.enabled
    assert all(
        value in pane.rows.items[0] for value in ("1", "In2.Cu", "0.175", "USB_P")
    )
    assert "needs review" in pane.rows.items[0]
    assert views == [] and not pane.ready
    with pytest.raises(ValueError, match="Images still needing review: 1"):
        pane.ensure_reviewed()
    paint(api, pane, drain=False)
    assert views == [] and not pane.ready
    api.drain()
    assert pane.ready and len(views) == 1
    assert views[0][0:2] == (api.section, api.capture.sha256)
    assert pane.canvas.drawn[0][0].size == (420, 220)
    assert api.state.image.size == (800, 420)
    assert pane.checklist.cells[0, 0] == "☑"
    assert pane.checklist.colours[0] == (0, 150, 50)
    assert "last viewed" not in pane.checklist.cells[0, 1].lower()
    assert pane.reviewed_capture_hashes() == ((api.section, api.capture.sha256),)


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
    with pytest.raises(ValueError, match="Images still needing review: 2"):
        pane.ensure_reviewed()
    assert pane.advance_to_unreviewed() is True
    assert pane.rows.GetSelection() == 1 and not pane.ready
    with pytest.raises(ValueError):
        pane.ensure_reviewed()
    show(api, pane)
    assert pane.advance_to_unreviewed() is False
    pane.ensure_reviewed()
    assert all("— viewed" in label for label in pane.rows.items)


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

    pane = api.module.WorkbookPreview(Control(), preview)
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
    pane.refresh_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    assert not pane.ready
    show(api, pane)
    assert pane.canvas._image is None
    assert "unavailable" in pane.status.GetValue().lower()
    with pytest.raises(ValueError, match="(?i)preview"):
        pane.ensure_reviewed()
    failed = False
    api.wx.Image = decoder
    api.state.image = Image((800, 420))
    pane.refresh_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
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
    assert pane.rows.items == [] and not pane.rows.enabled
    assert not pane.refresh_button.enabled
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
    assert pane.canvas._image is None and not pane.refresh_button.enabled
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

    pane = api.module.WorkbookPreview(Control(), preview)
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
    assert pane.rows.GetSelection() == 1 and pane.canvas._bitmap is bitmap
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
    assert "needs review" in pane.rows.items[1]
    with pytest.raises(ValueError, match="Images still needing review: 2"):
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
    pane.refresh_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
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

    pane = api.module.WorkbookPreview(Control(), preview)
    second = replace(api.section, section_id="row-b")
    pane.set_sections((api.section, second), "board-a")
    show(api, pane)
    choose(api, pane, 1)
    show(api, pane)
    pane.ensure_reviewed()
    failed = True
    pane.refresh_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())
    show(api, pane)
    choose(api, pane, 0)
    show(api, pane)
    with pytest.raises(ValueError, match="Images still needing review: 2"):
        pane.ensure_reviewed()


def test_layer_navigation_uses_capture_provider_and_review_keys_not_row_indices(
    pane_api: SimpleNamespace,
) -> None:
    """Layer changes preserve only evidence for the exact section and context."""
    api = pane_api
    captures: list[object] = []
    pane = api.module.WorkbookPreview(
        Control(), lambda section, **options: captures.append(section) or api.capture
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
    with pytest.raises(ValueError, match="Images still needing review: 1"):
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
    assert not pane.refresh_button.enabled and pane.canvas._image is None
    with pytest.raises(ValueError, match="unpaired CONTEXT_1"):
        pane.ensure_reviewed()
    pane.set_sections((api.section,), "board-b")
    show(api, pane)
    pane.ensure_reviewed()
