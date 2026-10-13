"""A board the tests edit freely, read by the real Store through the fake backend."""

from collections.abc import Iterator
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

import pytest

from .wx_harness import load_siblings, wx_stubs


class Footprint:
    """One footprint whose fields and flags a test changes between reads."""

    def __init__(self, reference: str = "R1", lcsc: str = "C100") -> None:
        self.reference = reference
        self.lcsc = lcsc
        self.value = "10k"
        self.footprint = "R_0603"
        self.attributes = 0  # KiCad's bits: 8 excludes from the BOM, 4 from placement
        self.dnp = False
        self.pads: list[Any] = []
        self.fields: Optional[dict[str, str]] = None  # noqa: UP045

    def record(self, fake: ModuleType, uuid: str) -> Any:
        """Return the footprint as the backend reports it now."""
        return fake.component(
            self.reference,
            uuid=uuid,
            value=self.value,
            footprint=f"Lib:{self.footprint}",
            fields=self.fields if self.fields is not None else {"LCSC": self.lcsc},
            dnp=self.dnp,
            exclude_from_bom=bool(self.attributes & 8),
            exclude_from_pos=bool(self.attributes & 4),
            pads=self.pads,
        )


class Board:
    """The footprints of an open board, in board order."""

    def __init__(self, *footprints: Footprint) -> None:
        self.footprints = list(footprints)


class BoardParent:
    """The window's side of the Store: each read builds a fresh snapshot.

    ``reads`` and ``held_reads`` count the full reads and the lookups answered
    from the last snapshot.
    """

    def __init__(
        self, fake: ModuleType, board: Board, settings: dict[str, Any]
    ) -> None:
        self.fake = fake
        self.board = board
        self.settings = settings
        self.reads = 0
        self.held_reads = 0

    def _snapshot(self) -> Any:
        """Read the board as the backend would now."""
        return self.fake.FakeBackend(
            footprint.record(self.fake, f"uuid-{id(footprint)}")
            for footprint in self.board.footprints
        ).snapshot()

    def _read_board(self) -> Any:
        """Read the whole board again."""
        self.reads += 1
        return self._snapshot()

    def _held_board(self) -> Any:
        """Answer a lookup; these tests' boards change between any two reads."""
        self.held_reads += 1
        return self._snapshot()


@pytest.fixture
def store_module() -> Iterator[ModuleType]:
    """Load the real Store with only unused wx import boundaries replaced."""
    with load_siblings(
        "_ordinary_board_store_tests", ("store", "kicad_backend.fake"), wx_stubs()
    ) as modules:
        yield modules["store"]


def make_store(module: ModuleType, path: Path, board: Board, **settings: Any) -> Any:
    """Run the real constructor with a parent reading ``board``."""
    fake = importlib.import_module(f"{module.__package__}.kicad_backend.fake")
    return module.Store(BoardParent(fake, board, settings), str(path))
