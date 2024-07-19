"""Entry point for running the plugin in standalone mode."""

import wx

from . import standalone_impl
from .mainwindow import JLCPCBTools

if __name__ == "__main__":
    print("starting jlcpcbtools standalone mode...")  # noqa: T201

    # See README.md for how to use this

    app = wx.App(None)

    dialog = JLCPCBTools(None, kicad_provider=standalone_impl.KicadStub())
    dialog.Center()
    dialog.Show()

    app.MainLoop()
