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


def filtered_highlight_terms(query: str) -> list[str]:
    """Return normalized terms that are long enough to highlight."""
    return [
        t
        for t in normalize_highlight_terms(query)
        if len(t) >= _MIN_HIGHLIGHT_TERM_LENGTH
    ]


class HighlightQueryCache:
    """Cache normalized query terms and highlight spans for one active query."""

    def __init__(self):
        self._query = ""
        self._terms: list[str] = []
        self._span_cache: dict[str, list[tuple[int, int]]] = {}

    def prepare(self, query: str):
        """Prepare cache state for a query, resetting cached spans on change."""
        if query != self._query:
            self._query = query
            self._terms = filtered_highlight_terms(query)
            self._span_cache.clear()

    def clear(self):
        """Clear all cached query and span data."""
        self._query = ""
        self._terms = []
        self._span_cache.clear()

    def get_terms(self) -> list[str]:
        """Return terms prepared for the active query."""
        return self._terms

    def get_spans(self, text: str) -> list[tuple[int, int]]:
        """Return cached spans for text, computing and storing on cache miss."""
        spans = self._span_cache.get(text)
        if spans is None:
            spans = find_highlight_spans(text, self._terms)
            self._span_cache[text] = spans
        return spans


if wx is not None and dv is not None:  # pragma: no branch

    class HighlightedTextRenderer(dv.DataViewCustomRenderer):
        """Simple text renderer that highlights keyword matches."""

        def __init__(self, highlight_text_getter: Callable[[], str], align: int):
            super().__init__("string", dv.DATAVIEW_CELL_INERT, align)
            self._highlight_text_getter = highlight_text_getter
            self._value = ""
            # Cache lifetime is tied to this renderer instance (one dialog/session).
            # It resets automatically when the query string changes via prepare(query).
            # A new part selector dialog constructs new renderer instances and caches.
            self._query_cache = HighlightQueryCache()

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
            self._query_cache.prepare(query)
            terms = self._query_cache.get_terms()
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

            spans = self._query_cache.get_spans(text)
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
                    segment_width, _ = dc.GetTextExtent(segment)
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
