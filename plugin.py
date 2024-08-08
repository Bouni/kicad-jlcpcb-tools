"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin  # pylint: disable=import-error

from .mainwindow import JLCPCBTools


class JLCPCBPlugin(ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self):   # noqa: DC04
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.description = (
            "Generate JLCPCB-compatible Gerber, Excellon, BOM and CPL files"
        )
        self.show_toolbar_button = True  # noqa: DC05
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")  # noqa: DC05
        self._pcbnew_frame = None  # noqa: DC05

    def Run(self):  # noqa: DC04
        """Overwrite Run."""
        dialog = JLCPCBTools(None)
        dialog.Center()
        dialog.Show()
