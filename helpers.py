import logging
import os
import re

import wx

PLUGIN_PATH = os.path.split(os.path.abspath(__file__))[0]

THT = 0
SMD = 1
EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3
NOT_IN_SCHEMATIC = 4


def getWxWidgetsVersion():
    v = re.search(r"wxWidgets\s([\d\.]+)", wx.version())
    v = int(v.group(1).replace(".", ""))
    return v


def getVersion():
    """READ Version from file"""
    if not os.path.isfile(os.path.join(PLUGIN_PATH, "VERSION")):
        return "unknown"
    with open(os.path.join(PLUGIN_PATH, "VERSION")) as f:
        return f.read()


def GetScaleFactor(window):
    """Workaround if wxWidgets Version does not support GetDPIScaleFactor"""
    if hasattr(window, "GetDPIScaleFactor"):
        return window.GetDPIScaleFactor()
    else:
        return 1.0


def HighResWxSize(window, size):
    """Workaround if wxWidgets Version does not support FromDIP"""
    if hasattr(window, "FromDIP"):
        return window.FromDIP(size)
    else:
        return size


def loadBitmapScaled(filename, scale=1.0, static=False):
    """Load a scaled bitmap, handle differences between Kicad versions"""
    if filename:
        path = os.path.join(PLUGIN_PATH, "icons", filename)
        bmp = wx.Bitmap(path)
        w, h = bmp.GetSize()
        img = bmp.ConvertToImage()
        bmp = wx.Bitmap(img.Scale(int(w * scale), int(h * scale)))
    else:
        bmp = wx.Bitmap()
    if getWxWidgetsVersion() > 315 and not static:
        return wx.BitmapBundle(bmp)
    return bmp


def loadIconScaled(filename, scale=1.0):
    """Load a scaled icon, handle differences between Kicad versions"""
    bmp = loadBitmapScaled(filename, scale=scale, static=False)
    if getWxWidgetsVersion() > 315:
        return bmp
    return wx.Icon(bmp)


def natural_sort_collation(a, b):
    """Natural sort collation for use in sqlite."""
    if a == b:
        return 0
    convert = lambda text: int(text) if text.isdigit() else text.lower()
    alphanum_key = lambda key: [convert(c) for c in re.split("([0-9]+)", key)]
    natorder = sorted([a, b], key=alphanum_key)
    return -1 if natorder.index(a) == 0 else 1


def get_lcsc_value(fp):
    """Get lcsc number (C123456 for example) from all properties and allow various variants and also validate the value."""
    tokens = ["lcsc", "jlc"]
    lcsc_keys = [
        key
        for key in fp.GetProperties().keys()
        if any(t in key.lower() for t in tokens)
    ]
    if lcsc_keys:
        for match in lcsc_keys:
            value = fp.GetProperties().get(match)
            if re.match(r"C\d+", value):
                return value
    return ""


def get_valid_footprints(board):
    """Get all footprints that have a valid reference (drop all REF**)"""
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
        reference = int(re.search("\d+", fp.GetReference())[0])
    except:
        reference = 0
    return (package, reference)


def get_footprint_by_ref(board, ref):
    """get a footprint from the list of footprints by its Reference."""
    fps = []
    for fp in get_valid_footprints(board):
        if str(fp.GetReference()) == ref:
            fps.append(fp)
    return fps


def get_bit(value, bit):
    """Get the nth bit of a byte."""
    return value & (1 << bit)


def set_bit(value, bit):
    """Set the nth bit of a byte."""
    return value | (1 << bit)


def clear_bit(value, bit):
    """Clear the nth bit of a byte."""
    return value & ~(1 << bit)


def toggle_bit(value, bit):
    """Toggle the nth bit of a byte."""
    return value ^ (1 << bit)


def get_tht(footprint):
    """Get the THT property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, THT))


def get_smd(footprint):
    """Get the SMD property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, SMD))


def get_exclude_from_pos(footprint):
    """Get the 'exclude from POS' property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def get_exclude_from_bom(footprint):
    """Get the 'exclude from BOM' property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def get_not_in_schematic(footprint):
    """Get the 'not in schematic' property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    return bool(get_bit(val, NOT_IN_SCHEMATIC))


def set_tht(footprint):
    """Set the THT property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, THT)
    footprint.SetAttributes(val)
    return bool(get_bit(val, THT))


def set_smd(footprint):
    """Set the SMD property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = set_bit(val, SMD)
    footprint.SetAttributes(val)
    return bool(get_bit(val, SMD))


def set_exclude_from_pos(footprint, v):
    """Set the 'exclude from POS' property of a footprint."""
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
    """Set the 'exclude from BOM' property of a footprint."""
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
    """Set the 'not in schematic' property of a footprint."""
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
    """Toggle the THT property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, THT)
    footprint.SetAttributes(val)
    return bool(get_bit(val, THT))


def toggle_smd(footprint):
    """Toggle the SMD property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, SMD)
    footprint.SetAttributes(val)
    return bool(get_bit(val, SMD))


def toggle_exclude_from_pos(footprint):
    """Toggle the 'exclude from POS' property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_POS)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_POS))


def toggle_exclude_from_bom(footprint):
    """Toggle the 'exclude from BOM' property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, EXCLUDE_FROM_BOM)
    footprint.SetAttributes(val)
    return bool(get_bit(val, EXCLUDE_FROM_BOM))


def toggle_not_in_schematic(footprint):
    """Toggle the 'not in schematic' property of a footprint."""
    if not footprint:
        return
    val = footprint.GetAttributes()
    val = toggle_bit(val, NOT_IN_SCHEMATIC)
    footprint.SetAttributes(val)
    return bool(get_bit(val, NOT_IN_SCHEMATIC))
