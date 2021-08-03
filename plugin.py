import csv
import logging
import os
import sys

import pcbnew


class JLCPCBPlugin(pcbnew.ActionPlugin):
    def __init__(self):
        super(JLCPCBPlugin, self).__init__()

        self.name = "JLCPCB Tools"
        self.category = "Pick and Place geneartion"
        self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
        self.show_toolbar_button = True
        path, filename = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self.description = "Generate a JLCPCB conform CPL file"

    def setup(self):
        self.board = pcbnew.GetBoard()
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.InitLogger()
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def decode_attributes(footprint):
        attributes = {}
        val = footprint.GetAttributes()
        attributes["tht"] = bool(val & 0b1)
        attributes["smd"] = bool(val & 0b10)
        attributes["not_in_schematic"] = bool(val & 0b100)
        attributes["exclude_from_pos"] = bool(val & 0b1000)
        attributes["exclude_from_bom"] = bool(val & 0b1000)
        attributes["other"] = not (attributes["tht"] or attributes["smd"])
        return attributes

    def generate_cpl(self):
        cplname = f"CPL-{self.filename.split('.')[0]}.csv"
        with open(os.path.join(self.path, cplname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(
                self.board.GetFootprints(), key=lambda fp: fp.GetReference()
            )
            for footprint in footprints:
                attributes = self.decode_attributes(footprint)
                if attributes.get("exclude_from_pos"):
                    self.logger.info(
                        f"{footprint.GetReference()} is marked as 'exclude from POS' and skipped!"
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

    def generate_bom(self):
        bomname = f"BOM-{self.filename.split('.')[0]}.csv"
        with open(os.path.join(self.path, bomname), "w", newline="") as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC"])
            footprints = {}
            for footprint in self.board.GetFootprints():
                attributes = self.decode_attributes(footprint)
                if attributes.get("exclude_from_bom"):
                    self.logger.info(
                        f"{footprint.GetReference()} is marked as 'exclude from BOM' and skipped!"
                    )
                    continue
                lcsc = footprint.GetProperties().get("LCSC")
                if not lcsc:
                    self.logger.error(
                        f"{footprint.GetReference()} has no LCSC attribute and skipped!"
                    )
                    continue
                if not lcsc in footprints:
                    footprints[lcsc] = {
                        "comment": footprint.GetValue(),
                        "designators": [footprint.GetReference()],
                        "footprint": footprint.GetFPID().GetLibItemName(),
                    }
                else:
                    footprints[lcsc]["designators"].append(footprint.GetReference())
            for lcsc, data in footprints.items():
                writer.writerow(
                    [
                        data["comment"],
                        ",".join(data["designators"]),
                        data["footprint"],
                        lcsc,
                    ]
                )

    def Run(self):
        self.setup()
        self.generate_cpl()
        self.generate_bom()

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
