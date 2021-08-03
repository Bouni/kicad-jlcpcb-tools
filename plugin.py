import csv
import logging
import os
import sys

import pcbnew


class JLCPCBPlugin(pcbnew.ActionPlugin):
    def __init__(self):
        super(JLCPCBPlugin, self).__init__()
        self.InitLogger()
        self.logger = logging.getLogger(__name__)
        self.name = "JLCPCB Tools"
        self.category = "Pick and Place geneartion"
        self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
        self.show_toolbar_button = True
        path, filename = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self.description = "Generate a JLCPCB conform CPL file"

    def generate_bom(self):
        pass

    @staticmethod
    def is_smd_footprint(footprint):
        return footprint.GetAttributes() == 2

    def generate_cpl(self):
        board = pcbnew.GetBoard()
        path, filename = os.path.split(board.GetFileName())
        cplname = f"CPL-{filename.split('.')[0]}.csv"
        with open(os.path.join(path, cplname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(board.GetFootprints(), key=lambda fp: fp.GetReference())
            for footprint in footprints:
                if not self.is_smd_footprint(footprint):
                    self.logger.info(
                        f"{footprint.GetReference()} is no SMD footprint and therefore skipped!"
                    )
                    continue
                writer.writerow(
                    [
                        footprint.GetReference(),
                        footprint.GetValue(),
                        footprint.GetFPID().GetLibItemName(),
                        pcbnew.ToMM(footprint.GetPosition().x),
                        pcbnew.ToMM(footprint.GetPosition().y),
                        footprint.GetOrientation() / 10,
                        "top" if footprint.GetLayer() == 0 else "bottom",
                    ]
                )

    def Run(self):
        self.generate_cpl()

    def InitLogger(self):
        # Remove all handlers associated with the root logger object.
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        log_file = os.path.join(self.path, "jlcpcb.log")

        # set up logger
        logging.basicConfig(
            level=logging.DEBUG,
            filename=log_file,
            filemode="w",
            format="%(asctime)s %(name)s %(lineno)d:%(message)s",
            datefmt="%m-%d %H:%M:%S",
        )

        stdout_logger = logging.getLogger("STDOUT")
        sl_out = StreamToLogger(stdout_logger, logging.INFO)
        sys.stdout = sl_out

        stderr_logger = logging.getLogger("STDERR")
        sl_err = StreamToLogger(stderr_logger, logging.ERROR)
        sys.stderr = sl_err


class StreamToLogger(object):
    """
    Fake file-like stream object that redirects writes to a logger instance.
    """

    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.linebuf = ""

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            self.logger.log(self.log_level, line.rstrip())

    def flush(self, *args, **kwargs):
        """No-op for wrapper"""
        pass


JLCPCBPlugin().register()
