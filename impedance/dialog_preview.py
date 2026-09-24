"""One captured-image display and review component shared by both dialogs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from io import BytesIO
from typing import Optional

import wx

from .capture_retry import retry_delay_ms
from .model import EXCLUSION_COVERED_STUB, Section, format_length
from .review_tracking import utc_now
from .service import CapturedImage

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
    ) -> None:
        super().__init__(parent)
        self.SetMinSize((420, 180))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._displayed, self._failed, self._active = displayed, failed, active
        self._max_scale = max(1.0, float(max_scale))
        self._image: Optional[wx.Image] = None
        self._bitmap: Optional[wx.Bitmap] = None
        self._dimensions = (0, 0)
        self._generation = 0
        self._recorded = False
        self._pending = False
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._resize)
        self.Bind(wx.EVT_LEFT_UP, self._on_click)
        self._enlarge: Optional[Callable[[], None]] = None

    def set_enlarge_handler(self, handler: Optional[Callable[[], None]]) -> None:
        """Open a larger review window when the thumbnail is clicked."""
        self._enlarge = handler

    def clear(self) -> None:
        """Invalidate queued display events and discard presentation state."""
        self._generation += 1
        self._image = self._bitmap = None
        self._dimensions = (0, 0)
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
            source = self._image
            scale = min(
                width / source.GetWidth(),
                height / source.GetHeight(),
                self._max_scale,
            )
            dimensions = (
                max(1, round(source.GetWidth() * scale)),
                max(1, round(source.GetHeight() * scale)),
            )
            if dimensions != self._dimensions:
                scaled = source.Scale(*dimensions, wx.IMAGE_QUALITY_HIGH)
                if not scaled.IsOk():
                    raise ValueError("The captured PCB image could not be resized.")
                self._bitmap = wx.Bitmap(scaled)
                if not self._bitmap.IsOk():
                    raise ValueError("The captured PCB bitmap could not be created.")
                self._dimensions = dimensions
            dc.DrawBitmap(
                self._bitmap,
                (width - dimensions[0]) // 2,
                (height - dimensions[1]) // 2,
                True,
            )
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

    def _resize(self, event: wx.SizeEvent) -> None:
        """Let the next paint refit pixels without reading or changing the board."""
        self.Refresh()
        event.Skip()


def _display_client_bounds(parent: wx.Window) -> tuple[int, int]:
    """Return a usable on-screen work area for sizing the enlarge dialog."""
    try:
        index = wx.Display.GetFromWindow(parent)
        index = max(index, 0)
        area = wx.Display(index).GetClientArea()
        return int(area.GetWidth()), int(area.GetHeight())
    except Exception:
        return 1280, 800


class EnlargedCaptureDialog(wx.Dialog):
    """Show one workbook capture large enough for careful review."""

    def __init__(self, parent: wx.Window, capture: CapturedImage, title: str) -> None:
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        max_width, max_height = _display_client_bounds(parent)
        max_width = max(320, int(max_width * 0.9))
        max_height = max(240, int(max_height * 0.9) - 80)
        if capture.width <= max_width and capture.height <= max_height:
            width, height = capture.width, capture.height
        else:
            scale = min(max_width / capture.width, max_height / capture.height)
            width = max(1, int(round(capture.width * scale)))
            height = max(1, int(round(capture.height * scale)))
        canvas = PreviewCanvas(
            self,
            lambda: None,
            lambda _message: None,
            lambda: True,
        )
        canvas.SetMinSize((width, height))
        canvas.set_capture(capture)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(canvas, 1, wx.EXPAND | wx.ALL, 8)
        buttons = self.CreateButtonSizer(wx.OK)
        if buttons is not None:
            layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(layout)


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
        approved_images: Optional[Callable[[], set[str]]] = None,
        active: Optional[Callable[[], bool]] = None,
        image_list: Optional[wx.ListCtrl] = None,
        state_changed: Optional[Callable[[], None]] = None,
        skip_stub_review: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._preview, self._verify_current = preview, verify_current
        self._appearance_context, self._appearance_changed = (
            appearance_context,
            appearance_changed,
        )
        self._review_failed, self._image_viewed = review_failed, image_viewed
        self._approved_images = approved_images
        self._active = active or (lambda: True)
        self._state_changed = state_changed or (lambda: None)
        self._skip_stub_review = skip_stub_review or (lambda: True)
        self._sections: tuple[Section, ...] = ()
        self._context_key: object = None
        self._appearance_key: object = None
        self._appearance_known = False
        self._index = -1
        self._request = 0
        self._failure = ""
        self._display: Optional[tuple[object, object, Section]] = None
        self._capture: Optional[CapturedImage] = None
        self._viewed: dict[tuple[object, object, Section], str] = {}
        self._updating_checklist = False
        self._closed = False
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
        layout.Add(
            wx.StaticText(self.window, label="Workbook image preview"), 0, wx.BOTTOM, 6
        )
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
    def sections(self) -> tuple[Section, ...]:
        """Return the current ordered workbook sections."""
        return self._sections

    @property
    def failure(self) -> str:
        """Return the inline explanation of the most recent preview failure."""
        return self._failure

    @property
    def ready(self) -> bool:
        """A visible capture has completed its source-checked display event."""
        return bool(
            0 <= self._index < len(self._sections)
            and self._display == self._key(self._index)
            and self._display in self._viewed
            and not self._failure
        )

    @property
    def capture_loaded(self) -> bool:
        """Return whether the latest selection has pixels ready for native paint."""
        return bool(
            0 <= self._index < len(self._sections)
            and self._display == self._key(self._index)
            and self._capture is not None
        )

    @property
    def selected_section(self) -> Optional[Section]:
        """Identify the selected row even when loading or native display failed."""
        return (
            self._sections[self._index]
            if 0 <= self._index < len(self._sections)
            else None
        )

    @property
    def displayed(self) -> bool:
        """Require the selected canvas to have painted visibly for a new approval."""
        return self.ready and self.canvas._recorded and self.window.IsShownOnScreen()

    def _key(self, index: int) -> tuple[object, object, Section]:
        return self._context_key, self._appearance_key, self._sections[index]

    def _requires_review(self, index: int) -> bool:
        """Return whether this preview row must be viewed before layer approval."""
        if not 0 <= index < len(self._sections):
            return False
        if (
            self._skip_stub_review()
            and self._sections[index].exclusion_reason == EXCLUSION_COVERED_STUB
        ):
            return False
        return True

    def _default_section_index(self, selected_id: Optional[str]) -> int:
        """Prefer an explicit id, otherwise the first row that still needs review."""
        if selected_id is not None:
            for index, section in enumerate(self._sections):
                if section.section_id == selected_id:
                    return index
        for index in range(len(self._sections)):
            if self._requires_review(index):
                return index
        return 0

    def image_digest(self, section: Section) -> Optional[str]:
        """Return a loaded or previously viewed capture hash in this context."""
        key = self._context_key, self._appearance_key, section
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
        self._sections, self._index, self._display = (), -1, None
        self._capture = None
        self._failure = message if error else ""
        if not preserve_review:
            self._viewed.clear()
        self.canvas.clear()
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
    ) -> None:
        """Bind rows without rendering hidden dialogs or stamping a view."""
        if sections == self._sections and context_key == self._context_key:
            if selected_id is not None:
                self.select(self._default_section_index(selected_id))
            return
        self.clear("Preparing workbook image…", preserve_review=True)
        self._sections, self._context_key = sections, context_key
        if self.checklist is not None:
            self.checklist.Enable(bool(sections))
        self.refresh_review_labels()
        if sections:
            self.select(self._default_section_index(selected_id))
        else:
            self._set_status("No matching routed sections to preview.")

    def select(self, index: int, *, refresh: bool = False) -> None:
        """Queue only the latest selected row after native show/layout."""
        if self._closed or not 0 <= index < len(self._sections):
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
        self.canvas.clear()
        self._display, self._capture = None, None
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
            self._viewed.pop(key, None)
            self.refresh_review_labels()
            self._set_status(f"Image {self._index + 1}: rendering workbook capture…")
            if self._preview is None:
                raise ValueError("No production preview renderer is available.")
            # The provider checks before capture. The owning dialog validates
            # against its rows before installing pixels, then again after paint.
            capture = self._preview(self._sections[self._index], refresh=refresh)
            self._verify()
            if request != self._request:
                return
            self._display, self._capture = key, capture
            self.canvas.set_capture(capture)
            self._set_status("Waiting for the workbook image to be displayed…")
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
        if self._image_viewed is not None:
            self._image_viewed(key[2], capture.sha256, utc_now())
        self._viewed[key] = capture.sha256
        self._retry_timer.Stop()
        self._retry_attempt = 0
        self.refresh_review_labels()
        self._set_status("")
        self._state_changed()

    def _report_failure(self, message: str) -> None:
        """Discard failed pixels/evidence before notifying the review owner."""
        if self._closed or not self.window:
            return
        if self._display is not None:
            self._viewed.pop(self._display, None)
        self._display, self._capture = None, None
        self.canvas.clear()
        self._failure = "Workbook preview unavailable: " + (message or "Please retry.")
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

    def refresh_review_labels(self) -> None:
        """Update rows in place, preserving native selection, focus and scrolling."""
        if self.checklist is None:
            return
        approved = (
            self._approved_images()
            if self._approved_images is not None and self._sections
            else None
        )
        self._updating_checklist = True
        try:
            if self.checklist.GetItemCount() != len(self._sections):
                self.checklist.DeleteAllItems()
                for index in range(len(self._sections)):
                    self.checklist.InsertItem(index, "—")
            for index, section in enumerate(self._sections):
                viewed = self._key(index) in self._viewed
                checked = viewed if approved is None else section.section_id in approved
                state = "viewed" if viewed else "needs review"
                if approved is not None and checked:
                    state = "approved"
                stub = section.exclusion_reason == EXCLUSION_COVERED_STUB
                width_or_stub = (
                    "Covered stub" if stub else f"{format_length(section.width_nm)} mm"
                )
                description = (
                    f"Image {index + 1} · {section.layer} · "
                    f"{', '.join(section.net_names)} · {width_or_stub} — {state}"
                )
                self.checklist.SetItem(index, 0, "☑" if checked else "—")
                self.checklist.SetItem(index, 1, description)
                colour = wx.SystemSettings.GetColour(
                    wx.SYS_COLOUR_GRAYTEXT if stub else wx.SYS_COLOUR_LISTBOXTEXT
                )
                if checked and not stub:
                    background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_LISTBOX)
                    dark = sum(background[channel] for channel in range(3)) < 384
                    colour = wx.Colour(*((70, 210, 110) if dark else (0, 120, 40)))
                self.checklist.SetItemTextColour(index, colour)
            if (
                0 <= self._index < len(self._sections)
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
            or not self._sections
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
        if not self._sections or self._preview is None:
            raise ValueError("No workbook images are available for approval.")
        missing = [
            str(index + 1)
            for index in range(len(self._sections))
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
            for index in range(len(self._sections))
            if self._requires_review(index)
            and (
                self._key(index) not in self._viewed
                if approved is None
                else self._sections[index].section_id not in approved
            )
        ]
        if not pending:
            return False
        if self._index in pending:
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
            for index, section in enumerate(self._sections)
            if self._requires_review(index)
        )

    def restore_viewed(
        self,
        matches: Callable[[Section, str], bool],
        capture_hashes: Mapping[Section, str],
    ) -> None:
        """Restore historical views from verified hashes without requesting images."""
        if not self._sections or not capture_hashes:
            return
        if self._appearance_context is not None:
            try:
                self.ensure_appearance_current()
            except ValueError:
                return
        for index, section in enumerate(self._sections):
            if not self._requires_review(index):
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
        if not self._updating_checklist and index != self._index:
            self.select(index)

    def _open_enlarged_capture(self) -> None:
        """Open a large modal view of the currently displayed capture."""
        capture = self._capture
        if capture is None or not (0 <= self._index < len(self._sections)):
            return
        section = self._sections[self._index]
        nets = ", ".join(section.net_names) or "(unnamed)"
        dialog = EnlargedCaptureDialog(
            self.window,
            capture,
            title=f"{section.layer} — {nets}",
        )
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
