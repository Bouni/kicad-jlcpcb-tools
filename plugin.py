"""Contains the Action Plugin."""

import os
import logging

from pcbnew import ActionPlugin  # pylint: disable=import-error

from .kicad_api import KicadProvider
from .mainwindow import JLCPCBTools

logger = logging.getLogger(__name__)


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

    def Run(self):
        """Overwrite Run."""
        try:
            # Initialize KiCad adapter set
            adapter_set = KicadProvider.create_adapter_set()
            logger.info("KiCad adapters initialized successfully")

            # Create and show main dialog with adapters
            dialog = JLCPCBTools(None, adapter_set=adapter_set)
        except Exception as e:
            logger.exception("Failed to initialize JLCPCB Tools: %s", str(e))
            raise
        dialog.Center()
        dialog.Show()
