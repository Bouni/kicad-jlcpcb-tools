import logging
import os
import re

from pcbnew import wxPoint

THT = 0
SMD = 1
EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3
NOT_IN_SCHEMATIC = 4

logger = logging.getLogger(__name__)


def natural_sort_collation(a, b):
    """Natural sort collation for use in sqlite."""
    if a == b:
        return 0
    convert = lambda text: int(text) if text.isdigit() else text.lower()
    alphanum_key = lambda key: [convert(c) for c in re.split("([0-9]+)", key)]
    natorder = sorted([a, b], key=alphanum_key)
    return -1 if natorder.index(a) == 0 else 1


def get_lcsc_value(fp):
    """Get lcsc from all properties and allow vaious variants."""
    lcsc_keys = [key for key in fp.GetProperties().keys() if "lcsc" in key.lower()]
    if lcsc_keys:
        return fp.GetProperties().get(lcsc_keys.pop(0), "")
    return ""


def get_version_info():
    """Get version info"""
    path, filename = os.path.split(os.path.abspath(__file__))
    fetch_head = os.path.join(path, ".git", "FETCH_HEAD")
    if not os.path.isfile(fetch_head):
        return "unknown"
    with open(fetch_head) as f:
        v = f.read()[:7]
    return v


def get_valid_footprints(board):
    """Get all footprints that have a vaild reference (drop all REF**)"""
    footprints = []
    for fp in board.GetFootprints():
        if re.match(r"\w+\d+", fp.GetReference()):
            footprints.append(fp)
    return footprints


def get_footprint_keys(fp):
    """get keys from footprint for sorting."""
    try:
        package = str(fp.GetFPID().GetLibItemName())
    except:
        package = ""
    try:
        refrerence = int(re.search("\d+", fp.GetReference())[0])
    except:
        refrerence = 0
    return (package, refrerence)


def get_footprint_by_ref(board, ref):
    """get a footprint from the list of footprints by its Reference."""
    for fp in get_valid_footprints(board):
        if str(fp.GetReference()) == ref:
            return fp


def get_bit(value, bit):
    return value & (1 << bit)


def set_bit(value, bit):
    return value | (1 << bit)


def clear_bit(value, bit):
    return value & ~(1 << bit)


def toggle_bit(value, bit):
    return value ^ (1 << bit)


def get_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, THT))


def get_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, SMD))


def get_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def get_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def get_not_in_schematic(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, NOT_IN_SCHEMATIC))


def set_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, THT)
    footprint.SetAttributes(val)
    return bool(get_bit(val, THT))


def set_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, SMD)
    footprint.SetAttributes(val)
    return bool(get_bit(val, SMD))


def set_exclude_from_pos(footprint, v):
    if not footprint:
        return
    val = footprint.GetAttributes()
    if v:
        val = set_bit(val, EXCLUDE_FROM_POS)
    else:
        val = clear_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def set_exclude_from_bom(footprint, v):
    if not footprint:
        return
    val = footprint.GetAttributes()
    if v:
        val = set_bit(val, EXCLUDE_FROM_BOM)
    else:
        val = clear_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def set_not_in_schematic(footprint, v):
    if not footprint:
        return
    val = footprint.GetAttributes()
    if v:
        val = set_bit(val, NOT_IN_SCHEMATIC)
    else:
        val = clear_bit(val, NOT_IN_SCHEMATIC)
    footprint.SetAttributes(val)
    return bool(get_bit(val, NOT_IN_SCHEMATIC))


def toggle_tht(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, THT)
    footprint.SetAttributes(val)
    return bool(get_bit(val, THT))


def toggle_smd(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, SMD)
    footprint.SetAttributes(val)
    return bool(get_bit(val, SMD))


def toggle_exclude_from_pos(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def toggle_exclude_from_bom(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def toggle_not_in_schematic(footprint):
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, NOT_IN_SCHEMATIC)
    footprint.SetAttributes(val)
    return bool(get_bit(val, NOT_IN_SCHEMATIC))
