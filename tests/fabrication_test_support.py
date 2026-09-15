"""Isolated production placement modules and board builders for export tests."""

from __future__ import annotations

from collections.abc import Iterator
import csv
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.correction_test_support import make_library
from tests.wx_harness import load_correction_modules, module


@dataclass
class Point:
    """Represent KiCad coordinates without rounding away offset assertions."""

    x: float
    y: float

    def __sub__(self, other: Point) -> Point:
        """Subtract the board auxiliary origin."""
        return Point(self.x - other.x, self.y - other.y)


@pytest.fixture
def modules() -> Iterator[SimpleNamespace]:
    """Keep real storage and placement modules registered with one set of doubles."""
    package = "fabrication_correction_recovery_tests"
    pcbnew = MagicMock()
    pcbnew.FromMM = lambda value: value
    pcbnew.ToMM = lambda value: value
    pcbnew.wxPoint = Point
    pcbnew.VECTOR2I = Point
    with load_correction_modules(
        package=package,
        pcbnew=pcbnew,
        names=("fabrication",),
        replacements={
            f"{package}.footprint_helpers": module(
                f"{package}.footprint_helpers", get_is_dnp=lambda _footprint: False
            )
        },
    ) as loaded:
        yield loaded


@pytest.fixture
def library(modules: SimpleNamespace, tmp_path: Path) -> Any:
    """Create actual SQLite correction storage away from user databases."""
    return make_library(modules.library, tmp_path)


def make_footprint(
    reference: str, layer: int, rotation: float, position: Point
) -> SimpleNamespace:
    """Expose the board data used by the production placement calculations."""
    return SimpleNamespace(
        GetReference=lambda: reference,
        GetValue=lambda: "Device",
        GetLayer=lambda: layer,
        GetOrientation=lambda: SimpleNamespace(AsDegrees=lambda: float(rotation)),
        GetFPID=lambda: SimpleNamespace(GetLibItemName=lambda: "Package:Device"),
        Pads=lambda: [],
        GetPosition=lambda: position,
    )


def make_fabrication(modules: SimpleNamespace, library: Any, tmp_path: Path) -> Any:
    """Create a real generator for one top and one bottom footprint."""
    footprints = [
        make_footprint("U1", 0, 0, Point(10, 20)),
        make_footprint("U2", 31, 90, Point(30, 40)),
    ]
    board = SimpleNamespace(
        GetFileName=lambda: str(tmp_path / "board.kicad_pcb"),
        GetDesignSettings=MagicMock(
            return_value=SimpleNamespace(GetAuxOrigin=lambda: Point(1, 2))
        ),
        Footprints=MagicMock(return_value=footprints),
    )
    parts = {
        reference: {
            "reference": reference,
            "value": "Device",
            "footprint": "Package:Device",
            "exclude_from_pos": 0,
            "lcsc": "C123",
        }
        for reference in ("U1", "U2")
    }
    parent = SimpleNamespace(
        library=library,
        settings={},
        store=SimpleNamespace(get_part=parts.get),
    )
    return modules.fabrication.Fabrication(parent, board)


def read_cpl(fabrication: Any) -> list[dict[str, str]]:
    """Read the actual generated CSV through a fresh file handle."""
    with Path(fabrication.get_cpl_csv_path()).open(newline="") as stream:
        return list(csv.DictReader(stream))
