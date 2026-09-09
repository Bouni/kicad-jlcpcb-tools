"""Format stock for display while preserving exact quantities for decisions."""

from typing import Optional


def parse_stock(value: object) -> Optional[int]:
    """Return known nonnegative integer stock; leave qualified supply unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def format_stock(value: object, simplified: bool = True) -> str:
    """Truncate stock to compact units without overstating available supply."""
    text = "" if value is None else str(value)
    stock = parse_stock(value)
    if not simplified or stock is None or stock < 1000:
        return text
    for unit, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "k")):
        if stock >= unit:
            whole = stock // unit
            tenth = (stock % unit) * 10 // unit
            if whole < 10 and tenth:
                return f"{whole}.{tenth}{suffix}"
            return f"{whole}{suffix}"
    return text


def stock_sort_key(value: object) -> tuple[int, int, str]:
    """Sort exact numeric stock before unknown supplier text deterministically."""
    stock = parse_stock(value)
    if stock is not None:
        return (0, stock, "")
    return (1, 0, format_stock(value, simplified=False).casefold())
