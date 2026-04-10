"""Highlight helpers and renderer for part selector search results."""

from __future__ import annotations

from collections.abc import Callable

try:
    import wx  # pylint: disable=import-error
    import wx.dataview as dv  # pylint: disable=import-error
except ImportError:  # pragma: no cover - test environments may not have wx
    wx = None  # type: ignore[assignment]
    dv = None  # type: ignore[assignment]


_HIGHLIGHT_FG = (180, 120, 0)
_HIGHLIGHT_FG_SELECTED = (255, 215, 64)
_MIN_HIGHLIGHT_TERM_LENGTH = 2


def normalize_highlight_terms(query: str) -> list[str]:
    """Split keyword query into normalized terms suitable for highlighting."""
    terms = []
    for raw_term in query.split():
        term = raw_term.strip().strip("%")
        if term:
            lowered = term.casefold()
            if lowered not in terms:
                terms.append(lowered)
    return terms


def find_highlight_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """Return merged `(start, end)` spans for all term matches in `text`."""
    if not text or not terms:
        return []

    lowered_text = text.casefold()
    spans: list[tuple[int, int]] = []

    for term in terms:
        start = 0
        while True:
            match_start = lowered_text.find(term, start)
            if match_start == -1:
                break
            match_end = match_start + len(term)
            spans.append((match_start, match_end))
            start = match_end

    if not spans:
        return []

    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


if wx is not None and dv is not None:  # pragma: no branch

    class HighlightedTextRenderer(dv.DataViewCustomRenderer):
        """Simple text renderer that highlights keyword matches."""

        def __init__(self, highlight_text_getter: Callable[[], str], align: int):
            super().__init__("string", dv.DATAVIEW_CELL_INERT, align)
            self._highlight_text_getter = highlight_text_getter
            self._value = ""
            self._cached_query = ""
            self._cached_terms: list[str] = []
            self._span_cache: dict[str, list[tuple[int, int]]] = {}

        def SetValue(self, value: str) -> bool:
            """Store value to render for the current cell."""
            self._value = "" if value is None else str(value)
            return True

        def GetValue(self) -> str:
            """Return current cell value."""
            return self._value

        def GetSize(self):
            """Return a best-effort size for the current text."""
            owner = self.GetOwner()
            font = (
                owner.GetOwner().GetFont()
                if owner is not None and owner.GetOwner() is not None
                else wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
            )
            dc = wx.ScreenDC()
            dc.SetFont(font)
            width, height = dc.GetTextExtent(self._value or "Hg")
            return wx.Size(width + 8, height + 6)

        def Render(self, rect, dc, state):
            """Draw the cell text and highlight search-term matches."""
            selected = bool(state & dv.DATAVIEW_CELL_SELECTED)
            foreground = wx.SystemSettings.GetColour(
                wx.SYS_COLOUR_HIGHLIGHTTEXT if selected else wx.SYS_COLOUR_LISTBOXTEXT
            )
            highlight = wx.Colour(
                *(_HIGHLIGHT_FG_SELECTED if selected else _HIGHLIGHT_FG)
            )

            dc.SetTextForeground(foreground)
            dc.SetBackgroundMode(wx.TRANSPARENT)

            text = self._value
            if not text:
                return True

            query = self._highlight_text_getter()
            if query != self._cached_query:
                self._cached_query = query
                self._cached_terms = [
                    t
                    for t in normalize_highlight_terms(query)
                    if len(t) >= _MIN_HIGHLIGHT_TERM_LENGTH
                ]
                self._span_cache.clear()

            terms = self._cached_terms
            if not terms:
                text_height = dc.GetTextExtent("Hg")[1]
                x = rect.x + 4
                y = rect.y + max(0, (rect.height - text_height) // 2)
                dc.SetClippingRegion(rect)
                try:
                    dc.DrawText(text, x, y)
                finally:
                    dc.DestroyClippingRegion()
                return True

            spans = self._span_cache.get(text)
            if spans is None:
                spans = find_highlight_spans(text, terms)
                self._span_cache[text] = spans
            text_height = dc.GetTextExtent("Hg")[1]
            x = rect.x + 4
            y = rect.y + max(0, (rect.height - text_height) // 2)

            dc.SetClippingRegion(rect)
            try:
                cursor = 0
                for start, end in spans:
                    if start > cursor:
                        segment = text[cursor:start]
                        dc.SetTextForeground(foreground)
                        dc.DrawText(segment, x, y)
                        x += dc.GetTextExtent(segment)[0]

                    segment = text[start:end]
                    segment_width, segment_height = dc.GetTextExtent(segment)
                    dc.SetTextForeground(highlight)
                    dc.DrawText(segment, x, y)
                    x += segment_width
                    cursor = end

                if cursor < len(text):
                    dc.SetTextForeground(foreground)
                    dc.DrawText(text[cursor:], x, y)
            finally:
                dc.DestroyClippingRegion()
            return True
