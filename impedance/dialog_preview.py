"""One captured-image display and review component shared by both dialogs."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
import math
from typing import TYPE_CHECKING, Optional, Union

import wx

from .capture_retry import retry_delay_ms
from .model import EXCLUSION_COVERED_STUB, Section, format_length
from .review_tracking import utc_now
from .service import CapturedImage

if TYPE_CHECKING:
    from .spacing_composition import SpacingComposition
    from .spacing_review import SpacingReviewEntry

ReviewEntry = Union[Section, "SpacingReviewEntry"]
PixelRect = tuple[float, float, float, float]


def review_entry_id(entry: ReviewEntry) -> str:
    """Keep diagnostic view identities separate from workbook section identities."""
    return entry.section_id if isinstance(entry, Section) else entry.view_id


@dataclass(frozen=True)
class _ReviewContext:
    """Cache structural hashing while preserving semantic context equality."""

    value: object
    _hash: int = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Hash an immutable board context once, rather than once per row lookup."""
        object.__setattr__(self, "_hash", hash(self.value))

    def __hash__(self) -> int:
        """Return the memoized structural hash for context interning."""
        return self._hash


REVIEW_INSTRUCTION = (
    "All workbook images must be viewed before this signal layer's settings can "
    "be approved."
)


class PreviewCanvas(wx.Panel):
    """Draw captured pixels without letting bitmap sizes drive native layout.

    The first successful visible paint schedules one source-checked display event.
    Resizing and painting themselves never read the board or change review state.
    """

    def __init__(
        self,
        parent: wx.Window,
        displayed: Callable[[], None],
        failed: Callable[[str], None],
        active: Callable[[], bool],
        *,
        max_scale: float = 1.0,
        revalidate_on_resize: bool = False,
        resized: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.SetMinSize((420, 180))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._displayed, self._failed, self._active = displayed, failed, active
        self._max_scale = max(1.0, float(max_scale))
        self._revalidate_on_resize = revalidate_on_resize
        self._resized = resized
        self._painted_size: Optional[tuple[int, int]] = None
        self._presentation_crop: Optional[tuple[int, int, int, int]] = None
        self._image: Optional[wx.Image] = None
        self._bitmap: Optional[wx.Bitmap] = None
        self._dimensions = (0, 0)
        self._scaled_view: Optional[tuple[int, int, int, int]] = None
        self._generation = 0
        self._recorded = False
        self._pending = False
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._resize)
        self.Bind(wx.EVT_LEFT_UP, self._on_click)
        self._enlarge: Optional[Callable[[], None]] = None
        self._visible_source_rect: Optional[PixelRect] = None
        self._painted_scale = 0.0

    def set_enlarge_handler(self, handler: Optional[Callable[[], None]]) -> None:
        """Open a larger review window when the thumbnail is clicked."""
        self._enlarge = handler

    def clear(self) -> None:
        """Invalidate queued display events and discard presentation state."""
        self._generation += 1
        self._image = self._bitmap = None
        self._dimensions = (0, 0)
        self._painted_size = None
        self._scaled_view = None
        self._presentation_crop = None
        self._visible_source_rect = None
        self._painted_scale = 0.0
        self._recorded = self._pending = False
        self.SetCursor(wx.NullCursor)
        self.Refresh()

    def set_capture(self, capture: CapturedImage) -> None:
        """Decode the same immutable PNG that the document will export."""
        self.clear()
        with wx.LogNull():
            image = wx.Image(BytesIO(capture.data), wx.BITMAP_TYPE_PNG)
        if not image.IsOk() or (image.GetWidth(), image.GetHeight()) != (
            capture.width,
            capture.height,
        ):
            raise ValueError("The captured PCB image could not be displayed.")
        self._image = image
        cursor = getattr(wx, "Cursor", None)
        hand = getattr(wx, "CURSOR_HAND", None)
        if cursor is not None and hand is not None:
            self.SetCursor(cursor(hand))
        self.Refresh()

    def _on_click(self, event: wx.MouseEvent) -> None:
        """Open the enlarged review dialog when a capture is visible."""
        if self._image is not None and self._enlarge is not None:
            self._enlarge()
        event.Skip()

    def _paint(self, _event: wx.PaintEvent) -> None:
        """Draw into a fixed layout footprint, retaining full-image context."""
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        if self._image is None or not self._active():
            return
        try:
            available = self.GetClientSize()
            width, height = available.GetWidth(), available.GetHeight()
            if width <= 0 or height <= 0:
                return
            view = self._presentation_crop or (
                0,
                0,
                self._image.GetWidth(),
                self._image.GetHeight(),
            )
            scale = min(width / view[2], height / view[3], self._max_scale)
            dimensions = (
                max(1, round(view[2] * scale)),
                max(1, round(view[3] * scale)),
            )
            if dimensions != self._dimensions or view != self._scaled_view:
                source = self._image
                if self._presentation_crop is not None:
                    source = source.GetSubImage(wx.Rect(*view))
                scaled = source.Scale(*dimensions, wx.IMAGE_QUALITY_HIGH)
                if not scaled.IsOk():
                    raise ValueError("The captured PCB image could not be resized.")
                self._bitmap = wx.Bitmap(scaled)
                if not self._bitmap.IsOk():
                    raise ValueError("The captured PCB bitmap could not be created.")
                self._dimensions = dimensions
                self._scaled_view = view
            origin = (width - dimensions[0]) // 2, (height - dimensions[1]) // 2
            dc.DrawBitmap(
                self._bitmap,
                *origin,
                True,
            )
            self._painted_size = (width, height)
            self._painted_scale = min(dimensions[0] / view[2], dimensions[1] / view[3])
            self._visible_source_rect = tuple(float(value) for value in view)
            if not self._recorded and not self._pending and self.IsShownOnScreen():
                self._pending = True
                wx.CallAfter(self._after_paint, self._generation)
        except Exception as error:
            # Control updates belong outside native painting.
            wx.CallAfter(self._paint_failed, self._generation, str(error))

    def _paint_failed(self, generation: int, message: str) -> None:
        """Ignore deferred failures belonging to an image that was replaced."""
        if self and generation == self._generation:
            self._failed(message)

    def _after_paint(self, generation: int) -> None:
        """Report a real current display once, never a hidden or obsolete paint."""
        if not self or generation != self._generation:
            return
        self._pending = False
        if (
            self._recorded
            or self._image is None
            or not self._active()
            or not self.IsShownOnScreen()
        ):
            return
        try:
            self._displayed()
            if generation == self._generation:
                self._recorded = True
        except Exception as error:
            if generation == self._generation:
                self._failed(str(error))

    def require_fresh_display(self) -> None:
        """Invalidate queued paint proof while retaining the exact image pixels."""
        self._generation += 1
        self._recorded = self._pending = False
        self._painted_size = None
        self.Refresh()

    def _resize(self, event: wx.SizeEvent) -> None:
        """Let the next paint refit pixels without reading or changing the board."""
        size = self.GetClientSize()
        current_size = (size.GetWidth(), size.GetHeight())
        if self._revalidate_on_resize and current_size != self._painted_size:
            self.require_fresh_display()
            if self._resized is not None:
                self._resized()
        self.Refresh()
        event.Skip()


def _display_client_area(parent: wx.Window) -> tuple[int, int, int, int]:
    """Return the selected display's usable on-screen work area."""
    try:
        index = max(wx.Display.GetFromWindow(parent), 0)
        area = wx.Display(index).GetClientArea()
        return int(area.x), int(area.y), int(area.GetWidth()), int(area.GetHeight())
    except Exception:
        return 0, 0, 1280, 800


def _display_client_bounds(parent: wx.Window) -> tuple[int, int]:
    """Return a usable on-screen work area for sizing the enlarge dialog."""
    return _display_client_area(parent)[2:]


class EnlargedCaptureDialog(wx.Dialog):
    """Show one immutable review image large enough for careful inspection."""

    def __init__(
        self,
        parent: wx.Window,
        capture: CapturedImage,
        title: str,
        *,
        caption: str = "",
        verify_current: Optional[Callable[[], None]] = None,
        displayed: Optional[Callable[[], None]] = None,
        approve: Optional[Callable[[], None]] = None,
        active: Optional[Callable[[], bool]] = None,
        failed: Optional[Callable[[str], None]] = None,
        composition: Optional[SpacingComposition] = None,
    ) -> None:
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE
            | wx.RESIZE_BORDER
            | getattr(wx, "MAXIMIZE_BOX", 0),
        )
        self.approved = False
        self._verify_current = verify_current
        self._displayed = displayed
        self._approve = approve
        self._active = active or (lambda: True)
        self._failed = failed
        self._display_verified = False
        self._view_recorded = False
        self._capture = capture
        self._composition = composition
        self._failure = ""
        self._caption_text = caption
        self._review_status_text = (
            "Review this spacing image, then approve the spacing exception or cancel."
            if approve is not None
            else ""
        )
        self._review_wrap_key: object = None
        work_area = _display_client_area(parent)
        max_width, max_height = work_area[2:]
        max_width = max(320, int(max_width * 0.9))
        max_height = max(240, int(max_height * 0.9) - 80)
        if capture.width <= max_width and capture.height <= max_height:
            width, height = capture.width, capture.height
        else:
            scale = min(max_width / capture.width, max_height / capture.height)
            width = max(1, int(round(capture.width * scale)))
            height = max(1, int(round(capture.height * scale)))
        self.canvas = PreviewCanvas(
            self,
            self._on_canvas_displayed if approve is not None else lambda: None,
            self._on_canvas_failed if approve is not None else lambda _message: None,
            self._active,
            max_scale=8.0,
            revalidate_on_resize=approve is not None,
            resized=self._on_canvas_resized if approve is not None else None,
        )
        self.canvas.SetMinSize((width, height))
        self.canvas.set_capture(capture)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.caption = wx.StaticText(self, label=caption)
        self.caption.Wrap(max_width)
        self.caption.Show(bool(caption))
        layout.Add(self.caption, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(self.canvas, 1, wx.EXPAND | wx.ALL, 8)
        navigation = getattr(wx, "WrapSizer", wx.BoxSizer)(wx.HORIZONTAL)
        self.approve_button: Optional[wx.Button] = None
        self.status = wx.StaticText(
            self,
            label=self._review_status_text,
        )
        self.status.Show(approve is not None)
        layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        if approve is not None:
            self.approve_button = wx.Button(self, label="Approve spacing exception")
            self.approve_button.Enable(False)
            self.approve_button.Bind(wx.EVT_BUTTON, self._on_approve)
            navigation.Add(self.approve_button, 0, wx.RIGHT, 8)
        buttons = self.CreateButtonSizer(wx.CANCEL if approve is not None else wx.OK)
        if buttons is not None:
            navigation.Add(buttons, 0)
        layout.Add(navigation, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizerAndFit(layout)
        self._fit_review_work_area((width, height), work_area)
        if approve is not None:
            self.Bind(wx.EVT_MOVE, self._on_move)
            self.Bind(wx.EVT_ICONIZE, self._on_iconize)
        self._wrap_review_labels()
        self.Bind(wx.EVT_SIZE, self._on_window_size)

    def _wrap_review_labels(self) -> None:
        """Keep captions and status inside narrow native windows."""
        width = max(180, self.GetClientSize().GetWidth() - 24)
        key = width, self._caption_text, self._review_status_text
        if self._review_wrap_key == key:
            return
        # wx.Layout may synchronously emit another same-size event. Cache before
        # changing native controls, and always rewrap the original text.
        self._review_wrap_key = key
        self.caption.SetLabel(self._caption_text)
        self.caption.Wrap(width)
        self.status.SetLabel(self._review_status_text)
        self.status.Wrap(width)
        self.Layout()

    def _on_window_size(self, event: wx.SizeEvent) -> None:
        """Reflow controls while native painting automatically refits the full image."""
        self._wrap_review_labels()
        event.Skip()

    def _set_review_status(self, message: str) -> None:
        """Wrap each changed message using the current native client width."""
        self._review_status_text = message
        self._wrap_review_labels()

    def _fit_review_work_area(
        self,
        image_size: tuple[int, int],
        area: tuple[int, int, int, int],
    ) -> None:
        """Reserve measured native controls and chrome before fitting the image."""
        fitted = self.GetSize()
        overhead = (
            max(0, fitted.GetWidth() - image_size[0]),
            max(0, fitted.GetHeight() - image_size[1]),
        )
        minimum_scale = (
            self._minimum_review_scale() if self._approve is not None else 0.1
        )
        minimum_canvas = (
            max(1, math.ceil(self._capture.width * minimum_scale)),
            max(1, math.ceil(self._capture.height * minimum_scale)),
        )
        self.canvas.SetMinSize(minimum_canvas)
        minimum_window = (
            minimum_canvas[0] + overhead[0],
            minimum_canvas[1] + overhead[1],
        )
        self.SetMinSize(minimum_window)
        desired = (
            min(area[2], max(minimum_window[0], int(area[2] * 0.94))),
            min(area[3], max(minimum_window[1], int(area[3] * 0.94))),
        )
        self.SetSize(desired)
        self.Layout()
        fitted = self.GetSize()
        self.SetPosition(
            (
                area[0] + max(0, (area[2] - fitted.GetWidth()) // 2),
                area[1] + max(0, (area[3] - fitted.GetHeight()) // 2),
            )
        )

    def _minimum_review_scale(self) -> float:
        """Use the composition's source feature contract for full-image review."""
        if self._composition is not None:
            return self._composition.minimum_scale
        from .spacing_review import (
            MIN_SPACING_DISPLAY_FEATURE_PIXELS,
            MIN_SPACING_FEATURE_PIXELS,
        )

        return MIN_SPACING_DISPLAY_FEATURE_PIXELS / MIN_SPACING_FEATURE_PIXELS

    def _on_move(self, event: wx.MoveEvent) -> None:
        """Recheck full visibility after moving the review window on screen."""
        self.canvas.require_fresh_display()
        self._on_canvas_resized()
        event.Skip()

    def _on_iconize(self, event: wx.IconizeEvent) -> None:
        """Require fresh Fit proof after minimize or restore, even at equal bounds."""
        self.canvas.require_fresh_display()
        self._on_canvas_resized()
        event.Skip()

    def _canvas_fully_visible(self) -> bool:
        """Reject image or approval controls clipped by an ancestor or display."""
        index = wx.Display.GetFromWindow(self)
        if index < 0:
            return False
        display = wx.Display(index).GetClientArea()
        cancel = self.FindWindow(wx.ID_CANCEL)
        if cancel is None or self.approve_button is None:
            return False
        controls = [self.canvas, self.approve_button, cancel]
        controls.extend(
            control
            for control in (self.caption, self.status)
            if control.IsShownOnScreen()
        )
        for control in controls:
            bounds = control.GetScreenRect()
            left, top, right, bottom = (
                bounds.x,
                bounds.y,
                bounds.x + bounds.width,
                bounds.y + bounds.height,
            )

            def contains(
                x: int,
                y: int,
                width: int,
                height: int,
                current: tuple[int, int, int, int] = (left, top, right, bottom),
            ) -> bool:
                """Compare one visible control with its actual enclosing rectangle."""
                return (
                    x <= current[0]
                    and y <= current[1]
                    and current[2] <= x + width
                    and current[3] <= y + height
                )

            if not control.IsShownOnScreen() or not contains(
                display.x, display.y, display.width, display.height
            ):
                return False
            parent = control.GetParent()
            while parent is not None:
                frame = parent.GetScreenRect()
                if not contains(frame.x, frame.y, frame.width, frame.height):
                    return False
                x, y = parent.ClientToScreen(0, 0)
                client = parent.GetClientSize()
                if not contains(x, y, client.GetWidth(), client.GetHeight()):
                    return False
                if parent is self:
                    break
                parent = parent.GetParent()
            if parent is not self:
                return False
        return True

    def _geometry_readable(self) -> bool:
        """Require current visible pixels at the appropriate readable scale."""
        canvas, capture = self.canvas, self._capture
        size = canvas.GetClientSize()
        current_size = (size.GetWidth(), size.GetHeight())
        full_source = (0, 0, capture.width, capture.height)
        return bool(
            canvas._presentation_crop is None
            and canvas._scaled_view == full_source
            and canvas._visible_source_rect == full_source
            and canvas._painted_size == current_size
            and canvas._painted_scale + 1e-9 >= self._minimum_review_scale()
        )

    @property
    def readable(self) -> bool:
        """Require one current legible full-image paint and visible consent controls."""
        return bool(
            self._display_verified
            and not self._failure
            and self._active()
            and self.canvas._recorded
            and self.canvas.IsShownOnScreen()
            and self._geometry_readable()
            and self._canvas_fully_visible()
        )

    def _on_canvas_displayed(self) -> None:
        """Record readable display only after the native buffered paint completes."""
        self._display_verified = False
        if self._verify_current is not None:
            self._verify_current()
        if (
            not self._active()
            or not self._geometry_readable()
            or not self._canvas_fully_visible()
        ):
            self._set_review_status(
                "The full spacing image does not fit legibly in this window. "
                "Use a larger window or cancel."
            )
            wx.CallAfter(self._refresh_approval_state)
            return
        if self._displayed is not None and not self._view_recorded:
            self._displayed()
            self._view_recorded = True
        if not self._active():
            return
        self._display_verified = True
        self._failure = ""
        self._set_review_status("Review this spacing image, then approve it or cancel.")
        # PreviewCanvas commits its visible-paint flag after this callback.
        wx.CallAfter(self._refresh_approval_state)

    def _refresh_approval_state(self) -> None:
        """Enable consent only after completed paint and current geometry checks."""
        if self and self.approve_button is not None:
            self.approve_button.Enable(self.readable)

    def _on_canvas_resized(self) -> None:
        """Require another full readable paint after native viewport changes."""
        self._display_verified = False
        self._refresh_approval_state()

    def _on_canvas_failed(self, message: str) -> None:
        """Keep failed or stale large images outside diagnostic approval evidence."""
        self._failure = message or "The spacing image could not be displayed."
        self._display_verified = False
        if self._failed is not None and self._active():
            self._failed(self._failure)
        if self:
            self._set_review_status(self._failure)
            self._refresh_approval_state()

    def _on_approve(self, event: wx.CommandEvent) -> None:
        """Approve only this currently readable source-checked full-image paint."""
        try:
            if self._verify_current is not None:
                self._verify_current()
            if not self.readable or self._approve is None:
                self._set_review_status(
                    "Open and review the enlarged spacing image before approving it."
                )
                self._refresh_approval_state()
                return
            self._approve()
            self.approved = True
            self.EndModal(wx.ID_OK)
        except Exception as error:
            self._on_canvas_failed(str(error))


class WorkbookPreview:
    """Present row captures; share display and view evidence across both UIs."""

    def __init__(
        self,
        parent: wx.Window,
        preview: Optional[Callable[..., CapturedImage]],
        *,
        verify_current: Optional[Callable[[], None]] = None,
        appearance_context: Optional[Callable[[], object]] = None,
        appearance_changed: Optional[Callable[[], None]] = None,
        review_failed: Optional[Callable[[], None]] = None,
        image_viewed: Optional[Callable[[Section, str, str], None]] = None,
        spacing_image_viewed: Optional[
            Callable[[SpacingReviewEntry, str, str], None]
        ] = None,
        approved_images: Optional[Callable[[], set[str]]] = None,
        active: Optional[Callable[[], bool]] = None,
        image_list: Optional[wx.ListCtrl] = None,
        user_selected: Optional[Callable[[], None]] = None,
        state_changed: Optional[Callable[[], None]] = None,
        skip_stub_review: Optional[Callable[[], bool]] = None,
        open_spacing_review: Optional[Callable[[], None]] = None,
        show_spacing_review_action: bool = True,
    ) -> None:
        self._preview, self._verify_current = preview, verify_current
        self._appearance_context, self._appearance_changed = (
            appearance_context,
            appearance_changed,
        )
        self._review_failed, self._image_viewed = review_failed, image_viewed
        self._spacing_image_viewed = spacing_image_viewed
        self._open_spacing_review = open_spacing_review
        self._show_spacing_review_action = show_spacing_review_action
        self._approved_images = approved_images
        self._active = active or (lambda: True)
        self._state_changed = state_changed or (lambda: None)
        self._user_selected = user_selected
        self._skip_stub_review = skip_stub_review or (lambda: True)
        self._sections: tuple[Section, ...] = ()
        self._spacing_entries: tuple[SpacingReviewEntry, ...] = ()
        self._entries: tuple[ReviewEntry, ...] = ()
        self._context_key: object = None
        self._context_tokens: dict[_ReviewContext, object] = {}
        self._bound_context: Optional[_ReviewContext] = None
        self._context_token: object = None
        self._appearance_token = object()
        self._appearance_key: object = None
        self._appearance_known = False
        self._index = -1
        self._request = 0
        self._failure = ""
        self._display: Optional[tuple[object, object, ReviewEntry]] = None
        self._capture: Optional[CapturedImage] = None
        self._viewed: dict[tuple[object, object, ReviewEntry], str] = {}
        self._updating_checklist = False
        self._review_rows: list[tuple[str, str, object]] = []
        self._closed = False
        self._review_window_depth = 0
        self._deferred_selected: Optional[tuple[int, bool]] = None
        self._retry_attempt = 0
        self._retry_request = -1
        self._status_text = ""
        self._background_status = ""
        self.window = wx.Panel(parent)
        self._retry_timer = wx.Timer(self.window)
        self.window.Bind(wx.EVT_TIMER, self._on_retry_timer, self._retry_timer)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.window.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._on_system_colours)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.title = wx.StaticText(self.window, label="Workbook image preview")
        layout.Add(self.title, 0, wx.BOTTOM, 6)
        self.open_spacing_button = wx.Button(self.window, label="Open spacing review")
        self.open_spacing_button.Bind(
            wx.EVT_BUTTON, lambda _event: self._open_enlarged_capture()
        )
        self.open_spacing_button.Hide()
        layout.Add(self.open_spacing_button, 0, wx.BOTTOM | wx.EXPAND, 6)
        self.caption = wx.StaticText(self.window, label="")
        self.caption.Hide()
        layout.Add(self.caption, 0, wx.BOTTOM | wx.EXPAND, 6)
        self.checklist = image_list
        if self.checklist is not None:
            self.checklist.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_list_selection)
        self.canvas = PreviewCanvas(
            self.window, self._record_display, self._report_failure, self._active
        )
        self.canvas.set_enlarge_handler(self._open_enlarged_capture)
        layout.Add(self.canvas, 1, wx.EXPAND)
        self.status = wx.TextCtrl(
            self.window,
            value="",
            size=(-1, 48),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        layout.Add(self.status, 0, wx.TOP | wx.EXPAND, 8)
        self.window.SetSizer(layout)
        self.clear("Complete the specification to preview its workbook images.")

    @property
    def review_window_open(self) -> bool:
        """Report enlarged-window lifetime without deactivating source or paint guards."""
        return self._review_window_depth > 0

    @contextmanager
    def _review_window_lifetime(self) -> Iterator[None]:
        """Pause parent preparation through nested construction, viewing and teardown."""
        self._review_window_depth += 1
        try:
            if self._review_window_depth == 1:
                self._state_changed()
            yield
        finally:
            self._review_window_depth -= 1
            if self._review_window_depth == 0:
                deferred, self._deferred_selected = self._deferred_selected, None
                self._state_changed()
                if (
                    deferred is not None
                    and not self._closed
                    and self.window
                    and deferred[0] == self._request
                ):
                    wx.CallAfter(self._show_selected, *deferred)

    @property
    def sections(self) -> tuple[Section, ...]:
        """Return the current ordered workbook sections."""
        return self._sections

    @property
    def entries(self) -> tuple[ReviewEntry, ...]:
        """Return display entries while keeping manufacturing sections separate."""
        return self._entries

    @property
    def spacing_entries(self) -> tuple[SpacingReviewEntry, ...]:
        """Return diagnostic captures, which never become workbook sections."""
        return self._spacing_entries

    @property
    def selected_entry(self) -> Optional[ReviewEntry]:
        """Identify the selected capture independently of manufacturing geometry."""
        return (
            self._entries[self._index]
            if 0 <= self._index < len(self._entries)
            else None
        )

    @property
    def failure(self) -> str:
        """Return the inline explanation of the most recent preview failure."""
        return self._failure

    @property
    def ready(self) -> bool:
        """A visible capture has completed its source-checked display event."""
        return bool(
            0 <= self._index < len(self._entries)
            and self._display == self._key(self._index)
            and (
                self._display in self._viewed
                if isinstance(self._entries[self._index], Section)
                else self.canvas._recorded
            )
            and not self._failure
        )

    @property
    def capture_loaded(self) -> bool:
        """Return whether the latest selection has pixels ready for native paint."""
        return bool(
            0 <= self._index < len(self._entries)
            and self._display == self._key(self._index)
            and self._capture is not None
        )

    @property
    def selected_section(self) -> Optional[Section]:
        """Identify the selected row even when loading or native display failed."""
        entry = self.selected_entry
        if isinstance(entry, Section):
            return entry
        if entry is None:
            return None
        return next(
            (
                section
                for section in self._sections
                if section.section_id in entry.parent_section_ids
            ),
            None,
        )

    @property
    def displayed(self) -> bool:
        """Require the selected canvas to have painted visibly for a new approval."""
        return self.ready and self.canvas._recorded and self.window.IsShownOnScreen()

    def _key(self, index: int) -> tuple[object, object, ReviewEntry]:
        return self._context_token, self._appearance_token, self._entries[index]

    def _requires_review(self, index: int) -> bool:
        """Return whether this preview row must be viewed before layer approval."""
        if not 0 <= index < len(self._entries):
            return False
        if (
            self._skip_stub_review()
            and isinstance(self._entries[index], Section)
            and self._entries[index].exclusion_reason == EXCLUSION_COVERED_STUB
        ):
            return False
        return True

    def _default_section_index(self, selected_id: Optional[str]) -> int:
        """Prefer an explicit id, otherwise the first row that still needs review."""
        if selected_id is not None:
            for index, section in enumerate(self._entries):
                if review_entry_id(section) == selected_id:
                    return index
        for index in range(len(self._entries)):
            if self._requires_review(index):
                return index
        return 0

    def image_digest(self, section: ReviewEntry) -> Optional[str]:
        """Return a loaded or previously viewed capture hash in this context."""
        key = self._context_token, self._appearance_token, section
        return (
            self._capture.sha256
            if key == self._display and self._capture
            else self._viewed.get(key)
        )

    def clear(
        self, message: str, *, error: bool = False, preserve_review: bool = False
    ) -> None:
        """Remove obsolete presentation, optionally retaining layer evidence."""
        self._retry_timer.Stop()
        self._retry_attempt = 0
        self._request += 1
        self._deferred_selected = None
        self._sections, self._entries, self._spacing_entries = (), (), ()
        self._index, self._display = -1, None
        self._capture = None
        self._failure = message if error else ""
        if not preserve_review:
            self._viewed.clear()
            self._context_tokens.clear()
        self._review_rows.clear()
        self.canvas.clear()
        self._refresh_presentation()
        if self.checklist is not None:
            self._updating_checklist = True
            try:
                self.checklist.DeleteAllItems()
                self.checklist.Enable(False)
            finally:
                self._updating_checklist = False
        self._set_status(message)

    def set_sections(
        self,
        sections: tuple[Section, ...],
        context_key: object,
        selected_id: Optional[str] = None,
        *,
        spacing_entries: tuple[SpacingReviewEntry, ...] = (),
    ) -> None:
        """Bind spacing and workbook rows atomically before selecting any capture."""
        same_context = (
            context_key is self._context_key or context_key == self._context_key
        )
        if (
            sections == self._sections
            and spacing_entries == self._spacing_entries
            and same_context
        ):
            self._context_key = context_key
            if selected_id is not None:
                self.select(self._default_section_index(selected_id))
            return
        if selected_id is None and same_context and self.selected_entry is not None:
            selected_id = review_entry_id(self.selected_entry)
        self.clear("Preparing review image…", preserve_review=True)
        self._sections = sections
        self._spacing_entries = spacing_entries
        self._entries = spacing_entries + sections
        self._context_key = context_key
        context = self._bound_context
        if context is None or context.value != context_key:
            context = _ReviewContext(context_key)
        self._bound_context = context
        self._context_token = self._context_tokens.setdefault(context, object())
        # Unseen intermediate revisions need no retained board-sized context.
        # Viewed contexts keep their token so equal reconstructed inputs can
        # restore evidence without rehashing or comparing the board per row.
        retained = {key[0] for key in self._viewed}
        retained.add(self._context_token)
        self._context_tokens = {
            value: token
            for value, token in self._context_tokens.items()
            if token in retained
        }
        if self.checklist is not None:
            self.checklist.Enable(bool(self._entries))
        self.refresh_review_labels()
        if self._entries:
            self.select(self._default_section_index(selected_id))
        else:
            self._set_status("No matching routed sections to preview.")

    def set_spacing_entries(self, entries: tuple[SpacingReviewEntry, ...]) -> None:
        """Update diagnostic-first entries while retaining the selected identity."""
        if entries == self._spacing_entries:
            return
        selected = self.selected_entry
        selected_id = review_entry_id(selected) if selected is not None else None
        self._spacing_entries = entries
        self._entries = entries + self._sections
        self._request += 1
        self.canvas.clear()
        self._display, self._capture = None, None
        if self.checklist is not None:
            self.checklist.Enable(bool(self._entries))
        self.refresh_review_labels()
        if self._entries:
            self.select(self._default_section_index(selected_id))
        else:
            self._index = -1
            self._refresh_presentation()

    def restore_spacing_viewed(
        self,
        matches: Callable[[SpacingReviewEntry, str], bool],
        capture_hashes: Mapping[SpacingReviewEntry, str],
    ) -> None:
        """Restore only independently verified diagnostic PNG evidence."""
        if self._appearance_context is not None:
            try:
                self.ensure_appearance_current()
            except ValueError:
                return
        for index, entry in enumerate(self._entries):
            if isinstance(entry, Section):
                continue
            image_hash = capture_hashes.get(entry)
            if image_hash is not None and matches(entry, image_hash):
                self._viewed[self._key(index)] = image_hash
        self.refresh_review_labels()

    def select(self, index: int, *, refresh: bool = False) -> None:
        """Queue only the latest selected row after native show/layout."""
        if self._closed or not 0 <= index < len(self._entries):
            return
        self._retry_timer.Stop()
        if index != self._index:
            self._retry_attempt = 0
        if refresh or index != self._index:
            # Revoke pending display callbacks immediately, before CallAfter can
            # deliver a forced retry or a newly selected row's render request.
            self.canvas.clear()
            self._display, self._capture = None, None
        if refresh:
            self._viewed.pop(self._key(index), None)
        self._index = index
        self._refresh_presentation()
        self.refresh_review_labels()
        self._request += 1
        wx.CallAfter(self._show_selected, self._request, refresh)
        self._state_changed()

    def show_selected(self) -> None:
        """Resume after the owning dialog is shown or a child dialog closes."""
        if self._index >= 0:
            self.select(self._index)

    def _verify(self) -> None:
        if self._verify_current is not None:
            self._verify_current()
        self.ensure_appearance_current()

    def ensure_appearance_current(self) -> None:
        """Check palette at action boundaries, never on resize."""
        if self._appearance_context is None:
            return
        try:
            current = self._appearance_context()
        except Exception as error:
            message = f"KiCad appearance is unavailable: {error}. The preview will retry automatically; review the images again."
            self._invalidate_appearance(message)
            raise ValueError(message) from error
        changed = self._appearance_known and current != self._appearance_key
        self._appearance_key, self._appearance_known = current, True
        if changed:
            message = "KiCad appearance changed. The preview will retry automatically; review the layer images again."
            self._invalidate_appearance(message)
            raise ValueError(message)

    def _invalidate_appearance(self, message: str) -> None:
        """Unavailable and changed themes both invalidate every layer's evidence."""
        self._viewed.clear()
        self._appearance_token = object()
        self._context_tokens = {
            value: token
            for value, token in self._context_tokens.items()
            if token is self._context_token
        }
        self.canvas.clear()
        self._display, self._capture = None, None
        self._refresh_presentation()
        self._failure = message
        self._set_status(message)
        self.refresh_review_labels()
        if self._appearance_changed is not None:
            self._appearance_changed()
        self._state_changed()
        self._schedule_retry()

    def _show_selected(self, request: int, refresh: bool = False) -> None:
        if (
            not self.window
            or self._closed
            or request != self._request
            or not self._active()
            or not self.window.IsShownOnScreen()
        ):
            return
        if self.review_window_open:
            previous = self._deferred_selected
            self._deferred_selected = (
                request,
                refresh or bool(previous and previous[0] == request and previous[1]),
            )
            return
        try:
            self.ensure_appearance_current()
            if request != self._request:
                return
            key = self._key(self._index)
            if key == self._display and not refresh and not self._failure:
                self._verify()
                if request != self._request:
                    return
                self.canvas.Refresh()
                return
            self._failure = ""
            self._display, self._capture = None, None
            self.canvas.clear()
            self._refresh_presentation()
            self._viewed.pop(key, None)
            self.refresh_review_labels()
            spacing = not isinstance(self._entries[self._index], Section)
            kind = "spacing" if spacing else "workbook"
            self._set_status(
                f"{self._entry_label(self._index)}: rendering {kind} capture…"
            )
            if self._preview is None:
                raise ValueError("No production preview renderer is available.")
            # The provider checks before capture. The owning dialog validates
            # against its rows before installing pixels, then again after paint.
            capture = self._preview(self._entries[self._index], refresh=refresh)
            self._verify()
            if request != self._request:
                return
            self._display, self._capture = key, capture
            self.canvas.set_capture(capture)
            self._refresh_presentation()
            self._set_status(f"Waiting for the {kind} image to be displayed…")
            self._state_changed()
        except Exception as error:
            # An optional image's render failure must not hide an independent
            # board or appearance change, which revokes the whole review.
            message = str(error)
            try:
                self._verify()
            except Exception as verification_error:
                message = str(verification_error)
            if self.window and self._active() and request == self._request:
                self._report_failure(message)

    def _record_display(self) -> None:
        """Verify once after real display, then publish the image timestamp."""
        key = self._display
        if self._closed or key is None or not self._active():
            return
        self._verify()
        if self._display != key:
            return
        capture = self._capture
        if capture is None:
            return
        entry = key[2]
        if isinstance(entry, Section):
            if self._image_viewed is not None:
                self._image_viewed(entry, capture.sha256, utc_now())
            self._viewed[key] = capture.sha256
        self._retry_timer.Stop()
        self._retry_attempt = 0
        self.refresh_review_labels()
        self._set_status("")
        # Canvas visibility is committed by _after_paint immediately after this
        # callback returns. Notify controls on the next turn so explicit approve
        # buttons observe that completed visible-paint state.
        wx.CallAfter(self._state_changed)

    def _refresh_presentation(self) -> None:
        """Show the selected capture's title, physical detail and external caption."""
        self.canvas.Refresh()
        entry = self.selected_entry
        spacing = entry is not None and not isinstance(entry, Section)
        self.title.SetLabel(
            "Spacing review image" if spacing else "Workbook image preview"
        )
        self.caption.SetLabel(entry.caption if spacing else "")
        composition = getattr(entry, "composition", None) if spacing else None
        capture = self._capture
        if (
            composition is not None
            and capture is not None
            and (capture.width, capture.height) == (entry.width_px, entry.height_px)
        ):
            self.canvas._presentation_crop = composition.detail_rect
            actual, target = composition.actual_gap_nm, entry.target_spacing_nm
            if actual is not None and target is not None:
                actual_mm = f"{actual / 1_000_000:.6f}".rstrip("0").rstrip(".")
                self.caption.SetLabel(
                    f"Shown gap {actual_mm} mm · target {format_length(target)} mm"
                )
            elif target is not None:
                region = composition.region_gap_nm
                if region is None:
                    summary = "Gap details in spacing review"
                else:
                    lower = f"{region.lower_nm / 1_000_000:.6f}".rstrip("0").rstrip(".")
                    upper = f"{region.upper_nm / 1_000_000:.6f}".rstrip("0").rstrip(".")
                    summary = f"Region gap {lower if lower == upper else lower + '–' + upper} mm"
                self.caption.SetLabel(f"{summary} · target {format_length(target)} mm")
        else:
            self.canvas._presentation_crop = None
        self.caption.Wrap(max(120, self.window.GetClientSize().GetWidth()))
        self.caption.Show(spacing)
        self.title.Show(
            not spacing
            or self._open_spacing_review is None
            or not self._show_spacing_review_action
        )
        self.open_spacing_button.Show(spacing and self._show_spacing_review_action)
        self.open_spacing_button.Enable(
            spacing and capture is not None and self._open_spacing_review is not None
        )
        self.window.Layout()

    def _report_failure(self, message: str) -> None:
        """Discard failed pixels/evidence before notifying the review owner."""
        if self._closed or not self.window:
            return
        if self._display is not None:
            self._viewed.pop(self._display, None)
        self._display, self._capture = None, None
        self.canvas.clear()
        self._refresh_presentation()
        kind = (
            "Spacing"
            if self.selected_entry is not None
            and not isinstance(self.selected_entry, Section)
            else "Workbook"
        )
        self._failure = kind + " preview unavailable: " + (message or "Please retry.")
        self._set_status(self._failure)
        self.refresh_review_labels()
        if self._review_failed is not None:
            self._review_failed()
        self._schedule_retry()
        self._state_changed()

    def _set_status(self, message: str) -> None:
        """Keep the selected preview's message separate from background recovery."""
        self._status_text = message
        text = "\n".join(text for text in (message, self._background_status) if text)
        if self.status.GetValue() != text:
            self.status.SetValue(text)

    def set_background_status(self, message: str) -> bool:
        """Surface hidden capture failures without replacing selected-image errors."""
        if self._closed or not self.window or message == self._background_status:
            return False
        self._background_status = message
        self._set_status(self._status_text)
        return True

    def _entry_label(self, index: int) -> str:
        """Return independent group numbering in constant time."""
        if isinstance(self._entries[index], Section):
            return f"Image {index - len(self._spacing_entries) + 1}"
        return f"Spacing image {index + 1}"

    def refresh_review_labels(self) -> None:
        """Update rows in place, preserving native selection, focus and scrolling."""
        if self.checklist is None:
            return
        approved = (
            self._approved_images()
            if self._approved_images is not None and self._entries
            else None
        )
        self._updating_checklist = True
        try:
            if self.checklist.GetItemCount() != len(self._entries):
                self.checklist.DeleteAllItems()
                self._review_rows.clear()
                for index in range(len(self._entries)):
                    self.checklist.InsertItem(index, "—")
            normal_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOXTEXT)
            stub_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
            background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOX)
            dark = sum(background[channel] for channel in range(3)) < 384
            approved_colour = wx.Colour(*((70, 210, 110) if dark else (0, 120, 40)))
            for index, section in enumerate(self._entries):
                viewed = self._key(index) in self._viewed
                checked = (
                    viewed if approved is None else review_entry_id(section) in approved
                )
                state = "viewed" if viewed else "needs review"
                if approved is not None and checked:
                    state = "approved"
                stub = (
                    isinstance(section, Section)
                    and section.exclusion_reason == EXCLUSION_COVERED_STUB
                )
                if isinstance(section, Section):
                    width_or_stub = (
                        "Covered stub"
                        if stub
                        else f"{format_length(section.width_nm)} mm"
                    )
                    description = (
                        f"{self._entry_label(index)} · {section.layer} · "
                        f"{', '.join(section.net_names)} · {width_or_stub} — {state}"
                    )
                else:
                    description = (
                        f"{self._entry_label(index)} · {section.layer} · "
                        f"{', '.join(section.pair)} — {state}"
                    )
                colour = (
                    stub_colour
                    if stub
                    else approved_colour
                    if checked
                    else normal_colour
                )
                row = ("☑" if checked else "—", description, colour)
                previous = (
                    self._review_rows[index] if index < len(self._review_rows) else None
                )
                if previous is None or row[0] != previous[0]:
                    self.checklist.SetItem(index, 0, row[0])
                if previous is None or row[1] != previous[1]:
                    self.checklist.SetItem(index, 1, row[1])
                if previous is None or row[2] != previous[2]:
                    self.checklist.SetItemTextColour(index, row[2])
                if previous is None:
                    self._review_rows.append(row)
                else:
                    self._review_rows[index] = row
            if (
                0 <= self._index < len(self._entries)
                and self.checklist.GetFirstSelected() != self._index
            ):
                self.checklist.Select(self._index)
                self.checklist.Focus(self._index)
                self.checklist.EnsureVisible(self._index)
        finally:
            self._updating_checklist = False

    def _schedule_retry(self) -> None:
        """Retry transient preview failures without spinning or fabricating a view."""
        if (
            self._closed
            or not self.window
            or not self._active()
            or not self._entries
            or self._preview is None
            or self._retry_timer.IsRunning()
        ):
            return
        self._retry_attempt += 1
        self._retry_request = self._request
        self._retry_timer.StartOnce(retry_delay_ms(self._retry_attempt))

    def _on_retry_timer(self, event: wx.TimerEvent) -> None:
        """Retry only the still-selected image in the live visible context."""
        if (
            self._closed
            or not self.window
            or not self._active()
            or self._retry_request != self._request
            or not self.window.IsShownOnScreen()
        ):
            return
        self._show_selected(self._request, refresh=True)

    def close(self) -> None:
        """Cancel owned retries and queued display work before the owner closes."""
        self._closed = True
        self._request += 1
        self._deferred_selected = None
        self._retry_timer.Stop()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        """Stop the native timer when its owner is destroyed independently."""
        if event.GetEventObject() is self.window:
            self.close()
        event.Skip()

    def _on_system_colours(self, event: wx.SysColourChangedEvent) -> None:
        """Reapply native text roles without changing capture or review evidence."""
        if self.window and not self._closed:
            self.refresh_review_labels()
        event.Skip()

    def ensure_reviewed(self) -> None:
        """Never approve missing, failed, or merely queued workbook images."""
        self._verify()
        if self._failure:
            raise ValueError(self._failure)
        if not self._entries or self._preview is None:
            raise ValueError("No workbook images are available for approval.")
        missing = [
            self._entry_label(index)
            for index in range(len(self._entries))
            if self._requires_review(index) and self._key(index) not in self._viewed
        ]
        if missing:
            raise ValueError(
                REVIEW_INSTRUCTION
                + " Images still needing review: "
                + ", ".join(missing)
                + "."
            )

    def advance_to_unreviewed(self, approved: Optional[set[str]] = None) -> bool:
        """Reveal the next pending image; require a later click to approve it."""
        self._verify()
        if self._failure:
            raise ValueError(self._failure)
        pending = [
            index
            for index in range(len(self._entries))
            if self._requires_review(index)
            and (
                self._key(index) not in self._viewed
                if approved is None
                else review_entry_id(self._entries[index]) not in approved
            )
        ]
        if not pending:
            return False
        spacing_pending = [
            index for index in pending if not isinstance(self._entries[index], Section)
        ]
        if spacing_pending:
            index = (
                self._index if self._index in spacing_pending else spacing_pending[0]
            )
        elif self._index in pending:
            index = self._index
        else:
            index = next(
                (index for index in pending if index > self._index), pending[0]
            )
        self.select(index)
        return True

    def reviewed_capture_hashes(self) -> tuple[tuple[Section, str], ...]:
        """Validate complete review and return its section-to-capture evidence."""
        self.ensure_reviewed()
        return tuple(
            (section, self._viewed[self._key(index)])
            for index, section in enumerate(self._entries)
            if self._requires_review(index) and isinstance(section, Section)
        )

    def restore_viewed(
        self,
        matches: Callable[[Section, str], bool],
        capture_hashes: Mapping[Section, str],
    ) -> None:
        """Restore historical views from verified hashes without requesting images."""
        if not self._entries or not capture_hashes:
            return
        if self._appearance_context is not None:
            try:
                self.ensure_appearance_current()
            except ValueError:
                return
        for index, section in enumerate(self._entries):
            if not isinstance(section, Section) or not self._requires_review(index):
                continue
            key = self._key(index)
            if key in self._viewed:
                continue
            image_sha256 = capture_hashes.get(section)
            if image_sha256 is None:
                continue
            if matches(section, image_sha256):
                self._viewed[key] = image_sha256
        self.refresh_review_labels()

    def _on_list_selection(self, event: wx.ListEvent) -> None:
        """Mouse and keyboard selection share the same guarded native event path."""
        index = event.GetIndex()
        if not self._updating_checklist and 0 <= index < len(self._entries):
            if self._user_selected is not None:
                self._user_selected()
            if index != self._index:
                self.select(index)

    def approve_spacing_image(
        self, approve: Callable[[SpacingReviewEntry, str], None]
    ) -> bool:
        """Require a distinct readable full-image paint and explicit modal consent."""
        # Verify before reading selection/pixel state so the shared action also
        # refreshes stale owner rows when no capture is currently displayed.
        self._verify()
        entry, capture, key, request = (
            self.selected_entry,
            self._capture,
            self._display,
            self._request,
        )
        if (
            entry is None
            or isinstance(entry, Section)
            or capture is None
            or key is None
            or not self.displayed
        ):
            raise ValueError("Select and display a spacing image before reviewing it.")

        def active() -> bool:
            """Reject obsolete modal events after selection, source or owner changes."""
            return bool(
                not self._closed
                and self.window
                and self._active()
                and self.window.IsShownOnScreen()
                and request == self._request
                and self._display == key
                and self._capture is capture
                and self.selected_entry == entry
            )

        def verify() -> None:
            """Check board and appearance at every large-view acceptance boundary."""
            self._verify()
            if not active():
                raise ValueError(
                    "The spacing image changed. Select its current entry and review again."
                )

        def displayed() -> None:
            """Publish the paint handler's freshly verified readable evidence.

            EnlargedCaptureDialog verifies source and appearance immediately
            before this callback in the same synchronous post-paint handler.
            Repeating the full-board read here cannot add a newer boundary.
            """
            if self._spacing_image_viewed is not None:
                self._spacing_image_viewed(entry, capture.sha256, utc_now())
            if not active():
                raise ValueError("The spacing image changed during review.")
            self._viewed[key] = capture.sha256
            self.refresh_review_labels()

        def approved() -> None:
            """Consume the button handler's fresh verification for this exact PNG."""
            approve(entry, capture.sha256)

        if not active():
            raise ValueError(
                "The spacing image changed. Select its current entry and review again."
            )
        with self._review_window_lifetime():
            dialog = EnlargedCaptureDialog(
                self.window,
                capture,
                title=f"Spacing review — {entry.layer} — {', '.join(entry.pair)}",
                caption=entry.caption,
                verify_current=verify,
                displayed=displayed,
                approve=approved,
                active=active,
                failed=lambda message: self._report_failure(message)
                if active()
                else None,
                composition=(
                    getattr(entry, "composition", None)
                    if (capture.width, capture.height)
                    == (entry.width_px, entry.height_px)
                    else None
                ),
            )
            try:
                dialog.ShowModal()
                return dialog.approved and active()
            finally:
                dialog.Destroy()
                self.refresh_review_labels()

    def _open_enlarged_capture(self) -> None:
        """Open a large modal view of the currently displayed capture."""
        capture = self._capture
        if capture is None or not (0 <= self._index < len(self._entries)):
            return
        section = self._entries[self._index]
        if not isinstance(section, Section) and self._open_spacing_review is not None:
            self._open_spacing_review()
            return
        nets = (
            ", ".join(
                section.net_names if isinstance(section, Section) else section.pair
            )
            or "(unnamed)"
        )
        with self._review_window_lifetime():
            dialog = EnlargedCaptureDialog(
                self.window,
                capture,
                title=f"{section.layer} — {nets}",
                caption=section.caption if not isinstance(section, Section) else "",
            )
            try:
                dialog.ShowModal()
            finally:
                dialog.Destroy()
