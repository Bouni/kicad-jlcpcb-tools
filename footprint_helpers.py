"""Helpers for reading KiCad footprint and board state."""

from collections.abc import Iterator
import re
import sqlite3
from typing import Any, Optional

EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def footprint_uuid(footprint: Any) -> str:
    """Read durable KiCad identity without falling back to mutable references."""
    uuid = getattr(footprint, "m_Uuid", None)
    value = uuid.AsString() if uuid is not None else ""
    if not value:
        raise sqlite3.IntegrityError("The footprint has no persistent UUID.")
    return value


def find_lcsc_assignment_text(fp: Any) -> Optional[tuple[str, str]]:
    """Check raw assignment fields independently of identifier validation.

    Any text occupies the field, including whitespace or an identifier the
    legacy reader does not recognize. Inspect every alias before filling.
    """
    try:
        fields = ((field.GetName(), field.GetText()) for field in fp.GetFields())
    except AttributeError:
        fields = iter(fp.GetProperties().items())
    for name, text in fields:
        if re.match(r"lcsc|jlc", name, re.IGNORECASE) and text != "":
            return name, text
    return None


def get_lcsc_value(fp):
    """Get the first lcsc number (C123456 for example) from the properties of the footprint."""
    try:
        for field in fp.GetFields():
            if re.match(r"lcsc|jlc", field.GetName(), re.IGNORECASE) and re.match(
                r"^C\d+$", field.GetText()
            ):
                return field.GetText()
    except AttributeError:
        for key, value in fp.GetProperties().items():
            if re.match(r"lcsc|jlc", key, re.IGNORECASE) and re.match(r"^C\d+$", value):
                return value
    return ""


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
