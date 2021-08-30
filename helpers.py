import os
import re

THT = 0
SMD = 1
EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3
NOT_IN_SCHEMATIC = 4


def get_version_info():
    """Get git short commit hash without haveing git installed."""
    path, filename = os.path.split(os.path.abspath(__file__))
    with open(os.path.join(path, ".git", "FETCH_HEAD")) as f:
        v = f.read()
    return v[:7]


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
    for fp in board.GetFootprints():
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
