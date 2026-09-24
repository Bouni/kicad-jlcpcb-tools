"""One captured-image display and review component shared by both dialogs."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Optional

import wx

from .model import Section, format_length
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
    ) -> None:
        super().__init__(parent)
        self.SetMinSize((420, 180))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._displayed, self._failed, self._active = displayed, failed, active
        self._image: Optional[wx.Image] = None
        self._bitmap: Optional[wx.Bitmap] = None
        self._dimensions = (0, 0)
        self._generation = 0
        self._recorded = False
        self._pending = False
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._resize)

    def clear(self) -> None:
        """Invalidate queued display events and discard presentation state."""
        self._generation += 1
        self._image = self._bitmap = None
        self._dimensions = (0, 0)
        self._recorded = self._pending = False
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
        self.Refresh()

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
            scale = min(width / source.GetWidth(), height / source.GetHeight(), 1.0)
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
        active: Optional[Callable[[], bool]] = None,
        hide_navigation: bool = False,
        state_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self._preview, self._verify_current = preview, verify_current
        self._appearance_context, self._appearance_changed = (
            appearance_context,
            appearance_changed,
        )
        self._review_failed, self._image_viewed = review_failed, image_viewed
        self._active = active or (lambda: True)
        self._state_changed = state_changed or (lambda: None)
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
        self.window = wx.Panel(parent)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(
            wx.StaticText(self.window, label="Workbook image preview"), 0, wx.BOTTOM, 6
        )
        self.rows = wx.Choice(self.window)
        self.rows.SetMinSize((1, -1))
        self.rows.Bind(wx.EVT_CHOICE, self._on_choice)
        layout.Add(self.rows, 0, wx.EXPAND | wx.BOTTOM, 6)
        self.canvas = PreviewCanvas(
            self.window, self._record_display, self._report_failure, self._active
        )
        layout.Add(self.canvas, 1, wx.EXPAND)
        self.checklist = wx.ListCtrl(
            self.window, style=wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL
        )
        self.checklist.InsertColumn(0, "Review", width=35)
        self.checklist.InsertColumn(1, "Workbook image", width=570)
        self.checklist.SetMinSize((1, 112))
        self.checklist.SetName("Workbook image review checklist")
        layout.Add(self.checklist, 0, wx.TOP | wx.EXPAND, 8)
        self.status = wx.TextCtrl(
            self.window,
            value="",
            size=(-1, 48),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE,
        )
        layout.Add(self.status, 0, wx.TOP | wx.EXPAND, 8)
        self.refresh_button = wx.Button(self.window, label="Refresh preview")
        self.refresh_button.Bind(wx.EVT_BUTTON, self._on_refresh)
        layout.Add(self.refresh_button, 0, wx.TOP, 6)
        self.rows.Show(not hide_navigation)
        self.checklist.Show(not hide_navigation)
        self.refresh_button.Show(not hide_navigation)
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

    def _key(self, index: int) -> tuple[object, object, Section]:
        return self._context_key, self._appearance_key, self._sections[index]

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
        self._request += 1
        self._sections, self._index, self._display = (), -1, None
        self._capture = None
        self._failure = message if error else ""
        if not preserve_review:
            self._viewed.clear()
        self.canvas.clear()
        self.rows.SetItems([])
        self.rows.Enable(False)
        self.checklist.DeleteAllItems()
        self.refresh_button.Enable(False)
        self.status.SetValue(message)

    def set_sections(
        self,
        sections: tuple[Section, ...],
        context_key: object,
        selected_id: Optional[str] = None,
    ) -> None:
        """Bind rows without rendering hidden dialogs or stamping a view."""
        if sections == self._sections and context_key == self._context_key:
            if selected_id is not None:
                self.select(
                    next(
                        (
                            i
                            for i, section in enumerate(sections)
                            if section.section_id == selected_id
                        ),
                        0,
                    )
                )
            return
        self.clear("Preparing workbook image…", preserve_review=True)
        self._sections, self._context_key = sections, context_key
        self.rows.SetItems([""] * len(sections))
        self.rows.Enable(bool(sections))
        self.refresh_button.Enable(bool(sections) and self._preview is not None)
        self.refresh_review_labels()
        if sections:
            self.select(
                next(
                    (
                        i
                        for i, section in enumerate(sections)
                        if section.section_id == selected_id
                    ),
                    0,
                )
            )
        else:
            self.status.SetValue("No matching routed sections to preview.")

    def select(self, index: int, *, refresh: bool = False) -> None:
        """Queue only the latest selected row after native show/layout."""
        if not 0 <= index < len(self._sections):
            return
        if refresh or index != self._index:
            # Revoke pending display callbacks immediately, before CallAfter can
            # deliver a forced retry or a newly selected row's render request.
            self.canvas.clear()
            self._display, self._capture = None, None
        if refresh:
            self._viewed.pop(self._key(index), None)
        self._index = index
        self.rows.SetSelection(index)
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
            message = f"KiCad appearance is unavailable: {error}. Refresh preview and review the images again."
            self._invalidate_appearance(message)
            raise ValueError(message) from error
        changed = self._appearance_known and current != self._appearance_key
        self._appearance_key, self._appearance_known = current, True
        if changed:
            message = "KiCad appearance changed. Refresh preview and review the layer images again."
            self._invalidate_appearance(message)
            raise ValueError(message)

    def _invalidate_appearance(self, message: str) -> None:
        """Unavailable and changed themes both invalidate every layer's evidence."""
        self._viewed.clear()
        self.canvas.clear()
        self._display, self._capture = None, None
        self._failure = message
        self.status.SetValue(message)
        self.refresh_review_labels()
        if self._appearance_changed is not None:
            self._appearance_changed()
        self._state_changed()

    def _show_selected(self, request: int, refresh: bool = False) -> None:
        if (
            not self.window
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
            self.status.SetValue(
                f"Image {self._index + 1}: rendering workbook capture…"
            )
            if self._preview is None:
                raise ValueError("No production preview renderer is available.")
            # The provider checks its source before/after rendering. Validate
            # against the dialog's rows before installing pixels, then again
            # after visible paint; an extra pre-render UI scan adds no evidence.
            capture = self._preview(self._sections[self._index], refresh=refresh)
            self._verify()
            if request != self._request:
                return
            self._display, self._capture = key, capture
            self.canvas.set_capture(capture)
            self.status.SetValue("Waiting for the workbook image to be displayed…")
        except Exception as error:
            if request == self._request:
                self._report_failure(str(error))

    def _record_display(self) -> None:
        """Verify once after real display, then publish the image timestamp."""
        key = self._display
        if key is None or not self._active():
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
        self.refresh_review_labels()
        self.status.SetValue("")
        self._state_changed()

    def _report_failure(self, message: str) -> None:
        """Discard failed pixels/evidence before notifying the review owner."""
        if self._display is not None:
            self._viewed.pop(self._display, None)
        self._display, self._capture = None, None
        self.canvas.clear()
        self._failure = "Workbook preview unavailable: " + (message or "Please retry.")
        self.status.SetValue(self._failure)
        self.refresh_review_labels()
        if self._review_failed is not None:
            self._review_failed()

    def refresh_review_labels(self) -> None:
        """Derive dropdown and native checklist from one view ledger."""
        self.checklist.DeleteAllItems()
        for index, section in enumerate(self._sections):
            viewed = self._key(index) in self._viewed
            state = "viewed" if viewed else "needs review"
            description = f"Image {index + 1} · {section.layer} · {', '.join(section.net_names)} · {format_length(section.width_nm)} mm"
            self.rows.SetString(index, f"{description} — {state}")
            self.checklist.InsertItem(index, "☑" if viewed else "—")
            current = "Current · " if index == self._index else ""
            self.checklist.SetItem(index, 1, current + description + f" — {state}")
            self.checklist.SetItemTextColour(
                index,
                wx.Colour(0, 150, 50)
                if viewed
                else wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT),
            )
        if 0 <= self._index < len(self._sections):
            self.rows.SetSelection(self._index)
            self.checklist.EnsureVisible(self._index)

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
            if self._key(index) not in self._viewed
        ]
        if missing:
            raise ValueError(
                REVIEW_INSTRUCTION
                + " Images still needing review: "
                + ", ".join(missing)
                + "."
            )

    def advance_to_unreviewed(self) -> bool:
        """Reveal the next pending image; require a later click to approve it."""
        self._verify()
        if self._failure:
            raise ValueError(self._failure)
        pending = [
            index
            for index in range(len(self._sections))
            if self._key(index) not in self._viewed
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
        )

    def _on_choice(self, event: wx.CommandEvent) -> None:
        self.select(event.GetSelection())

    def _on_refresh(self, _event: wx.CommandEvent) -> None:
        self.select(self._index, refresh=True)
