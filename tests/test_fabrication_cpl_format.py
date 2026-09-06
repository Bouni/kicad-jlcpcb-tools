"""Keep CPL coordinates in decimal millimetres for GitHub issue #769."""

import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from .wx_harness import load, module, package_stubs


class Point:
    """Represent KiCad's integer nanometre coordinates."""

    def __init__(self, x, y):
        self.x = int(x)
        self.y = int(y)

    def __sub__(self, other):
        """Subtract the board auxiliary origin."""
        return Point(self.x - other.x, self.y - other.y)


@pytest.fixture
def generate_cpl(tmp_path):
    """Run the real exporter with KiCad substitutes and temporary output."""
    package = "_cpl_format_test"
    stubs = package_stubs(package)
    stubs["pcbnew"] = MagicMock(
        ToMM=lambda value: value / 1_000_000,
        FromMM=lambda value: round(value * 1_000_000),
        wxPoint=Point,
        VECTOR2I=Point,
    )
    helpers = f"{package}.footprint_helpers"
    stubs[helpers] = module(helpers, get_is_dnp=lambda footprint: False)
    fabrication_module = load(package, "fabrication", stubs)

    def generate(position, *, layer=0, origin=(0, 0), offset=(0, 0)):
        """Return the CSV rows after actual origin and correction calculations."""
        footprint = SimpleNamespace(
            GetReference=lambda: "R1",
            GetValue=lambda: "10k",
            GetFPID=lambda: SimpleNamespace(GetLibItemName=lambda: "R_0603"),
            GetLayer=lambda: layer,
            GetOrientation=lambda: SimpleNamespace(AsDegrees=lambda: 0),
            Pads=lambda: [],
            GetPosition=lambda: Point(*position),
        )
        board = SimpleNamespace(
            GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
            GetDesignSettings=lambda: SimpleNamespace(
                GetAuxOrigin=lambda: Point(*origin)
            ),
            Footprints=lambda: [footprint],
        )
        part = {
            "reference": "R1",
            "value": "10k",
            "footprint": "R_0603",
            "exclude_from_pos": 0,
            "lcsc": "C123",
        }
        parent = SimpleNamespace(
            settings={},
            library=SimpleNamespace(
                get_all_correction_data=lambda: [("R1", 0, offset)]
            ),
            store=SimpleNamespace(get_part=lambda reference: part),
        )
        fabrication = fabrication_module.Fabrication(parent, board)
        fabrication.generate_cpl()
        with Path(fabrication.get_cpl_csv_path()).open(newline="") as stream:
            return list(csv.DictReader(stream))

    return generate


@pytest.mark.parametrize(
    "coordinate,expected_x,expected_y,layer",
    [
        (0, "0.000000", "-0.000000", 0),
        (1, "0.000001", "0.000001", 0),
        (-1, "-0.000001", "-0.000001", 31),
        (5, "0.000005", "0.000005", 0),
        (-5, "-0.000005", "-0.000005", 31),
        (50, "0.000050", "0.000050", 0),
        (-50, "-0.000050", "-0.000050", 31),
        (99, "0.000099", "0.000099", 0),
        (-99, "-0.000099", "-0.000099", 31),
        (100, "0.000100", "0.000100", 0),
        (-100, "-0.000100", "-0.000100", 31),
        (101, "0.000101", "0.000101", 0),
        (-101, "-0.000101", "-0.000101", 31),
        (12_345_678, "12.345678", "12.345678", 0),
        (-12_345_678, "-12.345678", "-12.345678", 31),
    ],
)
def test_cpl_coordinates_use_decimal_millimetres(
    generate_cpl, coordinate, expected_x, expected_y, layer
):
    """Preserve nanometre precision and Y inversion without exponent notation."""
    rows = generate_cpl((coordinate, -coordinate), layer=layer)

    assert rows == [
        {
            "Designator": "R1",
            "Val": "10k",
            "Package": "R_0603",
            "Mid X": expected_x,
            "Mid Y": expected_y,
            "Rotation": "0" if layer == 0 else "180",
            "Layer": "top" if layer == 0 else "bottom",
        }
    ]


@pytest.mark.parametrize(
    "position,origin,offset",
    [
        pytest.param(
            (10_000_005, 19_999_995),
            (10_000_000, 20_000_000),
            (0, 0),
            id="auxiliary-origin",
        ),
        pytest.param(
            (1_000_005, -2_000_005),
            (0, 0),
            (-1, 2),
            id="position-correction",
        ),
    ],
)
def test_cpl_formats_coordinates_after_transforms(
    generate_cpl, position, origin, offset
):
    """Origin subtraction and position correction can leave tiny residuals."""
    rows = generate_cpl(position, origin=origin, offset=offset)

    assert rows[0]["Mid X"] == "0.000005"
    assert rows[0]["Mid Y"] == "0.000005"
