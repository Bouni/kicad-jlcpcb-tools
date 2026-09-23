"""Arbitrary native reference text is data and never drives SQL cleanup."""

from pathlib import Path
from types import ModuleType

import pytest

from .store_board_support import (
    Board,
    Footprint,
    make_store,
    store_module as _store_module,
)
from .test_store_board_data import seed_legacy

store_module = _store_module


@pytest.mark.parametrize("reference", ["U1'A", "R1', 'C9"])
def test_reference_text_and_deletion_leave_legacy_database_untouched(
    tmp_path: Path, store_module: ModuleType, reference: str
) -> None:
    """Quoted references survive reads and disappear only when removed from the PCB."""
    path = seed_legacy(tmp_path)
    before = path.read_bytes()
    footprint = Footprint(reference)
    board = Board(footprint, Footprint("C9", "C900"))
    store = make_store(store_module, tmp_path, board)
    assert store.get_part(reference)["reference"] == reference
    assert len(store.read_all()) == 2
    board.footprints.remove(footprint)
    assert store.get_part(reference) is None
    assert [row["reference"] for row in store.read_all()] == ["C9"]
    board.footprints.clear()
    assert store.read_all() == []
    assert path.read_bytes() == before
