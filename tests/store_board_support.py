"""Stateful native board boundary for ordinary part-data tests."""

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Optional

import pytest

from .wx_harness import load_siblings, wx_stubs


class Footprint:
    """Retain editable fields and flags read through real footprint helpers."""

    def __init__(self, reference: str = "R1", lcsc: str = "C100") -> None:
        self.reference = reference
        self.lcsc = lcsc
        self.value = "10k"
        self.footprint = "R_0603"
        self.attributes = 0
        self.dnp = False
        self.pads: list[Any] = []
        self.pad_reads = 0

    def GetReference(self) -> str:
        """Return the current reference."""
        return self.reference

    def GetValue(self) -> str:
        """Return the current value."""
        return self.value

    def GetFPID(self) -> Any:
        """Return the current library item."""
        return SimpleNamespace(GetLibItemName=lambda: self.footprint)

    def GetFields(self) -> list[Any]:
        """Return the currently assigned supplier field."""
        return [SimpleNamespace(GetName=lambda: "LCSC", GetText=lambda: self.lcsc)]

    def GetAttributes(self) -> int:
        """Return the editable exclusion flags."""
        return self.attributes

    def IsDNP(self) -> bool:
        """Return the editable population flag."""
        return self.dnp

    def Pads(self) -> list[Any]:
        """Return the current electrical and mechanical pads."""
        self.pad_reads += 1
        return self.pads


class Board:
    """Retain additions, renames and removals across Store reads."""

    def __init__(self, *footprints: Footprint) -> None:
        self.footprints = list(footprints)
        self.inventory_reads = 0
        self.reference_lookups: list[str] = []

    def GetFootprints(self) -> list[Footprint]:
        """Return the live footprint collection."""
        self.inventory_reads += 1
        return self.footprints

    def FindFootprintByReference(self, reference: str) -> Optional[Footprint]:
        """Resolve the current native reference without reading each part's data."""
        self.reference_lookups.append(reference)
        return next((fp for fp in self.footprints if fp.reference == reference), None)


@pytest.fixture
def store_module() -> Iterator[ModuleType]:
    """Load the real Store with only unused wx import boundaries replaced."""
    with load_siblings(
        "_ordinary_board_store_tests", ("store",), wx_stubs()
    ) as modules:
        yield modules["store"]


def make_store(module: ModuleType, path: Path, board: Board, **settings: Any) -> Any:
    """Run the real constructor against mutable native state."""
    return module.Store(SimpleNamespace(settings=settings), str(path), board)
