"""Backward-compatible exports for dataview text highlighting."""

from .dataview_highlight import (
    HighlightedTextRenderer,
    find_highlight_spans,
    normalize_highlight_terms,
)

__all__ = [
    "HighlightedTextRenderer",
    "find_highlight_spans",
    "normalize_highlight_terms",
]
