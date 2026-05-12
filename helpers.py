"""Contains helper function used all over the plugin."""

import os
from pathlib import Path
import re

import wx  # pylint: disable=import-error
import wx.dataview  # pylint: disable=import-error

PLUGIN_PATH = Path(__file__).resolve().parent


def getWxWidgetsVersion():
    """Get wx widgets version."""
    v = re.search(r"wxWidgets\s([\d\.]+)", wx.version())
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
