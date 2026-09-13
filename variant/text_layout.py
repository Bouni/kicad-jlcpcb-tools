"""Measured text policies for compact, readable variant-matrix columns."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class FittedText:
    """Visible text and the original suffix positions used for highlighting."""

    display_text: str
    scale: float
    source_start: int = 0
    prefix_length: int = 0


def percentile_text_width(samples: Sequence[str], measure: Callable[[str], int]) -> int:
    """Fit eighty percent of longer entries, while retaining every short value."""
    longer = sorted(measure(text) for text in samples if len(text) > 3)
    shorter = max((measure(text) for text in samples if len(text) <= 3), default=0)
    typical = longer[ceil(0.8 * len(longer)) - 1] if longer else 0
    return max(typical, shorter)


def fit_text_suffix(
    text: str,
    available_width: int,
    measure_at_scale: Callable[[str, float], int],
    minimum_scale: float = 0.8,
) -> FittedText:
    """Shrink an outlier to the font floor, then preserve its suffix with ellipsis."""
    if not 0 < minimum_scale <= 1:
        raise ValueError("The minimum text scale must be between zero and one")
    if available_width <= 0:
        return FittedText("", minimum_scale, len(text))
    if measure_at_scale(text, 1.0) <= available_width:
        return FittedText(text, 1.0)
    if measure_at_scale(text, minimum_scale) <= available_width:
        # Native font metrics need not scale linearly. Keep the largest measured
        # fitting size, always starting from the caller's unmodified body font.
        lower, upper = minimum_scale, 1.0
        for _ in range(10):
            middle = (lower + upper) / 2
            if measure_at_scale(text, middle) <= available_width:
                lower = middle
            else:
                upper = middle
        return FittedText(text, lower)
    ellipsis = "…"
    if measure_at_scale(ellipsis, minimum_scale) > available_width:
        return FittedText("", minimum_scale, len(text))
    lower, upper = 0, len(text)
    while lower < upper:
        count = (lower + upper + 1) // 2
        suffix = text[len(text) - count :]
        if measure_at_scale(ellipsis + suffix, minimum_scale) <= available_width:
            lower = count
        else:
            upper = count - 1
    start = len(text) - lower
    return FittedText(ellipsis + text[start:], minimum_scale, start, 1)


def visible_highlight_spans(
    spans: Sequence[tuple[int, int]], fitted: FittedText
) -> tuple[tuple[int, int], ...]:
    """Map original match spans onto the visible suffix, excluding the ellipsis."""
    visible_end = fitted.source_start + len(fitted.display_text) - fitted.prefix_length
    mapped = []
    for start, end in spans:
        start, end = max(start, fitted.source_start), min(end, visible_end)
        if start < end:
            mapped.append(
                (
                    start - fitted.source_start + fitted.prefix_length,
                    end - fitted.source_start + fitted.prefix_length,
                )
            )
    return tuple(mapped)
