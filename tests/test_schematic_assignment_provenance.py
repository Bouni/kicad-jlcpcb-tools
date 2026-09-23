"""Preserve native assignment meaning through ordinary and variant auto-save."""

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .store_board_support import (
    Board,
    Footprint,
    make_store,
    store_module as _store_module,
)
from .test_schematicexport import _load_schematic, _schematic
from .variant_data_support import SavedNativeBoard, _native_session

store_module = _store_module


class AssignmentFootprint(Footprint):
    """Expose the actual raw fields, with counted native reads."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__()
        self.fields = fields
        self.field_reads = 0

    def GetFields(self) -> list[Any]:
        """Read each alias's name and current text exactly as native controls do."""
        self.field_reads += 1
        return [
            SimpleNamespace(
                GetName=lambda name=name: name,
                GetText=lambda name=name: self.fields[name],
            )
            for name in self.fields
        ]


def _rows(
    tmp_path: Path, module: ModuleType, mode: str, fields: dict[str, str]
) -> list[dict[str, Any]]:
    """Use the real source adapter and snapshot path for the chosen UI mode."""
    if mode == "ordinary":
        footprint = AssignmentFootprint(fields)
        rows = make_store(module, tmp_path, Board(footprint)).read_all()
        assert footprint.field_reads == 1
        return rows
    board = SavedNativeBoard(tmp_path)
    board.parts[0].fields = {"Reference": "R1", "Value": "10k", **fields}
    _, session, store, _, _ = _native_session(tmp_path, board)
    return store.assembly_rows(session.snapshot, "")


@pytest.mark.parametrize("mode", ["ordinary", "variant"])
@pytest.mark.parametrize("version", [7, 8])
@pytest.mark.parametrize(
    "fields,status",
    [
        ({"LCSC": "invalid"}, "invalid"),
        ({"LCSC": "C100", "JLCPCB Part Number": "C200"}, "conflict"),
        ({"LCSC": "", "JLCPCB Part Number": "C200"}, "conflict"),
    ],
)
def test_unresolved_native_assignments_block_all_schematic_writes(
    tmp_path: Path,
    store_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    version: int,
    fields: dict[str, str],
    status: str,
) -> None:
    """An unresolved native row is never mistaken for an intentional clear."""
    rows = _rows(tmp_path, store_module, mode, fields)
    paths = [tmp_path / f"sheet{index}.kicad_sch" for index in range(2)]
    original = _schematic(version, "yes", ("R1",), reference="R1")
    for path in paths:
        path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match=f"R1.*{status}"):
        _load_schematic(tmp_path, monkeypatch, version, paths, rows)

    assert rows[0]["assignment_status"] == status
    for path in paths:
        assert path.read_text(encoding="utf-8") == original
        assert not path.with_suffix(".kicad_sch_old").exists()


@pytest.mark.parametrize("mode", ["ordinary", "variant"])
@pytest.mark.parametrize("version", [7, 8])
@pytest.mark.parametrize(
    "fields,status,lcsc",
    [
        ({"LCSC": " C200 "}, "valid", "C200"),
        ({"LCSC": ""}, "empty", ""),
        ({}, "missing", ""),
    ],
)
def test_resolved_native_assignment_states_save_consistently_across_modes(
    tmp_path: Path,
    store_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    version: int,
    fields: dict[str, str],
    status: str,
    lcsc: str,
) -> None:
    """Valid assignments and intentional absence follow the same save path."""
    rows = _rows(tmp_path, store_module, mode, fields)
    assert rows[0]["assignment_status"] == status
    path = tmp_path / "board.kicad_sch"
    path.write_text(
        _schematic(version, "yes", ("R1",), reference="R1"), encoding="utf-8"
    )

    _load_schematic(tmp_path, monkeypatch, version, [path], rows)

    assert f'(property "LCSC" "{lcsc}"' in path.read_text(encoding="utf-8")
