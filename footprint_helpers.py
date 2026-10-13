"""Helpers for reading and mutating KiCad footprint and board state."""

from collections.abc import Iterator
from typing import Any

from .lcsc import normalize_lcsc
from .part_assignments import is_assignment_alias

EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def set_lcsc_value(fp: Any, lcsc: str) -> None:
    """Keep existing assignment aliases consistent, or create a hidden LCSC field."""
    if not fp:
        return
    lcsc = normalize_lcsc(lcsc)
    try:
        fields = list(fp.GetFields())
    except AttributeError:
        properties = dict(fp.GetProperties())
        names = [name for name in properties if is_assignment_alias(name)]
        for name in names or ["LCSC"]:
            properties[name] = lcsc
        fp.SetProperties(properties)
        return
    names = [
        field.GetName() for field in fields if is_assignment_alias(field.GetName())
    ]
    if names:
        for name in names:
            fp.SetField(name, lcsc)
    else:
        fp.SetField("LCSC", lcsc)
        if hasattr(fp, "GetFieldByName"):
            fp.GetFieldByName("LCSC").SetVisible(False)
        else:
            for field in fp.GetFields():
                if field.GetName() == "LCSC":
                    field.SetVisible(False)
                    break


def iter_board_items(container: Any) -> Iterator[Any]:
    """Yield the concrete board items held by a pcbnew container."""
    try:
        items = list(container)
    except AttributeError:
        items = []
        for index in range(len(container)):
            item = container[index]
            cast = getattr(item, "Cast", None)
            items.append(cast() if callable(cast) else item)
    return iter(items)


def get_bit(value, bit):
    """Get the nth bit of a byte."""
    return value & (1 << bit)


def toggle_bit(value, bit):
    """Toggle the nth bit of a byte."""
    return value ^ (1 << bit)


def get_is_dnp(footprint):
    """Get the runtime 'Do not place' state of a footprint."""
    if not footprint:
        return False
    is_dnp = getattr(footprint, "IsDNP", None)
    if not callable(is_dnp):
        return False
    return bool(is_dnp())


def toggle_exclude_from_pos(footprint):
    """Toggle the 'exclude from POS' property of a footprint."""
    if not footprint:
        return None
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def toggle_exclude_from_bom(footprint):
    """Toggle the 'exclude from BOM' property of a footprint."""
    if not footprint:
        return None
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_BOM))
