"""Shared preview workflow regressions; native rendering is deliberately mocked.

Use real component constructors and bound handlers. Refresh requests do not paint
automatically, and CallAfter stays queued until explicitly dispatched, so these
checks cannot mistake image installation or a pending callback for a viewed PNG.
These checks verify simulated behavior, not native wx execution.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import replace
import importlib
from io import BytesIO
from pathlib import Path
import struct
from types import ModuleType, SimpleNamespace
from typing import Any
import zlib

import pytest

from tests.test_impedance_dialog_preview import Control, Image, Sizer
from tests.wx_harness import FakeWxModule, temporary_modules


class PreviewControl(Control):
    """Retain native-like visibility, choice, text and report-list state."""

    def __init__(self, parent: object = None, **kwargs: Any) -> None:
        super().__init__(parent, **kwargs)
        self.value = kwargs.get("value", "")
        self.shown = self.alive = True
        self.on_screen = True
        self.refreshes = 0
        self.drawn: list[tuple[Image, int, int]] = []
        self.columns: list[tuple[str, int]] = []
        self.cells: dict[tuple[int, int], str] = {}
        self.colours: dict[int, object] = {}

    def __bool__(self) -> bool:
        """Match native window lifetime checks."""
        return self.alive

    def Show(self, shown: bool = True) -> None:
        """Retain owner visibility without inventing a paint event."""
        self.shown = shown

    def IsShownOnScreen(self) -> bool:
        """Require a live visible window and its owning panels."""
        if not self.alive or not self.shown:
            return False
        if isinstance(self.parent, PreviewControl):
            return self.parent.IsShownOnScreen()
        return self.on_screen

    def SetName(self, name: str) -> None:
        """Retain the accessibility name assigned by the real constructor."""
        self.name = name

    def SetBackgroundStyle(self, style: object) -> None:
        """Retain the buffered-paint background mode."""
        self.background_style = style

    def GetBackgroundColour(self) -> tuple[int, int, int]:
        """Provide a deterministic panel colour for paint assertions."""
        return (20, 20, 20)

    def Refresh(self) -> None:
        """Request painting without inventing a native paint event."""
        self.refreshes += 1

    def SetValue(self, value: str) -> None:
        """Store status text without synthesizing native events."""
        self.value = value

    def GetValue(self) -> str:
        """Read the value actually assigned by the production handler."""
        return self.value

    def SetString(self, index: int, value: str) -> None:
        """Update a row label without altering choice selection."""
        self.items[index] = value

    def InsertColumn(self, index: int, label: str, width: int) -> None:
        """Retain the native checklist's column layout."""
        self.columns.insert(index, (label, width))

    def DeleteAllItems(self) -> None:
        """Clear checklist cells, colours, and selection together."""
        self.items.clear()
        self.cells.clear()
        self.colours.clear()
        self.selection = -1

    def InsertItem(self, index: int, text: str) -> None:
        """Insert a checklist row at its displayed index."""
        self.items.insert(index, text)
        self.cells[index, 0] = text

    def SetItem(self, index: int, column: int, text: str) -> None:
        """Retain text in the requested checklist cell."""
        self.cells[index, column] = text

    def SetItemTextColour(self, index: int, colour: object) -> None:
        """Expose completed and pending row colours to assertions."""
        self.colours[index] = colour

    def EnsureVisible(self, index: int) -> None:
        """Record which checklist row native scrolling must reveal."""
        self.visible_index = index


class PaintDC:
    """Record a successful draw without deriving paint events from layout."""

    def __init__(self, canvas: PreviewControl) -> None:
        self.canvas = canvas

    def SetBackground(self, brush: object) -> None:
        """Retain the brush chosen for this paint."""
        self.brush = brush

    def Clear(self) -> None:
        """Discard draws belonging to the previous paint."""
        self.canvas.drawn.clear()

    def DrawBitmap(self, bitmap: Image, x: int, y: int, transparent: bool) -> None:
        """Record the real handler's successful bitmap draw."""
        assert bitmap.IsOk()
        self.canvas.drawn.append((bitmap, x, y))


def _png(width: int = 800, height: int = 420) -> bytes:
    """Supply a valid small-compressed PNG; wx pixel decoding remains mocked."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\0" * width * 3) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def shared_preview_api() -> Iterator[SimpleNamespace]:
    """Load only the real component/model siblings under explicit wx doubles."""
    package_name = "_impedance_shared_preview_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    queued: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    def call_after(callback: Callable[..., object], *args: object) -> None:
        queued.append((callback, args))

    def drain() -> None:
        while queued:
            callback, args = queued.pop(0)
            callback(*args)

    def decode(source: BytesIO, _kind: object) -> Image:
        return Image(struct.unpack_from(">II", source.getvalue(), 16))

    wx = FakeWxModule(
        "wx",
        Panel=PreviewControl,
        Choice=PreviewControl,
        Button=PreviewControl,
        StaticText=PreviewControl,
        ListCtrl=PreviewControl,
        TextCtrl=PreviewControl,
        BoxSizer=Sizer,
        CallAfter=call_after,
        AutoBufferedPaintDC=PaintDC,
        Brush=lambda colour: colour,
        Colour=lambda *channels: tuple(channels),
        SystemSettings=SimpleNamespace(GetColour=lambda _kind: (120, 120, 120)),
        Image=decode,
        Bitmap=lambda image: image,
        LogNull=nullcontext,
    )
    with temporary_modules(
        {package_name: package, "wx": wx}, namespaces=(package_name,)
    ):
        module = importlib.import_module(f"{package_name}.dialog_preview")
        model = importlib.import_module(f"{package_name}.model")
        service = importlib.import_module(f"{package_name}.service")
        section = model.Section(
            "row-a",
            "spec",
            "F.Cu",
            175_000,
            (),
            (0, 0, 100_000_000, 0),
            ("USB_P", "USB_N"),
        )
        capture = service.CapturedImage.from_bytes(_png())
        yield SimpleNamespace(
            module=module,
            wx=wx,
            section=section,
            capture=capture,
            queued=queued,
            drain=drain,
        )


def _open_preview(api: SimpleNamespace) -> SimpleNamespace:
    """Attach observable callbacks to the real preview owner boundary."""
    root = PreviewControl()
    state = SimpleNamespace(
        active=True,
        theme="theme-a",
        theme_error=False,
        verifies=0,
        views=[],
        captures=[],
        notices=[],
        failures=[],
        approvals={"F.Cu": True, "B.Cu": True},
    )

    def verify() -> None:
        state.verifies += 1

    def appearance() -> str:
        if state.theme_error:
            raise ValueError("The saved theme could not be read")
        return state.theme

    def invalidate_layers() -> None:
        for layer in state.approvals:
            state.approvals[layer] = False

    def preview(section: object, **kwargs: object) -> object:
        state.captures.append((section, kwargs))
        return api.capture

    def viewed(section: object, digest: str, timestamp: str) -> None:
        state.views.append((section, digest, timestamp))

    def changed() -> None:
        state.notices.append((pane.ready, pane.failure))

    pane = api.module.WorkbookPreview(
        root,
        preview,
        verify_current=verify,
        appearance_context=appearance,
        appearance_changed=invalidate_layers,
        review_failed=lambda: state.failures.append(pane.failure),
        image_viewed=viewed,
        active=lambda: state.active,
        state_changed=changed,
    )
    return SimpleNamespace(root=root, pane=pane, state=state)


def _paint(api: SimpleNamespace, pane: object, *, drain: bool = True) -> None:
    """Dispatch the bound native paint, then optionally its deferred callback."""
    pane.canvas.bindings[api.wx.EVT_PAINT](SimpleNamespace())
    if drain:
        api.drain()


@pytest.mark.parametrize("unavailable", [False, True], ids=["changed", "unreadable"])
def test_palette_failure_revokes_every_layer_and_notifies_retry_state(
    shared_preview_api: SimpleNamespace,
    unavailable: bool,
) -> None:
    """Both unreadable and changed palettes invalidate all layer review evidence."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    sections = (api.section, replace(api.section, section_id="row-b", layer="B.Cu"))
    for section in sections:
        pane.set_sections((section,), section.layer)
        api.drain()
        _paint(api, pane)
        pane.ensure_reviewed()
    assert len(pane._viewed) == 2
    assert len(state.views) == 2
    state.notices.clear()
    if unavailable:
        state.theme_error = True
    else:
        state.theme = "theme-b"

    with pytest.raises(ValueError, match="appearance"):
        pane.ensure_appearance_current()

    assert pane._viewed == {}
    assert pane.canvas._image is None
    assert pane.ready is False
    assert state.approvals == {"F.Cu": False, "B.Cu": False}
    assert pane.failure and pane.status.GetValue() == pane.failure
    assert state.notices[-1] == (False, pane.failure)
    assert len(state.views) == 2
    state.theme_error = False
    with pytest.raises(ValueError):
        pane.ensure_reviewed()
    for section in sections:
        pane.set_sections((section,), section.layer)
        with pytest.raises(ValueError):
            pane.ensure_reviewed()


def test_forced_refresh_is_unreviewed_before_queued_capture_starts(
    shared_preview_api: SimpleNamespace,
) -> None:
    """A queued retry immediately revokes the old image's displayed status."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    pane.set_sections((api.section,), "board-a")
    api.drain()
    _paint(api, pane)
    assert pane.ready is True
    prior_calls = len(state.captures)
    prior_views = len(state.views)

    pane.refresh_button.bindings[api.wx.EVT_BUTTON](SimpleNamespace())

    assert len(state.captures) == prior_calls
    assert len(state.views) == prior_views
    assert pane.ready is False
    assert pane.canvas._image is None
    assert "needs review" in pane.rows.items[0]
    assert state.notices[-1][0] is False
    with pytest.raises(ValueError, match="must be viewed"):
        pane.ensure_reviewed()
    api.drain()
    assert len(state.captures) == prior_calls + 1
    assert state.captures[-1][1].get("refresh") is True
    assert pane.ready is False
    assert len(state.views) == prior_views
    _paint(api, pane)
    assert pane.ready is True
    assert len(state.views) == prior_views + 1


@pytest.mark.parametrize("obstruction", ["hidden", "inactive", "destroyed"])
def test_first_paint_callback_cannot_stamp_after_owner_becomes_unavailable(
    shared_preview_api: SimpleNamespace,
    obstruction: str,
) -> None:
    """Deferred paints cannot stamp hidden, inactive, or destroyed windows."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    pane.set_sections((api.section,), "board-a")
    api.drain()
    _paint(api, pane, drain=False)
    assert state.views == []
    if obstruction == "hidden":
        opened.root.Show(False)
    elif obstruction == "inactive":
        state.active = False
    else:
        pane.canvas.alive = False

    api.drain()

    assert state.views == []
    assert pane.ready is False
    if obstruction != "destroyed":
        opened.root.Show(True)
        state.active = True
        pane.show_selected()
        api.drain()
        _paint(api, pane)
        assert [record[0] for record in state.views] == [api.section]
        assert pane.ready is True


def test_selected_row_replacement_invalidates_old_pending_paint(
    shared_preview_api: SimpleNamespace,
) -> None:
    """A newly selected row cannot inherit an obsolete paint callback's review."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    second = replace(api.section, section_id="row-b", net_names=("OTHER_P", "OTHER_N"))
    pane.set_sections((api.section, second), "board-a")
    api.drain()
    _paint(api, pane, drain=False)

    pane.rows.SetSelection(1)
    pane.rows.bindings[api.wx.EVT_CHOICE](SimpleNamespace(GetSelection=lambda: 1))
    api.drain()

    assert state.views == []
    assert pane.ready is False
    _paint(api, pane)
    assert [record[0] for record in state.views] == [second]
    assert "needs review" in pane.rows.items[0]
    assert pane.checklist.cells[0, 0] == "—"
    assert pane.checklist.cells[1, 0] == "☑"
    assert pane.checklist.colours[0] == (120, 120, 120)
    assert pane.checklist.colours[1] == (0, 150, 50)


def test_repaints_and_resizes_do_not_reread_source_or_stamp_again(
    shared_preview_api: SimpleNamespace,
) -> None:
    """Native sizing and repainting reuse pixels without rescanning or restamping."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    opened.root.Show(False)
    pane.set_sections((api.section,), "board-a")
    api.drain()
    assert state.captures == state.views == []
    opened.root.Show(True)
    pane.show_selected()
    api.drain()
    assert state.views == []
    _paint(api, pane)
    verified, views = state.verifies, list(state.views)
    for size in ((210, 120), (420, 240), (1200, 800)):
        pane.canvas.client_size = size
        skipped: list[bool] = []
        pane.canvas.bindings[api.wx.EVT_SIZE](
            SimpleNamespace(Skip=lambda skipped=skipped: skipped.append(True))
        )
        assert skipped == [True]
        _paint(api, pane)
        _paint(api, pane)
        assert state.verifies == verified
        assert state.views == views
        bitmap, x, y = pane.canvas.drawn[-1]
        ratio = min(size[0] / 800, size[1] / 420, 1.0)
        expected = (max(1, round(800 * ratio)), max(1, round(420 * ratio)))
        assert bitmap.size == expected
        assert (x, y) == ((size[0] - expected[0]) // 2, (size[1] - expected[1]) // 2)
    assert len(state.captures) == 1
