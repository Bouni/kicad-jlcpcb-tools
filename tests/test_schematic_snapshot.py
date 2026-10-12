"""Capture identity and explicit-clear provenance without store or catalog reads."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .wx_harness import load_siblings, module


@pytest.fixture
def modules() -> Iterator[dict[str, Any]]:
    """Load the capture, the exporter and the fake backend under one package."""
    with load_siblings(
        "_snapshot_tests",
        ("schematic_snapshot", "schematicexport", "kicad_backend.fake"),
        {"pcbnew": module("pcbnew", GetBuildVersion=lambda: "10.0")},
    ) as loaded:
        yield loaded


def _board(modules: dict[str, Any], *components: Any) -> Any:
    """Read a board holding ``components``."""
    return modules["kicad_backend.fake"].FakeBackend(components).snapshot()


def _footprint(
    modules: dict[str, Any], fields: dict[str, str], identity: str = "pcb-1"
) -> Any:
    """Build a footprint excluded from the BOM and linked to a placed symbol."""
    return modules["kicad_backend.fake"].component(
        "R1",
        uuid=identity,
        fields=fields,
        exclude_from_bom=True,
        schematic_path="/root/sheet/symbol",
    )


@pytest.mark.parametrize(
    "fields,status,value",
    [
        ({}, "missing", None),
        ({"LCSC": ""}, "empty", ""),
        ({"LCSC": " C100 "}, "valid", "C100"),
        ({"LCSC": "garbage"}, "invalid", None),
        ({"LCSC": "", "LCSC PartNr": "C200"}, "conflict", None),
        ({"LCSC": "C100", "JLCPCB Part #": "C200"}, "conflict", None),
        ({"LCSC": "C100", "JLCPCB Part #": "   "}, "valid", None),
    ],
)
def test_native_capture_detaches_identity_fields_and_bom_once(
    modules: dict[str, Any],
    fields: dict[str, str],
    status: str,
    value: Any,
) -> None:
    """Missing and whitespace cannot masquerade as an explicit clear or value."""
    snapshot = modules["schematic_snapshot"].capture_board(
        _board(modules, _footprint(modules, fields))
    )
    record = snapshot.parts[0]
    assert (record.component_id, record.reference) == ("pcb-1", "R1")
    assert (record.assignment.status, record.value) == (status, value)
    assert record.schematic_path == "/root/sheet/symbol"
    assert record.exclude_from_bom is True
    with pytest.raises(FrozenInstanceError):
        record.lcsc = "C999"


@pytest.mark.parametrize("alias", ["LCSCPartNr", "LCSC Part Nr", "JLCPCB PartNr"])
def test_historical_partnr_aliases_are_occupied_native_fields(
    modules: dict[str, Any], alias: str
) -> None:
    """An older supported field prevents migration from treating a part as missing."""
    snapshot = modules["schematic_snapshot"].capture_board(
        _board(modules, _footprint(modules, {alias: "C123"}))
    )
    assert snapshot.parts[0].assignment.status == "valid"
    assert snapshot.parts[0].value == "C123"


def test_duplicate_references_keep_distinct_native_identities(
    modules: dict[str, Any],
) -> None:
    """Stale annotation labels cannot make linked source capture ambiguous."""
    footprints = [
        _footprint(modules, {"LCSC": "C123"}, f"pcb-{index}") for index in (1, 2)
    ]
    snapshot = modules["schematic_snapshot"].capture_board(_board(modules, *footprints))
    assert [part.component_id for part in snapshot.parts] == ["pcb-1", "pcb-2"]
    assert [part.reference for part in snapshot.parts] == ["R1", "R1"]


def test_footprints_sharing_a_uuid_are_not_captured(modules: dict[str, Any]) -> None:
    """The schematic links footprints by UUID, so two with one are refused."""
    board = _board(
        modules,
        _footprint(modules, {"LCSC": "C100"}, "pcb-1"),
        _footprint(modules, {"LCSC": "C200"}, "pcb-1"),
    )

    with pytest.raises(ValueError, match="requires unique footprint UUIDs"):
        modules["schematic_snapshot"].capture_board(board)


def test_a_footprint_with_two_fields_of_one_name_is_not_captured(
    modules: dict[str, Any], tmp_path: Path
) -> None:
    """Which field the schematic should receive is ambiguous, so nothing is written.

    The capture names the first name that repeats, in KiCad's field order.
    """
    path = tmp_path / "board.kicad_sch"
    text = (
        '(kicad_sch (uuid "root") (symbol (lib_id "Device:R") '
        '(uuid "symbol") (in_bom yes) (property "Reference" "R2") '
        '(property "LCSC" "C900")))'
    )
    path.write_text(text, encoding="utf-8")
    repeated = modules["kicad_backend.fake"].component(
        "R2",
        uuid="pcb-2",
        fields=(("LCSC", "C1"), ("MPN", "X"), ("MPN", "Y"), ("LCSC", "C2")),
        schematic_path="/root/symbol",
    )
    board = _board(modules, _footprint(modules, {"LCSC": "C100"}), repeated)

    with pytest.raises(ValueError, match="^Duplicate Default PCB field 'MPN'$"):
        modules["schematic_snapshot"].capture_board(board)
    parent = SimpleNamespace(
        _read_board=lambda: board,
        board_name="board.kicad_pcb",
        project_path=str(tmp_path),
    )
    exporter = modules["schematicexport"].SchematicExport(parent)
    with pytest.raises(ValueError, match="Duplicate Default PCB field 'MPN'"):
        exporter.load_schematic([str(path)])
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("source", ["capture", "parts", "snapshot"])
def test_an_export_checks_the_board_once_and_reads_it_only_to_capture(
    modules: dict[str, Any], source: str
) -> None:
    """A capture reads the board, which checks it; supplied records only check it.

    In variant mode the window holds no snapshot, so a check that read the
    board as well would read it twice.
    """
    calls = []
    footprint = _footprint(modules, {"LCSC": "C100"})

    def read_board() -> Any:
        calls.append("read")
        return _board(modules, footprint)

    parent = SimpleNamespace(
        _read_board=read_board, _legacy_board=lambda: calls.append("check")
    )
    exporter = modules["schematicexport"].SchematicExport(parent)
    captured = modules["schematic_snapshot"].capture_board(_board(modules, footprint))
    supplied = {
        "capture": {},
        "parts": {"parts": []},
        "snapshot": {"snapshot": captured},
    }[source]

    exporter._snapshot(supplied.get("parts"), supplied.get("snapshot"))

    assert calls == (["read"] if source == "capture" else ["check"])


def test_live_whitespace_alias_preserves_schematic_and_explains_the_alias_problem(
    modules: dict[str, Any], tmp_path: Path
) -> None:
    """Raw native alias state survives export even when the resolved status is valid."""
    path = tmp_path / "board.kicad_sch"
    path.write_text(
        '(kicad_sch (uuid "root") (symbol (lib_id "Device:R") '
        '(uuid "symbol") (in_bom yes) (property "Reference" "R1") '
        '(property "LCSC" "C900")))',
        encoding="utf-8",
    )
    footprint = modules["kicad_backend.fake"].component(
        "R1",
        uuid="pcb-1",
        fields={"LCSC": "C100", "JLCPCB Part #": "   "},
        exclude_from_bom=True,
        schematic_path="/root/symbol",
    )
    parent = SimpleNamespace(
        _read_board=lambda: _board(modules, footprint),
        board_name="board.kicad_pcb",
        project_path=str(tmp_path),
    )
    exporter = modules["schematicexport"].SchematicExport(parent)
    result = exporter.load_schematic([str(path)])
    assert '(property "LCSC" "C900")' in path.read_text(encoding="utf-8")
    assert "(in_bom no)" in path.read_text(encoding="utf-8")
    assert result.skipped == ("pcb-1",)
    assert not result.retirement_eligible
    assert any("pcb-1" in text and "whitespace" in text for text in result.diagnostics)
