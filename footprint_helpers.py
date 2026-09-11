"""Helpers for reading and mutating KiCad footprint and board state."""

from collections.abc import Iterator
import re
from typing import Any, Optional

from .lcsc import is_lcsc_part, normalize_lcsc

EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def _iter_assignment_fields(fp: Any):
    """Yield (name, text) for every field, whichever KiCad API the board has."""
    try:
        return ((field.GetName(), field.GetText()) for field in fp.GetFields())
    except AttributeError:
        return iter(fp.GetProperties().items())


def _is_assignment_alias(name: str) -> bool:
    """Report whether a field name is one of the LCSC assignment aliases."""
    return bool(re.match(r"lcsc|jlc", name, re.IGNORECASE))


def _claims_the_part(name: str, text: str) -> bool:
    """Report whether an alias field is claiming to hold the part number.

    The canonical ``LCSC`` field claims it whatever it holds, because that is
    the one the plugin writes when nothing else is there. Any other alias
    claims it once its text names a part -- tested through the same normalised
    predicate the reader uses, so a field typed as `` c12345 `` is recognised
    as the claim it is rather than left behind as a stale duplicate.
    """
    return _is_assignment_alias(name) and (name.lower() == "lcsc" or is_lcsc_part(text))


def find_lcsc_assignment_text(fp: Any) -> Optional[tuple[str, str]]:
    """Check raw assignment fields independently of identifier validation.

    Any text occupies the field, including whitespace or an identifier the
    legacy reader does not recognize. Inspect every alias before filling.
    """
    for name, text in _iter_assignment_fields(fp):
        if _is_assignment_alias(name) and text != "":
            return name, text
    return None


def get_lcsc_value(fp):
    """Get the first lcsc number (C123456 for example) from the properties of the footprint.

    The text is normalised before it is tested, so a field holding ``C12345 ``
    or ``c12345`` names the part it looks like instead of reading as blank
    (issue #773), and the canonical form is what the caller gets back.
    """
    for name, text in _iter_assignment_fields(fp):
        if _is_assignment_alias(name) and is_lcsc_part(text):
            return normalize_lcsc(text)
    return ""


def set_lcsc_value(fp: Any, lcsc: str) -> None:
    """Keep existing assignment aliases consistent, or create a hidden LCSC field."""
    if not fp:
        return
    lcsc = normalize_lcsc(lcsc)
    names = [
        field.GetName()
        for field in fp.GetFields()
        if _claims_the_part(field.GetName(), field.GetText())
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
