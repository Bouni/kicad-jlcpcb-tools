"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin, GetBuildVersion  # pylint: disable=import-error
import wx

from .core.version import is_supported_version


class JLCPCBPlugin(ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self):
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.description = (
            "Generate JLCPCB-compatible Gerber, Excellon, BOM and CPL files"
        )
        self.show_toolbar_button = True
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self._pcbnew_frame = None

    def Run(self) -> None:
        """Overwrite Run."""
        if not is_supported_version(GetBuildVersion()):
            wx.MessageBox(
                "JLCPCB Tools requires KiCad 7.0 or newer.",
                "Unsupported KiCad version",
                wx.OK | wx.ICON_ERROR,
            )
            return

        from .mainwindow import JLCPCBTools  # noqa: PLC0415

        try:
            dialog = JLCPCBTools(None)
        except Exception as exc:
            wx.MessageBox(str(exc), "JLCPCB Tools", wx.OK | wx.ICON_ERROR)
            return
        dialog.Center()
        dialog.Show()
