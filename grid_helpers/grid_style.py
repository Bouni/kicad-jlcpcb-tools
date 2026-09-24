"""Grid row colors, independent of wx controls and their module lifetime."""

from collections.abc import Sequence


def alternating_row_background(
    background: Sequence[int], foreground: Sequence[int], row: int
) -> tuple[int, int, int]:
    """Use a subtle text-color tint on odd displayed rows in either theme."""
    if not row % 2:
        return background[0], background[1], background[2]
    weight = 0.16 if sum(background[index] for index in range(3)) < 384 else 0.10
    channels = tuple(
        round(background[index] * (1 - weight) + foreground[index] * weight)
        for index in range(3)
    )
    return channels[0], channels[1], channels[2]
