"""Contains helper function used all over the plugin."""

import os
from pathlib import Path
import re
from collections.abc import Iterable
from typing import Any, cast

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

PLUGIN_PATH = Path(__file__).resolve().parent

EXCLUDE_FROM_POS = 2
EXCLUDE_FROM_BOM = 3


def getWxWidgetsVersion():
    """Get wx widgets version."""
    v = re.search(r"wxWidgets\s([\d\.]+)", wx.version())
    if v is None:
        return 0
    v = int(v.group(1).replace(".", ""))
    return v


def getVersion():
    """READ Version from file."""
    if not os.path.isfile(os.path.join(PLUGIN_PATH, "VERSION")):
        return "unknown"
    with open(os.path.join(PLUGIN_PATH, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def GetOS():
    """Get String with OS type."""
    return wx.PlatformInformation.Get().GetOperatingSystemIdName()


def GetScaleFactor(window):
    """Workaround if wxWidgets Version does not support GetDPIScaleFactor, for Mac OS always return 1.0."""
    if "Apple Mac OS" in GetOS():
        return 1.0
    if hasattr(window, "GetDPIScaleFactor"):
        return window.GetDPIScaleFactor()
    return 1.0


def HighResWxSize(window, size):
    """Workaround if wxWidgets Version does not support FromDIP."""
    if hasattr(window, "FromDIP"):
        return window.FromDIP(size)
    return size


def loadBitmapScaled(filename, scale=1.0, static=False):
    """Load a scaled bitmap, handle differences between Kicad versions."""
    if filename:
        path = os.path.join(PLUGIN_PATH, "icons", filename)
        bmp = wx.Bitmap(path)
        w, h = bmp.GetSize()
        img = bmp.ConvertToImage()
        if hasattr(wx.SystemSettings, "GetAppearance") and hasattr(
            wx.SystemSettings.GetAppearance, "IsUsingDarkBackground"
        ):
            if wx.SystemSettings.GetAppearance().IsUsingDarkBackground():
                img.Replace(0, 0, 0, 255, 255, 255)
            bmp = wx.Bitmap(img.Scale(int(w * scale), int(h * scale)))
    else:
        bmp = wx.Bitmap()
    if getWxWidgetsVersion() > 315 and not static:
        return wx.BitmapBundle(bmp)
    return bmp


def loadIconScaled(filename, scale=1.0):
    """Load a scaled icon, handle differences between Kicad versions."""
    bmp = loadBitmapScaled(filename, scale=scale, static=False)
    if getWxWidgetsVersion() > 315:
        return bmp
    return wx.Icon(bmp)


def natural_sort_collation(a, b):
    """Natural sort collation for use in sqlite."""
    if a == b:
        return 0

    def convert(text):
        return int(text) if text.isdigit() else text.lower()

    def alphanum_key(key):
        return [convert(c) for c in re.split("([0-9]+)", key)]

    natorder = sorted([a, b], key=alphanum_key)
    return -1 if natorder.index(a) == 0 else 1


def dict_factory(cursor, row) -> dict:
    """Row factory that returns a dict."""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def get_lcsc_value(fp: Any) -> str:
    """Get the first LCSC number from a footprint-like object.

    Supports both KiCad SWIG footprints and compatible test/adapter stubs.
    """
    # KiCad 7.99
    try:
        for field in fp.GetFields():
            if re.match(r"lcsc|jlc", field.GetName(), re.IGNORECASE) and re.match(
                r"^C\d+$", field.GetText()
            ):
                return field.GetText()
    # KiCad <= V7
    except AttributeError:
        for key, value in fp.GetProperties().items():
            if re.match(r"lcsc|jlc", key, re.IGNORECASE) and re.match(r"^C\d+$", value):
                return value
    return ""


def _iter_footprints(board: Any):
    """Return an iterable of footprints from either a board or board adapter."""
    if hasattr(board, "get_all_footprints") and callable(board.get_all_footprints):
        return cast(Iterable[Any], board.get_all_footprints())
    if hasattr(board, "GetFootprints") and callable(board.GetFootprints):
        return cast(Iterable[Any], board.GetFootprints())
    if hasattr(board, "Footprints") and callable(board.Footprints):
        return cast(Iterable[Any], board.Footprints())
    raise TypeError(
        "board must expose get_all_footprints(), GetFootprints(), or Footprints()"
    )


def get_valid_footprints(board: Any) -> list:
    """Get all footprints that have a valid reference.

    Drop all REF** for example
    Drop kibuzzard footprints (length check)

    The input can be either:
    - A KiCad SWIG board object
    - A board adapter exposing `get_all_footprints()`
    """
    footprints = []
    for fp in _iter_footprints(board):
        if re.match(r"[\w\d-]+", fp.GetReference()):
            footprints.append(fp)
    return footprints


def get_exclude_from_pos(footprint: Any):
    """Get the 'exclude from POS' property of a footprint-like object."""
    if not footprint:
        return None
    val = footprint.GetAttributes()
    return bool(val & (1 << EXCLUDE_FROM_POS))


def get_exclude_from_bom(footprint: Any):
    """Get the 'exclude from BOM' property of a footprint-like object."""
    if not footprint:
        return None
    val = footprint.GetAttributes()
    return bool(val & (1 << EXCLUDE_FROM_BOM))
