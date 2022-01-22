import os

from pcbnew import ActionPlugin

from .mainwindow import JLCBCBTools


class JLCPCBPlugin(ActionPlugin):
    def __init__(self):
        super(JLCPCBPlugin, self).__init__()
        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
        self.show_toolbar_button = True
        path, filename = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self.description = "Generate JLCPCB conform Gerber, Excellon, BOM and CPL files"

    def Run(self):
        dialog = JLCBCBTools(None)
        dialog.Center()
        dialog.Show()
