"""Parity tests for fabrication BOM/CPL generation across adapter backends."""

import csv
from pathlib import Path

from fabrication import Fabrication


class _Point:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __iter__(self):
        yield self.x
        yield self.y


class _PathBoard:
    def __init__(self, board_path: Path):
        self._path = board_path

    def GetFileName(self) -> str:
        return str(self._path)


class _FakeStore:
    def __init__(self):
        self._parts_by_ref = {
            "C1": {
                "reference": "C1",
                "value": "100n",
                "footprint": "C_0603",
                "exclude_from_pos": 0,
                "lcsc": "C200",
            },
            "R1": {
                "reference": "R1",
                "value": "10k",
                "footprint": "R_0603",
                "exclude_from_pos": 0,
                "lcsc": "C100",
            },
            "U1": {
                "reference": "U1",
                "value": "MCU",
                "footprint": "QFN-32",
                "exclude_from_pos": 1,
                "lcsc": "C300",
            },
        }

    def get_part(self, reference: str):
        return self._parts_by_ref.get(reference)

    def read_bom_parts(self):
        return [
            {
                "value": "Passives",
                "refs": "C1,R1",
                "footprint": "0603",
                "lcsc": "C100",
            },
            {
                "value": "MCU",
                "refs": "U1",
                "footprint": "QFN-32",
                "lcsc": "C300",
            },
        ]


class _FakeLibrary:
    @staticmethod
    def get_all_correction_data():
        return []


class _FakeParent:
    def __init__(self):
        self.settings = {"gerber": {"lcsc_bom_cpl": True}}
        self.store = _FakeStore()
        self.library = _FakeLibrary()


class _ObjectFootprint:
    def __init__(self, reference: str, value: str, package: str, layer: int, dnp: bool):
        self.reference = reference
        self.value = value
        self.package = package
        self.layer = layer
        self.dnp = dnp
        self.x = 10_000_000 if reference == "R1" else 20_000_000
        self.y = 5_000_000 if reference == "R1" else 15_000_000


class _DictBoardAdapter:
    def __init__(self, footprints):
        self._footprints = footprints

    def get_footprints(self):
        return list(self._footprints)

    @staticmethod
    def get_aux_origin():
        return _Point(0, 0)


class _ObjectBoardAdapter:
    def __init__(self, footprints):
        self._footprints = footprints

    def get_footprints(self):
        return list(self._footprints)

    @staticmethod
    def get_aux_origin():
        return _Point(0, 0)


class _DictFootprintAdapter:
    @staticmethod
    def get_reference(footprint):
        return footprint["reference"]

    @staticmethod
    def get_is_dnp(footprint):
        return bool(footprint["dnp"])

    @staticmethod
    def get_layer(footprint):
        return footprint["layer"]

    @staticmethod
    def get_position(footprint):
        return (footprint["x"], footprint["y"])

    @staticmethod
    def get_pads(_footprint):
        return []

    @staticmethod
    def get_orientation(_footprint):
        return 0.0

    @staticmethod
    def get_value(footprint):
        return footprint["value"]

    @staticmethod
    def get_fpid_name(footprint):
        return footprint["package"]


class _ObjectFootprintAdapter:
    @staticmethod
    def get_reference(footprint):
        return footprint.reference

    @staticmethod
    def get_is_dnp(footprint):
        return bool(footprint.dnp)

    @staticmethod
    def get_layer(footprint):
        return footprint.layer

    @staticmethod
    def get_position(footprint):
        return (footprint.x, footprint.y)

    @staticmethod
    def get_pads(_footprint):
        return []

    @staticmethod
    def get_orientation(_footprint):
        return 0.0

    @staticmethod
    def get_value(footprint):
        return footprint.value

    @staticmethod
    def get_fpid_name(footprint):
        return footprint.package


class _UtilityAdapter:
    @staticmethod
    def from_mm(value):
        return int(value * 1_000_000)

    @staticmethod
    def to_mm(value):
        return float(value) / 1_000_000.0

    @staticmethod
    def create_vector2i(x, y):
        return _Point(int(x), int(y))

    @staticmethod
    def create_wx_point(x, y):
        return _Point(int(x), int(y))


class _AdapterSet:
    def __init__(self, board, footprint):
        self.board = board
        self.footprint = footprint
        self.utility = _UtilityAdapter()


def _make_object_backend():
    footprints = [
        _ObjectFootprint("U1", "MCU", "QFN-32", 0, False),
        _ObjectFootprint("C1", "100n", "C_0603", 1, True),
        _ObjectFootprint("R1", "10k", "R_0603", 0, False),
    ]
    return _AdapterSet(_ObjectBoardAdapter(footprints), _ObjectFootprintAdapter())


def _make_dict_backend():
    footprints = [
        {
            "reference": "U1",
            "value": "MCU",
            "package": "QFN-32",
            "layer": 0,
            "dnp": False,
            "x": 20_000_000,
            "y": 15_000_000,
        },
        {
            "reference": "C1",
            "value": "100n",
            "package": "C_0603",
            "layer": 1,
            "dnp": True,
            "x": 20_000_000,
            "y": 15_000_000,
        },
        {
            "reference": "R1",
            "value": "10k",
            "package": "R_0603",
            "layer": 0,
            "dnp": False,
            "x": 10_000_000,
            "y": 5_000_000,
        },
    ]
    return _AdapterSet(_DictBoardAdapter(footprints), _DictFootprintAdapter())


def _read_csv_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def _run_bom_cpl_generation(tmp_path: Path, adapter_set):
    tmp_path.mkdir(parents=True, exist_ok=True)
    board_path = tmp_path / "fixture.kicad_pcb"
    board_path.write_text("", encoding="utf-8")

    parent = _FakeParent()
    board = _PathBoard(board_path)

    fabrication = Fabrication(parent, board, adapter_set=adapter_set)
    fabrication.generate_cpl()
    fabrication.generate_bom()

    stem = board_path.stem
    cpl_path = tmp_path / "jlcpcb" / "production_files" / f"CPL-{stem}.csv"
    bom_path = tmp_path / "jlcpcb" / "production_files" / f"BOM-{stem}.csv"

    return {
        "cpl": _read_csv_rows(cpl_path),
        "bom": _read_csv_rows(bom_path),
    }


def test_bom_cpl_parity_between_object_and_dict_backends(tmp_path):
    """BOM/CPL generation should be identical across adapter backend shapes."""
    object_result = _run_bom_cpl_generation(tmp_path / "object", _make_object_backend())
    dict_result = _run_bom_cpl_generation(tmp_path / "dict", _make_dict_backend())

    assert object_result == dict_result
    assert object_result["cpl"] == [
        ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"],
        ["R1", "10k", "R_0603", "10.0", "-5.0", "0.0", "top"],
    ]
    assert object_result["bom"] == [
        ["Comment", "Designator", "Footprint", "LCSC", "Quantity"],
        ["Passives", "R1", "0603", "C100", "1"],
        ["MCU", "U1", "QFN-32", "C300", "1"],
    ]
