"""Identify populated parts with unknown or insufficient catalog stock."""

from collections.abc import Callable, Iterable, Mapping

from .stock_display import parse_stock

STOCK_CONCERN_MULTIPLIER = 10


def stock_concern_references(
    parts: Iterable[Mapping[str, object]],
    get_stock: Callable[[str], object],
    board_count: int = 1,
) -> set[str]:
    """Group populated BOM references by LCSC and compare exact catalog supply.

    Input records contain live ``is_dnp`` and ``exclude_from_bom`` flags.
    Placement exclusions do not remove hand-placed parts from board demand.
    Stock is one inventory quantity per LCSC, not a quantity per reference.
    Unknown stock is a concern. ``board_count`` must be a positive integer.
    """
    if (
        isinstance(board_count, bool)
        or not isinstance(board_count, int)
        or board_count <= 0
    ):
        raise ValueError("board_count must be a positive integer")

    groups: dict[str, set[str]] = {}
    for part in parts:
        if part.get("exclude_from_bom") or part.get("is_dnp"):
            continue
        reference = str(part.get("reference") or "").strip()
        lcsc = str(part.get("lcsc") or "").strip().upper()
        if reference and lcsc:
            groups.setdefault(lcsc, set()).add(reference)

    concerns: set[str] = set()
    for lcsc, references in groups.items():
        stock = parse_stock(get_stock(lcsc))
        if stock is None or stock < (
            STOCK_CONCERN_MULTIPLIER * len(references) * board_count
        ):
            concerns.update(references)
    return concerns
