import os

from pcbnew import ActionPlugin

from .mainwindow import JLCPCBTools


class JLCPCBPlugin(ActionPlugin):
    def defaults(self):
        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.description = (
            "Generate JLCPCB-compatible Gerber, Excellon, BOM and CPL files"
        )
        self.show_toolbar_button = True
        path, filename = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self._pcbnew_frame = None

    def Run(self):
        dialog = JLCPCBTools(None)
        dialog.Center()
        dialog.Show()
