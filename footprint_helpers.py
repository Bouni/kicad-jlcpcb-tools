"""Helpers for reading and mutating KiCad footprint and board state."""

from collections.abc import Iterator
import re
from typing import Any, Optional

from .lcsc import normalize_lcsc
from .part_assignments import (
    ResolvedAssignment,
    is_assignment_alias,
    resolve_assignment,
)

EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def _iter_assignment_fields(fp: Any) -> Iterator[tuple[str, str]]:
    """Yield (name, text) for every field, whichever KiCad API the board has."""
    try:
        return ((field.GetName(), field.GetText()) for field in fp.GetFields())
    except AttributeError:
        return iter(fp.GetProperties().items())


def find_lcsc_assignment_text(fp: Any) -> Optional[tuple[str, str]]:
    """Check raw assignment fields independently of identifier validation.

    Any text occupies the field, including whitespace or an identifier the
    legacy reader does not recognize. Inspect every alias before filling.
    """
    for name, text in _iter_assignment_fields(fp):
        if is_assignment_alias(name) and text != "":
            return name, text
    return None


def get_lcsc_assignment(fp: Any) -> tuple[ResolvedAssignment, str]:
    """Capture assignment provenance and its normalized value in one native read."""
    return resolve_assignment(dict(_iter_assignment_fields(fp)), {}, "")


def get_lcsc_value(fp: Any) -> str:
    """Read the same normalized, unambiguous assignment as variant Default."""
    return get_lcsc_assignment(fp)[1]


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


def get_valid_footprints(board):
    """Get all footprints that have a valid reference."""
    footprints = []
    for fp in board.GetFootprints():
        if re.match(r"[\w\d-]+", fp.GetReference()):
            footprints.append(fp)
    return footprints


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


def get_exclude_from_pos(footprint):
    """Get the 'exclude from POS' property of a footprint."""
    if not footprint:
        return None
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def get_exclude_from_bom(footprint):
    """Get the 'exclude from BOM' property of a footprint."""
    if not footprint:
        return None
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


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
